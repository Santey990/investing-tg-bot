import os
import re
import logging
import feedparser
import requests
from datetime import datetime
from google import genai

# ======================
# CONFIG
# ======================
RSS_FEEDS = [
    "https://ru.investing.com/rss/news.rss",
    "https://1prime.ru/export/rss2/index.xml",
]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)

logging.basicConfig(level=logging.INFO)

# ======================
# CLEANING
# ======================
def clean_text(text: str) -> str:
    if not text:
        return ""

    text = text.strip()

    # убрать мусор Gemini
    text = re.sub(r"\*\*Заголовок:\*\*", "", text)
    text = re.sub(r"\*\*Новость:\*\*", "", text)
    text = re.sub(r"Вот (несколько|несколько вариантов|варианты).*", "", text, flags=re.S)
    text = re.sub(r"Вариант \d+.*", "", text, flags=re.S)

    # убрать timestamp мусор
    text = re.sub(r"\[\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}\]\s*Investing-24:?", "", text)

    # убрать хэштеги блока
    text = re.sub(r"#markets.*", "", text)

    # убрать повторяющиеся пустые строки
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l]

    return "\n\n".join(lines).strip()


def is_valid(text: str) -> bool:
    if not text:
        return False
    if len(text) < 40:
        return False
    if "http" in text and len(text) < 60:
        return False
    return True


# ======================
# GEMINI REWRITE
# ======================
def rewrite_news(title: str, summary: str) -> str:
    prompt = f"""
You are a financial newsroom editor.

Rewrite this news into clean Investing-style format in Russian.

RULES:
- ONLY final text
- NO "Заголовок:"
- NO "Новость:"
- NO variants
- NO explanations
- 2-5 sentences max
- neutral financial tone

TITLE: {title}
TEXT: {summary}
"""

    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return resp.text or ""
    except Exception as e:
        logging.error(f"Gemini error: {e}")
        return ""


# ======================
# RSS
# ======================
def fetch_news():
    items = []

    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for e in feed.entries[:10]:
            title = getattr(e, "title", "")
            summary = getattr(e, "summary", "")

            if title:
                items.append({
                    "title": title,
                    "summary": summary
                })

    return items


# ======================
# TELEGRAM
# ======================
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text,
        "parse_mode": "Markdown"
    }

    requests.post(url, json=payload, timeout=20)


# ======================
# MAIN
# ======================
def main():
    news = fetch_news()
    posted = 0

    for item in news:
        title = item["title"]
        summary = item["summary"]

        logging.info(f"Processing: {title}")

        rewritten = rewrite_news(title, summary)
        cleaned = clean_text(rewritten)

        if not is_valid(cleaned):
            logging.warning("Skipped invalid post")
            continue

        # финальный формат поста
        final_post = f"{cleaned}\n\n#markets"

        send_telegram(final_post)
        posted += 1

    logging.info(f"Done. posted={posted}")


if __name__ == "__main__":
    main()
