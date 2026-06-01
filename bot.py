"""
Bandit.camp Rain Bot v5 — Playwright (реальный Chromium через прокси)
Браузер открывает сайт, WebSocket создаётся внутри страницы,
кадры перехватываются через page.on("websocket").
Протокол сообщений: {"a": [событие, данные], "i": номер}
"""

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
from playwright.async_api import async_playwright

# ─── Логи (print с flush — надёжно видно в Render) ───────────────────────────
def log(level, msg):
    print(f"{time.strftime('%H:%M:%S')} [{level}] {msg}", flush=True)
    sys.stdout.flush()

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHANNEL = os.environ["TELEGRAM_CHANNEL"]

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
)

SITE_URL = "https://bandit.camp"
VALUE_DIVISOR = 100

# Прокси по частям (как в Render Environment)
PROXY_HOST  = os.environ.get("PROXY_HOST", "").strip()
PROXY_PORT  = os.environ.get("PROXY_PORT", "").strip()
PROXY_LOGIN = os.environ.get("PROXY_LOGIN", "").strip()
PROXY_PASS  = os.environ.get("PROXY_PASS", "").strip()

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
traffic_bytes = 0

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

# ─── Telegram ─────────────────────────────────────────────────────────────────
async def tg_send(rain: Rain):
    try:
        msg = await bot.send_message(
            chat_id=TELEGRAM_CHANNEL, text=fmt(rain),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        rain.msg_id = msg.message_id
        log("INFO", f"✉️  Отправлено: value={rain.value}$ users={rain.user_count}")
    except TelegramError as e:
        log("ERROR", f"send error: {e}")

async def tg_edit(rain: Rain):
    if not rain.msg_id:
        await tg_send(rain); return
    try:
        await bot.edit_message_text(
            chat_id=TELEGRAM_CHANNEL, message_id=rain.msg_id, text=fmt(rain),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        log("INFO", f"✏️  Обновлено: value={rain.value}$ users={rain.user_count} status={rain.status}")
    except TelegramError as e:
        if "not modified" not in str(e).lower():
            log("ERROR", f"edit error: {e}")

# ─── Диагностика прямо в Telegram ─────────────────────────────────────────────
async def tg_diag(text: str):
    """Шлёт диагностическое сообщение в канал (чтобы видеть статус без логов Render)"""
    log("INFO", f"DIAG → {text}")
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL,
            text=f"🛠 <i>{text}</i>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True)
    except Exception as e:
        log("ERROR", f"diag send error: {e}")

# ─── Таймер обновления ────────────────────────────────────────────────────────
async def timer_updater(rain: Rain):
    while rain.status == "active":
        await asyncio.sleep(30)
        if rain.status == "active" and rain.msg_id:
            now_ms = int(time.time() * 1000)
            if rain.started_at_ms and rain.duration_ms and now_ms > rain.started_at_ms + rain.duration_ms:
                rain.status = "finished"
            await tg_edit(rain)

# ─── Обработчики событий ──────────────────────────────────────────────────────
async def on_rain(data: dict):
    started = int(data.get("startedAt") or 0)
    value   = float(data.get("value") or 0) / VALUE_DIVISOR
    users   = int(data.get("userCount") or 0)
    dur     = int(data.get("duration") or 0)
    key = str(started) if started else f"rain_{int(time.time())}"
    log("INFO", f"🌧 chat.rain: value={value}$ users={users} dur={dur}ms")
    rain = active.get(key)
    if rain is None:
        rain = Rain(rain_key=key, value=value, user_count=users, duration_ms=dur, started_at_ms=started)
        active[key] = rain
        await tg_send(rain)
        asyncio.create_task(timer_updater(rain))
    else:
        changed = (abs(rain.value - value) > 0.005) or (rain.user_count != users)
        rain.value = value; rain.user_count = users
        if changed:
            await tg_edit(rain)

async def on_rain_join(data: dict):
    users = int(data.get("userCount") or 0)
    if not active: return
    rain = max(active.values(), key=lambda r: r.started_at_ms)
    if rain.status == "active" and users and users != rain.user_count:
        rain.user_count = users
        await tg_edit(rain)

async def on_rain_payout(data: dict):
    users = int(data.get("userCount") or data.get("recipients") or 0)
    total = data.get("total") or data.get("value")
    log("INFO", f"✅ payoutSummary: users={users} total={total}")
    if not active: return
    rain = max(active.values(), key=lambda r: r.started_at_ms)
    if users: rain.user_count = users
    if total is not None: rain.value = float(total) / VALUE_DIVISOR
    rain.status = "finished"
    await tg_edit(rain)

async def route(event: str, data):
    if not isinstance(data, dict): data = {}
    if event == "chat.rain":
        await on_rain(data)
    elif event == "chat.rain.join":
        await on_rain_join(data)
    elif event in ("chat.rain.payoutSummary", "chat.rain.payout"):
        await on_rain_payout(data)
    elif "rain" in event.lower():
        log("INFO", f"🔔 Прочее rain-событие: {event} | {str(data)[:120]}")

async def handle_frame(payload: str):
    global traffic_bytes
    traffic_bytes += len(payload.encode("utf-8")) if isinstance(payload, str) else len(payload)
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(obj, dict):
        return
    a = obj.get("a")
    if isinstance(a, list) and len(a) >= 1 and isinstance(a[0], str):
        event = a[0]
        data = a[1] if len(a) >= 2 else {}
        await route(event, data)

# ─── Keep-alive HTTP для Render ───────────────────────────────────────────────
async def keep_alive():
    from aiohttp import web
    port = int(os.environ.get("PORT", 10000))
    async def health(request):
        mb = traffic_bytes / (1024 * 1024)
        return web.Response(text=f"OK | rains: {len(active)} | traffic: {mb:.3f} MB")
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log("INFO", f"🌐 Keep-alive на порту {port}")

# ─── Браузерный цикл ──────────────────────────────────────────────────────────
async def browser_loop():
    # настройки прокси для Playwright
    proxy = None
    if PROXY_HOST and PROXY_PORT:
        proxy = {
            "server": f"http://{PROXY_HOST}:{PROXY_PORT}",
            "username": PROXY_LOGIN,
            "password": PROXY_PASS,
        }
        log("INFO", f"🌍 Прокси: http://***@{PROXY_HOST}:{PROXY_PORT}")
    else:
        log("INFO", "🌍 Прокси НЕ задан")

    # флаги для экономии RAM (Render free = 512MB)
    chromium_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-software-rasterizer",
        "--single-process",
        "--no-zygote",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-sync",
        "--metrics-recording-only",
        "--mute-audio",
        "--no-first-run",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
    ]

    while True:
        try:
            async with async_playwright() as p:
                log("INFO", "🚀 Запускаю Chromium...")
                await tg_diag("Запускаю Chromium...")
                browser = await p.chromium.launch(
                    headless=True,
                    args=chromium_args,
                    proxy=proxy,
                )
                context = await browser.new_context(
                    user_agent=USER_AGENT,
                    locale="ru-RU",
                    viewport={"width": 1280, "height": 720},
                )
                # блокируем только тяжёлые картинки/видео/шрифты (CSS и JS пропускаем!)
                async def block_heavy(route_obj):
                    if route_obj.request.resource_type in ("image", "font", "media"):
                        await route_obj.abort()
                    else:
                        await route_obj.continue_()
                await context.route("**/*", block_heavy)

                page = await context.new_page()
                main_loop = asyncio.get_running_loop()

                ws_opened = {"flag": False}

                def on_ws(ws):
                    ws_opened["flag"] = True
                    log("INFO", f"🔌 WebSocket открыт: {ws.url}")
                    asyncio.run_coroutine_threadsafe(
                        tg_diag(f"🔌 WebSocket открыт: {ws.url}"), main_loop)
                    def on_frame(payload):
                        asyncio.run_coroutine_threadsafe(handle_frame(payload), main_loop)
                    ws.on("framereceived", lambda pl: on_frame(pl))
                    ws.on("close", lambda: log("INFO", "🔌 WebSocket закрыт"))

                page.on("websocket", on_ws)

                log("INFO", f"🌐 Открываю {SITE_URL} ...")
                await tg_diag(f"Открываю {SITE_URL} через прокси...")
                await page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60000)
                title = await page.title()
                log("INFO", f"📄 Заголовок страницы: {title}")
                await tg_diag(f"📄 Заголовок страницы: «{title}»")

                if "moment" in title.lower() or "attention" in title.lower():
                    await tg_diag("⚠️ Похоже на Cloudflare-челлендж. Жду 20с...")
                    await page.wait_for_timeout(20000)
                    title = await page.title()
                    await tg_diag(f"📄 После ожидания: «{title}»")

                # ждём появления WebSocket до 30 сек
                for _ in range(30):
                    if ws_opened["flag"]:
                        break
                    await page.wait_for_timeout(1000)

                if ws_opened["flag"]:
                    await tg_diag("✅ Всё работает! Слушаю рейны 🌧")
                else:
                    await tg_diag("⚠️ WebSocket за 30с не открылся. Возможно сайт его создаёт позже или режет CF.")

                log("INFO", "✅ Страница загружена, слушаю WebSocket события...")

                # держим страницу живой
                while True:
                    await page.wait_for_timeout(30000)
                    try:
                        await page.evaluate("1")
                    except Exception:
                        break

        except Exception as e:
            log("ERROR", f"💥 Браузер упал: {type(e).__name__}: {e}")
            try:
                await tg_diag(f"💥 Браузер упал: {type(e).__name__}: {str(e)[:200]}")
            except Exception:
                pass

        log("INFO", "♻️  Перезапуск браузера через 15 сек...")
        await asyncio.sleep(15)

# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    global bot
    await keep_alive()

    log("INFO", "🚀 Запуск Bandit Rain Bot v5 (Playwright)...")
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        me = await bot.get_me()
        log("INFO", f"✅ Бот @{me.username} готов")
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL,
            text="🤖 <b>Rain Bot v5 (Playwright) запущен!</b>\nОтслеживаю рейны 🌧",
            parse_mode=ParseMode.HTML)
    except Exception as e:
        log("ERROR", f"Telegram init: {type(e).__name__}: {e}")

    await browser_loop()

if __name__ == "__main__":
    asyncio.run(main())
