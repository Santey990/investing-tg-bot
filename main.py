import os
import json
import time
import logging
import hashlib
import re
import sys

import feedparser
import requests
import cloudscraper

from bs4 import BeautifulSoup
from readability import Document

from google import genai
from google.genai.errors import APIError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ==================== CONFIG ====================
RSS_URLS = [
    "https://1prime.ru/export/rss2/index.xml"
]

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

DATA_FILE = "posted_guids.json"
MAX_ITEMS_PER_RUN = 5
MAX_GEMINI_CALLS_PER_RUN = 2  # <-- Лимит вызовов Gemini за один запуск
LOG_FILE = "bot.log"

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

scraper = cloudscraper.create_scraper()

# ==================== GUID ====================
def load_posted_guids():
    if not os.path.exists(DATA_FILE):
        return set()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_posted_guids(guids):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(list(guids), f, ensure_ascii=False)

def make_guid(entry):
    if getattr(entry, "id", None):
        return entry.id
    return hashlib.md5(
        (entry.link + entry.get("title", "")).encode("utf-8")
    ).hexdigest()

# ==================== RSS ====================
def fetch_rss(url):
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20
        )
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.error(f"RSS error: {e}")
        return None

def get_all_entries():
    entries = []
    for url in RSS_URLS:
        content = fetch_rss(url)
        if not content:
            continue
        feed = feedparser.parse(content)
        logger.info(f"Получено {len(feed.entries)} записей из {url}")
        for item in feed.entries:
            entries.append(item)
    return entries

# ==================== IMAGE ====================
def is_valid_image_url(url):
    return url and isinstance(url, str) and url.startswith(("http://", "https://"))

