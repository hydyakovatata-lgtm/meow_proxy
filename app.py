import os
from flask import Flask, request, jsonify
import requests
import random
from datetime import datetime, timedelta
import logging
import certifi

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ===== КОНФИГУРАЦИЯ =====
GEMINI_KEYS = [
    key.strip().strip('"').strip("'")
    for key in os.getenv('GEMINI_KEYS', '').split(',')
    if key.strip()
]
YOUR_MASTER_KEY = os.getenv('MASTER_KEY', '')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-pro')
MAX_INPUT_TOKENS = int(os.getenv('MAX_INPUT_TOKENS', 2048000))
MAX_OUTPUT_TOKENS = int(os.getenv('MAX_OUTPUT_TOKENS', 65536))
DEFAULT_OUTPUT_TOKENS = int(os.getenv('DEFAULT_OUTPUT_TOKENS', 60000))

# Новая конфиг для имитации OpenRouter (без смены API)
MIMIC_OPENROUTER = os.getenv('MIMIC_OPENROUTER', 'false').lower() == 'true'

# Проверка ключей
if not GEMINI_KEYS:
    raise ValueError("❌ No Gemini keys found in environment variables")

if not YOUR_MASTER_KEY:
    raise ValueError("❌ No master key found in environment variables")

key_usage = {key: {'requests': 0, 'last_used': None, 'errors': 0} for key in GEMINI_KEYS}

# Инструкция для модели
DETAILED_INSTRUCTION = """You are Gemini 2.5 Pro, a large language model from google.

Formatting Rules:
- Use Markdown **only when semantically appropriate**. Examples: `inline code`, ```code fences```, tables, and lists.
- In assistant responses, format file names, directory paths, function names, and class names with backticks (`).
- For math: use \( and \) for inline expressions, and \[ and \] for display (block) math."""
class KeyBalancer:
    """
    Балансировщик ключей с учетом ошибок и cooldown.
    """
    def __init__(self):
        self.cooldown_period = timedelta(minutes=5)

    def get_best_key(self):
        now = datetime.now()
        available_keys = [
            k for k, v in key_usage.items()
            if v['errors'] < 3 and (v['last_used'] is None or now - datetime.fromisoformat(v['last_used']) > self.cooldown_period)
        ]
        if not available_keys:
            available_keys = GEMINI_KEYS  # Fallback на все ключи
        key = min(available_keys, key=lambda k: key_usage[k]['requests'])
        return key

balancer = KeyBalancer()

# ===== ENDPOINT ДЛЯ SILLYTAVERN =====
@app.route('/v1/models', methods=['GET'])
def list_models():
    """Возвращает список доступных моделей."""
    return jsonify({
        "object": "list",
        "data": [
            {
                "id": GEMINI_MODEL,
                "object": "model",
                "created": 1686935000,
                "owned_by": "google",
                "limits": {
                    "max_input_tokens": MAX_INPUT_TOKENS,
                    "max_output_tokens": MAX_OUTPUT_TOKENS
                }
            }
        ]
    })

# ===== ENDPOINTS ДЛЯ JANITORAI =====
@app.route('/v1/engines', methods=['GET'])
def list_engines():
    """Аналог list_models для совместимости."""
    return list_models()

@app.route('/v1/completions', methods=['POST'])
def completions():
    """Перенаправление на chat_completions для совместимости с JanitorAI."""
    logger.info("JanitorAI using chat format, redirecting to chat_completions")
    return chat_completions()

