#!/usr/bin/env python3
"""
Telegram-бот 'Контроль Снабжения' v3.8
- Добавлены прямые источники: ВсеИнструменты.ру и Оптогаджет.ру
- Оптимизирована очередность поиска
- Улучшена фильтрация для электроники и инструментов
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

def setup_tesseract():
    found_path = shutil.which('tesseract')
    if not found_path:
        for p in ['/usr/bin/tesseract', '/usr/local/bin/tesseract', '/nix/var/nix/profiles/default/bin/tesseract']:
            if os.path.exists(p): found_path = p; break
    if found_path: pytesseract.pytesseract.tesseract_cmd = found_path
    return found_path

TESS_PATH = setup_tesseract()

STOP_WORDS = ['инн', 'кпп', 'бик', 'огрн', 'окпо', 'расч', 'корр', 'счёт', 'счет №', 'сч.', 'банк', 'филиал', 'екатеринбург', 'москва', 'санкт', 'итого', 'ндс', 'всего', 'оплата', 'получатель', 'плательщик', 'руководитель', 'бухгалтер', 'подпись', 'дата', 'договор', 'основание', 'назначение', 'телефон', 'email', 'адрес', 'ооо', 'ип ', 'наименование организации', 'юридический', 'фактический', 'в том числе']

def is_service_line(line: str) -> bool:
    line_lower = line.lower()
    for word in STOP_WORDS:
        if word in line_lower: return True
    return len(re.sub(r'\D', '', line)) > 15

def extract_items_from_text(text: str) -> list:
    items = []
    for line in text.split('\n'):
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

async def fetch_prices(url, source, headers, user_price=None):
    try:
        await asyncio.sleep(random.uniform(1, 2))
        resp = requests.get(url, headers=headers, timeout=12)
        if resp.status_code != 200: return [], f"{source}:{resp.status_code}"
        if "captcha" in resp.text.lower() or "unusual traffic" in resp.text: return [], f"{source}:CAP"
        
        text = BeautifulSoup(resp.text, 'html.parser').get_text(separator=' ')
        # Ищем числа с валютой
        matches = re.findall(r'(\d{1,3}(?:[\s\xa0\.]\d{3})*(?:[.,]\d{1,2})?)\s*(?:руб|₽|р\.)', text)
        
        found = []
        for m in matches:
            p_str = re.sub(r'[^\d.]', '', m.replace('\xa0', '').replace(' ', '').replace('.', '').replace(',', '.'))
            try:
                p = float(p_str)
                if user_price:
                    # Фильтр: от 25% до 400% от цены пользователя
                    if user_price * 0.25 <= p <= user_price * 4.0: found.append(p)
                elif p >= 300: found.append(p)
            except: continue
        return found, f"{source}:OK({len(found)})"
    except: return [], f"{source}:ERR"

async def search_market_price(product_name: str, user_price: float) -> dict:
    agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1'
    ]
    headers = {'User-Agent': random.choice(agents), 'Accept-Language': 'ru-RU,ru;q=0.9', 'Referer': 'https://www.google.com/'}
    clean_name = re.sub(r'[^\w\s\d]', ' ', product_name).strip()
    all_prices, logs = [], []
    
    # 1. Спец. источники (ваши приоритетные)
    special_sources = [
        (f"https://www.vseinstrumenti.ru/search/?what={requests.utils.quote(product_name)}", "VSI"),
        (f"https://optogadget.ru/search/?q={requests.utils.quote(product_name)}", "OPTG"),
        (f"https://price.ru/search/?query={requests.utils.quote(product_name)}", "PRC"),
        (f"https://www.pulscen.ru/search/price?q={requests.utils.quote(product_name)}", "PLS")
    ]
    
    # 2. Поисковики (запасные)
    search_engines = [
        (f"https://www.google.com/search?q={requests.utils.quote('купить ' + clean_name + ' цена -чехол')}&hl=ru", "GGL"),
        (f"https://yandex.ru/search/?text={requests.utils.quote(clean_name + ' цена -аксессуары')}", "YNDX"),
        (f"https://html.duckduckgo.com/html/?q={requests.utils.quote(product_name + ' цена руб')}", "DDG")
    ]
    
    for url, name in (special_sources + search_engines):
        p, log = await fetch_prices(url, name, headers, user_price)
        all_prices.extend(p); logs.append(log)
        # Если нашли достаточно цен из надежных источников, не идем дальше
        if len(all_prices) >= 4 and name in ["VSI", "OPTG", "PRC"]: break

    if all_prices:
        all_prices.sort()
        # Отсекаем выбросы
        trimmed = all_prices[1:-1] if len(all_prices) > 3 else all_prices
        avg = sum(trimmed) / len(trimmed)
        return {"avg": round(avg), "found": True, "diag": " | ".join(logs)}
    return {"avg": None, "found": False, "diag": " | ".join(logs)}

def generate_report(name: str, user_price: float, market: dict) -> str:
    diag = f"\n<pre>Источники: {market.get('diag', '-')}</pre>"
    if not market.get('found') or market['avg'] is None:
        return f"📦 <b>{name}</b>\n💰 Ваша цена: {user_price:,.0f} ₽\n❓ Рынок: не найден{diag}\n"
    
    avg = market['avg']
    diff = user_price - avg
    percent = (diff / avg) * 100
    status = "🔴 ЗАВЫШЕНО" if percent > 15 else "🟡 ВЫШЕ РЫНКА" if percent > 5 else "🟢 НИЖЕ РЫНКА" if percent < -10 else "🟢 НОРМА"
    verdict = f"Переплата: <b>{diff:,.0f} ₽ (+{percent:.0f}%)</b>" if diff > 0 else f"Экономия: <b>{abs(diff):,.0f} ₽</b>"
    
    return f"📦 <b>{name}</b>\n💰 Ваша цена: {user_price:,.0f} ₽\n🌐 Рынок: {avg:,.0f} ₽\n📢 {status} | {verdict}{diag}\n"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = "✅ OCR OK" if TESS_PATH else "⚠️ OCR OFF"
    await update.message.reply_text(f"🚀 <b>Контроль Снабжения v3.8</b>\nСтатус: {status}\nИсточники: VSI, OPTG, PRC, PLS + Поиск\nОтправьте PDF, фото или 'Товар, цена'.", parse_mode='HTML')

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ',' not in update.message.text: return
    parts = update.message.text.rsplit(',', 1)
    try:
        name, price = parts[0].strip(), float(re.sub(r'[^\d.]', '', parts[1].replace(',', '.')))
        msg = await update.message.reply_text(f"🔍 Анализ рынка (вкл. VSI, OPTG): {name}...")
        market = await search_market_price(name, price)
        await msg.edit_text(generate_report(name, price, market), parse_mode='HTML')
    except: pass

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not TESS_PATH: return
    msg = await update.message.reply_text("📥 Читаю фото...")
    try:
        photo = await update.message.photo[-1].get_file()
        img = Image.open(BytesIO(await photo.download_as_bytearray()))
        text = pytesseract.image_to_string(img, lang='rus+eng')
        items = extract_items_from_text(text)
        if items:
            res = "📷 <b>АНАЛИЗ:</b>\n\n"
            for item in items[:3]:
                market = await search_market_price(item['name'], item['price'])
                res += generate_report(item['name'], item['price'], market)
            await msg.edit_text(res, parse_mode='HTML')
        else: await msg.edit_text("⚠️ Товары не найдены.")
    except Exception as e: await msg.edit_text(f"❌ Ошибка: {e}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.document.mime_type != 'application/pdf': return
    msg = await update.message.reply_text("📥 Анализ PDF...")
    try:
        file = await update.message.document.get_file()
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(await file.download_as_bytearray()); path = tmp.name
        text = ""
        with pdfplumber.open(path) as pdf:
            for p in pdf.pages: text += (p.extract_text() or "") + "\n"
        os.unlink(path)
        items = extract_items_from_text(text)
        if items:
            res = "📄 <b>АНАЛИЗ СЧЁТА:</b>\n\n"
            for item in items[:5]:
                market = await search_market_price(item['name'], item['price'])
                res += generate_report(item['name'], item['price'], market)
            await msg.edit_text(res, parse_mode='HTML')
        else: await msg.edit_text("⚠️ Товары не найдены.")
    except Exception as e: await msg.edit_text(f"❌ Ошибка: {e}")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.run_polling()