def extract_image(entry):
    # Пробуем enclosures
    try:
        if hasattr(entry, "enclosures"):
            for enc in entry.enclosures:
                if "image" in enc.get("type", "") and is_valid_image_url(enc.href):
                    return enc.href
    except:
        pass
    # Пробуем media_content
    try:
        if hasattr(entry, "media_content"):
            for media in entry.media_content:
                url = media.get("url")
                if is_valid_image_url(url):
                    return url
    except:
        pass
    # Пробуем Open Graph
    try:
        page = requests.get(
            entry.link,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        soup = BeautifulSoup(page.text, "html.parser")
        og = soup.find("meta", property="og:image")
        if og and is_valid_image_url(og.get("content")):
            return og.get("content")
    except:
        pass
    return None

# ==================== TEXT CLEANING ====================
def clean_text(text):
    """
    Удаляет мусорные строки, но сохраняет короткие значимые фрагменты
    (например, "Цена выросла на 2%" – 18 символов – полезно).
    """
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Пропускаем строки, состоящие только из цифр/знаков/однобуквенных слов
        if re.fullmatch(r'[\d\s\.,;:!?\-]+', line):
            continue
        # Пропускаем строки короче 15 символов, если в них нет цифр (цифры часто важны)
        if len(line) < 15 and not re.search(r'\d', line):
            continue
        # Удаляем упоминания источника
        if "ria.ru" in line.lower() or "прайм" in line.lower():
            continue
        lines.append(line)
    return "\n".join(lines)

# ==================== ARTICLE EXTRACTION ====================
def extract_article_text(url, fallback=""):
    """
    Извлекает основной текст статьи. Если fallback достаточно длинный (>300 символов),
    использует его, чтобы не скачивать страницу.
    """
    if fallback and len(fallback) > 300:
        logger.debug(f"Использую описание новости вместо полной статьи: {url[:60]}...")
        return fallback[:8000]

    try:
        response = scraper.get(url, timeout=20)
        doc = Document(response.text)
        soup = BeautifulSoup(doc.summary(), "html.parser")
        for tag in soup(["script", "style", "img", "figure"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        if len(text) > 200:
            return text[:8000]
    except Exception as e:
        logger.warning(f"Article parse error: {e}")
    return fallback[:8000]

# ==================== GEMINI (with retries) ====================
def extract_retry_delay(error_message: str) -> int:
    """
    Пытается извлечь число секунд из сообщения 'Please retry in Xs'.
    Возвращает 0, если не найдено.
    """
    match = re.search(r'retry in (\d+(?:\.\d+)?)\s*[sS]', error_message)
    if match:
        return int(float(match.group(1)))
    return 0

def is_retryable_exception(exception):
    """Определяет, можно ли повторить запрос при данной ошибке."""
    if isinstance(exception, APIError):
        # 429 – quota exceeded, 503 – temporary overload
        if hasattr(exception, 'code') and exception.code in (429, 503):
            return True
        # Некоторые ошибки могут быть без кода, проверяем сообщение
        msg = str(exception).lower()
        if "quota exceeded" in msg or "too many requests" in msg or "service unavailable" in msg:
            return True
    return False

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    retry=retry_if_exception_type(APIError),
    before_sleep=lambda retry_state: logger.warning(
        f"Gemini error, retry {retry_state.attempt_number}/3: {retry_state.outcome.exception()}"
    )
)
def ai_rewrite(text):
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
Ты финансовый редактор Telegram-канала Investing-24.

Полностью перепиши новость.

Требования:

- Новый стиль изложения
- Не копируй оригинальные предложения
- Сохрани факты и цифры
- Добавь подходящие эмодзи
- До 800 символов
- Без упоминания источника
- В конце добавь:

📢 Подписывайтесь: @Investing_24

Текст:

{text}
"""
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt
        )
        if response.text:
            return response.text.strip()
    except APIError as e:
        # Если ошибка 429 и есть рекомендация подождать – ждём
        if hasattr(e, 'code') and e.code == 429:
            delay = extract_retry_delay(str(e))
            if delay:
                logger.info(f"Gemini quota exceeded, waiting {delay} seconds before retry")
                time.sleep(delay)
        # Пробрасываем исключение дальше, чтобы tenacity повторил попытку
        raise
    except Exception as e:
        logger.error(f"Gemini unexpected error: {e}")
        return None
    return None

# ==================== TELEGRAM ====================
def send_post(text, image_url=None):
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    try:
        if image_url and is_valid_image_url(image_url):
            # Пытаемся отправить с фото
            result = requests.post(
                f"{base}/sendPhoto",
                data={
                    "chat_id": CHANNEL_ID,
                    "photo": image_url,
                    "caption": text[:1024]
                },
                timeout=30
            )
            if result.status_code == 400:
                # Если фото не принято (например, битая ссылка), отправим без фото
                logger.warning(f"Photo rejected, sending without image. URL: {image_url}")
                return send_post(text, image_url=None)
            result.raise_for_status()
        else:
            # Отправка только текста
            result = requests.post(
                f"{base}/sendMessage",
                data={
                    "chat_id": CHANNEL_ID,
                    "text": text,
                    "disable_web_page_preview": True
                },
                timeout=30
            )
            result.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

# ==================== MAIN ====================
def main():
    logger.info("=== START ===")

    posted = load_posted_guids()
    entries = get_all_entries()

    new_posts = 0
    gemini_calls = 0

    # Будем накапливать новые GUID, чтобы сохранить один раз в конце
    newly_posted_guids = set()

    for entry in entries:
        if new_posts >= MAX_ITEMS_PER_RUN:
            break

        guid = make_guid(entry)
        if guid in posted:
            continue

        title = entry.get("title", "")
        logger.info(f"Новость: {title}")

        description = entry.get("description", "")
        article = extract_article_text(entry.link, description)
        article = clean_text(article)
        if not article:
            article = title

        # Решаем, использовать ли Gemini
        use_gemini = gemini_calls < MAX_GEMINI_CALLS_PER_RUN
        rewritten = None

        if use_gemini:
            logger.info(f"Вызов Gemini для новости: {title[:50]}...")
            rewritten = ai_rewrite(article)
            gemini_calls += 1
            if rewritten:
                logger.info("Gemini успешно обработал новость")
            else:
                logger.warning("Gemini вернул пустой результат, использую заглушку")

        if not rewritten:
            # Заглушка, если Gemini не использовался или не сработал
            rewritten = f"📈 {title}\n\n📢 Подписывайтесь: @Investing_24"

        image = extract_image(entry)

        if send_post(rewritten, image):
            newly_posted_guids.add(guid)
            new_posts += 1
            logger.info(f"Опубликовано: {title}")
            time.sleep(3)  # небольшая пауза между постами

    # Сохраняем все новые GUID одной записью
    if newly_posted_guids:
        updated_guids = posted.union(newly_posted_guids)
        save_posted_guids(updated_guids)
        logger.info(f"Сохранено {len(newly_posted_guids)} новых GUID")

    logger.info(f"Готово. Добавлено {new_posts} постов. Вызовов Gemini: {gemini_calls}")

if __name__ == "__main__":
    main()
