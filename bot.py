import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telethon import TelegramClient
from telethon.sessions import StringSession

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])
TARGET_CHAT = os.environ["TARGET_CHAT"]
TIMEZONE = os.getenv("TIMEZONE", "Africa/Tunis")
DB_PATH = os.getenv("DB_PATH", "bot.db")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TELETHON_SESSION = os.environ["TELETHON_SESSION"]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

user_client = TelegramClient(
    StringSession(TELETHON_SESSION),
    API_ID,
    API_HASH,
)


class PostFlow(StatesGroup):
    waiting_text = State()
    waiting_schedule_choice = State()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL,
                text TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0
            )
            """
        )


def is_admin(message_or_query) -> bool:
    user = getattr(message_or_query, "from_user", None)
    return bool(user and user.id == ADMIN_USER_ID)


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📝 New Post"),
                KeyboardButton(text="📅 Scheduled Posts"),
            ],
            [
                KeyboardButton(text="⚡ Quick Publish"),
                KeyboardButton(text="🗑 Delete Post"),
            ],
            [
                KeyboardButton(text="👁 Preview Formatter"),
                KeyboardButton(text="ℹ️ Help"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Choose an action...",
    )


def format_post(text: str) -> str:
    text = text.strip()
    lower = text.lower()

    rules = [
        (("news", "announcement", "breaking"), "📰"),
        (("update", "updated", "new version"), "🔄"),
        (("important", "warning", "attention"), "⚠️"),
        (("offer", "discount", "sale", "promo"), "🔥"),
        (("contest", "giveaway", "winner", "prize"), "🎁"),
        (("video", "watch", "youtube"), "🎥"),
        (("link", "website", "http://", "https://"), "🔗"),
        (("channel", "telegram", "join"), "📢"),
        (("success", "done", "completed"), "✅"),
        (("tip", "tips", "guide"), "💡"),
        (("chatgpt", "business"), "🤖"),
    ]

    icon = "✨"
    for keywords, candidate in rules:
        if any(keyword in lower for keyword in keywords):
            icon = candidate
            break

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return text

    if not re.match(r"^[\U0001F300-\U0001FAFF\u2600-\u27BF]", lines[0]):
        lines[0] = f"{icon} {lines[0]}"

    return "\n".join(lines)


async def publish_as_personal_account(text: str):
    if not user_client.is_connected():
        await user_client.connect()

    if not await user_client.is_user_authorized():
        raise RuntimeError(
            "The personal Telegram session is not authorized. "
            "Generate TELETHON_SESSION locally first."
        )

    await user_client.send_message(
        entity=TARGET_CHAT,
        message=text,
        link_preview=True,
    )


def schedule_keyboard():
    now = datetime.now(ZoneInfo(TIMEZONE))
    presets = []

    for hour, label in [
        (10, "☀️ 10:00"),
        (14, "🌤 14:00"),
        (18, "🌆 18:00"),
        (21, "🌙 21:00"),
    ]:
        today = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if today > now:
            presets.append((f"Today {label}", today))

    tomorrow = now + timedelta(days=1)
    for hour, label in [
        (10, "☀️ 10:00"),
        (14, "🌤 14:00"),
        (18, "🌆 18:00"),
        (21, "🌙 21:00"),
    ]:
        dt = tomorrow.replace(hour=hour, minute=0, second=0, microsecond=0)
        presets.append((f"Tomorrow {label}", dt))

    buttons = []
    for i in range(0, len(presets), 2):
        row = []
        for label, dt in presets[i:i+2]:
            row.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"schedule:{dt.isoformat()}",
                )
            )
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_post_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Publish Now",
                    callback_data="publish_now",
                ),
                InlineKeyboardButton(
                    text="🕒 Schedule",
                    callback_data="choose_schedule",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="cancel",
                )
            ],
        ]
    )


async def send_scheduled_post(post_id: int):
    with db() as conn:
        row = conn.execute(
            """
            SELECT id, text, sent
            FROM scheduled_posts
            WHERE id = ?
            """,
            (post_id,),
        ).fetchone()

    if not row or row["sent"]:
        return

    try:
        await publish_as_personal_account(row["text"])

        with db() as conn:
            conn.execute(
                "UPDATE scheduled_posts SET sent = 1 WHERE id = ?",
                (post_id,),
            )

        logger.info("Sent scheduled post %s", post_id)

    except Exception:
        logger.exception("Failed to send scheduled post %s", post_id)


def load_jobs():
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, run_at
            FROM scheduled_posts
            WHERE sent = 0
            ORDER BY run_at
            """
        ).fetchall()

    for row in rows:
        run_at = datetime.fromisoformat(row["run_at"])

        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=tz)

        if run_at <= now:
            run_at = now

        scheduler.add_job(
            send_scheduled_post,
            "date",
            run_date=run_at,
            args=[row["id"]],
            id=f"post_{row['id']}",
            replace_existing=True,
        )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    if not is_admin(message):
        return

    await state.clear()

    await message.answer(
        "👋 Personal Account Publisher\n\n"
        "The control bot uses buttons, while posts are sent by your "
        "personal Telegram account.",
        reply_markup=main_menu(),
    )


