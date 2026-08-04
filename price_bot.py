#!/usr/bin/env python3
"""
Telegram-бот 'Контроль Снабжения' v3.4
Исправления:
- Автоматический поиск пути к Tesseract
- Диагностика ошибок поиска (вывод статусов в чат)
- Улучшенный обход блокировок (Random User-Agent, Referer)
"""
import logging
import os
import re
import asyncio
import tempfile
import random
import shutil
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

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Настройка Tesseract ---
def setup_tesseract():
    # Проверяем стандартные пути в Linux/Railway
    paths = ['/usr/bin/tesseract', '/usr/local/bin/tesseract', '/nix/var/nix/profiles/default/bin/tesseract']
    found_path = shutil.which('tesseract')
    
    if not found_path:
        for p in paths:
            if os.path.exists(p):
                found_path = p
                break
    
    if found_path:
        pytesseract.pytesseract.tesseract_cmd = found_path
        logger.info(f"Tesseract found at: {found_path}")
    else:
        logger.warning("Tesseract NOT found in PATH")

setup_tesseract()

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
        if word in line_lower: return True
    return len(re.sub(r'\D', '', line)) > 15

def extract_items_from_text(text: str) -> list:
    items = []
    lines = text.split('\n')
    for line in lines:
        line = line.strip()
        if not line or len(line) < 5 or is_service_line(line): continue
        price_match = re.search(r'(\d{1,3}(?:[\s,]\d{3})*(?:[.,]\d{1,2})?)\s*(?:руб|₽|р\.?|rub)?\s*$', line, re.IGNORECASE)
        if not price_match: price_match = re.search(r'\b(\d{2,8}(?:[.,]\d{1,2})?)\b', line)
        if price_match:
            try:
                price_str = re.sub(r'[^\d.]', '', price_match.group(1).replace(' ', '').replace(',', '.'))
                price = float(price_str)
                name = re.sub(r'[\s\|:,\.\-\_]+$', '', line[:price_match.start()].strip())
                name = re.sub(r'^\d+[\s\.]+', '', name)
                if len(name) >= 3: items.append({'name': name[:100], 'price': price})
            except: continue
    return items

async def fetch_prices(url, source, headers):
    try:
        # Добавляем случайную задержку
        await asyncio.sleep(random.uniform(1, 3))
        resp = requests.get(url, headers=headers, timeout=12)
        logger.info(f"{source} Status: {resp.status_code}")
        
        if resp.status_code != 200:
            return [], f"{source}: {resp.status_code}"
            
        if "detected unusual traffic" in resp.text or "captcha" in resp.text.lower():
            return [], f"{source}: CAPTCHA"

        text = BeautifulSoup(resp.text, 'html.parser').get_text(separator=' ')
        matches = re.findall(r'(\d{1,3}(?:[\s\xa0\.]\d{3})*(?:[.,]\d{1,2})?)\s*(?:руб|₽|р\.)', text)
        
        found = []
        for m in matches:
            p_str = re.sub(r'[^\d.]', '', m.replace('\xa0', '').replace(' ', '').replace('.', '').replace(',', '.'))
            try:
                p = float(p_str)
                if 100 <= p <= 50_000_000: found.append(p)
            except: continue
        return found, f"{source}: OK ({len(found)})"
    except Exception as e:
        return [], f"{source}: Error ({str(e)[:20]})"

