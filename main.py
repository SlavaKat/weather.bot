import os
import httpx
import requests
import sqlite3
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from dotenv import load_dotenv

# Новые функции для работы с базой данных
def set_user_default_city(user_id: int, city: str):
    conn = sqlite3.connect('weather_bot.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO user_preferences (user_id, default_city) VALUES (?, ?)', (user_id, city))
    conn.commit()
    conn.close()

def get_user_default_city(user_id: int) -> str | None:
    conn = sqlite3.connect('weather_bot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT default_city FROM user_preferences WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

# {{ Новые функции для работы с любимыми городами }}
def add_favorite_city(user_id: int, city: str):
    conn = sqlite3.connect('weather_bot.db')
    cursor = conn.cursor()
    # Проверяем, существует ли уже такой город у пользователя, чтобы избежать дубликатов
    cursor.execute('SELECT 1 FROM favorite_cities WHERE user_id = ? AND city_name = ?', (user_id, city))
    if not cursor.fetchone():
        cursor.execute('INSERT INTO favorite_cities (user_id, city_name) VALUES (?, ?)', (user_id, city))
        conn.commit()
        conn.close()
        return True # Город добавлен
    conn.close()
    return False # Город уже существует

def get_favorite_cities(user_id: int) -> list[str]:
    conn = sqlite3.connect('weather_bot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT city_name FROM favorite_cities WHERE user_id = ? ORDER BY city_name', (user_id,))
    cities = [row[0] for row in cursor.fetchall()]
    conn.close()
    return cities

#Функция для получения данных о погоде
async def get_weather( city: str, api_key: str) -> str:
    url = f'https://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}&units=metric&lang=ru'
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status() # Вызывает исключение для статусов HTTP ошибок (4xx или 5xx)
            data = response.json()

        if data.get('cod') != 200:
           return f"Ошибка получения данных: {data.get('message', 'Неизвестная ошибка')}. Пожалуйста, проверьте название пункта прогноза."

        city_name = data['name']
        country = data['sys']['country']
        temp = data['main']['temp']
        description = data['weather'][0]['description']
        humidity = data['main']['humidity']
        wind_speed = data['wind']['speed']
        
        return(
            f'Погода в {city_name}, {country}:\n'
            f'Температура: {temp}°C\n'
            f'Описание: {description.capitalize()}\n'
            f'Влажность: {humidity}%\n'
            f'Скорость ветра: {wind_speed} м/с'
        )
    except httpx.RequestError as e:
        return f'Ошибка сети или запроса: {e}. Проверьте ваше интернет-соединение или попробуйте позже.'
    except KeyError as e:
        return f'Ошибка обработки данных: Отсутствует ожидаемое поле в ответе: {e}. Возможно, данные для этого пункта прогноза недоступны.'
    except Exception as e:
        return f'Произошла непредвиденная ошибка: {e}'

# Функция для получения погоды по координатам
async def get_weather_by_coords(latitude: float, longitude: float, api_key: str) -> str:
    url = f'https://api.openweathermap.org/data/2.5/weather?lat={latitude}&lon={longitude}&appid={api_key}&units=metric&lang=ru'
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()

        if data.get('cod') != 200:
            return f"Ошибка получения данных по координатам: {data.get('message', 'Неизвестная ошибка')}"

        city_name = data['name']
        country = data['sys']['country']
        temp = data['main']['temp']
        description = data['weather'][0]['description']
        humidity = data['main']['humidity']
        wind_speed = data['wind']['speed']
        
        return(
            f'Погода в {city_name}, {country} (по вашему местоположению):\n'
            f'Температура: {temp}°C\n'
            f'Описание: {description.capitalize()}\n'
            f'Влажность: {humidity}%\n'
            f'Скорость ветра: {wind_speed} м/с'
        )
    except httpx.RequestError as e:
        return f'Ошибка сети или запроса: {e}. Проверьте ваше интернет-соединение или попробуйте позже.'
    except KeyError as e:
        return f'Ошибка обработки данных: Отсутствует ожидаемое поле в ответе: {e}. Возможно, данные для этого местоположения недоступны.'
    except Exception as e:
        return f'Произошла непредвиденная ошибка: {e}'

#Команда /start 
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("Текущая погода", callback_data='get_current_weather')],
        [InlineKeyboardButton("Прогноз на 5 дней", callback_data='get_5_day_forecast')],
        [InlineKeyboardButton("Почасовой прогноз", callback_data='get_hourly_forecast')],
        [InlineKeyboardButton("Погода по местоположению", callback_data='get_weather_by_location')],
        [InlineKeyboardButton("Избранные города", callback_data='show_favorite_cities')], # {{ Добавляем новую кнопку }}
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        'Привет! Я бот для проверки погоды. Выбери, что тебя интересует:',
        reply_markup=reply_markup
    )

