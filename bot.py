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
from datetime import datetime
from pathlib import Path

# ==================== БЛОК БЕЗОПАСНОЙ ЗАГРУЗКИ (SYSTEM BOOT) ====================

def force_install(package_name, import_name=None):
    """Устанавливает пакет через pip внутри скрипта"""
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
    """Установка браузеров Playwright (Chromium)"""
    if not HAS_PLAYWRIGHT: return
    print("🔄 [SYSTEM] Проверка браузеров Chromium...")
    try:
        # Пытаемся установить браузер. Если не выйдет - бот запустится, но скрипты с браузером упадут.
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
    except Exception as e:
        print(f"⚠️ [SYSTEM] Ошибка установки браузера (игнорируем): {e}")

# --- ЗАПУСК ПРОВЕРОК ---
print("🚀 [BOOT] Инициализация системы...")

# 1. Устанавливаем Python библиотеки
force_install("aiosqlite")
HAS_PLAYWRIGHT = force_install("playwright", "playwright.async_api")

# 2. Докачиваем браузеры (если Playwright встал)
if HAS_PLAYWRIGHT:
    install_browsers()

# 3. Импортируем Playwright безопасно
async_playwright = None
try:
    if HAS_PLAYWRIGHT:
        from playwright.async_api import async_playwright
except ImportError:
    pass

print("✅ [BOOT] Среда готова. Запуск Telegram бота...")

# ==================== ОСНОВНОЙ КОД БОТА ====================

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ParseMode

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = ""

# Путь к папке data
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"

# ==================== СИСТЕМА ХРАНЕНИЯ ДАННЫХ (БД) ====================

def init_database():
    """Инициализация базы данных"""
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
    
    # Загрузка скриптов
    scripts_registry = {}
    cursor.execute("SELECT chat_id, command, description, code, author, author_id, created_at, updated_at FROM scripts")
    for row in cursor.fetchall():
        chat_id, command, description, code, author, author_id, created_at, updated_at = row
        if chat_id not in scripts_registry:
            scripts_registry[chat_id] = {}
        scripts_registry[chat_id][command] = {
            'description': description,
            'code': code,
            'author': author,
            'author_id': author_id,
            'created': created_at,
            'updated': updated_at
        }
    
    # Загрузка пользователей
    users_data = {}
    cursor.execute("SELECT user_id, username, first_name, data FROM users")
    for row in cursor.fetchall():
        user_id, username, first_name, data = row
        try:
            users_data[user_id] = {
                'username': username,
                'first_name': first_name,
                'data': json.loads(data) if data else {}
            }
        except: pass
    
    # Загрузка состояния
    bot_state = {}
    cursor.execute("SELECT key, value FROM bot_state")
    for row in cursor.fetchall():
        try: bot_state[row[0]] = json.loads(row[1])
        except: bot_state[row[0]] = row[1]
    
    conn.close()
    logger.info(f"📦 Загружено скриптов: {sum(len(s) for s in scripts_registry.values())}")

