import asyncio
import json
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
    waiting_repeat_choice = State()


class GroupFlow(StatesGroup):
    waiting_ref = State()


class MultiPostFlow(StatesGroup):
    waiting_text = State()
    waiting_selection = State()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn, table: str, column: str, coltype: str):
    """Add a column to an existing table if it isn't there yet.

    CREATE TABLE IF NOT EXISTS does nothing to a table that already
    exists with an older schema, so this makes upgrades safe for
    databases created by earlier versions of this bot.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS repeating_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_ref TEXT NOT NULL,
                title TEXT,
                selected INTEGER NOT NULL DEFAULT 0,
                added_at TEXT NOT NULL
            )
            """
        )

        # Migrations for databases created by earlier versions of this bot.
        _ensure_column(conn, "scheduled_posts", "chat_ref", "TEXT")
        _ensure_column(conn, "repeating_posts", "chat_ref", "TEXT")
        _ensure_column(conn, "repeating_posts", "paused", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "repeating_posts", "target_refs", "TEXT")


def is_admin(message_or_query) -> bool:
    user = getattr(message_or_query, "from_user", None)
    return bool(user and user.id == ADMIN_USER_ID)


def get_target_chat_ref() -> str:
    """Return the chat currently selected in 📋 My Groups, or fall back
    to the TARGET_CHAT env var if no group has been added/selected yet
    (keeps old single-group setups working unchanged)."""
    with db() as conn:
        row = conn.execute(
            "SELECT chat_ref FROM groups WHERE selected = 1 LIMIT 1"
        ).fetchone()
    return row["chat_ref"] if row else TARGET_CHAT


def group_label(chat_ref) -> str:
    """Friendly display name for a stored chat_ref, using the saved
    group title when available."""
    if not chat_ref:
        return "(default target)"
    with db() as conn:
        row = conn.execute(
            "SELECT title FROM groups WHERE chat_ref = ?", (chat_ref,)
        ).fetchone()
    if row and row["title"]:
        return row["title"]
    return chat_ref