# {{ Новая команда /setcity }}
async def set_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text('Пожалуйста, укажите город, который хотите установить как город по умолчанию. Пример: /setcity Москва')
        return

    user_id = update.effective_user.id
    city = ' '.join(context.args)
    set_user_default_city(user_id, city)
    await update.message.reply_text(f'Ваш город по умолчанию установлен как: {city}.')

# {{ Новая команда /getcity }}
async def get_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    default_city = get_user_default_city(user_id)

    if default_city:
        await update.message.reply_text(f'Ваш текущий город по умолчанию: {default_city}.')
    else:
        await update.message.reply_text('У вас пока не установлен город по умолчанию. Используйте /setcity <название_города> для установки.')

# {{ Новая команда /addfav }}
async def add_fav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text('Пожалуйста, укажите город, который хотите добавить в избранное. Пример: /addfav Париж')
        return

    user_id = update.effective_user.id
    city = ' '.join(context.args).strip()
    if add_favorite_city(user_id, city):
        await update.message.reply_text(f'Город "{city}" добавлен в ваш список избранных.')
    else:
        await update.message.reply_text(f'Город "{city}" уже есть в вашем списке избранных.')

# {{ Новая команда /listfav }}
async def list_fav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    favorite_cities = get_favorite_cities(user_id)

    if favorite_cities:
        message = 'Ваши избранные города:\n' + '\n'.join(f'- {city}' for city in favorite_cities)
        await update.message.reply_text(message)
    else:
        await update.message.reply_text('У вас пока нет избранных городов. Используйте /addfav <название_города>, чтобы добавить.')

#Команда /weather
async def weather(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) == 0:
        await update.message.reply_text('Пожалуйста, укажите пункт прогноза. Пример: /weather Москва')
        return
    
    city = ' '.join(context.args)
    api_key = os.getenv('OPENWEATHER_API_KEY')
    if not api_key:
        await update.message.reply_text('API ключ OpenWeatherMap не установлен. Пожалуйста, установите переменную окружения OPENWEATHER_API_KEY.')
        return

    weather_info = await get_weather(city, api_key)
    await update.message.reply_text(weather_info)            

# Функция для получения прогноза на 5 дней
async def get_forecast(city: str, api_key: str) -> str:
    url = f'https://api.openweathermap.org/data/2.5/forecast?q={city}&appid={api_key}&units=metric&lang=ru&cnt=40'  # 40 = 8 интервалов * 5 дней
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
        
        if data.get('cod') != '200':
            return f"Ошибка получения данных: {data.get('message', 'Неизвестная ошибка')}. Пожалуйста, проверьте название пункта прогноза."

        city_name = data['city']['name']
        country = data['city']['country']
        
        # Группируем прогноз по дням
        forecast_by_day = {}
        for item in data['list']:
            date = datetime.fromtimestamp(item['dt']).strftime('%Y-%m-%d')
            if date not in forecast_by_day:
                forecast_by_day[date] = []
            forecast_by_day[date].append(item)
        
        # Формируем сообщение
        message = [f'Прогноз погоды в {city_name}, {country} на 5 дней:\n']
        
        for i, (date, forecasts) in enumerate(forecast_by_day.items()):
            if i >= 5:  # Ограничиваем 5 днями
                break
                
            day_name = datetime.strptime(date, '%Y-%m-%d').strftime('%A')
            day_temps = [f['main']['temp'] for f in forecasts]
            day_descriptions = [f['weather'][0]['description'] for f in forecasts]
            
            # Находим наиболее частое описание погоды за день
            most_common_desc = max(set(day_descriptions), key=day_descriptions.count)
            
            message.append(
                f"{day_name} ({date}):\n"
                f"🌡 {min(day_temps):.1f}°C...{max(day_temps):.1f}°C, "
                f"{most_common_desc.capitalize()}\n"
            )
        
        return '\n'.join(message)
        
    except httpx.RequestError as e:
        return f'Ошибка сети или запроса: {e}. Проверьте ваше интернет-соединение или попробуйте позже.'
    except KeyError as e:
        return f'Ошибка обработки данных: Отсутствует ожидаемое поле в ответе: {e}. Возможно, данные для этого пункта прогноза недоступны.'
    except Exception as e:
        return f'Произошла непредвиденная ошибка: {e}'

