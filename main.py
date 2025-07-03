import os
import random
import time
import threading
from datetime import datetime
import logging
from typing import Dict, Tuple, List, Optional
import requests
import json
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
    CallbackQueryHandler,
    ConversationHandler,
)
from telegram.error import BadRequest
from dotenv import load_dotenv

load_dotenv()  # Загружает переменные из .env

# Получаем настройки из .env
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CRYPTO_BOT_API_KEY = os.getenv('CRYPTOBOT_API_TOKEN')
ADMIN_IDS = [int(id.strip()) for id in os.getenv('ADMIN_IDS').split(',')]  # Для нескольких ID
MIN_BET = float(os.getenv('MIN_BET'))
MAX_BET = int(os.getenv('MAX_BET'))
SUPPORT_USERNAME = os.getenv('SUPPORT_USERNAME', '@CasaSupport')

# Проверка токена
if not TOKEN:
    raise ValueError("Не указан TELEGRAM_BOT_TOKEN в .env файле")
if not CRYPTO_BOT_API_KEY:
    raise ValueError("Не указан CRYPTOBOT_API_TOKEN в .env файле")

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния для ConversationHandler
DEPOSIT_AMOUNT, GAME_CHOICE, ROCKET_BET, MATRIX_BET, DICE_BET, ADMIN_ADD_BALANCE = range(6)

# Данные пользователей (в реальном приложении нужно использовать базу данных)
users_db = {}
active_rocket_games = {}
active_matrix_games = {}
active_dice_games = {}
active_invoices = {}

# Настройки для ракетки
ROCKET_CRASH_PROBABILITIES = [
    (1.0, 0.70),  # 70% chance to crash before 1.5x
    (1.5, 0.85),  # 15% chance to crash between 1.5x-3x (85% cumulative)
    (3.0, 0.95),  # 10% chance to crash between 3x-5x (95% cumulative)
    (5.0, 0.99),  # 4% chance to crash between 5x-25x (99% cumulative)
    (25.0, 1.0)   # 1% chance to crash after 25x (100% cumulative)
]

# Обновленные множители для Матрицы (1.2x на каждом уровне, чтобы казино было в плюсе)
MATRIX_MULTIPLIERS = [1.2 ** i for i in range(1, 10)]  # [1.2, 1.44, 1.728, 2.0736, ..., 5.15978]
DICE_MULTIPLIERS = {
    1: 2.0,  # Угадал чет/нечет
    2: 6.0,  # Угадал точное число
}

# Работа с пользователями
USER_FILE = "users.json"