async def search_market_price(product_name: str) -> dict:
    agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ]
    
    headers = {
        'User-Agent': random.choice(agents),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8',
        'Referer': 'https://www.google.com/'
    }

    clean_name = re.sub(r'[^\w\s\d]', ' ', product_name).strip()
    all_prices = []
    logs = []

    # 1. Google
    p, log = await fetch_prices(f"https://www.google.com/search?q={requests.utils.quote('купить ' + clean_name + ' цена')}&hl=ru", "GGL", headers)
    all_prices.extend(p); logs.append(log)

    # 2. Yandex
    if len(all_prices) < 3:
        p, log = await fetch_prices(f"https://yandex.ru/search/?text={requests.utils.quote(clean_name + ' цена руб')}", "YNDX", headers)
        all_prices.extend(p); logs.append(log)

    # 3. DuckDuckGo
    if len(all_prices) < 2:
        p, log = await fetch_prices(f"https://html.duckduckgo.com/html/?q={requests.utils.quote(product_name + ' цена руб')}", "DDG", headers)
        all_prices.extend(p); logs.append(log)

    if all_prices:
        all_prices.sort()
        trimmed = all_prices[1:-1] if len(all_prices) > 4 else all_prices
        avg = sum(trimmed) / len(trimmed)
        return {"avg": round(avg), "found": True, "diag": " | ".join(logs)}
    
    return {"avg": None, "found": False, "diag": " | ".join(logs)}

def generate_report(name: str, user_price: float, market: dict) -> str:
    diag_info = f"\n<i>Отладка: {market.get('diag', 'нет данных')}</i>"
    if not market.get('found') or market['avg'] is None:
        return f"📦 <b>{name}</b>\n💰 Ваша цена: {user_price:,.0f} ₽\n❓ Рыночная цена: не найдена{diag_info}\n"

    avg = market['avg']
    diff = user_price - avg
    percent = (diff / avg) * 100
    status = "🔴 ЗАВЫШЕНО" if percent > 15 else "🟡 ВЫШЕ РЫНКА" if percent > 5 else "🟢 НИЖЕ РЫНКА" if percent < -10 else "🟢 НОРМА"
    verdict = f"Переплата: <b>{diff:,.0f} ₽ (+{percent:.0f}%)</b>" if diff > 0 else f"Экономия: <b>{abs(diff):,.0f} ₽</b>"
    if abs(percent) <= 5: verdict = "Цена в рынке"

    return f"📦 <b>{name}</b>\n💰 Ваша цена: {user_price:,.0f} ₽\n🌐 Рынок (средняя): {avg:,.0f} ₽\n📢 {status} | {verdict}{diag_info}\n"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 <b>Контроль Снабжения v3.4</b>\nОтправьте PDF, фото или 'Товар, цена'.", parse_mode='HTML')

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ',' not in update.message.text: return
    parts = update.message.text.rsplit(',', 1)
    name, price_raw = parts[0].strip(), parts[1]
    try:
        price = float(re.sub(r'[^\d.]', '', price_raw.replace(',', '.')))
    except: return
    msg = await update.message.reply_text(f"🔍 Ищу цену для: <b>{name}</b>...", parse_mode='HTML')
    market = await search_market_price(name)
    await msg.edit_text(f"📊 <b>ОТЧЁТ</b>\n━━━━━━━━━━━━━━━\n" + generate_report(name, price, market) + "━━━━━━━━━━━━━━━", parse_mode='HTML')

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document or update.message.document.mime_type != 'application/pdf': return
    msg = await update.message.reply_text("📥 Читаю PDF...")
    try:
        file = await update.message.document.get_file()
        file_bytes = await file.download_as_bytearray()
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(file_bytes); tmp_path = tmp.name
        text = ""
        with pdfplumber.open(tmp_path) as pdf:
            for page in pdf.pages: text += (page.extract_text() or "") + "\n"
        os.unlink(tmp_path)
        items = extract_items_from_text(text)
        if not items:
            await msg.edit_text("⚠️ Товары не найдены."); return
        await msg.edit_text(f"🔍 Найдено {len(items)} поз. Сверяю...")
        report = "📄 <b>АНАЛИЗ PDF</b>\n━━━━━━━━━━━━━━━\n\n"
        for item in items[:5]:
            market = await search_market_price(item['name'])
            report += generate_report(item['name'], item['price'], market) + "\n"
        await msg.edit_text(report, parse_mode='HTML')
    except Exception as e: await msg.edit_text(f"❌ Ошибка: {e}")

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
        else: await msg.edit_text("⚠️ Цены не найдены.")
    except Exception as e: await msg.edit_text(f"❌ Ошибка OCR: {e}")

if __name__ == '__main__':
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.run_polling()
