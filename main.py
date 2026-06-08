import os
import json
import time
import logging
import feedparser
import requests
from bs4 import BeautifulSoup
from readability import Document
import google.generativeai as genai

# --- Config ---
RSS_URL = "https://ru.investing.com/rss/news.rss"
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]   # e.g. -1001234567890
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

DATA_FILE = "posted_guids.json"
MAX_ITEMS_PER_RUN = 5          # process up to 5 new posts each run
LOG_FILE = "bot.log"

# --- Logging ---
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger()

# --- Persistent storage (GUID list) ---
def load_posted_guids():
    if not os.path.exists(DATA_FILE):
        return set()
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # keep only the most recent 500 to avoid file bloat
    return set(data[-500:])

def save_posted_guids(guids):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(list(guids), f)

# --- 1. Parse RSS and extract image ---
def get_feed_entries():
    feed = feedparser.parse(RSS_URL)
    if feed.bozo:
        logger.error(f"RSS parse error: {feed.bozo_exception}")
    return feed.entries

def extract_image(entry):
    """Try to get the lead image from enclosure, media:content, or og:image."""
    # enclosure (most common in investing.com RSS)
    if hasattr(entry, "enclosures") and entry.enclosures:
        enc = entry.enclosures[0]
        if "image" in enc.get("type", ""):
            return enc.href

    # media:content (some feeds)
    if hasattr(entry, "media_content") and entry.media_content:
        for media in entry.media_content:
            if "image" in media.get("type", ""):
                return media.get("url")

    # fallback: fetch page and look for og:image
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

# --- 2. Extract full article text ---
def extract_article_text(url):
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)"
        })
        resp.raise_for_status()
        doc = Document(resp.text)
        # readability extracts main content, but we clean it further
        soup = BeautifulSoup(doc.summary(), "html.parser")
        # remove images, scripts, styles
        for tag in soup(["script", "style", "img", "figure", "figcaption"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # trim to ~4000 chars to keep Gemini happy
        return text[:4000]
    except Exception as e:
        logger.error(f"Article extraction failed for {url}: {e}")
        return None

# --- 3. Rewrite with Gemini (free tier) ---
def ai_rewrite(original_text, image_url=None):
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")   # free, fast

    prompt = (
        "Ты — редактор телеграм-канала об инвестициях и финансах.\n"
        "Перепиши новость в яркий и лаконичный пост для Telegram.\n"
        "Правила:\n"
        "- Используй эмодзи (🔹, 📈, 💡 и т.п.)\n"
        "- Сохрани ключевые цифры и факты\n"
        "- Максимум 250-300 слов\n"
        "- Не упоминай, что это пересказ\n"
        "- Заверши пост призывом подписаться на канал @Investing_24 (не более одной строки)\n\n"
        f"Исходная статья:\n{original_text}"
    )

    try:
        response = model.generate_content(prompt)
        if response.text:
            return response.text.strip()
        else:
            logger.error("Gemini returned empty response.")
            return None
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        return None

# --- 4. Send to Telegram ---
def send_telegram_post(text, image_url):
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    if image_url:
        # Telegram can accept direct image URLs
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
        logger.info("Post sent successfully.")
        return True
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return False

# --- Main workflow ---
def main():
    logger.info("=== Starting bot run ===")
    posted = load_posted_guids()
    entries = get_feed_entries()
    logger.info(f"Found {len(entries)} entries in RSS. Already posted: {len(posted)}")

    new_items = 0
    for entry in entries:
        guid = entry.get("id") or entry.link
        if guid in posted:
            continue
        if new_items >= MAX_ITEMS_PER_RUN:
            break

        logger.info(f"Processing new item: {entry.title}")

        image_url = extract_image(entry)
        full_text = extract_article_text(entry.link)
        if not full_text:
            logger.warning(f"Skipping {entry.link} – no text extracted.")
            posted.add(guid)   # mark as processed to avoid endless retries
            continue

        # AI rewrite
        edited = ai_rewrite(full_text, image_url)
        if not edited:
            edited = entry.title + "\n\n" + full_text[:500] + "..."  # fallback

        # Send
        success = send_telegram_post(edited, image_url)
        if success:
            posted.add(guid)
            new_items += 1
            time.sleep(2)   # gentle rate limit
        else:
            logger.error(f"Failed to send post for {entry.link}")

    if new_items > 0:
        save_posted_guids(posted)
        logger.info(f"Saved {new_items} new GUIDs.")
    else:
        logger.info("No new items to post.")

if __name__ == "__main__":
    main()
