# -*- coding: utf-8 -*-
# AYU — TeraBox Downloader Bot (single file, VPS-tuned)
# Requirements:
#   pip3.10 install pyrogram tgcrypto requests

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

# ========= USER CONFIG (EDIT THESE) =========
OWNER_ID = 1685470205               # <-- YOUR TELEGRAM ID
SUDO_IDS = []                        # extra admins (e.g. [111,222])
BOT_TOKEN = ""
API_ID   = 28244492
API_HASH = "38e4ce53faea889073f6f49e83cbc392"

FORCE_SUB_CHANNEL = ""       # without @
UPDATES_GROUP_URL = "https://t.me/+S1AMHMx-PiM0ZGJl"
DUMP_CHANNEL_ID   = -1002560282913   # confirmed by you
# ===========================================

# ---------- Core behavior knobs ----------
MAX_RETRIES_CONNECT = 2          # only ONE reconnect attempt mid-stream (no spam)
FIRST_DATA_TIMEOUT  = 300        # wait up to 5 minutes for first bytes, else "file not found"
CHUNK_SIZE_BYTES    = 1024*1024  # 1MB chunks for better throughput on VPS
SPEED_UPDATE_EVERY  = 3.0        # seconds between progress updates
USER_TASK_LIMIT     = 5          # max concurrent tasks per user
# -----------------------------------------

PUBLIC_MODE_DEFAULT = 0          # 0=private, 1=public
HISTORY_DAYS = 10
SPLIT_THRESHOLD = 2 * 1024 * 1024 * 1024 - (10 * 1024 * 1024)  # ≈1.99GB
MAX_FILE_SUPPORTED = 4 * 1024 * 1024 * 1024                    # 4GB max
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
    ✅ If verification works → check membership
    ✅ If verification fails (private channel/no rights) → allow anyway
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
        self.last_report = 0.0
        self.last_speed = 0.0

active_tasks = {}          # task_id -> Task
user_tasks = {}            # user_id -> set([task_ids])   (limit: USER_TASK_LIMIT)

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
                "title": (d.get("title") or "").strip(),
                "size": (d.get("size") or "").strip(),
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

def quoted_status(text: str) -> str:
    """HTML pre block for quoted look"""
    return f"<pre>{text}</pre>"

