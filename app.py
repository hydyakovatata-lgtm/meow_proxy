import logging
logging.basicConfig(level=logging.DEBUG)
from flask import Flask, request, jsonify
import requests
import random
from datetime import datetime
import logging
import urllib3
import os  

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
if not GEMINI_KEYS or GEMINI_KEYS == ['']:
    logger.error("❌ No Gemini keys found in environment variables")
    exit(1)
    
if not YOUR_MASTER_KEY:
    logger.error("❌ No master key found in environment variables") 
    exit(1)

key_usage = {key: {'requests': 0, 'last_used': None, 'errors': 0} for key in GEMINI_KEYS}

# Конфигурация Gemini 2.5 Pro
GEMINI_MODEL = "gemini-2.5-pro"
MAX_INPUT_TOKENS = 1048576    
MAX_OUTPUT_TOKENS = 65536     
DEFAULT_OUTPUT_TOKENS = 60000  

# Инструкция для подробных ответов
DETAILED_INSTRUCTION = """You are PROHIBITED from describing the thoughts, actions and any words of {{user}}. You are only allowed to describe the consequences of {{user}}'s actions or characters that perform some action on the {{user}}. You must NEVER write {{user}}'s dialogues, actions and thoughts under any circumstances, as they are off-limits; you only write for {{char}} and others.
Write EXTREMELY DETAILED and expansive responses in RUSSIAN.
Minimum response length: 3000+ words.
Describe: details, emotions, environment, dialogues, sensations, character thoughts.
Use rich vocabulary and literary style.
Always develop the story and add new narrative elements.
Focus on sensory descriptions - smells, sounds, textures, tastes.
Include internal monologues and character reflections.
Create immersive atmospheric descriptions.
Expand on subplots and secondary characters.
Never be concise - always elaborate and expand.You are required to adhere to the following
settings.
"""

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
    # JanitorAI уже использует новый формат, просто передаем в chat_completions
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
                "temperature": data.get("temperature", 0.9),  # Увеличил для креативности
                "maxOutputTokens": max_output_tokens,
                "topP": data.get("top_p", 0.95),            # Увеличил для разнообразия
                "topK": data.get("top_k", 60),              # Увеличил для богатства выбора
            },
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
        
        # Отправка к Gemini 2.5 Pro
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
        
        logger.info(f"✅ Success! Input: {total_input_chars} chars, Output: {len(response_text)} chars, Max tokens: {max_output_tokens}")
        return jsonify(openai_format)
        
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        return jsonify({"error": str(e)}), 500

# ===== АУТЕНТИФИКАЦИЯ =====
@app.before_request
def authenticate():
    # Разрешаем OPTIONS запросы без аутентификации
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
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
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

if __name__ == '__main__':
    print("🚀 Production Gemini Proxy starting...")
    print(f"📊 Available keys: {len(GEMINI_KEYS)}")
    print(f"🔑 Your master key: {YOUR_MASTER_KEY}")
    print(f"🤖 Model: {GEMINI_MODEL}")
    print(f"📖 Context: {MAX_INPUT_TOKENS:,} tokens")
    print(f"📝 Output: {MAX_OUTPUT_TOKENS:,} tokens")
    print("📍 Endpoint: http://localhost:5000/v1")
    print("🎯 Production server: Waitress")
    from waitress import serve
    serve(app, host='0.0.0.0', port=5000, threads=10)
