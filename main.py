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

# ==================== CONFIG ====================
RSS_URLS = [
    "https://life.ru/rss",
    "https://www.starhit.ru/rss/",
    "https://www.thesun.co.uk/feed/",
    "https://www.dailymail.co.uk/articles.rss",
]

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]

OPENROUTER_KEYS = []
for key_name in ["OPENROUTER_API_KEY", "OPENROUTER_API_KEY_2"]:
    key = os.environ.get(key_name)
    if key:
        OPENROUTER_KEYS.append(key)

DATA_FILE = "posted_guids.json"
MAX_ITEMS_PER_RUN = 2
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
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
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
    return extract_image_from_url(entry.link)

def extract_image_from_url(url):
    try:
        page = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(page.text, "html.parser")
        og = soup.find("meta", property="og:image")
        if og:
            return og.get("content")
    except:
        pass
    return None

# ==================== CLEAN ====================
def clean_text(text):
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', line):
            continue
        if re.search(r'(\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{2,4}[-.\s]?\d{2,4}', line):
            continue
        if re.search(r'ФГУП|МИА|Россия сегодня|internet-group|РИА|ПРАЙМ', line, re.IGNORECASE):
            continue
        if re.search(r'\d{1,2}\s+\w+|\d{4}', line) and re.search(r'МОСКВА|ПРАЙМ', line, re.IGNORECASE):
            continue
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{4}', line):
            continue
        if re.fullmatch(r'\d{2}\.\d{2}\.\d{4}', line):
            continue
        if re.match(r'https?://\S+', line):
            continue
        parts = [p.strip() for p in line.split(',') if p.strip()]
        if len(parts) >= 2 and all(len(w) < 25 for w in parts):
            if not any(w.endswith(('ть', 'чь', 'лся', 'ется', 'ются', 'ете', 'ают', 'ил', 'ел', 'ет', 'ит', 'ут', 'ют')) for w in parts):
                continue
        words = line.split()
        if 1 <= len(words) <= 3 and all(len(w) < 20 for w in words):
            if not any(w.endswith(('ть', 'чь', 'лся', 'ется', 'ются', 'ете', 'ают', 'ил', 'ел', 'ет', 'ит', 'ут', 'ют')) for w in words):
                continue
        lines.append(line)
    return "\n".join(lines)

# ==================== ARTICLE ====================
def extract_article_text(url, fallback=""):
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
    return fallback[:8000] if fallback else ""

# ==================== EMOJI ====================
def add_emoji_prefix(text):
    lower = text.lower()
    if any(w in lower for w in ['скандал', 'сенсаци', 'развод', 'взрыв']):
        return "💥 " + text
    if any(w in lower for w in ['звезд', 'певец', 'певица', 'актёр', 'актриса', 'шоу', 'селеб']):
        return "🌟 " + text
    if any(w in lower for w in ['смерть', 'трагеди', 'убийств', 'катастроф', 'криминал']):
        return "💀 " + text
    if any(w in lower for w in ['любовь', 'роман', 'свадьб', 'развод', 'измен']):
        return "💔 " + text
    if any(w in lower for w in ['деньг', 'миллион', 'миллиард', 'состояни', 'богат']):
        return "💰 " + text
    return "🔥 " + text

# ==================== AI через OpenRouter ====================
OPENROUTER_MODELS = [
    "google/gemma-4-31b-it:free",
    "google/gemma-2-9b-it:free",
    "mistralai/mistral-7b-instruct-v0.2:free",
    "huggingfaceh4/zephyr-7b-beta:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "openrouter/free",
]

def truncate_to_last_sentence(text, max_len=900):
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    for sep in ['.', '!', '?']:
        pos = cut.rfind(sep)
        if pos > 400:
            return cut[:pos+1]
    last_space = cut.rfind(' ')
    return cut[:last_space] + "…" if last_space > 0 else cut + "…"

def log_rate_limit(response, key_idx):
    remaining = response.headers.get("X-RateLimit-Remaining")
    limit = response.headers.get("X-RateLimit-Limit")
    if remaining is not None and limit is not None:
        logger.info(f"🔑 Ключ {key_idx}: осталось {remaining}/{limit} запросов")
    else:
        logger.info(f"🔑 Ключ {key_idx}: заголовки лимита не получены")

