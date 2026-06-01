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
from urllib.parse import quote

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

# ─── Прокси ───────────────────────────────────────────────────────────────────
# Можно задать готовый URL в PROXY (http://user:pass@host:port)
# ИЛИ по частям: PROXY_LOGIN, PROXY_PASS, PROXY_HOST, PROXY_PORT
def build_proxy() -> Optional[str]:
    full = os.environ.get("PROXY", "").strip()
    if full:
        return full

    login = os.environ.get("PROXY_LOGIN", "").strip()
    passwd = os.environ.get("PROXY_PASS", "").strip()
    host = os.environ.get("PROXY_HOST", "").strip()
    port = os.environ.get("PROXY_PORT", "").strip()

    if host and port:
        if login and passwd:
            # URL-кодируем логин и пароль (в логине бывают ; и спецсимволы)
            l = quote(login, safe="")
            p = quote(passwd, safe="")
            return f"http://{l}:{p}@{host}:{port}"
        return f"http://{host}:{port}"
    return None

PROXY_URL = build_proxy()

# ─── Счётчик трафика ──────────────────────────────────────────────────────────
traffic_bytes = 0
traffic_lock = threading.Lock()

def add_traffic(n: int):
    global traffic_bytes
    with traffic_lock:
        traffic_bytes += n


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

# ─── Авто-получение cf_clearance через прокси ────────────────────────────────
def fetch_cf_cookies(proxies) -> dict:
    """
    Заходит на bandit.camp через прокси обычным HTTPS-запросом.
    Если Cloudflare пропускает без интерактивного челленджа —
    возвращает выданные cookie (включая cf_clearance).
    """
    out = {}
    try:
        session = cffi.Session(impersonate=IMPERSONATE)
        r = session.get(
            SITE_URL,
            headers={"User-Agent": USER_AGENT},
            proxies=proxies,
            timeout=30,
            allow_redirects=True,
        )
        log.info(f"🍪 GET {SITE_URL} → HTTP {r.status_code}")
        # вытаскиваем все cookie из сессии
        try:
            jar = session.cookies.get_dict()
        except Exception:
            jar = dict(r.cookies)
        for k, v in jar.items():
            out[k] = v
        if "cf_clearance" in out:
            log.info("🍪 ✅ Получен свежий cf_clearance через прокси!")
        else:
            got = ", ".join(out.keys()) if out else "ничего"
            log.warning(f"🍪 ⚠️ cf_clearance НЕ выдан. Пришли cookie: {got}")
            # признак интерактивного челленджа
            if "just a moment" in (r.text or "").lower():
                log.warning("🍪 Cloudflare показывает интерактивный челлендж (нужен браузер).")
    except Exception as e:
        log.error(f"🍪 Ошибка получения cookie: {type(e).__name__}: {e}")
    return out

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

    proxies = None
    if PROXY_URL:
        proxies = {"https": PROXY_URL, "http": PROXY_URL}
        safe = PROXY_URL
        if "@" in safe:
            safe = "http://***@" + safe.split("@", 1)[1]
        log.info(f"🌍 Использую прокси: {safe}")
    else:
        log.info("🌍 Прокси НЕ задан — подключаюсь напрямую")

    while True:
        # 1) Получаем свежие cookie через прокси (тот же IP что и WS)
        cookies = {}
        if CF_CLEARANCE:
            cookies["cf_clearance"] = CF_CLEARANCE
            log.info("🍪 Использую CF_CLEARANCE из переменной окружения")
        else:
            fetched = fetch_cf_cookies(proxies)
            if "cf_clearance" in fetched:
                cookies = fetched

        # 2) Подключаемся к WebSocket
        ck = "с cookie" if cookies.get("cf_clearance") else "БЕЗ cookie"
        log.info(f"🔌 Подключаюсь к {WS_URL} (impersonate={IMPERSONATE}, {ck}) ...")
        try:
            session = cffi.Session(impersonate=IMPERSONATE)
            ws = session.ws_connect(
                WS_URL,
                headers=headers,
                cookies=cookies,
                proxies=proxies,
            )
            log.info("✅ WebSocket подключён!")

            while True:
                frame = ws.recv()
                if frame is None:
                    break
                data = frame[0] if isinstance(frame, tuple) else frame
                if isinstance(data, (bytes, bytearray)):
                    add_traffic(len(data))
                    try:
                        data = data.decode("utf-8")
                    except Exception:
                        continue
                else:
                    add_traffic(len(data.encode("utf-8")))
                if data:
                    handle_message(data)

        except Exception as e:
            msg = str(e)
            if "403" in msg:
                log.error("❌ 403 — Cloudflare блокирует даже через прокси/cookie. "
                          "Проверь совпадение страны прокси и региона cookie.")
            else:
                log.error(f"WS error: {type(e).__name__}: {msg}")

        log.info("♻️  Переподключение через 10 сек...")
        time.sleep(10)

# ─── Keep-alive HTTP сервер ───────────────────────────────────────────────────
async def keep_alive():
    from aiohttp import web
    port = int(os.environ.get("PORT", 10000))
    async def health(request):
        with traffic_lock:
            mb = traffic_bytes / (1024 * 1024)
        return web.Response(text=f"OK | active rains: {len(active)} | traffic: {mb:.2f} MB")
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"🌐 Keep-alive на порту {port}")

# ─── Репорт трафика раз в 10 минут ───────────────────────────────────────────
async def traffic_reporter():
    while True:
        await asyncio.sleep(600)
        with traffic_lock:
            mb = traffic_bytes / (1024 * 1024)
        # прогноз на месяц
        # (грубо: текущий объём за время работы экстраполируем)
        log.info(f"📊 Трафик с запуска: {mb:.2f} MB")

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

    asyncio.create_task(traffic_reporter())
    await keep_alive()
    # держим event loop живым
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