def save_data():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        for chat_id, scripts in scripts_registry.items():
            for command, info in scripts.items():
                cursor.execute('''
                    INSERT OR REPLACE INTO scripts 
                    (chat_id, command, description, code, author, author_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (
                    str(chat_id), command, info.get('description', 'Без описания'),
                    info['code'], info.get('author'), info.get('author_id')
                ))
        for user_id, info in users_data.items():
            cursor.execute('''
                INSERT OR REPLACE INTO users (user_id, username, first_name, data)
                VALUES (?, ?, ?, ?)
            ''', (
                user_id, info.get('username'), info.get('first_name'),
                json.dumps(info.get('data', {}), ensure_ascii=False)
            ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения: {e}")

def save_script_to_db(chat_id, command, description, code, author, author_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO scripts (chat_id, command, description, code, author, author_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ''', (str(chat_id), command, description, code, author, author_id))
    conn.commit()
    conn.close()
    
    if chat_id not in scripts_registry:
        scripts_registry[chat_id] = {}
    scripts_registry[chat_id][command] = {
        'description': description,
        'code': code,
        'author': author,
        'author_id': author_id,
        'updated': datetime.now().isoformat()
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
    cursor.execute("SELECT description, code, author, author_id, created_at, updated_at FROM scripts WHERE chat_id = ? AND command = ?", (str(chat_id), command))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {'description': row[0], 'code': row[1], 'author': row[2], 'author_id': row[3], 'created': row[4], 'updated': row[5]}
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

# ==================== ХЕНДЛЕРЫ ТЕЛЕГРАМ ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветственное сообщение"""
    await update.message.reply_text(
        f"🤖 *Привет! Я бот с кастомными скриптами!*\n\n"
        f"📌 *Доступные команды:*\n"
        f"`/addscript` - Добавить новый скрипт\n"
        f"`/listscripts` - Список скриптов чата\n"
        f"`/viewscript <команда>` - Посмотреть код\n"
        f"`/editscript <команда>` - Редактировать скрипт\n"
        f"`/deletescript <команда>` - Удалить скрипт\n"
        f"`/cancel` - Отменить текущее действие\n"
        f"`/help` - Помощь\n\n"
        f"💡 Вы можете создавать свои команды!",
        parse_mode='Markdown'
    )

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cancelled = False
    if uid in pending_scripts:
        del pending_scripts[uid]
        cancelled = True
    if uid in editing_scripts:
        del editing_scripts[uid]
        cancelled = True
    
    if cancelled:
        await update.message.reply_text("❌ Действие отменено.")
    else:
        await update.message.reply_text("ℹ️ Нет активных действий.")

async def add_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    pending_scripts[user_id] = {
        'chat_id': chat_id, 'code': '', 'command': None, 'description': 'Без описания', 'stage': 'waiting_first'
    }
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
        "⚠️ `/cancel` - отменить",
        parse_mode='Markdown'
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
    await update.message.reply_text(f"✏️ *Редактирование* `{command}`. Отправьте новый код.", parse_mode='Markdown')

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
        # ЛОГИКА РЕДАКТИРОВАНИЯ
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
        await update.message.reply_text(f"✅ Код получен. Напишите `готово` для сохранения или отправьте еще часть.", parse_mode='Markdown')
        return True

    if user_id in pending_scripts:
        # ЛОГИКА ДОБАВЛЕНИЯ
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
        await update.message.reply_text(f"✅ Код получен. Напишите `готово` для сохранения или отправьте еще часть.", parse_mode='Markdown')
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

# --- ИСПОЛНЕНИЕ СКРИПТОВ ---

async def execute_custom_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    text = update.message.text
    if not text.startswith('/'): return
    
    parts = text.split()
    cmd = parts[0].lower().split('@')[0]
    args = parts[1:]
    
    script = get_script_from_db(chat_id, cmd)
    if not script: return
    
    try:
        import builtins
        local_ns = {
            '__builtins__': builtins,
            'update': update, 'context': context, 'args': args,
            'DATA_DIR': DATA_DIR, 'DB_PATH': DB_PATH,
            'InlineKeyboardButton': InlineKeyboardButton,
            'InlineKeyboardMarkup': InlineKeyboardMarkup,
            # Пробрасываем библиотеки
            'async_playwright': async_playwright
        }
        
        # Добавляем стандартные модули
        popular_modules = ['math', 'random', 'datetime', 're', 'json', 'os', 'sys', 'subprocess', 'requests', 'asyncio', 'aiohttp', 'time', 'sqlite3', 'playwright', 'hashlib', 'base64', 'pathlib', 'shutil']
        for mod in popular_modules:
            try: local_ns[mod] = __import__(mod)
            except: pass
        
        # Telegram классы
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
    """Проверка триггеров (функция check_triggers в скриптах)"""
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
            # Импортируем модули
            for mod in ['math','random','datetime','re','json','os','sys','subprocess','requests','asyncio','aiohttp','time','sqlite3','playwright']:
                try: local_ns[mod] = __import__(mod)
                except: pass
                
            exec(s['code'], local_ns)
            if 'check_triggers' in local_ns:
                await local_ns['check_triggers'](update, context)
        except Exception as e:
            print(f"Trigger error in {cmd}: {e}")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик инлайн кнопок из скриптов"""
    query = update.callback_query
    chat_id = str(update.effective_chat.id)
    data = query.data
    scripts = get_chat_scripts(chat_id)
    handled = False
    
    for cmd in scripts:
        s = get_script_from_db(chat_id, cmd)
        # Проверяем наличие любого из обработчиков
        has_handler = (
            'handle_callback' in s['code'] or 
            'handle_somka_callbacks' in s['code']
        )
        if not has_handler: continue
        
        try:
            import builtins
            local_ns = {
                '__builtins__': builtins, 'update': update, 'context': context, 'query': query, 'callback_data': data,
                'InlineKeyboardButton': InlineKeyboardButton, 'InlineKeyboardMarkup': InlineKeyboardMarkup,
                'async_playwright': async_playwright
            }
            # Стандартные модули
            for mod in ['math','random','datetime','re','json','os','sys','asyncio','time','sqlite3','playwright']:
                try: local_ns[mod] = __import__(mod)
                except: pass
            
            exec(s['code'], local_ns)
            
            # Пробуем разные имена функций
            for handler_name in ['handle_callback', 'handle_somka_callbacks']:
                if handler_name in local_ns:
                    try:
                        res = await local_ns[handler_name](update, context, data)
                        if res: handled = True
                    except TypeError:
                        # Если функция не принимает callback_data
                        res = await local_ns[handler_name](update, context)
                        if res: handled = True
                    if handled: break
            
            if handled: break
        except Exception as e:
            logger.error(f"Callback error {cmd}: {e}")
    
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
        if str(update.effective_chat.id) in scripts_registry and cmd in scripts_registry[str(update.effective_chat.id)]:
            del scripts_registry[str(update.effective_chat.id)][cmd]
        await update.message.reply_text(f"✅ Скрипт `{cmd}` удалён!", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ Скрипт `{cmd}` не найден!", parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ℹ️ Используй `/addscript` для добавления кода.", parse_mode='Markdown')

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await handle_script_upload(update, context): return
    await run_triggers(update, context)
    if update.message.text and update.message.text.startswith('/'):
        await execute_custom_script(update, context)

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await handle_document_upload(update, context): return
    await run_triggers(update, context)

def main():
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addscript", add_script))
    application.add_handler(CommandHandler("listscripts", list_scripts))
    application.add_handler(CommandHandler("viewscript", view_script))
    application.add_handler(CommandHandler("editscript", edit_script))
    application.add_handler(CommandHandler("deletescript", delete_script))
    application.add_handler(CommandHandler("cancel", cancel_action))
    application.add_handler(CommandHandler("help", help_command))
    
    application.add_handler(MessageHandler(filters.Document.TEXT, document_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_handler(MessageHandler(filters.COMMAND, execute_custom_script))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    
    logger.info("🤖 Бот запущен!")
    application.run_polling()

if __name__ == "__main__":
    main()