def load_users():
    try:
        with open(USER_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        with open(USER_FILE, "w") as f:
            json.dump({}, f)
        return {}

def save_users(users):
    with open(USER_FILE, "w") as f:
        json.dump(users, f, indent=4)

def get_balance(user_id):
    users = load_users()
    return users.get(str(user_id), {}).get("balance", 0)

def update_balance(user_id, amount):
    users = load_users()
    user = users.setdefault(str(user_id), {"balance": 0, "username": ""})
    user["balance"] += amount
    save_users(users)

class User:
    def __init__(self, user_id: int, username: str = None):
        self.user_id = user_id
        self.username = username
        self.balance = float(get_balance(user_id))
        self.total_bets = 0.0
        self.total_wins = 0.0
        self.games_played = 0
        self.last_active = datetime.now()
        self.deposit_history = []
        self.withdraw_history = []
        self.is_admin = user_id in ADMIN_IDS

    def deposit(self, amount: float):
        self.balance += amount
        update_balance(self.user_id, amount)
        self.deposit_history.append((datetime.now(), amount))

    def withdraw(self, amount: float) -> bool:
        if self.balance >= amount:
            self.balance -= amount
            update_balance(self.user_id, -amount)
            self.withdraw_history.append((datetime.now(), amount))
            return True
        return False

    def add_win(self, amount: float):
        self.total_wins += amount

    def add_bet(self, amount: float):
        self.total_bets += amount
        self.games_played += 1

    def get_profile(self) -> str:
        return (
            "┌ Имя: @{}\n"
            "├ Баланс: {:.2f} $\n"
            "└ Всего выиграно: {:.2f} $".format(
                self.username if self.username else "не указано",
                self.balance,
                self.total_wins
            )
        )

    def get_stats(self) -> str:
        return (
            f"👤 ID: {self.user_id}\n"
            f"📛 Username: @{self.username if self.username else 'нет'}\n"
            f"💰 Баланс: {self.balance:.2f} $\n"
            f"🎰 Игр сыграно: {self.games_played}\n"
            f"🏆 Всего выиграно: {self.total_wins:.2f} $\n"
            f"💸 Всего поставлено: {self.total_bets:.2f} $\n"
            f"📊 Профит: {self.total_wins - self.total_bets:.2f} $"
        )


def get_user(user_id: int, username: str = None) -> User:
    if user_id not in users_db:
        users_db[user_id] = User(user_id, username)
    elif username and not users_db[user_id].username:
        users_db[user_id].username = username
    return users_db[user_id]


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def create_crypto_invoice(user_id: int, amount: float) -> Optional[str]:
    """Создает платеж через CryptoBot API"""
    headers = {
        "Crypto-Pay-API-Token": CRYPTO_BOT_API_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "asset": "USDT",
        "amount": amount,
        "description": f"Пополнение баланса на {amount}$",
        "hidden_message": "Спасибо за оплату! Баланс будет зачислен автоматически.",
        "payload": f"{user_id}:{amount}",
        "allow_comments": False
    }

    try:
        response = requests.post(
            "https://pay.crypt.bot/api/createInvoice",
            headers=headers,
            json=payload
        )
        response.raise_for_status()
        data = response.json()

        if data.get("ok"):
            invoice = data["result"]
            active_invoices[invoice["invoice_id"]] = {
                "user_id": user_id,
                "amount": amount,
                "paid": False
            }
            return invoice["pay_url"]
        return None

    except Exception as e:
        logger.error(f"Error creating CryptoBot payment: {str(e)}")
        return None


def check_invoices(context: CallbackContext):
    """Проверяет оплаченные инвойсы"""
    if not active_invoices:
        return

    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_API_KEY}
    try:
        response = requests.get("https://pay.crypt.bot/api/getInvoices", headers=headers)
        data = response.json()
    except Exception as e:
        logger.error(f"Ошибка при запросе инвойсов: {e}")
        return

    if data.get("ok"):
        result = data.get("result")
        if not isinstance(result, dict) or "items" not in result:
            logger.error(f"Unexpected result structure: {result}")
            return

        invoices = result["items"]
        if not isinstance(invoices, list):
            logger.error(f"Unexpected invoices type: {type(invoices)}, content: {invoices}")
            return

        for invoice in invoices:
            if not isinstance(invoice, dict):
                logger.error(f"Unexpected invoice type: {type(invoice)}, content: {invoice}")
                continue

            if invoice.get("status") == "paid":
                inv_id = invoice.get("invoice_id")
                if inv_id in active_invoices and not active_invoices[inv_id]["paid"]:
                    user_id = active_invoices[inv_id]["user_id"]
                    amount = active_invoices[inv_id]["amount"]
                    user = get_user(user_id)
                    user.deposit(amount)
                    active_invoices[inv_id]["paid"] = True
                    try:
                        context.bot.send_message(
                            user_id,
                            f"✅ Оплата на {amount}$ получена. Баланс пополнен."
                        )
                    except Exception as e:
                        logger.error(f"Ошибка отправки сообщения: {e}")


# ==================== Основные команды ====================

def start(update: Update, context: CallbackContext) -> None:
    user = get_user(update.effective_user.id, update.effective_user.username)

    text = (
        f"🎰 Добро пожаловать в *Casa Casino*!\n\n"
        f"💰 Ваш баланс: *{user.balance:.2f} $*\n"
    )

    keyboard = [
        [InlineKeyboardButton("🎮 Играть", callback_data='play_game')],
        [
            InlineKeyboardButton("❓ Помощь", callback_data='help'),
            InlineKeyboardButton("📊 Профиль", callback_data='profile')
        ]
    ]

    if user.is_admin:
        keyboard.append([InlineKeyboardButton("👑 Админ-панель", callback_data='admin_panel')])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        update.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        update.callback_query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )


