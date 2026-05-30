"""
Bandit.camp Rain Bot v2
- Подключается к wss://api.bandit.camp напрямую
- Использует cf_clearance cookies для обхода Cloudflare
- Слушает события chat.rain, chat.rain.join, chat.rain.payoutSummary
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import websockets
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHANNEL = os.environ["TELEGRAM_CHANNEL"]

# Cloudflare cookies — берёшь из браузера (см. ИНСТРУКЦИЯ.md)
CF_CLEARANCE = os.environ.get("CF_CLEARANCE", "")
CF_BM        = os.environ.get("CF_BM", "")
USER_AGENT   = os.environ.get("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

WS_URL   = "wss://api.bandit.camp/socket.io/?EIO=4&transport=websocket"
SITE_URL = "https://bandit.camp"

# ─── State ────────────────────────────────────────────────────────────────────
@dataclass
class Rain:
    rain_id: str
    value: float           # сумма в $
    user_count: int = 0    # кол-во участников
    duration: int = 0      # длительность в секундах
    started_at: float = field(default_factory=time.time)
    status: str = "active" # active | finished
    msg_id: Optional[int] = None

active: dict[str, Rain] = {}
bot: Optional[Bot] = None

# ─── Форматирование ───────────────────────────────────────────────────────────
def fmt(rain: Rain) -> str:
    age = int(time.time() - rain.started_at)
    m, s = divmod(age, 60)
    age_str = f"{m}м {s}с" if m else f"{s}с"

    if rain.duration > 0:
        remaining = max(0, rain.duration - age)
        rm, rs = divmod(remaining, 60)
        time_line = f"⏳ <b>Осталось:</b> {rm}м {rs}с\n"
    else:
        time_line = f"⏰ <b>Идёт:</b> {age_str}\n"

    if rain.status == "finished":
        header = "✅ <b>RAKEBACK RAIN — ЗАВЕРШЁН</b>"
        time_line = ""
    else:
        header = "🌧 <b>RAKEBACK RAIN — АКТИВЕН</b>"

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
            chat_id=TELEGRAM_CHANNEL,
            text=fmt(rain),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        rain.msg_id = msg.message_id
        log.info(f"✉️  Отправлено: value={rain.value} users={rain.user_count}")
    except TelegramError as e:
        log.error(f"send error: {e}")

async def tg_edit(rain: Rain):
    if not rain.msg_id:
        await tg_send(rain)
        return
    try:
        await bot.edit_message_text(
            chat_id=TELEGRAM_CHANNEL,
            message_id=rain.msg_id,
            text=fmt(rain),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        log.info(f"✏️  Обновлено: value={rain.value} users={rain.user_count} status={rain.status}")
    except TelegramError as e:
        if "not modified" not in str(e).lower():
            log.error(f"edit error: {e}")

# ─── Обновлятор таймера (каждые 30 сек пока rain активен) ────────────────────
async def timer_updater(rain: Rain):
    while rain.status == "active":
        await asyncio.sleep(30)
        if rain.status == "active" and rain.msg_id:
            await tg_edit(rain)

# ─── Обработчики событий bandit.camp ─────────────────────────────────────────
async def on_chat_rain(data: dict):
    """Новый рейн начался"""
    rain_id    = str(data.get("id") or data.get("_id") or f"rain_{time.time()}")
    value      = float(data.get("value") or data.get("amount") or 0)
    user_count = int(data.get("userCount") or data.get("user_count") or 0)
    duration   = int(data.get("duration") or 0)

    log.info(f"🌧 chat.rain: id={rain_id} value={value} users={user_count} duration={duration}s")

    if rain_id in active:
        # Обновляем если уже есть
        rain = active[rain_id]
        rain.value      = value
        rain.user_count = user_count
        await tg_edit(rain)
        return

    rain = Rain(rain_id=rain_id, value=value, user_count=user_count, duration=duration)
    active[rain_id] = rain
    await tg_send(rain)
    asyncio.create_task(timer_updater(rain))


async def on_chat_rain_join(data: dict):
    """Пользователь присоединился к рейну — обновляем счётчик"""
    rain_id    = str(data.get("rainId") or data.get("id") or "")
    user_count = int(data.get("userCount") or data.get("user_count") or 0)

    rain = active.get(rain_id)
    if not rain:
        return

    if user_count and user_count != rain.user_count:
        rain.user_count = user_count
        await tg_edit(rain)


async def on_chat_rain_payout(data: dict):
    """Рейн завершился — финальная сумма и участники"""
    rain_id    = str(data.get("rainId") or data.get("id") or "")
    value      = float(data.get("total") or data.get("value") or data.get("amount") or 0)
    user_count = int(data.get("userCount") or data.get("recipients") or 0)

    log.info(f"✅ chat.rain.payoutSummary: id={rain_id} total={value} users={user_count}")

    rain = active.get(rain_id)
    if not rain:
        # Если пропустили старт — всё равно постим финал
        rain = Rain(rain_id=rain_id, value=value, user_count=user_count, status="finished")
        active[rain_id] = rain
        await tg_send(rain)
        return

    rain.value      = value
    rain.user_count = user_count
    rain.status     = "finished"
    await tg_edit(rain)

    await asyncio.sleep(600)
    active.pop(rain_id, None)

# ─── Socket.IO поверх WebSocket ───────────────────────────────────────────────
def build_headers() -> dict:
    cookies = []
    if CF_CLEARANCE:
        cookies.append(f"cf_clearance={CF_CLEARANCE}")
    if CF_BM:
        cookies.append(f"__cf_bm={CF_BM}")

    headers = {
        "User-Agent": USER_AGENT,
        "Origin": "https://bandit.camp",
        "Referer": "https://bandit.camp/",
    }
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    return headers


async def handle_message(raw: str):
    """Парсит Socket.IO протокол и роутит события"""
    # Socket.IO пакеты: 0=open, 2=ping, 3=pong, 40=connect, 42=event, 42[event,data]
    if raw.startswith("42"):
        try:
            payload = json.loads(raw[2:])   # убираем "42"
            if isinstance(payload, list) and len(payload) >= 2:
                event = payload[0]
                data  = payload[1] if len(payload) > 1 else {}

                log.debug(f"EVENT: {event} | {str(data)[:150]}")

                if event == "chat.rain":
                    await on_chat_rain(data)
                elif event == "chat.rain.join":
                    await on_chat_rain_join(data)
                elif event in ("chat.rain.payoutSummary", "chat.rain.payout"):
                    await on_chat_rain_payout(data)
                elif "rain" in event.lower():
                    log.info(f"🔔 Неизвестное rain-событие: {event} | {data}")
        except Exception as e:
            log.error(f"parse error: {e} | raw={raw[:200]}")


async def run_websocket():
    """Основной цикл WebSocket с авто-переподключением"""
    headers = build_headers()

    while True:
        log.info("🔌 Подключаюсь к wss://api.bandit.camp...")
        try:
            async with websockets.connect(
                WS_URL,
                additional_headers=headers,
                ping_interval=25,
                ping_timeout=20,
                close_timeout=10,
            ) as ws:
                log.info("✅ WebSocket подключён!")

                async for message in ws:
                    if isinstance(message, bytes):
                        continue

                    # Socket.IO ping/pong
                    if message == "2":
                        await ws.send("3")
                        continue

                    await handle_message(message)

        except websockets.exceptions.InvalidStatusCode as e:
            log.error(f"❌ HTTP {e.status_code} — обнови CF_CLEARANCE cookie!")
            await asyncio.sleep(60)   # ждём дольше если 403
        except Exception as e:
            log.error(f"WS error: {e}")

        log.info("♻️  Переподключение через 10 сек...")
        await asyncio.sleep(10)

# ─── Keep-alive HTTP сервер для Render ───────────────────────────────────────
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
        log.warning("⚠️  CF_CLEARANCE не задан! Cloudflare может заблокировать соединение.")

    log.info("🚀 Запуск Bandit Rain Bot v2...")
    bot = Bot(token=TELEGRAM_TOKEN)
    me = await bot.get_me()
    log.info(f"✅ Бот @{me.username} готов")

    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL,
            text="🤖 <b>Rain Bot v2 запущен!</b>\nОтслеживаю рейны на bandit.camp 24/7 🌧",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        log.error(f"Не могу написать в канал: {e}")

    await asyncio.gather(
        keep_alive(),
        run_websocket(),
    )

if __name__ == "__main__":
    asyncio.run(main())
