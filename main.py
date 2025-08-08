import os
import random
import time
from datetime import datetime, timedelta
import logging
from typing import Optional
import requests
import json
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
from telegram.error import BadRequest, Unauthorized
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройки из .env
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CRYPTO_BOT_API_KEY = os.getenv('CRYPTOBOT_API_TOKEN')
ADMIN_IDS = [int(id.strip()) for id in os.getenv('ADMIN_IDS', '').split(',') if id.strip().isdigit()]
MIN_BET = float(os.getenv('MIN_BET', 0.1))
MAX_BET = int(os.getenv('MAX_BET', 1000))
SUPPORT_USERNAME = os.getenv('SUPPORT_USERNAME', '@CasaSupport')

# Проверка токена и других переменных
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не указан в .env файле")
if not CRYPTO_BOT_API_KEY:
    raise ValueError("CRYPTOBOT_API_TOKEN не указан в .env файле")
if not ADMIN_IDS:
    raise ValueError("ADMIN_IDS не указан или некорректен в .env файле")

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Состояния для ConversationHandler
DEPOSIT_AMOUNT, GAME_CHOICE, ROCKET_BET, MATRIX_BET, DICE_BET, ADMIN_ADD_BALANCE, ADMIN_ADD_VIRTUAL = range(7)

# Данные пользователей
users_db = {}
active_rocket_games = {}
active_matrix_games = {}
active_dice_games = {}
active_invoices = {}

# Настройки для ракетки
ROCKET_CRASH_PROBABILITIES = [
    (1.1, 0.10),  # 10% шанс краша на x1.0–x1.1
    (1.5, 0.30),  # 20% шанс краша на x1.1–x1.5
    (3.0, 0.60),  # 30% шанс краша на x1.5–x3.0
    (5.0, 0.90),  # 30% шанс краша на x3.0–x5.0
    (25.0, 1.00)  # 10% шанс краша на x5.0–x25.0
]

# Множители для Матрицы
MATRIX_MULTIPLIERS = [1.0] + [1.2 ** i for i in range(1, 10)]  # Level 1 starts at 1.0, then 1.2, 1.44, etc.
DICE_MULTIPLIERS = {
    1: 2.0,
    2: 6.0,
}

# Ограничение на начисление виртуального баланса
DAILY_VIRTUAL_LIMIT = 100.0
VIRTUAL_DEPOSIT_RESET = timedelta(days=1)

# Работа с пользователями
USER_FILE = "users.json"

def check_file_permissions(file_path: str) -> bool:
    """Проверка прав доступа к файлу"""
    try:
        if not os.path.exists(file_path):
            with open(file_path, "w") as f:
                json.dump({}, f)
        if not os.access(file_path, os.R_OK | os.W_OK):
            logger.error(f"Нет прав на чтение/запись для файла {file_path}")
            return False
        return True
    except Exception as e:
        logger.error(f"Ошибка проверки прав файла {file_path}: {str(e)}")
        return False

def load_users() -> dict:
    """Загрузка данных пользователей из файла"""
    try:
        if not check_file_permissions(USER_FILE):
            raise PermissionError(f"Недостаточно прав для работы с файлом {USER_FILE}")
        with open(USER_FILE, "r", encoding='utf-8') as f:
            data = json.load(f)
            for user_id, user_data in data.items():
                user_data['balance'] = float(user_data.get('balance', 0.0))
                user_data['virtual_balance'] = float(user_data.get('virtual_balance', 0.0))
                user_data['daily_virtual_deposited'] = float(user_data.get('daily_virtual_deposited', 0.0))
                user_data['last_virtual_deposit_time'] = user_data.get('last_virtual_deposit_time', None)
            logger.info(f"Успешно загружено {len(data)} пользователей из {USER_FILE}")
            return data
    except (json.JSONDecodeError, FileNotFoundError, PermissionError) as e:
        logger.warning(f"Не удалось загрузить пользователей: {str(e)}. Создается новый файл.")
        with open(USER_FILE, "w", encoding='utf-8') as f:
            json.dump({}, f)
        return {}
    except Exception as e:
        logger.error(f"Неожиданная ошибка при загрузке пользователей: {str(e)}")
        return {}

def save_users(users: dict) -> None:
    try:
        if not check_file_permissions(USER_FILE):
            raise PermissionError(f"Недостаточно прав для записи в файл {USER_FILE}")
        users_copy = {}
        for user_id, user_data in users.items():
            users_copy[user_id] = user_data.copy()
            users_copy[user_id]['balance'] = float(user_data.get('balance', 0.0))
            users_copy[user_id]['virtual_balance'] = float(user_data.get('virtual_balance', 0.0))
            users_copy[user_id]['daily_virtual_deposited'] = float(user_data.get('daily_virtual_deposited', 0.0))
            users_copy[user_id]['username'] = user_data.get('username', '').lower()
        with open(USER_FILE, "w", encoding='utf-8') as f:
            json.dump(users_copy, f, indent=4, ensure_ascii=False)
        logger.info(f"Успешно сохранено {len(users_copy)} пользователей в {USER_FILE}")
    except Exception as e:
        logger.error(f"Не удалось сохранить пользователей: {str(e)}", exc_info=True)
        raise

def get_balance(user_id: int, balance_type: str = 'balance') -> float:
    """Получение баланса пользователя"""
    users = load_users()
    balance = float(users.get(str(user_id), {}).get(balance_type, 0.0))
    logger.info(f"Получен {balance_type} для пользователя {user_id}: {balance}")
    return balance

def update_balance(user_id: int, amount: float, balance_type: str = 'balance') -> None:
    """Обновление баланса пользователя"""
    users = load_users()
    user = users.setdefault(str(user_id), {
        "balance": 0.0,
        "virtual_balance": 0.0,
        "username": "",
        "use_virtual": False,
        "daily_virtual_deposited": 0.0,
        "last_virtual_deposit_time": None
    })
    user[balance_type] = float(user.get(balance_type, 0.0)) + float(amount)
    save_users(users)
    logger.info(f"Обновлен {balance_type} для пользователя {user_id} на {amount}. Новый {balance_type}: {user[balance_type]}")