def repeat_target_label(chat_ref, target_refs_json) -> str:
    """Display label for a repeating post's target(s) — a single group
    (chat_ref) or several groups stored as a JSON list in target_refs."""
    if target_refs_json:
        try:
            refs = json.loads(target_refs_json)
        except (TypeError, ValueError):
            refs = []

        if refs:
            labels = [group_label(r) for r in refs]
            if len(labels) > 3:
                shown = ", ".join(labels[:3])
                return f"{len(labels)} groups: {shown}, ..."
            return f"{len(labels)} group(s): " + ", ".join(labels)

    return group_label(chat_ref)


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📝 New Post"),
                KeyboardButton(text="⚡ Quick Publish"),
            ],
            [
                KeyboardButton(text="📣 Multi Group Post"),
                KeyboardButton(text="➕ Add Group"),
            ],
            [
                KeyboardButton(text="📋 My Groups"),
                KeyboardButton(text="📅 Scheduled Posts"),
            ],
            [
                KeyboardButton(text="🔄 Repeating Posts"),
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


async def resolve_entity_ref(ref: str):
    """Resolve a username/link/ID to a Telethon entity, retrying once
    after refreshing the dialog cache (needed for bare numeric IDs —
    see the get_dialogs() note in main())."""
    if not user_client.is_connected():
        await user_client.connect()

    try:
        return await user_client.get_entity(ref)
    except ValueError:
        await user_client.get_dialogs()
        return await user_client.get_entity(ref)


async def publish_as_personal_account(text: str, chat_ref=None):
    if not user_client.is_connected():
        await user_client.connect()

    if not await user_client.is_user_authorized():
        raise RuntimeError(
            "The personal Telegram session is not authorized. "
            "Generate TELETHON_SESSION locally first."
        )

    target = chat_ref or get_target_chat_ref()

    await user_client.send_message(
        entity=target,
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
                    text="🔁 Repeat",
                    callback_data="choose_repeat",
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


REPEAT_INTERVALS = [
    (30, "30 min"),
    (60, "1 hour"),
    (120, "2 hours"),
    (180, "3 hours"),
    (360, "6 hours"),
    (720, "12 hours"),
    (1440, "24 hours"),
]


def interval_keyboard(callback_prefix: str):
    buttons = []
    for i in range(0, len(REPEAT_INTERVALS), 2):
        row = []
        for minutes, label in REPEAT_INTERVALS[i:i+2]:
            row.append(
                InlineKeyboardButton(
                    text=f"🔁 {label}",
                    callback_data=f"{callback_prefix}:{minutes}",
                )
            )
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def repeat_interval_keyboard():
    return interval_keyboard("repeat")


def multi_group_keyboard(groups, selected_ids):
    all_selected = bool(groups) and len(selected_ids) == len(groups)

    keyboard = [[
        InlineKeyboardButton(
            text="⬜ Deselect All" if all_selected else "☑️ Select All",
            callback_data="toggle_all_groups",
        )
    ]]

    for g in groups:
        mark = "✅" if g["id"] in selected_ids else "▫️"
        label = g["title"] or g["chat_ref"]
        keyboard.append([
            InlineKeyboardButton(
                text=f"{mark} {label}",
                callback_data=f"toggle_group:{g['id']}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton(text="🚀 Publish to Selected", callback_data="multi_publish")
    ])
    keyboard.append([
        InlineKeyboardButton(text="🔁 Repeat on Selected", callback_data="choose_multi_repeat")
    ])
    keyboard.append([
        InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def send_scheduled_post(post_id: int):
    with db() as conn:
        row = conn.execute(
            """
            SELECT id, text, sent, chat_ref
            FROM scheduled_posts
            WHERE id = ?
            """,
            (post_id,),
        ).fetchone()

    if not row or row["sent"]:
        return

    try:
        await publish_as_personal_account(row["text"], chat_ref=row["chat_ref"])

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


async def send_repeating_post(repeat_id: int):
    with db() as conn:
        row = conn.execute(
            """
            SELECT id, text, active, paused, chat_ref, target_refs
            FROM repeating_posts
            WHERE id = ?
            """,
            (repeat_id,),
        ).fetchone()

    if not row or not row["active"] or row["paused"]:
        return

    targets = []
    if row["target_refs"]:
        try:
            targets = json.loads(row["target_refs"]) or []
        except (TypeError, ValueError):
            targets = []

    if targets:
        # Multi-group repeat: send to every saved target, one at a time.
        failures = []
        for ref in targets:
            try:
                await publish_as_personal_account(row["text"], chat_ref=ref)
            except Exception as exc:
                logger.exception("Failed to send repeating post %s to %s", repeat_id, ref)
                failures.append(f"{group_label(ref)}: {type(exc).__name__}: {exc}")
            await asyncio.sleep(3)

        logger.info(
            "Repeating post %s sent to %d/%d group(s)",
            repeat_id, len(targets) - len(failures), len(targets),
        )

        if failures:
            try:
                await bot.send_message(
                    ADMIN_USER_ID,
                    f"⚠️ Repeating post #{repeat_id} failed for:\n" + "\n".join(failures),
                )
            except Exception:
                logger.exception("Failed to notify admin about repeat failures")
    else:
        # Single-group repeat (original behaviour).
        try:
            await publish_as_personal_account(row["text"], chat_ref=row["chat_ref"])
            logger.info("Sent repeating post %s", repeat_id)
        except Exception:
            logger.exception("Failed to send repeating post %s", repeat_id)


def load_repeating_jobs():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, interval_minutes
            FROM repeating_posts
            WHERE active = 1 AND paused = 0
            """
        ).fetchall()

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    for row in rows:
        scheduler.add_job(
            send_repeating_post,
            "interval",
            minutes=row["interval_minutes"],
            args=[row["id"]],
            id=f"repeat_{row['id']}",
            replace_existing=True,
            next_run_time=now,
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
    chat_ref = get_target_chat_ref()

    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO scheduled_posts (run_at, text, chat_ref)
            VALUES (?, ?, ?)
            """,
            (run_at.isoformat(), text, chat_ref),
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
        f"{run_at.strftime('%d %b %Y at %H:%M')} ({TIMEZONE}) "
        f"→ {group_label(chat_ref)}.",
        reply_markup=main_menu(),
    )
    await query.answer()


@router.callback_query(F.data == "choose_repeat")
async def choose_repeat(query: CallbackQuery):
    if not is_admin(query):
        return

    await query.message.answer(
        "🔁 Choose how often this post should repeat:",
        reply_markup=repeat_interval_keyboard(),
    )
    await query.answer()


@router.callback_query(F.data.startswith("repeat:"))
async def save_repeat(query: CallbackQuery, state: FSMContext):
    if not is_admin(query):
        return

    data = await state.get_data()
    text = data.get("post_text")

    if not text:
        await query.answer("Post expired. Start again.", show_alert=True)
        return

    minutes = int(query.data.split("repeat:", 1)[1])
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    chat_ref = get_target_chat_ref()

    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO repeating_posts (text, interval_minutes, active, paused, created_at, chat_ref)
            VALUES (?, ?, 1, 0, ?, ?)
            """,
            (text, minutes, now.isoformat(), chat_ref),
        )
        repeat_id = cursor.lastrowid

    scheduler.add_job(
        send_repeating_post,
        "interval",
        minutes=minutes,
        args=[repeat_id],
        id=f"repeat_{repeat_id}",
        replace_existing=True,
        next_run_time=now,
    )

    await state.clear()

    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    label = next((lbl for m, lbl in REPEAT_INTERVALS if m == minutes), f"{minutes} min")
    await query.message.answer(
        f"✅ Repeating post #{repeat_id} created — publishing now, then every {label} "
        f"→ {group_label(chat_ref)}.\n\n"
        "Use 🔄 Repeating Posts to pause, resume, or delete it later.",
        reply_markup=main_menu(),
    )
    await query.answer()


@router.message(F.text == "🔄 Repeating Posts")
async def repeating_posts_menu(message: Message):
    if not is_admin(message):
        return

    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, text, interval_minutes, paused, chat_ref, target_refs
            FROM repeating_posts
            WHERE active = 1
            ORDER BY id
            LIMIT 20
            """
        ).fetchall()

    if not rows:
        await message.answer("📭 There are no active repeating posts.")
        return

    keyboard = []
    lines = ["🔄 Repeating Posts\n"]

    for row in rows:
        label = next(
            (lbl for m, lbl in REPEAT_INTERVALS if m == row["interval_minutes"]),
            f"{row['interval_minutes']} min",
        )
        preview = row["text"].replace("\n", " ")
        if len(preview) > 50:
            preview = preview[:47] + "..."

        status = "⏸ Paused" if row["paused"] else "▶ Running"
        target = repeat_target_label(row["chat_ref"], row["target_refs"])
        lines.append(
            f"#{row['id']} • every {label} • {status} • {target}\n{preview}"
        )

        toggle_btn = (
            InlineKeyboardButton(text=f"▶ Resume #{row['id']}", callback_data=f"resume_repeat:{row['id']}")
            if row["paused"]
            else InlineKeyboardButton(text=f"⏸ Pause #{row['id']}", callback_data=f"pause_repeat:{row['id']}")
        )

        keyboard.append([
            toggle_btn,
            InlineKeyboardButton(
                text=f"🗑 Delete #{row['id']}",
                callback_data=f"stop_repeat:{row['id']}",
            ),
        ])

    await message.answer(
        "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


@router.callback_query(F.data.startswith("pause_repeat:"))
async def pause_repeat(query: CallbackQuery):
    if not is_admin(query):
        return

    repeat_id = int(query.data.split(":", 1)[1])

    with db() as conn:
        cursor = conn.execute(
            "UPDATE repeating_posts SET paused = 1 WHERE id = ? AND active = 1",
            (repeat_id,),
        )

    if cursor.rowcount:
        job = scheduler.get_job(f"repeat_{repeat_id}")
        if job:
            job.remove()
        await query.message.edit_text(f"⏸ Repeating post #{repeat_id} paused.")
    else:
        await query.message.edit_text("❌ Repeating post not found.")

    await query.answer()


@router.callback_query(F.data.startswith("resume_repeat:"))
async def resume_repeat(query: CallbackQuery):
    if not is_admin(query):
        return

    repeat_id = int(query.data.split(":", 1)[1])
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    with db() as conn:
        cursor = conn.execute(
            "UPDATE repeating_posts SET paused = 0 WHERE id = ? AND active = 1",
            (repeat_id,),
        )
        row = conn.execute(
            "SELECT interval_minutes FROM repeating_posts WHERE id = ?",
            (repeat_id,),
        ).fetchone()

    if cursor.rowcount and row:
        scheduler.add_job(
            send_repeating_post,
            "interval",
            minutes=row["interval_minutes"],
            args=[repeat_id],
            id=f"repeat_{repeat_id}",
            replace_existing=True,
            next_run_time=now,
        )
        await query.message.edit_text(f"▶ Repeating post #{repeat_id} resumed.")
    else:
        await query.message.edit_text("❌ Repeating post not found.")

    await query.answer()


@router.callback_query(F.data.startswith("stop_repeat:"))
async def stop_repeat(query: CallbackQuery):
    if not is_admin(query):
        return

    repeat_id = int(query.data.split(":", 1)[1])

    with db() as conn:
        cursor = conn.execute(
            "UPDATE repeating_posts SET active = 0 WHERE id = ? AND active = 1",
            (repeat_id,),
        )

    if cursor.rowcount:
        job = scheduler.get_job(f"repeat_{repeat_id}")
        if job:
            job.remove()

        await query.message.edit_text(f"🗑 Repeating post #{repeat_id} deleted.")
    else:
        await query.message.edit_text("❌ Repeating post not found or already deleted.")

    await query.answer()


@router.message(F.text == "➕ Add Group")
async def add_group_start(message: Message, state: FSMContext):
    if not is_admin(message):
        return

    await state.set_state(GroupFlow.waiting_ref)

    await message.answer(
        "➕ Send the group/channel's @username, invite link "
        "(https://t.me/+AbCdEfGh...), or numeric chat ID.\n\n"
        "Your personal Telegram account must already be a member "
        "and able to send messages there."
    )


@router.message(GroupFlow.waiting_ref)
async def add_group_receive(message: Message, state: FSMContext):
    if not is_admin(message):
        return

    ref = (message.text or "").strip()
    await state.clear()

    if not ref:
        await message.answer("Please send text only.", reply_markup=main_menu())
        return

    try:
        entity = await resolve_entity_ref(ref)
        title = (
            getattr(entity, "title", None)
            or getattr(entity, "username", None)
            or ref
        )

        with db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO groups (chat_ref, title, selected, added_at)
                VALUES (?, ?, 0, ?)
                """,
                (ref, title, datetime.now(ZoneInfo(TIMEZONE)).isoformat()),
            )
            group_id = cursor.lastrowid

        await message.answer(
            f"✅ Group added: {title} (#{group_id}).\n\n"
            "Open 📋 My Groups to select it as your active publishing target.",
            reply_markup=main_menu(),
        )

    except Exception as exc:
        logger.exception("Failed to add group")
        await message.answer(
            f"❌ Could not add this group:\n{type(exc).__name__}: {exc}\n\n"
            "Make sure your personal account has already joined it and "
            "the username/link/ID is correct.",
            reply_markup=main_menu(),
        )


@router.message(F.text == "📋 My Groups")
async def my_groups_menu(message: Message):
    if not is_admin(message):
        return

    with db() as conn:
        rows = conn.execute(
            "SELECT id, chat_ref, title, selected FROM groups ORDER BY id"
        ).fetchall()

    if not rows:
        await message.answer(
            "📭 No groups added yet. Use ➕ Add Group first.\n\n"
            "Until you add and select a group, posts go to the TARGET_CHAT "
            "set in your Railway variables (if any)."
        )
        return

    lines = ["📋 Your Groups\n"]
    keyboard = []

    for row in rows:
        mark = "✅" if row["selected"] else "▫️"
        label = row["title"] or row["chat_ref"]
        lines.append(f"{mark} #{row['id']} • {label}\n{row['chat_ref']}")

        btn_row = []
        if not row["selected"]:
            btn_row.append(
                InlineKeyboardButton(
                    text=f"✅ Select #{row['id']}",
                    callback_data=f"select_group:{row['id']}",
                )
            )
        btn_row.append(
            InlineKeyboardButton(
                text=f"🗑 Remove #{row['id']}",
                callback_data=f"remove_group:{row['id']}",
            )
        )
        keyboard.append(btn_row)

    await message.answer(
        "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


@router.callback_query(F.data.startswith("select_group:"))
async def select_group(query: CallbackQuery):
    if not is_admin(query):
        return

    group_id = int(query.data.split(":", 1)[1])

    with db() as conn:
        conn.execute("UPDATE groups SET selected = 0")
        cursor = conn.execute(
            "UPDATE groups SET selected = 1 WHERE id = ?", (group_id,)
        )

    if cursor.rowcount:
        await query.message.edit_text(
            "✅ Group selected. New Post, Quick Publish, Schedule and "
            "Repeat will now publish there."
        )
    else:
        await query.message.edit_text("❌ Group not found.")

    await query.answer()


@router.callback_query(F.data.startswith("remove_group:"))
async def remove_group(query: CallbackQuery):
    if not is_admin(query):
        return

    group_id = int(query.data.split(":", 1)[1])

    with db() as conn:
        cursor = conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))

    if cursor.rowcount:
        await query.message.edit_text(
            f"🗑 Group #{group_id} removed from the list "
            "(this does not remove you from it on Telegram)."
        )
    else:
        await query.message.edit_text("❌ Group not found.")

    await query.answer()


@router.message(F.text == "📣 Multi Group Post")
async def multi_post_start(message: Message, state: FSMContext):
    if not is_admin(message):
        return

    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM groups").fetchone()["c"]

    if not count:
        await message.answer("📭 No groups added yet. Use ➕ Add Group first.")
        return

    await state.set_state(MultiPostFlow.waiting_text)
    await message.answer("📣 Send the post text to publish to multiple groups.")


@router.message(MultiPostFlow.waiting_text)
async def multi_post_receive_text(message: Message, state: FSMContext):
    if not is_admin(message):
        return

    if not message.text:
        await message.answer("Please send text only.")
        return

    formatted = format_post(message.text)

    with db() as conn:
        groups = conn.execute(
            "SELECT id, chat_ref, title FROM groups ORDER BY id"
        ).fetchall()

    await state.update_data(multi_text=formatted, multi_selected=[])
    await state.set_state(MultiPostFlow.waiting_selection)

    await message.answer(
        "✨ Formatted post:\n\n" + formatted + "\n\nSelect the groups to publish to:",
        reply_markup=multi_group_keyboard(groups, []),
    )


@router.callback_query(F.data == "toggle_all_groups")
async def toggle_all_groups(query: CallbackQuery, state: FSMContext):
    if not is_admin(query):
        return

    with db() as conn:
        groups = conn.execute(
            "SELECT id, chat_ref, title FROM groups ORDER BY id"
        ).fetchall()

    data = await state.get_data()
    selected = set(data.get("multi_selected", []))
    all_ids = {g["id"] for g in groups}

    # If everything is already selected, clear it; otherwise select all.
    selected = set() if selected == all_ids else all_ids

    await state.update_data(multi_selected=list(selected))

    try:
        await query.message.edit_reply_markup(
            reply_markup=multi_group_keyboard(groups, selected)
        )
    except Exception:
        pass

    await query.answer()


@router.callback_query(F.data.startswith("toggle_group:"))
async def toggle_group(query: CallbackQuery, state: FSMContext):
    if not is_admin(query):
        return

    data = await state.get_data()
    selected = set(data.get("multi_selected", []))
    group_id = int(query.data.split(":", 1)[1])

    if group_id in selected:
        selected.discard(group_id)
    else:
        selected.add(group_id)

    await state.update_data(multi_selected=list(selected))

    with db() as conn:
        groups = conn.execute(
            "SELECT id, chat_ref, title FROM groups ORDER BY id"
        ).fetchall()

    try:
        await query.message.edit_reply_markup(
            reply_markup=multi_group_keyboard(groups, selected)
        )
    except Exception:
        pass

    await query.answer()


@router.callback_query(F.data == "multi_publish")
async def multi_publish(query: CallbackQuery, state: FSMContext):
    if not is_admin(query):
        return

    data = await state.get_data()
    text = data.get("multi_text")
    selected_ids = data.get("multi_selected", [])

    if not text or not selected_ids:
        await query.answer("Select at least one group first.", show_alert=True)
        return

    placeholders = ",".join("?" for _ in selected_ids)
    with db() as conn:
        groups = conn.execute(
            f"SELECT id, chat_ref, title FROM groups WHERE id IN ({placeholders})",
            selected_ids,
        ).fetchall()

    await state.clear()

    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await query.answer()
    await query.message.answer(f"📣 Publishing to {len(groups)} group(s)...")

    results = []
    for g in groups:
        try:
            await publish_as_personal_account(text, chat_ref=g["chat_ref"])
            results.append(f"✅ {g['title'] or g['chat_ref']}")
        except Exception as exc:
            logger.exception("Multi-group publish failed for %s", g["chat_ref"])
            results.append(f"❌ {g['title'] or g['chat_ref']}: {type(exc).__name__}")

        await asyncio.sleep(3)  # small delay between chats to reduce spam-flagging risk

    await query.message.answer(
        "📣 Multi-group publish results:\n\n" + "\n".join(results),
        reply_markup=main_menu(),
    )


@router.callback_query(F.data == "choose_multi_repeat")
async def choose_multi_repeat(query: CallbackQuery, state: FSMContext):
    if not is_admin(query):
        return

    data = await state.get_data()
    selected_ids = data.get("multi_selected", [])

    if not selected_ids:
        await query.answer("Select at least one group first.", show_alert=True)
        return

    await query.message.answer(
        f"🔁 Choose how often this post should repeat across your {len(selected_ids)} selected group(s):",
        reply_markup=interval_keyboard("multi_repeat"),
    )
    await query.answer()


@router.callback_query(F.data.startswith("multi_repeat:"))
async def save_multi_repeat(query: CallbackQuery, state: FSMContext):
    if not is_admin(query):
        return

    data = await state.get_data()
    text = data.get("multi_text")
    selected_ids = data.get("multi_selected", [])

    if not text or not selected_ids:
        await query.answer("Post expired or no groups selected. Start again.", show_alert=True)
        return

    minutes = int(query.data.split("multi_repeat:", 1)[1])

    placeholders = ",".join("?" for _ in selected_ids)
    with db() as conn:
        groups = conn.execute(
            f"SELECT id, chat_ref, title FROM groups WHERE id IN ({placeholders})",
            selected_ids,
        ).fetchall()

    if not groups:
        await query.answer("Selected groups no longer exist.", show_alert=True)
        return

    target_refs = [g["chat_ref"] for g in groups]
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO repeating_posts
                (text, interval_minutes, active, paused, created_at, chat_ref, target_refs)
            VALUES (?, ?, 1, 0, ?, NULL, ?)
            """,
            (text, minutes, now.isoformat(), json.dumps(target_refs)),
        )
        repeat_id = cursor.lastrowid

    scheduler.add_job(
        send_repeating_post,
        "interval",
        minutes=minutes,
        args=[repeat_id],
        id=f"repeat_{repeat_id}",
        replace_existing=True,
        next_run_time=now,
    )

    await state.clear()

    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    label = next((lbl for m, lbl in REPEAT_INTERVALS if m == minutes), f"{minutes} min")
    names = ", ".join(g["title"] or g["chat_ref"] for g in groups)
    await query.message.answer(
        f"✅ Repeating post #{repeat_id} created — publishing now, then every {label} "
        f"to {len(groups)} group(s): {names}.\n\n"
        "Use 🔄 Repeating Posts to pause, resume, or delete it later.",
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
            SELECT id, run_at, text, chat_ref
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
            f"#{row['id']} • {dt.strftime('%d %b %H:%M')} → {group_label(row['chat_ref'])}\n{preview}"
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
        "📝 New Post — format, preview, publish, schedule, or repeat "
        "(targets your currently selected group).\n"
        "⚡ Quick Publish — publish immediately to the selected group.\n"
        "📣 Multi Group Post — publish one post to several groups at once.\n"
        "➕ Add Group — register a group/channel by username, link, or ID.\n"
        "📋 My Groups — select your active group or remove one from the list.\n"
        "📅 Scheduled Posts — view pending one-time posts.\n"
        "🗑 Delete Post — delete pending one-time posts.\n"
        "🔄 Repeating Posts — pause, resume, or delete posts that repeat on an interval.\n"
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

    # Preload every chat this account is a member of so that Telethon
    # caches the access_hash for each one. Without this, TARGET_CHAT
    # set to a raw numeric ID (e.g. -1001234567890) fails with:
    #   ValueError: Cannot find any entity corresponding to "..."
    # because StringSession doesn't persist entity access_hashes across
    # restarts. Usernames/invite links don't need this, but it's kept
    # here so numeric IDs work too.
    try:
        dialogs = await user_client.get_dialogs()
        logger.info("Preloaded %d chats for entity resolution.", len(dialogs))
    except Exception:
        logger.exception("Failed to preload dialogs (entity cache).")

    load_jobs()
    load_repeating_jobs()
    scheduler.start()

    logger.info("Control bot started. Timezone: %s", TIMEZONE)

    try:
        await dp.start_polling(bot)
    finally:
        await user_client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
