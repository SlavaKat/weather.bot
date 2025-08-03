import os
import httpx
import sqlite3
import json
import pytz
import types
from datetime import datetime, time as dt_time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    PicklePersistence,
)
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- Глобальные переменные и константы ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, 'weather_bot.db')
PERSISTENCE_PATH = os.path.join(SCRIPT_DIR, 'bot_persistence.pickle')
load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
OPENWEATHER_API_KEY = os.getenv('OPENWEATHER_API_KEY')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID')

# --- Состояния для ConversationHandler ---
(
    SELECTING_ACTION, 
    AWAITING_CITY, 
    AWAITING_TIME, 
    SELECTING_DAYS,
    AWAITING_FEEDBACK,
) = range(5)

# --- Инициализация базы данных ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_preferences (
        user_id INTEGER PRIMARY KEY, default_city TEXT
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS favorite_cities (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, city_name TEXT, UNIQUE(user_id, city_name)
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        city TEXT NOT NULL,
        time TEXT, -- Может быть NULL для оповещений типа 'alert_rain'
        days TEXT, -- JSON list of ints 0-6. Может быть NULL для 'alert_rain'
        forecast_type TEXT NOT NULL, -- 'daily' или 'alert_rain'
        is_active BOOLEAN DEFAULT 1
    )
    ''')
    conn.commit()
    conn.close()

# --- Функции для работы с БД (Пользователи и Избранное) ---
def set_user_default_city(user_id: int, city: str):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO user_preferences (user_id, default_city) VALUES (?, ?)', (user_id, city))
        conn.commit()

def get_user_default_city(user_id: int) -> str | None:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT default_city FROM user_preferences WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result[0] if result else None

def add_favorite_city(user_id: int, city: str):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute('INSERT INTO favorite_cities (user_id, city_name) VALUES (?, ?)', (user_id, city))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def get_favorite_cities(user_id: int) -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT city_name FROM favorite_cities WHERE user_id = ? ORDER BY city_name', (user_id,))
        return [row[0] for row in cursor.fetchall()]

# --- Функции для работы с БД (Подписки) ---
def add_subscription(user_id: int, sub_data: dict) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO subscriptions (user_id, city, time, days, forecast_type) VALUES (?, ?, ?, ?, ?)',
            (user_id, sub_data['city'], sub_data.get('time'), sub_data.get('days'), sub_data['forecast_type'])
        )
        conn.commit()
        return cursor.lastrowid

def get_user_subscriptions(user_id: int) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM subscriptions WHERE user_id = ? AND is_active = 1', (user_id,))
        return [dict(row) for row in cursor.fetchall()]

def delete_subscription(sub_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE subscriptions SET is_active = 0 WHERE id = ?', (sub_id,))
        conn.commit()

# --- Функции получения погоды ---
async def get_weather(city: str, api_key: str) -> str:
    url = f'https://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}&units=metric&lang=ru'
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            geodata = response.json()
        
        if geodata.get('cod') != 200:
            return f"Ошибка: {geodata.get('message', 'Неизвестная ошибка')}. Проверьте название города."

        lat, lon = geodata['coord']['lat'], geodata['coord']['lon']
        return await get_weather_by_coords(lat, lon, api_key, geodata['name'])

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Город '{city}' не найден. Пожалуйста, проверьте название."
        return f'Ошибка сети: {e}'
    except Exception as e:
        return f'Произошла непредвиденная ошибка: {e}'

async def get_forecast(city: str, api_key: str) -> str:
    url = f'https://api.openweathermap.org/data/2.5/forecast?q={city}&appid={api_key}&units=metric&lang=ru'
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()

        if data.get('cod') != '200':
            return f"Ошибка: {data.get('message', 'Неизвестная ошибка')}"

        forecast_by_day = {}
        for item in data['list']:
            date = datetime.fromtimestamp(item['dt']).strftime('%Y-%m-%d')
            if date not in forecast_by_day:
                forecast_by_day[date] = []
            forecast_by_day[date].append(item)
        
        message = [f"Прогноз на 5 дней для {data['city']['name']}:\n"]
        for i, (date, forecasts) in enumerate(forecast_by_day.items()):
            if i >= 5: break
            day_name = datetime.strptime(date, '%Y-%m-%d').strftime('%A').capitalize()
            day_temps = [f['main']['temp'] for f in forecasts]
            desc = max(set(f['weather'][0]['description'] for f in forecasts), key=[f['weather'][0]['description'] for f in forecasts].count)
            message.append(f"{day_name} ({date}): {min(day_temps):.0f}°C...{max(day_temps):.0f}°C, {desc.capitalize()}")
        
        return '\n'.join(message)
    except Exception as e:
        return f'Произошла ошибка: {e}'

def get_uv_index_description(uv_index: float) -> str:
    if uv_index < 3: return f"{uv_index} (Низкий)"
    if uv_index < 6: return f"{uv_index} (Средний)"
    if uv_index < 8: return f"{uv_index} (Высокий)"
    if uv_index < 11: return f"{uv_index} (Очень высокий)"
    return f"{uv_index} (Экстремальный)"

async def get_one_call_data(lat: float, lon: float, api_key: str) -> dict:
    url = f'https://api.openweathermap.org/data/2.5/onecall?lat={lat}&lon={lon}&exclude=minutely,alerts&appid={api_key}&units=metric&lang=ru'
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()

async def get_weather_by_coords(latitude: float, longitude: float, api_key: str, city_name: str = None) -> str:
    try:
        url = f'https://api.openweathermap.org/data/2.5/weather?lat={latitude}&lon={longitude}&appid={api_key}&units=metric&lang=ru'
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            
        if not city_name:
            city_name = data.get('name', 'Неизвестное место')
            
        main_data = data['main']
        weather_data = data['weather'][0]
        wind_speed = data['wind']['speed']
        sunrise = datetime.fromtimestamp(data['sys']['sunrise']).strftime('%H:%M')
        sunset = datetime.fromtimestamp(data['sys']['sunset']).strftime('%H:%M')
        
        return (
            f'Погода в {city_name}:\n'
            f'🌡️ Температура: {main_data["temp"]:.1f}°C (ощущается как {main_data["feels_like"]:.1f}°C)\n'
            f'📝 Описание: {weather_data["description"].capitalize()}\n'
            f'💧 Влажность: {main_data["humidity"]}%\n'
            f'💨 Скорость ветра: {wind_speed} м/с\n'
            f'🌅 Восход: {sunrise} | 🌇 Закат: {sunset}'
        )
    except Exception as e:
        return f'Произошла ошибка при получении погоды. Пожалуйста, попробуйте позже.'

async def get_hourly_forecast(city: str, api_key: str) -> str:
    url = f'https://api.openweathermap.org/data/2.5/forecast?q={city}&appid={api_key}&units=metric&lang=ru'
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()

        if data.get('cod') != '200':
            return f"Ошибка: {data.get('message', 'Неизвестная ошибка')}"

        city_name = data['city']['name']
        message = [f'Почасовой прогноз в {city_name} на 24 часа:\n']

        for item in data['list'][:8]: 
            time_str = datetime.fromtimestamp(item['dt']).strftime('%H:%M')
            temp = item['main']['temp']
            description = item['weather'][0]['description']
            message.append(f"{time_str}: {temp:.1f}°C, {description.capitalize()}")
        
        return '\n'.join(message)
    except Exception as e:
        return f'Произошла ошибка: {e}'

# --- Основные команды и обработчики ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("Текущая погода", callback_data='ask_city_weather')],
        [InlineKeyboardButton("Прогноз на 5 дней", callback_data='ask_city_forecast')],
        [InlineKeyboardButton("Почасовой прогноз", callback_data='ask_city_hourly')],
        [InlineKeyboardButton("📍 Погода по местоположению", callback_data='get_weather_by_location')],
        [InlineKeyboardButton("Избранные города", callback_data='show_favorite_cities')],
        [InlineKeyboardButton("⚙️ Управление подписками", callback_data='manage_subscriptions')],
        [InlineKeyboardButton("✍️ Обратная связь", callback_data='feedback_start')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    user = update.effective_user
    text = f'Привет, {user.first_name}! Я MeteoBot. Выбери, что тебя интересует:'
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def set_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text('Пример: /setcity Москва')
        return
    city = ' '.join(context.args)
    set_user_default_city(update.effective_user.id, city)
    await update.message.reply_text(f'Город по умолчанию установлен: {city}.')

async def add_fav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text('Пример: /addfav Париж')
        return
    city = ' '.join(context.args).strip()
    if add_favorite_city(update.effective_user.id, city):
        await update.message.reply_text(f'Город "{city}" добавлен в избранное.')
    else:
        await update.message.reply_text(f'Город "{city}" уже в избранном.')

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    action = context.user_data.get('next_action')
    if not action:
        await update.message.reply_text('Не понимаю. Используйте /start для начала.')
        return

    city = update.message.text
    if action == 'get_weather':
        weather_info = await get_weather(city, OPENWEATHER_API_KEY)
        await update.message.reply_text(weather_info, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data='back_to_main')]]))
    elif action == 'get_forecast':
        forecast_info = await get_forecast(city, OPENWEATHER_API_KEY)
        await update.message.reply_text(forecast_info, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data='back_to_main')]]))
    elif action == 'get_hourly':
        hourly_info = await get_hourly_forecast(city, OPENWEATHER_API_KEY)
        await update.message.reply_text(hourly_info, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data='back_to_main')]]))
    
    context.user_data.pop('next_action', None)

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'ask_city_weather':
        context.user_data['next_action'] = 'get_weather'
        await query.edit_message_text('Введите название города:')
    elif data == 'ask_city_forecast':
        context.user_data['next_action'] = 'get_forecast'
        await query.edit_message_text('Введите название города:')
    elif data == 'ask_city_hourly':
        context.user_data['next_action'] = 'get_hourly'
        await query.edit_message_text('Введите название города:')
    elif data == 'get_weather_by_location':
        await query.edit_message_text('Пожалуйста, отправьте свою геолокацию.')
    elif data == 'show_favorite_cities':
        await show_favorite_cities_menu(update, context)
    elif data.startswith('weather_fav_'):
        city = data.replace('weather_fav_', '')
        weather_info = await get_weather(city, OPENWEATHER_API_KEY)
        await query.edit_message_text(weather_info, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data='back_to_main')]]))
    elif data == 'back_to_main':
        await start(update, context)

async def show_favorite_cities_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fav_cities = get_favorite_cities(query.from_user.id)
    if not fav_cities:
        await query.edit_message_text('У вас нет избранных городов.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data='back_to_main')]]))
        return
    
    keyboard = [[InlineKeyboardButton(city, callback_data=f'weather_fav_{city}')] for city in fav_cities]
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data='back_to_main')])
    await query.edit_message_text('Ваши избранные города:', reply_markup=InlineKeyboardMarkup(keyboard))

# --- Система подписок (ConversationHandler) ---

# 1. Главное меню подписок
async def manage_subscriptions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, is_new_message: bool = True) -> int:
    """Главное меню управления подписками."""
    query = update.callback_query
    if query:
        await query.answer()

    user_id = update.effective_user.id
    subs = get_user_subscriptions(user_id)

    text = "⚙️ <b>Управление подписками</b>\n\nЗдесь вы можете добавлять, просматривать и удалять свои подписки."
    keyboard = [
        [InlineKeyboardButton("➕ Добавить ежедневный прогноз", callback_data='sub_add_daily')],
        [InlineKeyboardButton("🚨 Добавить оповещение о дожде", callback_data='sub_add_rain_alert')],
    ]

    if subs:
        text += "\n\nВаши активные подписки:"
        for sub in subs:
            sub_type_rus = "Ежедневный прогноз" if sub['forecast_type'] == 'daily' else "Оповещение о дожде"
            button_text = f"{sub['city']} ({sub_type_rus})"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"sub_view_{sub['id']}")])
    else:
        text += "\n\nУ вас пока нет активных подписок."
    
    keyboard.append([InlineKeyboardButton("◀️ Назад в главное меню", callback_data='back_to_main_menu')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    if query.message.text != text:
        await query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await query.answer()

    return SELECTING_ACTION

async def sub_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    subscriptions = get_user_subscriptions(user_id)
    
    text = "⚙️ Управление подписками"
    keyboard = []
    if subscriptions:
        text += "\n\nВаши активные подписки:"
        for sub in subscriptions:
            sub_type_rus = "Ежедневный прогноз" if sub['forecast_type'] == 'daily' else "Оповещение о дожде"
            button_text = f"{sub['city']} ({sub_type_rus})"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"sub_view_{sub['id']}")])
    else:
        text += "\n\nУ вас пока нет активных подписок."
    
    keyboard.append([InlineKeyboardButton("➕ Создать новую подписку", callback_data='sub_new')])
    keyboard.append([InlineKeyboardButton("◀️ Назад в главное меню", callback_data='back_to_main_menu')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    if query.message.text != text:
        await query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await query.answer()

    return SELECTING_ACTION

async def sub_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['new_sub'] = {}
    keyboard = [
        [InlineKeyboardButton("Ежедневный прогноз погоды", callback_data='sub_type_daily')],
        [InlineKeyboardButton("Оповещение о дожде/снеге", callback_data='sub_type_alert_rain')],
        [InlineKeyboardButton("Отмена", callback_data='sub_cancel')]
    ]
    await query.edit_message_text(
        "Выберите тип подписки:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECTING_ACTION

async def sub_receive_forecast_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    forecast_type = query.data.split('_')[-1]
    context.user_data['new_sub']['forecast_type'] = forecast_type
    await query.answer()
    await query.edit_message_text("Введите город для подписки:")
    return AWAITING_CITY

async def sub_receive_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    city = update.message.text
    context.user_data['new_sub']['city'] = city
    
    forecast_type = context.user_data['new_sub']['forecast_type']
    if forecast_type == 'daily':
        await update.message.reply_text("Введите желаемое время для получения прогноза в формате ЧЧ:ММ (например, 08:00).")
        return AWAITING_TIME
    elif forecast_type == 'alert_rain':
        user_id = update.effective_user.id
        sub_data = context.user_data['new_sub']
        sub_data['time'] = None
        sub_data['days'] = None
        sub_id = add_subscription(user_id, sub_data)
        await schedule_subscription_jobs(context.application, sub_id, user_id, sub_data)
        await update.message.reply_text(f"Подписка на оповещения о дожде для города '{city}' успешно создана!")
        await sub_menu_command(update, context)
        return ConversationHandler.END

async def sub_receive_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        time_str = update.message.text
        user_time = dt_time.fromisoformat(time_str)
        context.user_data['new_sub']['time'] = user_time.strftime('%H:%M')
        context.user_data['selected_days'] = []
        
        keyboard = get_days_keyboard([])
        await update.message.reply_text(
            "Выберите дни недели для получения прогноза. Нажмите 'Готово', когда закончите.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SELECTING_DAYS
    except ValueError:
        await update.message.reply_text("Неверный формат времени. Пожалуйста, введите время в формате ЧЧ:ММ (например, 08:00).")
        return AWAITING_TIME

async def sub_receive_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    day = query.data.split('_')[-1]
    await query.answer()

    selected_days = context.user_data.get('selected_days', [])

    if day == 'done':
        if not selected_days:
            await query.answer("Пожалуйста, выберите хотя бы один день.", show_alert=True)
            return SELECTING_DAYS
        
        user_id = query.from_user.id
        sub_data = context.user_data['new_sub']
        sub_data['days'] = json.dumps(sorted(selected_days))
        sub_id = add_subscription(user_id, sub_data)
        await schedule_subscription_jobs(context.application, sub_id, user_id, sub_data)
        
        await query.edit_message_text(f"Подписка на ежедневный прогноз для города '{sub_data['city']}' успешно создана!")
        await sub_menu_command(update, context)
        return ConversationHandler.END

    day_int = int(day)
    if day_int in selected_days:
        selected_days.remove(day_int)
    else:
        selected_days.append(day_int)
    
    context.user_data['selected_days'] = selected_days
    keyboard = get_days_keyboard(selected_days)
    await query.edit_message_text(
        "Выберите дни недели. Нажмите 'Готово', когда закончите.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECTING_DAYS

async def sub_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sub_id = int(query.data.split('_')[-1])
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM subscriptions WHERE id = ?', (sub_id,))
        sub = dict(cursor.fetchone())

    if not sub:
        await query.edit_message_text("Ошибка: подписка не найдена.", reply_markup=await get_sub_menu_keyboard(query.from_user.id))
        return SELECTING_ACTION

    sub_type_rus = "Ежедневный прогноз" if sub['forecast_type'] == 'daily' else "Оповещение о дожде"
    text = f"<b>Детали подписки:</b>\n\n"
    text += f"<b>Город:</b> {sub['city']}\n"
    text += f"<b>Тип:</b> {sub_type_rus}\n"

    if sub['forecast_type'] == 'daily':
        days_map = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
        days_list = json.loads(sub['days'])
        days_str = ', '.join([days_map[d] for d in days_list])
        text += f"<b>Время:</b> {sub['time']}\n"
        text += f"<b>Дни:</b> {days_str}\n"

    keyboard = [
        [InlineKeyboardButton("🗑️ Удалить подписку", callback_data=f"sub_delete_{sub_id}")],
        [InlineKeyboardButton("◀️ Назад к списку", callback_data='manage_subscriptions')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    return SELECTING_ACTION

async def sub_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    sub_id = int(query.data.split('_')[-1])
    
    job_name = f"sub_{sub_id}"
    jobs = context.application.job_queue.get_jobs_by_name(job_name)
    for job in jobs:
        job.remove()

    delete_subscription(sub_id)
    await query.answer("Подписка удалена.")
    await sub_menu(update, context)
    return SELECTING_ACTION

async def sub_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("Действие отменено.")
    await sub_menu(update, context)
    return ConversationHandler.END

async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await start(update, context)
    return ConversationHandler.END

# --- Вспомогательные функции для клавиатур ---
def get_days_keyboard(selected_days: list) -> list:
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    keyboard = []
    row = []
    for i, day_name in enumerate(days):
        text = f"✅ {day_name}" if i in selected_days else day_name
        row.append(InlineKeyboardButton(text, callback_data=f"day_{i}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("Готово", callback_data="day_done")])
    return keyboard

async def get_sub_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    subscriptions = get_user_subscriptions(user_id)
    keyboard = []
    if subscriptions:
        for sub in subscriptions:
            sub_type_rus = "Ежедневный прогноз" if sub['forecast_type'] == 'daily' else "Оповещение о дожде"
            button_text = f"{sub['city']} ({sub_type_rus})"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"sub_view_{sub['id']}")])
    keyboard.append([InlineKeyboardButton("➕ Создать новую подписку", callback_data='sub_new')])
    keyboard.append([InlineKeyboardButton("◀️ Назад в главное меню", callback_data='back_to_main_menu')])
    return InlineKeyboardMarkup(keyboard)

async def sub_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = "⚙️ Управление подписками"
    reply_markup = await get_sub_menu_keyboard(user_id)
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

# --- Система обратной связи ---
async def feedback_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Пожалуйста, введите ваше сообщение для администратора.")
    return AWAITING_FEEDBACK

async def feedback_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user_name = update.effective_user.full_name
    feedback_text = update.message.text

    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"Новое сообщение от {user_name} (ID: {user_id}):\n\n{feedback_text}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Ответить", callback_data=f'admin_reply_{user_id}')]])
            )
            await update.message.reply_text("Спасибо! Ваше сообщение отправлено администратору.")
        except Exception as e:
            await update.message.reply_text(f"Не удалось отправить сообщение. Ошибка: {e}")
    else:
        await update.message.reply_text("Функция обратной связи не настроена.")
    
    await start(update, context)
    return ConversationHandler.END

async def feedback_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отправка отменена.")
    await start(update, context)
    return ConversationHandler.END

async def admin_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id_to_reply = int(query.data.split('_')[-1])
    context.user_data['user_id_to_reply'] = user_id_to_reply
    await query.answer()
    await query.edit_message_text(f"Введите ответ для пользователя {user_id_to_reply}:")
    return AWAITING_ADMIN_REPLY

async def admin_reply_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Эта функция-заглушка, её нужно будет реализовать
    pass

# --- Логика планировщика ---
async def send_daily_forecast(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id, city = job.data['user_id'], job.data['city']
    weather_info = await get_weather(city, OPENWEATHER_API_KEY)
    await context.bot.send_message(chat_id=user_id, text=weather_info)

async def check_rain_alerts(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id, city = job.data['user_id'], job.data['city']
    sub_id = job.data['sub_id']
    
    lock_job_name = f"rain_lock_{sub_id}"
    if context.application.job_queue.get_jobs_by_name(lock_job_name):
        return

    try:
        data = await get_one_call_data_by_city(city, OPENWEATHER_API_KEY)
        hourly_forecast = data.get('hourly', [])
        
        for hour in hourly_forecast[:4]:
            if 'rain' in hour.get('weather', [{}])[0].get('main', '').lower():
                await context.bot.send_message(
                    chat_id=user_id, 
                    text=f"❗️ Внимание! В городе {city} в ближайшее время ожидается дождь!"
                )
                context.application.job_queue.run_once(lambda: None, 3600, name=lock_job_name)
                break
    except Exception:
        pass

async def get_one_call_data_by_city(city: str, api_key: str) -> dict:
    url = f'https://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}'
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()
        geodata = response.json()
    lat, lon = geodata['coord']['lat'], geodata['coord']['lon']
    return await get_one_call_data(lat, lon, api_key)

async def schedule_subscription_jobs(application: Application, sub_id: int, user_id: int, sub_data: dict):
    scheduler = application.job_queue
    job_name = f"sub_{sub_id}"

    if sub_data['forecast_type'] == 'daily':
        moscow_tz = pytz.timezone('Europe/Moscow')
        user_time = dt_time.fromisoformat(sub_data['time'])
        days = tuple(json.loads(sub_data['days']))
        scheduler.run_daily(
            send_daily_forecast,
            time=user_time,
            days=days,
            chat_id=user_id,
            user_id=user_id,
            name=job_name,
            data={'user_id': user_id, 'city': sub_data['city']},
            tzinfo=moscow_tz
        )
    elif sub_data['forecast_type'] == 'alert_rain':
        scheduler.run_repeating(
            check_rain_alerts,
            interval=900, 
            first=10,
            name=job_name,
            data={'user_id': user_id, 'city': sub_data['city'], 'sub_id': sub_id}
        )

async def reschedule_all_jobs(application: Application):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM subscriptions WHERE is_active = 1')
        all_subs = [dict(row) for row in cursor.fetchall()]

    for sub in all_subs:
        await schedule_subscription_jobs(application, sub['id'], sub['user_id'], sub)
    print(f"Rescheduled {len(all_subs)} jobs.")

async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    latitude = update.message.location.latitude
    longitude = update.message.location.longitude
    weather_info = await get_weather_by_coords(latitude, longitude, OPENWEATHER_API_KEY)
    await update.message.reply_text(weather_info, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data='back_to_main')]]))

# --- Главная функция --- 
def main() -> None:
    init_db()

    persistence = PicklePersistence(filepath=PERSISTENCE_PATH)
    
    application = Application.builder().token(TOKEN).persistence(persistence).post_init(reschedule_all_jobs).build()

    # --- Хендлеры для подписок ---
    sub_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(sub_menu, pattern='^manage_subscriptions$')],
        states={
            SELECTING_ACTION: [
                CallbackQueryHandler(sub_new, pattern='^sub_new$'),
                CallbackQueryHandler(sub_receive_forecast_type, pattern='^sub_type_'),
                CallbackQueryHandler(sub_view, pattern='^sub_view_'),
                CallbackQueryHandler(sub_delete, pattern='^sub_delete_'),
                CallbackQueryHandler(back_to_main_menu, pattern='^back_to_main_menu$'),
                CallbackQueryHandler(sub_menu, pattern='^manage_subscriptions$'),
            ],
            AWAITING_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, sub_receive_city)],
            AWAITING_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sub_receive_time)],
            SELECTING_DAYS: [CallbackQueryHandler(sub_receive_days, pattern='^day_')],
        },
        fallbacks=[
            CallbackQueryHandler(sub_cancel, pattern='^sub_cancel$'),
            CommandHandler('start', start)
        ],
        map_to_parent={
            ConversationHandler.END: ConversationHandler.END
        },
        persistent=True, name="sub_conversation"
    )

    # --- Хендлер для обратной связи ---
    feedback_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(feedback_start, pattern='^feedback_start$')],
        states={
            AWAITING_FEEDBACK: [MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_receive)]
        },
        fallbacks=[CommandHandler('cancel', feedback_cancel)],
        persistent=True, name="feedback_conversation"
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setcity", set_city))
    application.add_handler(CommandHandler("addfav", add_fav))
    
    application.add_handler(sub_handler)
    application.add_handler(feedback_handler)

    application.add_handler(CallbackQueryHandler(button_callback_handler))
    application.add_handler(MessageHandler(filters.LOCATION, location_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    application.run_polling()

if __name__ == '__main__':
    main()