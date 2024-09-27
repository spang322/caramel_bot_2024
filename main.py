import config
import logging
import os
from datetime import datetime, timedelta
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputMediaPhoto,
    InputMedia,
    CallbackQuery,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
)
import pymongo
import asyncio

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# Настройка MongoDB
client = pymongo.MongoClient(config.MONGO)
db = client["club_bot_db"]
users_col = db["users"]
registration_requests_col = db["registration_requests"]
payment_requests_col = db["payment_requests"]
equipment_col = db["equipment"]  # Новая коллекция для оборудования

# Предопределенные имена членов клуба
with open('names.txt', 'r', encoding='utf-8') as file:
    club_member_names = file.read().splitlines()

# Сортируем фамилии в алфавитном порядке
club_member_names.sort()

# Секретные фразы для админа
admin_secret_phrases = [config.PWD]

# Необходимая сумма оплаты за период (например, ежемесячно)
required_payment = config.PAYMENT  # Настройте по необходимости

# Начало платежного периода с октября 2023
payment_start_date = config.START_DATE

# Состояния разговора
(
    CHOOSING_NAME,
    CONFIRM_REGISTRATION,
    ASK_SECRET,
    ENTER_SECRET,
    PAYMENT_AMOUNT,
    UPLOAD_PHOTO,
    NOTIFY_MESSAGE,
    PAYMENT_DENY_COMMENT,
    EQUIPMENT_ACTION,
    ADD_EQUIPMENT_NAME,
    ADD_EQUIPMENT_DESCRIPTION,
    REQUEST_EQUIPMENT_ITEM,
) = range(12)

# Убедитесь, что каталог для квитанций существует
if not os.path.exists('receipts'):
    os.makedirs('receipts')

# Функция отмены
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

# Обработчик команды /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received /start command from user {update.effective_user.id}")
    await update.message.reply_text(
        "Добро пожаловать в Карамельный Бот!\nИспользуйте /register для "
        "регистрации или /help для списка команд."
    )

# Обработчик команды /help
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received /help command from user {update.effective_user.id}")
    await update.message.reply_text(
        "Доступные команды:\n"
        "/register - Зарегистрироваться как пользователь или администратор\n"
        "/payment - Отправить запрос на платеж\n"
        "/balance - Проверить свой баланс\n"
        "/equipment - Взаимодействие с отрядным имуществом\n"
        "/admin - Доступ к функциям администратора (только для админов)"
    )

# Обработчик неизвестных команд
async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received unknown command from user {update.effective_user.id}")
    await update.message.reply_text(
        "Извините, я не понимаю эту команду.\nПожалуйста, используйте /help для списка доступных команд."
    )

# Регистрация пользователя
async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = users_col.find_one({"telegram_id": user_id})
    if user:
        await update.message.reply_text("Вы уже зарегистрированы.")
        return ConversationHandler.END

    keyboard = [[KeyboardButton(name)] for name in club_member_names]
    reply_markup = ReplyKeyboardMarkup(
        keyboard, one_time_keyboard=True, resize_keyboard=True
    )
    await update.message.reply_text(
        "Пожалуйста, выберите свое имя из списка:", reply_markup=reply_markup
    )
    return CHOOSING_NAME

async def choose_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text
    if name not in club_member_names:
        await update.message.reply_text("Неверное имя. Регистрация не удалась.")
        return ConversationHandler.END

    existing_user = users_col.find_one({"name": name})
    if existing_user:
        # Отправить запрос на регистрацию администратору
        registration_requests_col.insert_one({
            "name": name,
            "telegram_id": update.effective_user.id,
            "status": "pending"
        })
        await update.message.reply_text(
            "Это имя уже зарегистрировано.\nВаш запрос отправлен "
            "администратору на одобрение."
        )
        return ConversationHandler.END
    else:
        context.user_data['name'] = name
        keyboard = [["Да", "Нет"]]
        reply_markup = ReplyKeyboardMarkup(
            keyboard, one_time_keyboard=True, resize_keyboard=True
        )
        await update.message.reply_text(
            "Вы хотите зарегистрироваться как администратор?",
            reply_markup=reply_markup
        )
        return ASK_SECRET

