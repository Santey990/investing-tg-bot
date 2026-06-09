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

RSS_URLS = [
    "https://1prime.ru/export/rss2/index.xml"
]

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

DATA_FILE = "posted_guids.json"
MAX_ITEMS_PER_RUN = 3
LOG_FILE = "bot.log"

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


# ---------------- GUID ----------------

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


# ---------------- RSS ----------------

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

        logger.info(
            f"Получено {len(feed.entries)} записей из {url}"
        )

        for item in feed.entries:
            entries.append(item)

    return entries


# ---------------- IMAGE ----------------

def extract_image(entry):
    try:
        if hasattr(entry, "enclosures"):
            for enc in entry.enclosures:
                if "image" in enc.get("type", ""):
                    return enc.href
    except:
        pass

    try:
        if hasattr(entry, "media_content"):
            for media in entry.media_content:
                return media.get("url")
    except:
        pass

    try:
        page = requests.get(
            entry.link,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"}
        )

        soup = BeautifulSoup(page.text, "html.parser")

        og = soup.find("meta", property="og:image")

        if og:
            return og.get("content")
    except:
        pass

    return None


# ---------------- CLEAN ----------------

def clean_text(text):
    lines = []

    for line in text.splitlines():

        line = line.strip()

        if not line:
            continue

        if len(line) < 20:
            continue

        if "ria.ru" in line.lower():
            continue

        if "прайм" in line.lower():
            continue

        lines.append(line)

    return "\n".join(lines)


# ---------------- ARTICLE ----------------

def extract_article_text(url, fallback=""):
    try:
        response = scraper.get(url, timeout=20)

        doc = Document(response.text)

        soup = BeautifulSoup(
            doc.summary(),
            "html.parser"
        )

        for tag in soup(
            ["script", "style", "img", "figure"]
        ):
            tag.decompose()

        text = soup.get_text(
            separator="\n",
            strip=True
        )

        if len(text) > 200:
            return text[:8000]

    except Exception as e:
        logger.warning(f"Article parse error: {e}")

    return fallback[:8000]


# ---------------- GEMINI ----------------

def ai_rewrite(text):
    try:
        client = genai.Client(
            api_key=GEMINI_API_KEY
        )

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
            model="gemini-2.0-flash-lite",
            contents=prompt
        )

        if response.text:
            return response.text.strip()

    except Exception as e:
        logger.error(f"Gemini error: {e}")

    return None


# ---------------- TELEGRAM ----------------

def send_post(text, image_url=None):

    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

    try:

        if image_url:

            result = requests.post(
                f"{base}/sendPhoto",
                data={
                    "chat_id": CHANNEL_ID,
                    "photo": image_url,
                    "caption": text[:1024]
                },
                timeout=30
            )

        else:

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


# ---------------- MAIN ----------------

def main():

    logger.info("=== START ===")

    posted = load_posted_guids()

    entries = get_all_entries()

    new_posts = 0

    for entry in entries:

        guid = make_guid(entry)

        if guid in posted:
            continue

        if new_posts >= MAX_ITEMS_PER_RUN:
            break

        title = entry.get("title", "")

        logger.info(f"Новость: {title}")

        description = entry.get(
            "description",
            ""
        )

        article = extract_article_text(
            entry.link,
            description
        )

        article = clean_text(article)

        if not article:
            article = title

        rewritten = ai_rewrite(article)

        if not rewritten:
            rewritten = f"📈 {title}\n\n📢 Подписывайтесь: @Investing_24"

        image = extract_image(entry)

        if send_post(rewritten, image):

            posted.add(guid)

            save_posted_guids(posted)

            new_posts += 1

            logger.info(
                f"Опубликовано: {title}"
            )

            time.sleep(3)

    logger.info(
        f"Готово. Добавлено {new_posts} постов."
    )


if __name__ == "__main__":
    main()
