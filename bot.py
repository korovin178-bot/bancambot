"""
Bandit.camp Rain Bot v3
Протокол: кастомный WebSocket (НЕ Socket.IO)
Формат сообщений: {"a": [событие, данные], "i": номер}
Событие рейна: chat.rain → {startedAt, userCount, duration, joined, value}
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import websockets
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHANNEL = os.environ["TELEGRAM_CHANNEL"]

CF_CLEARANCE = os.environ.get("CF_CLEARANCE", "")
CF_BM        = os.environ.get("CF_BM", "")
# ВАЖНО: User-Agent должен ТОЧНО совпадать с браузером где взяли cookie
USER_AGENT   = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
)

WS_URL   = "wss://api.bandit.camp/"
SITE_URL = "https://bandit.camp"

# value на сайте в центах → делим на 100 для долларов
VALUE_DIVISOR = 100

# ─── State ────────────────────────────────────────────────────────────────────
@dataclass
class Rain:
    rain_key: str
    value: float            # в долларах (после деления)
    user_count: int = 0
    duration_ms: int = 0
    started_at_ms: int = 0
    created: float = field(default_factory=time.time)
    status: str = "active"
    msg_id: Optional[int] = None

active: dict[str, Rain] = {}
bot: Optional[Bot] = None

# ─── Форматирование сообщения ─────────────────────────────────────────────────
def fmt(rain: Rain) -> str:
    if rain.status == "finished":
        header = "✅ <b>RAKEBACK RAIN — ЗАВЕРШЁН</b>"
        time_line = ""
    else:
        header = "🌧 <b>RAKEBACK RAIN — АКТИВЕН</b>"
        now_ms = int(time.time() * 1000)
        if rain.started_at_ms and rain.duration_ms:
            remaining_ms = max(0, rain.started_at_ms + rain.duration_ms - now_ms)
            rm, rs = divmod(remaining_ms // 1000, 60)
            time_line = f"⏳ <b>Осталось:</b> {rm}м {rs}с\n"
        else:
            time_line = ""

    participants = f"👥 <b>Участники:</b> {rain.user_count}\n" if rain.user_count > 0 else ""

    return (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Сумма:</b> <code>{rain.value:.2f}$</code>\n"
        f"{participants}"
        f"{time_line}"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 <a href=\"{SITE_URL}\">Зайти на bandit.camp</a>"
    )

# ─── Telegram ─────────────────────────────────────────────────────────────────
async def tg_send(rain: Rain):
    try:
        msg = await bot.send_message(
            chat_id=TELEGRAM_CHANNEL, text=fmt(rain),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
        rain.msg_id = msg.message_id
        log.info(f"✉️  Отправлено: value={rain.value}$ users={rain.user_count}")
    except TelegramError as e:
        log.error(f"send error: {e}")

async def tg_edit(rain: Rain):
    if not rain.msg_id:
        await tg_send(rain)
        return
    try:
        await bot.edit_message_text(
            chat_id=TELEGRAM_CHANNEL, message_id=rain.msg_id, text=fmt(rain),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
        log.info(f"✏️  Обновлено: value={rain.value}$ users={rain.user_count} status={rain.status}")
    except TelegramError as e:
        if "not modified" not in str(e).lower():
            log.error(f"edit error: {e}")

# ─── Таймер: обновляет «осталось» каждые 30 сек ──────────────────────────────
async def timer_updater(rain: Rain):
    while rain.status == "active":
        await asyncio.sleep(30)
        if rain.status == "active" and rain.msg_id:
            now_ms = int(time.time() * 1000)
            if rain.started_at_ms and rain.duration_ms:
                if now_ms > rain.started_at_ms + rain.duration_ms:
                    rain.status = "finished"
            await tg_edit(rain)

# ─── Обработчики событий ──────────────────────────────────────────────────────
async def on_rain(data: dict):
    """chat.rain — новый рейн (или обновление существующего)"""
    started = int(data.get("startedAt") or 0)
    value   = float(data.get("value") or 0) / VALUE_DIVISOR
    users   = int(data.get("userCount") or 0)
    dur     = int(data.get("duration") or 0)

    key = str(started) if started else f"rain_{int(time.time())}"

    log.info(f"🌧 chat.rain: value={value}$ users={users} dur={dur}ms started={started}")

    rain = active.get(key)
    if rain is None:
        rain = Rain(rain_key=key, value=value, user_count=users,
                    duration_ms=dur, started_at_ms=started)
        active[key] = rain
        await tg_send(rain)
        asyncio.create_task(timer_updater(rain))
    else:
        changed = (abs(rain.value - value) > 0.005) or (rain.user_count != users)
        rain.value = value
        rain.user_count = users
        if changed:
            await tg_edit(rain)

async def on_rain_join(data: dict):
    """chat.rain.join — кто-то присоединился"""
    users = int(data.get("userCount") or 0)
    if not active:
        return
    rain = max(active.values(), key=lambda r: r.started_at_ms)
    if rain.status == "active" and users and users != rain.user_count:
        rain.user_count = users
        await tg_edit(rain)

async def on_rain_payout(data: dict):
    """chat.rain.payoutSummary — рейн завершён"""
    users = int(data.get("userCount") or data.get("recipients") or 0)
    total = data.get("total") or data.get("value")

    log.info(f"✅ chat.rain.payoutSummary: users={users} total={total}")

    if not active:
        return
    rain = max(active.values(), key=lambda r: r.started_at_ms)
    if users:
        rain.user_count = users
    if total is not None:
        rain.value = float(total) / VALUE_DIVISOR
    rain.status = "finished"
    await tg_edit(rain)

    key = rain.rain_key
    await asyncio.sleep(600)
    active.pop(key, None)

# ─── Роутер ───────────────────────────────────────────────────────────────────
async def route(event: str, data):
    if not isinstance(data, dict):
        data = {}
    if event == "chat.rain":
        await on_rain(data)
    elif event == "chat.rain.join":
        await on_rain_join(data)
    elif event in ("chat.rain.payoutSummary", "chat.rain.payout"):
        await on_rain_payout(data)
    elif "rain" in event.lower():
        log.info(f"🔔 Прочее rain-событие: {event} | {str(data)[:150]}")

# ─── Парсинг входящего сообщения ──────────────────────────────────────────────
async def handle_message(raw: str):
    """Формат: {"a": ["event.name", data, ...], "i": N}"""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(obj, dict):
        return

    a = obj.get("a")
    if isinstance(a, list) and len(a) >= 1:
        event = a[0]
        payload = a[1] if len(a) >= 2 else {}
        if isinstance(event, str):
            log.debug(f"EVENT: {event} | {str(payload)[:150]}")
            await route(event, payload)

# ─── WebSocket ────────────────────────────────────────────────────────────────
def build_headers() -> dict:
    cookies = []
    if CF_CLEARANCE:
        cookies.append(f"cf_clearance={CF_CLEARANCE}")
    if CF_BM:
        cookies.append(f"__cf_bm={CF_BM}")
    headers = {
        "User-Agent": USER_AGENT,
        "Origin": "https://bandit.camp",
    }
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    return headers

async def run_websocket():
    headers = build_headers()
    while True:
        log.info(f"🔌 Подключаюсь к {WS_URL} ...")
        try:
            async with websockets.connect(
                WS_URL,
                extra_headers=headers,
                ping_interval=25,
                ping_timeout=20,
                close_timeout=10,
                max_size=None,
            ) as ws:
                log.info("✅ WebSocket подключён!")
                async for message in ws:
                    if isinstance(message, bytes):
                        try:
                            message = message.decode("utf-8")
                        except Exception:
                            continue
                    await handle_message(message)

        except websockets.exceptions.InvalidStatusCode as e:
            code = getattr(e, "status_code", "?")
            if code == 403:
                log.error("❌ HTTP 403 — Cloudflare блокирует. Обнови CF_CLEARANCE + USER_AGENT.")
            else:
                log.error(f"❌ HTTP {code} при подключении.")
            await asyncio.sleep(30)
        except Exception as e:
            log.error(f"WS error: {type(e).__name__}: {e}")

        log.info("♻️  Переподключение через 10 сек...")
        await asyncio.sleep(10)

# ─── Keep-alive для Render ────────────────────────────────────────────────────
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
    global bot
    if not CF_CLEARANCE:
        log.warning("⚠️  CF_CLEARANCE не задан — Cloudflare скорее всего заблокирует.")

    log.info("🚀 Запуск Bandit Rain Bot v3...")
    bot = Bot(token=TELEGRAM_TOKEN)
    me = await bot.get_me()
    log.info(f"✅ Бот @{me.username} готов")

    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL,
            text="🤖 <b>Rain Bot v3 запущен!</b>\nОтслеживаю рейны на bandit.camp 🌧",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        log.error(f"Не могу написать в канал: {e}")

    await asyncio.gather(keep_alive(), run_websocket())

if __name__ == "__main__":
    asyncio.run(main())
