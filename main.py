import os, json, time, logging, sys, hashlib
import feedparser
import requests
import cloudscraper
from bs4 import BeautifulSoup
from readability import Document
from google import genai

# ---------------- CONFIG ----------------

RSS_URLS = [
    "https://1prime.ru/export/rss2/index.xml",
    "https://ru.investing.com/rss/news.rss",
    "https://feeds.reuters.com/reuters/businessNews",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html"
]

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

DATA_FILE = "posted_guids.json"
MAX_ITEMS_PER_RUN = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger()

scraper = cloudscraper.create_scraper()

# ---------------- STORAGE ----------------

def load_posted_guids():
    if not os.path.exists(DATA_FILE):
        return set()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except:
        return set()

def save_posted_guids(guids):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(list(guids), f)

def make_guid(entry):
    raw = (entry.get("id") or entry.get("link") or "") + entry.get("title", "")
    return hashlib.md5(raw.encode()).hexdigest()

# ---------------- RSS ----------------

def fetch_rss(url):
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.content
    except:
        return None

def get_all_entries():
    all_entries = []
    for url in RSS_URLS:
        content = fetch_rss(url)
        if not content:
            continue
        feed = feedparser.parse(content)
        for e in feed.entries:
            all_entries.append((e, url))
    return all_entries

# ---------------- IMAGE ENGINE (NO API) ----------------

def extract_image(entry, url=None, title=None):

    # 1. RSS media
    try:
        if hasattr(entry, "media_content"):
            for m in entry.media_content:
                if m.get("url"):
                    return m["url"]
    except:
        pass

    # 2. RSS enclosures
    try:
        if hasattr(entry, "enclosures"):
            for e in entry.enclosures:
                if "image" in e.get("type", ""):
                    return e.get("href")
    except:
        pass

    # 3. OpenGraph / Twitter / HTML parsing
    try:
        r = requests.get(
            url or entry.link,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )

        soup = BeautifulSoup(r.text, "html.parser")

        # og:image
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return og["content"]

        # twitter:image
        tw = soup.find("meta", attrs={"name": "twitter:image"})
        if tw and tw.get("content"):
            return tw["content"]

        # first article image fallback
        img = soup.find("img")
        if img and img.get("src"):
            return img["src"]

    except:
        pass

    # 4. SMART fallback (NO API)
    q = (title or entry.get("title", "")).lower()

    if any(w in q for w in ["oil", "gas", "energy", "нефть", "газ"]):
        return "https://images.unsplash.com/photo-1611273426858-4500b6f7f8c4"

    if any(w in q for w in ["stock", "market", "index", "бирж", "акци"]):
        return "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3"

    if any(w in q for w in ["crypto", "bitcoin", "btc"]):
        return "https://images.unsplash.com/photo-1621761191319-c6fb62004040"

    if any(w in q for w in ["bank", "finance", "credit"]):
        return "https://images.unsplash.com/photo-1601597111158-2fceff292cdc"

    # final fallback (always valid)
    return "https://images.unsplash.com/photo-1526304640581-d334cdbbf45e"

# ---------------- ARTICLE TEXT ----------------

def extract_article_text(url):
    try:
        r = scraper.get(url, timeout=10)
        doc = Document(r.text)
        soup = BeautifulSoup(doc.summary(), "html.parser")
        return soup.get_text("\n", strip=True)[:8000]
    except:
        return None

# ---------------- AI REWRITE ----------------

def ai_rewrite(text):
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)

        prompt = f"""
Ты редактор финансового Telegram-канала.
Перепиши новость полностью (100% уникально).
Сохрани факты и цифры.
Добавь эмодзи.
До 800 символов.
В конце: подписка @Investing_24

Текст:
{text}
"""

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )

        return response.text.strip()

    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return None

# ---------------- TELEGRAM ----------------

def send_post(text, image_url):
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

    if image_url:
        method = "sendPhoto"
        payload = {
            "chat_id": CHANNEL_ID,
            "photo": image_url,
            "caption": text[:1024]
        }
    else:
        method = "sendMessage"
        payload = {
            "chat_id": CHANNEL_ID,
            "text": text
        }

    try:
        r = requests.post(f"{base}/{method}", data=payload, timeout=15)
        return r.json().get("ok", False)
    except:
        return False

# ---------------- MAIN ----------------

def main():
    posted = load_posted_guids()
    entries = get_all_entries()

    count = 0

    for entry, rss in entries:
        if count >= MAX_ITEMS_PER_RUN:
            break

        guid = make_guid(entry)
        if guid in posted:
            continue

        title = entry.get("title", "")
        link = entry.get("link", "")

        logger.info(f"Processing: {title}")

        image_url = extract_image(entry, link, title)
        raw_text = extract_article_text(link) or title

        rewritten = ai_rewrite(raw_text)

        final_post = rewritten or title

        if send_post(final_post, image_url):
            posted.add(guid)
            save_posted_guids(posted)
            count += 1
            time.sleep(2)

    logger.info(f"Done. posted={count}")

# ---------------- RUN ----------------

if __name__ == "__main__":
    main()
