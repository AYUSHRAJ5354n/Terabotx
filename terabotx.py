# -*- coding: utf-8 -*-
# AYU — TeraBox Downloader Bot (single file, fixed)
# Requirements:
#   pip install pyrogram tgcrypto requests

import os
import re
import sys
import time
import json
import uuid
import math
import shutil
import sqlite3
import threading
import asyncio
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, RPCError, ChatAdminRequired, ChannelPrivate

# ========= USER CONFIG (EDIT THIS) =========
OWNER_ID = 1685470205           # <-- YOUR TELEGRAM ID
SUDO_IDS = []                  # Extra admins (example: [111,222])
BOT_TOKEN = "8343188725:AAFVsaUHOpIlq7V180W3VcDjDUhLFt2jfUM"
API_ID   = 28244492
API_HASH = "38e4ce53faea889073f6f49e83cbc392"

FORCE_SUB_CHANNEL = ""     # without @
UPDATES_GROUP_URL = "https://t.me/+S1AMHMx-PiM0ZGJl"
DUMP_CHANNEL_ID   = -1002560282913 # your dump/channel id
# ===========================================

MAX_RETRIES = 3
PUBLIC_MODE_DEFAULT = 0  # 0=private, 1=public
HISTORY_DAYS = 20
SPLIT_THRESHOLD = 2 * 1024 * 1024 * 1024 - (10 * 1024 * 1024)  # ≈1.99GB
MAX_FILE_SUPPORTED = 4 * 1024 * 1024 * 1024  # 4GB max
DOWNLOAD_DIR = "downloads"
DB_PATH = "ayubot.db"