# Функция для получения почасового прогноза
async def get_hourly_forecast(city: str, api_key: str) -> str:
    url = f'https://api.openweathermap.org/data/2.5/forecast?q={city}&appid={api_key}&units=metric&lang=ru'
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()

        if data.get('cod') != '200':
            return f"Ошибка получения данных: {data.get('message', 'Неизвестная ошибка')}. Пожалуйста, проверьте название пункта прогноза."

        city_name = data['city']['name']
        message = [f'Почасовой прогноз погоды в {city_name} на ближайшие 24 часа:\n']

        # Проходим по первым 8 записям (24 часа, так как данные каждые 3 часа)
        for item in data['list'][:8]:
            time = datetime.fromtimestamp(item['dt']).strftime('%H:%M')
            temp = item['main']['temp']
            description = item['weather'][0]['description']
            message.append(f'{time}: {temp}°C, {description.capitalize()}')
        
        return '\n'.join(message)

    except httpx.RequestError as e:
        return f'Ошибка сети или запроса: {e}. Проверьте ваше интернет-соединение или попробуйте позже.'
    except KeyError as e:
        return f'Ошибка обработки данных: Отсутствует ожидаемое поле в ответе: {e}. Возможно, данные для этого пункта прогноза недоступны.'
    except Exception as e:
        return f'Произошла непредвиденная ошибка: {e}'

