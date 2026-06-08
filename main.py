import os, json, time, logging, sys, re
import feedparser
import requests
import cloudscraper
from bs4 import BeautifulSoup
from readability import Document
from google import genai

RSS_URL = "https://1prime.ru/export/rss2/index.xml"
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

DATA_FILE = "posted_guids.json"
MAX_ITEMS_PER_RUN = 5
LOG_FILE = "bot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger()

scraper = cloudscraper.create_scraper()

# ---------- GUID ----------
def load_posted_guids():
    if not os.path.exists(DATA_FILE):
        return set()
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        logger.warning("posted_guids.json is empty, starting fresh.")
        return set()
    try:
        data = json.loads(raw)
        return set(data[-500:])
    except json.JSONDecodeError:
        logger.error("Invalid JSON in posted_guids.json, resetting file.")
        return set()

def save_posted_guids(guids):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(list(guids), f)

# ---------- RSS ----------
def fetch_rss(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.error(f"Failed to download RSS: {e}")
        return None

def get_feed_entries():
    content = fetch_rss(RSS_URL)
    if content is None:
        return []
    feed = feedparser.parse(content)
    if feed.bozo:
        logger.error(f"RSS parse error: {feed.bozo_exception}")
    logger.info(f"RSS feed contains {len(feed.entries)} items.")
    return feed.entries

# ---------- изображение ----------
def extract_image(entry):
    if hasattr(entry, "enclosures") and entry.enclosures:
        enc = entry.enclosures[0]
        if "image" in enc.get("type", ""):
            return enc.href
    if hasattr(entry, "media_content") and entry.media_content:
        for media in entry.media_content:
            if "image" in media.get("type", ""):
                return media.get("url")
    try:
        resp = requests.get(entry.link, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)"
        })
        soup = BeautifulSoup(resp.text, "html.parser")
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            return og_img["content"]
    except Exception as e:
        logger.warning(f"Could not fetch og:image for {entry.link}: {e}")
    return None

# ---------- проверка мусорной строки ----------
def is_garbage_line(line):
    """True, если строка – теги, даты, контакты, обрывки."""
    line = line.strip()
    if not line:
        return True

    # Email / телефон
    if re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', line):
        return True
    if re.search(r'(\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{2,4}[-.\s]?\d{2,4}', line):
        return True

    # Агентства
    if re.search(r'ФГУП|МИА|Россия сегодня|internet-group|РИА Новости|ПРАЙМ', line, re.IGNORECASE):
        return True

    # Дата + источник
    if re.search(r'\d{1,2}\s+\w+|\d{4}', line) and re.search(r'МОСКВА|ПРАЙМ', line, re.IGNORECASE):
        return True

    # ISO дата / чистая дата
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{4}', line):
        return True
    if re.fullmatch(r'\d{2}\.\d{2}\.\d{4}', line):
        return True
    # Год отдельно (20xx)
    if re.fullmatch(r'20\d{2}', line):
        return True
    # Четырёхзначное число (если строка только из него)
    if re.fullmatch(r'\d{4}', line):
        return True

    # URL
    if re.match(r'https?://\S+', line):
        return True

    # Теги через запятую (ключевые слова) – теперь лимит 35 символов на слово
    parts = [p.strip() for p in line.split(',') if p.strip()]
    if len(parts) >= 2 and all(len(w) < 35 for w in parts):
        # Дополнительно: если нет ни одного глагольного окончания, считаем тегами
        if not any(w.endswith(('ть', 'чь', 'лся', 'ется', 'ются', 'ете', 'ают')) for w in parts):
            return True

    # Одиночные короткие слова-теги (1-3 слова, без глаголов)
    words = line.split()
    if 1 <= len(words) <= 3 and all(len(w) < 20 for w in words):
        if not any(w[0].isdigit() for w in words):  # не числа
            return True

    # Строки, начинающиеся со знака препинания (обрывки)
    if line and line[0] in ',.;:!?':
        return True

    return False

def clean_text(raw_text, title=""):
    """Основная очистка."""
    lines = raw_text.splitlines()
    cleaned = []
    prev_ended = True  # предыдущая строка завершала предложение
    
    for line in lines:
        line = line.strip()
        if not line:
            continue

        if is_garbage_line(line):
            continue

        # Удаление обрывков: начинается с маленькой буквы после завершённого предложения
        if line and line[0].islower() and prev_ended:
            continue

        # Повтор заголовка
        if title and line.lower() == title.lower():
            continue

        cleaned.append(line)
        prev_ended = line.endswith(('.', '!', '?'))

    return "\n".join(cleaned)