@router.message(F.text == "📝 New Post")
async def new_post(message: Message, state: FSMContext):
    if not is_admin(message):
        return

    await state.set_state(PostFlow.waiting_text)
    await state.update_data(mode="normal")

    await message.answer(
        "✍️ Send the post text.\n\n"
        "I will format it and then show publish/schedule buttons."
    )


@router.message(F.text == "⚡ Quick Publish")
async def quick_publish(message: Message, state: FSMContext):
    if not is_admin(message):
        return

    await state.set_state(PostFlow.waiting_text)
    await state.update_data(mode="quick")

    await message.answer(
        "⚡ Send the post text and it will be published "
        "from your personal Telegram account."
    )


@router.message(F.text == "👁 Preview Formatter")
async def preview_mode(message: Message, state: FSMContext):
    if not is_admin(message):
        return

    await state.set_state(PostFlow.waiting_text)
    await state.update_data(mode="preview")

    await message.answer("👁 Send any text to preview formatting.")


@router.message(PostFlow.waiting_text)
async def receive_post_text(message: Message, state: FSMContext):
    if not is_admin(message):
        return

    if not message.text:
        await message.answer("Please send text only.")
        return

    data = await state.get_data()
    mode = data.get("mode", "normal")
    formatted = format_post(message.text)

    if mode == "quick":
        try:
            await publish_as_personal_account(formatted)
            await state.clear()
            await message.answer(
                "✅ Published from your personal account.",
                reply_markup=main_menu(),
            )
        except Exception as exc:
            logger.exception("Quick publish failed")
            await message.answer(
                f"❌ Publish failed:\n{type(exc).__name__}: {exc}"
            )
        return

    if mode == "preview":
        await state.clear()
        await message.answer(
            "👁 Preview:\n\n" + formatted,
            reply_markup=main_menu(),
        )
        return

    await state.update_data(post_text=formatted)
    await state.set_state(PostFlow.waiting_schedule_choice)

    await message.answer(
        "✨ Your formatted post:\n\n" + formatted,
        reply_markup=confirm_post_keyboard(),
    )


@router.callback_query(F.data == "publish_now")
async def publish_now(query: CallbackQuery, state: FSMContext):
    if not is_admin(query):
        return

    data = await state.get_data()
    text = data.get("post_text")

    if not text:
        await query.answer("Post expired. Start again.", show_alert=True)
        return

    try:
        await publish_as_personal_account(text)
        await state.clear()

        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await query.message.answer(
            "✅ Published from your personal account.",
            reply_markup=main_menu(),
        )
        await query.answer()

    except Exception as exc:
        logger.exception("Publish now failed")
        await query.answer("Publish failed", show_alert=True)
        await query.message.answer(
            f"❌ Publish failed:\n{type(exc).__name__}: {exc}"
        )


@router.callback_query(F.data == "choose_schedule")
async def choose_schedule(query: CallbackQuery):
    if not is_admin(query):
        return

    await query.message.answer(
        "🕒 Choose a publishing time:",
        reply_markup=schedule_keyboard(),
    )
    await query.answer()