# Обработчик для нажатий на кнопки
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer() # Отвечаем на callbackQuery, чтобы убрать индикатор загрузки

    if query.data == 'get_current_weather':
        context.user_data['next_action'] = 'weather'
        await query.edit_message_text('Пожалуйста, введите название пункта прогноза для текущей погоды:')
    elif query.data == 'get_5_day_forecast':
        context.user_data['next_action'] = 'forecast'
        await query.edit_message_text('Пожалуйста, введите название пункта прогноза для прогноза на 5 дней:')
    elif query.data == 'get_hourly_forecast': # Новый обработчик для почасового прогноза
        context.user_data['next_action'] = 'hourly_forecast'
        await query.edit_message_text('Пожалуйста, введите название пункта прогноза для почасового прогноза:')
    elif query.data == 'get_weather_by_location': # Новый обработчик для погоды по местоположению
        context.user_data['next_action'] = 'weather_by_location'
        await query.edit_message_text('Пожалуйста, отправьте свою геолокацию, чтобы я мог определить погоду в вашем текущем местоположении.')
    elif query.data == 'show_favorite_cities': # {{ Обработка новой кнопки "Избранные города" }}
        user_id = query.from_user.id
        favorite_cities = get_favorite_cities(user_id)
        
        if favorite_cities:
            # Создаем кнопки для каждого избранного города
            fav_city_buttons = []
            for city in favorite_cities:
                # Используем префикс 'fav_city:' для callback_data, чтобы потом легко определить, что это город из избранного
                fav_city_buttons.append([InlineKeyboardButton(city, callback_data=f'fav_city:{city}')])
            
            # Добавляем кнопку "Назад"
            fav_city_buttons.append([InlineKeyboardButton("⬅️ Назад в меню", callback_data='back_to_main_menu')])
            
            reply_markup = InlineKeyboardMarkup(fav_city_buttons)
            await query.edit_message_text(
                'Ваши избранные города. Нажмите, чтобы узнать погоду:',
                reply_markup=reply_markup
            )
        else:
            keyboard = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data='back_to_main_menu')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                'У вас пока нет избранных городов. Используйте команду /addfav <название_города>, чтобы добавить их.',
                reply_markup=reply_markup
            )
    elif query.data.startswith('fav_city:'): # {{ Обработка нажатия на кнопку с избранным городом }}
        city = query.data.split(':', 1)[1] # Извлекаем название города из callback_data
        api_key = os.getenv('OPENWEATHER_API_KEY')

        if not api_key:
            await query.edit_message_text('API ключ OpenWeatherMap не установлен. Пожалуйста, установите переменную окружения OPENWEATHER_API_KEY.')
            return
        
        weather_info = await get_weather(city, api_key)
        # После получения погоды, можно предложить вернуться в главное меню или показать снова список избранных
        keyboard = [
            [InlineKeyboardButton("⬅️ Назад в меню", callback_data='back_to_main_menu')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(weather_info, reply_markup=reply_markup)
    elif query.data == 'back_to_main_menu': # {{ Обработка кнопки "Назад" }}
        # Просто вызываем команду start, чтобы вернуть главное меню
        await start(update, context)


# Обработчик текстовых сообщений (после нажатия кнопки)
async def handle_city_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if 'next_action' in context.user_data:
        city = update.message.text
        # Изменения для использования города по умолчанию, если не введен
        if not city: # Если пользователь просто нажал Enter или отправил пустое сообщение
            user_id = update.effective_user.id
            default_city = get_user_default_city(user_id)
            if default_city:
                city = default_city
                await update.message.reply_text(f'Использую ваш город по умолчанию: {default_city}.')
            else:
                await update.message.reply_text('Вы не указали город. Пожалуйста, введите название города или установите город по умолчанию с помощью /setcity.')
                return

        api_key = os.getenv('OPENWEATHER_API_KEY')

        if not api_key:
            await update.message.reply_text('API ключ OpenWeatherMap не установлен. Пожалуйста, установите переменную окружения OPENWEATHER_API_KEY.')
            return

        action = context.user_data.pop('next_action') # Удаляем действие после использования

        if action == 'weather':
            weather_info = await get_weather(city, api_key)
            await update.message.reply_text(weather_info)
        elif action == 'forecast':
            forecast_info = await get_forecast(city, api_key)
            await update.message.reply_text(forecast_info)
        elif action == 'hourly_forecast': # Обработка для почасового прогноза
            hourly_forecast_info = await get_hourly_forecast(city, api_key)
            await update.message.reply_text(hourly_forecast_info)
    else:
        await update.message.reply_text('Я не понимаю эту команду. Пожалуйста, используйте /start, чтобы начать.')

# Обработчик сообщений с локацией
async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if 'next_action' in context.user_data and context.user_data['next_action'] == 'weather_by_location':
        latitude = update.message.location.latitude
        longitude = update.message.location.longitude
        api_key = os.getenv('OPENWEATHER_API_KEY')

        if not api_key:
            await update.message.reply_text('API ключ OpenWeatherMap не установлен. Пожалуйста, установите переменную окружения OPENWEATHER_API_KEY.')
            context.user_data.pop('next_action')
            return
        
        context.user_data.pop('next_action') # Удаляем действие после использования
        weather_info = await get_weather_by_coords(latitude, longitude, api_key)
        await update.message.reply_text(weather_info)
    else:
        await update.message.reply_text('Я не ожидал локацию. Пожалуйста, используйте /start, чтобы начать.')

# {{ 2. Функция для инициализации базы данных }}
def init_db():
    conn = sqlite3.connect('weather_bot.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id INTEGER PRIMARY KEY,
            default_city TEXT
        )
    ''')
    # {{ Добавляем новую таблицу для избранных городов }}
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS favorite_cities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            city_name TEXT NOT NULL,
            UNIQUE(user_id, city_name) # Гарантирует, что у пользователя не будет дубликатов городов
        )
    ''')
    conn.commit()
    conn.close()

#Основная функция

def main():
    load_dotenv() # Загружаем переменные из .env
    TELEGRAM_API_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    if not TELEGRAM_API_TOKEN:
        print('Токен Telegram бота не установлен. Пожалуйста, установите переменную окружения TELEGRAM_BOT_TOKEN.')
        return
    
    init_db() # {{ 3. Инициализируем базу данных при запуске бота }}

    #создаем объект приложения
    application = Application.builder().token(TELEGRAM_API_TOKEN).get_updates_pool_timeout(20).build()

    #Регистрация команд
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('setcity', set_city))
    application.add_handler(CommandHandler('getcity', get_city))
    application.add_handler(CommandHandler('addfav', add_fav)) # {{ Регистрируем новую команду /addfav }}
    application.add_handler(CommandHandler('listfav', list_fav)) # {{ Регистрируем новую команду /listfav }}
    application.add_handler(CallbackQueryHandler(button_callback_handler)) # Новый обработчик для кнопок
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_city_input)) # Новый обработчик для текстового ввода
    application.add_handler(MessageHandler(filters.LOCATION, handle_location)) # Новый обработчик для локации

    #Запуск бота
    print('Бот запущен. Нажмите Ctrl+C для остановки')
    application.run_polling()

if __name__ == '__main__':
    main()
    
