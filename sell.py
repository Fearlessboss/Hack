"""
============================================================
  ZUDO ACCOUNT SELLER BOT  (v3 — SESSION-SAFE)
  Virtual Telegram Account Selling Bot
============================================================
What's new in v3:
 - 100% in-memory StringSession used for login flow → no
   .session files are written to disk during OTP / sign-in.
   This kills the "no such column: version" /
   "database is locked" / "rm -rf sessions/*.session" pain
   FOREVER. You never need to delete anything to run again.
 - When we DO need to persist a session for owner re-use
   (fetch OTP later), we save it as a UNIQUE file inside
   `account_sessions/` with the name:
       <phone-digits>_<utc-timestamp>_<rand>.session
   so a new submission of the same number (after refund /
   retry) will NEVER collide with an older file.
 - The string-session text is also stored inside bot_data.json
   so even if the .session file is missing the owner can
   still operate the account via StringSession.
 - All previous v2 fixes preserved (retry flow, pyrogram
   string export, 2FA separate state, etc.)

Required packages:
    pip install python-telegram-bot==20.7 telethon==1.36.0 pyrogram==2.0.106 tgcrypto

Run:
    python account_seller_bot.py
============================================================
"""

import asyncio
import json
import logging
import os
import re
import secrets
import struct
from base64 import urlsafe_b64encode
from datetime import datetime
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from telethon import TelegramClient
from telethon.errors import (
    PhoneNumberInvalidError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    SessionPasswordNeededError,
    FloodWaitError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest

# =======================================================
#   CONFIGURATION
# =======================================================
BOT_TOKEN   = "8645471643:AAE3Ms7s7j6MJJ4d21JGD4a_FMQ5A-U7_1g"
API_ID      = 33628258
API_HASH    = "0850762925b9c1715b9b122f7b753128"
OWNER_ID    = 7661825494

DATA_FILE        = "bot_data.json"

# NEW dedicated folder for persisted account sessions.
# The OLD `sessions/` folder is intentionally NOT touched, so
# you can keep whatever is already there without deleting it.
SESSIONS_DIR     = "account_sessions"
Path(SESSIONS_DIR).mkdir(exist_ok=True)

# Conversation states
(
    SELL_COUNTRY,
    SELL_PRICE,
    SELL_PHONE,
    SELL_OTP,
    SELL_2FA,
    WITHDRAW_AMOUNT,
    WITHDRAW_UPI,
    OWNER_LOGIN_PHONE,
) = range(8)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ZudoBot")

# =======================================================
#   PERSISTENT JSON DATABASE
# =======================================================
DEFAULT_DB = {
    "users": {},
    "accounts": {},
    "withdrawals": {},
    "pending_retries": {},
    "counter": 0,
}

def load_db() -> dict:
    data = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    for k, v in DEFAULT_DB.items():
        if k not in data:
            data[k] = v if not isinstance(v, (dict, list)) else (v.copy() if isinstance(v, dict) else list(v))
    return data

def save_db(db: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

DB = load_db()
save_db(DB)

def get_user(uid: int) -> dict:
    uid = str(uid)
    if uid not in DB["users"]:
        DB["users"][uid] = {
            "balance": 0.0,
            "sold": 0,
            "history": [],
            "upi": "",
        }
        save_db(DB)
    return DB["users"][uid]

# =======================================================
#   UNIQUE SESSION FILENAME HELPER
#   Produces a fresh, never-colliding path every time.
#   We never overwrite or delete existing files — old
#   sessions stay safely in `account_sessions/`.
# =======================================================
def make_unique_session_path(phone: str) -> str:
    digits = re.sub(r"\D", "", phone) or "unknown"
    stamp  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    rand   = secrets.token_hex(3)        # 6 hex chars
    return os.path.join(SESSIONS_DIR, f"{digits}_{stamp}_{rand}.session")

# =======================================================
#   PENDING SUBMISSION CACHE (in-memory)
# =======================================================
PENDING: dict = {}

# =======================================================
#   PYROGRAM STRING SESSION GENERATOR
# =======================================================
_PYRO_STRUCT = ">BI?256sQ?"

def telethon_to_pyrogram_string(client: TelegramClient, user_id: int, is_bot: bool = False) -> str:
    try:
        dc_id     = client.session.dc_id
        auth_key  = client.session.auth_key.key
        packed    = struct.pack(
            _PYRO_STRUCT,
            dc_id,
            API_ID,
            False,
            auth_key,
            user_id,
            is_bot,
        )
        return urlsafe_b64encode(packed).decode("ascii").rstrip("=")
    except Exception as e:
        logger.warning(f"pyrogram string gen failed: {e}")
        return ""

# =======================================================
#   UI / KEYBOARDS
# =======================================================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Sell Account",     callback_data="sell")],
        [InlineKeyboardButton("💰 Balance",          callback_data="balance"),
         InlineKeyboardButton("🏧 Withdraw",         callback_data="withdraw")],
        [InlineKeyboardButton("📜 History",          callback_data="history"),
         InlineKeyboardButton("ℹ️ Help",             callback_data="help")],
        [InlineKeyboardButton("📢 Check OTP Status", url="https://t.me/zudootpbot")],
    ])

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="back")]])

