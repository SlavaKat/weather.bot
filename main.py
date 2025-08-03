import os
import httpx
import sqlite3
import json
import pytz
from datetime import datetime, time as dt_time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- Глобальные переменные и константы ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, 'weather_bot.db')
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
    SELECTING_FORECAST_TYPE,
    CONFIRM_SUBSCRIPTION,
    AWAITING_FEEDBACK,
    AWAITING_ADMIN_REPLY,
) = range(8)

# --- Инициализация базы данных ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Таблица для настроек пользователя (город по умолчанию)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_preferences (
        user_id INTEGER PRIMARY KEY,
        default_city TEXT
    )
    ''')
    # Таблица для избранных городов
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS favorite_cities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        city_name TEXT,
        UNIQUE(user_id, city_name)
    )
    ''')
    # Новая таблица для подписок
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        city TEXT NOT NULL,
        time TEXT, -- Может быть NULL для оповещений
        days TEXT, -- Может быть NULL для оповещений
        forecast_type TEXT NOT NULL, -- 'current', 'forecast', 'hourly', 'alert_rain'
        is_active BOOLEAN DEFAULT 1
    )
    ''')
    conn.commit()
    conn.close()

# --- Функции для работы с БД (Пользователи и Избранное) ---
def set_user_default_city(user_id: int, city: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO user_preferences (user_id, default_city) VALUES (?, ?)', (user_id, city))
    conn.commit()
    conn.close()

def get_user_default_city(user_id: int) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT default_city FROM user_preferences WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def add_favorite_city(user_id: int, city: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO favorite_cities (user_id, city_name) VALUES (?, ?)', (user_id, city))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False 
    finally:
        conn.close()

def get_favorite_cities(user_id: int) -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT city_name FROM favorite_cities WHERE user_id = ? ORDER BY city_name', (user_id,))
    cities = [row[0] for row in cursor.fetchall()]
    conn.close()
    return cities

# --- Функции для работы с БД (Подписки) ---
def add_subscription(user_id: int, sub_data: dict) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO subscriptions (user_id, city, time, days, forecast_type) VALUES (?, ?, ?, ?, ?)',
        (user_id, sub_data['city'], sub_data['time'], json.dumps(sub_data['days']), sub_data['forecast_type'])
    )
    sub_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return sub_id

def get_user_subscriptions(user_id: int) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM subscriptions WHERE user_id = ? AND is_active = 1', (user_id,))
    subs = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return subs

def delete_subscription(sub_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE subscriptions SET is_active = 0 WHERE id = ?', (sub_id,))
    conn.commit()
    conn.close()

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
        city_name, country = geodata['name'], geodata['sys']['country']

        data = await get_one_call_data(lat, lon, api_key)
        current = data['current']
        
        temp = current['temp']
        feels_like = current['feels_like']
        description = current['weather'][0]['description']
        humidity = current['humidity']
        wind_speed = current['wind_speed']
        uv_index = get_uv_index_description(current.get('uvi', 0))
        sunrise = datetime.fromtimestamp(current['sunrise']).strftime('%H:%M')
        sunset = datetime.fromtimestamp(current['sunset']).strftime('%H:%M')

        return (
            f"Погода в {city_name}, {country}:\n"
            f"🌡️ Температура: {temp:.1f}°C (ощущается как {feels_like:.1f}°C)\n"
            f"📝 Описание: {description.capitalize()}\n"
            f"💧 Влажность: {humidity}%\n"
            f"💨 Скорость ветра: {wind_speed} м/с\n"
            f"☀️ УФ-индекс: {uv_index}\n"
            f"🌅 Восход: {sunrise} | 🌇 Закат: {sunset}"
        )
    except Exception as e:
        return f'Произошла ошибка: {e}'

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
    if uv_index < 3:
        return f"{uv_index} (Низкий)"
    elif uv_index < 6:
        return f"{uv_index} (Средний)"
    elif uv_index < 8:
        return f"{uv_index} (Высокий)"
    elif uv_index < 11:
        return f"{uv_index} (Очень высокий)"
    else:
        return f"{uv_index} (Экстремальный)"

async def get_one_call_data(lat: float, lon: float, api_key: str) -> dict:
    url = f'https://api.openweathermap.org/data/3.0/onecall?lat={lat}&lon={lon}&exclude=minutely,alerts&appid={api_key}&units=metric&lang=ru'
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()

async def get_weather_by_coords(latitude: float, longitude: float, api_key: str) -> str:
    try:
        geo_url = f'https://api.openweathermap.org/data/2.5/weather?lat={latitude}&lon={longitude}&appid={api_key}&units=metric&lang=ru'
        async with httpx.AsyncClient() as client:
            response = await client.get(geo_url)
            response.raise_for_status()
            geodata = response.json()
        city_name = geodata.get('name', 'Неизвестное место')
        country = geodata.get('sys', {}).get('country', '')

        data = await get_one_call_data(latitude, longitude, api_key)
        current = data['current']
        
        temp = current['temp']
        feels_like = current['feels_like']
        description = current['weather'][0]['description']
        humidity = current['humidity']
        wind_speed = current['wind_speed']
        uv_index = get_uv_index_description(current.get('uvi', 0))
        sunrise = datetime.fromtimestamp(current['sunrise']).strftime('%H:%M')
        sunset = datetime.fromtimestamp(current['sunset']).strftime('%H:%M')
        
        return(
            f'Погода в {city_name}, {country} (по вашему местоположению):\n'
            f'🌡️ Температура: {temp:.1f}°C (ощущается как {feels_like:.1f}°C)\n'
            f'📝 Описание: {description.capitalize()}\n'
            f'💧 Влажность: {humidity}%\n'
            f'💨 Скорость ветра: {wind_speed} м/с\n'
            f'☀️ УФ-индекс: {uv_index}\n'
            f'🌅 Восход: {sunrise} | 🌇 Закат: {sunset}'
        )
    except Exception as e:
        return f'Произошла непредвиденная ошибка: {e}'

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

# --- Основные команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("Текущая погода", callback_data='ask_city_weather')],
        [InlineKeyboardButton("Прогноз на 5 дней", callback_data='ask_city_forecast')],
        [InlineKeyboardButton("Почасовой прогноз", callback_data='ask_city_hourly')],
        [InlineKeyboardButton("📍 Погода по местоположению", callback_data='get_weather_by_location')],
        [InlineKeyboardButton("Избранные города", callback_data='show_favorite_cities')],
        [InlineKeyboardButton("📅 Управление подписками", callback_data='manage_subscriptions')],
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

# --- Обработчик кнопок и текстового ввода ---
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    action = context.user_data.get('next_action')
    if not action:
        await update.message.reply_text('Не понимаю. Используйте /start для начала.')
        return

    city = update.message.text
    if action == 'get_weather':
        weather_info = await get_weather(city, OPENWEATHER_API_KEY)
        await update.message.reply_text(weather_info, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data='back_to_main')]]))
    elif action == 'get_forecast':
        forecast_info = await get_forecast(city, OPENWEATHER_API_KEY)
        await update.message.reply_text(forecast_info, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data='back_to_main')]]))
    elif action == 'get_hourly':
        hourly_info = await get_hourly_forecast(city, OPENWEATHER_API_KEY)
        await update.message.reply_text(hourly_info, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data='back_to_main')]]))
    
    context.user_data.pop('next_action', None)

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    if query.data == 'ask_city_weather':
        context.user_data['next_action'] = 'get_weather'
        await query.edit_message_text('Введите название города:')
    elif query.data == 'ask_city_forecast':
        context.user_data['next_action'] = 'get_forecast'
        await query.edit_message_text('Введите название города:')
    elif query.data == 'ask_city_hourly':
        context.user_data['next_action'] = 'get_hourly'
        await query.edit_message_text('Введите название города:')
    elif query.data == 'get_weather_by_location':
        await query.edit_message_text('Пожалуйста, отправьте свою геолокацию, чтобы я мог определить погоду.')
    elif query.data == 'show_favorite_cities':
        await show_favorite_cities_menu(update, context)
    elif query.data.startswith('weather_fav_'):
        city = query.data.replace('weather_fav_', '')
        weather_info = await get_weather(city, OPENWEATHER_API_KEY)
        await query.edit_message_text(weather_info, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data='back_to_main')]]))
    elif query.data == 'back_to_main':
        await start(update, context)
    # Остальные колбэки будут обрабатываться в ConversationHandler

async def show_favorite_cities_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fav_cities = get_favorite_cities(query.from_user.id)
    if not fav_cities:
        await query.edit_message_text('У вас нет избранных городов.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]]))
        return
    
    keyboard = [[InlineKeyboardButton(city, callback_data=f'weather_fav_{city}')] for city in fav_cities]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')])
    await query.edit_message_text('Ваши избранные города:', reply_markup=InlineKeyboardMarkup(keyboard))

# --- Система подписок (ConversationHandler) ---

# 1. Главное меню подписок
async def manage_subscriptions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    subscriptions = get_user_subscriptions(user_id)
    
    keyboard = [[InlineKeyboardButton("➕ Добавить новую подписку", callback_data='sub_add')]]
    text = "Управление подписками."

    if subscriptions:
        text += "\n\nВаши активные подписки:"
        for sub in subscriptions:
            days = " ".join(json.loads(sub['days'])) if sub['days'] != 'all' else 'ежедневно'
            keyboard.append([InlineKeyboardButton(f"📍 {sub['city']} в {sub['time']} ({days})", callback_data=f"sub_view_{sub['id']}")])
    
    keyboard.append([InlineKeyboardButton("⬅️ Назад в главное меню", callback_data='back_to_main')])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECTING_ACTION

# 2. Начало добавления подписки -> запрос города
async def sub_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data['new_sub'] = {}
    await query.edit_message_text("Введите город для новой подписки:")
    return AWAITING_CITY

# 3. Получение города -> запрос времени
async def sub_receive_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    city = update.message.text
    context.user_data['new_sub']['city'] = city
    await update.message.reply_text("Отлично. Теперь введите время для прогноза в формате ЧЧ:ММ (например, 08:30).")
    return AWAITING_TIME

# 4. Получение времени -> запрос дней
async def sub_receive_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        time_str = update.message.text
        dt_time.strptime(time_str, '%H:%M')
        context.user_data['new_sub']['time'] = time_str
        
        keyboard = [
            [InlineKeyboardButton("Ежедневно", callback_data='sub_days_all')],
            [InlineKeyboardButton("По будням", callback_data='sub_days_weekdays')],
            [InlineKeyboardButton("По выходным", callback_data='sub_days_weekends')],
        ]
        await update.message.reply_text("Выберите дни:", reply_markup=InlineKeyboardMarkup(keyboard))
        return SELECTING_DAYS
    except ValueError:
        await update.message.reply_text("Неверный формат времени. Пожалуйста, введите в формате ЧЧ:ММ.")
        return AWAITING_TIME

# 5. Получение дней -> запрос типа прогноза
async def sub_receive_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    days_choice = query.data.split('_')[-1]
    
    days_map = {
        'all': 'all',
        'weekdays': ['Пн', 'Вт', 'Ср', 'Чт', 'Пт'],
        'weekends': ['Сб', 'Вс']
    }
    context.user_data['new_sub']['days'] = days_map[days_choice]
    
    keyboard = [
        [InlineKeyboardButton("Текущая погода", callback_data='sub_type_current')],
        [InlineKeyboardButton("Прогноз на 5 дней", callback_data='sub_type_forecast')],
        [InlineKeyboardButton("Почасовой прогноз", callback_data='sub_type_hourly')],
        [InlineKeyboardButton("🚨 Оповещение о дожде", callback_data='sub_type_alert_rain')],
    ]
    await query.edit_message_text("Выберите тип прогноза:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECTING_FORECAST_TYPE

# 6. Получение типа -> подтверждение и сохранение
async def sub_receive_type_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    forecast_type = query.data.split('_')[-1]
    context.user_data['new_sub']['forecast_type'] = forecast_type
    
    # Для оповещений не нужно время и дни
    if 'alert' in forecast_type:
        context.user_data['new_sub']['time'] = None
        context.user_data['new_sub']['days'] = None
        add_subscription(query.from_user.id, context.user_data['new_sub'])
        await query.answer("Оповещение о дожде включено!")
        await query.edit_message_text("Оповещение о дожде включено! Проверка будет происходить каждый час.")
    else:
        sub_id = add_subscription(query.from_user.id, context.user_data['new_sub'])
        schedule_single_job(application, query.from_user.id, sub_id)
        await query.answer("Подписка успешно создана!")
        await query.edit_message_text("Подписка успешно создана!")

    context.user_data.pop('new_sub', None)
    await manage_subscriptions_menu(update, context)
    return SELECTING_ACTION

# 7. Просмотр и удаление подписки
async def sub_view_and_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    sub_id = int(query.data.split('_')[-1])
    
    keyboard = [
        [InlineKeyboardButton("🗑️ Удалить эту подписку", callback_data=f'sub_delete_confirm_{sub_id}')],
        [InlineKeyboardButton("⬅️ Назад к списку", callback_data='sub_back_to_list')]
    ]
    await query.edit_message_text("Вы можете удалить эту подписку.", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECTING_ACTION

async def sub_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает детали конкретной подписки."""
    query = update.callback_query
    await query.answer()
    sub_id = int(query.data.split('_')[-1])

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM subscriptions WHERE id = ?', (sub_id,))
    sub = cursor.fetchone()
    conn.close()

    if not sub:
        await query.edit_message_text("Ошибка: подписка не найдена.")
        # Возвращаемся к главному меню подписок
        await manage_subscriptions_menu(update, context, is_new_message=False)
        return SELECTING_ACTION

    days_map = {"0": "Пн", "1": "Вт", "2": "Ср", "3": "Чт", "4": "Пт", "5": "Сб", "6": "Вс"}
    forecast_map = {
        'daily': 'Ежедневный прогноз',
        'alert_rain': '🚨 Оповещение о дожде'
    }
    
    days_list = json.loads(sub['days'])
    days_str = ", ".join(sorted([days_map[d] for d in days_list], key=lambda x: list(days_map.values()).index(x)))
    forecast_type_str = forecast_map.get(sub['forecast_type'], 'Неизвестный тип')

    text = (
        f"<b>Детали подписки №{sub['id']}</b>\n"
        f"- <b>Город:</b> {sub['city']}\n"
        f"- <b>Время:</b> {sub['time']}\n"
        f"- <b>Дни:</b> {days_str}\n"
        f"- <b>Тип:</b> {forecast_type_str}"
    )

    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data='manage_subscriptions')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    return SELECTING_ACTION


async def sub_delete_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    sub_id = int(query.data.split('_')[-1])
    delete_subscription(sub_id)
    await query.answer("Подписка удалена.")
    await manage_subscriptions_menu(update, context)
    return SELECTING_ACTION

# 8. Отмена
async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await start(update, context)
    return ConversationHandler.END

# --- Обработчик геолокации ---
async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    location = update.message.location
    weather_info = await get_weather_by_coords(location.latitude, location.longitude, OPENWEATHER_API_KEY)
    await update.message.reply_text(
        weather_info,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data='back_to_main')]])
    )

# --- Система обратной связи ---
async def feedback_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text("Пожалуйста, отправьте ваше сообщение для разработчика.")
    return AWAITING_FEEDBACK

async def feedback_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    feedback_text = update.message.text
    user = update.effective_user
    
    if ADMIN_CHAT_ID:
        text_to_admin = (
            f"✉️ Новая обратная связь от {user.full_name} (@{user.username}, ID: {user.id})\n\n"
            f"Текст:\n{feedback_text}"
        )
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text_to_admin)
        await update.message.reply_text("Спасибо! Ваше сообщение отправлено.")
    else:
        await update.message.reply_text("Спасибо за отзыв! (Функция отправки отключена)")

    await start(update, context)
    return ConversationHandler.END

# --- Планировщик ---
async def send_scheduled_forecast(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    user_id, city, forecast_type = job_data['user_id'], job_data['city'], job_data['forecast_type']
    
    if forecast_type in ['current', 'forecast', 'hourly']:
        if forecast_type == 'current':
            weather_info = await get_weather(city, OPENWEATHER_API_KEY)
        elif forecast_type == 'forecast':
            weather_info = await get_forecast(city, OPENWEATHER_API_KEY)
        else: # hourly
            weather_info = await get_hourly_forecast(city, OPENWEATHER_API_KEY)
        await context.bot.send_message(chat_id=user_id, text=f"⏰ Ваш запланированный прогноз для {city}:\n\n{weather_info}")
    # Логика для оповещений находится в check_for_rain_alerts

async def check_for_rain_alerts(application: Application):
    """Проверяет прогноз на ближайшие часы и отправляет оповещения о дожде."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, city FROM subscriptions WHERE forecast_type = 'alert_rain' AND is_active = 1")
    alerts = cursor.fetchall()
    conn.close()

    for user_id, city in alerts:
        try:
            geo_url = f'https://api.openweathermap.org/data/2.5/weather?q={city}&appid={OPENWEATHER_API_KEY}'
            async with httpx.AsyncClient() as client:
                geo_res = await client.get(geo_url)
                geo_res.raise_for_status()
                geodata = geo_res.json()
                if geodata.get('cod') != 200: continue
                lat, lon = geodata['coord']['lat'], geodata['coord']['lon']

            data = await get_one_call_data(lat, lon, OPENWEATHER_API_KEY)
            hourly_forecast = data.get('hourly', [])

            for i in range(1, 4):
                if len(hourly_forecast) > i:
                    weather_id = hourly_forecast[i]['weather'][0]['id']
                    if 500 <= weather_id <= 531:
                        job_name = f'alert_rain_{user_id}_{city}_{hourly_forecast[i]["dt"]}'
                        if not application.job_queue.get_jobs_by_name(job_name):
                            await application.bot.send_message(
                                chat_id=user_id,
                                text=f"🚨 Внимание! В городе {city} в ближайшие часы ожидается дождь. Не забудьте зонт! ☔"
                            )
                            application.job_queue.run_once(lambda: None, 3 * 3600, name=job_name)
                        break
        except Exception as e:
            print(f"Ошибка при проверке оповещения о дожде для {city}: {e}")

async def schedule_single_job(application: Application, user_id: int, sub_id: int):
    """Создает или обновляет одну задачу для конкретной подписки."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM subscriptions WHERE id = ?', (sub_id,))
    sub_data = cursor.fetchone()
    conn.close()

    if not sub_data or not sub_data['time']:
        return

    sub = dict(sub_data)
    job_name = f"sub_{sub_id}"

    for job in application.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    days_of_week = tuple(range(7))
    if sub['days'] and sub['days'] != 'all':
        days_map = {'Пн': 0, 'Вт': 1, 'Ср': 2, 'Чт': 3, 'Пт': 4, 'Сб': 5, 'Вс': 6}
        try:
            days_list = json.loads(sub['days'])
            if days_list == 'weekdays':
                days_of_week = tuple(range(5))
            elif days_list == 'weekends':
                days_of_week = (5, 6)
            else:
                days_of_week = tuple(days_map[day] for day in days_list)
        except (json.JSONDecodeError, TypeError):
            pass

    hour, minute = map(int, sub['time'].split(':'))
    application.job_queue.run_daily(
        send_scheduled_forecast,
        time=dt_time(hour, minute, tzinfo=pytz.timezone('Europe/Moscow')),
        days=days_of_week,
        chat_id=user_id,
        user_id=user_id,
        name=job_name,
        data={'user_id': user_id, 'city': sub['city'], 'forecast_type': sub['forecast_type']}
    )

async def schedule_jobs(application: Application):
    """Запускается при старте и раз в час, чтобы обновить задачи и проверить оповещения."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id FROM subscriptions WHERE is_active = 1 AND forecast_type != 'alert_rain'")
    subs = cursor.fetchall()
    conn.close()

    for sub_id, user_id in subs:
        job_name = f'sub_{sub_id}'
        if not application.job_queue.get_jobs_by_name(job_name):
             await schedule_single_job(application, user_id, sub_id)
    print(f"[{datetime.now()}] Запланированные задачи (прогнозы) обновлены.")

    await check_for_rain_alerts(application)
    print(f"[{datetime.now()}] Проверка оповещений о дожде завершена.")

def main() -> None:
    if not TOKEN or not OPENWEATHER_API_KEY:
        print("Ошибка: не установлены переменные окружения TELEGRAM_BOT_TOKEN или OPENWEATHER_API_KEY")
        return

    init_db()
    
    application = Application.builder().token(TOKEN).build()

    sub_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(manage_subscriptions_menu, pattern='^manage_subscriptions$')],
        states={
            SELECTING_ACTION: [
                CallbackQueryHandler(sub_add_start, pattern='^sub_add$'),
                CallbackQueryHandler(sub_view, pattern=r'^sub_view_\d+$'),
                CallbackQueryHandler(sub_delete_confirmed, pattern=r'^sub_delete_\d+$'),
                CallbackQueryHandler(start, pattern='^back_to_main$'),
            ],
            AWAITING_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, sub_receive_city)],
            AWAITING_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sub_receive_time)],
            SELECTING_DAYS: [CallbackQueryHandler(sub_receive_days)],
            SELECTING_FORECAST_TYPE: [CallbackQueryHandler(sub_receive_forecast_type)],
        },
        fallbacks=[CallbackQueryHandler(cancel_conversation, pattern='^cancel$')],
        map_to_parent={
            SELECTING_ACTION: SELECTING_ACTION, 
            ConversationHandler.END: ConversationHandler.END
        }
    )

    feedback_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(feedback_start, pattern='^feedback_start$')],
        states={
            AWAITING_FEEDBACK: [MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_receive)],
            AWAITING_ADMIN_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_reply_receive)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conversation, pattern='^cancel_feedback$')]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(sub_conv_handler)
    application.add_handler(feedback_conv_handler)
    
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    application.add_handler(MessageHandler(filters.LOCATION, location_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    scheduler = AsyncIOScheduler(timezone=pytz.timezone('Europe/Moscow'))
    scheduler.add_job(schedule_jobs, 'interval', minutes=60, args=[application], next_run_time=datetime.now())
    scheduler.start()

    print("Бот запущен...")
    application.run_polling()

if __name__ == '__main__':
    main()