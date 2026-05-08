import asyncio
import os
import json
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, CallbackQuery
)
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from tonnelmp import getGifts  # библиотека для работы с Tonnel API [citation:2][citation:4]

# ===== НАСТРОЙКИ =====
BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_БОТА")
ADMIN_ID = 123456789  # твой Telegram ID для доступа к админ-панели

# ===== КОНФИГУРАЦИЯ МАРЖИ =====
PROFIT_MARGIN_PERCENT = 20  # базовая наценка 20% (можно менять)

# Комиссия Tonnel при продаже = 10% [citation:4]
TONNEL_FEE_PERCENT = 10

# ===== КУРС TON (желательно обновлять через API CoinGecko) =====
TON_RATE_RUB = 106.0

# ===== КЭШ ЦЕН (чтобы не дёргать API каждую секунду) =====
PRICE_CACHE = {}
CACHE_EXPIRY_MINUTES = 3  # обновляем кэш каждые 3 минуты

# ===== СПИСОК ПОДАРКОВ, КОТОРЫЕ ВЫ ОТСЛЕЖИВАЕТЕ =====
TRACKED_GIFTS = [
    "jelly bunny",
    "lunar snake",
    "crystal heart",
    "toy bear",
    "winter wreath",
    "delicious cake",
    "desk calendar",
]

# ===== ИНИЦИАЛИЗАЦИЯ =====
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())


# ===== ФУНКЦИИ ДЛЯ РАБОТЫ С TONNEL API =====

def get_market_data(gift_name: str) -> dict:
    """
    Получает актуальные цены на подарок с Tonnel.
    Возвращает минимальную цену (floor) для разных моделей.
    """
    now = datetime.now()
    
    # Проверяем кэш
    if gift_name in PRICE_CACHE:
        cached_time, cached_data = PRICE_CACHE[gift_name]
        if now - cached_time < timedelta(minutes=CACHE_EXPIRY_MINUTES):
            return cached_data
    
    models = {}
    
    try:
        # Поиск по всем моделям подарка (сортируем по возрастанию цены) [citation:2][citation:4]
        results = getGifts(
            gift_name=gift_name,
            sort="price_asc",
            limit=30,  # максимум 30 записей за раз
            asset="TON"
        )
        
        if results:
            for gift in results:
                model = gift.get('model', 'Unknown')
                price = gift.get('price', gift.get('price', 0))
                
                # Сохраняем минимальную цену для каждой модели
                if model not in models or price < models[model]:
                    models[model] = price
        
        # Логируем успех [citation:5]
        logging.info(f"Получены цены для {gift_name}: найдено {len(models)} моделей")
        
    except Exception as e:
        logging.error(f"Ошибка получения данных для {gift_name}: {e}")
        # Возвращаем пустой словарь при ошибке API
        return {}
    
    PRICE_CACHE[gift_name] = (now, models)
    return models


def calculate_client_price(ton_price: float, margin_percent: float = None) -> dict:
    """
    Рассчитывает конечную цену для клиента.
    Включает: закупочную цену + наценку продавца + комиссию Tonnel (10%).
    """
    if margin_percent is None:
        margin_percent = PROFIT_MARGIN_PERCENT
    
    # Базовая цена (закупка)
    base_price = ton_price
    
    # Цена с наценкой продавца
    price_with_margin = base_price * (1 + margin_percent / 100)
    
    # Финальная цена с комиссией площадки 10% [citation:4]
    final_price = price_with_margin * (1 + TONNEL_FEE_PERCENT / 100)
    
    # Чистая прибыль (после всех комиссий)
    net_profit = price_with_margin - base_price
    
    return {
        "base_price_ton": round(base_price, 2),
        "with_margin_ton": round(price_with_margin, 2),
        "final_price_ton": round(final_price, 2),
        "final_price_rub": round(final_price * TON_RATE_RUB),
        "net_profit_ton": round(net_profit, 2),
        "net_profit_rub": round(net_profit * TON_RATE_RUB),
        "margin_percent": margin_percent,
        "tonnel_fee_ton": round(final_price * TONNEL_FEE_PERCENT / 100, 2),
    }


# ===== КОМАНДЫ БОТА =====

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Приветственное сообщение с кнопкой каталога"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🛍 КАТАЛОГ NFT-ПОДАРКОВ",
            callback_data="catalog"
        )],
        [InlineKeyboardButton(
            text="📖 Как купить?",
            callback_data="how_to_buy"
        )],
        [InlineKeyboardButton(
            text="💬 Связаться с продавцом",
            callback_data="contact"
        )],
    ])
    
    await message.answer(
        "🎁 <b>Магазин NFT-подарков Telegram</b>\n\n"
        "Все подарки — оригинальные, на блокчейне TON.\n"
        "Цены обновляются в реальном времени с Tonnel Marketplace.\n\n"
        "🔹 Нажмите <b>КАТАЛОГ</b>, чтобы посмотреть актуальные цены",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "catalog")
async def show_catalog(callback: CallbackQuery):
    """Показывает каталог подарков — ТОЛЬКО названия и модели.
       Клиент ещё не видит цен!"""
    text = "🎁 <b>Доступные подарки:</b>\n\n"
    text += "Выберите подарок, чтобы увидеть актуальную цену:\n"
    
    keyboard = []
    for gift_name in TRACKED_GIFTS:
        display_name = gift_name.title()
        keyboard.append([
            InlineKeyboardButton(
                text=f"🔹 {display_name}",
                callback_data=f"gift_{gift_name}"
            )
        ])
    
    keyboard.append([
        InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_start")
    ])
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )


@dp.callback_query(F.data.startswith("gift_"))
async def show_gift_models(callback: CallbackQuery):
    """Показывает модели конкретного подарка с ЦЕНАМИ (уже с наценкой)"""
    gift_name = callback.data.replace("gift_", "")
    display_name = gift_name.title()
    
    # Сообщаем, что загружаем данные
    await callback.message.edit_text(
        f"⏳ Загружаю актуальные цены для <b>{display_name}</b> с Tonnel Marketplace...",
        reply_markup=None
    )
    
    # Получаем реальные данные с API [citation:2][citation:3]
    market_data = get_market_data(gift_name)
    
    if not market_data:
        await callback.message.edit_text(
            f"❌ <b>{display_name}</b>\n\n"
            "К сожалению, сейчас нет активных предложений на Tonnel Marketplace.\n"
            "Попробуйте другой подарок или зайдите позже.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 К каталогу", callback_data="catalog")]
            ])
        )
        return
    
    # Формируем ответ с ценами
    text = f"🎁 <b>{display_name}</b>\n\n"
    text += "Доступные модели и цены:\n\n"
    
    keyboard = []
    for model, floor_price in sorted(market_data.items()):
        # Рассчитываем конечную цену для клиента
        pricing = calculate_client_price(floor_price)
        
        text += (
            f"▫️ <b>{model}</b>\n"
            f"   Цена: <b>{pricing['final_price_rub']} ₽</b>\n"
            f"   (~{pricing['final_price_ton']} TON)\n\n"
        )
    
    text += "<i>Цены обновлены только что с Tonnel Marketplace</i>"
    
    keyboard.append([
        InlineKeyboardButton(text="🔙 К каталогу", callback_data="catalog"),
        InlineKeyboardButton(text="💬 Купить", callback_data=f"buy_{gift_name}")
    ])
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )


@dp.callback_query(F.data.startswith("buy_"))
async def buy_gift(callback: CallbackQuery):
    """Обрабатывает намерение купить — даёт контакты"""
    gift_name = callback.data.replace("buy_", "")
    display_name = gift_name.title()
    
    await callback.message.edit_text(
        f"✅ <b>Отлично! Вы выбрали {display_name}</b>\n\n"
        "Чтобы купить подарок:\n"
        "1️⃣ Напишите мне в личные сообщения: <b>@твой_username</b>\n"
        "2️⃣ Укажите название подарка и желаемую модель\n"
        "3️⃣ Я подтвержу наличие и отправлю реквизиты для оплаты\n\n"
        "<i>Оплата: переводом на карту / USDT / TON</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 К каталогу", callback_data="catalog")]
        ])
    )


@dp.callback_query(F.data == "how_to_buy")
async def how_to_buy(callback: CallbackQuery):
    await callback.message.edit_text(
        "📖 <b>Как купить NFT-подарок:</b>\n\n"
        "1️⃣ Выберите подарок из каталога\n"
        "2️⃣ Выберите модель\n"
        "3️⃣ Вы увидите актуальную цену в рублях\n"
        "4️⃣ Свяжитесь со мной для оформления сделки\n"
        "5️⃣ После оплаты я отправлю подарок на ваш аккаунт\n\n"
        "🎁 Подарок появится в Telegram → Settings → Gifts",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛍 К каталогу", callback_data="catalog")]
        ])
    )


@dp.callback_query(F.data == "contact")
async def contact(callback: CallbackQuery):
    await callback.message.edit_text(
        "💬 <b>Связь с продавцом:</b>\n\n"
        "Telegram: @твой_username\n"
        "Отвечаю в течение 10 минут.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛍 К каталогу", callback_data="catalog")]
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
    СЕКРЕТНАЯ админ-панель.
    Доступна только вам. Показывает закупочные цены и чистую прибыль.
    """
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Нет доступа")
        return
    
    await message.answer("🔐 <b>АДМИН-ПАНЕЛЬ</b>\nЗагружаю данные с Tonnel Marketplace...")
    
    text = "🔐 <b>ЗАКУПОЧНЫЕ ЦЕНЫ И ПРИБЫЛЬ</b>\n\n"
    
    for gift_name in TRACKED_GIFTS:
        market_data = get_market_data(gift_name)
        if not market_data:
            continue
        
        text += f"<b>{gift_name.title()}</b>\n"
        
        for model, floor_price in sorted(market_data.items()):
            pricing = calculate_client_price(floor_price)
            
            text += (
                f"  {model}:\n"
                f"  ┃ Закуп: <b>{pricing['base_price_ton']} TON</b>\n"
                f"  ┃ Продажа: <b>{pricing['final_price_rub']} ₽</b> ({pricing['final_price_ton']} TON)\n"
                f"  ┃ Чистая прибыль: <b>+{pricing['net_profit_rub']} ₽</b> (+{pricing['net_profit_ton']} TON)\n"
                f"  ┃ Маржа: {pricing['margin_percent']}%\n\n"
            )
    
    await message.answer(text)


@dp.message(Command("update_prices"))
async def cmd_update_prices(message: types.Message):
    """Принудительное обновление кэша цен"""
    global PRICE_CACHE
    PRICE_CACHE = {}  # сбрасываем кэш
    
    await message.answer("🔄 <b>Кэш цен очищен.</b>\nПри следующем запросе цены обновятся с Tonnel Marketplace.")
    
    # Предзагружаем цены для всех отслеживаемых подарков
    for gift_name in TRACKED_GIFTS:
        get_market_data(gift_name)
    
    await message.answer("✅ Цены обновлены для всех подарков.")


# ===== ЗАПУСК БОТА =====
async def main():
    logging.info("Бот запущен. Отслеживаем подарки:")
    for gift in TRACKED_GIFTS:
        logging.info(f"  - {gift.title()}")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