def retry_kb(phone: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Retry Request", callback_data=f"retry:{phone}")],
        [InlineKeyboardButton("❌ Cancel",        callback_data=f"abort:{phone}")],
    ])

# =======================================================
#   /start
# =======================================================
WELCOME_TEXT = (
    "👋 <b>Welcome to ZUDO Account Seller Bot</b>\n\n"
    "💼 <b>What is this bot?</b>\n"
    "This bot allows you to <b>sell your virtual Telegram accounts</b> "
    "safely and get paid directly to your <b>UPI</b>.\n\n"
    "🛒 <b>How it works:</b>\n"
    "1️⃣  Tap <b>💸 Sell Account</b>\n"
    "2️⃣  Choose your account's <b>country</b>\n"
    "3️⃣  Enter the <b>price</b> you want\n"
    "4️⃣  Send the <b>phone number</b> (with country code)\n"
    "5️⃣  Enter the <b>OTP</b> you receive on Telegram\n"
    "6️⃣  All other sessions will be logged out automatically\n"
    "7️⃣  Once verified, the amount is added to your balance\n"
    "8️⃣  Withdraw via <b>UPI</b> any time using /withdraw\n\n"
    "📢 Check your account OTP status here → @zudootpbot\n\n"
    "⚠️ <i>Do not log back into the account after submitting. "
    "Doing so will reverse the sale.</i>\n\n"
    "👇 <b>Choose an option below to begin:</b>"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_user(update.effective_user.id)
    await update.message.reply_text(
        WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb()
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>ℹ️ Help & Commands</b>\n\n"
        "/start — Open main menu\n"
        "/sell — Start selling an account\n"
        "/balance — Check your balance\n"
        "/withdraw — Withdraw to UPI\n"
        "/history — View your sales\n"
        "/cancel — Cancel ongoing action\n\n"
        "💬 Support: contact the owner if any issue arises."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())

# =======================================================
#   MENU CALLBACKS
# =======================================================
async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "back":
        await q.message.edit_text(
            WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb()
        )
        return ConversationHandler.END

    if data == "cancel":
        cli = context.user_data.get("client")
        if cli:
            try: await cli.disconnect()
            except: pass
        context.user_data.clear()
        await q.message.edit_text(
            "❌ Action cancelled.\n\nReturning to main menu…",
            reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END

    if data == "balance":
        u = get_user(q.from_user.id)
        text = (
            f"💰 <b>Your Wallet</b>\n\n"
            f"Balance: <b>₹{u['balance']:.2f}</b>\n"
            f"Total accounts sold: <b>{u['sold']}</b>\n"
            f"UPI on file: <code>{u['upi'] or 'Not set'}</code>"
        )
        await q.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    if data == "history":
        u = get_user(q.from_user.id)
        if not u["history"]:
            text = "📜 <b>History</b>\n\nNo records yet."
        else:
            lines = ["📜 <b>Recent Activity</b>\n"]
            for h in u["history"][-10:][::-1]:
                lines.append(f"• {h}")
            text = "\n".join(lines)
        await q.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    if data == "help":
        text = (
            "<b>ℹ️ How To Use</b>\n\n"
            "• Tap <b>Sell Account</b> to begin a sale\n"
            "• Provide country, price, phone, OTP\n"
            "• Bot logs out all other sessions automatically\n"
            "• On success your balance is credited shortly after verification\n"
            "• Use <b>Withdraw</b> to get UPI payout\n\n"
            "📢 Check OTP status anytime → @zudootpbot"
        )
        await q.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

# =======================================================
#   SELL  FLOW
# =======================================================
COUNTRY_PRESETS = [
    ("🇮🇳 India (+91)",       "+91"),
    ("🇺🇸 USA (+1)",          "+1"),
    ("🇬🇧 UK (+44)",          "+44"),
    ("🇮🇩 Indonesia (+62)",   "+62"),
    ("🇵🇭 Philippines (+63)", "+63"),
    ("🇳🇬 Nigeria (+234)",    "+234"),
    ("🇵🇰 Pakistan (+92)",    "+92"),
    ("🇧🇩 Bangladesh (+880)", "+880"),
    ("🌍 Other",              "other"),
]

def country_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for label, code in COUNTRY_PRESETS:
        row.append(InlineKeyboardButton(label, callback_data=f"ctry:{code}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

async def sell_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        await q.message.edit_text(
            "🌍 <b>Step 1 / 4 — Select Country</b>\n\n"
            "Choose the country of the account you want to sell:",
            parse_mode=ParseMode.HTML, reply_markup=country_kb(),
        )
    else:
        await update.message.reply_text(
            "🌍 <b>Step 1 / 4 — Select Country</b>\n\n"
            "Choose the country of the account you want to sell:",
            parse_mode=ParseMode.HTML, reply_markup=country_kb(),
        )
    return SELL_COUNTRY

async def sell_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "cancel":
        await q.message.edit_text("❌ Cancelled.", reply_markup=main_menu_kb())
        return ConversationHandler.END

    code = q.data.split(":", 1)[1]
    if code == "other":
        context.user_data["country"] = "Other"
        await q.message.edit_text(
            "🌍 You selected <b>Other</b>.\n\n"
            "Type the country name (e.g. <code>Germany</code>):",
            parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
        )
        context.user_data["awaiting_country_text"] = True
        return SELL_COUNTRY

    context.user_data["country"] = code
    await q.message.edit_text(
        f"✅ Country: <b>{code}</b>\n\n"
        f"💵 <b>Step 2 / 4 — Enter Price</b>\n"
        f"Send the price you want (in ₹). Example: <code>250</code>",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
    )
    return SELL_PRICE

async def sell_country_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_country_text"):
        context.user_data["country"] = update.message.text.strip()[:30]
        context.user_data.pop("awaiting_country_text", None)
        await update.message.reply_text(
            f"✅ Country: <b>{context.user_data['country']}</b>\n\n"
            f"💵 <b>Step 2 / 4 — Enter Price</b>\n"
            f"Send the price you want (in ₹). Example: <code>250</code>",
            parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
        )
        return SELL_PRICE
    return SELL_COUNTRY

async def sell_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().replace("₹", "").replace(",", "")
    try:
        price = float(txt)
        if price <= 0 or price > 100000:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Invalid price. Please send a number like <code>250</code>",
            parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
        )
        return SELL_PRICE

    context.user_data["price"] = price
    await update.message.reply_text(
        f"✅ Price: <b>₹{price:.2f}</b>\n\n"
        f"📱 <b>Step 3 / 4 — Phone Number</b>\n"
        f"Send the full phone number with country code.\n"
        f"Example: <code>+919876543210</code>",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
    )
    return SELL_PHONE

async def sell_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip().replace(" ", "").replace("-", "")
    if not re.match(r"^\+?\d{7,15}$", phone):
        await update.message.reply_text(
            "⚠️ Invalid phone number. Format: <code>+919876543210</code>",
            parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
        )
        return SELL_PHONE
    if not phone.startswith("+"):
        phone = "+" + phone

    if phone in DB["accounts"]:
        await update.message.reply_text(
            "❌ This number has already been submitted in our system.",
            reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END

    # Clean up any orphan pending submission for same phone (memory only)
    if phone in PENDING:
        old = PENDING.pop(phone)
        try: await old["client"].disconnect()
        except: pass

    context.user_data["phone"] = phone
    await update.message.reply_text("⏳ Sending OTP… please wait.")

    # -----------------------------------------------------
    # IMPORTANT: We use a pure in-memory StringSession.
    # NO .session file is created on disk during login.
    # This means you NEVER need to delete anything from
    # sessions/ to retry — every run is fresh & safe.
    # -----------------------------------------------------
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        context.user_data["client"] = client
        context.user_data["phone_code_hash"] = sent.phone_code_hash

        await update.message.reply_text(
            "📲 <b>Step 4 / 4 — Enter OTP</b>\n\n"
            "An OTP has been sent to your Telegram app.\n"
            "Type the code with spaces between digits.\n"
            "Example: if OTP is <code>12345</code>, send <code>1 2 3 4 5</code>\n\n"
            "<i>(Spaces are required to avoid Telegram auto-invalidating the code.)</i>",
            parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
        )
        return SELL_OTP
    except PhoneNumberInvalidError:
        try: await client.disconnect()
        except: pass
        await update.message.reply_text("❌ Invalid phone number.", reply_markup=main_menu_kb())
        return ConversationHandler.END
    except FloodWaitError as e:
        try: await client.disconnect()
        except: pass
        await update.message.reply_text(
            f"⏳ Telegram flood-wait: please try again in {e.seconds} seconds.",
            reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END
    except Exception as e:
        try: await client.disconnect()
        except: pass
        logger.exception("send_code error")
        await update.message.reply_text(
            f"❌ Error sending OTP: <code>{e}</code>",
            parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END

async def sell_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().replace(" ", "").replace("-", "")
    phone = context.user_data.get("phone")
    client: TelegramClient = context.user_data.get("client")
    phone_code_hash = context.user_data.get("phone_code_hash")

    if not client or not phone:
        await update.message.reply_text("⚠️ Session expired. /start again.")
        return ConversationHandler.END

    await update.message.reply_text("🔐 Verifying OTP…")

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        return await attempt_finalize(update, context, client, password=None)

    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔒 This account has <b>2-Step Verification (2FA)</b> enabled.\n"
            "Please send your 2FA password now:",
            parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
        )
        return SELL_2FA

    except PhoneCodeInvalidError:
        await update.message.reply_text(
            "❌ Invalid OTP. Try again (with spaces between digits):",
            reply_markup=cancel_kb(),
        )
        return SELL_OTP
    except PhoneCodeExpiredError:
        try: await client.disconnect()
        except: pass
        context.user_data.clear()
        await update.message.reply_text(
            "❌ OTP expired. Please /sell again.", reply_markup=main_menu_kb()
        )
        return ConversationHandler.END
    except Exception as e:
        try: await client.disconnect()
        except: pass
        context.user_data.clear()
        logger.exception("sign_in error")
        await update.message.reply_text(
            f"❌ Login failed: <code>{e}</code>",
            parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END

async def sell_2fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    client: TelegramClient = context.user_data.get("client")
    if not client:
        await update.message.reply_text("⚠️ Session expired. /start again.")
        return ConversationHandler.END
    try:
        await client.sign_in(password=password)
        return await attempt_finalize(update, context, client, password=password)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Wrong 2FA password: <code>{e}</code>\nTry again:",
            parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
        )
        return SELL_2FA

# =======================================================
#   ATTEMPT_FINALIZE
# =======================================================
async def attempt_finalize(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           client: TelegramClient, password):
    phone   = context.user_data["phone"]
    country = context.user_data["country"]
    price   = context.user_data["price"]
    user    = update.effective_user

    try:
        me = await client.get_me()
    except Exception as e:
        logger.exception("get_me failed")
        try: await client.disconnect()
        except: pass
        context.user_data.clear()
        await update.message.reply_text(
            f"❌ Couldn't fetch account info: <code>{e}</code>",
            parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END

    PENDING[phone] = {
        "client":   client,
        "user":     user,
        "country":  country,
        "price":    price,
        "password": password,
        "me":       me,
        "chat_id":  update.effective_chat.id,
        "attempts": 0,
    }

    terminated, failed = await terminate_other_sessions(client)

    if failed:
        names = "\n".join(f"• <code>{n}</code>" for n in failed[:10])
        await update.message.reply_text(
            f"⚠️ <b>Submission paused — other sessions still active</b>\n\n"
            f"Couldn't automatically log out:\n{names}\n\n"
            f"👉 Open <b>Telegram → Settings → Devices</b> on this account "
            f"and tap <b>Terminate</b> on the remaining sessions.\n\n"
            f"Then press <b>🔄 Retry Request</b> below. The bot will re-check, "
            f"and your account will only be submitted when no other sessions remain.",
            parse_mode=ParseMode.HTML,
            reply_markup=retry_kb(phone),
        )
        context.user_data.clear()
        return ConversationHandler.END

    await do_submit(context, phone, terminated_count=terminated)
    context.user_data.clear()
    return ConversationHandler.END

async def terminate_other_sessions(client: TelegramClient):
    terminated = 0
    failed = []
    try:
        auths = await client(GetAuthorizationsRequest())
        for a in auths.authorizations:
            if a.current:
                continue
            try:
                await client(ResetAuthorizationRequest(hash=a.hash))
                terminated += 1
            except Exception as ex:
                label = f"{a.device_model or '?'} / {a.platform or '?'} / {a.app_name or '?'}"
                failed.append(f"{label}  ({ex})")
    except Exception as e:
        logger.warning(f"session enumeration failed: {e}")
        failed.append(f"(enumeration failed: {e})")
    return terminated, failed

# =======================================================
#   RETRY  /  ABORT  CALLBACKS
# =======================================================
async def retry_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Re-checking…")
    _, phone = q.data.split(":", 1)
    pend = PENDING.get(phone)
    if not pend:
        await q.message.edit_text(
            "⚠️ This submission has expired. Please /sell again.",
            reply_markup=main_menu_kb(),
        )
        return
    if pend["user"].id != q.from_user.id:
        await q.answer("Not your submission.", show_alert=True)
        return

    client = pend["client"]
    pend["attempts"] += 1

    terminated, failed = await terminate_other_sessions(client)

    if failed:
        names = "\n".join(f"• <code>{n}</code>" for n in failed[:10])
        await q.message.edit_text(
            f"⚠️ <b>Still found active sessions ({len(failed)})</b>\n\n"
            f"{names}\n\n"
            f"Please open <b>Telegram → Settings → Devices</b> and "
            f"<b>Terminate</b> them manually, then press 🔄 Retry Request again.\n\n"
            f"<i>Attempt #{pend['attempts']}</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=retry_kb(phone),
        )
        return

    await q.message.edit_text(
        "✅ All other sessions are now terminated.\nSubmitting your account…",
        parse_mode=ParseMode.HTML,
    )
    await do_submit(context, phone, terminated_count=terminated)

async def abort_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, phone = q.data.split(":", 1)
    pend = PENDING.pop(phone, None)
    if pend:
        try: await pend["client"].disconnect()
        except: pass
    await q.message.edit_text(
        "❌ Submission cancelled. Your account was <b>NOT</b> submitted.",
        parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(),
    )

# =======================================================
#   DO_SUBMIT — actual save + owner notify
# =======================================================
async def do_submit(context: ContextTypes.DEFAULT_TYPE, phone: str, terminated_count: int):
    pend = PENDING.pop(phone, None)
    if not pend:
        return
    client   = pend["client"]
    user     = pend["user"]
    country  = pend["country"]
    price    = pend["price"]
    password = pend["password"]
    me       = pend["me"]
    chat_id  = pend["chat_id"]

    # Build string sessions BEFORE disconnect
    telethon_string = StringSession.save(client.session)
    pyro_string     = telethon_to_pyrogram_string(client, me.id, is_bot=False)

    # ---------------------------------------------------
    # OPTIONAL: also persist as a unique .session file
    # in account_sessions/ for backup/manual access.
    # Filename guaranteed unique (timestamp + random hex)
    # so it never clashes with anything that already
    # exists on disk — no deletion ever required.
    # ---------------------------------------------------
    session_file_path = ""
    try:
        unique_path = make_unique_session_path(phone)
        # Write a tiny companion .txt with the string session
        # (safer than copying SQLite — works across any host)
        txt_path = unique_path.replace(".session", ".txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"phone={phone}\n")
            f.write(f"telegram_id={me.id}\n")
            f.write(f"telethon_string={telethon_string}\n")
            f.write(f"pyrogram_string={pyro_string}\n")
        session_file_path = txt_path
    except Exception as e:
        logger.warning(f"could not write backup session file: {e}")

    # Disconnect underlying client
    try: await client.disconnect()
    except: pass

    DB["accounts"][phone] = {
        "seller_id":         user.id,
        "seller_name":       user.full_name,
        "seller_username":   user.username or "",
        "country":           country,
        "price":             price,
        "string_session":    telethon_string,
        "telethon_string":   telethon_string,
        "pyrogram_string":   pyro_string,
        "password":          password or "",
        "telegram_id":       me.id,
        "first_name":        me.first_name or "",
        "username":          me.username or "",
        "status":            "pending_verification",
        "ts":                datetime.utcnow().isoformat(),
        "sessions_terminated": terminated_count,
        "session_file":      session_file_path,
    }
    u = get_user(user.id)
    u["history"].append(
        f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} | Submitted {phone} ({country}) for ₹{price:.2f}"
    )
    save_db(DB)

    try:
        await context.bot.send_message(
            chat_id,
            f"✅ <b>Account submitted successfully!</b>\n\n"
            f"📱 Number: <code>{phone}</code>\n"
            f"🌍 Country: <b>{country}</b>\n"
            f"💵 Price: <b>₹{price:.2f}</b>\n"
            f"🔒 2FA: <b>{'Yes' if password else 'No'}</b>\n"
            f"🚪 Other sessions terminated: <b>{terminated_count}</b>\n\n"
            f"⏳ Your balance of <b>₹{price:.2f}</b> will be released "
            f"after verification (usually a few minutes).\n\n"
            f"📢 Monitor your account's OTP status here:\n"
            f"👉 @zudootpbot\n\n"
            f"💰 Once verified you can <b>/withdraw</b> via UPI.",
            parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(),
        )
    except Exception as e:
        logger.warning(f"notify seller failed: {e}")

    owner_text = (
        f"🆕 <b>NEW ACCOUNT RECEIVED</b>\n\n"
        f"👤 Seller: <a href='tg://user?id={user.id}'>{user.full_name}</a>"
        f" (@{user.username or '—'}) <code>{user.id}</code>\n"
        f"📱 Phone: <code>{phone}</code>\n"
        f"🌍 Country: <b>{country}</b>\n"
        f"💵 Price: <b>₹{price:.2f}</b>\n"
        f"🔒 2FA password: <code>{password or 'None'}</code>\n"
        f"🆔 TG ID: <code>{me.id}</code>\n"
        f"👤 Name: {me.first_name or ''}  @{me.username or '—'}\n"
        f"🚪 Sessions terminated: <b>{terminated_count}</b>\n\n"
        f"🧷 <b>Pyrogram String Session:</b>\n<code>{pyro_string or '(generation failed)'}</code>"
    )
    owner_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve & Credit",  callback_data=f"approve:{phone}"),
         InlineKeyboardButton("❌ Reject",           callback_data=f"reject:{phone}")],
        [InlineKeyboardButton("📥 Fetch Last OTP",    callback_data=f"fetchotp:{phone}")],
    ])
    try:
        await context.bot.send_message(
            OWNER_ID, owner_text, parse_mode=ParseMode.HTML,
            reply_markup=owner_kb, disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Failed to notify owner: {e}")

# =======================================================
#   OWNER  — APPROVE / REJECT / FETCH OTP
# =======================================================
async def owner_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("Not authorised.", show_alert=True); return
    await q.answer()
    action, phone = q.data.split(":", 1)
    acc = DB["accounts"].get(phone)
    if not acc:
        await q.message.reply_text("Account not found in DB.")
        return

    if action == "approve":
        acc["status"] = "sold"
        seller = get_user(acc["seller_id"])
        seller["balance"] += acc["price"]
        seller["sold"]    += 1
        seller["history"].append(
            f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} | ✅ Sold {phone} +₹{acc['price']:.2f}"
        )
        save_db(DB)
        try:
            await q.message.edit_text(
                q.message.text_html + "\n\n✅ <b>Approved & credited.</b>",
                parse_mode=ParseMode.HTML,
            )
        except: pass
        try:
            await context.bot.send_message(
                acc["seller_id"],
                f"🎉 Your account <code>{phone}</code> has been <b>verified & sold</b>!\n"
                f"💰 ₹{acc['price']:.2f} has been credited to your balance.\n\n"
                f"Use /withdraw to cash out.",
                parse_mode=ParseMode.HTML,
            )
        except: pass

    elif action == "reject":
        acc["status"] = "rejected"
        save_db(DB)
        try:
            await q.message.edit_text(
                q.message.text_html + "\n\n❌ <b>Rejected.</b>",
                parse_mode=ParseMode.HTML,
            )
        except: pass
        try:
            await context.bot.send_message(
                acc["seller_id"],
                f"❌ Your submission <code>{phone}</code> was <b>rejected</b>.\n"
                f"Please contact support if you believe this is a mistake.",
                parse_mode=ParseMode.HTML,
            )
        except: pass

    elif action == "fetchotp":
        await q.message.reply_text(
            f"⏳ Fetching last OTP for <code>{phone}</code>…",
            parse_mode=ParseMode.HTML,
        )
        ts = acc.get("telethon_string") or acc.get("string_session", "")
        otp_text = await fetch_last_otp(ts)
        await q.message.reply_text(otp_text, parse_mode=ParseMode.HTML)

async def fetch_last_otp(string_session: str) -> str:
    if not string_session:
        return "⚠️ No string session stored for this account."
    # Pure in-memory — no disk file is created here either.
    client = TelegramClient(StringSession(string_session), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return "⚠️ Session no longer authorised (account may have logged us out)."
        msgs = await client.get_messages(777000, limit=1)
        if not msgs:
            return "ℹ️ No messages from 777000 yet."
        m = msgs[0]
        text = m.message or "(no text)"
        m_code = re.search(r"(\d{5,7})", text)
        code = m_code.group(1) if m_code else "—"
        return (
            f"📩 <b>Latest message from Telegram (777000)</b>\n\n"
            f"🔑 Extracted code: <code>{code}</code>\n"
            f"🕒 Date: {m.date}\n\n"
            f"<b>Full message:</b>\n<pre>{text[:1500]}</pre>"
        )
    except Exception as e:
        return f"❌ Error: <code>{e}</code>"
    finally:
        try: await client.disconnect()
        except: pass

# =======================================================
#   OWNER  /login
# =======================================================
async def owner_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Not authorised.")
        return ConversationHandler.END
    await update.message.reply_text(
        "🔐 <b>Owner Login Panel</b>\n\n"
        "Send the phone number of the account you want to access:\n"
        "Example: <code>+919876543210</code>",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
    )
    return OWNER_LOGIN_PHONE

async def owner_login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not phone.startswith("+"):
        phone = "+" + phone
    acc = DB["accounts"].get(phone)
    if not acc:
        await update.message.reply_text("❌ No such account in DB.")
        return ConversationHandler.END

    text = (
        f"🔑 <b>Account access — <code>{phone}</code></b>\n\n"
        f"🌍 Country: <b>{acc['country']}</b>\n"
        f"💵 Price: ₹{acc['price']:.2f}\n"
        f"🔒 2FA: <code>{acc.get('password') or 'None'}</code>\n"
        f"📊 Status: <b>{acc['status']}</b>\n\n"
        f"<b>Pyrogram string:</b>\n<code>{acc.get('pyrogram_string','(none)')}</code>\n\n"
        f"<b>Telethon string:</b>\n<code>{acc.get('telethon_string') or acc.get('string_session','')}</code>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Fetch Last OTP", callback_data=f"fetchotp:{phone}")],
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    return ConversationHandler.END

# =======================================================
#   BALANCE / WITHDRAW
# =======================================================
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    await update.message.reply_text(
        f"💰 <b>Balance:</b> ₹{u['balance']:.2f}\n"
        f"📦 Accounts sold: <b>{u['sold']}</b>\n"
        f"💳 UPI: <code>{u['upi'] or 'Not set'}</code>",
        parse_mode=ParseMode.HTML, reply_markup=back_kb(),
    )

async def withdraw_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        send = q.message.edit_text
    else:
        send = update.message.reply_text

    u = get_user(update.effective_user.id)
    if u["balance"] <= 0:
        await send("❌ You have no balance to withdraw.", reply_markup=back_kb())
        return ConversationHandler.END

    await send(
        f"🏧 <b>Withdraw Request</b>\n\n"
        f"Available balance: <b>₹{u['balance']:.2f}</b>\n\n"
        f"Enter the amount you want to withdraw (minimum ₹50):",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
    )
    return WITHDRAW_AMOUNT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text.strip().replace("₹", "").replace(",", ""))
    except ValueError:
        await update.message.reply_text("⚠️ Invalid number.", reply_markup=cancel_kb())
        return WITHDRAW_AMOUNT

    u = get_user(update.effective_user.id)
    if amt < 50:
        await update.message.reply_text("⚠️ Minimum withdraw is ₹50.", reply_markup=cancel_kb())
        return WITHDRAW_AMOUNT
    if amt > u["balance"]:
        await update.message.reply_text(
            f"⚠️ Insufficient balance. You have ₹{u['balance']:.2f}.",
            reply_markup=cancel_kb(),
        )
        return WITHDRAW_AMOUNT

    context.user_data["w_amount"] = amt
    await update.message.reply_text(
        "💳 Send your <b>UPI ID</b> (e.g. <code>name@upi</code>):",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
    )
    return WITHDRAW_UPI

async def withdraw_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upi = update.message.text.strip()
    if "@" not in upi or len(upi) < 5:
        await update.message.reply_text("⚠️ Invalid UPI ID.", reply_markup=cancel_kb())
        return WITHDRAW_UPI

    user = update.effective_user
    u = get_user(user.id)
    amt = context.user_data["w_amount"]
    u["balance"] -= amt
    u["upi"] = upi

    DB["counter"] = DB.get("counter", 0) + 1
    wid = f"W{DB['counter']:05d}"
    DB.setdefault("withdrawals", {})
    DB["withdrawals"][wid] = {
        "user_id": user.id, "amount": amt, "upi": upi,
        "status": "pending", "ts": datetime.utcnow().isoformat(),
    }
    u["history"].append(
        f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} | 🏧 Withdraw {wid} ₹{amt:.2f} → {upi}"
    )
    save_db(DB)

    await update.message.reply_text(
        f"✅ <b>Withdraw request placed</b>\n\n"
        f"🆔 ID: <code>{wid}</code>\n"
        f"💵 Amount: ₹{amt:.2f}\n"
        f"💳 UPI: <code>{upi}</code>\n\n"
        f"You'll be notified once the owner marks it as paid.",
        parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(),
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Payment Done", callback_data=f"paid:{wid}"),
         InlineKeyboardButton("❌ Reject",      callback_data=f"wreject:{wid}")],
    ])
    try:
        await context.bot.send_message(
            OWNER_ID,
            f"🏧 <b>WITHDRAW REQUEST</b>\n\n"
            f"🆔 <code>{wid}</code>\n"
            f"👤 <a href='tg://user?id={user.id}'>{user.full_name}</a> "
            f"(@{user.username or '—'}) <code>{user.id}</code>\n"
            f"💵 Amount: <b>₹{amt:.2f}</b>\n"
            f"💳 UPI: <code>{upi}</code>",
            parse_mode=ParseMode.HTML, reply_markup=kb,
        )
    except Exception as e:
        logger.error(f"owner notify err: {e}")
    context.user_data.clear()
    return ConversationHandler.END

