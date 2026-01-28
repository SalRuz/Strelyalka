import os
import json
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from datetime import datetime

# Конфигурация
BOT_TOKEN = "8512207770:AAEKLtYEph7gleybGhF2lc7Gwq82Kj1yedM"
SCRIPTS_DIR = "custom_scripts"
SCRIPTS_DB = "scripts_registry.json"

# Создаем директорию для скриптов
os.makedirs(SCRIPTS_DIR, exist_ok=True)

# Загрузка реестра скриптов
def load_scripts_registry():
    if os.path.exists(SCRIPTS_DB):
        with open(SCRIPTS_DB, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_scripts_registry(registry):
    with open(SCRIPTS_DB, 'w', encoding='utf-8') as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)

# Глобальный реестр скриптов {chat_id: {command: script_info}}
scripts_registry = load_scripts_registry()

# Состояния ожидания скрипта
waiting_for_script = {}  # {user_id: chat_id}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветственное сообщение"""
    await update.message.reply_text(
        "🤖 *Привет! Я бот с кастомными скриптами!*\n\n"
        "📌 *Доступные команды:*\n"
        "`/addscript` - Добавить новый скрипт\n"
        "`/listscripts` - Список скриптов чата\n"
        "`/deletescript <команда>` - Удалить скрипт\n"
        "`/help` - Помощь\n\n"
        "💡 Вы можете создавать свои команды!",
        parse_mode='Markdown'
    )

async def add_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало процесса добавления скрипта"""
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    
    waiting_for_script[user_id] = chat_id
    
    await update.message.reply_text(
        "📝 *Отправьте скрипт в следующем формате:*\n\n"
        "```\n"
        "###COMMAND: название_команды\n"
        "###DESCRIPTION: описание\n"
        "###CODE:\n"
        "# Ваш Python код здесь\n"
        "async def execute(update, context, args):\n"
        "    # args - аргументы после команды\n"
        "    return 'Результат'\n"
        "```\n\n"
        "⚠️ Функция `execute` обязательна!",
        parse_mode='Markdown'
    )

async def handle_script_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка загруженного скрипта"""
    user_id = update.effective_user.id
    
    if user_id not in waiting_for_script:
        return False
    
    chat_id = waiting_for_script.pop(user_id)
    text = update.message.text
    
    try:
        # Парсинг скрипта
        lines = text.strip().split('\n')
        command = None
        description = "Без описания"
        code_lines = []
        in_code = False
        
        for line in lines:
            if line.startswith('###COMMAND:'):
                command = line.replace('###COMMAND:', '').strip().lower()
                if not command.startswith('/'):
                    command = '/' + command
            elif line.startswith('###DESCRIPTION:'):
                description = line.replace('###DESCRIPTION:', '').strip()
            elif line.startswith('###CODE:'):
                in_code = True
            elif in_code:
                code_lines.append(line)
        
        if not command:
            await update.message.reply_text("❌ Не указана команда (###COMMAND:)")
            return True
        
        code = '\n'.join(code_lines)
        
        if 'async def execute' not in code and 'def execute' not in code:
            await update.message.reply_text("❌ Не найдена функция execute!")
            return True
        
        # Сохранение скрипта
        script_filename = f"{chat_id}_{command.replace('/', '')}.py"
        script_path = os.path.join(SCRIPTS_DIR, script_filename)
        
        with open(script_path, 'w', encoding='utf-8') as f:
            f.write(code)
        
        # Обновление реестра
        if chat_id not in scripts_registry:
            scripts_registry[chat_id] = {}
        
        scripts_registry[chat_id][command] = {
            'description': description,
            'filename': script_filename,
            'author': update.effective_user.username or str(user_id),
            'created': datetime.now().isoformat()
        }
        
        save_scripts_registry(scripts_registry)
        
        await update.message.reply_text(
            f"✅ *Скрипт успешно добавлен!*\n\n"
            f"📌 Команда: `{command}`\n"
            f"📝 Описание: {description}\n\n"
            f"Теперь вы можете использовать `{command}` в этом чате!",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при обработке скрипта: {str(e)}")
    
    return True

async def execute_custom_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнение кастомного скрипта"""
    chat_id = str(update.effective_chat.id)
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
    
    # Проверяем наличие скрипта
    if chat_id not in scripts_registry:
        return False
    if command not in scripts_registry[chat_id]:
        return False
    
    script_info = scripts_registry[chat_id][command]
    script_path = os.path.join(SCRIPTS_DIR, script_info['filename'])
    
    if not os.path.exists(script_path):
        await update.message.reply_text("❌ Файл скрипта не найден!")
        return True
    
    try:
        # Загружаем и выполняем скрипт
        with open(script_path, 'r', encoding='utf-8') as f:
            script_code = f.read()
        
        # Создаем локальное пространство имен с полным доступом
        import builtins
        local_namespace = {
            '__builtins__': builtins,  # Полный доступ ко всем встроенным функциям
            'update': update,
            'context': context,
            'args': args,
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
                await update.message.reply_text(str(result), parse_mode='Markdown')
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка выполнения скрипта:\n`{str(e)}`", parse_mode='Markdown')
    
    return True

async def list_scripts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список скриптов чата"""
    chat_id = str(update.effective_chat.id)
    
    if chat_id not in scripts_registry or not scripts_registry[chat_id]:
        await update.message.reply_text("📭 В этом чате пока нет кастомных скриптов.")
        return
    
    text = "📜 *Кастомные скрипты этого чата:*\n\n"
    for cmd, info in scripts_registry[chat_id].items():
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
    
    if chat_id in scripts_registry and command in scripts_registry[chat_id]:
        script_info = scripts_registry[chat_id].pop(command)
        save_scripts_registry(scripts_registry)
        
        # Удаляем файл
        script_path = os.path.join(SCRIPTS_DIR, script_info['filename'])
        if os.path.exists(script_path):
            os.remove(script_path)
        
        await update.message.reply_text(f"✅ Скрипт `{command}` удалён!", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ Скрипт `{command}` не найден!", parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помощь"""
    await update.message.reply_text(
        "📖 *Справка по боту*\n\n"
        "*Как добавить свой скрипт:*\n"
        "1. Введите `/addscript`\n"
        "2. Отправьте скрипт в нужном формате\n"
        "3. Используйте новую команду!\n\n"
        "*Формат скрипта:*\n"
        "```\n"
        "###COMMAND: mycommand\n"
        "###DESCRIPTION: Мой скрипт\n"
        "###CODE:\n"
        "import requests  # любые импорты!\n"
        "async def execute(update, context, args):\n"
        "    return 'Привет!'\n"
        "```\n\n"
        "🔓 *Полный доступ:*\n"
        "Все модули Python разрешены!\n"
        "`os`, `subprocess`, `requests`, `aiohttp`, `sqlite3` и др.",
        parse_mode='Markdown'
    )

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Общий обработчик сообщений"""
    # Сначала проверяем, ожидаем ли скрипт
    if await handle_script_upload(update, context):
        return
    
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
    application.add_handler(CommandHandler("deletescript", delete_script))
    application.add_handler(CommandHandler("help", help_command))
    
    # Обработчик всех сообщений (для скриптов и кастомных команд)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_handler(MessageHandler(filters.COMMAND, execute_custom_script))
    
    print("🤖 Бот запущен!")
    application.run_polling()

if __name__ == "__main__":
    main()
