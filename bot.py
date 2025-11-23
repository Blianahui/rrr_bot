import os
import asyncio
import requests
import cloudscraper
from bs4 import BeautifulSoup

from aiohttp import web

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ========= НАСТРОЙКИ ЧЕРЕЗ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ =========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")  # строка, конвертнем в int позже
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))  # по умолчанию 300 секунд (5 минут)

MAX_PRICE = float(os.environ.get("MAX_PRICE", "20.0"))

PART_NUMBERS = ["30657756", "30657757"]

SEARCH_URL_TEMPLATE = "https://rrr.lt/ru/poisk?q={part_number}&exact=1"

# будем помнить, о чем уже уведомляли
notified_items = set()


def fetch_offers_for_part(part_number: str):
    """
    Лезем на rrr.lt и достаём объявления по номеру детали.
    Возвращает список словарей: [{ 'title': ..., 'price': ..., 'url': ..., 'part_number': ... }, ...]
    """
    url = SEARCH_URL_TEMPLATE.format(part_number=part_number)

    # используем cloudscraper вместо requests
    scraper = cloudscraper.create_scraper(
        browser={
            "browser": "chrome",
            "platform": "windows",
            "mobile": False
        }
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://rrr.lt/ru/poisk",
    }

    resp = scraper.get(url, headers=headers, timeout=20)
    resp.raise_for_status()  # если всё равно 403, прилетит тут

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")
    offers = []

    items = soup.find_all("div", class_="products__items", attrs={"data-testid": "product-card"})
    for item in items:
        # ссылка на товар
        link_tag = item.find("a", class_="products__items__link")
        if not link_tag:
            continue
        url_item = link_tag.get("href", "").strip()
        if not url_item:
            continue
        if not url_item.startswith("http"):
            url_item = "https://rrr.lt" + url_item

        # название
        title_tag = item.find("span", class_="products__text__header", attrs={"data-testid": "product-header"})
        title = title_tag.get_text(strip=True) if title_tag else f"Товар {part_number}"

        # код детали
        code_tag = item.find("p", class_="products__code")
        code_text = ""
        if code_tag:
            a_code = code_tag.find("a")
            if a_code:
                code_text = a_code.get_text(strip=True)

        # цена
        price_tag = item.find("strong", attrs={"data-testid": "product-price"})
        if not price_tag:
            continue
        price_text = price_tag.get_text(strip=True)

        price_val = None
        tmp = ""
        for ch in price_text:
            if ch.isdigit() or ch in ",.":
                tmp += ch
            elif tmp:
                break
        if tmp:
            try:
                price_val = float(tmp.replace(",", "."))
            except ValueError:
                price_val = None

        if price_val is None:
            continue

        offers.append(
            {
                "title": title,
                "price": price_val,
                "url": url_item,
                "part_number": code_text or part_number,
            }
        )

    return offers



async def send_message(app, text: str):
    chat_id_int = int(CHAT_ID)
    await app.bot.send_message(chat_id=chat_id_int, text=text, disable_web_page_preview=False)


async def checker_loop(app):
    global notified_items

    # чуть подождём, чтобы всё поднялось
    await asyncio.sleep(5)

    while True:
        try:
            for part_number in PART_NUMBERS:
                offers = fetch_offers_for_part(part_number)
                for offer in offers:
                    if offer["price"] <= MAX_PRICE:
                        key = f'{offer["part_number"]}|{offer["url"]}|{offer["price"]}'
                        if key in notified_items:
                            continue  # уже уведомляли про это
                        notified_items.add(key)

                        text = (
                            f'Нашёл деталь {offer["part_number"]} дешевле {MAX_PRICE}€!\n\n'
                            f'{offer["title"]}\n'
                            f'Цена: {offer["price"]} €\n'
                            f'Ссылка: {offer["url"]}'
                        )
                        await send_message(app, text)
        except Exception as e:
            # можно закомментить, чтобы не спамил
            try:
                await send_message(app, f"Ошибка при проверке: {e}")
            except Exception:
                pass

        await asyncio.sleep(CHECK_INTERVAL)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я мониторю детали 30657756 и 30657757 на rrr.lt.\n"
        f"Уведомлю, если найду что-то дешевле {MAX_PRICE}€."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Настройки мониторинга:\n"
        f"- Детали: {', '.join(PART_NUMBERS)}\n"
        f"- Лимит цены: {MAX_PRICE}€\n"
        f"- Интервал проверки: {CHECK_INTERVAL} сек.\n"
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — краткая инфа\n"
        "/status — показать текущие настройки\n"
        "Мониторинг идёт автоматически в фоне."
    )


# ==== МАЛЕНЬКИЙ HTTP-СЕРВЕР ДЛЯ RENDER ====

async def handle_root(request):
    return web.Response(text="Bot is running.\n")


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle_root)

    port = int(os.environ.get("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main_async():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Не задан TELEGRAM_TOKEN в переменных окружения")
    if not CHAT_ID:
        raise RuntimeError("Не задан CHAT_ID в переменных окружения")

    # Создаём Telegram-приложение
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))

    # Поднимаем web-сервер и бота параллельно
    async with application:
        # стартуем фоновый чекер
        asyncio.create_task(checker_loop(application))

        # стартуем HTTP-сервер
        asyncio.create_task(run_web_server())

        # запускаем polling (блокирующий внутри, но мы в async-контексте)
        await application.start()
        print("Bot started")
        await application.updater.start_polling()
        # держим приложение живым
        await asyncio.Event().wait()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

