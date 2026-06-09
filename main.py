# ==================== AI через OpenRouter (с автоматическим переключением) ====================
# Список моделей в порядке приоритета (только проверенные)
OPENROUTER_MODELS = [
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "microsoft/phi-3-mini-128k-instruct:free"
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
                timeout=45,
            )

            if response.status_code == 200:
                data = response.json()
                # Безопасно извлекаем контент
                if data.get("choices") and len(data["choices"]) > 0:
                    message = data["choices"][0].get("message")
                    if message and message.get("content"):
                        content = message["content"].strip()
                        if content:
                            logger.info(f"✅ Успешно использована модель: {model_name}")
                            return content
                        else:
                            logger.warning(f"⚠️ Модель {model_name} вернула пустой текст")
                    else:
                        logger.warning(f"⚠️ Модель {model_name} вернула ответ без message/content: {data}")
                else:
                    logger.warning(f"⚠️ Модель {model_name} вернула ответ без choices: {data}")

            elif response.status_code == 429:
                logger.warning(f"⏳ Модель {model_name} превысила лимит (429), пробуем следующую...")
                time.sleep(5)
                continue
            elif response.status_code == 404:
                logger.warning(f"❌ Модель {model_name} не найдена (404), пробуем следующую...")
                continue
            else:
                logger.error(f"❌ Ошибка {response.status_code} для модели {model_name}: {response.text[:200]}")
                continue

        except Exception as e:
            logger.error(f"❌ Исключение для модели {model_name}: {e}")
            continue

    logger.error("❌ Все модели из списка недоступны, используем fallback.")
    return None
