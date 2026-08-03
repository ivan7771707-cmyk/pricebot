#!/usr/bin/env python3
"""
Telegram-бот 'Контроль Снабжения' v3.0
- Умный парсинг PDF (игнорирует реквизиты)
- Реальный поиск рыночных цен через Google
"""
import logging
import os
import re
import asyncio
import tempfile
from io import BytesIO
from PIL import Image
import pytesseract
import pdfplumber
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = "8628765612:AAFwGwSnbBXjuI4sXhgEdFIqG-u-jo2wwuY"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Стоп-слова: строки с этими словами НЕ являются товарами
STOP_WORDS = [
    'инн', 'кпп', 'бик', 'огрн', 'окпо', 'расч', 'корр', 'счёт', 'счет №', 'сч.', 'банк',
    'филиал', 'екатеринбург', 'москва', 'санкт', 'итого', 'ндс', 'всего', 'оплата',
    'получатель', 'плательщик', 'руководитель', 'бухгалтер', 'подпись', 'дата',
    'договор', 'основание', 'назначение', 'телефон', 'email', 'адрес', 'ооо', 'ип ',
    'наименование организации', 'юридический', 'фактический', 'в том числе'
]


def is_service_line(line: str) -> bool:
    """Проверяет, является ли строка реквизитом/служебной информацией."""
    line_lower = line.lower()
    for word in STOP_WORDS:
        if word in line_lower:
            return True
    # Строки, состоящие в основном из цифр (ИНН, БИК, номер счёта)
    digits_only = re.sub(r'\D', '', line)
    if len(digits_only) > 15:  # Длинные числа — это реквизиты
        return True
    return False


def extract_items_from_text(text: str) -> list:
    """Умное извлечение товаров и цен из текста счёта."""
    items = []
    lines = text.split('\n')

    for line in lines:
        line = line.strip()
        if not line or len(line) < 5:
            continue

        # Пропускаем служебные строки
        if is_service_line(line):
            continue

        # Ищем цену в строке (формат: число с разделителями, от 3 до 10 цифр)
        # Исключаем слишком большие числа (номера счётов, ИНН)
        price_match = re.search(r'\b(\d{1,3}(?:[\s,]\d{3})*(?:[.,]\d{1,2})?)\s*(?:руб|₽|р\.?)?\s*$', line)
        if not price_match:
            # Попробуем найти цену в любом месте строки
            price_match = re.search(r'\b(\d{2,7}(?:[.,]\d{1,2})?)\b', line)

        if price_match:
            price_str = re.sub(r'[\s,]', '', price_match.group(1).replace(',', '.'))
            try:
                price = float(price_str)
                # Фильтр: цена должна быть реалистичной (от 100 до 50 млн)
                if not (100 <= price <= 50_000_000):
                    continue

                # Название товара — всё до цены
                name = line[:price_match.start()].strip()
                # Убираем лишние символы в конце названия
                name = re.sub(r'[\s\|:,\.]+$', '', name)

                if len(name) < 3:
                    continue

                items.append({'name': name[:80], 'price': price})
            except ValueError:
                continue

    return items