app = Client(
    "ayu_terabox_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ============ DATABASE ============

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

DB = db()

def init_db():
    cur = DB.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        is_authorized INTEGER DEFAULT 0,
        first_seen TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS groups(
        chat_id INTEGER PRIMARY KEY,
        is_authorized INTEGER DEFAULT 0,
        title TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        val TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        link TEXT,
        title TEXT,
        size_bytes INTEGER,
        created_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS stats(
        key TEXT PRIMARY KEY,
        val INTEGER
    )""")
    cur.execute("INSERT OR IGNORE INTO settings(key,val) VALUES('public_mode', ?)",
                (str(PUBLIC_MODE_DEFAULT),))
    cur.execute("INSERT OR IGNORE INTO stats(key,val) VALUES('total_bytes', 0)")
    cur.execute("INSERT OR IGNORE INTO stats(key,val) VALUES('total_files', 0)")
    DB.commit()

init_db()

def set_setting(key, val):
    DB.execute("REPLACE INTO settings(key,val) VALUES(?,?)", (key, str(val)))
    DB.commit()

def get_setting(key, default=None):
    cur = DB.execute("SELECT val FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else default

def add_user(uid):
    DB.execute(
        "INSERT OR IGNORE INTO users(user_id, is_authorized, first_seen) VALUES(?,?,?)",
        (uid, 0, datetime.now(timezone.utc).isoformat())
    )
    DB.commit()

def set_user_auth(uid, val):
    DB.execute("UPDATE users SET is_authorized=? WHERE user_id=?", (1 if val else 0, uid))
    DB.commit()

def is_user_authorized(uid):
    if uid == OWNER_ID or uid in SUDO_IDS:
        return True
    cur = DB.execute("SELECT is_authorized FROM users WHERE user_id=?", (uid,))
    row = cur.fetchone()
    return bool(row and row[0] == 1)

def add_group(chat_id, title):
    DB.execute("INSERT OR IGNORE INTO groups(chat_id, is_authorized, title) VALUES(?,?,?)",
               (chat_id, 0, title or ""))
    DB.commit()

def set_group_auth(chat_id, val):
    DB.execute("UPDATE groups SET is_authorized=? WHERE chat_id=?", (1 if val else 0, chat_id))
    DB.commit()

def is_group_authorized(chat_id):
    cur = DB.execute("SELECT is_authorized FROM groups WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    return bool(row and row[0] == 1)

def bump_stats(bytes_added):
    DB.execute("UPDATE stats SET val = val + ? WHERE key='total_bytes'", (bytes_added,))
    DB.execute("UPDATE stats SET val = val + 1 WHERE key='total_files'")
    DB.commit()

def total_bytes():
    cur = DB.execute("SELECT val FROM stats WHERE key='total_bytes'")
    r = cur.fetchone()
    return int(r[0]) if r else 0

def total_files():
    cur = DB.execute("SELECT val FROM stats WHERE key='total_files'")
    r = cur.fetchone()
    return int(r[0]) if r else 0

def known_user_ids():
    cur = DB.execute("SELECT user_id FROM users")
    return [r[0] for r in cur.fetchall()]

def add_history(uid, link, title, size_bytes):
    DB.execute("""
        INSERT INTO history(user_id, link, title, size_bytes, created_at)
        VALUES(?,?,?,?,?)
    """, (uid, link, title, size_bytes, datetime.now(timezone.utc).isoformat()))
    DB.commit()

def recent_history(uid, days=HISTORY_DAYS, limit=50):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    cur = DB.execute(
        "SELECT title, link, size_bytes, created_at FROM history "
        "WHERE user_id=? AND datetime(created_at) >= datetime(?) "
        "ORDER BY id DESC LIMIT ?",
        (uid, since.isoformat(), limit)
    )
    return cur.fetchall()

# ============ ACCESS CONTROL / MODE / FSUB ============

def is_owner_or_sudo(uid):
    return uid == OWNER_ID or uid in SUDO_IDS

async def require_fsub(user_id: int) -> bool:
    """
    LIGHT-FSUB:
    ✅ If bot can verify → allow only if joined
    ✅ If verification fails (private channel / no admin rights) → allow anyway
    """
    try:
        member = await app.get_chat_member(f"@{FORCE_SUB_CHANNEL}", user_id)
        if member and member.status in ("member", "administrator", "creator"):
            return True
        return False
    except Exception:
        return True  # allow when cannot verify

def current_mode_public() -> bool:
    return get_setting("public_mode", "0") == "1"

# ============ TASK STATE & HELPERS ============

class Task:
    def __init__(self, user_id, chat_id, source_link):
        self.task_id = uuid.uuid4().hex[:8]
        self.user_id = user_id
        self.chat_id = chat_id
        self.source_link = source_link
        self.title = None
        self.size_str = None
        self.size_bytes = 0
        self.download_url = None
        self.filename = None
        self.temp_path = None
        self.stop_flag = False
        self.started_at = time.time()
        self.last_report = 0
        self.last_speed = 0.0
        self.part_paths = []

active_tasks = {}
user_active_task = {}

def human_bytes(n):
    if n is None:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    n = float(n)
    while n >= 1024 and i < len(units) - 1:
        n /= 1024; i += 1
    return f"{n:.2f} {units[i]}"

def sanitize_filename(name: str) -> str:
    name = (name or "").strip()
    name = name.replace("@BasicCode", "").replace("@BesicCode", "")
    invalid = r'<>:"/\\|?*'
    for ch in invalid:
        name = name.replace(ch, "_")
    name = re.sub(r"\s+", " ", name).strip()
    return name or "@AYU_BOTs"

def teradl_info(link: str):
    """returns {title,size,download} or None"""
    try:
        url = f"https://teradl.tiiny.io/?link={link}"
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        j = r.json()
        if "data" in j and j["data"]:
            d = j["data"][0]
            return {
                "title": d.get("title") or "",
                "size": d.get("size") or "",
                "download": d.get("download")
            }
    except Exception:
        return None
    return None

def parse_size_to_bytes(size_str: str) -> int:
    try:
        m = re.match(r"([\d\.]+)\s*([KMGTP]?B)", size_str, re.I)
        if not m:
            return 0
        val = float(m.group(1)); unit = m.group(2).upper()
        scale = {"B":1,"KB":1024,"MB":1024**2,"GB":1024**3,"TB":1024**4}.get(unit,1)
        return int(val*scale)
    except Exception:
        return 0

def split_file(path, part_size):
    """Returns list of part paths. Removes original after splitting."""
    parts = []
    size = os.path.getsize(path)
    if size <= part_size:
        return [path]
    root, ext = os.path.splitext(path)
    with open(path, "rb") as f:
        idx = 1
        while True:
            chunk = f.read(part_size)
            if not chunk:
                break
            part_path = f"{root}.part{idx}{ext}"
            with open(part_path, "wb") as p:
                p.write(chunk)
            parts.append(part_path)
            idx += 1
    os.remove(path)
    return parts

def range_supported(head_resp):
    cr = head_resp.headers.get("Accept-Ranges") or head_resp.headers.get("accept-ranges")
    return (cr or "").lower() == "bytes"

def quoted_status(text: str) -> str:
    """HTML pre block for quoted look"""
    return f"<pre>{text}</pre>"

def build_live_block(task: Task, downloaded, total):
    sp = human_bytes(getattr(task, "last_speed", 0)) + "/s" if getattr(task, "last_speed", 0) else "N/A"
    return (
        f"📥 Downloading\n\n"
        f"📄 {task.title}\n"
        f"📦 Size: {human_bytes(total)}\n"
        f"⚡ Speed: {sp}\n"
        f"📊 Progress: {human_bytes(downloaded)}/{human_bytes(total)}\n"
        f"🆔 Task ID: {task.task_id}\n"
        f"🚀 Will resume from where it left off"
    )

def build_retry_block(downloaded, total, tries):
    return (
        "⚠️ Connection Issue Detected\n\n"
        f"🔄 Retrying in 2 seconds... ({tries}/{MAX_RETRIES})\n"
        f"📊 Progress saved: {human_bytes(downloaded)}/{human_bytes(total)}\n"
    )

def build_done_block(task: Task):
    return (
        "✅ Download complete\n\n"
        f"📄 {task.title}\n"
        f"🆔 Task ID: {task.task_id}\n"
        "⏫ Uploading to Telegram..."
    )

def build_error_block(msg: str, task: Task | None = None):
    base = f"❌ {msg}"
    if task:
        base += f"\n🆔 Task ID: {task.task_id}"
    return base
    # ============ DOWNLOAD (RESUME + SPEED) ============

def download_with_resume(task: Task, status_cb):
    """
    Downloads with range-resume + retry.
    Calls status_cb(downloaded, total, retry_count, is_connection_issue)
    """
    url = task.download_url
    filename  = os.path.join(DOWNLOAD_DIR, task.filename)
    part_path = filename + ".part"
    task.temp_path = part_path

    # ===== GET REAL SIZE =====
    total = 0
    ranges_ok = True

    try:
        head = requests.head(url, allow_redirects=True, timeout=15)
        total = int(head.headers.get("content-length") or 0)
        ranges_ok = range_supported(head)
    except:
        pass

    # ensure fallback GET
    if total == 0:
        try:
            g = requests.get(url, stream=True, timeout=15)
            total = int(g.headers.get("content-length") or 0)
            g.close()
        except:
            pass

    # If still unknown, allow anyway
    if total > MAX_FILE_SUPPORTED:
        raise RuntimeError("File size exceeds 4GB limit")

    # ===== RESUME SUPPORT =====
    downloaded = 0
    if os.path.exists(part_path):
        downloaded = os.path.getsize(part_path)

    tries = 0
    last_time  = time.time()
    last_bytes = downloaded

    # ===== LOOP =====
    while tries < MAX_RETRIES and not task.stop_flag:
        try:
            hdr = {}
            if ranges_ok and downloaded > 0:
                hdr["Range"] = f"bytes={downloaded}-"

            with requests.get(url, stream=True, headers=hdr, timeout=30) as r:
                r.raise_for_status()
                mode = "ab" if hdr else "wb"
                with open(part_path, mode) as f:
                    for chunk in r.iter_content(chunk_size=256*1024):
                        if task.stop_flag:
                            break
                        if not chunk:
                            continue

                        f.write(chunk)
                        downloaded += len(chunk)

                        # === SPEED
                        now = time.time()
                        if now - last_time >= 2:
                            speed = (downloaded - last_bytes) / (now - last_time + 1e-6)
                            task.last_speed = speed
                            last_bytes = downloaded
                            last_time  = now
                            status_cb(downloaded, total, None, False)

            if task.stop_flag:
                raise RuntimeError("Task cancelled by user")

            # Completed
            if os.path.exists(part_path):
                os.rename(part_path, filename)

            final_size = total if total else os.path.getsize(filename)
            return filename, final_size

        except Exception:
            tries += 1
            status_cb(downloaded, total, tries, True)
            if tries >= MAX_RETRIES:
                break
            time.sleep(2)

    raise RuntimeError("Failed after retries")


# ============ ACCESS CONTROL ============

async def can_use_here(message: Message) -> bool:
    uid  = message.from_user.id if message.from_user else 0
    chat = message.chat

    # Store into DB
    if message.chat.type.name.lower() == "private":
        add_user(uid)
    else:
        add_group(chat.id, chat.title)

    # ✅ LIGHT-FSUB → only when sending a link
    if message.chat.type.name.lower() == "private":
        if message.text and message.text.startswith("http"):
            ok = await require_fsub(uid)
            if not ok:
                btn = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCE_SUB_CHANNEL}")],
                    [InlineKeyboardButton("👥 Join Group",  url=UPDATES_GROUP_URL)]
                ])
                await message.reply("🔒 Please join our channel to use this bot.", reply_markup=btn)
                return False

    # Public mode
    if current_mode_public():
        return True

    # Private mode
    if message.chat.type.name.lower() == "private":
        return is_user_authorized(uid) or is_owner_or_sudo(uid)
    else:
        return is_group_authorized(message.chat.id) or is_owner_or_sudo(uid)


# ============ UPLOAD ============

async def upload_result(task: Task, path: str, caption: str):
    """
    Split if >2GB. Upload parts. Dump to log channel.
    """
    size   = os.path.getsize(path)
    root   = os.path.splitext(os.path.basename(path))[0]
    ext    = os.path.splitext(path)[1] or ".bin"
    root   = root or "@AYU_BOTs"

    # Split
    parts = split_file(path, SPLIT_THRESHOLD) if size > SPLIT_THRESHOLD else [path]

    success = True
    part_i = 1

    for item in parts:
        filename = os.path.basename(item)

        # rename part if needed
        if len(parts) > 1 and ".part" not in filename:
            new_name = f"{root}.part{part_i}{ext}"
            new_path = os.path.join(os.path.dirname(item), new_name)
            os.rename(item, new_path)
            item     = new_path
            filename = new_name

        try:
            await app.send_document(
                task.chat_id,
                item,
                file_name=filename,
                caption=caption
            )

            # Dump
            try:
                dumpCap = (
                    f"👤 User: <code>{task.user_id}</code>\n"
                    f"🔗 Link: {task.source_link}\n"
                    f"📄 {task.title}"
                )
                await app.send_document(
                    DUMP_CHANNEL_ID,
                    item,
                    file_name=filename,
                    caption=dumpCap,
                    parse_mode=ParseMode.HTML
                )
            except:
                pass

        except FloodWait as e:
            time.sleep(e.value)
            await app.send_document(
                task.chat_id,
                item,
                file_name=filename,
                caption=caption
            )
        except:
            success = False
            await app.send_message(
                task.chat_id,
                quoted_status(build_error_block("Upload failed for some part", task)),
                parse_mode=ParseMode.HTML
            )

        # Clean
        try: os.remove(item)
        except: pass

        part_i += 1

    return success
    # ============ START UI ============

WELCOME_HTML = (
    "👋 <b>𝘏𝘦𝘭𝘭𝘰 Channel!</b>\n\n"
    "📥 𝘐'𝘮 𝘺𝘰𝘶𝘳 <b>TeraBox Downloader Bot</b>\n\n"
    "🎬 𝘑𝘶𝘴𝘵 𝘴𝘦𝘯𝘥 𝘮𝘦 𝘢𝘯𝘺 𝘷𝘢𝘭𝘪𝘥 𝘛𝘦𝘳𝘢𝘣𝘰𝘹 𝘭𝘪𝘯𝘬.\n\n"
    "📌 𝘖𝘯𝘭𝘺 𝘧𝘪𝘭𝘦𝘴 𝘶𝘯𝘥𝘦𝘳 <b>2GB</b> 𝘢𝘳𝘦 𝘶𝘱𝘭𝘰𝘢𝘥𝘦𝘥 𝘥𝘪𝘳𝘦𝘤𝘵𝘭𝘺; bigger ones are split automatically.\n"
    "⚡️ 𝘍𝘢𝘴𝘵, 𝘴𝘢𝘧𝘦 𝘢𝘯𝘥 𝘦𝘢𝘴𝘺 𝘵𝘰 𝘶𝘴𝘦.\n\n"
    "🔔 𝘑𝘰𝘪𝘯 𝘰𝘶𝘳 𝘤𝘩𝘢𝘯𝘯𝘦𝘭 𝘢𝘯𝘥 𝘨𝘳𝘰𝘶𝘱 𝘧𝘰𝘳 𝘶𝘱𝘥𝘢𝘵𝘦𝘴."
)

def start_buttons():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Channel", url=f"https://t.me/{FORCE_SUB_CHANNEL}"),
            InlineKeyboardButton("👥 Group",   url=UPDATES_GROUP_URL),
        ]
    ])

@app.on_message(filters.command("start"))
async def cmd_start(_, m: Message):
    if not await can_use_here(m):
        return
    await m.reply_text(
        WELCOME_HTML,
        parse_mode=ParseMode.HTML,
        reply_markup=start_buttons(),
        disable_web_page_preview=True
    )


# ============ PUBLIC / PRIVATE MODE ============

@app.on_message(filters.command("public"))
async def cmd_public(_, m: Message):
    if m.from_user and is_owner_or_sudo(m.from_user.id):
        set_setting("public_mode", "1")
        await m.reply("🌐 Bot mode set to: <b>PUBLIC</b>", parse_mode=ParseMode.HTML)
    else:
        await m.reply("🚫 Only owner / sudo.", quote=True)

@app.on_message(filters.command("private"))
async def cmd_private(_, m: Message):
    if m.from_user and is_owner_or_sudo(m.from_user.id):
        set_setting("public_mode", "0")
        await m.reply("🔒 Bot mode set to: <b>PRIVATE</b>", parse_mode=ParseMode.HTML)
    else:
        await m.reply("🚫 Only owner / sudo.", quote=True)


# ============ AUTHORIZATION ============

@app.on_message(filters.command(["az","authorize","Az"]))
async def cmd_az(_, m: Message):
    if not (m.from_user and is_owner_or_sudo(m.from_user.id)):
        return await m.reply("🚫 Only owner/sudo.")
    parts = m.text.strip().split()
    if len(parts) < 2:
        return await m.reply("Usage:\n<code>/az &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
    try:
        uid = int(parts[1])
        add_user(uid)
        set_user_auth(uid, 1)
        await m.reply(f"✅ Authorized user <code>{uid}</code>", parse_mode=ParseMode.HTML)
    except:
        await m.reply("❌ Invalid user ID.", quote=True)

@app.on_message(filters.command(["az_grp","authorize_group","Az_grp"]))
async def cmd_az_grp(_, m: Message):
    if not (m.from_user and is_owner_or_sudo(m.from_user.id)):
        return await m.reply("🚫 Only owner/sudo.")
    parts = m.text.strip().split()
    if len(parts) < 2:
        return await m.reply("Usage:\n<code>/az_grp &lt;chat_id&gt;</code>", parse_mode=ParseMode.HTML)
    try:
        cid = int(parts[1])
        add_group(cid, "")
        set_group_auth(cid, 1)
        await m.reply(f"✅ Authorized group <code>{cid}</code>", parse_mode=ParseMode.HTML)
    except:
        await m.reply("❌ Invalid chat ID.", quote=True)


# ============ BROADCAST ============

@app.on_message(filters.command("broadcast"))
async def cmd_broadcast(_, m: Message):
    if not (m.from_user and is_owner_or_sudo(m.from_user.id)):
        return await m.reply("🚫 Only owner/sudo.")
    txt = m.text.split(" ", 1)
    if len(txt) < 2:
        return await m.reply("Usage:\n<code>/broadcast your message</code>", parse_mode=ParseMode.HTML)

    message_to_send = txt[1]
    users = known_user_ids()
    sent = 0
    for uid in users:
        try:
            await app.send_message(uid, message_to_send)
            sent += 1
        except:
            pass
    await m.reply(f"✅ Broadcast sent to <b>{sent}</b> users.")


# ============ HISTORY ============

@app.on_message(filters.command("history"))
async def cmd_history(_, m: Message):
    if not await can_use_here(m):
        return

    uid = m.from_user.id
    rows = recent_history(uid, HISTORY_DAYS, 50)

    if not rows:
        return await m.reply("🗂 No downloads in the last 20 days.")

    lines = []
    for title, link, size_bytes, created_at in rows:
        lines.append(f"• <b>{sanitize_filename(title)}</b>\n  <code>{link}</code>")

    msg = "🕘 <b>Last 20 days — your download links:</b>\n\n" + "\n".join(lines[:50])
    await m.reply(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    # ============ STATS ============

@app.on_message(filters.command("stats"))
async def cmd_stats(_, m: Message):
    if not await can_use_here(m):
        return

    total_b = total_bytes()
    files   = total_files()

    # Disk
    try:
        du = shutil.disk_usage("/")
        disk = f"{human_bytes(du.free)} free / {human_bytes(du.total)} total"
    except:
        disk = "N/A"

    # Load
    try:
        load = os.getloadavg()
        load_txt = f"{load[0]:.2f}, {load[1]:.2f}, {load[2]:.2f}"
    except:
        load_txt = "N/A"

    mode = "PUBLIC ✅" if current_mode_public() else "PRIVATE 🔒"

    txt = (
        "📊 <b>Bot Stats</b>\n\n"
        f"• Total Files: <b>{files}</b>\n"
        f"• Total Downloaded: <b>{human_bytes(total_b)}</b>\n"
        f"• Disk: <b>{disk}</b>\n"
        f"• Load Avg: <b>{load_txt}</b>\n"
        f"• Mode: <b>{mode}</b>"
    )

    await m.reply(txt, parse_mode=ParseMode.HTML)


# ============ STATUS ============

@app.on_message(filters.command("status"))
async def cmd_status(_, m: Message):
    if not await can_use_here(m):
        return

    uid = m.from_user.id
    tid = user_active_task.get(uid)

    if not tid or tid not in active_tasks:
        return await m.reply(
            quoted_status("ℹ️ No active task."),
            parse_mode=ParseMode.HTML
        )

    task = active_tasks[tid]
    downloaded = 0

    if task.temp_path and os.path.exists(task.temp_path):
        downloaded = os.path.getsize(task.temp_path)

    text = build_live_block(task, downloaded, task.size_bytes or 0)
    msg  = quoted_status(text)

    # cancel OUTSIDE
    cancel = f"\n❌ /cancel_{task.task_id}"

    await m.reply(msg + cancel, parse_mode=ParseMode.HTML)


# ============ CANCEL ============

@app.on_message(filters.regex(r"^/cancel_[0-9a-fA-F]{8}$"))
async def cmd_cancel(_, m: Message):
    if not await can_use_here(m):
        return

    tid = m.text.split("_", 1)[1]
    task = active_tasks.get(tid)

    if not task:
        return await m.reply(
            quoted_status("ℹ️ Task not found."),
            parse_mode=ParseMode.HTML
        )

    if task.user_id != m.from_user.id and not is_owner_or_sudo(m.from_user.id):
        return await m.reply("🚫 You cannot cancel others' tasks.")

    task.stop_flag = True
    await m.reply(
        quoted_status("🛑 Cancel Requested"),
        parse_mode=ParseMode.HTML
    )


# ============ MAIN LINK HANDLER ============

@app.on_message(
    filters.text
    & ~filters.command([
        "start","stats","status","history",
        "public","private","az","authorize","Az",
        "az_grp","authorize_group","Az_grp","broadcast","help","about"
    ])
)
async def handle_link(_, m: Message):

    if not await can_use_here(m):
        return

    link = m.text.strip()
    if not (link.startswith("http://") or link.startswith("https://")):
        return await m.reply("❌ Send a valid TeraBox link.")

    uid = m.from_user.id if m.from_user else 0

    if uid in user_active_task:
        return await m.reply(
            quoted_status("⏳ You already have a task. Use /status"),
            parse_mode=ParseMode.HTML
        )

    info = teradl_info(link)
    if not info or not info.get("download"):
        return await m.reply(
            quoted_status("❌ Error in the link or file not found."),
            parse_mode=ParseMode.HTML
        )

    title     = sanitize_filename(info["title"] or "")
    size_str  = info["size"] or ""
    size_est  = parse_size_to_bytes(size_str)
    dl_url    = info["download"]

    # If no extension → get from URL
    if "." not in os.path.basename(title):
        ext = os.path.splitext(urlparse(dl_url).path)[1] or ".bin"
        title += ext

    task = Task(uid, m.chat.id, link)
    task.title       = title
    task.size_str    = size_str
    task.size_bytes  = size_est
    task.download_url = dl_url
    task.filename    = title

    active_tasks[task.task_id]    = task
    user_active_task[uid]         = task.task_id

    head = (
        "✅ Link Found!\n"
        f"📄 {task.title}\n"
        f"📦 {task.size_str or human_bytes(task.size_bytes)}\n"
        f"🆔 Task ID: {task.task_id}\n\n"
        "⏳ Starting download..."
    )

    status_msg = await m.reply(
        quoted_status(head) + f"\n❌ /cancel_{task.task_id}",
        parse_mode=ParseMode.HTML
    )
    # ===== STATUS CALLBACK (LIVE UPDATES) =====
    def status_cb(downloaded, total, retry_idx, is_connect_issue):
        if is_connect_issue and retry_idx:
            block = build_retry_block(downloaded, total, retry_idx)
            block += f"🆔 Task ID: {task.task_id}\n🚀 Will resume from where it left off"
            txt = quoted_status(block) + f"\n❌ /cancel_{task.task_id}"
        else:
            live = build_live_block(task, downloaded, total)
            txt  = quoted_status(live) + f"\n❌ /cancel_{task.task_id}"

        try:
            app.loop.create_task(
                status_msg.edit_text(
                    txt,
                    parse_mode=ParseMode.HTML
                )
            )
        except:
            pass


    # ====== BACKGROUND WORKER ======
    def worker():
        try:
            # DOWNLOAD
            path, real_size = download_with_resume(task, status_cb)

            task.size_bytes = real_size  # update actual size

            done = build_done_block(task)
            try:
                app.loop.create_task(
                    status_msg.edit_text(
                        quoted_status(done) + f"\n❌ /cancel_{task.task_id}",
                        parse_mode=ParseMode.HTML
                    )
                )
            except:
                pass

            # UPLOAD
            caption = f"📄 {task.title}\n🆔 {task.task_id}"

            try:
                fut = asyncio.run_coroutine_threadsafe(
                    upload_result(task, path, caption),
                    app.loop
                )
                ok = fut.result()
            except:
                ok = False

            # SAVE HISTORY
            add_history(task.user_id, task.source_link, task.title, real_size)
            bump_stats(real_size)

            # final status
            if ok:
                final = "✅ Uploaded Successfully!"
            else:
                final = "❌ Upload failed."

            try:
                app.loop.create_task(
                    status_msg.edit_text(
                        quoted_status(final),
                        parse_mode=ParseMode.HTML
                    )
                )
            except:
                pass

        except RuntimeError as e:
            msg = str(e)
            if "4GB" in msg:
                err = "File exceeds 4GB limit."
            elif "cancelled" in msg.lower():
                err = "Task cancelled."
            else:
                err = "Error in the link or file not found."

            try:
                app.loop.create_task(
                    status_msg.edit_text(
                        quoted_status(build_error_block(err, task)),
                        parse_mode=ParseMode.HTML
                    )
                )
            except:
                pass

            # Cleanup partial
            try:
                if task.temp_path and os.path.exists(task.temp_path):
                    os.remove(task.temp_path)
            except:
                pass

        except Exception:
            try:
                app.loop.create_task(
                    status_msg.edit_text(
                        quoted_status(build_error_block("Unexpected error.", task)),
                        parse_mode=ParseMode.HTML
                    )
                )
            except:
                pass

            try:
                if task.temp_path and os.path.exists(task.temp_path):
                    os.remove(task.temp_path)
            except:
                pass

        finally:
            active_tasks.pop(task.task_id, None)
            user_active_task.pop(task.user_id, None)

    threading.Thread(target=worker, daemon=True).start()


# ============ HELP / LEGAL ============

LEGAL_NOTE = (
    "⚠️ Use this bot only for files you have permission to download.\n"
    "Do NOT use to infringe copyright."
)

@app.on_message(filters.command(["help","about"]))
async def cmd_help(_, m: Message):
    await m.reply(LEGAL_NOTE)


# ============ RUN ============

if __name__ == "__main__":
    print("AYU TeraBox Bot starting...")
    try:
        app.run()
    except KeyboardInterrupt:
        pass