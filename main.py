import os
import random
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError(
        "Переменная окружения BOT_TOKEN не задана! "
        "Зайди в панель Bothost → настройки контейнера → Variables → добавь BOT_TOKEN"
    )

# Таймеры
CAPTCHA_TIMEOUT_MINUTES = 5
RULES_TIMEOUT_HOURS = 12

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- ТЕКСТ КАПЧИ ---
CAPTCHA_TEXT = """Здравствуйте, {name}!

Перед вступлением в группу подтвердите, что вы не спам-бот.

🤖 Решите простую задачу:"""

# --- ТЕКСТ ПРАВИЛ ---
RULES_TEXT = """✅ Капча пройдена! Теперь ознакомьтесь с правилами.

Здравствуйте, Вы собираетесь вступить в группу "Услуги Митино и окрестности". 
Ознакомьтесь пожалуйста с правилами группы

ПРАВИЛА ГРУППЫ

Вступая в данную группу, Вы соглашаетесь с ее правилами, обязуетесь их выполнять и предупреждены о штрафах за их нарушение

📝Данная Группа создана для поиска услуг в Митино и ближайших районах (для услуг репетиторов, нянь и домашнего персонала; вакансий есть соответствующие группы. Просьба публиковать там, или это будет расцениваться как реклама).

✴️ На данный момент реклама возможна только в группах Макс. Она должна быть замаркирована. 38-фз ст 18.1. Пишите в лс админам) 👩‍🔧

☝️В группе возможно размещение информационных сообщений. Без выделений текста, без фото/видео/коллажа/без призывов/цен. Такое сообщение  можно публиковать ОДИН раз в неделю без маркировки. 

🙋Просьба уважительно обращаться  друг к другу . 

❌За рекламу в личку участникам группы,   удаляем; за повторное размещение рекламы без маркировки и согласования делаем предупреждение, потом удаляем из группы.

‼️Посты в группе только по теме услуг (за исключением согласованной с админами рекламы) 

❌Не обсуждаем объявления друг друга. Все обсуждения в ЛС или в группе для общения (ссылка у админов)!

‼️Если не согласны с чем-то или увидели нарушение - пишите админам в лс. 

‼️‼️За содержание текста сообщений ответственны авторы сообщений. Админы за это ответственность не несут. 

‼️Ссылки на другие группы по согласованию с админами

🚫В группе запрещено выкладывать информацию о любых денежных сборах и информацию, противоречащую законодательству Российской Федерации 

При несоблюдении правил группы удаление❌

⏰ У вас есть {hours} часов, чтобы подтвердить согласие с правилами. Нажмите кнопку ниже."""


# --- КАПЧА ---
def generate_captcha():
    a = random.randint(1, 9)
    b = random.randint(1, 9)
    correct_answer = a + b

    wrong_answers = set()
    while len(wrong_answers) < 3:
        offset = random.randint(1, 5) * random.choice([-1, 1])
        candidate = correct_answer + offset
        if candidate > 0 and candidate not in wrong_answers:
            wrong_answers.add(candidate)

    options = [correct_answer] + list(wrong_answers)
    random.shuffle(options)
    question = f"Сколько будет {a} + {b}?"
    return question, correct_answer, options


def make_captcha_keyboard(options, correct_answer):
    buttons = []
    for option in options:
        callback = f"cap_{option}_{correct_answer}"
        buttons.append(InlineKeyboardButton(str(option), callback_data=callback))
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(keyboard)


def make_rules_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("✅ Согласен с правилами", callback_data="rules_yes"),
            InlineKeyboardButton("❌ Не согласен", callback_data="rules_no"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


# --- ХРАНИЛИЩЕ ---
pending_users: Dict[int, dict] = {}


# --- ФОНОВАЯ ПРОВЕРКА ТАЙМАУТОВ (БЕЗ job_queue) ---
async def timeout_checker(bot):
    """Фоновая задача: каждые 60 секунд проверяет просроченные подтверждения."""
    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        to_remove = []

        for user_id, record in pending_users.items():
            if record["confirmed"]:
                continue

            kick_reason = None

            if record["stage"] == "captcha":
                if now > record["captcha_deadline"]:
                    kick_reason = f"не прошёл капчу за {CAPTCHA_TIMEOUT_MINUTES} минут"

            elif record["stage"] == "rules":
                if now > record["rules_deadline"]:
                    kick_reason = f"не подтвердил правила за {RULES_TIMEOUT_HOURS} часов"

            if kick_reason:
                to_remove.append(user_id)
                logger.warning(f"Пользователь {user_id} будет исключён: {kick_reason}")

        for user_id in to_remove:
            record = pending_users.pop(user_id, None)
            if not record:
                continue
            chat_id = record["chat_id"]
            message_id = record.get("message_id")
            try:
                # Удаляем сообщение бота (капча или правила)
                if message_id:
                    try:
                        await bot.delete_message(chat_id=chat_id, message_id=message_id)
                    except Exception:
                        pass

                # Кик через бан/разбан
                await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
                await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)

                if record["stage"] == "captcha":
                    reason_text = f"⏰ Участник не прошёл капчу за {CAPTCHA_TIMEOUT_MINUTES} минут и был удалён из группы."
                else:
                    reason_text = f"⏰ Участник не подтвердил правила за {RULES_TIMEOUT_HOURS} часов и был удалён из группы."

                await bot.send_message(
                    chat_id=chat_id,
                    text=reason_text,
                )
                logger.info(f"Пользователь {user_id} исключён: {reason_text}")
            except Exception as e:
                logger.error(f"Не удалось исключить пользователя {user_id}: {e}")