class User:
    def __init__(self, user_id: int, username: str = None):
        self.user_id = user_id
        self.username = username
        self.balance = float(get_balance(user_id, 'balance'))
        self.virtual_balance = float(get_balance(user_id, 'virtual_balance'))
        self.use_virtual = load_users().get(str(user_id), {}).get('use_virtual', False)
        self.daily_virtual_deposited = float(load_users().get(str(user_id), {}).get('daily_virtual_deposited', 0.0))
        self.last_virtual_deposit_time = load_users().get(str(user_id), {}).get('last_virtual_deposit_time', None)
        self.total_bets = 0.0
        self.total_wins = 0.0
        self.games_played = 0
        self.last_active = datetime.now()
        self.deposit_history = []
        self.withdraw_history = []
        self.is_admin = user_id in ADMIN_IDS

    def deposit(self, amount: float, balance_type: str = 'balance') -> None:
        """Начисление средств на баланс"""
        logger.info(f"Начало начисления {amount} на {balance_type} для пользователя {self.user_id} (@{self.username})")
        try:
            users = load_users()
            user_data = users.setdefault(str(self.user_id), {
                "balance": 0.0,
                "virtual_balance": 0.0,
                "username": self.username or "",
                "use_virtual": self.use_virtual,
                "daily_virtual_deposited": 0.0,
                "last_virtual_deposit_time": None
            })
            if balance_type == 'virtual_balance':
                now = datetime.now()
                if self.last_virtual_deposit_time:
                    last_deposit = datetime.fromisoformat(self.last_virtual_deposit_time)
                    if now - last_deposit >= VIRTUAL_DEPOSIT_RESET:
                        self.daily_virtual_deposited = 0.0
                        user_data['daily_virtual_deposited'] = 0.0
                        user_data['last_virtual_deposit_time'] = now.isoformat()
                else:
                    user_data['last_virtual_deposit_time'] = now.isoformat()
                if self.daily_virtual_deposited + amount > DAILY_VIRTUAL_LIMIT:
                    time_passed = now - datetime.fromisoformat(
                        self.last_virtual_deposit_time) if self.last_virtual_deposit_time else timedelta(0)
                    time_left = VIRTUAL_DEPOSIT_RESET - time_passed
                    hours, remainder = divmod(int(time_left.total_seconds()), 3600)
                    minutes = remainder // 60
                    logger.error(
                        f"Превышен суточный лимит для пользователя {self.user_id}: {self.daily_virtual_deposited + amount} > {DAILY_VIRTUAL_LIMIT}")
                    raise ValueError(
                        f"Превышен суточный лимит начисления виртуального баланса ({DAILY_VIRTUAL_LIMIT}$). "
                        f"Попробуйте через {hours}ч {minutes}мин.")
                self.daily_virtual_deposited += amount
                user_data['daily_virtual_deposited'] = self.daily_virtual_deposited
                self.virtual_balance += amount
                user_data['virtual_balance'] = self.virtual_balance
            else:
                self.balance += amount
                user_data['balance'] = self.balance
            save_users(users)
            self.deposit_history.append((datetime.now(), amount, balance_type))
            logger.info(f"Начислено {amount} на {balance_type} для пользователя {self.user_id} (@{self.username})")
            users_db[self.user_id] = self
        except Exception as e:
            logger.error(f"Ошибка начисления {amount} на {balance_type} для пользователя {self.user_id}: {str(e)}",
                         exc_info=True)
            raise

    def withdraw(self, amount: float, balance_type: str = 'balance') -> bool:
        """Снятие средств с баланса"""
        target_balance = self.virtual_balance if balance_type == 'virtual_balance' else self.balance
        if target_balance >= amount:
            if balance_type == 'balance':
                self.balance -= amount
            else:
                self.virtual_balance -= amount
            update_balance(self.user_id, -amount, balance_type)
            self.withdraw_history.append((datetime.now(), amount, balance_type))
            users_db[self.user_id] = self
            return True
        return False

    def add_win(self, amount: float) -> None:
        """Добавление выигрыша"""
        self.total_wins += amount
        users_db[self.user_id] = self

    def add_bet(self, amount: float) -> None:
        """Добавление ставки"""
        self.total_bets += amount
        self.games_played += 1
        users_db[self.user_id] = self

    def toggle_balance_type(self) -> None:
        """Переключение типа баланса"""
        self.use_virtual = not self.use_virtual
        users = load_users()
        users[str(self.user_id)]['use_virtual'] = self.use_virtual
        save_users(users)
        users_db[self.user_id] = self

    def get_profile(self) -> str:
        """Получение профиля пользователя"""
        return (
            f"┌ Имя: @{self.username if self.username else 'не указано'}\n"
            f"├ Баланс: {self.balance:.2f} $\n"
            f"├ Виртуальный баланс: {self.virtual_balance:.2f} 💎\n"
            f"└ Всего выиграно: {self.total_wins:.2f} $"
        )

    def get_stats(self) -> str:
        """Получение статистики пользователя"""
        return (
            f"👤 ID: {self.user_id}\n"
            f"📛 Username: @{self.username if self.username else 'нет'}\n"
            f"💰 Баланс: {self.balance:.2f} $\n"
            f"💎 Виртуальный баланс: {self.virtual_balance:.2f} $\n"
            f"🎰 Игр сыграно: {self.games_played}\n"
            f"🏆 Всего выиграно: {self.total_wins:.2f} $\n"
            f"💸 Всего поставлено: {self.total_bets:.2f} $\n"
            f"📊 Профит: {self.total_wins - self.total_bets:.2f} $"
        )

def get_user(user_id: int, username: str = None) -> User:
    """Получение объекта пользователя"""
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str not in users:
        users[user_id_str] = {
            "balance": 0.0,
            "virtual_balance": 0.0,
            "username": username or "",
            "use_virtual": False,
            "daily_virtual_deposited": 0.0,
            "last_virtual_deposit_time": None
        }
        save_users(users)
    user_data = users[user_id_str]
    user = User(user_id, user_data.get('username', username))
    user.balance = float(user_data.get('balance', 0.0))
    user.virtual_balance = float(user_data.get('virtual_balance', 0.0))
    user.use_virtual = user_data.get('use_virtual', False)
    user.daily_virtual_deposited = float(user_data.get('daily_virtual_deposited', 0.0))
    user.last_virtual_deposit_time = user_data.get('last_virtual_deposit_time', None)
    if username and not user.username:
        user.username = username
        users[user_id_str]['username'] = username
        save_users(users)
    users_db[user_id] = user
    logger.info(f"Получен пользователь {user_id} (@{user.username})")
    return user