def ai_rewrite(text):
    if not OPENROUTER_KEYS:
        logger.warning("Нет ни одного ключа OpenRouter")
        return None

    # ⚠️ ВАЖНО: убрано слово "шок" из лексики модели
    prompt = f"""Перепиши новость для телеграм-канала «Хайпожор».
Стиль: кликбейтный, эмоциональный, с восклицаниями и интригой.
Язык: только русский.
Формат: ТОЛЬКО готовый пост (до 800 символов), без предисловий.
Включи минимум 3 эмодзи (😱, 🔥, 💔, ⚡ и т.д.).
В конце обязательно: 📢 Подписывайтесь: @Hype_Zhor
Завершай текст полностью, не обрывай на полуслове.
Ни в коем случае не используй слово "шок" и его производные.

Новость:
{text}"""

    for key_idx, api_key in enumerate(OPENROUTER_KEYS, 1):
        logger.info(f"🔑 Пробую ключ {key_idx}/{len(OPENROUTER_KEYS)}")

        for model_name in OPENROUTER_MODELS:
            try:
                response = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 1000,
                        "temperature": 0.8,
                    },
                    timeout=60,
                )

                log_rate_limit(response, key_idx)

                if response.status_code == 200:
                    data = response.json()
                    content = data["choices"][0]["message"]["content"].strip()

                    if any(phrase in content.lower() for phrase in [
                        "перепиши новость", "кликбейтный", "только русский",
                        "we need to rewrite", "rewrite the news", "must be in russian"
                    ]):
                        logger.warning(f"Модель {model_name} вернула промпт вместо пересказа")
                        continue

                    if content:
                        content = truncate_to_last_sentence(content)
                        logger.info(f"✅ Успешно использована модель: {model_name} (ключ {key_idx})")
                        return content
                    else:
                        logger.warning(f"⚠️ Модель {model_name} вернула пустой текст")
                elif response.status_code == 429:
                    logger.warning(f"⏳ Модель {model_name} превысила лимит (429) на ключе {key_idx}")
                elif response.status_code == 404:
                    logger.warning(f"❌ Модель {model_name} не найдена (404)")
                else:
                    logger.error(f"❌ Ошибка {response.status_code} для модели {model_name}")
            except Exception as e:
                logger.error(f"❌ Исключение для модели {model_name}: {e}")

    logger.error("❌ Все ключи и модели исчерпаны.")
    return None

# ==================== TELEGRAM ====================
def send_photo_as_file(image_url, caption):
    for attempt in range(2):
        try:
            response = requests.get(image_url, timeout=30, stream=True)
            response.raise_for_status()
            content_type = response.headers.get('content-type', '')
            ext = 'jpg'
            if 'png' in content_type:
                ext = 'png'
            elif 'gif' in content_type:
                ext = 'gif'
            elif 'webp' in content_type:
                ext = 'webp'
            files = {'photo': (f'image.{ext}', response.content, content_type)}
            data = {'chat_id': CHANNEL_ID, 'caption': caption[:1024]}
            result = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                files=files,
                data=data,
                timeout=45
            )
            result.raise_for_status()
            return True
        except Exception as e:
            if attempt == 0:
                logger.warning(f"Попытка {attempt+1} загрузки фото не удалась: {e}. Повтор через 2 сек.")
                time.sleep(2)
            else:
                logger.error(f"Ошибка при отправке фото после 2 попыток: {e}")
    return False

def send_post(text, image_url=None):
    if image_url and isinstance(image_url, str) and image_url.startswith(('http://', 'https://')):
        if send_photo_as_file(image_url, text):
            return True
        logger.warning("Не удалось отправить фото, пробую только текст")
    try:
        result = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": CHANNEL_ID, "text": text, "disable_web_page_preview": True},
            timeout=30
        )
        result.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Не удалось отправить даже текст: {e}")
        return False

# ==================== MAIN ====================
def main():
    logger.info("=== START ===")
    posted = load_posted_guids()
    newly_posted_guids = set()
    total_published = 0

    for entry in get_all_entries():
        if total_published >= MAX_ITEMS_PER_RUN:
            break
        guid = make_guid(entry)
        if guid in posted:
            continue

        title = entry.get("title", "")
        logger.info(f"Новость: {title}")

        article = extract_article_text(entry.link, entry.get("description", ""))
        article = clean_text(article) or title

        rewritten = ai_rewrite(article)
        if rewritten:
            if send_post(rewritten, extract_image(entry)):
                posted.add(guid)
                newly_posted_guids.add(guid)
                total_published += 1
                logger.info(f"Опубликовано: {title}")
                continue

        body_text = article if article and article != title else ""
        if body_text:
            body_text = clean_text(body_text)
            if len(body_text) > 500:
                cut = body_text[:500]
                last_period = max(cut.rfind('.'), cut.rfind('!'), cut.rfind('?'))
                body_text = cut[:last_period+1] if last_period > 200 else cut + "…"
            fallback = f"{add_emoji_prefix('🔥 ' + title)}\n\n{body_text}\n\n📢 Подписывайтесь: @Hype_Zhor"
        else:
            fallback = f"{add_emoji_prefix('🔥 ' + title)}\n\n📢 Подписывайтесь: @Hype_Zhor"
        if send_post(fallback, extract_image(entry)):
            posted.add(guid)
            newly_posted_guids.add(guid)
            total_published += 1
            logger.warning(f"Fallback: {title}")

        time.sleep(3)

    if newly_posted_guids:
        save_posted_guids(posted.union(newly_posted_guids))
        logger.info(f"Сохранено {len(newly_posted_guids)} GUID")

    logger.info(f"Готово. Постов: {total_published}.")

if __name__ == "__main__":
    main()
