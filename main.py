import requests
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

#Функция для получения данных о погоде
def get_weather( city: str, api_key: str) -> str:
    url = f'https://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}&units=metric&lang=ru'
    try:
        response = requests.get(url)
        data = response.json()
        if data.get('cod') != 200:
           return f"Ошибка: {data.get('message', 'Неизвестная ошибка')}"

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
    except Exception as e:
        return f'НЕ удалось получить данные о погоде. Ошибка: {e}'

#Команда /start 
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        'Привет! Я бот для проверки погоды. Используй команду /weather <город>, чтобы узнать погоду.\n Пример: /weather Москва\n'
        'Также можно узнать прогноз на 5 дней: /forecast <город>'
        )     

#Команда /weather
async def weather(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) == 0:
        await update.message.reply_text('Пожалуйста, укажите город. Пример: /weather Москва')
        return
    
    city = ' '.join(context.args)
    api_key = '6f9162cff16eb10bbc9f9513884c0cb0'
    weather_info = get_weather(city, api_key)
    await update.message.reply_text(weather_info)            

# Функция для получения прогноза на 5 дней
def get_forecast(city: str, api_key: str) -> str:
    url = f'https://api.openweathermap.org/data/2.5/forecast?q={city}&appid={api_key}&units=metric&lang=ru&cnt=40'  # 40 = 8 интервалов * 5 дней
    try:
        response = requests.get(url)
        data = response.json()
        
        if data.get('cod') != '200':
            return f"Ошибка: {data.get('message', 'Неизвестная ошибка')}"

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
        
    except Exception as e:
        return f'Не удалось получить прогноз погоды. Ошибка: {e}'

# Команда /forecast
async def forecast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) == 0:
        await update.message.reply_text('Пожалуйста, укажите город. Пример: /forecast Москва')
        return
    
    city = ' '.join(context.args)
    api_key = '6f9162cff16eb10bbc9f9513884c0cb0'
    forecast_info = get_forecast(city, api_key)
    await update.message.reply_text(forecast_info)

#Основная функция

def main():
    TELEGRAM_API_TOKEN = '7909129269:AAGaQHJ43rZAE49uuEmzd_ZoNlAS0txDRXc'

    #создаем объект приложения
    application = Application.builder().token(TELEGRAM_API_TOKEN).build()

    #Регистрация команд
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('weather', weather))
    application.add_handler(CommandHandler('forecast', forecast))

    #Запуск бота
    print('Бот запущен. Нажмите Ctrl+C для остановки')
    application.run_polling()

if __name__ == '__main__':
    main()
    
