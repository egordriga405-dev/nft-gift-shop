"""
NFT Gift Shop Bot - Полностью автоматический магазин NFT-подарков Telegram
Все данные тянутся из реальных источников:
- Подарки и цены: Tonnel Marketplace API
- Курс TON/RUB: CoinGecko API
- Список подарков: динамически с маркетплейса
"""

import asyncio
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery
)
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

# ===== НАСТРОЙКИ =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден! Добавьте в Railway Variables")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Ваш Telegram ID

# Наценка (можно изменить)
PROFIT_MARGIN_PERCENT = 20  # Ваша наценка 20%

# Комиссия площадки
TONNEL_FEE_PERCENT = 10  # Комиссия Tonnel 10%

# ===== ИНИЦИАЛИЗАЦИЯ =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())

# ===== ГЛОБАЛЬНЫЙ КЭШ =====
TON_RATE_RUB = 100.0  # Будет обновляться через CoinGecko
GIFTS_CACHE = {}      # Кэш подарков с ценами
CACHE_TIME = None     # Время последнего обновления кэша
CACHE_DURATION = timedelta(minutes=5)  # Обновлять кэш каждые 5 минут


# =====================================================================
# API КЛИЕНТЫ
# =====================================================================

class CoinGeckoAPI:
    """Получение курса TON к RUB через CoinGecko"""
    
    BASE_URL = "https://api.coingecko.com/api/v3"
    
    @staticmethod
    async def get_ton_price_rub() -> float:
        """Получает актуальный курс TON к рублю"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{CoinGeckoAPI.BASE_URL}/simple/price"
                params = {
                    "ids": "the-open-network",
                    "vs_currencies": "rub"
                }
                async with session.get(url, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        price = data.get("the-open-network", {}).get("rub", 0)
                        logger.info(f"✅ Курс TON: {price} RUB")
                        return price
                    else:
                        logger.error(f"CoinGecko API error: {resp.status}")
                        return TON_RATE_RUB
        except Exception as e:
            logger.error(f"❌ Ошибка получения курса: {e}")
            return TON_RATE_RUB


class TonnelAPI:
    """Работа с Tonnel Marketplace API"""
    
    @staticmethod
    async def get_all_gifts() -> List[str]:
        """
        Получает список ВСЕХ доступных подарков прямо сейчас.
        Парсит главную страницу или использует поиск.
        """
        # Популярные подарки (заглушка, если API не даёт полный список)
        popular_gifts = [
            "jelly bunny", "lunar snake", "crystal heart",
            "toy bear", "winter wreath", "delicious cake",
            "desk calendar", "cute frog", "magic star",
            "golden bell", "pixel cat", "crown frog",
            "diamond ring", "lucky clover", "rainbow unicorn",
            "pepe dragon", "moon cake", "star cookie",
            "flame heart", "ice crystal"
        ]
        
        try:
            # Пытаемся получить список с Tonnel API
            # (используем tonnelmp библиотеку)
            from tonnelmp import searchGifts
            
            # Поиск по пустому запросу или "*" чтобы получить всё
            results = searchGifts(query="", limit=100, asset="TON")
            
            if results:
                # Извлекаем уникальные названия подарков
                gift_names = list(set(
                    gift.get("name", "").lower() 
                    for gift in results 
                    if gift.get("name")
                ))
                logger.info(f"✅ Получено {len(gift_names)} подарков с Tonnel")
                return gift_names[:50]  # Ограничиваем 50 для скорости
            
        except Exception as e:
            logger.warning(f"Не удалось получить список с Tonnel: {e}")
        
        return popular_gifts
    
    @staticmethod
    async def get_gift_prices(gift_name: str) -> Dict[str, float]:
        """
        Получает цены для конкретного подарка.
        Возвращает словарь {модель: минимальная_цена}
        """
        try:
            from tonnelmp import getGifts
            
            results = getGifts(
                gift_name=gift_name,
                sort="price_asc",
                limit=50,
                asset="TON"
            )
            
            if not results:
                return {}
            
            # Группируем по моделям и берём минимальную цену
            models = {}
            for gift in results:
                model = gift.get("model", "Unknown")
                price = float(gift.get("price", 0))
                
                if price > 0 and (model not in models or price < models[model]):
                    models[model] = price
            
            logger.info(f"✅ {gift_name}: {len(models)} моделей, цены от {min(models.values()):.2f} TON")
            return models
            
        except Exception as e:
            logger.error(f"❌ Ошибка для {gift_name}: {e}")
            return {}


# =====================================================================
# БИЗНЕС-ЛОГИКА
# =====================================================================

def calculate_final_price(buy_price_ton: float) -> dict:
    """
    Рассчитывает финальную цену для клиента.
    buy_price_ton - закупочная цена с маркетплейса
    """
    # Добавляем наценку
    with_margin = buy_price_ton * (1 + PROFIT_MARGIN_PERCENT / 100)
    
    # Добавляем комиссию площадки
    final_ton = with_margin * (1 + TONNEL_FEE_PERCENT / 100)
    
    # Переводим в рубли по актуальному курсу
    final_rub = round(final_ton * TON_RATE_RUB)
    
    # Чистая прибыль
    profit_ton = with_margin - buy_price_ton
    profit_rub = round(profit_ton * TON_RATE_RUB)
    
    return {
        "buy_ton": round(buy_price_ton, 2),
        "sell_ton": round(final_ton, 2),
        "sell_rub": final_rub,
        "profit_ton": round(profit_ton, 2),
        "profit_rub": profit_rub,
        "margin": PROFIT_MARGIN_PERCENT,
    }


async def refresh_cache():
    """Обновляет ВСЕ данные: курс TON и цены на подарки"""
    global TON_RATE_RUB, GIFTS_CACHE, CACHE_TIME
    
    logger.info("🔄 Обновляю кэш...")
    
    # 1. Обновляем курс TON
    ton_rate = await CoinGeckoAPI.get_ton_price_rub()
    if ton_rate > 0:
        TON_RATE_RUB = ton_rate
    
    # 2. Получаем список всех подарков
    gift_names = await TonnelAPI.get_all_gifts()
    
    # 3. Для каждого подарка получаем цены
    new_cache = {}
    for gift_name in gift_names[:30]:  # Ограничим 30 для скорости
        prices = await TonnelAPI.get_gift_prices(gift_name)
        if prices:
            new_cache[gift_name] = prices
    
    GIFTS_CACHE = new_cache
    CACHE_TIME = datetime.now()
    
    logger.info(f"✅ Кэш обновлён: {len(GIFTS_CACHE)} подарков, курс {TON_RATE_RUB} RUB/TON")


# =====================================================================
# КОМАНДЫ БОТА
# =====================================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Приветствие и главное меню"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🛍 КАТАЛОГ NFT-ПОДАРКОВ",
            callback_data="catalog"
        )],
        [InlineKeyboardButton(
            text="📊 ТОП-10 ПОДАРКОВ",
            callback_data="top10"
        )],
        [InlineKeyboardButton(
            text="💰 Курс TON",
            callback_data="ton_rate"
        )],
        [InlineKeyboardButton(
            text="📖 Как купить?",
            callback_data="how_to_buy"
        )],
        [InlineKeyboardButton(
            text="💬 Связаться",
            callback_data="contact"
        )],
    ])
    
    await message.answer(
        "🎁 <b>МАГАЗИН NFT-ПОДАРКОВ TELEGRAM</b>\n\n"
        f"💎 <b>Курс TON:</b> {TON_RATE_RUB:.0f} ₽\n"
        f"🛍 <b>Подарков в каталоге:</b> {len(GIFTS_CACHE)}\n"
        f"🔄 <b>Обновлено:</b> {CACHE_TIME.strftime('%H:%M:%S') if CACHE_TIME else 'сейчас'}\n\n"
        "Все подарки — оригинальные, на блокчейне TON.\n"
        "Цены обновляются каждые 5 минут с маркетплейса.\n\n"
        "Нажмите <b>КАТАЛОГ</b> чтобы посмотреть 👇",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "catalog")
async def show_catalog(callback: CallbackQuery):
    """Показывает КАТАЛОГ всех подарков с ценами"""
    
    # Обновляем кэш если нужно
    if not GIFTS_CACHE or not CACHE_TIME or datetime.now() - CACHE_TIME > CACHE_DURATION:
        await callback.message.edit_text("⏳ Обновляю данные с маркетплейса...")
        await callback.answer()
        await refresh_cache()
    
    if not GIFTS_CACHE:
        await callback.message.edit_text(
            "❌ Не удалось загрузить данные. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="catalog")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_start")]
            ])
        )
        return
    
    # Группируем подарки по страницам (по 8 на странице)
    gifts_list = sorted(GIFTS_CACHE.items())
    page = 0  # Начинаем с первой страницы
    
    await show_catalog_page(callback, gifts_list, page)


async def show_catalog_page(callback: CallbackQuery, gifts_list: List, page: int):
    """Показывает одну страницу каталога"""
    items_per_page = 8
    total_pages = (len(gifts_list) + items_per_page - 1) // items_per_page
    
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, len(gifts_list))
    page_gifts = gifts_list[start_idx:end_idx]
    
    text = f"🛍 <b>КАТАЛОГ ПОДАРКОВ</b>\n"
    text += f"Страница {page + 1} из {total_pages}\n\n"
    
    keyboard = []
    
    for gift_name, models in page_gifts:
        if not models:
            continue
        
        # Находим минимальную цену
        min_price_ton = min(models.values())
        pricing = calculate_final_price(min_price_ton)
        
        display_name = gift_name.title()
        
        text += f"<b>{display_name}</b>\n"
        text += f"┃ от <b>{pricing['sell_rub']:,} ₽</b> "
        text += f"({pricing['sell_ton']} TON)\n"
        text += f"┃ {len(models)} моделей\n\n"
        
        keyboard.append([
            InlineKeyboardButton(
                text=f"{display_name} | от {pricing['sell_rub']:,} ₽",
                callback_data=f"gift_{gift_name}"
            )
        ])
    
    # Кнопки навигации
    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"catpage_{page - 1}")
        )
    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"catpage_{page + 1}")
        )
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([
        InlineKeyboardButton(text="🔄 Обновить цены", callback_data="refresh_catalog"),
        InlineKeyboardButton(text="🏠 Главная", callback_data="back_to_start")
    ])
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("catpage_"))
async def handle_catalog_page(callback: CallbackQuery):
    """Обработчик переключения страниц каталога"""
    page = int(callback.data.replace("catpage_", ""))
    gifts_list = sorted(GIFTS_CACHE.items())
    await show_catalog_page(callback, gifts_list, page)


@dp.callback_query(F.data == "refresh_catalog")
async def refresh_catalog(callback: CallbackQuery):
    """Принудительное обновление каталога"""
    await callback.message.edit_text("⏳ Обновляю все данные...")
    await refresh_cache()
    await show_catalog(callback)


@dp.callback_query(F.data.startswith("gift_"))
async def show_gift_details(callback: CallbackQuery):
    """Показывает детальную информацию о подарке и его моделях"""
    gift_name = callback.data.replace("gift_", "")
    display_name = gift_name.title()
    
    # Получаем свежие цены для этого подарка
    await callback.message.edit_text(f"⏳ Загружаю цены для <b>{display_name}</b>...")
    
    models = await TonnelAPI.get_gift_prices(gift_name)
    
    if not models:
        await callback.message.edit_text(
            f"❌ <b>{display_name}</b>\n\n"
            "Сейчас нет активных предложений.\n"
            "Попробуйте позже или выберите другой подарок.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 В каталог", callback_data="catalog")]
            ])
        )
        return
    
    text = f"🎁 <b>{display_name}</b>\n\n"
    text += "<b>Доступные модели:</b>\n\n"
    
    keyboard = []
    
    # Сортируем модели по цене
    sorted_models = sorted(models.items(), key=lambda x: x[1])
    
    for model, price_ton in sorted_models:
        pricing = calculate_final_price(price_ton)
        
        # Определяем эмодзи для модели
        model_emoji = {
            "Common": "⚪",
            "Patterned Accent": "🔵",
            "Ornament": "🟣",
            "Brilliant": "💎",
            "Holographic": "🌈",
        }.get(model, "✨")
        
        text += (
            f"{model_emoji} <b>{model}</b>\n"
            f"   💰 <b>{pricing['sell_rub']:,} ₽</b>\n"
            f"   💎 {pricing['sell_ton']} TON\n\n"
        )
    
    text += f"<i>Курс: {TON_RATE_RUB:.0f} ₽/TON | Обновлено: {datetime.now().strftime('%H:%M')}</i>"
    
    keyboard.append([
        InlineKeyboardButton(text="💬 Хочу купить", callback_data=f"buy_{gift_name}"),
        InlineKeyboardButton(text="🔙 В каталог", callback_data="catalog")
    ])
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )


@dp.callback_query(F.data.startswith("buy_"))
async def buy_gift(callback: CallbackQuery):
    """Обработка намерения купить"""
    gift_name = callback.data.replace("buy_", "")
    display_name = gift_name.title()
    
    text = (
        f"✅ <b>Отлично! Вы выбрали {display_name}</b>\n\n"
        "Для покупки:\n"
        "1️⃣ Напишите мне: <b>@your_telegram_username</b>\n"
        "2️⃣ Укажите название подарка и модель\n"
        "3️⃣ Я подтвержу наличие и отправлю реквизиты\n\n"
        "<i>Оплата: перевод на карту / TON / USDT</i>"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 В каталог", callback_data="catalog"),
             InlineKeyboardButton(text="🏠 Главная", callback_data="back_to_start")]
        ])
    )


@dp.callback_query(F.data == "top10")
async def show_top10(callback: CallbackQuery):
    """ТОП-10 самых дорогих/популярных подарков"""
    if not GIFTS_CACHE:
        await refresh_cache()
    
    # Сортируем по минимальной цене (самые дорогие)
    sorted_gifts = sorted(
        GIFTS_CACHE.items(),
        key=lambda x: min(x[1].values()) if x[1] else 0,
        reverse=True
    )[:10]
    
    text = "🏆 <b>ТОП-10 ПОДАРКОВ</b>\n\n"
    
    for i, (gift_name, models) in enumerate(sorted_gifts, 1):
        if not models:
            continue
        min_price = min(models.values())
        pricing = calculate_final_price(min_price)
        
        text += f"{i}. <b>{gift_name.title()}</b>\n"
        text += f"   от {pricing['sell_rub']:,} ₽ ({len(models)} моделей)\n\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛍 Полный каталог", callback_data="catalog"),
             InlineKeyboardButton(text="🏠 Главная", callback_data="back_to_start")]
        ])
    )


@dp.callback_query(F.data == "ton_rate")
async def show_ton_rate(callback: CallbackQuery):
    """Показывает актуальный курс TON"""
    rate = await CoinGeckoAPI.get_ton_price_rub()
    
    text = (
        "💰 <b>КУРС TON</b>\n\n"
        f"1 TON = <b>{rate:.2f} ₽</b>\n\n"
        f"<i>Данные: CoinGecko</i>\n"
        f"<i>Обновлено: {datetime.now().strftime('%H:%M:%S')}</i>"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="ton_rate"),
             InlineKeyboardButton(text="🏠 Главная", callback_data="back_to_start")]
        ])
    )


@dp.callback_query(F.data == "how_to_buy")
async def how_to_buy(callback: CallbackQuery):
    await callback.message.edit_text(
        "📖 <b>КАК КУПИТЬ NFT-ПОДАРОК</b>\n\n"
        "1️⃣ Выберите подарок из каталога\n"
        "2️⃣ Выберите модель\n"
        "3️⃣ Свяжитесь со мной для оформления\n"
        "4️⃣ Оплатите удобным способом\n"
        "5️⃣ Я отправлю подарок на ваш аккаунт\n\n"
        "🎁 Подарок появится в Telegram → Settings → Gifts\n"
        "🔗 Можно вывести на TON-кошелёк через Fragment",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛍 В каталог", callback_data="catalog"),
             InlineKeyboardButton(text="🏠 Главная", callback_data="back_to_start")]
        ])
    )


@dp.callback_query(F.data == "contact")
async def contact(callback: CallbackQuery):
    await callback.message.edit_text(
        "💬 <b>СВЯЗЬ С ПРОДАВЦОМ</b>\n\n"
        "Telegram: <b>@your_telegram_username</b>\n\n"
        "Отвечаю в течение 10-15 минут.\n"
        "Работаю ежедневно.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Главная", callback_data="back_to_start")]
        ])
    )


@dp.callback_query(F.data == "back_to_start")
async def back_to_start(callback: CallbackQuery):
    """Возврат в главное меню"""
    await cmd_start(callback.message)
    await callback.answer()


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    """
    🔐 АДМИН-ПАНЕЛЬ
    Показывает реальные закупочные цены и чистую прибыль.
    """
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Доступ запрещён")
        return
    
    await message.answer("🔐 Загружаю данные...")
    
    if not GIFTS_CACHE:
        await refresh_cache()
    
    text = "🔐 <b>АДМИН-ПАНЕЛЬ</b>\n"
    text += f"Курс: {TON_RATE_RUB:.0f} ₽/TON | Маржа: {PROFIT_MARGIN_PERCENT}%\n\n"
    
    total_profit = 0
    
    for gift_name, models in sorted(GIFTS_CACHE.items()):
        if not models:
            continue
        
        text += f"<b>{gift_name.title()}</b>\n"
        
        for model, price_ton in sorted(models.items(), key=lambda x: x[1]):
            pricing = calculate_final_price(price_ton)
            
            text += (
                f"  {model}:\n"
                f"  ┃ Закуп: {pricing['buy_ton']} TON\n"
                f"  ┃ Продажа: {pricing['sell_rub']:,} ₽ ({pricing['sell_ton']} TON)\n"
                f"  ┃ Прибыль: +{pricing['profit_rub']:,} ₽\n\n"
            )
            total_profit += pricing['profit_ton']
    
    text += f"<b>💰 Суммарная прибыль по всем моделям: +{total_profit:.1f} TON</b>"
    
    await message.answer(text)


@dp.message(Command("refresh"))
async def cmd_refresh(message: types.Message):
    """Принудительное обновление ВСЕХ данных"""
    await message.answer("🔄 Обновляю все данные с маркетплейсов...")
    await refresh_cache()
    await message.answer(
        f"✅ Готово!\n"
        f"📊 Подарков загружено: {len(GIFTS_CACHE)}\n"
        f"💰 Курс TON: {TON_RATE_RUB:.0f} ₽"
    )


# =====================================================================
# ЗАПУСК
# =====================================================================

async def on_startup():
    """Действия при запуске бота"""
    logger.info("🚀 Бот запускается...")
    logger.info(f"📊 Наценка: {PROFIT_MARGIN_PERCENT}%")
    logger.info(f"💸 Комиссия площадки: {TONNEL_FEE_PERCENT}%")
    
    # Первичная загрузка данных
    await refresh_cache()
    logger.info("✅ Бот готов к работе!")


async def main():
    """Точка входа"""
    await on_startup()
    
    # Запускаем бота
    logger.info("🤖 Запуск polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