@router.callback_query(F.data.startswith("schedule:"))
async def save_schedule(query: CallbackQuery, state: FSMContext):
    if not is_admin(query):
        return

    data = await state.get_data()
    text = data.get("post_text")

    if not text:
        await query.answer("Post expired. Start again.", show_alert=True)
        return

    run_at = datetime.fromisoformat(
        query.data.split("schedule:", 1)[1]
    )

    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO scheduled_posts (run_at, text)
            VALUES (?, ?)
            """,
            (run_at.isoformat(), text),
        )
        post_id = cursor.lastrowid

    scheduler.add_job(
        send_scheduled_post,
        "date",
        run_date=run_at,
        args=[post_id],
        id=f"post_{post_id}",
        replace_existing=True,
    )

    await state.clear()

    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await query.message.answer(
        f"✅ Post #{post_id} scheduled for "
        f"{run_at.strftime('%d %b %Y at %H:%M')} ({TIMEZONE}).",
        reply_markup=main_menu(),
    )
    await query.answer()


@router.message(F.text == "📅 Scheduled Posts")
async def scheduled_posts(message: Message):
    if not is_admin(message):
        return

    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, run_at, text
            FROM scheduled_posts
            WHERE sent = 0
            ORDER BY run_at
            LIMIT 20
            """
        ).fetchall()

    if not rows:
        await message.answer("📭 There are no scheduled posts.")
        return

    parts = ["📅 Scheduled Posts\n"]

    for row in rows:
        dt = datetime.fromisoformat(row["run_at"])
        preview = row["text"].replace("\n", " ")
        if len(preview) > 70:
            preview = preview[:67] + "..."

        parts.append(
            f"#{row['id']} • {dt.strftime('%d %b %H:%M')}\n{preview}"
        )

    await message.answer("\n\n".join(parts))


@router.message(F.text == "🗑 Delete Post")
async def delete_menu(message: Message):
    if not is_admin(message):
        return

    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, run_at
            FROM scheduled_posts
            WHERE sent = 0
            ORDER BY run_at
            LIMIT 20
            """
        ).fetchall()

    if not rows:
        await message.answer("📭 There are no scheduled posts to delete.")
        return

    keyboard = []

    for row in rows:
        dt = datetime.fromisoformat(row["run_at"])
        keyboard.append([
            InlineKeyboardButton(
                text=f"🗑 #{row['id']} • {dt.strftime('%d %b %H:%M')}",
                callback_data=f"delete:{row['id']}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")
    ])

    await message.answer(
        "Choose the scheduled post you want to delete:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


@router.callback_query(F.data.startswith("delete:"))
async def delete_selected(query: CallbackQuery):
    if not is_admin(query):
        return

    post_id = int(query.data.split(":", 1)[1])

    with db() as conn:
        cursor = conn.execute(
            """
            DELETE FROM scheduled_posts
            WHERE id = ? AND sent = 0
            """,
            (post_id,),
        )

    if cursor.rowcount:
        job = scheduler.get_job(f"post_{post_id}")
        if job:
            job.remove()

        await query.message.edit_text(
            f"✅ Scheduled post #{post_id} deleted."
        )
    else:
        await query.message.edit_text(
            "❌ Post not found or already published."
        )

    await query.answer()


@router.callback_query(F.data == "cancel")
async def cancel(query: CallbackQuery, state: FSMContext):
    if not is_admin(query):
        return

    await state.clear()

    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await query.message.answer(
        "Cancelled.",
        reply_markup=main_menu(),
    )
    await query.answer()


@router.message(F.text == "ℹ️ Help")
async def help_menu(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "🤖 Personal Account Publisher\n\n"
        "📝 New Post — format, preview, publish or schedule.\n"
        "⚡ Quick Publish — publish immediately.\n"
        "📅 Scheduled Posts — view pending posts.\n"
        "🗑 Delete Post — delete pending posts.\n"
        "👁 Preview Formatter — test formatting.\n\n"
        "Posts are sent by your personal Telegram account through MTProto."
    )


async def main():
    init_db()

    await user_client.connect()

    if not await user_client.is_user_authorized():
        raise RuntimeError(
            "TELETHON_SESSION is invalid or not authorized. "
            "Generate a new session locally."
        )

    me = await user_client.get_me()
    logger.info(
        "Personal Telegram account connected: %s (%s)",
        getattr(me, "username", None),
        me.id,
    )

    load_jobs()
    scheduler.start()

    logger.info("Control bot started. Timezone: %s", TIMEZONE)

    try:
        await dp.start_polling(bot)
    finally:
        await user_client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
