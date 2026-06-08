import os, json, time, logging, sys
import feedparser
import requests
from bs4 import BeautifulSoup
from readability import Document
from google import genai

RSS_URL = "https://ru.investing.com/rss/news.rss"
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
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

def extract_article_text(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://ru.investing.com/",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            doc = Document(resp.text)
            soup = BeautifulSoup(doc.summary(), "html.parser")
            for tag in soup(["script", "style", "img", "figure", "figcaption"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            return text[:4000]
        except requests.exceptions.HTTPError as e:
            logger.warning(f"Attempt {attempt+1} failed for {url}: {e}")
            time.sleep(2)
        except Exception as e:
            logger.error(f"Article extraction error on {url}: {e}")
            break
    return None

def ai_rewrite(original_text, image_url=None):
    client = genai.Client(api_key=GEMINI_API_KEY)
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
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
        )
        if response.text:
            return response.text.strip()
        else:
            logger.error("Gemini returned empty response.")
            return None
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
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
        logger.info("Post sent successfully.")
        return True
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return False

def main():
    logger.info("=== Starting bot run ===")
    posted = load_posted_guids()
    entries = get_feed_entries()
    if not entries:
        logger.warning("No entries found in feed. Exiting.")
        return

    logger.info(f"Found {len(entries)} items total, {len(posted)} already posted.")
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
            posted.add(guid)
            continue

        edited = ai_rewrite(full_text, image_url)
        if not edited:
            edited = entry.title + "\n\n" + full_text[:500] + "..."

        success = send_telegram_post(edited, image_url)
        if success:
            posted.add(guid)
            new_items += 1
            time.sleep(2)
        else:
            logger.error(f"Failed to send post for {entry.link}")

    if new_items > 0:
        save_posted_guids(posted)
        logger.info(f"Saved {new_items} new GUIDs.")
    else:
        logger.info("No new items to post.")

if __name__ == "__main__":
    main()
