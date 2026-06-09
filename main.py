import os, json, time, logging, sys, re, hashlib
import feedparser
import requests
import cloudscraper
from bs4 import BeautifulSoup
from readability import Document
from google import genai
from google.genai import types

RSS_URLS = [
    "https://1prime.ru/export/rss2/index.xml",
]

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
        logger.warning("posted_guids.json пуст, начинаю с чистого списка.")
        return set()
    try:
        data = json.loads(raw)
        return set(data[-1000:])
    except json.JSONDecodeError:
        logger.error("Ошибка в JSON, сбрасываю список.")
        return set()

def save_posted_guids(guids):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(list(guids), f)

def make_guid(entry):
    if hasattr(entry, "id") and entry.id:
        return entry.id
    raw = entry.link + entry.get("title", "")
    return hashlib.md5(raw.encode()).hexdigest()

# ---------- RSS ----------
def fetch_rss(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.error(f"Не удалось скачать RSS {url}: {e}")
        return None

def get_all_entries():
    all_entries = []
    for url in RSS_URLS:
        content = fetch_rss(url)
        if content is None:
            continue
        feed = feedparser.parse(content)
        if feed.bozo:
            logger.error(f"Ошибка парсинга RSS {url}: {feed.bozo_exception}")
        logger.info(f"Лента {url} содержит {len(feed.entries)} новостей.")
        for entry in feed.entries:
            all_entries.append((entry, url))
    return all_entries

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
        logger.warning(f"Не удалось извлечь изображение: {e}")
    return None

# ---------- мусорная строка ----------
def is_garbage_line(line):
    line = line.strip()
    if not line:
        return True
    if re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', line):
        return True
    if re.search(r'(\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{2,4}[-.\s]?\d{2,4}', line):
        return True
    if re.search(r'ФГУП|МИА|Россия сегодня|internet-group|РИА Новости|ПРАЙМ', line, re.IGNORECASE):
        return True
    if re.search(r'\d{1,2}\s+\w+|\d{4}', line) and re.search(r'МОСКВА|ПРАЙМ', line, re.IGNORECASE):
        return True
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{4}', line):
        return True
    if re.fullmatch(r'\d{2}\.\d{2}\.\d{4}', line):
        return True
    if re.fullmatch(r'20\d{2}', line):
        return True
    if re.fullmatch(r'\d{4}', line):
        return True
    if re.match(r'https?://\S+', line):
        return True
    if line.startswith('—') or line.startswith('- '):
        return True
    if line and line[0] in ',.;:!?':
        return True
    parts = [p.strip() for p in line.split(',') if p.strip()]
    if len(parts) >= 2 and all(len(w) < 25 for w in parts):
        if not any(w.endswith(('ть', 'чь', 'лся', 'ется', 'ются', 'ете', 'ают', 'ил', 'ел', 'ет', 'ит', 'ут', 'ют')) for w in parts):
            return True
    words = line.split()
    if 1 <= len(words) <= 3 and all(len(w) < 20 for w in words):
        if not any(w[0].isdigit() for w in words):
            if not any(w.endswith(('ть', 'чь', 'лся', 'ется', 'ются', 'ете', 'ают', 'ил', 'ел', 'ет', 'ит', 'ут', 'ют')) for w in words):
                return True
    return False

def clean_text(raw_text, title=""):
    lines = raw_text.splitlines()
    cleaned = []
    prev_ended = True
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if is_garbage_line(line):
            continue
        if line and line[0].islower() and prev_ended:
            continue
        if title and line.lower() == title.lower():
            continue
        cleaned.append(line)
        prev_ended = line.endswith(('.', '!', '?'))
    return "\n".join(cleaned)

def has_verb(line):
    return any(w.endswith(('ет', 'ит', 'ут', 'ют', 'ал', 'ил', 'ел', 'ть', 'чь', 'ся', 'сь', 'ете', 'ают', 'яют', 'ует', 'ирует')) for w in line.split())

def filter_body_lines(text):
    lines = text.splitlines()
    result = []
    for line in lines:
        line = line.strip()
        if not line or is_garbage_line(line):
            continue
        if len(line) > 60 or has_verb(line) or line.startswith('"') or line.startswith('«'):
            result.append(line)
    return "\n".join(result)

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
            logger.info("Полный текст получен.")
            return text[:9000]
    except Exception as e:
        logger.warning(f"Ошибка cloudscraper: {e}")
    if fallback_description and len(fallback_description.strip()) > 30:
        logger.info("Использую описание из RSS.")
        return fallback_description.strip()[:9000]
    return None

def add_emoji_prefix(text):
    lower = text.lower()
    if any(w in lower for w in ['акци', 'биржа', 'индекс', 'торг', 's&p', 'nasdaq', 'инвест']):
        return "📈 " + text
    if any(w in lower for w in ['нефть', 'газ', 'топлив', 'энерг']):
        return "🛢️ " + text
    if any(w in lower for w in ['золот', 'серебр', 'драгметалл']):
        return "💰 " + text
    if any(w in lower for w in ['банк', 'кредит', 'финанс', 'втб', 'сбер']):
        return "🏦 " + text
    if any(w in lower for w in ['доллар', 'валюта', 'рубл']):
        return "💵 " + text
    return "🔹 " + text

# ---------- ИИ-рерайт с максимальной уникальностью ----------
def ai_rewrite(original_text, image_url=None):
    """Переписывает через Gemini, требуя 100% уникальности."""
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)

        # Усиленный промпт
        prompt = (
            "Ты — редактор телеграм-канала. Полностью переделай эту новость так, "
            "чтобы она отличалась от оригинала на 100% по стилю, лексике и построению предложений. "
            "Используй совершенно другие формулировки, синонимы, измени порядок подачи фактов. "
            "Ни одна фраза из исходного текста не должна повторяться дословно. "
            "Сохрани только точные цифры и факты. "
            "Добавь эмодзи, сделай пост ярким и лаконичным (до 800 символов). "
            "Не упоминай источник и дату. "
            "Закончи призывом подписаться на канал @Investing_24.\n\n"
            f"Исходная статья:\n{original_text}"
        )

        # Высокая креативность
        generation_config = types.GenerateContentConfig(
            temperature=0.9,
            top_p=0.95,
            max_output_tokens=800,
        )

        response = client.models.generate_content(
            model="models/gemini-1.5-flash",
            contents=prompt,
            config=generation_config,
        )

        if response.text:
            logger.info("ИИ успешно переписал текст.")
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
        payload = {"chat_id": CHANNEL_ID, "photo": image_url, "caption": text, "parse_mode": "HTML"}
        method = "sendPhoto"
    else:
        payload = {"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        method = "sendMessage"
    try:
        resp = requests.post(f"{base}/{method}", data=payload, timeout=20)
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            logger.error(f"Telegram API ошибка: {result}")
            return False
        logger.info("Пост отправлен.")
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        return False

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
    all_entries = get_all_entries()
    if not all_entries:
        logger.warning("Новостей нет. Выход.")
        return

    logger.info(f"Всего записей из {len(RSS_URLS)} лент: {len(all_entries)}, уже обработано: {len(posted)}.")
    new_items = 0

    for entry, rss_url in all_entries:
        guid = make_guid(entry)
        if guid in posted:
            continue
        if new_items >= MAX_ITEMS_PER_RUN:
            break

        original_title = entry.get("title", "")
        logger.info(f"Обрабатываю ({rss_url}): {original_title}")

        image_url = extract_image(entry)
        description = entry.get("description", "")
        raw_text = extract_article_text(entry.link, fallback_description=description)
        if not raw_text:
            raw_text = original_title

        cleaned_text = clean_text(raw_text, title=original_title)
        if not cleaned_text:
            cleaned_text = original_title

        title = original_title
        if is_garbage_line(title):
            lines = cleaned_text.splitlines()
            new_title = ""
            for i, line in enumerate(lines):
                if line and not is_garbage_line(line) and line[0].isupper():
                    new_title = line
                    lines.pop(i)
                    cleaned_text = "\n".join(lines) if lines else ""
                    break
            if new_title:
                title = new_title

        cleaned_lines = cleaned_text.splitlines()
        filtered = []
        for line in cleaned_lines:
            if title and line.lower().startswith(title.lower()):
                continue
            if original_title and line.lower().startswith(original_title.lower()):
                continue
            filtered.append(line)
        cleaned_text = "\n".join(filtered)

        rewritten = ai_rewrite(cleaned_text, image_url)

        if rewritten:
            post = rewritten
        else:
            body = filter_body_lines(cleaned_text)
            body = trim_text(body, 800) if body else ""
            if not body and cleaned_text:
                for line in cleaned_text.splitlines():
                    if line and not is_garbage_line(line):
                        body = line
                        break
                if body:
                    body = trim_text(body, 800)
            post = add_emoji_prefix(title)
            if body:
                post += "\n\n" + body
            if "@Investing_24" not in post:
                post += "\n\nПодпишись на канал @Investing_24"

        success = send_telegram_post(post, image_url)
        if success:
            posted.add(guid)
            new_items += 1
            save_posted_guids(posted)
            logger.info(f"GUID {guid} сохранён. Всего обработано: {len(posted)}.")
            time.sleep(2)
        else:
            logger.error(f"Не удалось отправить пост для {entry.link}")

    if new_items == 0:
        logger.info("Новых постов нет.")
    else:
        logger.info(f"Добавлено {new_items} постов.")

if __name__ == "__main__":
    main()