def play_game(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()

    keyboard = [
        [InlineKeyboardButton("🚀 Ракетка", callback_data='game_rocket')],
        [InlineKeyboardButton("🔢 Матрица", callback_data='game_matrix')],
        [InlineKeyboardButton("🎲 Кости", callback_data='game_dice')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
    ]

    query.edit_message_text(
        text="🎮 Выберите игру:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


def help_command(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()

    text = (
        f"🆘 *Помощь*\n\n"
        f"Если у вас возникли вопросы или проблемы, обратитесь к нашему менеджеру: {SUPPORT_USERNAME}\n\n"
        f"Техническая поддержка работает 24/7 и ответит вам в течение 15 минут."
    )

    query.edit_message_text(
        text=text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
        ])
    )


def safe_answer_query(query, text=None):
    """Безопасно отвечает на callback-запрос"""
    try:
        query.answer(text=text)
        return True
    except BadRequest as e:
        if "Query is too old" in str(e) or "query id is invalid" in str(e):
            # Запрос устарел - ничего не делаем
            return False
        raise  # Если другая ошибка - прокидываем её дальше


def safe_edit_message(query, text, reply_markup=None, parse_mode=None):
    """Безопасно редактирует сообщение"""
    try:
        query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
        return True
    except BadRequest as e:
        if "Message is not modified" in str(e):
            # Сообщение не изменилось - это не ошибка
            return True
        if "Message to edit not found" in str(e):
            # Сообщение удалено или недоступно
            return False
        raise  # Если другая ошибка - прокидываем её дальше


def profile_command(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not safe_answer_query(query):
        return  # Запрос устарел - выходим

    user = get_user(query.from_user.id)
    text = f"📊 *Профиль*\n\n{user.get_profile()}"

    keyboard = [
        [InlineKeyboardButton("💳 Пополнить баланс", callback_data='deposit')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
    ]

    safe_edit_message(
        query,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


def deposit(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()

    query.edit_message_text(
        text="💳 Введите сумму для пополнения (минимум 1 $):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data='profile')]
        ])
    )

    return DEPOSIT_AMOUNT


def deposit_amount(update: Update, context: CallbackContext) -> int:
    try:
        amount = float(update.message.text)
        if amount < 1:
            update.message.reply_text(
                "Минимальная сумма пополнения - 1 $",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Назад", callback_data='profile')]
                ])
            )
            return DEPOSIT_AMOUNT
    except ValueError:
        update.message.reply_text(
            "Пожалуйста, введите корректную сумму (число).",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='profile')]
            ])
        )
        return DEPOSIT_AMOUNT

    user = get_user(update.effective_user.id)

    # Создаем платеж через CryptoBot
    payment_url = create_crypto_invoice(user.user_id, amount)

    if not payment_url:
        update.message.reply_text(
            "Ошибка при создании платежа. Пожалуйста, попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='profile')]
            ])
        )
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("💳 Перейти к оплате", url=payment_url)],
        [InlineKeyboardButton("🔙 В профиль", callback_data='profile')]
    ]

    update.message.reply_text(
        text=f"✅ Для пополнения баланса на *{amount:.2f} $* перейдите по ссылке ниже:\n\n"
             f"После оплаты средства будут зачислены автоматически.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return ConversationHandler.END


# ==================== Игры ====================

def game_choice(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()

    game_type = query.data.split('_')[1]

    if game_type == 'rocket':
        query.edit_message_text(
            text="🚀 *Игра Ракетка*\n\n"
                 "Правила:\n"
                 "1. Сделайте ставку\n"
                 "2. Ракетка взлетает, множитель растет\n"
                 "3. Нажмите 'Забрать' до взрыва ракетки\n"
                 "4. Если успеете - получаете ставку × множитель!\n\n"
                 "Введите сумму ставки (от 0.1 $):",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return ROCKET_BET
    elif game_type == 'matrix':
        query.edit_message_text(
            text="🔢 *Игра Матрица*\n\n"
                 "Правила:\n"
                 "1. Сделайте ставку\n"
                 "2. В каждой строке 5 клеток (4 выигрышные, 1 бомба)\n"
                 "3. Выбирайте клетки, пока не попадете на бомбу\n"
                 "4. Чем дальше пройдете, тем выше множитель!\n\n"
                 "Введите сумму ставки (от 0.1 $):",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return MATRIX_BET
    elif game_type == 'dice':
        query.edit_message_text(
            text="🎲 *Игра в Кости*\n\n"
                 "Правила:\n"
                 "1. Сделайте ставку\n"
                 "2. Выберите тип ставки:\n"
                 "   - Чет/Нечет (x2.0)\n"
                 "   - Конкретное число (x6.0)\n"
                 "3. Бот бросает кости\n"
                 "4. Если угадали - получаете выигрыш!\n\n"
                 "Введите сумму ставки (от 0.1 $):",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return DICE_BET


def rocket_bet(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    user = get_user(user_id)

    try:
        bet_amount = float(update.message.text)
    except ValueError:
        update.message.reply_text(
            "Пожалуйста, введите корректную сумму ставки (число).",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return ROCKET_BET

    if bet_amount < MIN_BET:
        update.message.reply_text(
            f"Минимальная ставка: {MIN_BET} $",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return ROCKET_BET
    if bet_amount > MAX_BET:
        update.message.reply_text(
            f"Максимальная ставка: {MAX_BET} $",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return ROCKET_BET
    if bet_amount > user.balance:
        update.message.reply_text(
            "Недостаточно средств на балансе.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return ROCKET_BET

    # Проверяем, нет ли уже активной игры
    if user_id in active_rocket_games:
        update.message.reply_text(
            "У вас уже есть активная игра. Дождитесь её завершения.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return ROCKET_BET

    # Снимаем деньги со счета
    user.withdraw(bet_amount)
    user.add_bet(bet_amount)

    # Генерируем точку взрыва согласно новым вероятностям
    rand = random.random()
    crash_at = 1.0

    for threshold, prob in ROCKET_CRASH_PROBABILITIES:
        if rand <= prob:
            # Линейная интерполяция между предыдущим и текущим порогом
            if ROCKET_CRASH_PROBABILITIES.index((threshold, prob)) == 0:
                # Первый диапазон
                crash_at = 1.0 + (threshold - 1.0) * rand / prob
            else:
                prev_threshold, prev_prob = ROCKET_CRASH_PROBABILITIES[
                    ROCKET_CRASH_PROBABILITIES.index((threshold, prob)) - 1]
                segment_prob = (rand - prev_prob) / (prob - prev_prob)
                crash_at = prev_threshold + (threshold - prev_threshold) * segment_prob
            break

    # Создаем игру
    active_rocket_games[user_id] = {
        'bet': bet_amount,
        'multiplier': 1.0,
        'crashed': False,
        'crash_at': crash_at,
        'message_id': None,
        'chat_id': None
    }

    # Запускаем игру
    run_rocket_game(context, user_id)

    return ConversationHandler.END


def run_rocket_game(context: CallbackContext, user_id: int):
    game = active_rocket_games[user_id]
    user = get_user(user_id)

    start_time = time.time()
    crash_time = game['crash_at'] * 3  # Время до взрыва пропорционально множителю

    # Отправляем стартовое сообщение
    message = context.bot.send_message(
        chat_id=user_id,
        text=f"🚀 Ракетка взлетает!\n\nСтавка: {game['bet']:.2f} $\nМножитель: x1.00",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Забрать", callback_data='rocket_cashout')],
            [InlineKeyboardButton("🔙 В меню", callback_data='back_to_menu')]
        ])
    )
    game['message_id'] = message.message_id
    game['chat_id'] = message.chat_id

    def update_multiplier(context: CallbackContext):
        if user_id not in active_rocket_games:
            return

        elapsed = time.time() - start_time
        current_multiplier = 1.0 + (game['crash_at'] - 1.0) * (elapsed / crash_time)

        if current_multiplier >= game['crash_at'] or game['crashed']:
            if not game['crashed']:
                game['crashed'] = True
                try:
                    context.bot.edit_message_text(
                        chat_id=game['chat_id'],
                        message_id=game['message_id'],
                        text=f"💥 Ракетка взорвалась на x{game['multiplier']:.2f}!\n\nСтавка: {game['bet']:.2f} $\nВы проиграли.",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
                        ])
                    )
                except BadRequest:
                    pass
                if user_id in active_rocket_games:
                    del active_rocket_games[user_id]
            return

        game['multiplier'] = current_multiplier
        try:
            context.bot.edit_message_text(
                chat_id=game['chat_id'],
                message_id=game['message_id'],
                text=f"🚀 Ракетка летит!\n\nСтавка: {game['bet']:.2f} $\nМножитель: x{game['multiplier']:.2f}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💰 Забрать", callback_data='rocket_cashout')],
                    [InlineKeyboardButton("🔙 В меню", callback_data='back_to_menu')]
                ])
            )
        except BadRequest:
            pass
        context.job_queue.run_once(update_multiplier, 0.1)

    context.job_queue.run_once(update_multiplier, 0.1)


def rocket_cashout(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()

    user_id = query.from_user.id
    if user_id not in active_rocket_games:
        query.edit_message_text(
            "Игра уже завершена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )
        return

    game = active_rocket_games[user_id]
    if game['crashed']:
        query.edit_message_text(
            "Игра уже завершена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )
        return

    # Помечаем игру как завершенную
    game['crashed'] = True

    # Вычисляем выигрыш
    win_amount = game['bet'] * game['multiplier']
    user = get_user(user_id)
    user.deposit(win_amount)
    user.add_win(win_amount)

    # Обновляем сообщение
    query.edit_message_text(
        text=f"🎉 Вы успешно забрали выигрыш!\n\n"
             f"Ставка: {game['bet']:.2f} $\n"
             f"Множитель: x{game['multiplier']:.2f}\n"
             f"Выигрыш: {win_amount:.2f} $",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
        ])
    )

    # Удаляем игру из активных
    if user_id in active_rocket_games:
        del active_rocket_games[user_id]


def matrix_bet(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    user = get_user(user_id)

    try:
        bet_amount = float(update.message.text)
    except ValueError:
        update.message.reply_text(
            "Пожалуйста, введите корректную сумму ставки (число).",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return MATRIX_BET

    if bet_amount < MIN_BET:
        update.message.reply_text(
            f"Минимальная ставка: {MIN_BET} $",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return MATRIX_BET
    if bet_amount > MAX_BET:
        update.message.reply_text(
            f"Максимальная ставка: {MAX_BET} $",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return MATRIX_BET
    if bet_amount > user.balance:
        update.message.reply_text(
            "Недостаточно средств на балансе.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return MATRIX_BET

    # Проверяем, нет ли уже активной игры
    if user_id in active_matrix_games:
        update.message.reply_text(
            "У вас уже есть активная игра. Дождитесь её завершения.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return MATRIX_BET

    # Снимаем деньги со счета
    user.withdraw(bet_amount)
    user.add_bet(bet_amount)

    # Создаем игру
    active_matrix_games[user_id] = {
        'bet': bet_amount,
        'current_level': 0,
        'message_id': None,
        'chat_id': None
    }

    # Показываем первый уровень
    show_matrix_level(context, user_id)

    return ConversationHandler.END


def show_matrix_level(context: CallbackContext, user_id: int):
    game = active_matrix_games[user_id]
    user = get_user(user_id)

    if game['current_level'] == 0:
        # На первом уровне нельзя забрать деньги
        cashout_text = "❌ На первом уровне нельзя забрать выигрыш"
        cashout_disabled = True
    else:
        cashout_text = f"💰 Забрать {game['bet'] * MATRIX_MULTIPLIERS[game['current_level'] - 1]:.2f} $"
        cashout_disabled = False

    if game['current_level'] >= len(MATRIX_MULTIPLIERS):
        # Игрок прошел все уровни
        win_amount = game['bet'] * MATRIX_MULTIPLIERS[-1]
        user.deposit(win_amount)
        user.add_win(win_amount)

        try:
            context.bot.edit_message_text(
                chat_id=game['chat_id'],
                message_id=game['message_id'],
                text=f"🏆 *Поздравляем!* 🏆\n\n"
                     f"Вы прошли все уровни Матрицы!\n\n"
                     f"Ставка: {game['bet']:.2f} $\n"
                     f"Множитель: x{MATRIX_MULTIPLIERS[-1]:.2f}\n"
                     f"Выигрыш: {win_amount:.2f} $\n\n"
                     "Невероятный результат!",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
                ])
            )
        except BadRequest:
            pass

        del active_matrix_games[user_id]
        return

    # Создаем клавиатуру с 5 кнопками (4 правильные, 1 бомба)
    bomb_position = random.randint(1, 5)
    keyboard = []
    for i in range(1, 6):
        if i == bomb_position:
            callback_data = 'matrix_bomb'
        else:
            callback_data = f'matrix_correct_{i}'
        keyboard.append([InlineKeyboardButton(f"🔷 Клетка {i}", callback_data=callback_data)])

    # Добавляем кнопку для выхода (если не первый уровень)
    if not cashout_disabled:
        keyboard.append([InlineKeyboardButton(cashout_text, callback_data='matrix_cashout')])
    else:
        keyboard.append([InlineKeyboardButton(cashout_text, callback_data='matrix_disabled')])

    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data='back_to_menu')])

    # Отправляем или обновляем сообщение
    if game['message_id'] is None:
        message = context.bot.send_message(
            chat_id=user_id,
            text=f"🔢 *Уровень {game['current_level'] + 1}*\n\n"
                 f"Ставка: {game['bet']:.2f} $\n"
                 f"Текущий множитель: x{MATRIX_MULTIPLIERS[game['current_level']]:.2f}\n"
                 f"Выигрыш при выходе: {game['bet'] * MATRIX_MULTIPLIERS[game['current_level']]:.2f} $\n\n"
                 "Выберите клетку:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        game['message_id'] = message.message_id
        game['chat_id'] = message.chat_id
    else:
        try:
            context.bot.edit_message_text(
                chat_id=game['chat_id'],
                message_id=game['message_id'],
                text=f"🔢 *Уровень {game['current_level'] + 1}*\n\n"
                     f"Ставка: {game['bet']:.2f} $\n"
                     f"Текущий множитель: x{MATRIX_MULTIPLIERS[game['current_level']]:.2f}\n"
                     f"Выигрыш при выходе: {game['bet'] * MATRIX_MULTIPLIERS[game['current_level']]:.2f} $\n\n"
                     "Выберите клетку:",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except BadRequest:
            pass


def matrix_choice(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()

    user_id = query.from_user.id
    if user_id not in active_matrix_games:
        query.edit_message_text(
            "Игра уже завершена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )
        return

    game = active_matrix_games[user_id]
    user = get_user(user_id)

    if query.data == 'matrix_disabled':
        query.answer(text="На первом уровне нельзя забрать выигрыш!", show_alert=True)
        return
    elif query.data.startswith('matrix_correct'):
        # Игрок выбрал правильную клетку - переходим на следующий уровень
        game['current_level'] += 1
        show_matrix_level(context, user_id)
    elif query.data == 'matrix_bomb':
        # Игрок выбрал бомбу - игра окончена
        query.edit_message_text(
            text=f"💥 Бомба! Игра окончена.\n\n"
                 f"Ставка: {game['bet']:.2f} $\n"
                 f"Вы проиграли свою ставку 😢",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )
        del active_matrix_games[user_id]
    elif query.data == 'matrix_cashout':
        # Игрок решил забрать выигрыш
        win_amount = game['bet'] * MATRIX_MULTIPLIERS[game['current_level'] - 1]
        user.deposit(win_amount)
        user.add_win(win_amount)

        query.edit_message_text(
            text=f"🎉 Вы забрали выигрыш!\n\n"
                 f"Ставка: {game['bet']:.2f} $\n"
                 f"Множитель: x{MATRIX_MULTIPLIERS[game['current_level'] - 1]:.2f}\n"
                 f"Выигрыш: {win_amount:.2f} $",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )
        del active_matrix_games[user_id]


def dice_bet(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    user = get_user(user_id)

    try:
        bet_amount = float(update.message.text)
    except ValueError:
        update.message.reply_text(
            "Пожалуйста, введите корректную сумму ставки (число).",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return DICE_BET

    if bet_amount < MIN_BET:
        update.message.reply_text(
            f"Минимальная ставка: {MIN_BET} $",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return DICE_BET
    if bet_amount > MAX_BET:
        update.message.reply_text(
            f"Максимальная ставка: {MAX_BET} $",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return DICE_BET
    if bet_amount > user.balance:
        update.message.reply_text(
            "Недостаточно средств на балансе.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return DICE_BET

    # Проверяем, нет ли уже активной игры
    if user_id in active_dice_games:
        update.message.reply_text(
            "У вас уже есть активная игра. Дождитесь её завершения.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return DICE_BET

    # Снимаем деньги со счета
    user.withdraw(bet_amount)
    user.add_bet(bet_amount)

    # Создаем игру
    active_dice_games[user_id] = {
        'bet': bet_amount,
        'message_id': None,
        'chat_id': None
    }

    # Показываем выбор типа ставки
    keyboard = [
        [
            InlineKeyboardButton("Чёт", callback_data='dice_even'),
            InlineKeyboardButton("Нечёт", callback_data='dice_odd')
        ],
        [
            InlineKeyboardButton("1", callback_data='dice_1'),
            InlineKeyboardButton("2", callback_data='dice_2'),
            InlineKeyboardButton("3", callback_data='dice_3'),
            InlineKeyboardButton("4", callback_data='dice_4'),
            InlineKeyboardButton("5", callback_data='dice_5'),
            InlineKeyboardButton("6", callback_data='dice_6')
        ],
        [InlineKeyboardButton("🔙 В меню", callback_data='back_to_menu')]
    ]

    message = context.bot.send_message(
        chat_id=user_id,
        text=f"🎲 Ваша ставка: {bet_amount:.2f} $\n\nВыберите тип ставки:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    active_dice_games[user_id]['message_id'] = message.message_id
    active_dice_games[user_id]['chat_id'] = message.chat_id

    return ConversationHandler.END


def dice_choice(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()

    user_id = query.from_user.id
    if user_id not in active_dice_games:
        query.edit_message_text(
            "Игра уже завершена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )
        return

    game = active_dice_games[user_id]
    user = get_user(user_id)

    # Бросаем кости
    dice_result = random.randint(1, 6)

    # Определяем тип ставки и множитель
    if query.data in ['dice_even', 'dice_odd']:
        bet_type = 1  # Чет/нечет
        if query.data == 'dice_even':
            player_choice = "чёт"
            win_condition = dice_result % 2 == 0
        else:
            player_choice = "нечёт"
            win_condition = dice_result % 2 == 1
        multiplier = DICE_MULTIPLIERS[1]
    else:
        bet_type = 2  # Конкретное число
        player_choice = query.data.split('_')[1]
        win_condition = int(player_choice) == dice_result
        multiplier = DICE_MULTIPLIERS[2]

    if win_condition:
        win_amount = game['bet'] * multiplier
        user.deposit(win_amount)
        user.add_win(win_amount)
        result_text = (
            f"🎉 Поздравляем! Вы выиграли!\n\n"
            f"Ваш выбор: {player_choice}\n"
            f"Результат: {dice_result}\n"
            f"Множитель: x{multiplier:.1f}\n"
            f"Выигрыш: {win_amount:.2f} $"
        )
    else:
        result_text = (
            f"💥 Вы проиграли!\n\n"
            f"Ваш выбор: {player_choice}\n"
            f"Результат: {dice_result}\n"
            f"Ставка: {game['bet']:.2f} $"
        )

    query.edit_message_text(
        text=result_text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
        ])
    )

    del active_dice_games[user_id]


# ==================== Админ-панель ====================

def admin_panel(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user = get_user(query.from_user.id)

    if not user.is_admin:
        query.answer("У вас нет прав доступа!")
        return

    query.answer()

    keyboard = [
        [InlineKeyboardButton("📊 Статистика", callback_data='admin_stats')],
        [InlineKeyboardButton("➕ Начислить баланс", callback_data='admin_add_balance')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
    ]

    query.edit_message_text(
        text="👑 *Админ-панель*",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


def admin_stats(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user = get_user(query.from_user.id)

    if not user.is_admin:
        query.answer("У вас нет прав доступа!")
        return

    query.answer()

    total_users = len(users_db)
    total_balance = sum(user.balance for user in users_db.values())
    total_bets = sum(user.total_bets for user in users_db.values())
    total_wins = sum(user.total_wins for user in users_db.values())

    text = (
        "📊 *Статистика казино*\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"💰 Общий баланс: {total_balance:.2f} $\n"
        f"🎰 Всего ставок: {total_bets:.2f} $\n"
        f"🏆 Всего выиграно: {total_wins:.2f} $\n"
        f"📊 Профит казино: {total_bets - total_wins:.2f} $"
    )

    query.edit_message_text(
        text=text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]
        ])
    )


def admin_add_balance(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    user = get_user(query.from_user.id)

    if not user.is_admin:
        query.answer("У вас нет прав доступа!")
        return ConversationHandler.END

    query.answer()
    query.edit_message_text(
        text="Введите username пользователя и сумму для начисления через пробел (например: @username 100):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]
        ])
    )

    return ADMIN_ADD_BALANCE


def admin_add_balance_handler(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    user = get_user(user_id)

    if not user.is_admin:
        update.message.reply_text("У вас нет прав доступа!")
        return ConversationHandler.END

    try:
        parts = update.message.text.split()
        if len(parts) != 2 or not parts[0].startswith('@'):
            raise ValueError

        username = parts[0][1:]  # Убираем @
        amount = float(parts[1])

        # Находим пользователя по username
        target_user = None
        for user in users_db.values():
            if user.username and user.username.lower() == username.lower():
                target_user = user
                break

        if not target_user:
            update.message.reply_text(
                f"Пользователь @{username} не найден.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]
                ])
            )
            return ADMIN_ADD_BALANCE

        # Начисляем баланс
        target_user.deposit(amount)

        update.message.reply_text(
            f"✅ Пользователю @{target_user.username} начислено {amount:.2f} $\n"
            f"💰 Новый баланс: {target_user.balance:.2f} $",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]
            ])
        )

        # Уведомляем пользователя
        context.bot.send_message(
            chat_id=target_user.user_id,
            text=f"🎁 Администратор начислил вам {amount:.2f} $\n"
                 f"💰 Ваш баланс: {target_user.balance:.2f} $"
        )

    except ValueError:
        update.message.reply_text(
            "Неверный формат. Введите username и сумму через пробел (например: @username 100)",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]
            ])
        )
        return ADMIN_ADD_BALANCE

    return ConversationHandler.END


def admin_add_balance_command(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    user = get_user(user_id)

    if not user.is_admin:
        update.message.reply_text("У вас нет прав доступа!")
        return ConversationHandler.END

    update.message.reply_text(
        "Введите username пользователя и сумму для начисления через пробел (например: @username 100):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]
        ])
    )

    return ADMIN_ADD_BALANCE


# ==================== Вспомогательные функции ====================

def cancel(update: Update, context: CallbackContext) -> int:
    update.message.reply_text(
        'Действие отменено.',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
        ])
    )
    return ConversationHandler.END


def error_handler(update: Update, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    if update.effective_user:
        context.bot.send_message(
            chat_id=update.effective_user.id,
            text="Произошла ошибка. Пожалуйста, попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )


# ==================== Основная функция ====================

def main() -> None:
    updater = Updater(TOKEN)
    dispatcher = updater.dispatcher

    # Обработчики команд
    dispatcher.add_handler(CommandHandler('start', start))
    dispatcher.add_handler(CommandHandler('addbalance', admin_add_balance_command))
    dispatcher.add_handler(CommandHandler('help', help_command))

    # Обработчики callback-запросов
    dispatcher.add_handler(CallbackQueryHandler(start, pattern='^back_to_menu$'))
    dispatcher.add_handler(CallbackQueryHandler(play_game, pattern='^play_game$'))
    dispatcher.add_handler(CallbackQueryHandler(help_command, pattern='^help$'))
    dispatcher.add_handler(CallbackQueryHandler(profile_command, pattern='^profile$'))
    dispatcher.add_handler(CallbackQueryHandler(admin_panel, pattern='^admin_panel$'))
    dispatcher.add_handler(CallbackQueryHandler(admin_stats, pattern='^admin_stats$'))
    dispatcher.add_handler(CallbackQueryHandler(rocket_cashout, pattern='^rocket_cashout$'))
    dispatcher.add_handler(CallbackQueryHandler(matrix_choice, pattern='^matrix_'))
    dispatcher.add_handler(CallbackQueryHandler(dice_choice, pattern='^dice_'))

    # ConversationHandler для пополнения баланса
    deposit_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(deposit, pattern='^deposit$')],
        states={
            DEPOSIT_AMOUNT: [MessageHandler(Filters.text & ~Filters.command, deposit_amount)],
        },
        fallbacks=[CallbackQueryHandler(profile_command, pattern='^profile$'), CommandHandler('cancel', cancel)],
    )
    dispatcher.add_handler(deposit_conv_handler)

    # ConversationHandler для игры в ракетку
    rocket_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(game_choice, pattern='^game_rocket$')],
        states={
            ROCKET_BET: [MessageHandler(Filters.text & ~Filters.command, rocket_bet)],
        },
        fallbacks=[CommandHandler('cancel', cancel), CallbackQueryHandler(play_game, pattern='^play_game$')],
    )
    dispatcher.add_handler(rocket_conv_handler)

    # ConversationHandler для игры в матрицу
    matrix_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(game_choice, pattern='^game_matrix$')],
        states={
            MATRIX_BET: [MessageHandler(Filters.text & ~Filters.command, matrix_bet)],
        },
        fallbacks=[CommandHandler('cancel', cancel), CallbackQueryHandler(play_game, pattern='^play_game$')],
    )
    dispatcher.add_handler(matrix_conv_handler)

    # ConversationHandler для игры в кости
    dice_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(game_choice, pattern='^game_dice$')],
        states={
            DICE_BET: [MessageHandler(Filters.text & ~Filters.command, dice_bet)],
        },
        fallbacks=[CommandHandler('cancel', cancel), CallbackQueryHandler(play_game, pattern='^play_game$')],
    )
    dispatcher.add_handler(dice_conv_handler)

    # ConversationHandler для админской команды добавления баланса
    admin_add_balance_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_add_balance, pattern='^admin_add_balance$'),
            CommandHandler('addbalance', admin_add_balance_command)
        ],
        states={
            ADMIN_ADD_BALANCE: [MessageHandler(Filters.text & ~Filters.command, admin_add_balance_handler)],
        },
        fallbacks=[CommandHandler('cancel', cancel), CallbackQueryHandler(admin_panel, pattern='^admin_panel$')],
    )
    dispatcher.add_handler(admin_add_balance_conv_handler)

    # Добавляем периодическую проверку инвойсов
    job_queue = updater.job_queue
    job_queue.run_repeating(check_invoices, interval=10.0, first=0.0)

    # Обработчик ошибок
    dispatcher.add_error_handler(error_handler)

    # Запуск бота
    updater.start_polling()
    updater.idle()


if __name__ == '__main__':
    main()