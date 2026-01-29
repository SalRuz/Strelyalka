import os
import sqlite3
import asyncio
import json
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from datetime import datetime
from pathlib import Path

# Конфигурация
BOT_TOKEN = "8512207770:AAEKLtYEph7gleybGhF2lc7Gwq82Kj1yedM"
DEVELOPER_ID = 1170970828  # ID разработчика для /dbinfo

# === ИНИЦИАЛИЗАЦИЯ ДИРЕКТОРИИ И БД ===

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"

def init_database():
    """Инициализация базы данных"""
    conn = sqlite3.connect(str(DB_PATH))
    
    # Таблица скриптов (без author)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS scripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            command TEXT NOT NULL,
            description TEXT DEFAULT 'Без описания',
            code TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, command)
        )
    ''')
    
    # Таблица пользователей
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица логов
    conn.execute('''
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
    
    # Таблица настроек чатов (права доступа)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id TEXT PRIMARY KEY,
            creator_id INTEGER,
            access_mode TEXT DEFAULT 'creator',
            allowed_users TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    print(f"✅ База данных инициализирована: {DB_PATH.absolute()}")

def get_db_connection():
    """Получить соединение с БД"""
    return sqlite3.connect(str(DB_PATH))

def load_scripts_registry():
    """Загрузка реестра скриптов из БД"""
    global scripts_registry
    
    init_database()
    
    registry = {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, command, description, code, created_at, updated_at FROM scripts")
        for row in cursor.fetchall():
            chat_id, command, description, code, created_at, updated_at = row
            if chat_id not in registry:
                registry[chat_id] = {}
            registry[chat_id][command] = {
                'description': description,
                'code': code,
                'created': created_at,
                'updated': updated_at
            }
        conn.close()
        total_scripts = sum(len(v) for v in registry.values())
        print(f"📚 Загружено скриптов: {total_scripts}")
    except Exception as e:
        print(f"❌ Ошибка загрузки скриптов: {e}")
    
    return registry

# === ФУНКЦИИ ДЛЯ РАБОТЫ С ПРАВАМИ ДОСТУПА ===

def get_chat_settings(chat_id):
    """Получить настройки чата"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT creator_id, access_mode, allowed_users FROM chat_settings WHERE chat_id = ?", (str(chat_id),))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return {
            'creator_id': row[0],
            'access_mode': row[1],
            'allowed_users': json.loads(row[2]) if row[2] else []
        }
    return None

def save_chat_settings(chat_id, creator_id, access_mode='creator', allowed_users=None):
    """Сохранить настройки чата"""
    if allowed_users is None:
        allowed_users = []
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO chat_settings (chat_id, creator_id, access_mode, allowed_users, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
    ''', (str(chat_id), creator_id, access_mode, json.dumps(allowed_users)))
    conn.commit()
    conn.close()

def add_allowed_user(chat_id, user_id):
    """Добавить пользователя в список разрешённых"""
    settings = get_chat_settings(chat_id)
    if settings:
        allowed = settings['allowed_users']
        if user_id not in allowed:
            allowed.append(user_id)
            save_chat_settings(chat_id, settings['creator_id'], settings['access_mode'], allowed)
            return True
    return False

def remove_allowed_user(chat_id, user_id):
    """Удалить пользователя из списка разрешённых"""
    settings = get_chat_settings(chat_id)
    if settings:
        allowed = settings['allowed_users']
        if user_id in allowed:
            allowed.remove(user_id)
            save_chat_settings(chat_id, settings['creator_id'], settings['access_mode'], allowed)
            return True
    return False

async def check_script_permission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверить, имеет ли пользователь право на создание/редактирование скриптов"""
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    chat_type = update.effective_chat.type
    
    # В личных сообщениях - всегда разрешено
    if chat_type == 'private':
        return True
    
    # Разработчик имеет полный доступ
    if user_id == DEVELOPER_ID:
        return True
    
    # Получаем настройки чата
    settings = get_chat_settings(chat_id)
    
    # Если настроек нет - создаём, определяя создателя чата
    if not settings:
        try:
            chat_admins = await context.bot.get_chat_administrators(chat_id)
            creator_id = None
            for admin in chat_admins:
                if admin.status == 'creator':
                    creator_id = admin.user.id
                    break
            
            if creator_id:
                save_chat_settings(chat_id, creator_id, 'creator', [])
                settings = {'creator_id': creator_id, 'access_mode': 'creator', 'allowed_users': []}
            else:
                save_chat_settings(chat_id, user_id, 'admins', [])
                settings = {'creator_id': user_id, 'access_mode': 'admins', 'allowed_users': []}
        except Exception as e:
            print(f"Ошибка получения админов: {e}")
            return False
    
    access_mode = settings['access_mode']
    creator_id = settings['creator_id']
    allowed_users = settings['allowed_users']
    
    # Создатель всегда имеет доступ
    if user_id == creator_id:
        return True
    
    # Проверяем режим доступа
    if access_mode == 'creator':
        return False
    
    elif access_mode == 'admins':
        try:
            chat_admins = await context.bot.get_chat_administrators(chat_id)
            admin_ids = [admin.user.id for admin in chat_admins]
            return user_id in admin_ids
        except:
            return False
    
    elif access_mode == 'selected':
        return user_id in allowed_users
    
    elif access_mode == 'everyone':
        return True
    
    return False

async def is_chat_creator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверить, является ли пользователь создателем чата"""
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    chat_type = update.effective_chat.type
    
    if chat_type == 'private':
        return True
    
    if user_id == DEVELOPER_ID:
        return True
    
    settings = get_chat_settings(chat_id)
    if settings and settings['creator_id'] == user_id:
        return True
    
    try:
        chat_admins = await context.bot.get_chat_administrators(chat_id)
        for admin in chat_admins:
            if admin.status == 'creator' and admin.user.id == user_id:
                return True
    except:
        pass
    
    return False

# === ФУНКЦИИ ДЛЯ РАБОТЫ СО СКРИПТАМИ ===

def save_script_to_db(chat_id, command, description, code):
    """Сохранение скрипта в БД"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO scripts (chat_id, command, description, code, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
    ''', (str(chat_id), command, description, code))
    conn.commit()
    conn.close()

def delete_script_from_db(chat_id, command):
    """Удаление скрипта из БД"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM scripts WHERE chat_id = ? AND command = ?", (str(chat_id), command))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def get_script_from_db(chat_id, command):
    """Получение скрипта из БД"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT description, code, created_at, updated_at FROM scripts WHERE chat_id = ? AND command = ?",
        (str(chat_id), command)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            'description': row[0],
            'code': row[1],
            'created': row[2],
            'updated': row[3]
        }
    return None

def get_chat_scripts(chat_id):
    """Получение всех скриптов чата"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT command, description FROM scripts WHERE chat_id = ?",
        (str(chat_id),)
    )
    scripts = {row[0]: {'description': row[1]} for row in cursor.fetchall()}
    conn.close()
    return scripts

def log_execution(chat_id, user_id, command, success, error_message=None):
    """Логирование выполнения скрипта"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO execution_logs (chat_id, user_id, command, success, error_message)
            VALUES (?, ?, ?, ?, ?)
        ''', (str(chat_id), user_id, command, 1 if success else 0, error_message))
        conn.commit()
        conn.close()
    except:
        pass

