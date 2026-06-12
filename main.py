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
    "https://www.vedomosti.ru/rss/issue.xml",
    "https://www.finmarket.ru/rss/mainnews.asp",
    "http://www.cbr.ru/rss/RssNews",
]

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

DATA_FILE = "posted_guids.json"
RETRY_FILE = "retry_queue.json"
MAX_RETRIES = 3
MAX_ITEMS_PER_RUN = 3          # Строго 3 поста за запуск
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
    try:
        page = requests.get(entry.link, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(page.text, "html.parser")
        og = soup.find("meta", property="og:image")
        if og:
            return og.get("content")
    except:
        pass
    return None

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
        if len(line) < 20:
            continue
        if "ria.ru" in line.lower() or "прайм" in line.lower():
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
    return fallback[:8000]

# ==================== AI через OpenRouter ====================
OPENROUTER_MODELS = [
    "openai/gpt-oss-120b:free",
    "google/gemma-4-31b-it:free",
    "z-ai/glm-4.5-air:free",
    "moonshotai/kimi-k2.6:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "openrouter/free"
]

def ai_rewrite(text):
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY не задан, AI недоступен")
        return None

    prompt = f"""Ты финансовый редактор Telegram-канала Investing-24. Полностью перепиши новость.

Требования:
- Новый стиль изложения, не копируй оригинал
- Сохрани факты и цифры
- Добавь подходящие эмодзи
- До 800 символов
- Без упоминания источника
- В конце добавь: 📢 Подписывайтесь: @Investing_24

Текст новости:
{text}"""

    for model_name in OPENROUTER_MODELS:
        for attempt in range(1, 3):
            try:
                response = requests.post(
                    url="https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 800,
                        "temperature": 0.7,
                    },
                    timeout=60,
                )

                if response.status_code == 200:
                    data = response.json()
                    try:
                        content = data["choices"][0]["message"]["content"]
                        if content and isinstance(content, str):
                            content = content.strip()
                            if content:
                                logger.info(f"✅ Успешно использована модель: {model_name}")
                                return content
                            else:
                                logger.warning(f"⚠️ Модель {model_name} вернула пустой текст (попытка {attempt})")
                        else:
                            logger.warning(f"⚠️ Модель {model_name} вернула нестроковое значение: {content} (попытка {attempt})")
                    except (KeyError, IndexError, TypeError) as e:
                        logger.warning(f"⚠️ Модель {model_name} вернула неожиданный формат: {e} (попытка {attempt})")
                    
                    if attempt == 1:
                        time.sleep(3)
                        continue
                    else:
                        break

                elif response.status_code == 429:
                    logger.warning(f"⏳ Модель {model_name} превысила лимит (429), пробуем следующую...")
                    time.sleep(5)
                    break
                elif response.status_code == 404:
                    logger.warning(f"❌ Модель {model_name} не найдена (404), пробуем следующую...")
                    break
                else:
                    logger.error(f"❌ Ошибка {response.status_code} для модели {model_name}: {response.text[:200]}")
                    break

            except Exception as e:
                logger.error(f"❌ Исключение для модели {model_name} (попытка {attempt}): {e}")
                if attempt == 1:
                    time.sleep(3)
                    continue
                else:
                    break

    logger.error("❌ Все модели из списка недоступны, используем fallback.")
    return None