def build_live_block(task: Task, downloaded, total):
    sp = human_bytes(getattr(task, "last_speed", 0)) + "/s" if getattr(task, "last_speed", 0) else "N/A"
    size_text = human_bytes(total) if total and total >= 1024 else "Unknown"
    prog_right = human_bytes(total) if total and total >= 1024 else "Unknown"
    return (
        f"📥 Downloading\n\n"
        f"📄 {task.title}\n"
        f"📦 Size: {size_text}\n"
        f"⚡ Speed: {sp}\n"
        f"📊 Progress: {human_bytes(downloaded)}/{prog_right}\n"
        f"🆔 Task ID: {task.task_id}\n"
        f"🚀 Will resume from where it left off"
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
    # ============ DOWNLOAD ENGINE (5-min wait, single reconnect, resume) ============

def download_with_resume(task: Task, status_cb):
    """
    VPS-tuned downloader:
      • Waits up to FIRST_DATA_TIMEOUT (3–5 min) for first bytes; else raises "file not found"
      • Supports resume from .part
      • If mid-stream fails, refreshes URL ONCE and resumes (no retry spam)
      • 1MB chunks for better throughput on unstable networks
    """
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    filename  = os.path.join(DOWNLOAD_DIR, task.filename)
    part_path = filename + ".part"
    task.temp_path = part_path

    # resume info
    downloaded = 0
    if os.path.exists(part_path):
        downloaded = os.path.getsize(part_path)

    # probe size (do not trust tiny headers)
    total = 0
    try:
        h = requests.head(task.download_url, headers={"User-Agent": UA}, timeout=20, allow_redirects=True)
        total = int(h.headers.get("content-length") or 0)
    except Exception:
        total = 0
    if total < 1024:  # treat tiny (e.g., 93 B) as unknown
        total = 0

    reconnect_attempts = 0

    def stream_once(resume: bool):
        nonlocal downloaded, total

        headers = {"User-Agent": UA, "Accept": "*/*"}
        if resume and downloaded > 0:
            headers["Range"] = f"bytes={downloaded}-"

        with requests.get(
            task.download_url,
            stream=True,
            headers=headers,
            timeout=120,           # longer timeout for VPS
            allow_redirects=True
        ) as r:
            ctype = (r.headers.get("content-type") or "").lower()
            if "text/html" in ctype or r.status_code in (403, 404):
                raise RuntimeError(f"Invalid response: {r.status_code} {ctype}")

            r.raise_for_status()

            # adopt sane length if server reveals it now
            if total == 0:
                try:
                    c_len = int(r.headers.get("content-length") or 0)
                    if c_len >= 1024:
                        total = c_len + (downloaded if resume else 0)
                except Exception:
                    pass

            mode = "ab" if resume else "wb"
            with open(part_path, mode) as f:
                first_byte_deadline = time.time() + FIRST_DATA_TIMEOUT
                got_any_data = False
                last_tick = time.time()
                last_sent = downloaded

                for chunk in r.iter_content(chunk_size=CHUNK_SIZE_BYTES):
                    if task.stop_flag:
                        raise RuntimeError("Task cancelled")

                    if chunk:
                        # got data — clear first-byte wait
                        got_any_data = True
                        first_byte_deadline = time.time() + FIRST_DATA_TIMEOUT

                        f.write(chunk)
                        downloaded += len(chunk)

                        # speed & UI tick
                        now = time.time()
                        if now - last_tick >= SPEED_UPDATE_EVERY:
                            task.last_speed = (downloaded - last_sent) / max(1e-6, (now - last_tick))
                            last_sent = downloaded
                            last_tick = now
                            status_cb(downloaded, total, None, False)
                    else:
                        # no chunk — if never got any AND exceeded wait window -> abort
                        if not got_any_data and time.time() > first_byte_deadline:
                            raise RuntimeError("No data received — file not found or server unavailable")

        return True

    try:
        # first attempt (resume if .part exists)
        stream_once(resume=(downloaded > 0))

    except Exception:
        # single reconnect with refreshed URL
        if reconnect_attempts >= MAX_RETRIES_CONNECT:
            raise RuntimeError("Failed — no data received or server issue")

        reconnect_attempts += 1

        info = teradl_info(task.source_link)
        if info and info.get("download"):
            task.download_url = info["download"]

        # resume once
        stream_once(resume=True)

    # finalize
    if os.path.exists(part_path):
        os.rename(part_path, filename)

    final_size = os.path.getsize(filename)
    if not total or total < 1024:
        total = final_size
    return filename, final_size


# ============ ACCESS CONTROL GUARD (Light FSUB only for links) ============

async def can_use_here(message: Message) -> bool:
    uid  = message.from_user.id if message.from_user else 0
    chat = message.chat

    # track users/groups
    if message.chat.type.name.lower() == "private":
        add_user(uid)
    else:
        add_group(chat.id, chat.title)

    # Light FSUB only when sending a link
    if message.chat.type.name.lower() == "private":
        if message.text and message.text.startswith("http"):
            ok = await require_fsub(uid)
            if not ok:
                btn = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCE_SUB_CHANNEL}")],
                    [InlineKeyboardButton("👥 Join Group",   url=UPDATES_GROUP_URL)]
                ])
                await message.reply("🔒 Please join our channel to use this bot.", reply_markup=btn)
                return False

    # PUBLIC / PRIVATE
    if current_mode_public():
        return True

    # PRIVATE mode
    if message.chat.type.name.lower() == "private":
        return is_user_authorized(uid) or is_owner_or_sudo(uid)
    else:
        return is_group_authorized(message.chat.id) or is_owner_or_sudo(uid)


# ============ UPLOAD (split >2GB) + DUMP (fixed order) ============

