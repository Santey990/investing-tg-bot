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

def fetch_rss(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
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

def clean_text(raw_text, title=""):
    """
    Удаляем URL, даты, время, строки с 'ПРАЙМ', 'МОСКВА', теги-ключевики.
    Оставляем только связный текст.
    """
    lines = raw_text.splitlines()
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Удалить чистые URL
        if re.match(r'https?://\S+', line):
            continue

        # Удалить строки с датой и источником (например, "МОСКВА, 8 июн - ПРАЙМ")
        if re.search(r'МОСКВА|ПРАЙМ', line, re.IGNORECASE) and re.search(r'\d{1,2}\s+\w+|\d{4}', line):
            continue

        # Удалить строки только с датой и временем (например, "2026-06-08T16:45+0300")
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{4}', line):
            continue

        # Удалить строки, состоящие только из даты ("08.06.2026")
        if re.fullmatch(r'\d{2}\.\d{2}\.\d{4}', line):
            continue

        # Удалить строки-теги (1-2 коротких слова, обычно это ключевые слова)
        words = line.split()
        if len(words) <= 2 and all(len(w) < 15 for w in words):
            continue

        # Удалить повторение заголовка (если строка полностью совпадает с заголовком)
        if title and line.lower() == title.lower():
            continue

        cleaned.append(line)

    return "\n".join(cleaned)

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
            return text[:8000]   # увеличили лимит
    except Exception as e:
        logger.warning(f"Cloudscraper error for {url}: {e}")

    if fallback_description and len(fallback_description.strip()) > 30:
        logger.info("Используем описание из RSS.")
        return fallback_description.strip()[:8000]

    logger.warning("Нет ни полного текста, ни описания.")
    return None

def ai_rewrite(original_text, image_url=None):
    """
    Отправляет текст в Gemini и получает переписанный пост до 800 символов.
    """
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        "Ты — редактор телеграм-канала об инвестициях и финансах.\n"
        "Перепиши новость в яркий и лаконичный пост для Telegram.\n"
        "Правила:\n"
        "- Используй эмодзи (🔹, 📈, 💡 и т.п.)\n"
        "- Сохрани ключевые цифры и факты\n"
        "- Максимум 800 символов (включая эмодзи и пробелы)\n"
        "- Не упоминай источник (ПРАЙМ, МОСКВА) и дату\n"
        "- Не перечисляй ключевые слова (теги) в начале\n"
        "- Заверши пост призывом подписаться на канал @Investing_24 (не более одной строки)\n\n"
        f"Исходная статья:\n{original_text}"
    )
    try:
        response = client.models.generate_content(
            model="gemini-1.5-pro",
            contents=prompt,
        )
        if response.text:
            return response.text.strip()
        else:
            logger.error("Gemini вернул пустой ответ.")
            return None
    except Exception as e:
        logger.error(f"Ошибка Gemini API: {e}")
        return None

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

        # Очищаем текст от мусора
        cleaned_text = clean_text(raw_text, title=title)
        if not cleaned_text:
            cleaned_text = title

        # Просим Gemini переписать
        edited = ai_rewrite(cleaned_text, image_url)
        if not edited:
            # Fallback – берём очищенный текст (первые 800 символов)
            edited = cleaned_text[:800]
            if len(cleaned_text) > 800:
                edited += "…"

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