async def owner_withdraw_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("Not authorised.", show_alert=True); return
    await q.answer()
    action, wid = q.data.split(":", 1)
    w = DB["withdrawals"].get(wid)
    if not w:
        await q.message.reply_text("Not found."); return

    if action == "paid":
        w["status"] = "paid"
        save_db(DB)
        try:
            await q.message.edit_text(
                q.message.text_html + "\n\n✅ <b>Marked as PAID.</b>",
                parse_mode=ParseMode.HTML,
            )
        except: pass
        try:
            await context.bot.send_message(
                w["user_id"],
                f"💸 <b>Payment Done!</b>\n\n"
                f"🆔 <code>{wid}</code>\n"
                f"💵 ₹{w['amount']:.2f} sent to <code>{w['upi']}</code>.\n\n"
                f"Thank you for using ZUDO! 🚀",
                parse_mode=ParseMode.HTML,
            )
        except: pass

    elif action == "wreject":
        w["status"] = "rejected"
        u = get_user(w["user_id"])
        u["balance"] += w["amount"]
        save_db(DB)
        try:
            await q.message.edit_text(
                q.message.text_html + "\n\n❌ <b>Rejected — refunded.</b>",
                parse_mode=ParseMode.HTML,
            )
        except: pass
        try:
            await context.bot.send_message(
                w["user_id"],
                f"❌ Your withdraw <code>{wid}</code> was rejected. "
                f"₹{w['amount']:.2f} has been refunded to your balance.",
                parse_mode=ParseMode.HTML,
            )
        except: pass

