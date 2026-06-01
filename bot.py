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
from playwright_stealth import Stealth

# ─── Логи (print с flush — надёжно видно в Render) ───────────────────────────
def log(level, msg):
    print(f"{time.strftime('%H:%M:%S')} [{level}] {msg}", flush=True)
    sys.stdout.flush()

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHANNEL = os.environ["TELEGRAM_CHANNEL"]

# чистим UA от любых небезопасных/невидимых символов (только печатный ASCII)
_raw_ua = os.environ.get("USER_AGENT", "") or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
USER_AGENT = "".join(c for c in _raw_ua if 32 <= ord(c) < 127).strip()

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
DIAG_QUIET = {"on": False}  # после первого успешного коннекта — тишина в канале

async def tg_diag(text: str, force: bool = False):
    """Шлёт диагностику в канал. После первого успеха молчит (кроме force)."""
    log("INFO", f"DIAG → {text}")
    if DIAG_QUIET["on"] and not force:
        return
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

# ─── Клик по Cloudflare Turnstile ─────────────────────────────────────────────
async def human_click(page, x, y):
    """Человекоподобное движение мыши к точке и клик (Turnstile следит за поведением)"""
    import random
    # стартовая позиция
    sx, sy = random.randint(100, 400), random.randint(100, 400)
    await page.mouse.move(sx, sy)
    # плавное движение в несколько шагов с дрожанием
    steps = random.randint(15, 25)
    for i in range(1, steps + 1):
        nx = sx + (x - sx) * i / steps + random.uniform(-3, 3)
        ny = sy + (y - sy) * i / steps + random.uniform(-3, 3)
        await page.mouse.move(nx, ny)
        await page.wait_for_timeout(random.randint(10, 35))
    await page.wait_for_timeout(random.randint(200, 500))
    await page.mouse.move(x, y)
    await page.wait_for_timeout(random.randint(100, 300))
    await page.mouse.click(x, y, delay=random.randint(40, 120))


async def try_click_turnstile(page) -> bool:
    """
    Находит галочку Cloudflare Turnstile и нажимает человекоподобно.
    Галочка в левой части виджета (как на скрине пользователя).
    """
    # Способ 1: координаты iframe Turnstile + человекоподобный клик по левой части
    try:
        for sel in [
            "iframe[src*='challenges.cloudflare.com']",
            "div.cf-turnstile",
            "#cf-turnstile",
            "[class*=turnstile]",
        ]:
            try:
                box = await page.locator(sel).first.bounding_box(timeout=3000)
                if box and box["width"] > 0:
                    # галочка слева внутри виджета (~30px от левого края)
                    x = box["x"] + 30
                    y = box["y"] + box["height"] / 2
                    await human_click(page, x, y)
                    log("INFO", f"☑️ Человекоподобный клик ({sel}) x={x:.0f} y={y:.0f}")
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # Способ 2: чекбокс внутри iframe через frame_locator
    try:
        fl = page.frame_locator("iframe[src*='challenges.cloudflare.com']")
        for sel in ["input[type=checkbox]", "label", "body"]:
            try:
                await fl.locator(sel).first.click(timeout=4000)
                log("INFO", f"☑️ Клик frame_locator ({sel})")
                return True
            except Exception:
                continue
    except Exception:
        pass

    # Способ 3: перебор фреймов
    try:
        for frame in page.frames:
            url = (frame.url or "").lower()
            if "challenges.cloudflare.com" in url or "turnstile" in url:
                for sel in ["input[type=checkbox]", "label", "#challenge-stage", "body"]:
                    try:
                        el = await frame.wait_for_selector(sel, timeout=3000)
                        if el:
                            await el.click(timeout=3000)
                            log("INFO", f"☑️ Клик в iframe ({sel})")
                            return True
                    except Exception:
                        continue
    except Exception:
        pass

    return False

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

    first_success = {"done": False}

    # флаги для экономии RAM (Render free = 512MB)
    chromium_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-software-rasterizer",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-sync",
        "--mute-audio",
        "--no-first-run",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
    ]

    while True:
        try:
            async with Stealth().use_async(async_playwright()) as p:
                log("INFO", "🚀 Запускаю Chromium (stealth)...")
                await tg_diag("Запускаю Chromium (stealth-режим)...")
                browser = await asyncio.wait_for(
                    p.chromium.launch(headless=True, args=chromium_args, proxy=proxy),
                    timeout=90,
                )
                await tg_diag("Chromium запущен, открываю контекст...")
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

                # ── Прохождение Cloudflare Turnstile (клик по галочке) ──────
                def is_challenge(t: str) -> bool:
                    t = t.lower()
                    return any(k in t for k in ("moment", "момент", "attention", "проверк", "just a"))

                if is_challenge(title):
                    await tg_diag("⚠️ Turnstile-челлендж. Делаю скриншот и пытаюсь нажать...")
                    # скриншот того что видит бот — отправим в канал
                    try:
                        shot = await page.screenshot(full_page=False)
                        await bot.send_photo(
                            chat_id=TELEGRAM_CHANNEL,
                            photo=shot,
                            caption="📸 Что видит бот на челлендже")
                    except Exception as e:
                        log("ERROR", f"screenshot error: {e}")
                    clicked = await try_click_turnstile(page)
                    if clicked:
                        await tg_diag("☑️ Кликнул по галочке, жду прохождения...")
                    else:
                        await tg_diag("⚠️ Галочку сразу не нашёл, продолжаю пытаться...")

                    # ждём смены заголовка до 60 сек, повторяя клик каждые 8 сек
                    passed = False
                    for i in range(60):
                        await page.wait_for_timeout(1000)
                        t = await page.title()
                        if not is_challenge(t):
                            passed = True
                            break
                        if i % 8 == 7:
                            await try_click_turnstile(page)

                    title = await page.title()
                    if passed:
                        await tg_diag(f"✅ Челлендж пройден! Заголовок: «{title}»")
                    else:
                        await tg_diag(f"❌ Не прошёл за 60с. Заголовок: «{title}»")

                # ждём появления WebSocket до 40 сек
                for _ in range(40):
                    if ws_opened["flag"]:
                        break
                    await page.wait_for_timeout(1000)

                if ws_opened["flag"]:
                    await tg_diag("✅ Всё работает! Слушаю рейны 🌧", force=True)
                    DIAG_QUIET["on"] = True  # дальше канал не спамим, только рейны
                    log("INFO", "✅ WebSocket активен, держу соединение")
                    # Раз подключились — держим МЯГКО, НЕ трогаем страницу,
                    # чтобы случайное исключение не уронило рабочий браузер.
                    # Перезапуск только если WS реально закрылся.
                    ws_closed = {"flag": False}
                    # переопределяем обработчик close чтобы помечать флаг
                    # (соединение уже поймано в on_ws; здесь просто ждём долго)
                    while True:
                        await asyncio.sleep(60)
                        # если браузер/контекст умер — выйдет исключение и уйдём в reconnect
                        try:
                            _ = browser.is_connected()
                            if not _:
                                log("INFO", "Браузер отключился, переподключаюсь")
                                break
                        except Exception:
                            break
                else:
                    await tg_diag("⚠️ WebSocket за 40с не открылся (попал на челлендж). Перезаход...")
                    log("INFO", "WS не открылся, перезаход")
                    # короткая пауза и новый заход — поймаем удачный проход
                    await browser.close()
                    await asyncio.sleep(5)
                    continue

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
