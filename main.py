import os
import json
import time
import logging
import sys
import hashlib
import feedparser
import requests
from google import genai
from google.genai import types

# ================= RSS SOURCES =================

RSS_URLS = [
    # 🇷🇺 Russian sources
    "https://1prime.ru/export/rss2/index.xml",
    "https://ru.investing.com/rss/news.rss",

    # 🌍 Global markets
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/reuters/marketsNews",
    "https://feeds.bbci.co.uk/news/business/rss.xml",

    # 📊 Investing feeds (markets/forex/economy)
    "https://www.investing.com/rss/news_25.rss",
    "https://www.investing.com/rss/news_288.rss",
    "https://www.investing.com/rss/news_11.rss",

    # 💡 Macro alternative feed
    "https://www.ft.com/?format=rss",
]

# ================= ENV =================

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

DATA_FILE = "posted.json"
MAX_ITEMS_PER_RUN = 5

# ================= LOGGING =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger()

# ================= STORAGE =================

def load_posted():
    if not os.path.exists(DATA_FILE):
        return set()
    try:
        return set(json.load(open(DATA_FILE, "r")))
    except:
        return set()

def save_posted(data):
    with open(DATA_FILE, "w") as f:
        json.dump(list(data), f)

def make_id(entry):
    raw = entry.get("link", "") + entry.get("title", "")
    return hashlib.md5(raw.encode()).hexdigest()

# ================= RSS =================

def fetch_rss(url):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/rss+xml,text/xml,*/*"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        return feedparser.parse(resp.content).entries
    except Exception as e:
        logger.error(f"RSS error {url}: {e}")
        return []

def get_all_entries():
    all_items = []

    for url in RSS_URLS:
        entries = fetch_rss(url)
        logger.info(f"{url} -> {len(entries)} items")

        for e in entries:
            all_items.append((e, url))

    return all_items

# ================= FILTER =================

def is_bad(entry):
    title = entry.get("title", "")
    if len(title) < 20:
        return True
    return False

# ================= AI REWRITE =================

def ai_rewrite(title, text):
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)

        prompt = f"""
Ты редактор финансового новостного агентства уровня Bloomberg.

СДЕЛАЙ:
1. Заголовок (сильный, короткий)
2. Лид (1-2 предложения)
3. Основной текст (2–4 предложения, переработай полностью)
4. Рыночный контекст (если есть влияние)
5. 2-3 тега (#markets #stocks #crypto)

ПРАВИЛА:
- не копируй текст
- сохраняй факты
- русский язык
- до 900 символов

ЗАГОЛОВОК:
{title}

ТЕКСТ:
{text}
"""

        resp = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.6,
                max_output_tokens=900
            )
        )

        return resp.text.strip() if resp.text else None

    except Exception as e:
        logger.error(f"AI error: {e}")
        return None

# ================= TELEGRAM =================

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        r = requests.post(url, data=payload, timeout=20)
        return r.json().get("ok", False)
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

# ================= MAIN =================

def main():
    posted = load_posted()
    entries = get_all_entries()

    count = 0

    for entry, src in entries:
        if count >= MAX_ITEMS_PER_RUN:
            break

        if is_bad(entry):
            continue

        uid = make_id(entry)
        if uid in posted:
            continue

        title = entry.get("title", "")
        summary = entry.get("summary", "") or entry.get("description", "")

        logger.info(f"Processing: {title}")

        post = ai_rewrite(title, summary)

        if not post:
            post = f"{title}\n\n{summary[:700]}\n\n#markets"

        if send_telegram(post):
            posted.add(uid)
            save_posted(posted)
            count += 1
            logger.info("Posted OK")
            time.sleep(2)

    logger.info(f"Done. New posts: {count}")

if __name__ == "__main__":
    main()