async def upload_result(task: Task, path: str, caption: str):
    """
    Upload to user chat; also mirror to dump channel BEFORE deleting.
    Ensures we keep the file on disk until BOTH uploads are done.
    """
    size = os.path.getsize(path)
    root = os.path.splitext(os.path.basename(path))[0] or "@AYU_BOTs"
    ext  = os.path.splitext(path)[1] or ".bin"

    # split if needed
    parts = split_file(path, SPLIT_THRESHOLD) if size > SPLIT_THRESHOLD else [path]
    ok_all = True
    idx = 1

    for item in parts:
        filename = os.path.basename(item)

        if len(parts) > 1 and ".part" not in filename:
            new_name = f"{root}.part{idx}{ext}"
            new_path = os.path.join(os.path.dirname(item), new_name)
            os.rename(item, new_path)
            item     = new_path
            filename = new_name

        # 1) send to user chat
        try:
            await app.send_document(
                task.chat_id,
                item,
                file_name=filename,
                caption=caption
            )
        except FloodWait as e:
            time.sleep(e.value)
            await app.send_document(
                task.chat_id,
                item,
                file_name=filename,
                caption=caption
            )
        except Exception:
            ok_all = False
            await app.send_message(
                task.chat_id,
                quoted_status(build_error_block("Upload failed for a part.", task)),
                parse_mode=ParseMode.HTML
            )

        # 2) mirror to dump channel (BEFORE deletion)
        try:
            dump_caption = (
                f"👤 User: <code>{task.user_id}</code>\n"
                f"🔗 Link: {task.source_link}\n"
                f"📄 {task.title}"
            )
            await app.send_document(
                DUMP_CHANNEL_ID,
                item,
                file_name=filename,
                caption=dump_caption,
                parse_mode=ParseMode.HTML
            )
        except FloodWait as e:
            time.sleep(e.value)
            try:
                await app.send_document(
                    DUMP_CHANNEL_ID,
                    item,
                    file_name=filename,
                    caption=dump_caption,
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
        except Exception:
            # ignore dump failure, but continue
            pass

        # finally remove local part
        try:
            os.remove(item)
        except Exception:
            pass

        idx += 1

    return ok_all

# ============ START UI ============

WELCOME_HTML = (
    "👋 <b>𝘏𝘦𝘭𝘭𝘰 Channel!</b>\n\n"
    "📥 𝘐'𝘮 𝘺𝘰𝘶𝘳 <b>TeraBox Downloader Bot</b>\n\n"
    "🎬 𝘑𝘶𝘴𝘵 𝘴𝘦𝘯𝘥 𝘮𝘦 𝘢𝘯𝘺 𝘷𝘢𝘭𝘪𝘥 𝘛𝘦𝘳𝘢𝘣𝘰𝘹 𝘭𝘪𝘯𝘬.\n\n"
    "📌 𝘍𝘪𝘭𝘦𝘴 ≤ <b>2GB</b> upload directly; bigger ones auto-split (<b>2GB</b> parts).\n"
    "⚡️ 𝘖𝘱𝘵𝘪𝘮𝘪𝘻𝘦𝘥 𝘧𝘰𝘳 𝘝𝘗𝘚: 1MB chunks, resume, single reconnect.\n\n"
    "🔔 𝘑𝘰𝘪𝘯 𝘤𝘩𝘢𝘯𝘯𝘦𝘭 & 𝘨𝘳𝘰𝘶𝘱 𝘧𝘰𝘳 𝘶𝘱𝘥𝘢𝘵𝘦𝘴."
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
        await m.reply("🚫 Only owner/sudo.", quote=True)

@app.on_message(filters.command("private"))
async def cmd_private(_, m: Message):
    if m.from_user and is_owner_or_sudo(m.from_user.id):
        set_setting("public_mode", "0")
        await m.reply("🔒 Bot mode set to: <b>PRIVATE</b>", parse_mode=ParseMode.HTML)
    else:
        await m.reply("🚫 Only owner/sudo.", quote=True)


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


# ============ STATUS (user task OR owner-wide) ============

@app.on_message(filters.command("status"))
async def cmd_status(_, m: Message):
    if not await can_use_here(m):
        return

    uid = m.from_user.id
    # Owner/Sudo can see ALL active tasks (owner-wide)
    if is_owner_or_sudo(uid):
        if not active_tasks:
            return await m.reply(quoted_status("ℹ️ No active tasks."), parse_mode=ParseMode.HTML)

        lines = []
        for tid, t in list(active_tasks.items()):
            downloaded = 0
            if t.temp_path and os.path.exists(t.temp_path):
                downloaded = os.path.getsize(t.temp_path)
            sp = human_bytes(getattr(t, "last_speed", 0)) + "/s" if getattr(t, "last_speed", 0) else "N/A"
            sz = human_bytes(t.size_bytes) if t.size_bytes else "Unknown"
            lines.append(
                f"🆔 {tid} • 👤 {t.user_id}\n"
                f"📄 {sanitize_filename(t.title)}\n"
                f"⚡ {sp} • 📥 {human_bytes(downloaded)}/{sz}"
            )
        text = "📡 <b>Active tasks (all users)</b>\n\n" + "\n\n".join(lines)
        return await m.reply(text, parse_mode=ParseMode.HTML)

    # Normal user → only their first active task
    # (User can have up to 5; show the first in list)
    tids = list(user_tasks.get(uid, set()))
    if not tids:
        return await m.reply(quoted_status("ℹ️ No active task."), parse_mode=ParseMode.HTML)
    tid = tids[0]
    t = active_tasks.get(tid)
    if not t:
        return await m.reply(quoted_status("ℹ️ No active task."), parse_mode=ParseMode.HTML)

    downloaded = 0
    if t.temp_path and os.path.exists(t.temp_path):
        downloaded = os.path.getsize(t.temp_path)
    msg = quoted_status(build_live_block(t, downloaded, t.size_bytes or 0)) + f"\n❌ /cancel_{t.task_id}"
    await m.reply(msg, parse_mode=ParseMode.HTML)


# ============ CANCEL ============

@app.on_message(filters.regex(r"^/cancel_[0-9a-fA-F]{8}$"))
async def cmd_cancel(_, m: Message):
    if not await can_use_here(m):
        return

    tid = m.text.split("_", 1)[1]
    task = active_tasks.get(tid)

    if not task:
        return await m.reply(quoted_status("ℹ️ Task not found."), parse_mode=ParseMode.HTML)

    if task.user_id != m.from_user.id and not is_owner_or_sudo(m.from_user.id):
        return await m.reply("🚫 You cannot cancel others' tasks.")

    task.stop_flag = True
    await m.reply(quoted_status("🛑 Cancel requested."), parse_mode=ParseMode.HTML)


# ============ MAIN LINK HANDLER (5 tasks per user) ============

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

    # enforce per-user task limit
    cur_set = user_tasks.get(uid, set())
    if len(cur_set) >= USER_TASK_LIMIT:
        return await m.reply(
            quoted_status(f"⛔ You already have {USER_TASK_LIMIT} active tasks. Wait or /cancel one."),
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

    # ensure extension
    if "." not in os.path.basename(title):
        ext = os.path.splitext(urlparse(dl_url).path)[1] or ".bin"
        title += ext

    task = Task(uid, m.chat.id, link)
    task.title        = title
    task.size_str     = size_str
    task.size_bytes   = size_est
    task.download_url = dl_url
    task.filename     = title

    active_tasks[task.task_id] = task
    cur_set.add(task.task_id)
    user_tasks[uid] = cur_set  # save back

    head = (
        "✅ Link Found!\n"
        f"📄 {task.title}\n"
        f"📦 {task.size_str or (human_bytes(task.size_bytes) if task.size_bytes else 'Unknown')}\n"
        f"🆔 Task ID: {task.task_id}\n\n"
        "⏳ Starting download..."
    )

    status_msg = await m.reply(
        quoted_status(head) + f"\n❌ /cancel_{task.task_id}",
        parse_mode=ParseMode.HTML
    )

    # ===== STATUS CALLBACK (live updates) =====
    def status_cb(downloaded, total, retry_idx, is_connect_issue):
        live = build_live_block(task, downloaded, total)
        txt  = quoted_status(live) + f"\n❌ /cancel_{task.task_id}"
        try:
            app.loop.create_task(status_msg.edit_text(txt, parse_mode=ParseMode.HTML))
        except:
            pass

    # ===== BACKGROUND WORKER =====
    def worker():
        try:
            # DOWNLOAD
            path, real_size = download_with_resume(task, status_cb)
            task.size_bytes = real_size  # update actual size

            # NOTIFY DONE
            try:
                app.loop.create_task(
                    status_msg.edit_text(
                        quoted_status(build_done_block(task)) + f"\n❌ /cancel_{task.task_id}",
                        parse_mode=ParseMode.HTML
                    )
                )
            except:
                pass

            # UPLOAD
            caption = f"📄 {task.title}\n🆔 {task.task_id}"
            try:
                fut = asyncio.run_coroutine_threadsafe(upload_result(task, path, caption), app.loop)
                ok = fut.result()
            except:
                ok = False

            # SAVE HISTORY / STATS
            add_history(task.user_id, task.source_link, task.title, real_size)
            bump_stats(real_size)

            # FINAL STATUS
            final = "✅ Uploaded Successfully!" if ok else "❌ Upload failed."
            try:
                app.loop.create_task(status_msg.edit_text(quoted_status(final), parse_mode=ParseMode.HTML))
            except:
                pass

        except RuntimeError as e:
            msg = str(e)
            if "file not found" in msg.lower() or "no data received" in msg.lower():
                err = "File not found or server unavailable."
            elif "4GB" in msg:
                err = "File exceeds 4GB limit."
            elif "cancelled" in msg.lower():
                err = "Task cancelled."
            else:
                err = "Unexpected download error."

            try:
                app.loop.create_task(status_msg.edit_text(quoted_status(build_error_block(err, task)), parse_mode=ParseMode.HTML))
            except:
                pass

            # Remove partial
            try:
                if task.temp_path and os.path.exists(task.temp_path):
                    os.remove(task.temp_path)
            except:
                pass

        except Exception:
            try:
                app.loop.create_task(status_msg.edit_text(quoted_status(build_error_block("Unexpected error.", task)), parse_mode=ParseMode.HTML))
            except:
                pass
            try:
                if task.temp_path and os.path.exists(task.temp_path):
                    os.remove(task.temp_path)
            except:
                pass

        finally:
            # release task from registry
            active_tasks.pop(task.task_id, None)
            s = user_tasks.get(task.user_id, set())
            s.discard(task.task_id)
            if s:
                user_tasks[task.user_id] = s
            else:
                user_tasks.pop(task.user_id, None)

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