# ===== OPENAI-СОВМЕСТИМЫЙ API =====
@app.route('/v1/chat/completions', methods=['POST', 'OPTIONS'])
def chat_completions():
    """OpenAI-совместимый эндпоинт для генерации чата через Gemini."""
    if request.method == 'OPTIONS':
        return '', 200

    try:
        data = request.json
        if not data or 'messages' not in data:
            return jsonify({"error": "Invalid request format"}), 400

        gemini_key = balancer.get_best_key()
        key_usage[gemini_key]['requests'] += 1
        key_usage[gemini_key]['last_used'] = datetime.now().isoformat()

        logger.info(f"Using key: {gemini_key[:20]}... | Requests: {key_usage[gemini_key]['requests']}")

        contents = []
        system_instruction = DETAILED_INSTRUCTION

        # Формируем историю сообщений
        if MIMIC_OPENROUTER:
            # Имитация OpenRouter: Включаем system как первое 'user' сообщение в contents (для похожей обработки промпта)
            contents.append({
                "role": "user",
                "parts": [{"text": f"System instruction: {system_instruction}"}]
            })
            system_instruction = ""  # Отключаем separate, чтобы избежать дублирования
            logger.info("Mimicking OpenRouter prompt format: System as first user message")

        for msg in data["messages"]:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({
                "role": role,
                "parts": [{"text": msg["content"]}]
            })

        # Рассчитываем max_tokens с учетом лимитов Gemini
        requested_tokens = data.get("max_tokens", DEFAULT_OUTPUT_TOKENS)
        max_output_tokens = min(requested_tokens, MAX_OUTPUT_TOKENS)

        # Добавляем параметры генерации для имитации OpenRouter (креативность, вариативность)
        temperature = data.get("temperature", 0.95)  # Default как в Gemini/OpenRouter для creativity
        top_p = data.get("top_p", 0.95)
        

        gemini_data = {
            "contents": contents,
        }
        if system_instruction:  # Только если не MIMIC
            gemini_data["system_instruction"] = {
                "parts": [{"text": system_instruction}]
            }
        gemini_data["generationConfig"] = {
            "maxOutputTokens": max_output_tokens,
            "temperature": temperature,  # Добавлено для похожести
            "topP": top_p,              # Добавлено
         
            # JanitorAI сам передает temperature, top_p, etc через data.get()
        }
        # ПОЛНОЕ ОТКЛЮЧЕНИЕ ЦЕНЗУРЫ
        gemini_data["safetySettings"] = [
            {
                "category": "HARM_CATEGORY_HARASSMENT",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_HATE_SPEECH",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                "threshold": "BLOCK_NONE"
            }
        ]

        # Отправка к Gemini с верификацией SSL
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={gemini_key}",
            json=gemini_data,
            headers={'Content-Type': 'application/json'},
            timeout=120,
            verify=certifi.where()
        )

        if response.status_code == 429:
            key_usage[gemini_key]['errors'] += 1
            key_usage[gemini_key]['last_used'] = datetime.now().isoformat()  # Для cooldown
            logger.warning(f"Rate limit for key: {gemini_key[:20]}... Switching key.")
            return chat_completions()  # Рекурсия, но с cooldown для предотвращения петли

        if response.status_code != 200:
            key_usage[gemini_key]['errors'] += 1
            logger.error(f"Gemini API error {response.status_code} for key: {gemini_key[:20]}...")
            return jsonify({"error": f"Gemini API error: {response.status_code}"}), 500

        gemini_response = response.json()

        if ('candidates' not in gemini_response or
            not gemini_response['candidates'] or
            'content' not in gemini_response['candidates'][0] or
            'parts' not in gemini_response['candidates'][0]['content']):
            logger.error(f"Invalid Gemini response structure: {gemini_response}")
            return jsonify({"error": "Invalid response from Gemini API"}), 500

        response_text = gemini_response["candidates"][0]["content"]["parts"][0]["text"]

        total_input_chars = sum(len(msg["content"]) for msg in data["messages"])

        openai_format = {
            "id": f"chatcmpl-{random.randint(1000,9999)}",
            "object": "chat.completion",
            "created": int(datetime.now().timestamp()),
            "model": GEMINI_MODEL,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_text
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": total_input_chars // 4,
                "completion_tokens": len(response_text) // 4,
                "total_tokens": (total_input_chars + len(response_text)) // 4
            }
        }

        logger.info(f"✅ Success! Input: {total_input_chars} chars, Output: {len(response_text)} chars")
        return jsonify(openai_format)

    except requests.Timeout:
        logger.error(f"Timeout for key: {gemini_key}")
        key_usage[gemini_key]['errors'] += 1
        return chat_completions()
    except Exception as e:
        logger.exception(f"❌ Unexpected error: {e}")
        return jsonify({"error": str(e)}), 500

# ===== АУТЕНТИФИКАЦИЯ =====
@app.before_request
def authenticate():
    if request.method == 'OPTIONS':
        return None

    if request.endpoint in ['chat_completions', 'list_models', 'completions']:
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({"error": "Authorization header required"}), 401
        api_key = auth_header.replace('Bearer ', '')
        if api_key != YOUR_MASTER_KEY:
            return jsonify({"error": "Invalid API key"}), 401

# ===== CORS =====
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')  # Для production ограничьте origins
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
    return response

# ===== ИНФОРМАЦИЯ О МОДЕЛИ =====
@app.route('/v1/model-info', methods=['GET'])
def model_info():
    """Информация о модели."""
    return jsonify({
        "model": GEMINI_MODEL,
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "features": ["large_context", "multimodal", "reasoning"]
    })

# ===== HEALTH CHECK =====
@app.route('/health', methods=['GET'])
def health():
    """Проверка статуса сервиса."""
    return jsonify({
        "status": "ok",
        "service": "Gemini Proxy", 
        "timestamp": datetime.now().isoformat(),
        "keys_available": len(GEMINI_KEYS)
        
    })

# ===== ГЛАВНАЯ СТРАНИЦА =====
@app.route('/')
def home():
    """Главная страница с информацией."""
    return """
    <html>
        <head>
            <title>🚀 Gemini Proxy API</title>
            <style>
                body { font-family: Arial, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; }
                h1 { color: #333; }
                ul { line-height: 1.6; }
                a { color: #007bff; text-decoration: none; }
                a:hover { text-decoration: underline; }
            </style>
        </head>
        <body>
            <h1>🚀 Gemini Proxy API</h1>
            <p>Server is running successfully! ✅</p>
            <p>Available endpoints:</p>
            <ul>
                <li><a href="/health">/health</a> - Status check</li>
                <li><a href="/v1/models">/v1/models</a> - List models</li>
                <li>POST /v1/chat/completions - Main chat API</li>
                <li>POST /v1/completions - JanitorAI compatibility</li>
            </ul>
            <p><strong>Usage:</strong> Set API URL to <code>https://meow-meow-mme0.onrender.com/v1</code> in SillyTavern/JanitorAI</p>
        </body>
    </html>
    """

if __name__ == '__main__':
    print("🚀 Production Gemini Proxy starting...")
    print(f"📊 Available keys: {len(GEMINI_KEYS)}")
    print(f"🔑 Your master key: {YOUR_MASTER_KEY[:10]}...")
    print(f"🤖 Model: {GEMINI_MODEL}")
    print(f"📖 Context: {MAX_INPUT_TOKENS:,} tokens")
    print(f"📝 Output: {MAX_OUTPUT_TOKENS:,} tokens")
    print("📍 Endpoint: https://meow-meow-mme0.onrender.com/v1")

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