# --- ОБРАБОТКА НОВЫХ УЧАСТНИКОВ ---
async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.new_chat_members:
        return

    chat = message.chat
    if chat.type not in ("group", "supergroup"):
        return

    for member in message.new_chat_members:
        if member.is_bot:
            continue

        user_id = member.id

        captcha_deadline = datetime.now() + timedelta(minutes=CAPTCHA_TIMEOUT_MINUTES)
        rules_deadline = datetime.now() + timedelta(hours=RULES_TIMEOUT_HOURS)

        question, correct_answer, options = generate_captcha()
        full_text = f"{CAPTCHA_TEXT.format(name=member.full_name)}\n\n🔢 {question}"

        try:
            sent_msg = await message.reply_text(
                text=full_text,
                reply_markup=make_captcha_keyboard(options, correct_answer),
            )

            pending_users[user_id] = {
                "chat_id": chat.id,
                "captcha_deadline": captcha_deadline,
                "rules_deadline": rules_deadline,
                "confirmed": False,
                "stage": "captcha",
                "message_id": sent_msg.message_id,
            }
            logger.info(
                f"Новый участник {member.full_name} (id={user_id}) — этап: капча, "
                f"до {captcha_deadline}"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить капчу: {e}")


# --- ОБРАБОТКА НАЖАТИЙ КНОПОК ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    # --- ЭТАП 1: КАПЧА ---
    if data.startswith("cap_"):
        if user_id not in pending_users:
            await query.edit_message_text("ℹ️ Это сообщение уже неактуально.")
            return

        record = pending_users[user_id]
        if record["stage"] != "captcha":
            return

        parts = data.split("_")
        if len(parts) != 3:
            return

        selected_answer = int(parts[1])
        correct_answer = int(parts[2])

        if selected_answer == correct_answer:
            # Капча пройдена — удаляем сообщение с капчей
            record["stage"] = "rules"
            rules_text = RULES_TEXT.format(hours=RULES_TIMEOUT_HOURS)

            try:
                # Удаляем сообщение с капчей
                await query.delete_message()

                # Отправляем новое сообщение с правилами
                sent_rules = await context.bot.send_message(
                    chat_id=record["chat_id"],
                    text=rules_text,
                    reply_markup=make_rules_keyboard(),
                )

                # Обновляем message_id в хранилище
                record["message_id"] = sent_rules.message_id

                logger.info(f"Пользователь {user_id} прошёл капчу → показываем правила.")
            except Exception as e:
                logger.error(f"Не удалось показать правила для {user_id}: {e}")
        else:
            # Неверный ответ — новая капча
            question, new_correct, options = generate_captcha()
            new_text = (
                f"❌ Неверно! Попробуйте ещё раз.\n\n"
                f"🔢 {question}\n\n"
                f"⏰ Капча сгорает через {CAPTCHA_TIMEOUT_MINUTES} минут после вступления."
            )
            try:
                await query.edit_message_text(
                    text=new_text,
                    reply_markup=make_captcha_keyboard(options, new_correct),
                )
                logger.info(f"Пользователь {user_id} ответил неверно — новая капча.")
            except Exception as e:
                logger.error(f"Не удалось обновить капчу для {user_id}: {e}")

    # --- ЭТАП 2: ПРАВИЛА ---
    elif data == "rules_yes":
        if user_id not in pending_users:
            await query.edit_message_text("ℹ️ Это сообщение уже неактуально.")
            return

        record = pending_users[user_id]
        if record["stage"] != "rules":
            return

        record["confirmed"] = True
        pending_users.pop(user_id, None)

        try:
            # Удаляем сообщение с правилами
            await query.delete_message()

            # Отправляем короткое приветствие
            await context.bot.send_message(
                chat_id=record["chat_id"],
                text="✅ Спасибо! Вы подтвердили согласие с правилами. Добро пожаловать в группу!",
            )

            logger.info(f"Пользователь {user_id} согласился с правилами — доступ открыт.")
        except Exception as e:
            logger.error(f"Не удалось удалить сообщение с правилами: {e}")

    elif data == "rules_no":
        if user_id not in pending_users:
            await query.edit_message_text("ℹ️ Это сообщение уже неактуально.")
            return

        record = pending_users[user_id]
        chat_id = record["chat_id"]
        pending_users.pop(user_id, None)

        try:
            # Удаляем сообщение с правилами
            await query.delete_message()

            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id)

            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Участник отказался подтвердить правила и был удалён из группы.",
            )
            logger.info(f"Пользователь {user_id} отказался от правил — кик.")
        except Exception as e:
            logger.error(f"Не удалось исключить пользователя {user_id}: {e}")


# --- БЛОКИРОВКА СООБЩЕНИЙ ДО ПОДТВЕРЖДЕНИЯ ---
async def block_unconfirmed_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.from_user:
        return

    user_id = message.from_user.id
    if user_id in pending_users and not pending_users[user_id]["confirmed"]:
        try:
            await message.delete()
            logger.info(f"Удалено сообщение от неподтвердившего пользователя {user_id}")
        except Exception as e:
            logger.error(f"Не удалось удалить сообщение от {user_id}: {e}")


# --- ЗАПУСК БЕЗ job_queue ---
async def post_init(application):
    """Запускаем фоновый таймер через asyncio вместо job_queue."""
    asyncio.create_task(timeout_checker(application.bot))
    logger.info("✅ Фоновая проверка таймаутов запущена через asyncio.")


def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, block_unconfirmed_messages))

    logger.info("🤖 Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