# =======================================================
#   CANCEL / HISTORY / STATS  COMMANDS
# =======================================================
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.user_data.get("client")
    if client:
        try: await client.disconnect()
        except: pass
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=main_menu_kb())
    return ConversationHandler.END

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    if not u["history"]:
        text = "📜 No history."
    else:
        text = "📜 <b>Recent activity</b>\n\n" + "\n".join(
            f"• {h}" for h in u["history"][-15:][::-1]
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    users   = len(DB["users"])
    accs    = len(DB["accounts"])
    sold    = sum(1 for a in DB["accounts"].values() if a["status"] == "sold")
    pending = sum(1 for a in DB["accounts"].values() if a["status"] == "pending_verification")
    wd_pend = sum(1 for w in DB["withdrawals"].values() if w["status"] == "pending")
    wd_paid = sum(w["amount"] for w in DB["withdrawals"].values() if w["status"] == "paid")
    await update.message.reply_text(
        f"📊 <b>Admin Stats</b>\n\n"
        f"👥 Users: <b>{users}</b>\n"
        f"📱 Accounts received: <b>{accs}</b>\n"
        f"   ✅ Sold: <b>{sold}</b>\n"
        f"   ⏳ Pending: <b>{pending}</b>\n"
        f"🏧 Withdrawals pending: <b>{wd_pend}</b>\n"
        f"💸 Total paid out: ₹<b>{wd_paid:.2f}</b>\n"
        f"📌 Awaiting retry: <b>{len(PENDING)}</b>",
        parse_mode=ParseMode.HTML,
    )

# =======================================================
#   APP BOOTSTRAP
# =======================================================
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    sell_conv = ConversationHandler(
        entry_points=[
            CommandHandler("sell", sell_entry),
            CallbackQueryHandler(sell_entry, pattern=r"^sell$"),
        ],
        states={
            SELL_COUNTRY: [
                CallbackQueryHandler(sell_country, pattern=r"^(ctry:.+|cancel)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sell_country_text),
            ],
            SELL_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sell_price),
                CallbackQueryHandler(menu_cb, pattern=r"^cancel$"),
            ],
            SELL_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sell_phone),
                CallbackQueryHandler(menu_cb, pattern=r"^cancel$"),
            ],
            SELL_OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sell_otp),
                CallbackQueryHandler(menu_cb, pattern=r"^cancel$"),
            ],
            SELL_2FA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sell_2fa_password),
                CallbackQueryHandler(menu_cb, pattern=r"^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
    )

    wd_conv = ConversationHandler(
        entry_points=[
            CommandHandler("withdraw", withdraw_entry),
            CallbackQueryHandler(withdraw_entry, pattern=r"^withdraw$"),
        ],
        states={
            WITHDRAW_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount),
                CallbackQueryHandler(menu_cb, pattern=r"^cancel$"),
            ],
            WITHDRAW_UPI: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_upi),
                CallbackQueryHandler(menu_cb, pattern=r"^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
    )

    own_conv = ConversationHandler(
        entry_points=[CommandHandler("login", owner_login)],
        states={
            OWNER_LOGIN_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, owner_login_phone),
                CallbackQueryHandler(menu_cb, pattern=r"^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
    )

    app.add_handler(sell_conv)
    app.add_handler(wd_conv)
    app.add_handler(own_conv)

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(CommandHandler("stats",   cmd_stats))

    app.add_handler(CallbackQueryHandler(retry_cb, pattern=r"^retry:"))
    app.add_handler(CallbackQueryHandler(abort_cb, pattern=r"^abort:"))

    app.add_handler(CallbackQueryHandler(owner_action_cb,   pattern=r"^(approve|reject|fetchotp):"))
    app.add_handler(CallbackQueryHandler(owner_withdraw_cb, pattern=r"^(paid|wreject):"))

    app.add_handler(CallbackQueryHandler(menu_cb))

    return app

def main():
    app = build_app()
    logger.info("🚀 ZUDO Account Seller Bot (v3 — session-safe) is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
