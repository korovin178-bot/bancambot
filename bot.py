"""
Bandit.camp Rain Bot v4 — curl_cffi
Маскирует TLS-отпечаток под настоящий Chrome чтобы обойти Cloudflare.
Протокол: {"a": [событие, данные], "i": номер}
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from curl_cffi import requests as cffi
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
import asyncio

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHANNEL = os.environ["TELEGRAM_CHANNEL"]

CF_CLEARANCE = os.environ.get("CF_CLEARANCE", "")
USER_AGENT   = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
)

WS_URL   = "wss://api.bandit.camp/"
SITE_URL = "https://bandit.camp"
VALUE_DIVISOR = 100

# curl_cffi имперсонация — "chrome" = всегда последняя доступная версия
IMPERSONATE = os.environ.get("IMPERSONATE", "chrome")

# ─── State ────────────────────────────────────────────────────────────────────
@dataclass
class Rain:
    rain_key: str
    value: float
    user_count: int = 0
    duration_ms: int = 0
    started_at_ms: int = 0
    status: str = "active"
    msg_id: Optional[int] = None

active: dict[str, Rain] = {}
bot: Optional[Bot] = None
loop: Optional[asyncio.AbstractEventLoop] = None

# ─── Форматирование ───────────────────────────────────────────────────────────
def fmt(rain: Rain) -> str:
    if rain.status == "finished":
        header = "✅ <b>RAKEBACK RAIN — ЗАВЕРШЁН</b>"
        time_line = ""
    else:
        header = "🌧 <b>RAKEBACK RAIN — АКТИВЕН</b>"
        now_ms = int(time.time() * 1000)
        if rain.started_at_ms and rain.duration_ms:
            rem = max(0, rain.started_at_ms + rain.duration_ms - now_ms)
            rm, rs = divmod(rem // 1000, 60)
            time_line = f"⏳ <b>Осталось:</b> {rm}м {rs}с\n"
        else:
            time_line = ""
    participants = f"👥 <b>Участники:</b> {rain.user_count}\n" if rain.user_count > 0 else ""
    return (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Сумма:</b> <code>{rain.value:.2f}$</code>\n"
        f"{participants}{time_line}"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 <a href=\"{SITE_URL}\">Зайти на bandit.camp</a>"
    )

# ─── Telegram (вызывается из другого потока через loop) ──────────────────────
async def _send(rain: Rain):
    try:
        msg = await bot.send_message(
            chat_id=TELEGRAM_CHANNEL, text=fmt(rain),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        rain.msg_id = msg.message_id
        log.info(f"✉️  Отправлено: value={rain.value}$ users={rain.user_count}")
    except TelegramError as e:
        log.error(f"send error: {e}")

async def _edit(rain: Rain):
    if not rain.msg_id:
        await _send(rain); return
    try:
        await bot.edit_message_text(
            chat_id=TELEGRAM_CHANNEL, message_id=rain.msg_id, text=fmt(rain),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        log.info(f"✏️  Обновлено: value={rain.value}$ users={rain.user_count} status={rain.status}")
    except TelegramError as e:
        if "not modified" not in str(e).lower():
            log.error(f"edit error: {e}")

def send_threadsafe(coro):
    """Безопасно запускает корутину Telegram из WS-потока"""
    if loop:
        asyncio.run_coroutine_threadsafe(coro, loop)

# ─── Обработчики событий ──────────────────────────────────────────────────────
def on_rain(data: dict):
    started = int(data.get("startedAt") or 0)
    value   = float(data.get("value") or 0) / VALUE_DIVISOR
    users   = int(data.get("userCount") or 0)
    dur     = int(data.get("duration") or 0)
    key = str(started) if started else f"rain_{int(time.time())}"
    log.info(f"🌧 chat.rain: value={value}$ users={users} dur={dur}ms")

    rain = active.get(key)
    if rain is None:
        rain = Rain(rain_key=key, value=value, user_count=users,
                    duration_ms=dur, started_at_ms=started)
        active[key] = rain
        send_threadsafe(_send(rain))
    else:
        changed = (abs(rain.value - value) > 0.005) or (rain.user_count != users)
        rain.value = value; rain.user_count = users
        if changed:
            send_threadsafe(_edit(rain))

def on_rain_join(data: dict):
    users = int(data.get("userCount") or 0)
    if not active: return
    rain = max(active.values(), key=lambda r: r.started_at_ms)
    if rain.status == "active" and users and users != rain.user_count:
        rain.user_count = users
        send_threadsafe(_edit(rain))

def on_rain_payout(data: dict):
    users = int(data.get("userCount") or data.get("recipients") or 0)
    total = data.get("total") or data.get("value")
    log.info(f"✅ payoutSummary: users={users} total={total}")
    if not active: return
    rain = max(active.values(), key=lambda r: r.started_at_ms)
    if users: rain.user_count = users
    if total is not None: rain.value = float(total) / VALUE_DIVISOR
    rain.status = "finished"
    send_threadsafe(_edit(rain))

def route(event: str, data):
    if not isinstance(data, dict): data = {}
    if event == "chat.rain":
        on_rain(data)
    elif event == "chat.rain.join":
        on_rain_join(data)
    elif event in ("chat.rain.payoutSummary", "chat.rain.payout"):
        on_rain_payout(data)
    elif "rain" in event.lower():
        log.info(f"🔔 Прочее rain-событие: {event} | {str(data)[:120]}")

def handle_message(raw: str):
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(obj, dict):
        return
    a = obj.get("a")
    if isinstance(a, list) and len(a) >= 1 and isinstance(a[0], str):
        event = a[0]
        payload = a[1] if len(a) >= 2 else {}
        log.debug(f"EVENT: {event} | {str(payload)[:120]}")
        route(event, payload)

# ─── WebSocket через curl_cffi (в отдельном потоке) ──────────────────────────
def ws_thread():
    headers = {
        "User-Agent": USER_AGENT,
        "Origin": "https://bandit.camp",
    }
    cookies = {}
    if CF_CLEARANCE:
        cookies["cf_clearance"] = CF_CLEARANCE

    while True:
        log.info(f"🔌 Подключаюсь к {WS_URL} (impersonate={IMPERSONATE}) ...")
        try:
            session = cffi.Session(impersonate=IMPERSONATE)
            ws = session.ws_connect(WS_URL, headers=headers, cookies=cookies)
            log.info("✅ WebSocket подключён!")

            while True:
                frame = ws.recv()
                if frame is None:
                    break
                # ws.recv() может вернуть (data, flags) или просто data
                data = frame[0] if isinstance(frame, tuple) else frame
                if isinstance(data, bytes):
                    try:
                        data = data.decode("utf-8")
                    except Exception:
                        continue
                if data:
                    handle_message(data)

        except Exception as e:
            log.error(f"WS error: {type(e).__name__}: {e}")

        log.info("♻️  Переподключение через 10 сек...")
        time.sleep(10)

# ─── Keep-alive HTTP сервер ───────────────────────────────────────────────────
async def keep_alive():
    from aiohttp import web
    port = int(os.environ.get("PORT", 10000))
    async def health(request):
        return web.Response(text=f"OK | active rains: {len(active)}")
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"🌐 Keep-alive на порту {port}")

# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    global bot, loop
    loop = asyncio.get_running_loop()

    log.info("🚀 Запуск Bandit Rain Bot v4 (curl_cffi)...")
    bot = Bot(token=TELEGRAM_TOKEN)
    me = await bot.get_me()
    log.info(f"✅ Бот @{me.username} готов")

    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL,
            text="🤖 <b>Rain Bot v4 запущен!</b>\nОтслеживаю рейны на bandit.camp 🌧",
            parse_mode=ParseMode.HTML)
    except TelegramError as e:
        log.error(f"Не могу написать в канал: {e}")

    # WebSocket в отдельном потоке (curl_cffi синхронный)
    t = threading.Thread(target=ws_thread, daemon=True)
    t.start()

    await keep_alive()
    # держим event loop живым
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