# ---------- извлечение статьи ----------
def extract_article_text(url, fallback_description=""):
    try:
        resp = scraper.get(url, timeout=15)
        resp.raise_for_status()
        doc = Document(resp.text)
        soup = BeautifulSoup(doc.summary(), "html.parser")
        for tag in soup(["script", "style", "img", "figure", "figcaption"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        if len(text) > 200:
            logger.info("Полный текст получен через cloudscraper.")
            return text[:9000]
    except Exception as e:
        logger.warning(f"Cloudscraper error for {url}: {e}")

    if fallback_description and len(fallback_description.strip()) > 30:
        logger.info("Используем описание из RSS.")
        return fallback_description.strip()[:9000]
    return None

# ---------- эмодзи по тематике ----------
def add_emoji_prefix(text):
    """Добавляет эмодзи к тексту, убрав возможные оставшиеся годы в начале."""
    # Сначала удалим ведущие строки, которые выглядят как год (20xx) или просто цифры
    lines = text.splitlines()
    while lines and re.fullmatch(r'20\d{2}', lines[0].strip()):
        lines.pop(0)
    text = "\n".join(lines).strip()
    
    lower = text.lower()
    if any(w in lower for w in ['акци', 'биржа', 'индекс', 'торг', 's&p', 'nasdaq', 'инвест']):
        return "📈 " + text
    if any(w in lower for w in ['нефть', 'газ', 'топлив', 'энерг']):
        return "🛢️ " + text
    if any(w in lower for w in ['банк', 'кредит', 'финанс', 'втб', 'сбер']):
        return "🏦 " + text
    if any(w in lower for w in ['доллар', 'валюта', 'рубл']):
        return "💵 " + text
    return "🔹 " + text

# ---------- ИИ рерайт ----------
def ai_rewrite(original_text, image_url=None):
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = (
            "Ты — редактор телеграм-канала об инвестициях и финансах.\n"
            "Перепиши новость в яркий и лаконичный пост для Telegram.\n"
            "Правила:\n"
            "- Используй эмодзи (🔹, 📈, 💡 и т.п.)\n"
            "- Сохрани ключевые цифры и факты\n"
            "- Максимум 800 символов (включая эмодзи и пробелы)\n"
            "- Не упоминай источник (ПРАЙМ, МОСКВА, РИА) и дату\n"
            "- Не вставляй контактные данные\n"
            "- Заверши пост призывом подписаться на канал @Investing_24 (не более одной строки)\n\n"
            f"Исходная статья:\n{original_text}"
        )
        response = client.models.generate_content(
            model="gemini-1.5-flash",   # самая надёжная бесплатная модель
            contents=prompt,
        )
        if response.text:
            logger.info("Gemini успешно переписал текст.")
            return response.text.strip()
        else:
            logger.error("Gemini вернул пустой ответ.")
            return None
    except Exception as e:
        logger.error(f"Ошибка Gemini API: {e}")
        return None

# ---------- отправка в Telegram ----------
def send_telegram_post(text, image_url):
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    if image_url:
        payload = {
            "chat_id": CHANNEL_ID,
            "photo": image_url,
            "caption": text,
            "parse_mode": "HTML",
        }
        method = "sendPhoto"
    else:
        payload = {
            "chat_id": CHANNEL_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        method = "sendMessage"
    try:
        resp = requests.post(f"{base}/{method}", data=payload, timeout=20)
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            logger.error(f"Telegram API error: {result}")
            return False
        logger.info("Пост успешно отправлен.")
        return True
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return False

# ---------- умное обрезание ----------
def trim_text(text, max_len=900):
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    for sep in ['.', '!', '?']:
        pos = cut.rfind(sep)
        if pos > 400:
            return cut[:pos+1]
    last_space = cut.rfind(' ')
    return cut[:last_space] + "…" if last_space > 0 else cut + "…"

# ---------- главный цикл ----------
def main():
    logger.info("=== Запуск бота ===")
    posted = load_posted_guids()
    entries = get_feed_entries()
    if not entries:
        logger.warning("Новостей в ленте нет. Выход.")
        return

    logger.info(f"Найдено {len(entries)} записей, обработано ранее: {len(posted)}.")
    new_items = 0
    posted_changed = False

    for entry in entries:
        guid = entry.get("id") or entry.link
        if guid in posted:
            continue
        if new_items >= MAX_ITEMS_PER_RUN:
            break

        title = entry.get("title", "")
        logger.info(f"Обрабатываю: {title}")

        image_url = extract_image(entry)
        description = entry.get("description", "")
        raw_text = extract_article_text(entry.link, fallback_description=description)
        if not raw_text:
            raw_text = title

        cleaned_text = clean_text(raw_text, title=title)
        if not cleaned_text:
            cleaned_text = title

        # Попробовать ИИ-рерайт
        edited = ai_rewrite(cleaned_text, image_url)

        if not edited:
            # Fallback – очищенный текст с эмодзи и обрезанием
            fallback = cleaned_text
            # Убираем возможные оставшиеся теги, которые проскочили (повторная очистка)
            fallback = clean_text(fallback, title="")  # без сравнения с заголовком
            edited = trim_text(fallback, 900)
            edited = add_emoji_prefix(edited)
            if "@Investing_24" not in edited:
                edited += "\n\nПодпишись на канал @Investing_24"
        else:
            # Gemini сработал – проверяем длину
            if len(edited) > 1024:
                edited = trim_text(edited, 900)

        success = send_telegram_post(edited, image_url)
        if success:
            posted.add(guid)
            new_items += 1
            posted_changed = True
            time.sleep(2)
        else:
            logger.error(f"Не удалось отправить пост для {entry.link}")

    if posted_changed:
        save_posted_guids(posted)
        logger.info(f"Сохранено GUID: добавлено {new_items} новых.")
    else:
        logger.info("Новых записей нет.")

if __name__ == "__main__":
    main()
