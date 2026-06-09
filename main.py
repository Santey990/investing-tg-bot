import os
import time
import json
import logging
import random
import feedparser
import requests
from datetime import datetime
from google import genai

# =========================
# CONFIG
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)

RSS_FEEDS = [
    "https://ru.investing.com/rss/news.rss",
    "https://1prime.ru/export/rss2/index.xml",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://feeds.reuters.com/reuters/businessNews",
]

POSTED_FILE = "posted_guids.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


# =========================
# STORAGE
# =========================

def load_posted():
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_posted(data):
    with open(POSTED_FILE, "w") as f:
        json.dump(list(data), f)


# =========================
# RSS
# =========================

def fetch_news():
    items = []

    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            guid = getattr(entry, "id", None) or getattr(entry, "link", None)
            if not guid:
                continue

            items.append({
                "guid": guid,
                "title": entry.title,
                "link": getattr(entry, "link", ""),
                "source": url
            })

    return items


# =========================
# AI (with fallback + retry)
# =========================

def generate_text(title):
    prompt = f"""
Ты финансовый редактор premium newsroom.

Перепиши новость профессионально, кратко и понятно на русском.

Заголовок:
{title}

Требования:
- 1–2 предложения
- без воды
- без повторов
- финансовый стиль
"""

    last_error = None

    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )

            text = (response.text or "").strip()

            if len(text) < 10:
                raise ValueError("Empty AI response")

            return text

        except Exception as e:
            last_error = e
            logging.warning(f"Gemini attempt {attempt+1} failed: {e}")
            time.sleep(2 + attempt * 2)

    # fallback если AI умер
    logging.error(f"Gemini failed permanently: {last_error}")
    return f"Краткая сводка: {title}"


# =========================
# TELEGRAM
# =========================

def send_post(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    r = requests.post(url, json=payload, timeout=15)
    if not r.ok:
        logging.error(f"Telegram error: {r.text}")


# =========================
# FORMAT POST
# =========================

def format_post(item, text):
    tag = "#markets"

    return (
        f"[{datetime.now().strftime('%d.%m.%Y %H:%M')}] Investing-24:\n"
        f"{text}\n\n"
        f"{tag}"
    )


# =========================
# MAIN
# =========================

def main():
    posted = load_posted()
    news = fetch_news()

    random.shuffle(news)

    sent = 0

    for item in news:
        if item["guid"] in posted:
            continue

        logging.info(f"Processing: {item['title']}")

        text = generate_text(item["title"])

        # защита от пустоты
        if not text or len(text.strip()) < 5:
            text = f"Финансовая новость: {item['title']}"

        post = format_post(item, text)

        send_post(post)

        posted.add(item["guid"])
        sent += 1

        time.sleep(3)

        if sent >= 10:
            break

    save_posted(posted)
    logging.info(f"Done. posted={sent}")


if __name__ == "__main__":
    main()