def is_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь администратором"""
    return user_id in ADMIN_IDS

def create_crypto_invoice(user_id: int, amount: float) -> Optional[str]:
    """Создание инвойса для пополнения баланса"""
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
            logger.info(f"Создан инвойс для пользователя {user_id} на сумму {amount}")
            return invoice["pay_url"]
        return None
    except Exception as e:
        logger.error(f"Ошибка создания инвойса CryptoBot для пользователя {user_id}: {str(e)}")
        return None

def check_invoices(context: CallbackContext) -> None:
    """Проверка статуса инвойсов"""
    if not active_invoices:
        return
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_API_KEY}
    try:
        response = requests.get("https://pay.crypt.bot/api/getInvoices", headers=headers)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            logger.error(f"Неожиданный ответ от CryptoBot API: {data}")
            return
        invoices = data.get("result", {}).get("items", [])
        if not isinstance(invoices, list):
            logger.error(f"Неожиданный тип инвойсов: {type(invoices)}, содержимое: {invoices}")
            return
        for invoice in invoices:
            if not isinstance(invoice, dict):
                logger.error(f"Неожиданный тип инвойса: {type(invoice)}, содержимое: {invoice}")
                continue
            if invoice.get("status") == "paid":
                inv_id = invoice.get("invoice_id")
                if inv_id in active_invoices and not active_invoices[inv_id]["paid"]:
                    user_id = active_invoices[inv_id]["user_id"]
                    amount = active_invoices[inv_id]["amount"]
                    user = get_user(user_id)
                    user.deposit(amount, 'balance')
                    active_invoices[inv_id]["paid"] = True
                    try:
                        context.bot.send_message(
                            chat_id=user_id,
                            text=f"✅ Оплата на {amount}$ получена. Баланс пополнен."
                        )
                    except Exception as e:
                        logger.error(f"Ошибка отправки сообщения о пополнении пользователю {user_id}: {e}")
    except Exception as e:
        logger.error(f"Ошибка при запросе инвойсов: {str(e)}")

def safe_send_message(context: CallbackContext, chat_id: int, text: str, reply_markup=None, parse_mode=None) -> Optional[dict]:
    retries = 3
    for attempt in range(retries):
        try:
            message = context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
            logger.info(f"Сообщение отправлено в чат {chat_id}: {text[:50]}...")
            return {'message_id': message.message_id, 'chat_id': message.chat_id}
        except Unauthorized:
            logger.error(f"Пользователь {chat_id} заблокировал бота или не начал диалог")
            return None
        except BadRequest as e:
            if "retry after" in str(e).lower():
                retry_after = int(str(e).split("retry after")[-1].strip()) + 1
                logger.warning(f"Rate limit exceeded, waiting {retry_after} seconds (attempt {attempt + 1}/{retries})")
                time.sleep(retry_after)
            else:
                logger.error(f"Ошибка BadRequest при отправке сообщения в чат {chat_id}: {str(e)}")
                return None
        except Exception as e:
            logger.error(f"Неожиданная ошибка при отправке сообщения в чат {chat_id}: {str(e)}")
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return None
    logger.error(f"Не удалось отправить сообщение в чат {chat_id} после {retries} попыток")
    return None

def safe_answer_query(query, text: str = None) -> bool:
    """Безопасная обработка callback-запроса"""
    try:
        query.answer(text=text)
        return True
    except BadRequest as e:
        if "Query is too old" in str(e) or "query id is invalid" in str(e):
            logger.warning(f"Старая или недействительная callback-запрос: {str(e)}")
            return False
        logger.error(f"Ошибка обработки callback-запроса: {str(e)}")
        return False

def safe_edit_message(query, text: str, reply_markup=None, parse_mode=None) -> bool:
    try:
        query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
        logger.info(f"Сообщение отредактировано: {text[:50]}...")
        return True
    except BadRequest as e:
        if "Message is not modified" in str(e):
            logger.debug("Сообщение не изменено, пропуск")
            return True
        if "Message to edit not found" in str(e):
            logger.warning(f"Сообщение для редактирования не найдено, отправка нового: {text[:50]}...")
            try:
                query.message.reply_text(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode
                )
                return True
            except Exception as e:
                logger.error(f"Ошибка отправки нового сообщения: {str(e)}")
                return False
        logger.error(f"Ошибка редактирования сообщения: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"Неожиданная ошибка при редактировании сообщения: {str(e)}")
        return False

# ==================== Основные команды ====================

def start(update: Update, context: CallbackContext) -> None:
    """Команда /start"""
    user = get_user(update.effective_user.id, update.effective_user.username)
    current_state = context.user_data.get('__current_conversation_state', None)
    logger.info(
        f"Вызов команды /start пользователем {user.user_id} (@{user.username}), текущее состояние: {current_state}")

    if current_state == 'ADMIN_ADD_BALANCE':
        logger.info(f"Пользователь {user.user_id} (@{user.username}) вызвал /start в состоянии ADMIN_ADD_BALANCE")
        safe_send_message(
            context,
            update.effective_chat.id,
            "Вы находитесь в процессе начисления баланса. Введите username и сумму (например: @username 100) или используйте /cancel для отмены.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')],
                [InlineKeyboardButton("❌ Отменить", callback_data='cancel_conversation')]
            ])
        )
        return

    balance_type = "💎" if user.use_virtual else "💰"
    text = (
        f"🎰 Добро пожаловать в *Casa Casino*!\n\n"
        f"💰 Ваш баланс: *{user.balance:.2f} $*\n"
        f"💎 Ваш виртуальный баланс: *{user.virtual_balance:.2f} $*\n"
        f"📌 Текущий режим: {balance_type}"
    )
    keyboard = [
        [InlineKeyboardButton("🎮 Играть", callback_data='play_game')],
        [
            InlineKeyboardButton("💳 Пополнить", callback_data='deposit'),
            InlineKeyboardButton("🔄 Сменить режим", callback_data='change_balance')
        ],
        [
            InlineKeyboardButton("❓ Помощь", callback_data='help'),
            InlineKeyboardButton("📊 Профиль", callback_data='profile')
        ]
    ]
    if user.is_admin:
        keyboard.append([InlineKeyboardButton("👑 Админ-панель", callback_data='admin_panel')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        safe_send_message(context, update.effective_chat.id, text, reply_markup, 'Markdown')
    else:
        safe_edit_message(update.callback_query, text, reply_markup, 'Markdown')

def base_command(update: Update, context: CallbackContext) -> None:
    """Команда /base для вывода списка пользователей и их балансов"""
    user = get_user(update.effective_user.id, update.effective_user.username)
    if not user.is_admin:
        safe_send_message(
            context,
            update.effective_chat.id,
            "❌ У вас нет прав доступа!",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]])
        )
        return
    users = load_users()
    if not users:
        safe_send_message(
            context,
            update.effective_chat.id,
            "📋 База пользователей пуста.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]])
        )
        return
    user_list = []
    for user_id, user_data in users.items():
        username = user_data.get('username', 'не указано')
        balance = float(user_data.get('balance', 0.0))
        virtual_balance = float(user_data.get('virtual_balance', 0.0))
        user_list.append(
            f"👤 @{username if username else 'не указано'} (ID: {user_id})\n"
            f"💰 Реальный баланс: {balance:.2f} $\n"
            f"💎 Виртуальный баланс: {virtual_balance:.2f} $\n"
        )
    text = "📋 *Список пользователей:*\n\n" + "\n".join(user_list)
    safe_send_message(
        context,
        update.effective_chat.id,
        text,
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]]),
        parse_mode='Markdown'
    )

def deposit(update: Update, context: CallbackContext) -> int:
    """Вход в режим пополнения баланса"""
    query = update.callback_query
    if not safe_answer_query(query):
        return ConversationHandler.END
    safe_edit_message(
        query,
        text="💳 Введите сумму для пополнения реального баланса (минимум 1 $):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data='profile')]
        ])
    )
    return DEPOSIT_AMOUNT

def deposit_amount(update: Update, context: CallbackContext) -> int:
    """Обработка суммы пополнения"""
    try:
        amount = float(update.effective_message.text)
        if amount < 1:
            safe_send_message(
                context,
                update.effective_chat.id,
                "Минимальная сумма пополнения - 1 $",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='profile')]])
            )
            return DEPOSIT_AMOUNT
    except ValueError:
        safe_send_message(
            context,
            update.effective_chat.id,
            "Пожалуйста, введите корректную сумму (число).",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='profile')]])
        )
        return DEPOSIT_AMOUNT
    user = get_user(update.effective_user.id)
    payment_url = create_crypto_invoice(user.user_id, amount)
    if not payment_url:
        safe_send_message(
            context,
            update.effective_chat.id,
            "Ошибка при создании платежа. Пожалуйста, попробуйте позже.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='profile')]])
        )
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton("💳 Перейти к оплате", url=payment_url)],
        [InlineKeyboardButton("🔙 В профиль", callback_data='profile')]
    ]
    safe_send_message(
        context,
        update.effective_chat.id,
        f"✅ Для пополнения реального баланса на *{amount:.2f} $* перейдите по ссылке ниже:\n\n"
        f"После оплаты средства будут зачислены автоматически.",
        InlineKeyboardMarkup(keyboard),
        'Markdown'
    )
    return ConversationHandler.END

def change_balance(update: Update, context: CallbackContext) -> None:
    """Смена типа баланса"""
    query = update.callback_query
    if not safe_answer_query(query):
        return
    user = get_user(query.from_user.id)
    user.toggle_balance_type()
    balance_type = "виртуальный" if user.use_virtual else "реальный"
    safe_edit_message(
        query,
        text=f"✅ Баланс изменен на {balance_type}!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
        ])
    )

def add_virtual_balance(update: Update, context: CallbackContext) -> None:
    """Команда /add для начисления виртуального баланса"""
    user = get_user(update.effective_user.id, update.effective_user.username)
    logger.info(f"Пользователь {user.user_id} (@{user.username}) вызвал /add с аргументами: {context.args}")
    if not context.args:
        safe_send_message(
            context,
            update.effective_chat.id,
            "❌ Пожалуйста, укажите сумму для начисления (например: /add 5)",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data='back_to_menu')]])
        )
        return
    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError("Сумма должна быть положительной")
        user.deposit(amount, 'virtual_balance')
        safe_send_message(
            context,
            update.effective_chat.id,
            f"✅ На ваш виртуальный баланс начислено {amount:.2f} $\n"
            f"💎 Новый виртуальный баланс: {user.virtual_balance:.2f} $",
            InlineKeyboardMarkup([[InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]])
        )
    except ValueError as e:
        logger.error(f"Неверный ввод для /add пользователем {user.user_id}: {str(e)}")
        safe_send_message(
            context,
            update.effective_chat.id,
            f"❌ Ошибка: {str(e)}",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data='back_to_menu')]])
        )
    except Exception as e:
        logger.error(f"Неожиданная ошибка в add_virtual_balance для пользователя {user.user_id}: {str(e)}")
        safe_send_message(
            context,
            update.effective_chat.id,
            "❌ Произошла ошибка. Пожалуйста, попробуйте позже.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data='back_to_menu')]])
        )

def play_game(update: Update, context: CallbackContext) -> None:
    """Выбор игры"""
    query = update.callback_query
    if not safe_answer_query(query):
        return
    keyboard = [
        [InlineKeyboardButton("🚀 Ракетка", callback_data='game_rocket')],
        [InlineKeyboardButton("🔢 Матрица", callback_data='game_matrix')],
        [InlineKeyboardButton("🎲 Кости", callback_data='game_dice')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
    ]
    safe_edit_message(
        query,
        text="🎮 Выберите игру:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def help_command(update: Update, context: CallbackContext) -> None:
    """Команда /help"""
    query = update.callback_query
    if not safe_answer_query(query):
        return
    text = (
        f"🆘 *Помощь*\n\n"
        f"Если у вас возникли вопросы или проблемы, обратитесь к нашему менеджеру: {SUPPORT_USERNAME}\n\n"
        f"Техническая поддержка работает 24/7 и ответит вам в течение 15 минут."
    )
    safe_edit_message(
        query,
        text=text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
        ])
    )

def profile_command(update: Update, context: CallbackContext) -> None:
    """Команда /profile"""
    query = update.callback_query
    if not safe_answer_query(query):
        return
    user = get_user(query.from_user.id)
    text = f"📊 *Профиль*\n\n{user.get_profile()}"
    keyboard = [
        [InlineKeyboardButton("💳 Пополнить баланс", callback_data='deposit')],
        [InlineKeyboardButton("🔄 Сменить баланс", callback_data='change_balance')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
    ]
    safe_edit_message(
        query,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
def game_choice(update: Update, context: CallbackContext) -> int:
    """Выбор типа игры"""
    query = update.callback_query
    if not safe_answer_query(query):
        return ConversationHandler.END
    user = get_user(query.from_user.id)
    balance_type = "виртуальный" if user.use_virtual else "реальный"
    game_type = query.data.split('_')[1]
    context.user_data['initiator_id'] = query.from_user.id
    context.user_data['chat_id'] = query.message.chat_id
    if game_type == 'rocket':
        safe_edit_message(
            query,
            text=f"🚀 *Игра Ракетка* (@{user.username})\n\n"
                 f"Текущий режим: {balance_type}\n\n"
                 f"Правила:\n"
                 f"1. Сделайте ставку\n"
                 f"2. Ракетка взлетает, множитель растет\n"
                 f"3. Нажмите 'Забрать' до взрыва ракетки\n"
                 f"4. Если успеете - получаете ставку × множитель!\n\n"
                 f"Введите сумму ставки (от {MIN_BET} $):",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return ROCKET_BET
    elif game_type == 'matrix':
        safe_edit_message(
            query,
            text=f"🔢 *Игра Матрица* (@{user.username})\n\n"
                 f"Текущий режим: {balance_type}\n\n"
                 f"Правила:\n"
                 f"1. Сделайте ставку\n"
                 f"2. В каждой строке 5 клеток (4 выигрышные, 1 бомба)\n"
                 f"3. Выбирайте клетки, пока не попадете на бомбу\n"
                 f"4. Чем дальше пройдете, тем выше множитель!\n\n"
                 f"Введите сумму ставки (от {MIN_BET} $):",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return MATRIX_BET
    elif game_type == 'dice':
        safe_edit_message(
            query,
            text=f"🎲 *Игра в Кости* (@{user.username})\n\n"
                 f"Текущий режим: {balance_type}\n\n"
                 f"Правила:\n"
                 f"1. Сделайте ставку\n"
                 f"2. Выберите тип ставки:\n"
                 f"   - Чет/Нечет (x2.0)\n"
                 f"   - Конкретное число (x6.0)\n"
                 f"3. Бот бросает кости\n"
                 f"4. Если угадали - получаете выигрыш!\n\n"
                 f"Введите сумму ставки (от {MIN_BET} $):",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data='play_game')]
            ])
        )
        return DICE_BET
    return ConversationHandler.END

def rocket_bet(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    initiator_id = context.user_data.get('initiator_id')
    chat_id = context.user_data.get('chat_id', update.effective_chat.id)
    if user_id != initiator_id:
        safe_send_message(
            context,
            chat_id,
            "Только пользователь, начавший игру, может ввести ставку.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='play_game')]])
        )
        return ROCKET_BET
    if user_id in active_rocket_games:
        logger.warning(f"Пользователь {user_id} уже имеет активную игру Ракетка, завершаем старую")
        del active_rocket_games[user_id]
    user = get_user(user_id)
    balance_type = 'virtual_balance' if user.use_virtual else 'balance'
    try:
        bet_amount = float(update.effective_message.text)
    except ValueError:
        safe_send_message(
            context,
            chat_id,
            "Пожалуйста, введите корректную сумму ставки (число).",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='play_game')]])
        )
        return ROCKET_BET
    if bet_amount < MIN_BET:
        safe_send_message(
            context,
            chat_id,
            f"Минимальная ставка: {MIN_BET} $",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='play_game')]])
        )
        return ROCKET_BET
    if bet_amount > MAX_BET:
        safe_send_message(
            context,
            chat_id,
            f"Максимальная ставка: {MAX_BET} $",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='play_game')]])
        )
        return ROCKET_BET
    if bet_amount > (user.virtual_balance if user.use_virtual else user.balance):
        safe_send_message(
            context,
            chat_id,
            f"Недостаточно средств на {'виртуальном' if user.use_virtual else 'реальном'} балансе.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='play_game')]])
        )
        return ROCKET_BET
    user.withdraw(bet_amount, balance_type)
    user.add_bet(bet_amount)
    rand = random.random()
    crash_at = 1.0
    for threshold, prob in ROCKET_CRASH_PROBABILITIES:
        if rand <= prob:
            if ROCKET_CRASH_PROBABILITIES.index((threshold, prob)) == 0:
                # Для первого порога задаем диапазон, например, от 1.0 до threshold (1.1)
                crash_at = 1.0 + (threshold - 1.0) * (rand / prob)
            else:
                prev_threshold, prev_prob = ROCKET_CRASH_PROBABILITIES[ROCKET_CRASH_PROBABILITIES.index((threshold, prob)) - 1]
                segment_prob = (rand - prev_prob) / (prob - prev_prob)
                crash_at = prev_threshold + (threshold - prev_threshold) * segment_prob
            break
    # Убедимся, что crash_at не меньше 1.01, чтобы избежать мгновенных крашей
    crash_at = max(crash_at, 1.01)
    active_rocket_games[user_id] = {
        'bet': bet_amount,
        'multiplier': 1.0,
        'crashed': False,
        'crash_at': crash_at,
        'message_id': None,
        'chat_id': chat_id,
        'balance_type': balance_type,
        'initiator_id': initiator_id
    }
    logger.info(f"Инициализирована игра Ракетка для пользователя {user_id}: ставка={bet_amount}, crash_at={crash_at}")
    run_rocket_game(context, user_id)
    context.user_data['__current_conversation_state'] = None
    return ConversationHandler.END

def run_rocket_game(context: CallbackContext, user_id: int) -> None:
    """Запуск игры Ракетка"""
    game = active_rocket_games.get(user_id)
    if not game:
        logger.error(f"Игра для пользователя {user_id} не найдена")
        return
    user = get_user(user_id)
    start_time = time.time()
    crash_time = game['crash_at'] * 3  # Time to reach crash multiplier

    # Send initial message
    result = safe_send_message(
        context,
        game['chat_id'],
        f"🚀 Ракетка взлетает! (@{user.username})\n\nСтавка: {game['bet']:.2f} $\nМножитель: x1.00",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Забрать", callback_data=f'rocket_cashout_{user_id}')],
            [InlineKeyboardButton("🔙 В меню", callback_data='back_to_menu')]
        ])
    )
    if not result:
        logger.error(f"Не удалось отправить начальное сообщение для игры Ракетка пользователю {user_id}")
        del active_rocket_games[user_id]
        safe_send_message(
            context,
            game['chat_id'],
            "❌ Произошла ошибка при запуске игры. Пожалуйста, попробуйте позже.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]])
        )
        return
    game['message_id'] = result['message_id']
    game['chat_id'] = result['chat_id']

    def update_multiplier(context: CallbackContext):
        """Обновление множителя и сообщения"""
        if user_id not in active_rocket_games:
            logger.info(f"Игра для пользователя {user_id} завершена или удалена")
            return
        game = active_rocket_games[user_id]
        elapsed = time.time() - start_time
        current_multiplier = 1.0 + (game['crash_at'] - 1.0) * (elapsed / crash_time)
        game['multiplier'] = current_multiplier

        if current_multiplier >= game['crash_at'] or game['crashed']:
            if not game['crashed']:
                game['crashed'] = True
                text = (
                    f"💥 Ракетка взорвалась на x{game['multiplier']:.2f}! (@{user.username})\n\n"
                    f"Ставка: {game['bet']:.2f} $\nВы проиграли."
                )
                reply_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
                ])
                try:
                    context.bot.edit_message_text(
                        chat_id=game['chat_id'],
                        message_id=game['message_id'],
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode='Markdown'
                    )
                except BadRequest as e:
                    logger.warning(f"Не удалось отредактировать сообщение: {str(e)}")
                    safe_send_message(context, game['chat_id'], text, reply_markup, 'Markdown')
                finally:
                    if user_id in active_rocket_games:
                        del active_rocket_games[user_id]
            return

        # Update message with current multiplier
        text = (
            f"🚀 Ракетка летит! (@{user.username})\n\n"
            f"Ставка: {game['bet']:.2f} $\nМножитель: x{game['multiplier']:.2f}"
        )
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Забрать", callback_data=f'rocket_cashout_{user_id}')],
            [InlineKeyboardButton("🔙 В меню", callback_data='back_to_menu')]
        ])
        try:
            context.bot.edit_message_text(
                chat_id=game['chat_id'],
                message_id=game['message_id'],
                text=text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        except BadRequest as e:
            if "Message is not modified" in str(e):
                logger.debug(f"Сообщение не изменено для пользователя {user_id}")
            elif "Message to edit not found" in str(e):
                logger.warning(f"Сообщение не найдено, отправка нового для пользователя {user_id}")
                result = safe_send_message(context, game['chat_id'], text, reply_markup, 'Markdown')
                if result:
                    game['message_id'] = result['message_id']
                    game['chat_id'] = result['chat_id']
                else:
                    logger.error(f"Не удалось отправить новое сообщение для пользователя {user_id}")
                    del active_rocket_games[user_id]
                    safe_send_message(
                        context,
                        game['chat_id'],
                        "❌ Произошла ошибка. Пожалуйста, попробуйте позже.",
                        InlineKeyboardMarkup([[InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]])
                    )
                    return
            else:
                logger.error(f"Ошибка редактирования сообщения: {str(e)}")
                del active_rocket_games[user_id]
                safe_send_message(
                    context,
                    game['chat_id'],
                    "❌ Произошла ошибка. Пожалуйста, попробуйте позже.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]])
                )
                return
        except Exception as e:
            logger.error(f"Неожиданная ошибка при обновлении сообщения: {str(e)}")
            del active_rocket_games[user_id]
            safe_send_message(
                context,
                game['chat_id'],
                "❌ Произошла ошибка. Пожалуйста, попробуйте позже.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]])
            )
            return
        # Schedule the next update
        context.job_queue.run_once(update_multiplier, 0.5, context=context)

    # Schedule the first update
    context.job_queue.run_once(update_multiplier, 0.5, context=context)

def rocket_cashout(update: Update, context: CallbackContext) -> None:
    """Обработка вывода выигрыша в игре Ракетка"""
    query = update.callback_query
    if not safe_answer_query(query):
        return
    user_id = query.from_user.id
    callback_data = query.data
    if not callback_data.startswith('rocket_cashout_'):
        safe_edit_message(
            query,
            text="Ошибка: некорректный формат команды.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )
        return
    target_user_id = int(callback_data.split('_')[-1])
    if user_id != target_user_id:
        query.answer(text="Только пользователь, начавший игру, может забрать выигрыш!", show_alert=True)
        return
    if user_id not in active_rocket_games:
        safe_edit_message(
            query,
            text="Игра уже завершена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )
        return
    game = active_rocket_games[user_id]
    if game['crashed']:
        safe_edit_message(
            query,
            text="Игра уже завершена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )
        return
    game['crashed'] = True
    win_amount = game['bet'] * game['multiplier']
    user = get_user(user_id)
    user.deposit(win_amount, game['balance_type'])
    user.add_win(win_amount)
    safe_edit_message(
        query,
        text=f"🎉 Вы успешно забрали выигрыш! (@{user.username})\n\n"
             f"Ставка: {game['bet']:.2f} $\n"
             f"Множитель: x{game['multiplier']:.2f}\n"
             f"Выигрыш: {win_amount:.2f} $",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
        ])
    )
    if user_id in active_rocket_games:
        del active_rocket_games[user_id]

def matrix_bet(update: Update, context: CallbackContext) -> int:
    """Обработка ставки в игре Матрица"""
    user_id = update.effective_user.id
    initiator_id = context.user_data.get('initiator_id')
    chat_id = context.user_data.get('chat_id', update.effective_chat.id)
    if user_id != initiator_id:
        safe_send_message(
            context,
            chat_id,
            "Только пользователь, начавший игру, может ввести ставку.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='play_game')]])
        )
        return MATRIX_BET
    user = get_user(user_id)
    balance_type = 'virtual_balance' if user.use_virtual else 'balance'
    try:
        bet_amount = float(update.effective_message.text)
    except ValueError:
        safe_send_message(
            context,
            chat_id,
            "Пожалуйста, введите корректную сумму ставки (число).",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='play_game')]])
        )
        return MATRIX_BET
    if bet_amount < MIN_BET:
        safe_send_message(
            context,
            chat_id,
            f"Минимальная ставка: {MIN_BET} $",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='play_game')]])
        )
        return MATRIX_BET
    if bet_amount > MAX_BET:
        safe_send_message(
            context,
            chat_id,
            f"Максимальная ставка: {MAX_BET} $",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='play_game')]])
        )
        return MATRIX_BET
    if bet_amount > (user.virtual_balance if user.use_virtual else user.balance):
        safe_send_message(
            context,
            chat_id,
            f"Недостаточно средств на {'виртуальном' if user.use_virtual else 'реальном'} балансе.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='play_game')]])
        )
        return MATRIX_BET
    user.withdraw(bet_amount, balance_type)
    user.add_bet(bet_amount)
    active_matrix_games[user_id] = {
        'bet': bet_amount,
        'current_level': 0,
        'message_id': None,
        'chat_id': chat_id,
        'balance_type': balance_type,
        'initiator_id': initiator_id
    }
    try:
        current_multiplier = MATRIX_MULTIPLIERS[0]
        bomb_position = random.randint(1, 5)
        keyboard = []
        for i in range(1, 6):
            if i == bomb_position:
                callback_data = f'matrix_bomb_{user_id}'
            else:
                callback_data = f'matrix_correct_{i}_{user_id}'
            keyboard.append([InlineKeyboardButton(f"🔷 Клетка {i}", callback_data=callback_data)])
        keyboard.append([InlineKeyboardButton("❌ На первом уровне нельзя забрать выигрыш", callback_data=f'matrix_disabled_{user_id}')])
        keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data='back_to_menu')])
        text = (
            f"🔢 *Уровень 1* (@{user.username})\n\n"
            f"Ставка: {bet_amount:.2f} $\n"
            f"Текущий множитель: x{current_multiplier:.2f}\n"
            f"Выигрыш при выходе: {bet_amount * current_multiplier:.2f} $\n\n"
            f"Выберите клетку:"
        )
        sent_message = context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        active_matrix_games[user_id]['message_id'] = sent_message.message_id
        active_matrix_games[user_id]['chat_id'] = sent_message.chat_id
    except Exception as e:
        logger.error(f"Ошибка при отправке начального сообщения для Матрицы пользователю {user_id}: {str(e)}")
        safe_send_message(
            context,
            chat_id,
            "❌ Произошла ошибка. Пожалуйста, попробуйте позже.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]])
        )
        if user_id in active_matrix_games:
            del active_matrix_games[user_id]
    context.user_data['__current_conversation_state'] = None
    return ConversationHandler.END

def show_matrix_level(context: CallbackContext, user_id: int) -> None:
    """Отображение уровня игры Матрица"""
    game = active_matrix_games.get(user_id)
    if not game:
        return
    user = get_user(user_id)
    if game['current_level'] >= len(MATRIX_MULTIPLIERS):
        win_amount = game['bet'] * MATRIX_MULTIPLIERS[-1]
        user.deposit(win_amount, game['balance_type'])
        user.add_win(win_amount)
        safe_send_message(
            context,
            game['chat_id'],
            f"🏆 *Поздравляем!* 🏆 (@{user.username})\n\n"
            f"Вы прошли все уровни Матрицы!\n\n"
            f"Ставка: {game['bet']:.2f} $\n"
            f"Множитель: x{MATRIX_MULTIPLIERS[-1]:.2f}\n"
            f"Выигрыш: {win_amount:.2f} $\n\n"
            f"Невероятный результат!",
            InlineKeyboardMarkup([[InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]]),
            'Markdown'
        )
        if user_id in active_matrix_games:
            del active_matrix_games[user_id]
        return
    current_multiplier = MATRIX_MULTIPLIERS[game['current_level']] if game['current_level'] < len(MATRIX_MULTIPLIERS) else MATRIX_MULTIPLIERS[-1]
    if game['current_level'] == 0:
        cashout_text = "❌ На первом уровне нельзя забрать выигрыш"
        cashout_disabled = True
        cashout_amount = 0.0
    else:
        cashout_amount = game['bet'] * current_multiplier
        cashout_text = f"💰 Забрать {cashout_amount:.2f} $"
        cashout_disabled = False
    bomb_position = random.randint(1, 5)
    keyboard = []
    for i in range(1, 6):
        if i == bomb_position:
            callback_data = f'matrix_bomb_{user_id}'
        else:
            callback_data = f'matrix_correct_{i}_{user_id}'
        keyboard.append([InlineKeyboardButton(f"🔷 Клетка {i}", callback_data=callback_data)])
    if not cashout_disabled:
        keyboard.append([InlineKeyboardButton(cashout_text, callback_data=f'matrix_cashout_{user_id}')])
    else:
        keyboard.append([InlineKeyboardButton(cashout_text, callback_data=f'matrix_disabled_{user_id}')])
    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data='back_to_menu')])
    text = (
        f"🔢 *Уровень {game['current_level'] + 1}* (@{user.username})\n\n"
        f"Ставка: {game['bet']:.2f} $\n"
        f"Текущий множитель: x{current_multiplier:.2f}\n"
        f"Выигрыш при выходе: {cashout_amount:.2f} $\n\n"
        f"Выберите клетку:"
    )
    try:
        if game['message_id'] is None:
            sent_message = context.bot.send_message(
                chat_id=game['chat_id'],
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            game['message_id'] = sent_message.message_id
            game['chat_id'] = sent_message.chat_id
        else:
            context.bot.edit_message_text(
                chat_id=game['chat_id'],
                message_id=game['message_id'],
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
    except BadRequest as e:
        if "Message to edit not found" in str(e):
            logger.warning(f"Сообщение для редактирования не найдено, отправка нового для пользователя {user_id}")
            sent_message = context.bot.send_message(
                chat_id=game['chat_id'],
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            game['message_id'] = sent_message.message_id
            game['chat_id'] = sent_message.chat_id
        else:
            logger.error(f"Ошибка редактирования сообщения: {str(e)}")
            safe_send_message(
                context,
                game['chat_id'],
                "❌ Произошла ошибка. Пожалуйста, попробуйте позже.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]])
            )
            if user_id in active_matrix_games:
                del active_matrix_games[user_id]
    except Exception as e:
        logger.error(f"Неожиданная ошибка в show_matrix_level для пользователя {user_id}: {str(e)}")
        safe_send_message(
            context,
            game['chat_id'],
            "❌ Произошла ошибка. Пожалуйста, попробуйте позже.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]])
        )
        if user_id in active_matrix_games:
            del active_matrix_games[user_id]

def matrix_choice(update: Update, context: CallbackContext) -> None:
    """Обработка выбора в игре Матрица"""
    query = update.callback_query
    if not safe_answer_query(query):
        return
    user_id = query.from_user.id
    callback_data = query.data
    if not callback_data.startswith('matrix_'):
        safe_edit_message(
            query,
            text="Ошибка: некорректный формат команды.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )
        return
    parts = callback_data.split('_')
    target_user_id = int(parts[-1])
    if user_id != target_user_id:
        query.answer(text="Только пользователь, начавший игру, может продолжать!", show_alert=True)
        return
    if user_id not in active_matrix_games:
        safe_edit_message(
            query,
            text="Игра уже завершена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )
        return
    game = active_matrix_games[user_id]
    user = get_user(user_id)
    if callback_data.startswith('matrix_disabled'):
        query.answer(text="На первом уровне нельзя забрать выигрыш!", show_alert=True)
        return
    elif callback_data.startswith('matrix_correct'):
        game['current_level'] += 1
        show_matrix_level(context, user_id)
    elif callback_data.startswith('matrix_bomb'):
        text = (
            f"💥 Бомба! Игра окончена. (@{user.username})\n\n"
            f"Ставка: {game['bet']:.2f} $\n"
            f"Вы проиграли свою ставку 😢"
        )
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
        ])
        if not safe_edit_message(query, text, reply_markup, 'Markdown'):
            result = safe_send_message(context, game['chat_id'], text, reply_markup, 'Markdown')
            if result:
                game['message_id'] = result['message_id']
                game['chat_id'] = result['chat_id']
            else:
                logger.error(f"Не удалось отправить сообщение о проигрыше для пользователя {user_id}")
                if user_id in active_matrix_games:
                    del active_matrix_games[user_id]
        if user_id in active_matrix_games:
            del active_matrix_games[user_id]
    elif callback_data.startswith('matrix_cashout'):
        win_amount = game['bet'] * MATRIX_MULTIPLIERS[game['current_level']]
        user.deposit(win_amount, game['balance_type'])
        user.add_win(win_amount)
        text = (
            f"🎉 Вы забрали выигрыш! (@{user.username})\n\n"
            f"Ставка: {game['bet']:.2f} $\n"
            f"Множитель: x{MATRIX_MULTIPLIERS[game['current_level']]:.2f}\n"
            f"Выигрыш: {win_amount:.2f} $"
        )
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
        ])
        if not safe_edit_message(query, text, reply_markup, 'Markdown'):
            result = safe_send_message(context, game['chat_id'], text, reply_markup, 'Markdown')
            if result:
                game['message_id'] = result['message_id']
                game['chat_id'] = result['chat_id']
            else:
                logger.error(f"Не удалось отправить сообщение о выигрыше для пользователя {user_id}")
                if user_id in active_matrix_games:
                    del active_matrix_games[user_id]
        if user_id in active_matrix_games:
            del active_matrix_games[user_id]

def dice_bet(update: Update, context: CallbackContext) -> int:
    """Обработка ставки в игре Кости"""
    user_id = update.effective_user.id
    initiator_id = context.user_data.get('initiator_id')
    chat_id = context.user_data.get('chat_id', update.effective_chat.id)
    if user_id != initiator_id:
        safe_send_message(
            context,
            chat_id,
            "Только пользователь, начавший игру, может ввести ставку.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='play_game')]])
        )
        return DICE_BET
    user = get_user(user_id)
    balance_type = 'virtual_balance' if user.use_virtual else 'balance'
    try:
        bet_amount = float(update.effective_message.text)
    except ValueError:
        safe_send_message(
            context,
            chat_id,
            "Пожалуйста, введите корректную сумму ставки (число).",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='play_game')]])
        )
        return DICE_BET
    if bet_amount < MIN_BET:
        safe_send_message(
            context,
            chat_id,
            f"Минимальная ставка: {MIN_BET} $",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='play_game')]])
        )
        return DICE_BET
    if bet_amount > MAX_BET:
        safe_send_message(
            context,
            chat_id,
            f"Максимальная ставка: {MAX_BET} $",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='play_game')]])
        )
        return DICE_BET
    if bet_amount > (user.virtual_balance if user.use_virtual else user.balance):
        safe_send_message(
            context,
            chat_id,
            f"Недостаточно средств на {'виртуальном' if user.use_virtual else 'реальном'} балансе.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='play_game')]])
        )
        return DICE_BET
    user.withdraw(bet_amount, balance_type)
    user.add_bet(bet_amount)
    active_dice_games[user_id] = {
        'bet': bet_amount,
        'message_id': None,
        'chat_id': chat_id,
        'balance_type': balance_type,
        'initiator_id': initiator_id
    }
    keyboard = [
        [
            InlineKeyboardButton("Чёт", callback_data=f'dice_even_{user_id}'),
            InlineKeyboardButton("Нечёт", callback_data=f'dice_odd_{user_id}')
        ],
        [
            InlineKeyboardButton("1", callback_data=f'dice_1_{user_id}'),
            InlineKeyboardButton("2", callback_data=f'dice_2_{user_id}'),
            InlineKeyboardButton("3", callback_data=f'dice_3_{user_id}'),
            InlineKeyboardButton("4", callback_data=f'dice_4_{user_id}'),
            InlineKeyboardButton("5", callback_data=f'dice_5_{user_id}'),
            InlineKeyboardButton("6", callback_data=f'dice_6_{user_id}')
        ],
        [InlineKeyboardButton("🔙 В меню", callback_data='back_to_menu')]
    ]
    # Используем safe_send_message и проверяем результат
    result = safe_send_message(
        context,
        chat_id,
        f"🎲 Ваша ставка: {bet_amount:.2f} $ (@{user.username})\n\nВыберите тип ставки:",
        InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    if result:
        active_dice_games[user_id]['message_id'] = result['message_id']
        active_dice_games[user_id]['chat_id'] = result['chat_id']
        logger.info(f"Сообщение успешно отправлено для пользователя {user_id}, message_id={result['message_id']}")
    else:
        logger.error(f"Не удалось отправить сообщение для игры Кости пользователю {user_id}")
        # Очищаем состояние игры, чтобы пользователь мог начать заново
        if user_id in active_dice_games:
            del active_dice_games[user_id]
        safe_send_message(
            context,
            chat_id,
            "❌ Произошла ошибка при отправке сообщения. Пожалуйста, попробуйте снова.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data='back_to_menu')]])
        )
        return ConversationHandler.END
    context.user_data['__current_conversation_state'] = None
    return ConversationHandler.END

def dice_choice(update: Update, context: CallbackContext) -> None:
    """Обработка выбора в игре Кости"""
    query = update.callback_query
    if not safe_answer_query(query):
        return
    user_id = query.from_user.id
    callback_data = query.data
    if not callback_data.startswith('dice_'):
        safe_edit_message(
            query,
            text="Ошибка: некорректный формат команды.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )
        return
    parts = callback_data.split('_')
    target_user_id = int(parts[-1])
    if user_id != target_user_id:
        query.answer(text="Только пользователь, начавший игру, может продолжать!", show_alert=True)
        return
    if user_id not in active_dice_games:
        safe_edit_message(
            query,
            text="Игра уже завершена или не начата.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )
        return
    game = active_dice_games[user_id]
    user = get_user(user_id)
    dice_result = random.randint(1, 6)
    if callback_data.startswith('dice_even') or callback_data.startswith('dice_odd'):
        bet_type = 1
        if callback_data.startswith('dice_even'):
            player_choice = "чёт"
            win_condition = dice_result % 2 == 0
        else:
            player_choice = "нечёт"
            win_condition = dice_result % 2 == 1
        multiplier = DICE_MULTIPLIERS[1]
    else:
        bet_type = 2
        player_choice = parts[1]
        win_condition = int(player_choice) == dice_result
        multiplier = DICE_MULTIPLIERS[2]
    if win_condition:
        win_amount = game['bet'] * multiplier
        user.deposit(win_amount, game['balance_type'])
        user.add_win(win_amount)
        result_text = (
            f"🎉 Поздравляем! Вы выиграли! (@{user.username})\n\n"
            f"Ваш выбор: {player_choice}\n"
            f"Результат: {dice_result}\n"
            f"Множитель: x{multiplier:.1f}\n"
            f"Выигрыш: {win_amount:.2f} $"
        )
    else:
        result_text = (
            f"💥 Вы проиграли! (@{user.username})\n\n"
            f"Ваш выбор: {player_choice}\n"
            f"Результат: {dice_result}\n"
            f"Ставка: {game['bet']:.2f} $"
        )
    if not safe_edit_message(query, result_text, InlineKeyboardMarkup([[InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]]), 'Markdown'):
        safe_send_message(
            context,
            game['chat_id'],
            result_text,
            InlineKeyboardMarkup([[InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]]),
            'Markdown'
        )
    if user_id in active_dice_games:
        del active_dice_games[user_id]
        
def admin_panel(update: Update, context: CallbackContext) -> None:
    """Админ-панель"""
    query = update.callback_query
    user = get_user(query.from_user.id)
    if not user.is_admin:
        query.answer("У вас нет прав доступа!")
        return
    if not safe_answer_query(query):
        return
    keyboard = [
        [InlineKeyboardButton("📊 Статистика", callback_data='admin_stats')],
        [InlineKeyboardButton("💰 Начислить реальный баланс", callback_data='admin_add_balance')],
        [InlineKeyboardButton("💎 Начислить виртуальный баланс", callback_data='admin_add_virtual')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
    ]
    safe_edit_message(
        query,
        text="👑 *Админ-панель*",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def admin_stats(update: Update, context: CallbackContext) -> None:
    """Статистика казино"""
    query = update.callback_query
    user = get_user(query.from_user.id)
    if not user.is_admin:
        query.answer("У вас нет прав доступа!")
        return
    if not safe_answer_query(query):
        return
    total_users = len(users_db)
    total_balance = sum(user.balance for user in users_db.values())
    total_virtual_balance = sum(user.virtual_balance for user in users_db.values())
    total_bets = sum(user.total_bets for user in users_db.values())
    total_wins = sum(user.total_wins for user in users_db.values())
    text = (
        f"📊 *Статистика казино*\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"💰 Общий реальный баланс: {total_balance:.2f} $\n"
        f"💎 Общий виртуальный баланс: {total_virtual_balance:.2f} $\n"
        f"🎰 Всего ставок: {total_bets:.2f} $\n"
        f"🏆 Всего выиграно: {total_wins:.2f} $\n"
        f"📊 Профит казино: {total_bets - total_wins:.2f} $"
    )
    safe_edit_message(
        query,
        text=text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]
        ])
    )

def admin_add_balance(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    user = get_user(query.from_user.id)
    logger.info(f"Вход в admin_add_balance для пользователя {user.user_id} (@{user.username})")
    if not user.is_admin:
        query.answer("У вас нет прав доступа!")
        logger.warning(f"Пользователь {user.user_id} (@{user.username}) попытался открыть admin_add_balance без прав")
        return ConversationHandler.END
    if not safe_answer_query(query):
        logger.error("Не удалось ответить на callback-запрос")
        return ConversationHandler.END
    safe_edit_message(
        query,
        text="Введите username пользователя и сумму для начисления на реальный баланс через пробел (например: @username 100):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]
        ])
    )
    logger.info(f"Переход в состояние ADMIN_ADD_BALANCE для пользователя {user.user_id}")
    return ADMIN_ADD_BALANCE

def admin_add_virtual(update: Update, context: CallbackContext) -> int:
    """Вход в режим начисления виртуального баланса"""
    query = update.callback_query
    user = get_user(query.from_user.id)
    if not user.is_admin:
        query.answer("У вас нет прав доступа!")
        return ConversationHandler.END
    if not safe_answer_query(query):
        return ConversationHandler.END
    safe_edit_message(
        query,
        text="Введите username пользователя и сумму для начисления на виртуальный баланс через пробел (например: @username 100):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]
        ])
    )
    return ADMIN_ADD_VIRTUAL

def admin_add_balance_handler(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    user = get_user(user_id, update.effective_user.username)
    logger.info(
        f"Вход в admin_add_balance_handler для пользователя {user_id} (@{user.username}), текст сообщения: {update.effective_message.text}")

    if not user.is_admin:
        logger.warning(f"Пользователь {user_id} (@{user.username}) попытался выполнить админскую команду без прав")
        safe_send_message(
            context,
            update.effective_chat.id,
            "❌ У вас нет прав доступа!",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]])
        )
        context.user_data['__current_conversation_state'] = None
        return ConversationHandler.END

    try:
        parts = update.effective_message.text.strip().split()
        logger.info(f"Введенные данные: {parts}")
        if len(parts) != 2 or not parts[0].startswith('@'):
            raise ValueError("Неверный формат. Введите username и сумму через пробел (например: @username 100)")

        username = parts[0][1:].lower()
        amount = float(parts[1])
        if amount <= 0:
            raise ValueError("Сумма должна быть положительной")

        logger.info(f"Распарсено: username={username}, amount={amount}")

        users = load_users()
        logger.info(f"Загружено {len(users)} пользователей из {USER_FILE}")
        target_user_id = None
        for uid, u in users.items():
            stored_username = u.get('username', '').lower()
            logger.debug(f"Сравнение username: введено={username}, сохранено={stored_username}")
            if stored_username == username:
                target_user_id = uid
                break

        if not target_user_id:
            logger.warning(f"Пользователь @{username} не найден в базе")
            safe_send_message(
                context,
                update.effective_chat.id,
                f"❌ Пользователь @{username} не найден.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]])
            )
            return ADMIN_ADD_BALANCE

        logger.info(f"Найден пользователь: target_user_id={target_user_id}")
        target_user = get_user(int(target_user_id), username)
        logger.info(f"Получен объект пользователя: {target_user.user_id} (@{target_user.username})")

        target_user.deposit(amount, 'balance')
        logger.info(
            f"Начислено {amount} на реальный баланс для пользователя {target_user.user_id} (@{target_user.username})")

        users_db[int(target_user_id)] = target_user
        logger.info(f"Обновлен users_db для пользователя {target_user_id}")

        safe_send_message(
            context,
            update.effective_chat.id,
            f"✅ Пользователю @{target_user.username} начислено {amount:.2f} $ на реальный баланс\n"
            f"💰 Новый реальный баланс: {target_user.balance:.2f} $",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]])
        )

        if not safe_send_message(
                context,
                target_user.user_id,
                f"🎁 Администратор начислил вам {amount:.2f} $ на реальный баланс\n"
                f"💰 Ваш реальный баланс: {target_user.balance:.2f} $"
        ):
            logger.warning(f"Не удалось отправить уведомление пользователю @{target_user.username}")
            safe_send_message(
                context,
                update.effective_chat.id,
                f"⚠️ Не удалось отправить уведомление пользователю @{target_user.username}. Возможно, пользователь заблокировал бота.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]])
            )

    except ValueError as e:
        logger.error(f"Ошибка ввода в admin_add_balance_handler: {str(e)}")
        safe_send_message(
            context,
            update.effective_chat.id,
            f"❌ Ошибка: {str(e)}",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]])
        )
        return ADMIN_ADD_BALANCE
    except Exception as e:
        logger.error(f"Неожиданная ошибка в admin_add_balance_handler: {str(e)}", exc_info=True)
        safe_send_message(
            context,
            update.effective_chat.id,
            "❌ Произошла ошибка. Пожалуйста, попробуйте позже.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]])
        )
        context.user_data['__current_conversation_state'] = None
        return ConversationHandler.END

    context.user_data['__current_conversation_state'] = None
    logger.info(f"Успешное завершение admin_add_balance_handler для пользователя {user_id}, состояние сброшено")
    return ConversationHandler.END

def admin_add_virtual_handler(update: Update, context: CallbackContext) -> int:
    """Обработка начисления виртуального баланса"""
    user_id = update.effective_user.id
    user = get_user(user_id, update.effective_user.username)
    logger.info(f"Начало обработки admin_add_virtual_handler для пользователя {user_id} (@{user.username})")
    if not user.is_admin:
        safe_send_message(
            context,
            update.effective_chat.id,
            "У вас нет прав доступа!",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]])
        )
        logger.warning(f"Пользователь {user_id} (@{user.username}) попытался выполнить админскую команду без прав")
        return ConversationHandler.END
    try:
        parts = update.effective_message.text.strip().split()
        logger.info(f"Введенные данные: {parts}")
        if len(parts) != 2 or not parts[0].startswith('@'):
            raise ValueError("Неверный формат. Введите username и сумму через пробел (например: @username 100)")
        username = parts[0][1:].lower()
        amount = float(parts[1])
        if amount <= 0:
            raise ValueError("Сумма должна быть положительной")
        logger.info(f"Распарсено: username={username}, amount={amount}")
        users = load_users()
        logger.info(f"Загружено {len(users)} пользователей из {USER_FILE}")
        target_user_id = None
        for uid, u in users.items():
            if u.get('username', '').lower() == username:
                target_user_id = uid
                break
        if not target_user_id:
            safe_send_message(
                context,
                update.effective_chat.id,
                f"Пользователь @{username} не найден.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]])
            )
            logger.warning(f"Пользователь @{username} не найден в базе")
            return ADMIN_ADD_VIRTUAL
        logger.info(f"Найден пользователь: target_user_id={target_user_id}")
        target_user = get_user(int(target_user_id), username)
        logger.info(f"Получен объект пользователя: {target_user.user_id} (@{target_user.username})")
        target_user.deposit(amount, 'virtual_balance')
        logger.info(f"Начислено {amount} на виртуальный баланс для пользователя {target_user.user_id}")
        users_db[int(target_user_id)] = target_user
        logger.info(f"Обновлен users_db для пользователя {target_user_id}")
        safe_send_message(
            context,
            update.effective_chat.id,
            f"✅ Пользователю @{target_user.username} начислено {amount:.2f} $ на виртуальный баланс\n"
            f"💎 Новый виртуальный баланс: {target_user.virtual_balance:.2f} $",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]])
        )
        if not safe_send_message(
            context,
            target_user.user_id,
            f"🎁 Администратор начислил вам {amount:.2f} $ на виртуальный баланс\n"
            f"💎 Ваш виртуальный баланс: {target_user.virtual_balance:.2f} $"
        ):
            safe_send_message(
                context,
                update.effective_chat.id,
                f"⚠️ Не удалось отправить уведомление пользователю @{target_user.username}. Возможно, пользователь заблокировал бота.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]])
            )
    except ValueError as e:
        logger.error(f"Неверный ввод для admin_add_virtual пользователем {user.user_id}: {str(e)}")
        safe_send_message(
            context,
            update.effective_chat.id,
            f"❌ Ошибка: {str(e)}",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]])
        )
        return ADMIN_ADD_VIRTUAL
    except Exception as e:
        logger.error(f"Неожиданная ошибка в admin_add_virtual_handler для пользователя {user.user_id}: {str(e)}")
        safe_send_message(
            context,
            update.effective_chat.id,
            "❌ Произошла ошибка. Пожалуйста, попробуйте позже.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')]])
        )
        return ADMIN_ADD_VIRTUAL
    return ConversationHandler.END

def admin_add_balance_command(update: Update, context: CallbackContext) -> int:
    """Команда /addbalance"""
    user_id = update.effective_user.id
    context.user_data['__current_conversation_state'] = 'ADMIN_ADD_BALANCE'
    logger.info(f"Установлено состояние диалога: ADMIN_ADD_BALANCE для пользователя {user_id}")
    user = get_user(user_id, update.effective_user.username)
    if not user.is_admin:
        safe_send_message(
            context,
            update.effective_chat.id,
            "❌ У вас нет прав доступа!",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]])
        )
        logger.warning(f"Пользователь {user_id} (@{user.username}) попытался выполнить /addbalance без прав")
        context.user_data['__current_conversation_state'] = None
        return ConversationHandler.END
    safe_send_message(
        context,
        update.effective_chat.id,
        "Введите username пользователя и сумму для начисления на реальный баланс через пробел (например: @username 100):",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data='admin_panel')],
            [InlineKeyboardButton("❌ Отменить", callback_data='cancel_conversation')]
        ])
    )
    logger.info(f"Пользователь {user_id} (@{user.username}) вошел в режим начисления реального баланса")
    return ADMIN_ADD_BALANCE

def cancel_conversation(update: Update, context: CallbackContext) -> int:
    """Отмена текущего диалога"""
    query = update.callback_query
    user_id = query.from_user.id if query else update.effective_user.id
    user = get_user(user_id, query.from_user.username if query else update.effective_user.username)
    logger.info(f"Пользователь {user_id} (@{user.username}) вызвал отмену диалога")
    context.user_data['__current_conversation_state'] = None
    if query:
        if not safe_answer_query(query):
            return ConversationHandler.END
        safe_edit_message(
            query,
            text="❌ Действие отменено.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )
    else:
        safe_send_message(
            context,
            update.effective_chat.id,
            "❌ Действие отменено.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]
            ])
        )
    return ConversationHandler.END

def button_handler(update: Update, context: CallbackContext) -> None:
    """Обработка всех callback-запросов"""
    query = update.callback_query
    if not query or not safe_answer_query(query):
        return
    user_id = query.from_user.id
    user = get_user(user_id, query.from_user.username)
    data = query.data
    logger.info(f"Callback-запрос от пользователя {user_id} (@{user.username}): {data}")

    if data == 'back_to_menu':
        start(update, context)
    elif data == 'play_game':
        play_game(update, context)
    elif data == 'deposit':
        deposit(update, context)
    elif data == 'change_balance':
        change_balance(update, context)
    elif data == 'help':
        help_command(update, context)
    elif data == 'profile':
        profile_command(update, context)
    elif data == 'admin_panel':
        admin_panel(update, context)
    elif data == 'admin_stats':
        admin_stats(update, context)
    elif data == 'admin_add_balance':
        admin_add_balance(update, context)
    elif data == 'admin_add_virtual':
        admin_add_virtual(update, context)
    elif data.startswith('game_'):
        context.user_data['__current_conversation_state'] = 'GAME_CHOICE'
        game_choice(update, context)
    elif data.startswith('rocket_cashout_'):
        rocket_cashout(update, context)
    elif data.startswith('matrix_'):
        matrix_choice(update, context)
    elif data.startswith('dice_'):
        dice_choice(update, context)
    elif data == 'cancel_conversation':
        cancel_conversation(update, context)
    else:
        safe_edit_message(
            query,
            text="Неизвестная команда.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 В меню", callback_data='back_to_menu')]
            ])
        )

def error_handler(update: Update, context: CallbackContext) -> None:
    """Обработка ошибок"""
    logger.error(f"Ошибка: {context.error}", exc_info=True)
    if update:
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        if user_id and chat_id:
            # Clean up active game states
            if user_id in active_rocket_games:
                logger.info(f"Очищено состояние игры Ракетка для пользователя {user_id}")
                del active_rocket_games[user_id]
            if user_id in active_matrix_games:
                logger.info(f"Очищено состояние игры Матрица для пользователя {user_id}")
                del active_matrix_games[user_id]
            if user_id in active_dice_games:
                logger.info(f"Очищено состояние игры Кости для пользователя {user_id}")
                del active_dice_games[user_id]
            # Send error message only if no recent error message was sent
            if not hasattr(context, 'last_error_time') or (time.time() - context.last_error_time) > 5:
                safe_send_message(
                    context,
                    chat_id,
                    "❌ Произошла ошибка. Пожалуйста, попробуйте позже.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("🎮 В меню", callback_data='back_to_menu')]])
                )
                context.last_error_time = time.time()

def main() -> None:
    """Запуск бота"""
    try:
        updater = Updater(TOKEN, use_context=True)
        dp = updater.dispatcher

        # Обработчик диалогов
        conv_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(game_choice, pattern='^game_'),
                CallbackQueryHandler(deposit, pattern='^deposit$'),
                CallbackQueryHandler(admin_add_balance, pattern='^admin_add_balance$'),
                CallbackQueryHandler(admin_add_virtual, pattern='^admin_add_virtual$'),
                CommandHandler('addbalance', admin_add_balance_command)
            ],
            states={
                DEPOSIT_AMOUNT: [MessageHandler(Filters.text & ~Filters.command, deposit_amount)],
                ROCKET_BET: [MessageHandler(Filters.text & ~Filters.command, rocket_bet)],
                MATRIX_BET: [MessageHandler(Filters.text & ~Filters.command, matrix_bet)],
                DICE_BET: [MessageHandler(Filters.text & ~Filters.command, dice_bet)],
                ADMIN_ADD_BALANCE: [MessageHandler(Filters.text & ~Filters.command, admin_add_balance_handler)],
                ADMIN_ADD_VIRTUAL: [MessageHandler(Filters.text & ~Filters.command, admin_add_virtual_handler)],
            },
            fallbacks=[
                CommandHandler('cancel', cancel_conversation),
                CallbackQueryHandler(cancel_conversation, pattern='^cancel_conversation$'),
                CallbackQueryHandler(button_handler, pattern='^back_to_menu$|^play_game$|^deposit$|^change_balance$|^help$|^profile$|^admin_|^game_|^rocket_cashout_|^matrix_|^dice_')
            ]
        )

        dp.add_handler(conv_handler)
        dp.add_handler(CommandHandler("start", start))
        dp.add_handler(CommandHandler("help", help_command))
        dp.add_handler(CommandHandler("profile", profile_command))
        dp.add_handler(CommandHandler("add", add_virtual_balance))
        dp.add_handler(CommandHandler("base", base_command))
        dp.add_handler(CallbackQueryHandler(button_handler))
        dp.add_error_handler(error_handler)

        # Периодическая проверка инвойсов
        updater.job_queue.run_repeating(check_invoices, interval=30, first=10)

        updater.start_polling()
        logger.info("Бот запущен")
        updater.idle()
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {str(e)}", exc_info=True)
        raise

if __name__ == '__main__':
    main()
