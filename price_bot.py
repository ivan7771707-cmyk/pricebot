#!/usr/bin/env python3
"""
Telegram-бот 'Контроль Снабжения' v3.3
Исправления для Railway:
- Добавлена ротация User-Agent и использование Session
- Добавлен поиск по конкретным маркетплейсам (запасной вариант)
- Расширенное логирование для отладки на сервере
- Улучшен парсинг цен из текстовых блоков
"""
import logging
import os
import re
import asyncio
import tempfile
import random
import time
from io import BytesIO
from PIL import Image
import pytesseract
import pdfplumber
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ТОКЕН БОТА
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8628765612:AAFwGwSnbBXjuI4sXhgEdFIqG-u-jo2wwuY")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

STOP_WORDS = [
    'инн', 'кпп', 'бик', 'огрн', 'окпо', 'расч', 'корр', 'счёт', 'счет №', 'сч.', 'банк',
    'филиал', 'екатеринбург', 'москва', 'санкт', 'итого', 'ндс', 'всего', 'оплата',
    'получатель', 'плательщик', 'руководитель', 'бухгалтер', 'подпись', 'дата',
    'договор', 'основание', 'назначение', 'телефон', 'email', 'адрес', 'ооо', 'ип ',
    'наименование организации', 'юридический', 'фактический', 'в том числе'
]

def is_service_line(line: str) -> bool:
    line_lower = line.lower()
    for word in STOP_WORDS:
        if word in line_lower:
            return True
    digits_only = re.sub(r'\D', '', line)
    return len(digits_only) > 15

def extract_items_from_text(text: str) -> list:
    items = []
    lines = text.split('\n')
    for line in lines:
        line = line.strip()
        if not line or len(line) < 5 or is_service_line(line):
            continue
        
        # Поиск цены: число в конце или с валютой
        price_match = re.search(r'(\d{1,3}(?:[\s,]\d{3})*(?:[.,]\d{1,2})?)\s*(?:руб|₽|р\.?|rub)?\s*$', line, re.IGNORECASE)
        if not price_match:
            price_match = re.search(r'\b(\d{2,8}(?:[.,]\d{1,2})?)\b', line)
            
        if price_match:
            try:
                price_str = re.sub(r'[^\d.]', '', price_match.group(1).replace(' ', '').replace(',', '.'))
                price = float(price_str)
                if 50 <= price <= 100_000_000:
                    name = line[:price_match.start()].strip()
                    name = re.sub(r'[\s\|:,\.\-\_]+$', '', name)
                    name = re.sub(r'^\d+[\s\.]+', '', name)
                    if len(name) >= 3:
                        items.append({'name': name[:100], 'price': price})
            except:
                continue
    return items

async def get_prices_from_url(session, url, headers, source_name):
    """Вспомогательная функция для парсинга цен с URL."""
    try:
        resp = session.get(url, headers=headers, timeout=10)
        logger.info(f"[{source_name}] Status: {resp.status_code}")
        
        if resp.status_code != 200:
            return []
            
        if "detected unusual traffic" in resp.text or "капча" in resp.text.lower():
            logger.warning(f"[{source_name}] Blocked by CAPTCHA/Bot detection")
            return []

        soup = BeautifulSoup(resp.text, 'html.parser')
        # Удаляем скрипты и стили
        for script in soup(["script", "style"]):
            script.decompose()
            
        text = soup.get_text(separator=' ')
        # Улучшенный regex для поиска цен в тексте
        matches = re.findall(r'(\d{1,3}(?:[\s\xa0\.]\d{3})*(?:[.,]\d{1,2})?)\s*(?:руб|₽|р\.)', text)
        
        found = []
        for m in matches:
            # Очистка: убираем точки-разделители тысяч, запятую меняем на точку
            p_str = m.replace('\xa0', '').replace(' ', '')
            if '.' in p_str and ',' in p_str: # Формат 1.234,56
                p_str = p_str.replace('.', '').replace(',', '.')
            elif ',' in p_str: # Формат 1234,56
                p_str = p_str.replace(',', '.')
            # Если точек несколько (1.234.567), убираем все кроме последней или все если это разделители
            if p_str.count('.') > 1:
                p_str = p_str.replace('.', '')
                
            try:
                p = float(p_str)
                if 100 <= p <= 50_000_000:
                    found.append(p)
            except: continue
            
        logger.info(f"[{source_name}] Found {len(found)} price matches")
        return found
    except Exception as e:
        logger.error(f"[{source_name}] Error: {e}")
        return []