async def search_market_price(product_name: str) -> dict:
    """Реальный поиск рыночной цены через Google."""
    try:
        query = f'купить "{product_name}" цена рублей site:avito.ru OR site:ozon.ru OR site:vseinstrumenti.ru OR site:pulscen.ru'
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        }
        url = f"https://www.google.com/search?q={requests.utils.quote(query)}&num=5&hl=ru"
        resp = requests.get(url, headers=headers, timeout=8)
        soup = BeautifulSoup(resp.text, 'html.parser')

        prices = []
        # Ищем числа, похожие на цены, в результатах поиска
        text = soup.get_text()
        matches = re.findall(r'(\d{1,3}(?:\s\d{3})*)\s*(?:руб|₽|р\.)', text)
        for m in matches:
            try:
                p = float(re.sub(r'\s', '', m))
                if 100 <= p <= 10_000_000:
                    prices.append(p)
            except:
                continue

        if len(prices) >= 2:
            prices.sort()
            # Убираем выбросы (топ и боттом 20%)
            cut = max(1, len(prices) // 5)
            trimmed = prices[cut:-cut] if len(prices) > 4 else prices
            avg = sum(trimmed) / len(trimmed)
            return {"avg": round(avg), "min": min(trimmed), "max": max(trimmed), "found": True}

    except Exception as e:
        logger.warning(f"Ошибка поиска цены: {e}")

    # Если поиск не дал результата
    return {"avg": None, "found": False}


def generate_report(name: str, user_price: float, market: dict) -> str:
    """Генерация отчёта."""
    if not market.get('found') or market['avg'] is None:
        return (
            f"📦 <b>{name}</b>\n"
            f"💰 Ваша цена: {user_price:,.0f} ₽\n"
            f"❓ Рыночная цена: не найдена (редкий/специфический товар)\n"
            f"<i>Рекомендуется запросить 3 коммерческих предложения.</i>\n"
        )

    avg = market['avg']
    diff = user_price - avg
    percent = (diff / avg) * 100

    if percent > 15:
        status = "🔴 ЗАВЫШЕНО"
        verdict = f"Переплата: <b>{diff:,.0f} ₽ (+{percent:.0f}%)</b>"
    elif percent > 5:
        status = "🟡 НЕМНОГО ВЫШЕ РЫНКА"
        verdict = f"Переплата: <b>{diff:,.0f} ₽ (+{percent:.0f}%)</b>"
    elif percent < -10:
        status = "🟢 НИЖЕ РЫНКА (хорошая цена)"
        verdict = f"Экономия: <b>{abs(diff):,.0f} ₽</b>"
    else:
        status = "🟢 НОРМА"
        verdict = "Цена соответствует рынку"

    return (
        f"📦 <b>{name}</b>\n"
        f"💰 Ваша цена: {user_price:,.0f} ₽\n"
        f"🌐 Рынок (средняя): {avg:,.0f} ₽\n"
        f"📢 {status} | {verdict}\n"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 <b>Контроль Снабжения v3.0</b>\n\n"
        "Отправьте мне:\n"
        "🔹 <b>PDF-счёт</b> — разберу позиции и проверю цены\n"
        "🔹 <b>Фото счёта</b> — прочитаю через OCR\n"
        "🔹 <b>Текст:</b> <i>Название товара, цена</i>\n"
        "   Пример: <i>Сплит-система Mitsubishi 9000 BTU, 45000</i>",
        parse_mode='HTML'
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    parts = text.split(',')
    if len(parts) < 2:
        await update.message.reply_text("❌ Формат: <i>Название, Цена</i>", parse_mode='HTML')
        return

    name = parts[0].strip()
    try:
        price = float(re.sub(r'[^\d.]', '', parts[1].replace(',', '.')))
    except:
        await update.message.reply_text("❌ Неверный формат цены.")
        return

    msg = await update.message.reply_text(f"🔍 Ищу рыночную цену для: <b>{name}</b>...", parse_mode='HTML')
    market = await search_market_price(name)
    report = (
        "📊 <b>ОТЧЁТ АУДИТА СНАБЖЕНИЯ</b>\n"
        "━━━━━━━━━━━━━━━\n" +
        generate_report(name, price, market) +
        "\n━━━━━━━━━━━━━━━\n"
        "<i>Для полного аудита отдела снабжения — свяжитесь с нами.</i>"
    )
    await msg.edit_text(report, parse_mode='HTML')


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if document.mime_type != 'application/pdf':
        await update.message.reply_text("❌ Поддерживаются только PDF-файлы.")
        return

    msg = await update.message.reply_text("📥 Получил PDF. Извлекаю товарные позиции...")

    try:
        file = await document.get_file()
        file_bytes = await file.download_as_bytearray()

        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        extracted_text = ""
        with pdfplumber.open(tmp_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    extracted_text += page_text + "\n"
        os.unlink(tmp_path)

        if not extracted_text.strip():
            await msg.edit_text("❌ PDF пустой или содержит только изображения. Отправьте фото счёта.")
            return

        items = extract_items_from_text(extracted_text)

        if not items:
            await msg.edit_text(
                "⚠️ Товарные позиции не найдены автоматически.\n"
                "Введите вручную: <i>Название, Цена</i>",
                parse_mode='HTML'
            )
            return

        await msg.edit_text(f"🔍 Найдено {len(items)} позиций. Проверяю цены по рынку...")

        report = "📄 <b>АНАЛИЗ PDF-СЧЁТА</b>\n━━━━━━━━━━━━━━━\n\n"
        total_overpay = 0

        for item in items[:8]:
            market = await search_market_price(item['name'])
            report += generate_report(item['name'], item['price'], market) + "\n"
            if market.get('found') and market['avg']:
                diff = item['price'] - market['avg']
                if diff > 0:
                    total_overpay += diff

        report += "━━━━━━━━━━━━━━━\n"
        if total_overpay > 0:
            report += f"💸 <b>ИТОГО ПЕРЕПЛАТА: {total_overpay:,.0f} ₽</b>\n⚠️ <i>Рекомендуется проверка закупщика!</i>"
        else:
            report += "✅ <b>Цены в пределах нормы</b>"

        await msg.edit_text(report, parse_mode='HTML')

    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("📥 Получил фото. Запускаю OCR...")
    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()
    try:
        image = Image.open(BytesIO(photo_bytes))
        text = pytesseract.image_to_string(image, lang='rus+eng')
        if not text.strip():
            await msg.edit_text("❌ Не удалось прочитать текст. Попробуйте более чёткое фото.")
            return
        items = extract_items_from_text(text)
        if items:
            await msg.edit_text(f"🔍 Найдено {len(items)} позиций. Анализирую...")
            report = "📷 <b>АНАЛИЗ ФОТО-СЧЁТА</b>\n━━━━━━━━━━━━━━━\n\n"
            for item in items[:5]:
                market = await search_market_price(item['name'])
                report += generate_report(item['name'], item['price'], market) + "\n"
            await msg.edit_text(report, parse_mode='HTML')
        else:
            await msg.edit_text("⚠️ Цены не распознаны. Введите вручную: <i>Название, Цена</i>", parse_mode='HTML')
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка OCR: {e}\nУстановите Tesseract: brew install tesseract")


if __name__ == '__main__':
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    print("Бот запущен (v3.0). Напишите ему в Telegram!")
    application.run_polling()