async def ask_secret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    response = update.message.text
    if response == "Да":
        await update.message.reply_text("Пожалуйста, введите секретную фразу:")
        return ENTER_SECRET
    else:
        # Регистрация как обычный пользователь
        users_col.insert_one({
            "name": context.user_data['name'],
            "telegram_id": update.effective_user.id,
            "is_admin": False,
            "amount_paid": 0
        })
        await update.message.reply_text("Вы успешно зарегистрировались как пользователь.")
        return ConversationHandler.END

async def enter_secret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    secret_phrase = update.message.text
    if secret_phrase in admin_secret_phrases:
        # Регистрация как администратор
        users_col.insert_one({
            "name": context.user_data['name'],
            "telegram_id": update.effective_user.id,
            "is_admin": True,
            "amount_paid": 0
        })
        await update.message.reply_text("Вы успешно зарегистрировались как администратор.")
    else:
        await update.message.reply_text("Неверная секретная фраза. Регистрация не удалась.")
    return ConversationHandler.END

# Обработчик платежей
async def payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = users_col.find_one({"telegram_id": user_id})
    if not user:
        await update.message.reply_text("Сначала вам нужно зарегистрироваться, используя /register.")
        return ConversationHandler.END

    await update.message.reply_text("Пожалуйста, введите сумму, которую вы хотите оплатить:")
    return PAYMENT_AMOUNT

async def payment_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        context.user_data['amount'] = amount
        await update.message.reply_text("Пожалуйста, загрузите фотографию вашего платежного чека:")
        return UPLOAD_PHOTO
    except ValueError:
        await update.message.reply_text("Неверная сумма. Пожалуйста, введите числовое значение.")
        return PAYMENT_AMOUNT

async def upload_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Пожалуйста, загрузите фотографию.")
        return UPLOAD_PHOTO

    photo = update.message.photo[-1]
    file = await photo.get_file()
    file_path = f"receipts/{update.effective_user.id}_{photo.file_unique_id}.jpg"
    await file.download_to_drive(custom_path=file_path)

    # Создать запрос на платеж
    payment_requests_col.insert_one({
        "telegram_id": update.effective_user.id,
        "amount": context.user_data['amount'],
        "receipt_path": file_path,
        "status": "pending"
    })
    await update.message.reply_text("Ваш запрос на платеж отправлен на одобрение.")
    return ConversationHandler.END

# Проверка баланса
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = users_col.find_one({"telegram_id": user_id})
    if not user:
        await update.message.reply_text("Сначала вам нужно зарегистрироваться, используя /register.")
        return

    # Вычисление количества месяцев с начала периода
    today = datetime.now()
    delta = today - payment_start_date
    months_since_start = delta.days // 30  # Приблизительное количество месяцев
    months_since_start = months_since_start % 12  # Сброс после 12 месяцев

    total_required_payment = required_payment * (months_since_start + 1)

    amount_paid = user.get('amount_paid', 0)
    balance_amount = amount_paid - total_required_payment

    if balance_amount >= 0:
        next_payment_date = payment_start_date + timedelta(days=30 * (months_since_start + 1))
        next_payment_date_str = next_payment_date.strftime("%d %B %Y")
        await update.message.reply_text(
            f"Ваши платежи актуальны!\nСледующий платеж должен быть внесен "
            f"{next_payment_date_str}.\nСпасибо!"
        )
    else:
        debt = abs(balance_amount)
        await update.message.reply_text(
            f"Вы должны {debt} единиц.\nПожалуйста, совершите платеж для погашения долга."
        )

