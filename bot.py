import os
import sqlite3
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from datetime import datetime
from pathlib import Path

# Конфигурация
BOT_TOKEN = "8512207770:AAEKLtYEph7gleybGhF2lc7Gwq82Kj1yedM"

# Путь к папке data
DATA_DIR = Path("/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Путь к базе данных
DB_PATH = DATA_DIR / "bot.db"

def init_database():
    """Инициализация базы данных"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # Таблица скриптов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            command TEXT NOT NULL,
            description TEXT DEFAULT 'Без описания',
            code TEXT NOT NULL,
            author TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, command)
        )
    ''')
    
    # Таблица пользователей (для статистики)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица логов выполнения
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
    print(f"База данных инициализирована: {DB_PATH}")

# Инициализируем БД при запуске
init_database()

def get_db_connection():
    """Получить соединение с БД"""
    return sqlite3.connect(str(DB_PATH))

def load_scripts_registry():
    """Загрузка реестра скриптов из БД"""
    registry = {}
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, command, description, code, author, created_at, updated_at FROM scripts")
    for row in cursor.fetchall():
        chat_id, command, description, code, author, created_at, updated_at = row
        if chat_id not in registry:
            registry[chat_id] = {}
        registry[chat_id][command] = {
            'description': description,
            'code': code,
            'author': author,
            'created': created_at,
            'updated': updated_at
        }
    conn.close()
    return registry

def save_script_to_db(chat_id, command, description, code, author):
    """Сохранение скрипта в БД"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO scripts (chat_id, command, description, code, author, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ''', (str(chat_id), command, description, code, author))
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
        "SELECT description, code, author, created_at, updated_at FROM scripts WHERE chat_id = ? AND command = ?",
        (str(chat_id), command)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            'description': row[0],
            'code': row[1],
            'author': row[2],
            'created': row[3],
            'updated': row[4]
        }
    return None

