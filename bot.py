import os
import io
import json
import sqlite3
import asyncio
import logging
import subprocess
import sys
import importlib
import site
import time
import re
from datetime import datetime
from pathlib import Path

# ==================== БЛОК БЕЗОПАСНОЙ ЗАГРУЗКИ (SYSTEM BOOT) ====================

def force_install(package_name, import_name=None):
    if import_name is None:
        import_name = package_name
    try:
        importlib.import_module(import_name)
        return True
    except ImportError:
        pass
    print(f"🔄 [SYSTEM] Устанавливаю {package_name}...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package_name])
        importlib.invalidate_caches()
        user_site = site.getusersitepackages()
        if user_site not in sys.path:
            sys.path.append(user_site)
        importlib.import_module(import_name)
        print(f"✅ [SYSTEM] {package_name} установлен.")
        return True
    except Exception as e:
        print(f"❌ [SYSTEM] Ошибка установки {package_name}: {e}")
        return False

def install_browsers():
    if not HAS_PLAYWRIGHT: return
    print("🔄 [SYSTEM] Проверка браузеров Chromium...")
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
    except Exception as e:
        print(f"⚠️ [SYSTEM] Ошибка установки браузера (игнорируем): {e}")

print("🚀 [BOOT] Инициализация системы...")
force_install("requests")
force_install("python-telegram-bot", "telegram")
force_install("aiosqlite")
IS_TERMUX = "com.termux" in os.environ.get("PREFIX", "") or "com.termux" in sys.prefix
if IS_TERMUX:
    print("⏭️ [SYSTEM] Termux: пропускаю playwright (не поддерживается на Android).")
    HAS_PLAYWRIGHT = False
else:
    HAS_PLAYWRIGHT = force_install("playwright", "playwright.async_api")
if HAS_PLAYWRIGHT:
    install_browsers()

async_playwright = None
try:
    if HAS_PLAYWRIGHT:
        from playwright.async_api import async_playwright
except ImportError:
    pass

import requests
print("✅ [BOOT] Среда готова. Запуск Telegram бота...")

# ==================== ОСНОВНОЙ КОД БОТА ====================

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ParseMode

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = "8271478255:AAG0PDYzM1YLGTjokJqSMaJhjRiiPdm7df4"
DEVELOPER_ID = 1170970828  # ID разработчика (команды управления ключами)

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"

# ==================== СИСТЕМА ХРАНЕНИЯ ДАННЫХ (БД) ====================

def init_database():
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            command TEXT NOT NULL,
            description TEXT DEFAULT 'Без описания',
            code TEXT NOT NULL,
            author TEXT,
            author_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, command)
        )
    ''')
    try:
        cursor.execute("ALTER TABLE scripts ADD COLUMN ai_comment TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # колонка уже существует
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            data TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS execution_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            user_id INTEGER,
            command TEXT,
            success INTEGER,
            error_message TEXT,
            executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    logger.info(f"✅ База данных инициализирована: {DB_PATH}")

def get_db_connection():
    return sqlite3.connect(str(DB_PATH))

def load_data():
    global scripts_registry, users_data, bot_state
    init_database()
    conn = get_db_connection()
    cursor = conn.cursor()
    scripts_registry = {}
    cursor.execute("SELECT chat_id, command, description, code, author, author_id, created_at, updated_at, ai_comment FROM scripts")
    for row in cursor.fetchall():
        chat_id, command, description, code, author, author_id, created_at, updated_at, ai_comment = row
        if chat_id not in scripts_registry:
            scripts_registry[chat_id] = {}
        scripts_registry[chat_id][command] = {
            'description': description, 'code': code, 'author': author,
            'author_id': author_id, 'ai_comment': ai_comment or '', 'created': created_at, 'updated': updated_at
        }
    users_data = {}
    cursor.execute("SELECT user_id, username, first_name, data FROM users")
    for row in cursor.fetchall():
        user_id, username, first_name, data = row
        try:
            users_data[user_id] = {'username': username, 'first_name': first_name,
                                   'data': json.loads(data) if data else {}}
        except: pass
    bot_state = {}
    cursor.execute("SELECT key, value FROM bot_state")
    for row in cursor.fetchall():
        try: bot_state[row[0]] = json.loads(row[1])
        except: bot_state[row[0]] = row[1]
    conn.close()
    logger.info(f"📦 Загружено скриптов: {sum(len(s) for s in scripts_registry.values())}")

def persist_globals():
    """Синхронизирует глобальные переменные ИИ с bot_state перед сохранением"""
    bot_state['api_keys'] = api_keys
    bot_state['active_key_index'] = active_key_index

def save_data():
    try:
        persist_globals()
        conn = get_db_connection()
        cursor = conn.cursor()
        for chat_id, scripts in scripts_registry.items():
            for command, info in scripts.items():
                cursor.execute('''
                    INSERT OR REPLACE INTO scripts 
                    (chat_id, command, description, code, author, author_id, ai_comment, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (str(chat_id), command, info.get('description', 'Без описания'),
                      info['code'], info.get('author'), info.get('author_id'), info.get('ai_comment', '')))
        for user_id, info in users_data.items():
            cursor.execute('INSERT OR REPLACE INTO users (user_id, username, first_name, data) VALUES (?, ?, ?, ?)',
                           (user_id, info.get('username'), info.get('first_name'),
                            json.dumps(info.get('data', {}), ensure_ascii=False)))
        for key, value in bot_state.items():
            cursor.execute('INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)',
                           (key, json.dumps(value, ensure_ascii=False)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения: {e}")

def save_script_to_db(chat_id, command, description, code, author, author_id=None, ai_comment=''):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO scripts (chat_id, command, description, code, author, author_id, ai_comment, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ''', (str(chat_id), command, description, code, author, author_id, ai_comment))
    conn.commit()
    conn.close()
    if chat_id not in scripts_registry:
        scripts_registry[chat_id] = {}
    scripts_registry[chat_id][command] = {
        'description': description, 'code': code, 'author': author,
        'author_id': author_id, 'ai_comment': ai_comment, 'updated': datetime.now().isoformat()
    }

def delete_script_from_db(chat_id, command):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM scripts WHERE chat_id = ? AND command = ?", (str(chat_id), command))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if deleted and chat_id in scripts_registry and command in scripts_registry[chat_id]:
        del scripts_registry[chat_id][command]
    return deleted

def get_script_from_db(chat_id, command):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT description, code, author, author_id, created_at, updated_at, ai_comment FROM scripts WHERE chat_id = ? AND command = ?", (str(chat_id), command))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {'description': row[0], 'code': row[1], 'author': row[2], 'author_id': row[3], 'created': row[4], 'updated': row[5], 'ai_comment': row[6] or ''}
    return None

def get_chat_scripts(chat_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT command, description, author FROM scripts WHERE chat_id = ?", (str(chat_id),))
    scripts = {row[0]: {'description': row[1], 'author': row[2]} for row in cursor.fetchall()}
    conn.close()
    return scripts

def log_execution(chat_id, user_id, command, success, error_message=None):
    try:
        conn = get_db_connection()
        conn.execute('INSERT INTO execution_logs (chat_id, user_id, command, success, error_message) VALUES (?, ?, ?, ?, ?)',
                     (str(chat_id), user_id, command, 1 if success else 0, error_message))
        conn.commit()
        conn.close()
    except: pass

def save_user(user_id, username, first_name, extra_data=None):
    try:
        if user_id not in users_data:
            users_data[user_id] = {'username': username, 'first_name': first_name, 'data': {}}
        else:
            users_data[user_id]['username'] = username
            users_data[user_id]['first_name'] = first_name
        if extra_data: users_data[user_id]['data'].update(extra_data)
        conn = get_db_connection()
        conn.execute('INSERT OR REPLACE INTO users (user_id, username, first_name, data) VALUES (?, ?, ?, ?)',
                     (user_id, username, first_name, json.dumps(users_data[user_id].get('data', {}), ensure_ascii=False)))
        conn.commit()
        conn.close()
    except: pass

# --- ПЕРЕМЕННЫЕ СОСТОЯНИЯ ---
scripts_registry = {}
users_data = {}
bot_state = {}
load_data()
pending_scripts = {}
editing_scripts = {}

# ==================== СИСТЕМА ИИ (GROQ) ====================

DEFAULT_GROQ_KEYS = ["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"]
if not bot_state.get('api_keys'):
    bot_state['api_keys'] = [{'key': k, 'status': 'ok', 'limited_until': 0} for k in DEFAULT_GROQ_KEYS]

api_keys = bot_state['api_keys']                      # пул ключей Groq
active_key_index = int(bot_state.get('active_key_index', 0) or 0)
for _i, _k in enumerate(api_keys):
    _k.setdefault('added_at', '')
ai_creation = {}
ai_edit = {}  # процесс починки скрипта через ИИ                                      # процесс создания мини-бота ИИ
WORKING_MODEL = None                                  # запомненная рабочая модель

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
MODELS_TO_TRY = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "groq/compound",
    "openai/gpt-oss-20b",
    "groq/compound-mini",
]
AI_SCRIPT_SYSTEM = (
    "Ты — генератор кода для платформы Telegram-бота (python-telegram-bot v20+, asyncio).\n"
    "Верни скрипт СТРОГО в формате, без пояснений и без markdown-ограждений:\n"
    "###COMMAND: /{command}\n"
    "###DESCRIPTION: короткое описание на русском\n"
    "###COMMENT: 1-2 предложения на русском: что делает скрипт / что именно изменено\n"
    "###CODE:\n"
    "<python код>\n\n"
    "Требования к коду:\n"
    "- Обязательно определи async def execute(update, context, args) — точку входа; она может вернуть строку, которая отправится пользователю.\n"
    "- При желании async def check_triggers(update, context) — срабатывает на каждом сообщении чата.\n"
    "- При желании async def handle_callback(update, context, data) — обработчик инлайн-кнопок.\n"
    "- В namespace УЖЕ доступны (НЕ импортируй их): update, context, args, requests, asyncio, json, re, math, random, datetime, time, os, sys, sqlite3, hashlib, base64, pathlib, shutil, DB_PATH, DATA_DIR, InlineKeyboardButton, InlineKeyboardMarkup, Update, ContextTypes, ParseMode, async_playwright.\n"
    "- Другие библиотеки использовать ЗАПРЕЩЕНО.\n"
    "- Код должен быть синтаксически корректным и безопасным.\n"
    "\n"
    "КРИТИЧЕСКОЕ АРХИТЕКТУРНОЕ ПРАВИЛО (обязательно к соблюдению):\n"
    "Скрипты выполняются через `exec(code, local_ns)` при КАЖДОМ вызове команды/коллбэка — заново.\n"
    "Это значит:\n"
    "  ❌ ЗАПРЕЩЕНО хранить состояние игры/сессии в глобальных переменных уровня модуля\n"
    "     (типа `GAMES = {{}}`, `PLAYERS = []`, `SESSION = {{}}` в начале файла).\n"
    "     Они сбрасываются при каждом нажатии кнопки или вызове команды!\n"
    "  ✅ ВМЕСТО ЭТОГО храни состояние в БД (sqlite3 через DB_PATH) или в context.bot_data.\n"
    "\n"
    "Шаблон-хелпер для хранения состояния (используй его в своих скриптах, меняя TABLE_NAME):\n"
    "```\n"
    "import sqlite3, json\n"
    "TABLE_12 = 'my_script_state'  # уникальное имя для каждого скрипта\n"
    "def _init_state():\n"
    "    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()\n"
    "    cur.execute(f'CREATE TABLE IF NOT EXISTS {{TABLE_12}} (chat_id INTEGER PRIMARY KEY, data TEXT)')\n"
    "    conn.commit(); conn.close()\n"
    "_init_state()\n"
    "def get_state(chat_id):\n"
    "    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()\n"
    "    cur.execute(f'SELECT data FROM {{TABLE_12}} WHERE chat_id=?', (chat_id,))\n"
    "    row = cur.fetchone(); conn.close()\n"
    "    return json.loads(row[0]) if row else None\n"
    "def save_state(chat_id, state):\n"
    "    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()\n"
    "    cur.execute(f'INSERT OR REPLACE INTO {{TABLE_12}} (chat_id,data) VALUES (?,?)',\n"
    "                (chat_id, json.dumps(state, ensure_ascii=False)))\n"
    "    conn.commit(); conn.close()\n"
    "def delete_state(chat_id):\n"
    "    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()\n"
    "    cur.execute(f'DELETE FROM {{TABLE_12}} WHERE chat_id=?', (chat_id,))\n"
    "    conn.commit(); conn.close()\n"
    "```\n"
    "При обработке коллбэков всегда начинай с `game = get_state(chat_id)` и после изменений делай `save_state(chat_id, game)`.\n"
    "КОНТРАКТ ПЛАТФОРМЫ (обязательно):\n"
    "- execute(): может вернуть СТРОКУ — она отправится пользователю. Если нужны кнопки — отправь сообщение сам: await context.bot.send_message(chat_id=update.effective_chat.id, text=..., reply_markup=...) и верни None.\n"
    "- ЗАПРЕЩЕНО возвращать из execute словарь вида {'text': ..., 'reply_markup': ...} — платформа не умеет его показывать.\n"
    "- handle_callback(update, context, data): обязан сам вызвать await query.answer() (query = update.callback_query) и сам изменить сообщение: await query.edit_message_text(text=..., reply_markup=...); оберни в try/except (ошибка 'message is not modified'). Верни True, если коллбэк твой, и False, если нет.\n"
    "- ЗАПРЕЩЕНО возвращать из handle_callback словарь — платформа его игнорирует, кнопки будут мёртвыми.\n"
    "- Состояние из JSON всегда сливай с дефолтом: base = default_dict(); base.update(loaded) — иначе старые сохранения упадут с KeyError.\n"
    "- Ключ состояния выбирай осознанно: user_id — личный прогресс игрока, chat_id — общий прогресс чата.\n"
    "Имя команды пользователя: /{command} — используй именно его в ###COMMAND.\n"
    "Идея пользователя (что должен уметь мини-бот): {prompt}"
)

AI_FIX_SYSTEM = (
    "Ты — ремонтник кода для скриптов Telegram-бота (python-telegram-bot v20+, asyncio).\n"
    "Тебе дают ТЕКУЩИЙ код скрипта и запрос пользователя с описанием неполадки или улучшения.\n"
    "Верни ИСПРАВЛЕННЫЙ скрипт ЦЕЛИКОМ, строго в формате, без пояснений и без markdown-ограждений:\n"
    "###DESCRIPTION: короткое описание на русском\n"
    "###COMMENT: 1-2 предложения на русском: что делает скрипт / что именно изменено\n"
    "###CODE:\n"
    "<полный python код>\n\n"
    "Правила:\n"
    "- Сохрани async def execute(update, context, args); сохрани check_triggers / handle_callback, если они есть и запрос не требует их убрать.\n"
    "- В namespace УЖЕ доступны (НЕ импортируй): update, context, args, requests, asyncio, json, re, math, random, datetime, time, os, sys, sqlite3, hashlib, base64, pathlib, shutil, DB_PATH, DATA_DIR, InlineKeyboardButton, InlineKeyboardMarkup, Update, ContextTypes, ParseMode, async_playwright.\n"
    "- Другие библиотеки использовать ЗАПРЕЩЕНО.\n"
    "- Исправь именно то, что просит пользователь, не ломай остальную логику.\n"
    "\n"
    "КРИТИЧЕСКАЯ АРХИТЕКТУРНАЯ ПРОВЕРКА (обязательно):\n"
    "Если в коде есть глобальные переменные уровня модуля для хранения состояния\n"
    "(например `GAMES = {{}}`, `SESSIONS = {{}}`, любые словари/списки для игр или сессий) —\n"
    "это ОШИБКА. Скрипт выполняется через `exec()` заново при каждом коллбэке,\n"
    "поэтому такие переменные сбрасываются.\n"
    "✅ ЗАМЕНИ на хранение в БД через DB_PATH (sqlite3 + json). Пример:\n"
    "```\n"
    "import sqlite3, json\n"
    "TABLE = 'script_state'\n"
    "def _init():\n"
    "    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()\n"
    "    cur.execute(f'CREATE TABLE IF NOT EXISTS {{TABLE}} (chat_id INTEGER PRIMARY KEY, data TEXT)')\n"
    "    conn.commit(); conn.close()\n"
    "_init()\n"
    "def get_state(cid):\n"
    "    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()\n"
    "    cur.execute(f'SELECT data FROM {{TABLE}} WHERE chat_id=?', (cid,))\n"
    "    row = cur.fetchone(); conn.close()\n"
    "    return json.loads(row[0]) if row else None\n"
    "def save_state(cid, s):\n"
    "    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()\n"
    "    cur.execute(f'INSERT OR REPLACE INTO {{TABLE}} (chat_id,data) VALUES (?,?)',\n"
    "                (cid, json.dumps(s, ensure_ascii=False)))\n"
    "    conn.commit(); conn.close()\n"
    "```\n"
    "Если в исходном коде есть глобальное состояние — исправь его именно этим способом,\n"
    "даже если пользователь не просил об этом явно (укажи в ###COMMENT, что исправил).\n"
    "ТЕКУЩИЙ КОД СКРИПТА:\n{code}\n\n"
    "ПРОВЕРКА КОНТРАКТА ПЛАТФОРМЫ (обязательно):\n"
    "- Если execute возвращает словарь — перепиши на строку или на самов отправку сообщения с return None.\n"
    "- Если handle_callback возвращает словарь или не редактирует сообщение сам — перепиши: query.answer(), query.edit_message_text(...) в try/except, return True/False.\n"
    "- Если состояние читается из JSON без слияния с дефолтом (риск KeyError на старых сохранениях) — добавь: base = default_dict(); base.update(data).\n"
    "Последняя правка (комментарий ИИ): {prev_comment}\n"
    "Запрос пользователя: {prompt}"
)

RESERVED_COMMANDS = {'start', 'help', 'ai', 'clear', 'models', 'addscript', 'listscripts',
                     'viewscript', 'editscript', 'deletescript', 'cancel', 'addkey', 'keys', 'usekey', 'delkey'}

def _now():
    return time.time()

def pick_key(start_idx):
    """Выбирает первый доступный (не лимитированный и не невалидный) ключ по кругу"""
    n = len(api_keys)
    if n == 0:
        return None, None
    for i in range(n):
        idx = (start_idx + i) % n
        k = api_keys[idx]
        if k.get('status') == 'invalid':
            continue
        if k.get('status') == 'limited' and k.get('limited_until', 0) > _now():
            continue
        if k.get('status') == 'limited':
            k['status'] = 'ok'  # лимит прошёл
        return idx, k['key']
    return None, None

def extract_err(r):
    try:
        return r.json().get("error", {}).get("message", r.text[:300])
    except Exception:
        return r.text[:300]

def extract_ai_comment(text):
    """Достаёт короткий комментарий ИИ (###COMMENT:) из ответа."""
    for line in text.split('\n'):
        if line.startswith('###COMMENT:'):
            return line.replace('###COMMENT:', '', 1).strip()
    return ''


def clean_fences(text):
    lines = [l for l in text.split('\n') if not l.strip().startswith('```')]
    return '\n'.join(lines).strip()

def ask_ai(messages, max_tokens=2048):
    """Запрос к Groq с авто-переключением ключей и моделей.
    Возвращает dict: {'ok': bool, 'text': str, 'notice': str|None}"""
    global active_key_index, WORKING_MODEL
    notices = []
    if not api_keys:
        return {'ok': False, 'text': "❌ Не настроено ни одного ключа Groq. Попросите разработчика добавить ключ (/addkey).", 'notice': None}
    n = len(api_keys)
    start = active_key_index % n
    last_model_err = None

    for step in range(n):
        idx, key = pick_key(start + step)
        if idx is None:
            break
        models = ([WORKING_MODEL] if WORKING_MODEL else []) + [m for m in MODELS_TO_TRY if m != WORKING_MODEL]
        switched = False
        for model in models:
            payload = {
                "model": model,
                "messages": messages,
                "max_completion_tokens": max_tokens
            }
            try:
                r = requests.post(GROQ_URL,
                                  headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                  json=payload, timeout=120)
            except Exception as e:
                return {'ok': False, 'text': f"⚠️ Ошибка сети: {e}", 'notice': "\n".join(notices) or None}

            if r.status_code == 200:
                try:
                    answer = r.json()["choices"][0]["message"]["content"]
                except Exception:
                    return {'ok': False, 'text': "⚠️ Не удалось разобрать ответ Groq.", 'notice': "\n".join(notices) or None}
                active_key_index = idx
                WORKING_MODEL = model
                return {'ok': True, 'text': answer, 'notice': "\n".join(notices) or None}

            if r.status_code in (400, 404):  # модель отключена — пробуем следующую
                last_model_err = f"{model}: {extract_err(r)}"
                continue

            if r.status_code == 429:  # ЛИМИТ → помечаем ключ и переключаемся
                try: retry = int(r.headers.get('retry-after', 60) or 60)
                except: retry = 60
                api_keys[idx]['status'] = 'limited'
                api_keys[idx]['limited_until'] = _now() + retry
                notices.append(f"⚠️ Ключ #{idx+1} упёрся в лимит запросов. Переключаюсь на следующий ключ... (восстановление ~{retry} сек)")
                switched = True
                break

            if r.status_code == 401:  # ключ невалиден
                api_keys[idx]['status'] = 'invalid'
                notices.append(f"⚠️ Ключ #{idx+1} недействителен (401). Переключаюсь...")
                switched = True
                break

            return {'ok': False, 'text': f"⚠️ Ошибка API ({r.status_code}): {extract_err(r)}", 'notice': "\n".join(notices) or None}

        if not switched:  # все модели отвергнуты
            return {'ok': False, 'text': f"❌ Ни одна модель не смогла ответить.\nПоследняя ошибка: {last_model_err}", 'notice': "\n".join(notices) or None}

    # Сюда попадаем, если все ключи в лимите/невалидны
    if all(k.get('status') == 'invalid' for k in api_keys):
        msg = "❌ Все API-ключи Groq недействительны. Попросите разработчика добавить рабочий ключ."
    else:
        limited = [k for k in api_keys if k.get('status') == 'limited' and k.get('limited_until', 0) > _now()]
        if limited:
            secs = int(min(k['limited_until'] for k in limited) - _now())
            msg = f"⚠️ Все ключи упёрлись в лимиты. Попробуйте снова через ~{max(1, secs // 60)} мин."
        else:
            msg = "❌ Нет доступных ключей Groq."
    return {'ok': False, 'text': msg, 'notice': "\n".join(notices) or None}

# ==================== ХЕНДЛЕРЫ ТЕЛЕГРАМ ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 *Привет! Я бот с кастомными скриптами!*\n\n"
        f"📌 *Команды:*\n"
        f"`/addscript` - Добавить новый скрипт (есть кнопка «Помощь бота»)\n"
        f"`/listscripts` - Список скриптов чата\n"
        f"`/viewscript <команда>` - Посмотреть код\n"
        f"`/editscript <команда>` - Редактировать скрипт\n"
        f"`/deletescript <команда>` - Удалить скрипт\n"
        f"`/cancel` - Отменить текущее действие\n"
        f"`/help` - Помощь\n\n"
        f"💡 Вы можете создавать свои команды!",
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ `/addscript` — добавить код вручную или через кнопку «🤖 Помощь бота» (ИИ сам напишет скрипт по вашему описанию).",
        parse_mode='Markdown')

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cancelled = False
    if uid in pending_scripts: del pending_scripts[uid]; cancelled = True
    if uid in editing_scripts: del editing_scripts[uid]; cancelled = True
    if uid in ai_creation: del ai_creation[uid]; cancelled = True
    if uid in ai_edit: del ai_edit[uid]; cancelled = True
    await update.message.reply_text("❌ Действие отменено." if cancelled else "ℹ️ Нет активных действий.")

# ---------- КОМАНДЫ РАЗРАБОТЧИКА (ПУЛ КЛЮЧЕЙ GROQ) ----------

def is_dev(uid):
    return uid == DEVELOPER_ID

def mask_key(k):
    return k[:6] + "…" + k[-4:] if len(k) > 12 else k

async def dev_addkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_dev(update.effective_user.id):
        return await update.message.reply_text("⛳ Команда доступна только разработчику.")
    if not context.args:
        return await update.message.reply_text("Использование: `/addkey gsk_...`", parse_mode='Markdown')
    key = context.args[0].strip()
    if any(k['key'] == key for k in api_keys):
        return await update.message.reply_text("ℹ️ Такой ключ уже есть в пуле.")
    api_keys.append({'key': key, 'status': 'ok', 'limited_until': 0, 'added_at': datetime.now().isoformat()})
    save_data()
    await update.message.reply_text(f"✅ Ключ #{len(api_keys)} (`{mask_key(key)}`) добавлен в пул. Теперь при лимитах бот сам переключится на него.", parse_mode='Markdown')

async def dev_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_dev(update.effective_user.id):
        return await update.message.reply_text("⛳ Команда доступна только разработчику.")
    if not api_keys:
        return await update.message.reply_text("📭 Пул ключей пуст.")
    order = sorted(range(len(api_keys)),
                   key=lambda i: api_keys[i].get('added_at') or f"0000{i:04d}")
    lines = []
    for pos, i in enumerate(order, 1):
        k = api_keys[i]
        if k.get('status') == 'invalid':
            status = "❌ недействителен (замени ключ)"
        elif k.get('status') == 'limited' and k.get('limited_until', 0) > _now():
            left = int(k['limited_until'] - _now())
            status = f"🔄 восстанавливает лимиты (~{left // 60} мин {left % 60} сек)"
        else:
            status = "✅ в строю"
        mark = " ← активный" if i == active_key_index else ""
        date = (k.get('added_at') or '')[:10] or 'б/д'
        lines.append(f"{pos}. `#{i+1}` `{mask_key(k['key'])}`\n    📅 {date} | {status}{mark}")
    await update.message.reply_text(
        "🔑 *Пул ключей Groq (по дате добавления):*\n" + "\n".join(lines) +
        "\n\n`/usekey N` - переключить, `/delkey N` - удалить, `/addkey gsk_...` - добавить",
        parse_mode='Markdown')

async def dev_usekey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_key_index
    if not is_dev(update.effective_user.id):
        return await update.message.reply_text("⛳ Команда доступна только разработчику.")
    if not context.args:
        return await update.message.reply_text("Использование: `/usekey N`", parse_mode='Markdown')
    try: idx = int(context.args[0]) - 1
    except ValueError:
        return await update.message.reply_text("❌ Нужно число.")
    if not (0 <= idx < len(api_keys)):
        return await update.message.reply_text(f"❌ Ключа #{idx+1} нет.")
    api_keys[idx]['status'] = 'ok'
    api_keys[idx]['limited_until'] = 0
    active_key_index = idx
    save_data()
    await update.message.reply_text(f"✅ Активный ключ переключён на #{idx+1} (`{mask_key(api_keys[idx]['key'])}`).", parse_mode='Markdown')

async def dev_delkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_key_index
    if not is_dev(update.effective_user.id):
        return await update.message.reply_text("⛳ Команда доступна только разработчику.")
    if not context.args:
        return await update.message.reply_text("Использование: `/delkey N`", parse_mode='Markdown')
    try: idx = int(context.args[0]) - 1
    except ValueError:
        return await update.message.reply_text("❌ Нужно число.")
    if not (0 <= idx < len(api_keys)):
        return await update.message.reply_text(f"❌ Ключа #{idx+1} нет.")
    removed = api_keys.pop(idx)
    if active_key_index >= len(api_keys): active_key_index = 0
    save_data()
    warn = "\n⚠️ Пул пуст — ИИ не будет работать, пока не добавите ключ." if not api_keys else ""
    await update.message.reply_text(f"🗑 Ключ `{mask_key(removed['key'])}` удалён.{warn}", parse_mode='Markdown')

# ---------- СКРИПТЫ: ЗАГРУЗКА / РЕДАКТИРОВАНИЕ ----------

async def add_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    ai_creation.pop(user_id, None)   # FIX: отменяем создание через ИИ
    pending_scripts[user_id] = {
        'chat_id': chat_id, 'code': '', 'command': None, 'description': 'Без описания', 'stage': 'waiting_first'
    }
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🤖 Помощь бота", callback_data="aicreate_start")]])
    await update.message.reply_text(
        "📝 *Отправьте скрипт в следующем формате:*\n\n"
        "```\n"
        "###COMMAND: название_команды\n"
        "###DESCRIPTION: описание\n"
        "###CODE:\n"
        "# Ваш Python код здесь\n"
        "async def execute(update, context, args):\n"
        "    return 'Результат'\n"
        "```\n\n"
        "📌 Можно отправлять код частями!\n"
        "🤖 Кнопка «Помощь бота» — ИИ напишет скрипт за вас по описанию.\n"
        "⚠️ `/cancel` - отменить",
        parse_mode='Markdown', reply_markup=kb
    )

async def view_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if not context.args: return await update.message.reply_text("❌ Укажите команду: `/viewscript /команда`", parse_mode='Markdown')
    command = context.args[0].lower()
    if not command.startswith('/'): command = '/' + command
    script_info = get_script_from_db(chat_id, command)
    if not script_info: return await update.message.reply_text(f"❌ Скрипт `{command}` не найден!", parse_mode='Markdown')
    code = script_info['code']
    if len(code) > 3000:
        f = io.BytesIO(code.encode('utf-8'))
        f.name = f"{command}.txt"
        await update.message.reply_document(f, caption=f"📄 Исходный код {command}")
    else:
        await update.message.reply_text(f"📄 *Скрипт:* `{command}`\n```python\n{code}\n```", parse_mode='Markdown')

async def edit_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    if not context.args: return await update.message.reply_text("❌ Укажите команду: `/editscript /команда`", parse_mode='Markdown')
    command = context.args[0].lower()
    if not command.startswith('/'): command = '/' + command
    script_info = get_script_from_db(chat_id, command)
    if not script_info: return await update.message.reply_text(f"❌ Скрипт `{command}` не найден!", parse_mode='Markdown')
    editing_scripts[user_id] = {'chat_id': chat_id, 'command': command, 'code': '', 'stage': 'waiting_new_code'}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🤖 Помощь бота (починить через ИИ)", callback_data=f"aiedit_start:{command}")]])
    await update.message.reply_text(f"✏️ *Редактирование* `{command}`. Отправьте новый код.\n🤖 Или нажмите кнопку и опишите неполадку словами — ИИ сам исправит скрипт.", parse_mode='Markdown', reply_markup=kb)

def parse_script_text(text):
    lines = text.strip().split('\n')
    command = None
    description = "Без описания"
    code_lines = []
    in_code = False
    for line in lines:
        if line.startswith('###COMMAND:'):
            command = line.replace('###COMMAND:', '').strip().lower()
            if command and not command.startswith('/'): command = '/' + command
        elif line.startswith('###DESCRIPTION:'):
            description = line.replace('###DESCRIPTION:', '').strip()
        elif line.startswith('###CODE:'):
            in_code = True
        elif in_code:
            code_lines.append(line)
    return command, description, '\n'.join(code_lines)

async def handle_script_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    if user_id in editing_scripts:
        editing = editing_scripts[user_id]
        lower = text.lower().strip()
        if lower in ['готово', 'done', 'save', 'сохранить']:
            if not editing['code'].strip(): return await update.message.reply_text("❌ Код пустой!")
            sinfo = get_script_from_db(editing['chat_id'], editing['command'])
            save_script_to_db(editing['chat_id'], editing['command'], editing.get('desc', sinfo['description']), editing['code'], sinfo['author'], sinfo['author_id'])
            save_data()
            del editing_scripts[user_id]
            return await update.message.reply_text(f"✅ Скрипт `{editing['command']}` обновлен!", parse_mode='Markdown')
        if lower in ['да', 'yes', 'ещё', 'еще']:
            return await update.message.reply_text("📝 Жду продолжение кода...")
        if editing['stage'] == 'waiting_new_code':
            c, d, code = parse_script_text(text)
            editing['code'] = code if code else text
            if d != "Без описания": editing['desc'] = d
            editing['stage'] = 'more'
        else:
            editing['code'] += '\n' + text
        await update.message.reply_text("✅ Код получен. Напишите `готово` для сохранения или отправьте еще часть.", parse_mode='Markdown')
        return True

    if user_id in pending_scripts:
        pending = pending_scripts[user_id]
        lower = text.lower().strip()
        if lower in ['готово', 'done', 'save', 'сохранить']:
            if not pending['command']: return await update.message.reply_text("❌ Не указана команда (###COMMAND:)!")
            save_script_to_db(pending['chat_id'], pending['command'], pending['description'], pending['code'], update.effective_user.username, user_id)
            save_data()
            del pending_scripts[user_id]
            return await update.message.reply_text(f"✅ Скрипт `{pending['command']}` сохранен!", parse_mode='Markdown')
        if lower in ['да', 'yes', 'ещё', 'еще']:
            return await update.message.reply_text("📝 Жду продолжение кода...")
        if pending['stage'] == 'waiting_first':
            c, d, code = parse_script_text(text)
            if c: pending['command'] = c
            if d != "Без описания": pending['description'] = d
            pending['code'] = code if code else text
            pending['stage'] = 'more'
        else:
            pending['code'] += '\n' + text
        await update.message.reply_text("✅ Код получен. Напишите `готово` для сохранения или отправьте еще часть.", parse_mode='Markdown')
        return True
    return False

async def handle_document_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    uid = update.effective_user.id
    if not doc.file_name.endswith('.txt'): return False
    if uid not in pending_scripts and uid not in editing_scripts: return False
    try:
        f = await context.bot.get_file(doc.file_id)
        content = (await f.download_as_bytearray()).decode('utf-8')
        cmd, desc, code = parse_script_text(content)
        if not code.strip(): code = content
        chat_id = str(update.effective_chat.id)
        if uid in pending_scripts:
            pending = pending_scripts.pop(uid)
            final_cmd = cmd or pending.get('command')
            if not final_cmd: return await update.message.reply_text("❌ Не указана команда!")
            save_script_to_db(chat_id, final_cmd, desc, code, update.effective_user.username, uid)
            save_data()
            await update.message.reply_text(f"✅ Скрипт `{final_cmd}` загружен из файла!", parse_mode='Markdown')
            return True
        if uid in editing_scripts:
            editing = editing_scripts.pop(uid)
            sinfo = get_script_from_db(chat_id, editing['command'])
            save_script_to_db(chat_id, editing['command'], desc, code, sinfo['author'], sinfo['author_id'])
            save_data()
            await update.message.reply_text(f"✅ Скрипт `{editing['command']}` обновлен из файла!", parse_mode='Markdown')
            return True
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка файла: {e}")
        return True
    return False

# ---------- СОЗДАНИЕ МИНИ-БОТА ЧЕРЕЗ ИИ («ПОМОЩЬ БОТА») ----------

async def cb_aicreate_start(query, context):
    uid = query.from_user.id
    chat_id = str(query.message.chat.id)
    pending_scripts.pop(uid, None)   # FIX: отменяем ручную загрузку /addscript
    editing_scripts.pop(uid, None)   # FIX: отменяем редактирование
    ai_creation[uid] = {'chat_id': chat_id, 'stage': 'name', 'name': None}
    await query.message.reply_text(
        "🤖 *Помощь бота*\n\n"
        "*Шаг 1/2:* напишите *название* мини-бота (латиницей, без пробелов).\n"
        "Пример: `weather`, `jokes`, `calc`\n\n"
        "⚠️ `/cancel` - отменить", parse_mode='Markdown')

async def handle_ai_creation(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    uid = update.effective_user.id
    st = ai_creation[uid]

    if st['stage'] == 'name':
        name = re.sub(r'[^a-z0-9_]', '', text.lower().strip().replace(' ', '_').lstrip('/'))
        if not name or len(name) > 24:
            return await update.message.reply_text("❌ Название должно быть латиницей, без пробелов (до 24 символов). Попробуйте ещё раз.")
        if name in RESERVED_COMMANDS:
            return await update.message.reply_text(f"❌ Имя `{name}` занято системной командой. Выберите другое.", parse_mode='Markdown')
        if get_script_from_db(st['chat_id'], '/' + name):
            return await update.message.reply_text(f"❌ Скрипт `/{name}` уже существует в этом чате. Выберите другое имя.", parse_mode='Markdown')
        st['name'] = name
        st['stage'] = 'prompt'
        return await update.message.reply_text(
            f"*Шаг 2/2:* опишите своими словами, что должен уметь мини-бот `/{name}`.\n"
            f"Чем подробнее промт — тем лучше результат.\n\n"
            f"Пример: _Бот должен брать город из аргументов команды, запрашивать погоду через open-meteo.com и отвечать температурой и описанием._",
            parse_mode='Markdown')

    if st['stage'] == 'prompt':
        wait = await update.message.reply_text("🤖 Создаю скрипт... Обычно это занимает до минуты. Не прерывайте.")
        sys_prompt = AI_SCRIPT_SYSTEM.format(command=st['name'], prompt=text)
        msgs = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": "Сгенерируй скрипт сейчас. Верни ТОЛЬКО блок в указанном формате, без пояснений."}
        ]
        ai_comment = ''
        code = ''
        desc = 'Без описания'
        ok_code = False
        for attempt in range(3):
            res = await asyncio.to_thread(ask_ai, msgs, 16384)
            if not res['ok']:
                st['stage'] = 'prompt'
                out = (res['notice'] + "\n\n" if res['notice'] else "") + res['text'] + "\n\nПопробуйте отправить промт ещё раз или `/cancel`."
                try: await context.bot.edit_message_text(chat_id=wait.chat.id, message_id=wait.message_id, text=out[:4096])
                except Exception: await update.message.reply_text(out[:4096])
                return
            raw = clean_fences(res['text'])
            ai_comment = extract_ai_comment(res['text'])
            cmd_parsed, desc, code = parse_script_text(raw)
            if not code.strip(): code = raw
            err_text = None
            if 'async def execute' not in code:
                err_text = "нет точки входа async def execute"
            else:
                try:
                    compile(code, '<ai_script>', 'exec')
                except SyntaxError as e:
                    err_text = str(e)
            if not err_text:
                ok_code = True
                break
            if attempt == 2:
                st['stage'] = 'prompt'
                return await update.message.reply_text(f"❌ В сгенерированном коде ошибка синтаксиса: `{err_text}`\nОтправьте промт ещё раз.", parse_mode='Markdown')
            try: await context.bot.edit_message_text(chat_id=wait.chat.id, message_id=wait.message_id, text=f"🤖 Попытка {attempt+1}: код пришёл с ошибкой ({err_text[:100]}). Прошу ИИ исправить...")
            except Exception: pass
            msgs = msgs + [
                {"role": "assistant", "content": res['text'][:15000]},
                {"role": "user", "content": f"Вернутый тобой код содержит синтаксическую ошибку: {err_text}. Скорее всего код был ОБРЕЗАН на середине или содержит опечатку. Верни ИСПРАВЛЕННЫЙ код ЦЕЛИКОМ, в том же формате (###COMMENT, затем ###CODE), без обрезки."}
            ]
        if not ok_code:
            return

        command = '/' + st['name']
        author = (update.effective_user.username or 'user') + ' (через ИИ)'
        save_script_to_db(st['chat_id'], command, desc if desc != "Без описания" else f"Мини-бот ИИ: {st['name']}",
                          code, author, uid, ai_comment=ai_comment)
        save_data()
        del ai_creation[uid]
        out = (res['notice'] + "\n\n" if res['notice'] else "") + \
              f"✅ Скрипт `{command}` готов и уже добавлен в список загруженных (`/listscripts`)!\nЗапуск: `{command}`" + (f"\n🤖 ИИ: {ai_comment}" if ai_comment else ""),
        out = out if isinstance(out, str) else out[0]
        try: await context.bot.edit_message_text(chat_id=wait.chat.id, message_id=wait.message_id, text=out[:4096], parse_mode='Markdown')
        except Exception:
            try: await context.bot.edit_message_text(chat_id=wait.chat.id, message_id=wait.message_id, text=out[:4096])
            except Exception: await update.message.reply_text(out[:4096])

# ---------- ПОЧИНКА СКРИПТА ЧЕРЕЗ ИИ ----------

async def handle_ai_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    uid = update.effective_user.id
    st = ai_edit[uid]
    wait = await update.message.reply_text("🤖 Анализирую и исправляю скрипт... Обычно до минуты. Не прерывайте.")
    sinfo = get_script_from_db(st['chat_id'], st['command'])
    if not sinfo:
        del ai_edit[uid]
        return await update.message.reply_text(f"❌ Скрипт `{st['command']}` не найден.", parse_mode='Markdown')
    sys_prompt = AI_FIX_SYSTEM.format(code=sinfo['code'], prev_comment=sinfo.get('ai_comment') or 'нет', prompt=text)
    msgs = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "Верни исправленный скрипт сейчас. Только блок в указанном формате."}
    ]
    ai_comment = ''
    code = ''
    desc = 'Без описания'
    ok_code = False
    for attempt in range(3):
        res = await asyncio.to_thread(ask_ai, msgs, 16384)
        if not res['ok']:
            out = (res['notice'] + "\n\n" if res['notice'] else "") + res['text'] + "\n\nОтправьте промт с правками ещё раз или `/cancel`."
            try: await context.bot.edit_message_text(chat_id=wait.chat.id, message_id=wait.message_id, text=out[:4096])
            except Exception: await update.message.reply_text(out[:4096])
            return
        raw = clean_fences(res['text'])
        ai_comment = extract_ai_comment(res['text'])
        cmd_parsed, desc, code = parse_script_text(raw)
        if not code.strip(): code = raw
        err_text = None
        if 'async def execute' not in code:
            err_text = "нет точки входа async def execute"
        else:
            try:
                compile(code, '<ai_fix>', 'exec')
            except SyntaxError as e:
                err_text = str(e)
        if not err_text:
            ok_code = True
            break
        if attempt == 2:
            return await update.message.reply_text(f"❌ Ошибка синтаксиса в исправленном коде: `{err_text}`\nОтправьте промт ещё раз.", parse_mode='Markdown')
        try: await context.bot.edit_message_text(chat_id=wait.chat.id, message_id=wait.message_id, text=f"🤖 Попытка {attempt+1}: код пришёл с ошибкой ({err_text[:100]}). Прошу ИИ исправить...")
        except Exception: pass
        msgs = msgs + [
            {"role": "assistant", "content": res['text'][:15000]},
            {"role": "user", "content": f"Вернутый тобой код содержит синтаксическую ошибку: {err_text}. Скорее всего код был ОБРЕЗАН на середине или содержит опечатку. Верни ИСПРАВЛЕННЫЙ код ЦЕЛИКОМ, в том же формате (###COMMENT, затем ###CODE), без обрезки."}
        ]
    if not ok_code:
        return
    save_script_to_db(st['chat_id'], st['command'],
                      desc if desc != "Без описания" else sinfo['description'],
                      code, (update.effective_user.username or 'user') + ' (починено ИИ)', uid, ai_comment=ai_comment)
    save_data()
    del ai_edit[uid]
    out = (res['notice'] + "\n\n" if res['notice'] else "") + f"✅ Скрипт `{st['command']}` исправлен ИИ и сохранён!\nПроверьте: `{st['command']}`" + (f"\n🤖 ИИ: {ai_comment}" if ai_comment else "")
    try: await context.bot.edit_message_text(chat_id=wait.chat.id, message_id=wait.message_id, text=out[:4096], parse_mode='Markdown')
    except Exception:
        try: await context.bot.edit_message_text(chat_id=wait.chat.id, message_id=wait.message_id, text=out[:4096])
        except Exception: await update.message.reply_text(out[:4096])

# ---------- ИСПОЛНЕНИЕ СКРИПТОВ ----------

async def execute_custom_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    if update.message is None: return
    text = update.message.text
    if not text or not text.startswith('/'): return
    parts = text.split()
    cmd = parts[0].lower().split('@')[0]
    args = parts[1:]
    script = get_script_from_db(chat_id, cmd)
    if not script:
        # Алиасы (строка '# ALIASES: /cmd1 /cmd2' в коде) и префиксы (/12top -> /12)
        best = None
        best_len = 0
        for other_cmd in get_chat_scripts(chat_id):
            if other_cmd == cmd or len(other_cmd) <= best_len:
                continue
            s2 = get_script_from_db(chat_id, other_cmd)
            if not s2:
                continue
            hit = False
            m = re.search(r'^#\s*ALIASES:\s*(.+)$', s2['code'] or '', re.MULTILINE)
            if m:
                aliases = [a if a.startswith('/') else '/' + a
                           for a in re.split(r'[,\s]+', m.group(1).lower()) if a.strip()]
                if cmd in aliases:
                    hit = True
            if not hit and cmd.startswith(other_cmd):
                hit = True
            if hit:
                best, best_len = s2, len(other_cmd)
        script = best
    if not script: return
    try:
        import builtins
        local_ns = {
            '__builtins__': builtins,
            'update': update, 'context': context, 'args': args,
            'DATA_DIR': DATA_DIR, 'DB_PATH': DB_PATH,
            'InlineKeyboardButton': InlineKeyboardButton,
            'InlineKeyboardMarkup': InlineKeyboardMarkup,
            'async_playwright': async_playwright
        }
        popular_modules = ['math', 'random', 'datetime', 're', 'json', 'os', 'sys', 'subprocess', 'requests', 'asyncio', 'aiohttp', 'time', 'sqlite3', 'playwright', 'hashlib', 'base64', 'pathlib', 'shutil']
        for mod in popular_modules:
            try: local_ns[mod] = __import__(mod)
            except: pass
        try:
            local_ns['Update'] = Update
            local_ns['ContextTypes'] = ContextTypes
            local_ns['ParseMode'] = ParseMode
        except: pass
        exec(script['code'], local_ns)
        if 'execute' in local_ns:
            res = await local_ns['execute'](update, context, args)
            if res:
                result_str = str(res)
                try: await update.message.reply_text(result_str, parse_mode='Markdown')
                except: await update.message.reply_text(result_str)
        log_execution(chat_id, user_id, cmd, True)
    except Exception as e:
        log_execution(chat_id, user_id, cmd, False, str(e))
        error_msg = str(e)
        if len(error_msg) > 500: error_msg = error_msg[:500] + "..."
        await update.message.reply_text(f"❌ Ошибка скрипта:\n`{error_msg}`", parse_mode='Markdown')

async def run_triggers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    scripts = get_chat_scripts(chat_id)
    if not scripts: return
    for cmd in scripts:
        s = get_script_from_db(chat_id, cmd)
        if 'check_triggers' not in s['code']: continue
        try:
            import builtins
            local_ns = {
                '__builtins__': builtins, 'update': update, 'context': context,
                'DATA_DIR': DATA_DIR, 'DB_PATH': DB_PATH,
                'async_playwright': async_playwright
            }
            for mod in ['math','random','datetime','re','json','os','sys','subprocess','requests','asyncio','aiohttp','time','sqlite3','playwright']:
                try: local_ns[mod] = __import__(mod)
                except: pass
            exec(s['code'], local_ns)
            if 'check_triggers' in local_ns:
                await local_ns['check_triggers'](update, context)
        except Exception as e:
            print(f"Trigger error in {cmd}: {e}")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = str(update.effective_chat.id)
    data = query.data or ""

    # --- Наши системные кнопки ---
    if data == "aicreate_start":
        await query.answer()
        await cb_aicreate_start(query, context)
        return
    if data.startswith("aiedit_start:"):
        await query.answer()
        cmd = data.split(":", 1)[1]
        uid = query.from_user.id
        pending_scripts.pop(uid, None)
        editing_scripts.pop(uid, None)
        ai_creation.pop(uid, None)
        ai_edit[uid] = {'chat_id': str(query.message.chat.id), 'command': cmd, 'stage': 'prompt'}
        await query.message.reply_text(
            f"🤖 *Починка через ИИ* скрипта `{cmd}`\n\nОтправьте промт: опишите своими словами, что нужно исправить или улучшить.\nПример: _скрипт падает, если нет аргументов — пусть отвечает подсказкой._\n\n⚠️ `/cancel` - отменить",
            parse_mode='Markdown')
        return
    # --- Кнопки из кастомных скриптов ---
    scripts = get_chat_scripts(chat_id)
    handled = False
    for cmd in scripts:
        s = get_script_from_db(chat_id, cmd)
        has_handler = ('handle_callback' in s['code'] or 'handle_somka_callbacks' in s['code'])
        if not has_handler: continue
        try:
            import builtins
            local_ns = {
                '__builtins__': builtins, 'update': update, 'context': context, 'query': query, 'callback_data': data,
                'DATA_DIR': DATA_DIR, 'DB_PATH': DB_PATH,
                'Update': Update, 'ContextTypes': ContextTypes, 'ParseMode': ParseMode,
                'InlineKeyboardButton': InlineKeyboardButton,
                'InlineKeyboardMarkup': InlineKeyboardMarkup,
                'async_playwright': async_playwright
            }
            for mod in ['math','random','datetime','re','json','os','sys','subprocess','requests','asyncio','aiohttp','time','sqlite3','playwright','hashlib','base64','pathlib','shutil']:
                try: local_ns[mod] = __import__(mod)
                except: pass
            exec(s['code'], local_ns)
            for handler_name in ['handle_callback', 'handle_somka_callbacks']:
                if handler_name in local_ns:
                    try:
                        res = await local_ns[handler_name](update, context, data)
                        if res: handled = True
                    except TypeError:
                        res = await local_ns[handler_name](update, context)
                        if res: handled = True
                    if handled: break
            if handled: break
        except Exception as e:
            logger.error(f"Callback error {cmd}: {e}")
            try:
                await query.message.reply_text(f"❌ Ошибка колбэка в скрипте `{cmd}`:\n`{str(e)[:300]}`", parse_mode='Markdown')
            except Exception:
                pass
    if not handled:
        try: await query.answer()
        except: pass

async def list_scripts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    scripts = get_chat_scripts(chat_id)
    if not scripts: return await update.message.reply_text("📭 В этом чате пока нет кастомных скриптов.")
    text = "📜 *Кастомные скрипты:*\n\n"
    for cmd, info in scripts.items():
        text += f"• `{cmd}` - {info['description']}\n"
    await update.message.reply_text(text, parse_mode='Markdown')

async def delete_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("❌ Укажите команду: `/deletescript /команда`", parse_mode='Markdown')
    cmd = context.args[0].lower()
    if not cmd.startswith('/'): cmd = '/' + cmd
    if delete_script_from_db(str(update.effective_chat.id), cmd):
        await update.message.reply_text(f"✅ Скрипт `{cmd}` удалён!", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ Скрипт `{cmd}` не найден!", parse_mode='Markdown')

# ---------- МАРШРУТИЗАЦИЯ СООБЩЕНИЙ ----------

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None: return
    uid = update.effective_user.id
    text = update.message.text or ""
    # 1) Создание/починка мини-бота через ИИ (приоритет над ручной загрузкой)
    if uid in ai_edit:
        await handle_ai_edit(update, context, text)
        return
    if uid in ai_creation:
        await handle_ai_creation(update, context, text)
        return
    # 2) Ручная загрузка/редактирование скриптов
    if await handle_script_upload(update, context): return
    # 3) Обычное поведение: триггеры скриптов
    await run_triggers(update, context)
    if text.startswith('/'):
        await execute_custom_script(update, context)

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None: return
    if await handle_document_upload(update, context): return
    await run_triggers(update, context)

async def error_handler(update, context):
    logger.error(f"❌ Ошибка при обработке update: {context.error}")
    try:
        if update is not None and update.effective_message:
            await update.effective_message.reply_text(f"⚠️ Внутренняя ошибка бота:\n{context.error}")
    except Exception:
        pass

LOCK_HANDLE = None

def acquire_single_instance_lock():
    """Файл-блокировка в приватной папке Termux (на /storage/emulated/0 flock не работает)."""
    global LOCK_HANDLE
    import fcntl
    lock_path = Path.home() / ".tgbot_single_instance.lock"
    LOCK_HANDLE = open(lock_path, "w")
    try:
        fcntl.flock(LOCK_HANDLE, fcntl.LOCK_EX | fcntl.LOCK_NB)
        LOCK_HANDLE.write(str(os.getpid()))
        LOCK_HANDLE.flush()
        return True
    except BlockingIOError:
        return False  # действительно держит другой живой процесс
    except OSError as e:
        print(f"⚠️ Не удалось поставить блокировку ({e}) — запускаюсь без неё.")
        return True

# ==================== БРАУЗЕР-АГЕНТ (QWEN ЧЕРЕЗ БРАУЗЕР, ТОЛЬКО РАЗРАБ) ====================
_qwen_profile = Path(__file__).parent / "qwen_profile"
_PW = None
_CTX = None
_PAGE = None
_AGENT_LOCK = asyncio.Lock()

def _has_playwright():
    try:
        import playwright.async_api
        return True
    except Exception:
        return False

HAS_PW = _has_playwright()

AGENT_SEL = 'a,button,textarea,input,[role="button"],[contenteditable="true"]'
AGENT_JS = """() => {
  const nodes = [...document.querySelectorAll('a,button,textarea,input,[role="button"],[contenteditable="true"]')];
  const out = [];
  nodes.forEach((n, i) => {
    const r = n.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    const txt = (n.getAttribute('aria-label') || n.getAttribute('placeholder') || n.innerText || n.value || '').trim().replace(/\s+/g,' ').slice(0,80);
    out.push({i, tag: n.tagName.toLowerCase(), text: txt});
  });
  return out.slice(0, 60);
}"""

AGENT_SYS = (
    "Ты — браузер-агент на сайте chat.qwen.ai. Цель: отправить вопрос пользователя в чат Qwen и вернуть его полный ответ.\n"
    "Каждый ход ты получаешь состояние страницы (url, элементы с индексами, хвост текста страницы).\n"
    "Отвечай ОДНИМ JSON-объектом действия без пояснений:\n"
    '{"action":"click","index":N} | {"action":"type","index":N,"text":"..."} | '
    '{"action":"press","key":"Enter"} | {"action":"wait","seconds":5} | '
    '{"action":"scroll","dir":"down"} | {"action":"finish","answer":"полный ответ Qwen"}\n'
    "Тактика: найди поле ввода чата (textarea/contenteditable) -> type текста вопроса -> press Enter -> "
    "wait 5-10 сек -> читай page_tail: если ответ ещё генерируется (видна кнопка Stop) — wait ещё; "
    "когда готов — finish с ПОЛНЫМ текстом ответа из page_tail."
)

async def agent_ensure():
    global _PW, _CTX, _PAGE
    if not HAS_PW:
        raise RuntimeError("Playwright не установлен. Добавь playwright в requirements.txt и перезапусти деплой.")
    if _CTX: return
    from playwright.async_api import async_playwright
    _PW = await async_playwright().start()
    _CTX = await _PW.chromium.launch_persistent_context(str(_qwen_profile), headless=True, viewport={"width": 1280, "height": 900}, locale="ru-RU")
    _PAGE = _CTX.pages[0] if _CTX.pages else await _CTX.new_page()

async def agent_logged_in():
    try:
        if "qwen" not in _PAGE.url.lower():
            await _PAGE.goto("https://chat.qwen.ai", wait_until="domcontentloaded", timeout=30000)
            await _PAGE.wait_for_timeout(3000)
        sel = await _PAGE.query_selector('textarea, [contenteditable="true"]')
        return sel is not None
    except Exception:
        return False

async def agent_observe():
    els = await _PAGE.evaluate(AGENT_JS)
    tail = await _PAGE.evaluate("() => (document.body.innerText || '').slice(-1500)")
    shot = await _PAGE.screenshot()
    return els, tail, shot

async def agent_act(a):
    kind = a.get("action")
    if kind == "goto":
        await _PAGE.goto(a["url"], wait_until="domcontentloaded", timeout=30000)
    elif kind == "click":
        nodes = await _PAGE.query_selector_all(AGENT_SEL)
        n = nodes[int(a["index"])]
        await n.scroll_into_view_if_needed()
        await n.click()
    elif kind == "type":
        nodes = await _PAGE.query_selector_all(AGENT_SEL)
        n = nodes[int(a["index"])]
        await n.click()
        await _PAGE.keyboard.type(a["text"], delay=10)
    elif kind == "press":
        await _PAGE.keyboard.press(a.get("key", "Enter"))
    elif kind == "wait":
        await _PAGE.wait_for_timeout(min(30, int(a.get("seconds", 5))) * 1000)
    elif kind == "scroll":
        await _PAGE.mouse.wheel(0, 800 if a.get("dir") == "down" else -800)
    await _PAGE.wait_for_timeout(1200)

def agent_parse_json(content):
    import re, json
    m = re.search(r'\{.*\}', content, re.DOTALL)
    if not m: return None
    try: return json.loads(m.group(0))
    except Exception: return None

async def agent_task(prompt):
    async with _AGENT_LOCK:
        await agent_ensure()
        if not await agent_logged_in():
            return {"ok": False, "error": "not_logged_in"}
        try:
            for b in await _PAGE.query_selector_all('button, a'):
                t = ((await b.inner_text()) or "").strip().lower()
                if t in ("new chat", "новый чат"):
                    await b.click(); await _PAGE.wait_for_timeout(1500); break
        except Exception:
            pass
        idx, key = pick_key(active_key_index)
        if not key:
            return {"ok": False, "error": "no groq keys"}
        msgs = [{"role": "system", "content": AGENT_SYS},
                {"role": "user", "content": f"Задача: отправь Qwen этот вопрос и верни его полный ответ:\n{prompt}"}]
        for _ in range(14):
            els, tail, _ = await agent_observe()
            msgs.append({"role": "user", "content": "Состояние страницы:\n" +
                         json.dumps({"url": _PAGE.url, "elements": els, "page_tail": tail}, ensure_ascii=False)})
            try:
                r = requests.post(GROQ_URL, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                  json={"model": "openai/gpt-oss-120b", "messages": msgs, "max_completion_tokens": 1024}, timeout=60)
                content = r.json()["choices"][0]["message"]["content"]
            except Exception as e:
                return {"ok": False, "error": f"groq error: {e}"}
            msgs.append({"role": "assistant", "content": content})
            action = agent_parse_json(content)
            if not action:
                continue
            if action.get("action") == "finish":
                return {"ok": True, "answer": action.get("answer", "")}
            try:
                await agent_act(action)
            except Exception as e:
                msgs.append({"role": "user", "content": f"Ошибка действия: {e}"})
        return {"ok": False, "error": "step limit exceeded"}

# ---------- Команды разработчика для Qwen-агента ----------

async def _edit(msg, text):
    try: await msg.edit_text(text[:4000])
    except Exception:
        try: await msg.reply_text(text[:4000])
        except Exception: pass

async def dev_qwen(update, context):
    if not is_dev(update.effective_user.id):
        return await update.message.reply_text("⛳ Команда доступна только разработчику.")
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return await update.message.reply_text("Использование: `/qwen <вопрос>`", parse_mode='Markdown')
    if not HAS_PW:
        return await update.message.reply_text("❌ Playwright не установлен. Добавь `playwright` в requirements.txt на bothost.ru и перезапусти деплой.")
    wait = await update.message.reply_text("🌐 Браузер-агент спрашивает Qwen... это может занять до 2 минут.")
    try:
        res = await agent_task(parts[1])
    except Exception as e:
        await _edit(wait, f"❌ Ошибка агента: {str(e)[:300]}")
        return
    if not res.get("ok"):
        err = res.get("error", "неизвестная ошибка")
        hint = "\nСначала залогинься: /qurl https://chat.qwen.ai → /qshot → /qclick N, /qtype N текст, /qenter." if err == "not_logged_in" else ""
        await _edit(wait, f"❌ Агент: {err}{hint}")
        return
    ans = res.get("answer", "")[:4000]
    try: await context.bot.edit_message_text(chat_id=wait.chat.id, message_id=wait.message_id, text=f"🤖 *Qwen отвечает:*\n{ans}", parse_mode='Markdown')
    except Exception:
        try: await context.bot.edit_message_text(chat_id=wait.chat.id, message_id=wait.message_id, text=f"🤖 Qwen отвечает:\n{ans}")
        except Exception: await update.message.reply_text(f"🤖 Qwen отвечает:\n{ans}")

async def dev_qshot(update, context):
    if not is_dev(update.effective_user.id):
        return await update.message.reply_text("⛳ Команда доступна только разработчику.")
    if not HAS_PW:
        return await update.message.reply_text("❌ Playwright не установлен.")
    try:
        await agent_ensure()
        els, tail, shot = await agent_observe()
    except Exception as e:
        return await update.message.reply_text(f"❌ {str(e)[:200]}")
    import base64, io
    img = base64.b64decode(base64.b64encode(shot).decode())
    lines = [f"{e['i']}: <{e['tag']}> {e['text'][:50]}" for e in els[:40]]
    caption = f"URL: {_PAGE.url}\nЭлементы:\n" + "\n".join(lines)
    await update.message.reply_photo(photo=io.BytesIO(img), filename="page.png", caption=caption[:1000])

async def dev_qclick(update, context):
    if not is_dev(update.effective_user.id):
        return await update.message.reply_text("⛳ Команда доступна только разработчику.")
    if not HAS_PW:
        return await update.message.reply_text("❌ Playwright не установлен.")
    if not context.args: return await update.message.reply_text("Использование: `/qclick N`", parse_mode='Markdown')
    try:
        await agent_ensure()
        await agent_act({"action": "click", "index": int(context.args[0])})
        await update.message.reply_text("✅ Клик выполнен. /qshot для проверки.")
    except Exception as e:
        await update.message.reply_text(f"❌ {str(e)[:200]}")

async def dev_qtype(update, context):
    if not is_dev(update.effective_user.id):
        return await update.message.reply_text("⛳ Команда доступна только разработчику.")
    if not HAS_PW:
        return await update.message.reply_text("❌ Playwright не установлен.")
    if len(context.args) < 2: return await update.message.reply_text("Использование: `/qtype N текст`", parse_mode='Markdown')
    text = update.message.text.split(maxsplit=2)[2]
    try:
        await agent_ensure()
        await agent_act({"action": "type", "index": int(context.args[0]), "text": text})
        await update.message.reply_text("✅ Текст введён.")
    except Exception as e:
        await update.message.reply_text(f"❌ {str(e)[:200]}")

async def dev_qenter(update, context):
    if not is_dev(update.effective_user.id):
        return await update.message.reply_text("⛳ Команда доступна только разработчику.")
    if not HAS_PW:
        return await update.message.reply_text("❌ Playwright не установлен.")
    key = context.args[0] if context.args else "Enter"
    try:
        await agent_ensure()
        await agent_act({"action": "press", "key": key})
        await update.message.reply_text("✅ Нажато.")
    except Exception as e:
        await update.message.reply_text(f"❌ {str(e)[:200]}")

async def dev_qurl(update, context):
    if not is_dev(update.effective_user.id):
        return await update.message.reply_text("⛳ Команда доступна только разработчику.")
    if not HAS_PW:
        return await update.message.reply_text("❌ Playwright не установлен.")
    if not context.args: return await update.message.reply_text("Использование: `/qurl https://...`", parse_mode='Markdown')
    try:
        await agent_ensure()
        await agent_act({"action": "goto", "url": context.args[0]})
        await update.message.reply_text(f"✅ Открыто: {_PAGE.url}")
    except Exception as e:
        await update.message.reply_text(f"❌ {str(e)[:200]}")

async def dev_qstatus(update, context):
    if not is_dev(update.effective_user.id):
        return await update.message.reply_text("⛳ Команда доступна только разработчику.")
    if not HAS_PW:
        return await update.message.reply_text("❌ Playwright не установлен. Добавь `playwright` в requirements.txt на bothost.ru.")
    try:
        await agent_ensure()
        logged = await agent_logged_in()
        st = "✅ залогинен" if logged else "❌ не залогинен"
        await update.message.reply_text(f"🖥️ Браузер жив. Логин Qwen: {st}\nURL: {_PAGE.url}")
    except Exception as e:
        await update.message.reply_text(f"❌ {str(e)[:200]}")

def main():
    if not acquire_single_instance_lock():
        print("❌ Бот УЖЕ запущен в другом процессе! Выходим, чтобы не перехватывать обновления.")
        print("   Если уверен, что никто не запущен: pkill -f ai.py")
        sys.exit(1)
    from telegram.request import HTTPXRequest
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=10.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=10.0,
    )
    application = Application.builder().token(BOT_TOKEN).request(request).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("addkey", dev_addkey))
    application.add_handler(CommandHandler("keys", dev_keys))
    application.add_handler(CommandHandler("usekey", dev_usekey))
    application.add_handler(CommandHandler("delkey", dev_delkey))
    application.add_handler(CommandHandler("qwen", dev_qwen))
    application.add_handler(CommandHandler("qshot", dev_qshot))
    application.add_handler(CommandHandler("qclick", dev_qclick))
    application.add_handler(CommandHandler("qtype", dev_qtype))
    application.add_handler(CommandHandler("qenter", dev_qenter))
    application.add_handler(CommandHandler("qurl", dev_qurl))
    application.add_handler(CommandHandler("qstatus", dev_qstatus))
    application.add_handler(CommandHandler("addscript", add_script))
    application.add_handler(CommandHandler("listscripts", list_scripts))
    application.add_handler(CommandHandler("viewscript", view_script))
    application.add_handler(CommandHandler("editscript", edit_script))
    application.add_handler(CommandHandler("deletescript", delete_script))
    application.add_handler(CommandHandler("cancel", cancel_action))

    application.add_handler(MessageHandler(filters.Document.TEXT, document_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_handler(MessageHandler(filters.COMMAND, execute_custom_script))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_error_handler(error_handler)

    logger.info("🤖 Бот запущен!")
    application.run_polling()

if __name__ == "__main__":
    main()