def save_user(user_id, username, first_name):
    """Сохранение информации о пользователе"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO users (id, username, first_name)
            VALUES (?, ?, ?)
        ''', (user_id, username, first_name))
        conn.commit()
        conn.close()
    except:
        pass

def get_db_stats():
    """Получение статистики БД"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM scripts")
        scripts_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM users")
        users_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM execution_logs")
        logs_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(DISTINCT chat_id) FROM scripts")
        chats_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM chat_settings")
        settings_count = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            'scripts': scripts_count,
            'users': users_count,
            'logs': logs_count,
            'chats': chats_count,
            'settings': settings_count,
            'db_path': str(DB_PATH.absolute()),
            'db_size': DB_PATH.stat().st_size if DB_PATH.exists() else 0
        }
    except Exception as e:
        print(f"Ошибка получения статистики: {e}")
        return None

# Глобальный реестр скриптов
scripts_registry = {}

# Состояния для загрузки скриптов
pending_scripts = {}

# Состояния редактирования
editing_scripts = {}

# === КОМАНДЫ БОТА ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветственное сообщение"""
    chat_type = update.effective_chat.type
    
    if chat_type == 'private':
        await update.message.reply_text(
            "🤖 *Привет! Я бот с кастомными скриптами!*\n\n"
            "📌 *Команды:*\n"
            "`/addscript` - Добавить скрипт\n"
            "`/listscripts` - Список скриптов\n"
            "`/viewscript` - Посмотреть код\n"
            "`/editscript` - Редактировать\n"
            "`/deletescript` - Удалить\n"
            "`/help` - Помощь\n\n"
            "📄 Можно отправить скрипт как .txt файл!",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "🤖 *Бот активирован в чате!*\n\n"
            "📌 *Команды:*\n"
            "`/addscript` - Добавить скрипт\n"
            "`/listscripts` - Список скриптов\n"
            "`/settings` - Настройки доступа (создатель)\n"
            "`/help` - Помощь\n\n"
            "🔐 По умолчанию скрипты может создавать только создатель чата.",
            parse_mode='Markdown'
        )

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройки доступа к созданию скриптов"""
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    chat_type = update.effective_chat.type
    
    if chat_type == 'private':
        await update.message.reply_text("⚙️ Настройки доступа работают только в группах.")
        return
    
    if not await is_chat_creator(update, context):
        await update.message.reply_text("❌ Только создатель чата может менять настройки!")
        return
    
    settings = get_chat_settings(chat_id)
    if not settings:
        save_chat_settings(chat_id, user_id, 'creator', [])
        settings = {'creator_id': user_id, 'access_mode': 'creator', 'allowed_users': []}
    
    if context.args:
        new_mode = context.args[0].lower()
        if new_mode in ['creator', 'admins', 'selected', 'everyone']:
            save_chat_settings(chat_id, settings['creator_id'], new_mode, settings['allowed_users'])
            
            mode_names = {
                'creator': '👑 Только создатель',
                'admins': '👮 Все администраторы',
                'selected': '👥 Выбранные пользователи',
                'everyone': '🌍 Все участники'
            }
            
            await update.message.reply_text(
                f"✅ Режим доступа изменён!\n\n"
                f"🔐 Новый режим: *{mode_names[new_mode]}*",
                parse_mode='Markdown'
            )
            return
    
    mode_names = {
        'creator': '👑 Только создатель',
        'admins': '👮 Все администраторы',
        'selected': '👥 Выбранные пользователи',
        'everyone': '🌍 Все участники'
    }
    
    allowed_text = ""
    if settings['allowed_users']:
        allowed_text = f"\n👥 Разрешённые ID: {settings['allowed_users']}"
    
    await update.message.reply_text(
        f"⚙️ *Настройки доступа*\n\n"
        f"🔐 Текущий режим: *{mode_names.get(settings['access_mode'], settings['access_mode'])}*"
        f"{allowed_text}\n\n"
        f"📌 *Изменить режим:*\n"
        f"`/settings creator` - только создатель\n"
        f"`/settings admins` - все админы\n"
        f"`/settings selected` - выбранные\n"
        f"`/settings everyone` - все\n\n"
        f"👥 *Управление пользователями:*\n"
        f"`/allowuser @username` - разрешить\n"
        f"`/denyuser @username` - запретить",
        parse_mode='Markdown'
    )

async def allow_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разрешить пользователю создавать скрипты"""
    chat_id = str(update.effective_chat.id)
    
    if not await is_chat_creator(update, context):
        await update.message.reply_text("❌ Только создатель чата может управлять доступом!")
        return
    
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        target_id = target_user.id
        target_name = target_user.username or target_user.first_name
    elif context.args:
        await update.message.reply_text(
            "💡 Ответьте на сообщение пользователя командой `/allowuser`\n"
            "Или укажите ID: `/allowuser 123456789`",
            parse_mode='Markdown'
        )
        try:
            target_id = int(context.args[0].replace('@', ''))
            target_name = str(target_id)
        except:
            return
    else:
        await update.message.reply_text(
            "❌ Ответьте на сообщение пользователя или укажите ID:\n"
            "`/allowuser 123456789`",
            parse_mode='Markdown'
        )
        return
    
    if add_allowed_user(chat_id, target_id):
        await update.message.reply_text(f"✅ Пользователь {target_name} (ID: {target_id}) добавлен в список разрешённых!")
    else:
        await update.message.reply_text("❌ Не удалось добавить пользователя. Возможно, нет настроек чата.")

