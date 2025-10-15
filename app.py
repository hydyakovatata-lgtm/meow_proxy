import os
from flask import Flask, request, jsonify
import requests
import random
from datetime import datetime
import logging
import urllib3

# Отключаем предупреждения SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ===== КОНФИГУРАЦИЯ =====
GEMINI_KEYS = [key.strip().strip('"').strip("'") for key in os.getenv('GEMINI_KEYS', '').split(',') if key.strip()]
YOUR_MASTER_KEY = os.getenv('MASTER_KEY', '')

# Проверка ключей
if not GEMINI_KEYS:
    logger.error("❌ No Gemini keys found in environment variables")
    exit(1)
    
if not YOUR_MASTER_KEY:
    logger.error("❌ No master key found in environment variables") 
    exit(1)

key_usage = {key: {'requests': 0, 'last_used': None, 'errors': 0} for key in GEMINI_KEYS}

# Конфигурация Gemini
GEMINI_MODEL = "gemini-2.5-pro"
MAX_INPUT_TOKENS = 1048576    
MAX_OUTPUT_TOKENS = 65536     
DEFAULT_OUTPUT_TOKENS = 60000  

# Инструкция для модели
DETAILED_INSTRUCTION = """You are ABSOLUTELY FORBIDDEN from describing the thoughts, actions, or words of the user {{user}}.
You describe ONLY the actions of the character {{char}} and other NPCs. Write VERY DETAILED answers in Russian (2500+ token)

RULES:
1. NEVER write for {{user}} - no dialogues, no actions, no thoughts
2. Write detailed responses in Russian
3. Characters are independent - they have their own goals and opinions
4. The world is alive - unexpected events and consequences are possible

TEXT FORMATTING:
- Thoughts: *italic text* or *Мысли персонажа...*
- Dialogue: «quoted text» or «Диалог персонажа»
- Internal monologue: *Internal thoughts here*
- Actions: regular text
- Descriptions: regular text

STRICTLY PROHIBITED to write for {{user}}!"""

class KeyBalancer:
    def get_best_key(self):
        available_keys = [k for k, v in key_usage.items() if v['errors'] < 3]
        if not available_keys:
            available_keys = GEMINI_KEYS
        key = min(available_keys, key=lambda k: key_usage[k]['requests'])
        # Очистка ключа от лишних символов
        key = key.strip().strip('"').strip("'")
        return key

balancer = KeyBalancer()

# ===== ENDPOINT ДЛЯ SILLYTAVERN =====
@app.route('/v1/models', methods=['GET'])
def list_models():
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
    return list_models()

@app.route('/v1/completions', methods=['POST'])
def completions():
    logger.info("JanitorAI using chat format, redirecting to chat_completions")
    return chat_completions()

# ===== OPENAI-СОВМЕСТИМЫЙ API =====
@app.route('/v1/chat/completions', methods=['POST', 'OPTIONS'])
def chat_completions():
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
        for msg in data["messages"]:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({
                "role": role,
                "parts": [{"text": msg["content"]}]
            })

        # Рассчитываем max_tokens с учетом лимитов Gemini
        requested_tokens = data.get("max_tokens", DEFAULT_OUTPUT_TOKENS)
        max_output_tokens = min(requested_tokens, MAX_OUTPUT_TOKENS)
        
        gemini_data = {
            "contents": contents,
            "system_instruction": {
                "parts": [{"text": system_instruction}]
            },
            "generationConfig": {
                "maxOutputTokens": max_output_tokens,
                # JanitorAI сам передает temperature, top_p, etc через data.get()
            },
            # ПОЛНОЕ ОТКЛЮЧЕНИЕ ЦЕНЗУРЫ
            "safetySettings": [
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
        }
        
        # Отправка к Gemini
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={gemini_key}",
            json=gemini_data,
            headers={'Content-Type': 'application/json'},
            timeout=120,
            verify=False
        )
        
        if response.status_code == 429:
            key_usage[gemini_key]['errors'] += 1
            logger.warning(f"Rate limit for key: {gemini_key[:20]}...")
            return chat_completions()
            
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
        
    except Exception as e:
        logger.error(f"❌ Error: {e}")
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
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
    return response

# ===== ИНФОРМАЦИЯ О МОДЕЛИ =====
@app.route('/v1/model-info', methods=['GET'])
def model_info():
    return jsonify({
        "model": GEMINI_MODEL,
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "features": ["large_context", "multimodal", "reasoning"]
    })

# ===== HEALTH CHECK =====
@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok", 
        "service": "Gemini Proxy",
        "timestamp": datetime.now().isoformat(),
        "keys_available": len(GEMINI_KEYS)
    })

# ===== ГЛАВНАЯ СТРАНИЦА =====
@app.route('/')
def home():
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