# Управление оборудованием
async def equipment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Добавить вещь", callback_data='add_equipment')],
        [InlineKeyboardButton("Просмотр списка имущества", callback_data='view_equipment')],
        [InlineKeyboardButton("Запросить вещь", callback_data='request_equipment')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Меню взаимодействия с отрядным имуществом:", reply_markup=reply_markup)
    return EQUIPMENT_ACTION

async def equipment_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == 'add_equipment':
        await query.edit_message_text("Пожалуйста, введите название вещи:")
        return ADD_EQUIPMENT_NAME
    elif action == 'view_equipment':
        items = list(equipment_col.find())
        if not items:
            await query.edit_message_text("Вещь не найдена.")
        else:
            equipment_list = "\n".join([f"{item['name']}: {item['description']}" for item in items])
            await query.edit_message_text(f"Список вещей:\n{equipment_list}")
        return ConversationHandler.END
    elif action == 'request_equipment':
        items = list(equipment_col.find({"available": True}))
        if not items:
            await query.edit_message_text("Нет доступной вещи для запроса.")
            return ConversationHandler.END
        equipment_names = [item['name'] for item in items]
        keyboard = [[KeyboardButton(name)] for name in equipment_names]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        await query.message.reply_text("Пожалуйста, выберите вещь, которую вы хотите запросить:", reply_markup=reply_markup)
        return REQUEST_EQUIPMENT_ITEM

async def add_equipment_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text
    context.user_data['equipment_name'] = name
    await update.message.reply_text("Пожалуйста, введите описание вещи:")
    return ADD_EQUIPMENT_DESCRIPTION

async def add_equipment_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    description = update.message.text
    name = context.user_data.get('equipment_name')

    # Добавление оборудования в базу данных
    equipment_col.insert_one({
        "name": name,
        "description": description,
        "available": True,
    })
    await update.message.reply_text(f"Вещь '{name}' успешно добавлена.")
    return ConversationHandler.END

async def request_equipment_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text
    item = equipment_col.find_one({"name": name})
    if not item:
        await update.message.reply_text("Неверное название вещи.")
        return ConversationHandler.END

    if not item.get('available', True):
        await update.message.reply_text("Эта вещь в настоящее время недоступна.")
        return ConversationHandler.END

    # Отметить оборудование как недоступное
    equipment_col.update_one({"name": name}, {'$set': {'available': False}})
    await update.message.reply_text(
        f"Вы запросили '{name}'. Пожалуйста, свяжитесь с администратором для дальнейших инструкций."
    )
    return ConversationHandler.END

# Функции администратора
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = users_col.find_one({"telegram_id": user_id, "is_admin": True})
    if not user:
        await update.message.reply_text("Доступ запрещен. Только для администраторов.")
        return

    keyboard = [
        [InlineKeyboardButton("Управление запросами на регистрацию", callback_data='manage_registrations')],
        [InlineKeyboardButton("Управление запросами на платежи", callback_data='manage_payments')],
        [InlineKeyboardButton("Список всех зарегистрированных пользователей", callback_data='list_registered')],
        [InlineKeyboardButton("Список всех незарегистрированных пользователей", callback_data='list_unregistered')],
        [InlineKeyboardButton("Уведомить пользователей", callback_data='notify_users')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Меню администратора:", reply_markup=reply_markup)

async def admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'manage_registrations':
        await manage_registrations(query, context)
    elif data == 'manage_payments':
        await manage_payments(query, context)
    elif data == 'list_registered':
        await list_registered_users(query, context)
    elif data == 'list_unregistered':
        await list_unregistered_users(query, context)
    elif data == 'notify_users':
        await notify_users_start(query, context)

# Управление запросами на регистрацию
async def manage_registrations(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    requests = list(registration_requests_col.find({"status": "pending"}))
    if not requests:
        await query.edit_message_text("Нет ожидающих запросов на регистрацию.")
        return

    context.user_data['registration_requests'] = requests
    context.user_data['current_request_index'] = 0
    await show_registration_request(query, context)

async def show_registration_request(query, context):
    index = context.user_data['current_request_index']
    requests = context.user_data['registration_requests']
    if index >= len(requests):
        await query.edit_message_text("Нет больше запросов на регистрацию.")
        return

    request = requests[index]

    keyboard = [
        [
            InlineKeyboardButton("Одобрить", callback_data='approve_registration'),
            InlineKeyboardButton("Отклонить", callback_data='deny_registration'),
            InlineKeyboardButton("Отложить", callback_data='postpone_registration'),
        ],
        [InlineKeyboardButton("Прекратить управление", callback_data='stop_managing_registrations')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = f"Запрос на регистрацию:\nИмя: {request['name']}\nTelegram ID: {request['telegram_id']}"
    await query.edit_message_text(text, reply_markup=reply_markup)

async def handle_registration_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data

    index = context.user_data['current_request_index']
    requests = context.user_data['registration_requests']
    if index >= len(requests):
        await query.edit_message_text("Нет больше запросов на регистрацию.")
        return ConversationHandler.END

    request = requests[index]
    user_chat_id = request['telegram_id']

    if action == 'approve_registration':
        users_col.insert_one({
            "name": request['name'],
            "telegram_id": request['telegram_id'],
            "is_admin": False,
            "amount_paid": 0
        })
        registration_requests_col.update_one({'_id': request['_id']}, {'$set': {'status': 'approved'}})
        await context.bot.send_message(user_chat_id, "Ваша регистрация одобрена.")
        await query.edit_message_text("Регистрация одобрена.")
        context.user_data['current_request_index'] += 1
        await show_registration_request(query, context)
    elif action == 'deny_registration':
        registration_requests_col.update_one({'_id': request['_id']}, {'$set': {'status': 'denied'}})
        await context.bot.send_message(user_chat_id, "Ваша регистрация отклонена.")
        await query.edit_message_text("Регистрация отклонена.")
        context.user_data['current_request_index'] += 1
        await show_registration_request(query, context)
    elif action == 'postpone_registration':
        # Переместить в конец списка
        context.user_data['registration_requests'].append(context.user_data['registration_requests'].pop(index))
        await query.edit_message_text("Регистрация отложена.")
        await show_registration_request(query, context)
    elif action == 'stop_managing_registrations':
        await query.edit_message_text("Управление запросами на регистрацию остановлено.")
        return ConversationHandler.END

# Управление запросами на платежи
async def manage_payments(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    requests = list(payment_requests_col.find({"status": "pending"}))
    if not requests:
        await query.edit_message_text("Нет ожидающих запросов на платежи.")
        return

    context.user_data['payment_requests'] = requests
    context.user_data['current_payment_index'] = 0
    await show_payment_request(query, context)

async def show_payment_request(query_or_update, context):
    index = context.user_data['current_payment_index']
    requests = context.user_data['payment_requests']
    if index >= len(requests):
        if isinstance(query_or_update, Update):
            await query_or_update.message.reply_text("Нет больше запросов на платежи.")
        else:
            await query_or_update.edit_message_text("Нет больше запросов на платежи.")
        return

    request = requests[index]

    keyboard = [
        [
            InlineKeyboardButton("Одобрить", callback_data='approve_payment'),
            InlineKeyboardButton("Отклонить", callback_data='deny_payment'),
            InlineKeyboardButton("Отложить", callback_data='postpone_payment'),
        ],
        [InlineKeyboardButton("Прекратить управление", callback_data='stop_managing_payments')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = f"Запрос на платеж:\nID пользователя: {request['telegram_id']}\nСумма: {request['amount']}"

    # Отправка фото квитанции с подписью и инлайн-клавиатурой
    try:
        with open(request['receipt_path'], 'rb') as photo_file:
            # Удаляем предыдущее сообщение и отправляем новое
            if isinstance(query_or_update, CallbackQuery):
                await query_or_update.message.delete()
                sent_message = await query_or_update.message.chat.send_photo(
                    photo=photo_file,
                    caption=text,
                    reply_markup=reply_markup
                )
                context.user_data['admin_message_id'] = sent_message.message_id
            elif isinstance(query_or_update, Update):
                sent_message = await query_or_update.message.reply_photo(
                    photo=photo_file,
                    caption=text,
                    reply_markup=reply_markup
                )
                context.user_data['admin_message_id'] = sent_message.message_id
    except FileNotFoundError:
        if isinstance(query_or_update, CallbackQuery):
            await query_or_update.edit_message_text("Изображение квитанции не найдено.")
        elif isinstance(query_or_update, Update):
            await query_or_update.message.reply_text("Изображение квитанции не найдено.")

async def handle_payment_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data

    index = context.user_data['current_payment_index']
    requests = context.user_data['payment_requests']
    if index >= len(requests):
        await context.bot.send_message(chat_id=query.from_user.id, text="Нет больше запросов на платежи.")
        return ConversationHandler.END

    request = requests[index]
    user_chat_id = request['telegram_id']

    if action == 'approve_payment':
        # Одобрить платеж
        users_col.update_one(
            {"telegram_id": request['telegram_id']},
            {'$inc': {'amount_paid': request['amount']}}
        )
        payment_requests_col.update_one({'_id': request['_id']}, {'$set': {'status': 'approved'}})
        await context.bot.send_message(user_chat_id, "Ваш платеж одобрен.")
        context.user_data['current_payment_index'] += 1
        # Показать следующий запрос
        await show_payment_request(update, context)
    elif action == 'deny_payment':
        # Отклонить платеж и запросить комментарий
        context.user_data['denied_request'] = request
        await context.bot.send_message(chat_id=query.from_user.id, text="Пожалуйста, введите комментарий для отказа:")
        return PAYMENT_DENY_COMMENT
    elif action == 'postpone_payment':
        # Отложить платеж
        context.user_data['payment_requests'].append(context.user_data['payment_requests'].pop(index))
        await context.bot.send_message(chat_id=query.from_user.id, text="Платеж отложен.")
        # Показать следующий запрос
        await show_payment_request(update, context)
    elif action == 'stop_managing_payments':
        await context.bot.send_message(chat_id=query.from_user.id, text="Управление запросами на платежи остановлено.")
        return ConversationHandler.END

async def handle_payment_denial_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text
    request = context.user_data['denied_request']
    user_chat_id = request['telegram_id']

    # Обновить статус запроса на платеж и добавить комментарий
    payment_requests_col.update_one(
        {'_id': request['_id']},
        {'$set': {'status': 'denied', 'comment': comment}}
    )

    # Уведомить пользователя
    await context.bot.send_message(
        user_chat_id,
        f"Ваш платеж отклонен. Комментарий от администратора: {comment}"
    )

    await update.message.reply_text("Платеж отклонен, и пользователь уведомлен с вашим комментарием.")
    context.user_data['current_payment_index'] += 1

    # Продолжить с следующим запросом
    await show_payment_request(update, context)
    return ConversationHandler.END

# Список зарегистрированных пользователей
async def list_registered_users(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    users = list(users_col.find())
    if not users:
        await query.edit_message_text("Зарегистрированных пользователей не найдено.")
        return

    user_list = "\n".join([user['name'] for user in users])
    await query.edit_message_text(f"Зарегистрированные пользователи:\n{user_list}")

# Список незарегистрированных пользователей
async def list_unregistered_users(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    registered_names = [user['name'] for user in users_col.find()]
    unregistered_names = [name for name in club_member_names if name not in registered_names]

    if not unregistered_names:
        await query.edit_message_text("Все члены клуба зарегистрированы.")
        return

    user_list = "\n".join(unregistered_names)
    await query.edit_message_text(f"Незарегистрированные члены клуба:\n{user_list}")

# Уведомление пользователей
async def notify_users_start(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("Все", callback_data='notify_all')],
        [InlineKeyboardButton("Должники", callback_data='notify_debtors')],
        [InlineKeyboardButton("Не должники", callback_data='notify_not_debtors')],
        [InlineKeyboardButton("Отмена", callback_data='notify_cancel')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Выберите категорию пользователей для уведомления:", reply_markup=reply_markup)
    return NOTIFY_MESSAGE

async def notify_users_category_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data

    if category == 'notify_cancel':
        await query.edit_message_text("Уведомление отменено.")
        return ConversationHandler.END

    context.user_data['notify_category'] = category
    await query.edit_message_text("Пожалуйста, введите сообщение для отправки:")
    return NOTIFY_MESSAGE

async def notify_users_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text
    category = context.user_data.get('notify_category')

    if category == 'notify_all':
        users = users_col.find()
    elif category == 'notify_debtors':
        # Пользователи, у которых сумма оплаты меньше требуемой
        users = users_col.find({"amount_paid": {"$lt": required_payment}})
    elif category == 'notify_not_debtors':
        users = users_col.find({"amount_paid": {"$gte": required_payment}})
    else:
        await update.message.reply_text("Неверная категория.")
        return ConversationHandler.END

    user_ids = [user['telegram_id'] for user in users]
    for user_id in user_ids:
        try:
            await context.bot.send_message(chat_id=user_id, text=message)
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    await update.message.reply_text("Уведомление отправлено.")
    return ConversationHandler.END

# Главная функция
def main():
    # Создайте приложение и передайте токен вашего бота
    app = ApplicationBuilder().token(config.TOKEN).build()

    # Обработчик разговоров для регистрации пользователя
    registration_conv = ConversationHandler(
        entry_points=[CommandHandler('register', register)],
        states={
            CHOOSING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_name)],
            ASK_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_secret)],
            ENTER_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_secret)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # Обработчик разговоров для запросов на платеж
    payment_conv = ConversationHandler(
        entry_points=[CommandHandler('payment', payment)],
        states={
            PAYMENT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_amount)],
            UPLOAD_PHOTO: [MessageHandler(filters.PHOTO, upload_photo)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # Обработчик разговоров для управления оборудованием
    equipment_conv = ConversationHandler(
        entry_points=[CommandHandler('equipment', equipment)],
        states={
            EQUIPMENT_ACTION: [CallbackQueryHandler(equipment_menu, pattern='^(add_equipment|view_equipment|request_equipment)$')],
            ADD_EQUIPMENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_equipment_name)],
            ADD_EQUIPMENT_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_equipment_description)],
            REQUEST_EQUIPMENT_ITEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, request_equipment_item)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # Обработчик разговоров для управления регистрациями
    registration_management_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_registration_decision, pattern='^(approve_registration|deny_registration|postpone_registration|stop_managing_registrations)$')],
        states={},
        fallbacks=[CommandHandler('cancel', cancel)],
        per_user=True,
        per_chat=True,
        per_message=False,
    )

    # Обработчик разговоров для управления платежами
    payment_management_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_payment_decision, pattern='^(approve_payment|deny_payment|postpone_payment|stop_managing_payments)$')],
        states={
            PAYMENT_DENY_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payment_denial_comment)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_user=True,
        per_chat=True,
        per_message=False,
    )

    # Обработчик разговоров для уведомлений
    notify_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(notify_users_category_selected, pattern='^(notify_all|notify_debtors|notify_not_debtors|notify_cancel)$')],
        states={
            NOTIFY_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, notify_users_message)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_user=True,
        per_chat=False,
        per_message=False,
    )

    # Добавьте обработчики в приложение
    # Порядок добавления имеет значение
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('balance', balance))
    app.add_handler(CommandHandler('admin', admin_menu))
    app.add_handler(CommandHandler('cancel', cancel))

    # Добавьте обработчики разговоров
    app.add_handler(registration_conv)
    app.add_handler(payment_conv)
    app.add_handler(equipment_conv)
    app.add_handler(payment_management_conv)
    app.add_handler(registration_management_conv)
    app.add_handler(notify_conv)

    # Добавьте обработчики CallbackQuery
    app.add_handler(CallbackQueryHandler(admin_button, pattern='^(manage_registrations|manage_payments|list_registered|list_unregistered|notify_users)$'))
    app.add_handler(CallbackQueryHandler(handle_registration_decision, pattern='^(approve_registration|deny_registration|postpone_registration|stop_managing_registrations)$'))
    app.add_handler(CallbackQueryHandler(handle_payment_decision, pattern='^(approve_payment|deny_payment|postpone_payment|stop_managing_payments)$'))
    app.add_handler(CallbackQueryHandler(notify_users_category_selected, pattern='^(notify_all|notify_debtors|notify_not_debtors|notify_cancel)$'))
    app.add_handler(CallbackQueryHandler(equipment_menu, pattern='^(add_equipment|view_equipment|request_equipment)$'))

    # Обработчик неизвестных команд должен быть добавлен последним
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # Запуск бота
    app.run_polling()

if __name__ == '__main__':
    main()