# ==================== RETRY QUEUE ====================
def load_retry_queue():
    if not os.path.exists(RETRY_FILE):
        return {}
    try:
        with open(RETRY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_retry_queue(queue):
    with open(RETRY_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)

def add_to_retry_queue(guid, entry_data):
    queue = load_retry_queue()
    if guid in queue:
        queue[guid]["attempts"] += 1
    else:
        queue[guid] = {
            "attempts": 1,
            "title": entry_data.get("title"),
            "link": entry_data.get("link"),
            "description": entry_data.get("description", ""),
            "published": entry_data.get("published")
        }
    save_retry_queue(queue)

# ==================== TELEGRAM (с fallback при ошибке фото) ====================
def send_post(text, image_url=None):
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    
    def send_text_only():
        result = requests.post(
            f"{base}/sendMessage",
            data={"chat_id": CHANNEL_ID, "text": text, "disable_web_page_preview": True},
            timeout=30
        )
        result.raise_for_status()
        return True

    try:
        if image_url:
            result = requests.post(
                f"{base}/sendPhoto",
                data={"chat_id": CHANNEL_ID, "photo": image_url, "caption": text[:1024]},
                timeout=30
            )
            if result.status_code == 400:
                logger.warning(f"Фото не принято (400), отправляю без фото: {image_url[:100]}")
                return send_text_only()
            result.raise_for_status()
        else:
            return send_text_only()
        return True
    except Exception as e:
        logger.error(f"Ошибка при отправке с фото: {e}, пробую без фото")
        try:
            return send_text_only()
        except Exception as e2:
            logger.error(f"Не удалось отправить даже текст: {e2}")
            return False

# ==================== MAIN ====================
def main():
    logger.info("=== START ===")
    posted = load_posted_guids()
    retry_queue = load_retry_queue()
    newly_posted_guids = set()
    total_published = 0

    # 1. Обработка отложенных новостей (не более MAX_ITEMS_PER_RUN)
    retry_processed = 0
    for guid, data in list(retry_queue.items()):
        if retry_processed >= MAX_ITEMS_PER_RUN:
            break
        if guid in posted:
            del retry_queue[guid]
            continue

        title = data.get("title", "")
        link = data.get("link", "")
        description = data.get("description", "")
        attempts = data.get("attempts", 1)

        logger.info(f"Повторная попытка для отложенной новости: {title} (попытка {attempts}/{MAX_RETRIES})")

        article = extract_article_text(link, description)
        article = clean_text(article)
        if not article:
            article = title

        rewritten = ai_rewrite(article)
        if rewritten:
            image = extract_image_from_url(link)
            if send_post(rewritten, image):
                posted.add(guid)
                newly_posted_guids.add(guid)
                del retry_queue[guid]
                retry_processed += 1
                total_published += 1
                logger.info(f"Отложенная новость опубликована с пересказом: {title}")
        else:
            if attempts >= MAX_RETRIES:
                fallback_text = f"📈 {title}\n\n📢 Подписывайтесь: @Investing_24"
                image = extract_image_from_url(link)
                if send_post(fallback_text, image):
                    posted.add(guid)
                    newly_posted_guids.add(guid)
                    del retry_queue[guid]
                    retry_processed += 1
                    total_published += 1
                    logger.warning(f"Отложенная новость опубликована как fallback после {MAX_RETRIES} попыток: {title}")
            else:
                retry_queue[guid]["attempts"] = attempts + 1
                logger.info(f"Новость остаётся в очереди, попытка {attempts+1}/{MAX_RETRIES}")

        time.sleep(2)

    save_retry_queue(retry_queue)

    # 2. Обработка свежих новостей из RSS (не более оставшихся слотов)
    remaining_slots = MAX_ITEMS_PER_RUN - total_published
    if remaining_slots > 0:
        entries = get_all_entries()
        new_posts = 0

        for entry in entries:
            if new_posts >= remaining_slots:
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

            rewritten = ai_rewrite(article)
            if rewritten:
                image = extract_image(entry)
                if send_post(rewritten, image):
                    posted.add(guid)
                    newly_posted_guids.add(guid)
                    new_posts += 1
                    total_published += 1
                    logger.info(f"Опубликовано: {title}")
            else:
                add_to_retry_queue(guid, {
                    "title": title,
                    "link": entry.link,
                    "description": description,
                    "published": entry.get("published")
                })
                logger.info(f"Новость добавлена в очередь повторных попыток: {title}")

            time.sleep(3)

    # 3. Сохранение состояния
    if newly_posted_guids:
        updated_guids = posted.union(newly_posted_guids)
        save_posted_guids(updated_guids)
        logger.info(f"Сохранено {len(newly_posted_guids)} новых GUID")

    queue_remaining = len(load_retry_queue())
    logger.info(f"Готово. Добавлено {total_published} постов. В очереди осталось {queue_remaining}.")

if __name__ == "__main__":
    main()