async def deny_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запретить пользователю создавать скрипты"""
    chat_id = str(update.effective_chat.id)
    
    if not await is_chat_creator(update, context):
        await update.message.reply_text("❌ Только создатель чата может управлять доступом!")
        return
    
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        target_id = target_user.id
        target_name = target_user.username or target_user.first_name
    elif context.args:
        try:
            target_id = int(context.args[0].replace('@', ''))
            target_name = str(target_id)
        except:
            await update.message.reply_text("❌ Укажите корректный ID пользователя")
            return
    else:
        await update.message.reply_text(
            "❌ Ответьте на сообщение пользователя или укажите ID:\n"
            "`/denyuser 123456789`",
            parse_mode='Markdown'
        )
        return
    
    if remove_allowed_user(chat_id, target_id):
        await update.message.reply_text(f"✅ Пользователь {target_name} (ID: {target_id}) удалён из списка разрешённых!")
    else:
        await update.message.reply_text("❌ Пользователь не найден в списке разрешённых.")

async def db_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать информацию о базе данных (только для разработчика)"""
    user_id = update.effective_user.id
    
    if user_id != DEVELOPER_ID:
        await update.message.reply_text("❌ Эта команда доступна только разработчику.")
        return
    
    stats = get_db_stats()
    
    if stats:
        await update.message.reply_text(
            "📊 *Информация о базе данных:*\n\n"
            f"📁 Путь: `{stats['db_path']}`\n"
            f"💾 Размер: {stats['db_size']} байт\n"
            f"📜 Скриптов: {stats['scripts']}\n"
            f"👥 Пользователей: {stats['users']}\n"
            f"💬 Чатов: {stats['chats']}\n"
            f"⚙️ Настроек чатов: {stats['settings']}\n"
            f"📝 Логов: {stats['logs']}\n\n"
            f"✅ БД работает нормально!",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ Ошибка получения информации о БД!")

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущего действия"""
    user_id = update.effective_user.id
    cancelled = False
    
    if user_id in pending_scripts:
        del pending_scripts[user_id]
        cancelled = True
    if user_id in editing_scripts:
        del editing_scripts[user_id]
        cancelled = True
    
    if cancelled:
        await update.message.reply_text("❌ Действие отменено.")
    else:
        await update.message.reply_text("ℹ️ Нет активных действий для отмены.")

async def add_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало процесса добавления скрипта"""
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    
    if not await check_script_permission(update, context):
        await update.message.reply_text(
            "❌ У вас нет прав на создание скриптов в этом чате.\n"
            "Обратитесь к создателю чата.",
            parse_mode='Markdown'
        )
        return
    
    pending_scripts[user_id] = {
        'chat_id': chat_id,
        'code': '',
        'command': None,
        'description': 'Без описания',
        'stage': 'waiting_first'
    }
    
    await update.message.reply_text(
        "📝 *Отправьте скрипт одним из способов:*\n\n"
        "1️⃣ *Текстом:*\n"
        "```\n"
        "###COMMAND: название\n"
        "###DESCRIPTION: описание\n"
        "###CODE:\n"
        "async def execute(update, context, args):\n"
        "    return 'Результат'\n\n"
        "Если в скрипте есть база данных, то использовать строго SQlite.\n\n"
        "Если в скрипте есть подкоманды, то использовать их строго после основной команды, пример: /kod start, /kod stop.\n\n"
        "Если в скрипте есть отчет времени, то использовать для него строго отдельную def функцию."
        "```\n\n"
        "2️⃣ *Файлом .txt* с тем же форматом\n\n"
        "📌 Можно отправлять частями!\n"
        "⚠️ `/cancel` - отменить",
        parse_mode='Markdown'
    )

async def view_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр исходного кода скрипта"""
    chat_id = str(update.effective_chat.id)
    
    if not context.args:
        await update.message.reply_text(
            "❌ Укажите команду: `/viewscript /команда`",
            parse_mode='Markdown'
        )
        return
    
    command = context.args[0].lower()
    if not command.startswith('/'):
        command = '/' + command
    
    script_info = get_script_from_db(chat_id, command)
    
    if not script_info:
        await update.message.reply_text(f"❌ Скрипт `{command}` не найден!", parse_mode='Markdown')
        return
    
    code = script_info['code']
    header = f"📄 *Исходный код* `{command}`\n📝 {script_info['description']}\n\n"
    
    max_code_len = 3500
    
    if len(code) > max_code_len:
        # Отправляем как .txt файл
        import io
        file_content = code.encode('utf-8')
        file = io.BytesIO(file_content)
        filename = command.replace('/', '') + '.txt'
        
        await update.message.reply_document(
            document=file,
            filename=filename,
            caption=f"📄 *Исходный код* `{command}`\n📝 {script_info['description']}\n\n📦 Размер: {len(code)} символов",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(header + f"```python\n{code}\n```", parse_mode='Markdown')

async def edit_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало редактирования скрипта"""
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    
    if not await check_script_permission(update, context):
        await update.message.reply_text("❌ У вас нет прав на редактирование скриптов!")
        return
    
    if not context.args:
        await update.message.reply_text(
            "❌ Укажите команду: `/editscript /команда`",
            parse_mode='Markdown'
        )
        return
    
    command = context.args[0].lower()
    if not command.startswith('/'):
        command = '/' + command
    
    script_info = get_script_from_db(chat_id, command)
    
    if not script_info:
        await update.message.reply_text(f"❌ Скрипт `{command}` не найден!", parse_mode='Markdown')
        return
    
    current_code = script_info['code']
    
    editing_scripts[user_id] = {
        'chat_id': chat_id,
        'command': command,
        'code': '',
        'stage': 'waiting_new_code'
    }
    
    await update.message.reply_text(
        f"✏️ *Редактирование* `{command}`\n\n"
        f"📝 Описание: {script_info['description']}\n\n"
        f"Отправьте *новый код* (текстом или .txt файлом).\n\n"
        f"⚠️ `/cancel` - отменить",
        parse_mode='Markdown'
    )
    
    if len(current_code) > 3500:
        await update.message.reply_text("📄 Текущий код (начало):\n```python\n" + current_code[:3500] + "\n...\n```", parse_mode='Markdown')
    else:
        await update.message.reply_text("📄 Текущий код:\n```python\n" + current_code + "\n```", parse_mode='Markdown')

def parse_script_text(text):
    """Парсинг текста скрипта"""
    lines = text.strip().split('\n')
    command = None
    description = "Без описания"
    code_lines = []
    in_code = False
    
    for line in lines:
        if line.startswith('###COMMAND:'):
            command = line.replace('###COMMAND:', '').strip().lower()
            if command and not command.startswith('/'):
                command = '/' + command
        elif line.startswith('###DESCRIPTION:'):
            description = line.replace('###DESCRIPTION:', '').strip()
        elif line.startswith('###CODE:'):
            in_code = True
        elif in_code:
            code_lines.append(line)
    
    code = '\n'.join(code_lines)
    return command, description, code

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка загруженных документов (.txt файлов)"""
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    document = update.message.document
    
    if not document:
        return False
    
    file_name = document.file_name or ""
    if not file_name.endswith('.txt'):
        return False
    
    if user_id not in pending_scripts and user_id not in editing_scripts:
        if not await check_script_permission(update, context):
            await update.message.reply_text(
                "❌ У вас нет прав на создание скриптов в этом чате.",
                parse_mode='Markdown'
            )
            return True
    
    try:
        file = await context.bot.get_file(document.file_id)
        file_content = await file.download_as_bytearray()
        text = file_content.decode('utf-8')
        
        await update.message.reply_text(f"📄 Файл `{file_name}` получен ({len(text)} символов)", parse_mode='Markdown')
        
        command, description, code = parse_script_text(text)
        
        if not code.strip():
            code = text
        
        # Режим редактирования
        if user_id in editing_scripts:
            editing = editing_scripts.pop(user_id)
            target_command = editing['command']
            target_chat_id = editing['chat_id']
            
            if description != "Без описания":
                new_description = description
            else:
                script_info = get_script_from_db(target_chat_id, target_command)
                new_description = script_info['description'] if script_info else 'Без описания'
            
            if 'async def execute' not in code and 'def execute' not in code:
                await update.message.reply_text("❌ Не найдена функция execute! Файл не сохранён.")
                return True
            
            save_script_to_db(target_chat_id, target_command, new_description, code)
            
            if target_chat_id in scripts_registry and target_command in scripts_registry[target_chat_id]:
                scripts_registry[target_chat_id][target_command]['code'] = code
                scripts_registry[target_chat_id][target_command]['description'] = new_description
            
            await update.message.reply_text(
                f"✅ *Скрипт обновлён из файла!*\n\n"
                f"📌 Команда: `{target_command}`\n"
                f"📦 Размер: {len(code)} символов",
                parse_mode='Markdown'
            )
            return True
        
        # Режим добавления нового скрипта
        if user_id in pending_scripts:
            pending = pending_scripts.pop(user_id)
            if not command and pending.get('command'):
                command = pending['command']
            if description == "Без описания" and pending.get('description') != "Без описания":
                description = pending['description']
            chat_id = pending['chat_id']
        
        if not command:
            await update.message.reply_text(
                "❌ Не указана команда!\n\n"
                "Добавьте в начало файла:\n"
                "`###COMMAND: название`",
                parse_mode='Markdown'
            )
            return True
        
        if 'async def execute' not in code and 'def execute' not in code:
            await update.message.reply_text("❌ Не найдена функция execute! Файл не сохранён.")
            return True
        
        save_script_to_db(chat_id, command, description, code)
        
        if chat_id not in scripts_registry:
            scripts_registry[chat_id] = {}
        
        scripts_registry[chat_id][command] = {
            'description': description,
            'code': code,
            'created': datetime.now().isoformat()
        }
        
        save_user(user_id, update.effective_user.username, update.effective_user.first_name)
        
        await update.message.reply_text(
            f"✅ *Скрипт сохранён из файла!*\n\n"
            f"📌 Команда: `{command}`\n"
            f"📝 Описание: {description}\n"
            f"📦 Размер: {len(code)} символов\n\n"
            f"Используйте `{command}` в этом чате!",
            parse_mode='Markdown'
        )
        
        return True
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка чтения файла: {e}")
        return True

async def handle_script_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка загруженного скрипта"""
    if not update.message or not update.message.text:
        return False
    
    user_id = update.effective_user.id
    text = update.message.text
    
    if user_id in editing_scripts:
        return await handle_edit_upload(update, context)
    
    if user_id not in pending_scripts:
        return False
    
    pending = pending_scripts[user_id]
    chat_id = pending['chat_id']
    
    lower_text = text.lower().strip().rstrip('!.,;:?')
    finish_words = ['нет', 'no', 'готово', 'done', 'сохранить', 'save', 'хватит', 'всё', 'все', 'конец', 'end', 'finish', 'ok', 'ок', 'сохрани', 'финиш']
    continue_words = ['да', 'yes', 'ещё', 'еще', 'more', 'есть', 'продолжить', 'дальше']
    
    if lower_text in finish_words:
        return await finalize_script(update, context, user_id)
    
    if lower_text in continue_words:
        await update.message.reply_text("📝 Отправьте продолжение кода:")
        return True
    
    if pending['stage'] == 'waiting_first':
        command, description, code = parse_script_text(text)
        if command:
            pending['command'] = command
        if description != "Без описания":
            pending['description'] = description
        pending['code'] = code if code else text
        pending['stage'] = 'waiting_more'
    else:
        pending['code'] += '\n' + text
    
    await update.message.reply_text(
        f"✅ Код получен! (всего {len(pending['code'])} символов)\n\n"
        f"📌 Команда: `{pending['command'] or 'не указана'}`\n\n"
        f"❓ *Есть ещё код?*\n"
        f"• Отправьте продолжение\n"
        f"• Или напишите `готово`\n\n"
        f"⚠️ `/cancel` - отменить",
        parse_mode='Markdown'
    )
    
    return True

async def handle_edit_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка загрузки при редактировании"""
    if not update.message or not update.message.text:
        return False
    
    user_id = update.effective_user.id
    text = update.message.text
    
    if user_id not in editing_scripts:
        return False
    
    editing = editing_scripts[user_id]
    
    lower_text = text.lower().strip().rstrip('!.,;:?')
    finish_words = ['нет', 'no', 'готово', 'done', 'сохранить', 'save', 'хватит', 'всё', 'все', 'конец', 'end', 'finish', 'ok', 'ок', 'сохрани', 'финиш']
    continue_words = ['да', 'yes', 'ещё', 'еще', 'more', 'есть', 'продолжить', 'дальше']
    
    if lower_text in finish_words:
        return await finalize_edit(update, context, user_id)
    
    if lower_text in continue_words:
        await update.message.reply_text("📝 Отправьте продолжение кода:")
        return True
    
    if editing['stage'] == 'waiting_new_code':
        command, description, code = parse_script_text(text)
        editing['code'] = code if code else text
        if description != "Без описания":
            editing['new_description'] = description
        editing['stage'] = 'waiting_more'
    else:
        editing['code'] += '\n' + text
    
    await update.message.reply_text(
        f"✅ Код получен! (всего {len(editing['code'])} символов)\n\n"
        f"❓ Есть ещё код? Отправьте или напишите `готово`",
        parse_mode='Markdown'
    )
    
    return True

async def finalize_script(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Финализация и сохранение нового скрипта"""
    try:
        if user_id not in pending_scripts:
            await update.message.reply_text("❌ Нет данных для сохранения. Начните заново с /addscript")
            return True
        
        pending = pending_scripts.pop(user_id)
        chat_id = pending['chat_id']
        code = pending['code']
        command = pending['command']
        description = pending['description']
        
        if not code or not code.strip():
            await update.message.reply_text("❌ Пустой код! Скрипт не сохранён. Начните заново с /addscript")
            return True
        
        if not command:
            await update.message.reply_text("❌ Не указана команда (###COMMAND:)! Скрипт не сохранён.")
            return True
        
        if 'async def execute' not in code and 'def execute' not in code:
            await update.message.reply_text("❌ Не найдена функция execute! Скрипт не сохранён.")
            return True
        
        save_script_to_db(chat_id, command, description, code)
        
        if chat_id not in scripts_registry:
            scripts_registry[chat_id] = {}
        
        scripts_registry[chat_id][command] = {
            'description': description,
            'code': code,
            'created': datetime.now().isoformat()
        }
        
        save_user(user_id, update.effective_user.username, update.effective_user.first_name)
        
        await update.message.reply_text(
            f"✅ *Скрипт сохранён!*\n\n"
            f"📌 Команда: `{command}`\n"
            f"📝 Описание: {description}\n"
            f"📦 Размер: {len(code)} символов\n\n"
            f"Используйте `{command}` в этом чате!",
            parse_mode='Markdown'
        )
        
        return True
    except Exception as e:
        print(f"Ошибка finalize_script: {e}")
        await update.message.reply_text(f"❌ Ошибка сохранения: {e}")
        return True

async def finalize_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Финализация редактирования скрипта"""
    try:
        if user_id not in editing_scripts:
            await update.message.reply_text("❌ Нет данных для сохранения. Начните заново с /editscript")
            return True
        
        editing = editing_scripts.pop(user_id)
        chat_id = editing['chat_id']
        command = editing['command']
        code = editing['code']
        
        if not code or not code.strip():
            await update.message.reply_text("❌ Пустой код! Редактирование отменено.")
            return True
        
        if 'async def execute' not in code and 'def execute' not in code:
            await update.message.reply_text("❌ Не найдена функция execute! Редактирование отменено.")
            return True
        
        script_info = get_script_from_db(chat_id, command)
        description = editing.get('new_description', script_info['description'])
        
        save_script_to_db(chat_id, command, description, code)
        
        if chat_id in scripts_registry and command in scripts_registry[chat_id]:
            scripts_registry[chat_id][command]['code'] = code
            scripts_registry[chat_id][command]['description'] = description
            scripts_registry[chat_id][command]['updated'] = datetime.now().isoformat()
        
        await update.message.reply_text(
            f"✅ *Скрипт обновлён!*\n\n"
            f"📌 Команда: `{command}`\n"
            f"📦 Новый размер: {len(code)} символов",
            parse_mode='Markdown'
        )
        
        return True
    except Exception as e:
        print(f"Ошибка finalize_edit: {e}")
        await update.message.reply_text(f"❌ Ошибка сохранения: {e}")
        return True

async def execute_custom_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнение кастомного скрипта"""
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    message_text = update.message.text
    
    if not message_text.startswith('/'):
        return False
    
    parts = message_text.split()
    command = parts[0].lower()
    args = parts[1:] if len(parts) > 1 else []
    
    if '@' in command:
        command = command.split('@')[0]
    
    script_info = get_script_from_db(chat_id, command)
    
    if not script_info:
        return False
    
    script_code = script_info['code']
    
    try:
        import builtins
        local_namespace = {
            '__builtins__': builtins,
            'update': update,
            'context': context,
            'args': args,
            'DATA_DIR': DATA_DIR,
            'DB_PATH': DB_PATH,
        }
        
        popular_modules = [
            'math', 'random', 'datetime', 're', 'json', 'os', 'sys',
            'subprocess', 'requests', 'asyncio', 'aiohttp', 'time',
            'hashlib', 'base64', 'urllib', 'collections', 'itertools',
            'functools', 'operator', 'string', 'textwrap', 'uuid',
            'pathlib', 'shutil', 'glob', 'fnmatch', 'tempfile',
            'pickle', 'sqlite3', 'csv', 'io', 'struct', 'codecs',
            'html', 'xml', 'email', 'mimetypes', 'socket', 'ssl',
            'threading', 'multiprocessing', 'queue', 'concurrent',
        ]
        
        for mod_name in popular_modules:
            try:
                local_namespace[mod_name] = __import__(mod_name)
            except ImportError:
                pass
        
        exec(script_code, local_namespace)
        
        if 'execute' in local_namespace:
            result = await local_namespace['execute'](update, context, args)
            if result:
                result_str = str(result)
                try:
                    await update.message.reply_text(result_str, parse_mode='Markdown')
                except Exception:
                    await update.message.reply_text(result_str)
        
        log_execution(chat_id, user_id, command, True)
        
    except Exception as e:
        log_execution(chat_id, user_id, command, False, str(e))
        print(f"Script execution error: {e}")
        await update.message.reply_text(f"❌ Ошибка выполнения:\n`{str(e)}`", parse_mode='Markdown')
    
    return True

async def list_scripts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список скриптов чата"""
    chat_id = str(update.effective_chat.id)
    
    scripts = get_chat_scripts(chat_id)
    
    if not scripts:
        await update.message.reply_text("📭 В этом чате нет скриптов.")
        return
    
    text = "📜 *Скрипты этого чата:*\n\n"
    for cmd, info in scripts.items():
        text += f"• `{cmd}` - {info['description']}\n"
    
    await update.message.reply_text(text, parse_mode='Markdown')

async def delete_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удалить скрипт"""
    chat_id = str(update.effective_chat.id)
    
    if not await check_script_permission(update, context):
        await update.message.reply_text("❌ У вас нет прав на удаление скриптов!")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Укажите команду: `/deletescript /команда`", parse_mode='Markdown')
        return
    
    command = context.args[0].lower()
    if not command.startswith('/'):
        command = '/' + command
    
    if delete_script_from_db(chat_id, command):
        if chat_id in scripts_registry and command in scripts_registry[chat_id]:
            del scripts_registry[chat_id][command]
        
        await update.message.reply_text(f"✅ Скрипт `{command}` удалён!", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ Скрипт `{command}` не найден!", parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помощь"""
    await update.message.reply_text(
        "📖 *Справка по боту*\n\n"
        "*Команды:*\n"
        "`/addscript` - Добавить скрипт\n"
        "`/listscripts` - Список скриптов\n"
        "`/viewscript /cmd` - Посмотреть код\n"
        "`/editscript /cmd` - Редактировать\n"
        "`/deletescript /cmd` - Удалить\n"
        "`/settings` - Настройки доступа\n"
        "`/allowuser` - Разрешить пользователю\n"
        "`/denyuser` - Запретить пользователю\n"
        "`/cancel` - Отменить действие\n\n"
        "*Как добавить скрипт:*\n"
        "1. `/addscript`\n"
        "2. Отправьте код текстом или .txt файлом\n"
        "3. Напишите `готово`\n\n"
        "*Формат:*\n"
        "```\n"
        "###COMMAND: mycommand\n"
        "###DESCRIPTION: Описание\n"
        "###CODE:\n"
        "async def execute(update, context, args):\n"
        "    return 'Привет!'\n"
        "```",
        parse_mode='Markdown'
    )

async def run_triggers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск триггер-скриптов"""
    chat_id = str(update.effective_chat.id)
    
    scripts = get_chat_scripts(chat_id)
    
    if not scripts:
        return
    
    for cmd in scripts.keys():
        script_info = get_script_from_db(chat_id, cmd)
        if not script_info:
            continue
        
        script_code = script_info['code']
        
        if 'async def check_triggers' not in script_code and 'def check_triggers' not in script_code:
            continue
        
        try:
            import builtins
            local_namespace = {
                '__builtins__': builtins,
                'update': update,
                'context': context,
                'DATA_DIR': DATA_DIR,
                'DB_PATH': DB_PATH,
            }
            
            for mod in ['math','random','datetime','re','json','os','sys','subprocess',
                        'requests','asyncio','aiohttp','time','sqlite3','hashlib','base64','pathlib']:
                try: local_namespace[mod] = __import__(mod)
                except: pass
            
            exec(script_code, local_namespace)
            
            if 'check_triggers' in local_namespace:
                await local_namespace['check_triggers'](update, context)
        except Exception as e:
            print(f"Trigger error in {cmd}: {e}")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Общий обработчик сообщений"""
    if await handle_script_upload(update, context):
        return
    
    await run_triggers(update, context)
    
    if update.message.text and update.message.text.startswith('/'):
        await execute_custom_script(update, context)

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик документов"""
    await handle_document(update, context)

def main():
    """Запуск бота"""
    print("🚀 Запуск бота...")
    print(f"📁 Директория данных: {DATA_DIR.absolute()}")
    print(f"🗄️ База данных: {DB_PATH.absolute()}")
    
    global scripts_registry
    scripts_registry = load_scripts_registry()
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Системные команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addscript", add_script))
    application.add_handler(CommandHandler("listscripts", list_scripts))
    application.add_handler(CommandHandler("viewscript", view_script))
    application.add_handler(CommandHandler("editscript", edit_script))
    application.add_handler(CommandHandler("deletescript", delete_script))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("allowuser", allow_user))
    application.add_handler(CommandHandler("denyuser", deny_user))
    application.add_handler(CommandHandler("dbinfo", db_info))
    application.add_handler(CommandHandler("cancel", cancel_action))
    application.add_handler(CommandHandler("help", help_command))
    
    application.add_handler(CallbackQueryHandler(handle_somka_callbacks, pattern=r"^somka_"))
    
    # Обработчик документов (.txt файлы)
    application.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    
    # Обработчик текстовых сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_handler(MessageHandler(filters.COMMAND, execute_custom_script))
    
    print("🤖 Бот запущен!")
    application.run_polling()

if __name__ == "__main__":
    main()