async def search_market_price(product_name: str) -> dict:
    """Глобальный поиск цен с ротацией и запасными вариантами."""
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0'
    ]
    
    headers = {
        'User-Agent': random.choice(user_agents),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
    }

    session = requests.Session()
    prices = []
    clean_name = re.sub(r'[^\w\s\d]', ' ', product_name).strip()
    
    # 1. Поиск в Google
    google_url = f"https://www.google.com/search?q={requests.utils.quote('купить ' + clean_name + ' цена')}&hl=ru"
    prices.extend(await get_prices_from_url(session, google_url, headers, "Google"))

    # 2. Поиск в Яндексе (если мало результатов)
    if len(prices) < 3:
        yandex_url = f"https://yandex.ru/search/?text={requests.utils.quote(clean_name + ' цена руб')}"
        prices.extend(await get_prices_from_url(session, yandex_url, headers, "Yandex"))

    # 3. Поиск в DuckDuckGo (надежный fallback)
    if len(prices) < 2:
        ddg_url = f"https://html.duckduckgo.com/html/?q={requests.utils.quote(product_name + ' цена руб')}"
        prices.extend(await get_prices_from_url(session, ddg_url, headers, "DuckDuckGo"))

    # 4. Прямой поиск на Pulscen (часто открыт для ботов)
    if len(prices) < 2:
        pulscen_url = f"https://www.pulscen.ru/search/price?q={requests.utils.quote(product_name)}"
        prices.extend(await get_prices_from_url(session, pulscen_url, headers, "Pulscen"))

    if len(prices) >= 1:
        prices.sort()
        # Фильтрация выбросов
        if len(prices) > 4:
            cut = max(1, len(prices) // 5)
            trimmed = prices[cut:-cut]
        else:
            trimmed = prices
        
        avg = sum(trimmed) / len(trimmed)
        logger.info(f"Final result for '{product_name}': avg={avg}, count={len(prices)}")
        return {
            "avg": round(avg), 
            "min": min(trimmed), 
            "max": max(trimmed), 
            "found": True,
            "count": len(prices)
        }
    
    logger.warning(f"No prices found for '{product_name}'")
    return {"avg": None, "found": False}

def generate_report(name: str, user_price: float, market: dict) -> str:
    if not market.get('found') or market['avg'] is None:
        return (
            f"📦 <b>{name}</b>\n"
            f"💰 Ваша цена: {user_price:,.0f} ₽\n"
            f"❓ Рыночная цена: не найдена\n"
            f"<i>Попробуйте сократить название товара.</i>\n"
        )

    avg = market['avg']
    diff = user_price - avg
    percent = (diff / avg) * 100
    status = "🟢 НОРМА"
    if percent > 15: status = "🔴 ЗАВЫШЕНО"
    elif percent > 5: status = "🟡 ВЫШЕ РЫНКА"
    elif percent < -10: status = "🟢 НИЖЕ РЫНКА"
    
    verdict = f"Переплата: <b>{diff:,.0f} ₽ (+{percent:.0f}%)</b>" if diff > 0 else f"Экономия: <b>{abs(diff):,.0f} ₽</b>"
    if abs(percent) <= 5: verdict = "Цена в рынке"

    return (
        f"📦 <b>{name}</b>\n"
        f"💰 Ваша цена: {user_price:,.0f} ₽\n"
        f"🌐 Рынок (средняя): {avg:,.0f} ₽\n"
        f"📢 {status} | {verdict}\n"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 <b>Контроль Снабжения v3.3</b>\n\n"
        "Отправьте мне <b>PDF-счёт</b>, <b>фото</b> или <b>текст</b> (<i>Товар, цена</i>).",
        parse_mode='HTML'
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if ',' not in text:
        await update.message.reply_text("❌ Формат: <i>Название, Цена</i>")
        return
    parts = text.rsplit(',', 1)
    name, price_raw = parts[0].strip(), parts[1]
    try:
        price = float(re.sub(r'[^\d.]', '', price_raw.replace(',', '.')))
    except:
        await update.message.reply_text("❌ Ошибка в цене.")
        return
    
    msg = await update.message.reply_text(f"🔍 Ищу рыночную цену для: <b>{name}</b>...", parse_mode='HTML')
    market = await search_market_price(name)
    await msg.edit_text(f"📊 <b>ОТЧЁТ</b>\n━━━━━━━━━━━━━━━\n" + generate_report(name, price, market) + "━━━━━━━━━━━━━━━", parse_mode='HTML')

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document or document.mime_type != 'application/pdf':
        await update.message.reply_text("❌ Нужен PDF.")
        return
    msg = await update.message.reply_text("📥 Читаю PDF...")
    try:
        file = await document.get_file()
        file_bytes = await file.download_as_bytearray()
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        
        extracted_text = ""
        with pdfplumber.open(tmp_path) as pdf:
            for page in pdf.pages:
                extracted_text += (page.extract_text() or "") + "\n"
        os.unlink(tmp_path)
        
        items = extract_items_from_text(extracted_text)
        if not items:
            await msg.edit_text("⚠️ Товары не найдены.")
            return
            
        await msg.edit_text(f"🔍 Найдено {len(items)} поз. Проверяю...")
        report = "📄 <b>АНАЛИЗ PDF</b>\n━━━━━━━━━━━━━━━\n\n"
        for item in items[:5]:
            market = await search_market_price(item['name'])
            report += generate_report(item['name'], item['price'], market) + "\n"
            await asyncio.sleep(1) # Небольшая пауза между запросами
        await msg.edit_text(report, parse_mode='HTML')
    except Exception as e:
        logger.error(f"PDF Error: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("📥 Распознаю фото...")
    try:
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        image = Image.open(BytesIO(photo_bytes))
        text = pytesseract.image_to_string(image, lang='rus+eng')
        items = extract_items_from_text(text)
        if items:
            report = "📷 <b>АНАЛИЗ ФОТО</b>\n━━━━━━━━━━━━━━━\n\n"
            for item in items[:3]:
                market = await search_market_price(item['name'])
                report += generate_report(item['name'], item['price'], market) + "\n"
            await msg.edit_text(report, parse_mode='HTML')
        else:
            await msg.edit_text("⚠️ Цены не найдены.")
    except Exception as e:
        logger.error(f"Photo Error: {e}")
        await msg.edit_text(f"❌ Ошибка OCR: {e}")

if __name__ == '__main__':
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    logger.info("Бот v3.3 запущен.")
    application.run_polling()
