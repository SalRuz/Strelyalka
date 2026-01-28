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
        "    return 'Результат'\n"
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
    
    if chat_id not in scripts_registry or command not in scripts_registry[chat_id]:
        await update.message.reply_text(f"❌ Скрипт `{command}` не найден!", parse_mode='Markdown')
        return
    
    script_info = scripts_registry[chat_id][command]
    script_path = os.path.join(SCRIPTS_DIR, script_info['filename'])
    
    if not os.path.exists(script_path):
        await update.message.reply_text("❌ Файл скрипта не найден!")
        return
    
    with open(script_path, 'r', encoding='utf-8') as f:
        code = f.read()
    
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
    
    if chat_id not in scripts_registry or command not in scripts_registry[chat_id]:
        await update.message.reply_text(f"❌ Скрипт `{command}` не найден!", parse_mode='Markdown')
        return
    
    # Показываем текущий код
    script_info = scripts_registry[chat_id][command]
    script_path = os.path.join(SCRIPTS_DIR, script_info['filename'])
    
    with open(script_path, 'r', encoding='utf-8') as f:
        current_code = f.read()
    
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
    
    if not command:
        await update.message.reply_text("❌ Не указана команда (###COMMAND:)! Скрипт не сохранён.")
        return True
    
    if 'async def execute' not in code and 'def execute' not in code:
        await update.message.reply_text("❌ Не найдена функция execute! Скрипт не сохранён.")
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
    
    # Сохранение
    script_info = scripts_registry[chat_id][command]
    script_path = os.path.join(SCRIPTS_DIR, script_info['filename'])
    
    with open(script_path, 'w', encoding='utf-8') as f:
        f.write(code)
    
    # Обновляем описание если было новое
    if 'new_description' in editing:
        scripts_registry[chat_id][command]['description'] = editing['new_description']
    
    scripts_registry[chat_id][command]['updated'] = datetime.now().isoformat()
    save_scripts_registry(scripts_registry)
    
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
                result_str = str(result)
                # Пробуем отправить с Markdown, если ошибка - без форматирования
                try:
                    await update.message.reply_text(result_str, parse_mode='Markdown')
                except Exception:
                    # Если Markdown не работает (скобки {} и др.), отправляем как есть
                    await update.message.reply_text(result_str)
        
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
    
    if chat_id not in scripts_registry:
        return
    
    # Ищем скрипты с функцией check_triggers
    for cmd, script_info in scripts_registry[chat_id].items():
        script_path = os.path.join(SCRIPTS_DIR, script_info['filename'])
        if not os.path.exists(script_path):
            continue
        
        try:
            with open(script_path, 'r', encoding='utf-8') as f:
                script_code = f.read()
            
            # Проверяем, есть ли функция check_triggers
            if 'async def check_triggers' not in script_code and 'def check_triggers' not in script_code:
                continue
            
            import builtins
            local_namespace = {'__builtins__': builtins, 'update': update, 'context': context}
            
            # Импортируем модули
            for mod in ['math','random','datetime','re','json','os','sys','subprocess',
                        'requests','asyncio','aiohttp','time','sqlite3','hashlib','base64']:
                try: local_namespace[mod] = __import__(mod)
                except: pass
            
            exec(script_code, local_namespace)
            
            if 'check_triggers' in local_namespace:
                await local_namespace['check_triggers'](update, context)
        except Exception as e:
            print(f"Trigger error in {cmd}: {e}")

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
    
    print("🤖 Бот запущен!")
    application.run_polling()

if __name__ == "__main__":
    main()