def get_chat_scripts(chat_id):
    """Получение всех скриптов чата"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT command, description, author FROM scripts WHERE chat_id = ?",
        (str(chat_id),)
    )
    scripts = {row[0]: {'description': row[1], 'author': row[2]} for row in cursor.fetchall()}
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

# Глобальный реестр скриптов (кэш из БД)
scripts_registry = load_scripts_registry()

# Состояния для многочастной загрузки скриптов
# {user_id: {'chat_id': str, 'code': str, 'command': str, 'description': str, 'stage': str}}
pending_scripts = {}

# Состояния редактирования {user_id: {'chat_id': str, 'command': str, 'stage': str}}
editing_scripts = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветственное сообщение"""
    await update.message.reply_text(
        "🤖 *Привет! Я бот с кастомными скриптами!*\n\n"
        "📌 *Доступные команды:*\n"
        "`/addscript` - Добавить новый скрипт\n"
        "`/listscripts` - Список скриптов чата\n"
        "`/viewscript <команда>` - Посмотреть код\n"
        "`/editscript <команда>` - Редактировать скрипт\n"
        "`/deletescript <команда>` - Удалить скрипт\n"
        "`/cancel` - Отменить текущее действие\n"
        "`/help` - Помощь\n\n"
        "💡 Вы можете создавать свои команды!",
        parse_mode='Markdown'
    )

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
    
    pending_scripts[user_id] = {
        'chat_id': chat_id,
        'code': '',
        'command': None,
        'description': 'Без описания',
        'stage': 'waiting_first'
    }
    
    await update.message.reply_text(
        "📝 *Отправьте скрипт в следующем формате:*\n\n"
        "```\n"
        "###COMMAND: название_команды\n"
        "###DESCRIPTION: описание\n"
        "###CODE:\n"
        "# Ваш Python код здесь\n"
        "async def execute(update, context, args):\n"
        "    return 'Результат'\n\n"
        "Если в скрипте есть база данных, то использовать строго SQlite.\n\n"
        "Если в скрипте есть подкоманды, то использовать их строго после основной команды, пример: /kod start, /kod stop.\n\n"
        "Если в скрипте есть отчет времени, то использовать для него строго отдельную def функцию."
        "```\n\n"
        "📌 Можно отправлять код частями!\n"
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
    
    # Получаем скрипт из БД
    script_info = get_script_from_db(chat_id, command)
    
    if not script_info:
        await update.message.reply_text(f"❌ Скрипт `{command}` не найден!", parse_mode='Markdown')
        return
    
    code = script_info['code']
    
    # Экранируем специальные символы для Markdown
    # Разбиваем на части если код большой
    header = f"📄 *Исходный код* `{command}`\n👤 Автор: @{script_info['author']}\n📝 {script_info['description']}\n\n"
    
    # Telegram limit ~4096, оставляем запас
    max_code_len = 3500
    
    if len(code) > max_code_len:
        parts = [code[i:i+max_code_len] for i in range(0, len(code), max_code_len)]
        await update.message.reply_text(header + f"📦 Код разбит на {len(parts)} частей:")
        for i, part in enumerate(parts, 1):
            await update.message.reply_text(f"```python\n{part}\n```", parse_mode='Markdown')
    else:
        await update.message.reply_text(header + f"```python\n{code}\n```", parse_mode='Markdown')

async def edit_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало редактирования скрипта"""
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    
    if not context.args:
        await update.message.reply_text(
            "❌ Укажите команду: `/editscript /команда`",
            parse_mode='Markdown'
        )
        return
    
    command = context.args[0].lower()
    if not command.startswith('/'):
        command = '/' + command
    
    # Получаем скрипт из БД
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
        f"📝 Текущее описание: {script_info['description']}\n\n"
        f"Отправьте *новый код полностью* (можно частями).\n"
        f"Формат такой же как при добавлении.\n\n"
        f"⚠️ `/cancel` - отменить редактирование",
        parse_mode='Markdown'
    )
    
    # Отправляем текущий код для справки
    if len(current_code) > 3500:
        await update.message.reply_text("📄 Текущий код (начало):\n```python\n" + current_code[:3500] + "\n...\n```", parse_mode='Markdown')
    else:
        await update.message.reply_text("📄 Текущий код:\n```python\n" + current_code + "\n```", parse_mode='Markdown')

def parse_script_text(text):
    """Парсинг текста скрипта, возвращает (command, description, code)"""
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

async def handle_script_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка загруженного скрипта (многочастная загрузка)"""
    user_id = update.effective_user.id
    text = update.message.text
    
    # Проверяем режим редактирования
    if user_id in editing_scripts:
        return await handle_edit_upload(update, context)
    
    # Проверяем режим добавления
    if user_id not in pending_scripts:
        return False
    
    pending = pending_scripts[user_id]
    chat_id = pending['chat_id']
    
    # Проверяем ответ "нет" / "да" / "готово"
    lower_text = text.lower().strip()
    if lower_text in ['нет', 'no', 'готово', 'done', 'сохранить', 'save']:
        # Финализируем скрипт
        return await finalize_script(update, context, user_id)
    
    if lower_text in ['да', 'yes', 'ещё', 'еще', 'more']:
        await update.message.reply_text("📝 Отправьте продолжение кода:")
        return True
    
    # Добавляем код
    if pending['stage'] == 'waiting_first':
        # Первая часть - парсим заголовки
        command, description, code = parse_script_text(text)
        if command:
            pending['command'] = command
        if description != "Без описания":
            pending['description'] = description
        pending['code'] = code if code else text
        pending['stage'] = 'waiting_more'
    else:
        # Дополнительные части - просто добавляем
        pending['code'] += '\n' + text
    
    await update.message.reply_text(
        f"✅ Код получен! (всего {len(pending['code'])} символов)\n\n"
        f"📌 Команда: `{pending['command'] or 'не указана'}`\n\n"
        f"❓ *Есть чем дополнить код?*\n"
        f"• Отправьте продолжение кода\n"
        f"• Или напишите `нет` / `готово` для сохранения\n\n"
        f"⚠️ `/cancel` - отменить",
        parse_mode='Markdown'
    )
    
    return True

async def handle_edit_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка загрузки при редактировании"""
    user_id = update.effective_user.id
    text = update.message.text
    
    if user_id not in editing_scripts:
        return False
    
    editing = editing_scripts[user_id]
    
    lower_text = text.lower().strip()
    if lower_text in ['нет', 'no', 'готово', 'done', 'сохранить', 'save']:
        return await finalize_edit(update, context, user_id)
    
    if lower_text in ['да', 'yes', 'ещё', 'еще', 'more']:
        await update.message.reply_text("📝 Отправьте продолжение кода:")
        return True
    
    # Добавляем код
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
        f"❓ *Есть чем дополнить код?*\n"
        f"• Отправьте продолжение\n"
        f"• Или напишите `нет` / `готово` для сохранения\n\n"
        f"⚠️ `/cancel` - отменить",
        parse_mode='Markdown'
    )
    
    return True

async def finalize_script(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Финализация и сохранение нового скрипта"""
    pending = pending_scripts.pop(user_id)
    chat_id = pending['chat_id']
    code = pending['code']
    command = pending['command']
    description = pending['description']
    author = update.effective_user.username or str(user_id)
    
    if not command:
        await update.message.reply_text("❌ Не указана команда (###COMMAND:)! Скрипт не сохранён.")
        return True
    
    if 'async def execute' not in code and 'def execute' not in code:
        await update.message.reply_text("❌ Не найдена функция execute! Скрипт не сохранён.")
        return True
    
    # Сохранение скрипта в БД
    save_script_to_db(chat_id, command, description, code, author)
    
    # Обновление кэша
    if chat_id not in scripts_registry:
        scripts_registry[chat_id] = {}
    
    scripts_registry[chat_id][command] = {
        'description': description,
        'code': code,
        'author': author,
        'created': datetime.now().isoformat()
    }
    
    # Сохраняем пользователя
    save_user(user_id, update.effective_user.username, update.effective_user.first_name)
    
    await update.message.reply_text(
        f"✅ *Скрипт успешно сохранён!*\n\n"
        f"📌 Команда: `{command}`\n"
        f"📝 Описание: {description}\n"
        f"📦 Размер: {len(code)} символов\n\n"
        f"Теперь вы можете использовать `{command}` в этом чате!",
        parse_mode='Markdown'
    )
    
    return True

async def finalize_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Финализация редактирования скрипта"""
    editing = editing_scripts.pop(user_id)
    chat_id = editing['chat_id']
    command = editing['command']
    code = editing['code']
    
    if not code.strip():
        await update.message.reply_text("❌ Пустой код! Редактирование отменено.")
        return True
    
    if 'async def execute' not in code and 'def execute' not in code:
        await update.message.reply_text("❌ Не найдена функция execute! Редактирование отменено.")
        return True
    
    # Получаем текущую информацию о скрипте
    script_info = get_script_from_db(chat_id, command)
    description = editing.get('new_description', script_info['description'])
    author = script_info['author']
    
    # Сохраняем в БД
    save_script_to_db(chat_id, command, description, code, author)
    
    # Обновляем кэш
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

async def execute_custom_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнение кастомного скрипта"""
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    message_text = update.message.text
    
    # Проверяем, это команда?
    if not message_text.startswith('/'):
        return False
    
    parts = message_text.split()
    command = parts[0].lower()
    args = parts[1:] if len(parts) > 1 else []
    
    # Убираем @botname если есть
    if '@' in command:
        command = command.split('@')[0]
    
    # Получаем скрипт из БД
    script_info = get_script_from_db(chat_id, command)
    
    if not script_info:
        return False
    
    script_code = script_info['code']
    
    try:
        # Создаем локальное пространство имен с полным доступом
        import builtins
        local_namespace = {
            '__builtins__': builtins,  # Полный доступ ко всем встроенным функциям
            'update': update,
            'context': context,
            'args': args,
            'DATA_DIR': DATA_DIR,  # Доступ к папке данных
            'DB_PATH': DB_PATH,    # Доступ к пути БД
            'InlineKeyboardButton': InlineKeyboardButton,  # Для inline-кнопок
            'InlineKeyboardMarkup': InlineKeyboardMarkup,  # Для разметки кнопок
        }
        
        # Предварительно импортируем популярные модули
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
                pass  # Модуль не установлен
        
        exec(script_code, local_namespace)
        
        if 'execute' in local_namespace:
            result = await local_namespace['execute'](update, context, args)
            if result:
                result_str = str(result)
                # Пробуем отправить с Markdown, если ошибка - без форматирования
                try:
                    await update.message.reply_text(result_str, parse_mode='Markdown')
                except Exception:
                    # Если Markdown не работает (скобки {} и др.), отправляем как есть
                    await update.message.reply_text(result_str)
        
        # Логируем успешное выполнение
        log_execution(chat_id, user_id, command, True)
        
    except Exception as e:
        # Логируем ошибку
        log_execution(chat_id, user_id, command, False, str(e))
        await update.message.reply_text(f"❌ Ошибка выполнения скрипта:\n`{str(e)}`", parse_mode='Markdown')
    
    return True

async def list_scripts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список скриптов чата"""
    chat_id = str(update.effective_chat.id)
    
    # Получаем скрипты из БД
    scripts = get_chat_scripts(chat_id)
    
    if not scripts:
        await update.message.reply_text("📭 В этом чате пока нет кастомных скриптов.")
        return
    
    text = "📜 *Кастомные скрипты этого чата:*\n\n"
    for cmd, info in scripts.items():
        text += f"• `{cmd}` - {info['description']}\n"
        text += f"  _Автор: @{info['author']}_\n\n"
    
    await update.message.reply_text(text, parse_mode='Markdown')

async def delete_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удалить скрипт"""
    chat_id = str(update.effective_chat.id)
    
    if not context.args:
        await update.message.reply_text("❌ Укажите команду для удаления: `/deletescript /команда`", parse_mode='Markdown')
        return
    
    command = context.args[0].lower()
    if not command.startswith('/'):
        command = '/' + command
    
    # Удаляем из БД
    if delete_script_from_db(chat_id, command):
        # Удаляем из кэша
        if chat_id in scripts_registry and command in scripts_registry[chat_id]:
            del scripts_registry[chat_id][command]
        
        await update.message.reply_text(f"✅ Скрипт `{command}` удалён!", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ Скрипт `{command}` не найден!", parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помощь"""
    await update.message.reply_text(
        "📖 *Справка по боту*\n\n"
        "*Команды управления:*\n"
        "`/addscript` - Добавить скрипт\n"
        "`/listscripts` - Список скриптов\n"
        "`/viewscript /cmd` - Посмотреть код\n"
        "`/editscript /cmd` - Редактировать\n"
        "`/deletescript /cmd` - Удалить\n"
        "`/cancel` - Отменить действие\n\n"
        "*Как добавить скрипт:*\n"
        "1. Введите `/addscript`\n"
        "2. Отправьте код (можно частями!)\n"
        "3. Напишите `готово` когда закончите\n\n"
        "*Формат скрипта:*\n"
        "```\n"
        "###COMMAND: mycommand\n"
        "###DESCRIPTION: Описание\n"
        "###CODE:\n"
        "async def execute(update, context, args):\n"
        "    return 'Привет!'\n"
        "```\n\n"
        "🔓 Все модули Python разрешены!",
        parse_mode='Markdown'
    )

async def run_triggers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск триггер-скриптов на каждое сообщение"""
    chat_id = str(update.effective_chat.id)
    
    # Получаем скрипты чата из БД
    scripts = get_chat_scripts(chat_id)
    
    if not scripts:
        return
    
    # Ищем скрипты с функцией check_triggers
    for cmd in scripts.keys():
        script_info = get_script_from_db(chat_id, cmd)
        if not script_info:
            continue
        
        script_code = script_info['code']
        
        # Проверяем, есть ли функция check_triggers
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
            
            # Импортируем модули
            for mod in ['math','random','datetime','re','json','os','sys','subprocess',
                        'requests','asyncio','aiohttp','time','sqlite3','hashlib','base64','pathlib']:
                try: local_namespace[mod] = __import__(mod)
                except: pass
            
            exec(script_code, local_namespace)
            
            if 'check_triggers' in local_namespace:
                await local_namespace['check_triggers'](update, context)
        except Exception as e:
            print(f"Trigger error in {cmd}: {e}")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик callback-кнопок из пользовательских скриптов"""
    query = update.callback_query
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    callback_data = query.data
    
    # Получаем все скрипты чата
    scripts = get_chat_scripts(chat_id)
    
    if not scripts:
        await query.answer("Скрипты не найдены")
        return
    
    # Ищем скрипты с функцией handle_callback
    for cmd in scripts.keys():
        script_info = get_script_from_db(chat_id, cmd)
        if not script_info:
            continue
        
        script_code = script_info['code']
        
        # Проверяем, есть ли функция handle_callback
        if 'async def handle_callback' not in script_code and 'def handle_callback' not in script_code:
            continue
        
        try:
            import builtins
            local_namespace = {
                '__builtins__': builtins,
                'update': update,
                'context': context,
                'query': query,
                'callback_data': callback_data,
                'DATA_DIR': DATA_DIR,
                'DB_PATH': DB_PATH,
                'InlineKeyboardButton': InlineKeyboardButton,
                'InlineKeyboardMarkup': InlineKeyboardMarkup,
            }
            
            # Импортируем модули
            for mod in ['math','random','datetime','re','json','os','sys','subprocess',
                        'requests','asyncio','aiohttp','time','sqlite3','hashlib','base64','pathlib']:
                try: local_namespace[mod] = __import__(mod)
                except: pass
            
            exec(script_code, local_namespace)
            
            if 'handle_callback' in local_namespace:
                # Передаём callback_data в функцию
                result = await local_namespace['handle_callback'](update, context, callback_data)
                if result:
                    # Если функция вернула True, значит она обработала callback
                    return
        except Exception as e:
            print(f"Callback error in {cmd}: {e}")
            await query.answer(f"Ошибка: {str(e)[:50]}")
    
    # Если никто не обработал
    await query.answer()

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Общий обработчик сообщений"""
    # Сначала проверяем, ожидаем ли скрипт
    if await handle_script_upload(update, context):
        return
    
    # Запускаем проверку триггеров на каждое сообщение
    await run_triggers(update, context)
    
    # Затем проверяем кастомные команды
    if update.message.text and update.message.text.startswith('/'):
        await execute_custom_script(update, context)

def main():
    """Запуск бота"""
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Системные команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addscript", add_script))
    application.add_handler(CommandHandler("listscripts", list_scripts))
    application.add_handler(CommandHandler("viewscript", view_script))
    application.add_handler(CommandHandler("editscript", edit_script))
    application.add_handler(CommandHandler("deletescript", delete_script))
    application.add_handler(CommandHandler("cancel", cancel_action))
    application.add_handler(CommandHandler("help", help_command))
    
    # Обработчик всех сообщений (для скриптов и кастомных команд)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_handler(MessageHandler(filters.COMMAND, execute_custom_script))
    
    # Обработчик callback-кнопок (inline buttons)
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    
    print("🤖 Бот запущен!")
    application.run_polling()

if __name__ == "__main__":
    main()
