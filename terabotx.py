# -*- coding: utf-8 -*-
# AYU — TeraBox Downloader Bot (stable, VPS-tuned)
# WAIT 30 mins for slow VPS
# Uses Pyrogram MTProto (NO 20MB limit)
# New extractor: ?key=RushVx

import os
import re
import time
import json
import uuid
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
from pyrogram.errors import FloodWait

# ========= USER CONFIG =========
OWNER_ID = 1685470205
SUDO_IDS = []
BOT_TOKEN = "8343188725:AAFVsaUHOpIlq7V180W3VcDjDUhLFt2jfUM"
API_ID   = 28244492
API_HASH = "38e4ce53faea889073f6f49e83cbc392"

FORCE_SUB_CHANNEL = ""     # without @
UPDATES_GROUP_URL = "https://t.me/+S1AMHMx-PiM0ZGJl"
DUMP_CHANNEL_ID   = -1002560282913
# ================================

# ---------- Engine Settings ----------
MAX_RETRIES_CONNECT = 2
FIRST_DATA_TIMEOUT  = 1800   # 30 minutes for slow VPS
CHUNK_SIZE_BYTES    = 1024*1024
SPEED_UPDATE_EVERY  = 3.0
USER_TASK_LIMIT     = 5
PUBLIC_MODE_DEFAULT = 0
HISTORY_DAYS = 10
SPLIT_THRESHOLD = 2 * 1024 * 1024 * 1024 - (10 * 1024 * 1024)
MAX_FILE_SUPPORTED = 4 * 1024 * 1024 * 1024
DOWNLOAD_DIR = "downloads"
DB_PATH = "ayubot.db"
# --------------------------------------

app = Client(
    "ayu_terabox_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN   # <-- this ensures MTProto upload up to 4GB
)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------------- DATABASE ----------------

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
    cur.execute("""CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        val TEXT
    )""")
    cur.execute("INSERT OR IGNORE INTO settings VALUES('public_mode',?)",
                (str(PUBLIC_MODE_DEFAULT),))
    cur.execute("INSERT OR IGNORE INTO stats VALUES('total_bytes',0)")
    cur.execute("INSERT OR IGNORE INTO stats VALUES('total_files',0)")
    DB.commit()

init_db()

# ---------------- HELPERS ----------------

def set_setting(key,val):
    DB.execute("REPLACE INTO settings VALUES(?,?)",(key,str(val))); DB.commit()

def get_setting(key,default=None):
    r = DB.execute("SELECT val FROM settings WHERE key=?",(key,)).fetchone()
    return r[0] if r else default

def add_user(uid):
    DB.execute("INSERT OR IGNORE INTO users VALUES(?,?,?)",
        (uid,0,datetime.now(timezone.utc).isoformat()))
    DB.commit()

def set_user_auth(uid,v):
    DB.execute("UPDATE users SET is_authorized=? WHERE user_id=?",(1 if v else 0,uid))
    DB.commit()

def is_user_authorized(uid):
    if uid==OWNER_ID or uid in SUDO_IDS: return True
    r = DB.execute("SELECT is_authorized FROM users WHERE user_id=?",(uid,)).fetchone()
    return bool(r and r[0]==1)

def bump_stats(b):
    DB.execute("UPDATE stats SET val=val+? WHERE key='total_bytes'",(b,))
    DB.execute("UPDATE stats SET val=val+1 WHERE key='total_files'")
    DB.commit()

def add_history(uid,link,title,size):
    DB.execute("INSERT INTO history(user_id,link,title,size_bytes,created_at) VALUES(?,?,?,?,?)",
        (uid,link,title,size,datetime.now(timezone.utc).isoformat()))
    DB.commit()

def recent_history(uid):
    since = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
    q = DB.execute(
        "SELECT title,link,size_bytes FROM history WHERE user_id=? AND datetime(created_at)>=datetime(?) ORDER BY id DESC LIMIT 50",
        (uid,since.isoformat())
    )
    return q.fetchall()

def human_bytes(n):
    units=["B","KB","MB","GB","TB"]; i=0; n=float(n)
    while n>=1024 and i<len(units)-1: n/=1024; i+=1
    return f"{n:.2f} {units[i]}"

def sanitize_filename(name):
    name=(name or "").strip()
    bad='<>:"/\\|?*'
    for ch in bad: name=name.replace(ch,"_")
    return name or "file.bin"

# ---------------- EXTRACTOR ----------------

def teradl_info(link:str):
    """
    Updated extractor using ?key=RushVx
    """
    try:
        url=f"https://teradl.tiiny.io/?key=RushVx&link={link}"
        r=requests.get(url,timeout=30)
        r.raise_for_status()
        j=r.json()

        if "data" in j and isinstance(j["data"],list) and j["data"]:
            d=j["data"][0]
            return {
                "title": (d.get("title") or "").strip(),
                "size":  (d.get("size")  or "").strip(),
                "download": d.get("download")
            }
        return None
    except:
        return None

# ---------------- DOWNLOAD ENGINE ----------------

class Task:
    def __init__(self,uid,cid,link):
        self.task_id=uuid.uuid4().hex[:8]
        self.user_id=uid
        self.chat_id=cid
        self.source_link=link
        self.title=None
        self.size_bytes=0
        self.download_url=None
        self.filename=None
        self.temp_path=None
        self.stop_flag=False
        self.last_speed=0

active_tasks={}
user_tasks={}

def download_with_resume(task,status_cb):
    UA="Mozilla/5.0"
    filename=os.path.join(DOWNLOAD_DIR,task.filename)
    part=filename+".part"
    task.temp_path=part

    downloaded = os.path.getsize(part) if os.path.exists(part) else 0
    total=0

    reconnect=0
    def stream_once(resume):
        nonlocal downloaded,total
        headers={"User-Agent":UA}
        if resume and downloaded>0:
            headers["Range"]=f"bytes={downloaded}-"

        with requests.get(task.download_url,stream=True,headers=headers,
            timeout=1800,allow_redirects=True) as r:

            ctype=(r.headers.get("content-type") or "").lower()
            if "html" in ctype: raise RuntimeError("Bad content-type")

            if total==0:
                try:
                    clen=int(r.headers.get("content-length") or 0)
                    if clen>=1024: total=clen+(downloaded if resume else 0)
                except: pass

            mode="ab" if resume else "wb"
            with open(part,mode) as f:
                first_deadline=time.time()+FIRST_DATA_TIMEOUT
                got_any=False
                last_tick=time.time()
                last_sent=downloaded

                for chunk in r.iter_content(chunk_size=CHUNK_SIZE_BYTES):
                    if task.stop_flag: raise RuntimeError("cancelled")
                    if chunk:
                        got_any=True
                        first_deadline=time.time()+FIRST_DATA_TIMEOUT
                        f.write(chunk)
                        downloaded+=len(chunk)
                        now=time.time()
                        if now-last_tick>=SPEED_UPDATE_EVERY:
                            task.last_speed=(downloaded-last_sent)/(now-last_tick)
                            last_sent=downloaded
                            last_tick=now
                            status_cb(downloaded,total)
                    else:
                        if not got_any and time.time()>first_deadline:
                            raise RuntimeError("No data")

    try:
        stream_once(downloaded>0)
    except Exception:
        if reconnect>=MAX_RETRIES_CONNECT:
            raise RuntimeError("Failed")
        reconnect+=1
        info=teradl_info(task.source_link)
        if info and info.get("download"):
            task.download_url=info["download"]
        stream_once(True)

    if os.path.exists(part): os.rename(part,filename)
    return filename, os.path.getsize(filename)

# ---------------- BOT COMMANDS ----------------

async def can_use_here(m):
    uid=m.from_user.id
    add_user(uid)
    if get_setting("public_mode")=="1": return True
    return is_user_authorized(uid) or uid==OWNER_ID

WELCOME=(
    "👋 <b>Hello!</b>\n"
    "Send any valid TeraBox link.\n"
    "This bot supports 4GB upload (MTProto).\n"
    "⏳ VPS slow? No problem — Will wait up to <b>30 mins</b> for download to start."
)

@app.on_message(filters.command("start"))
async def start(_,m):
    await m.reply(WELCOME,parse_mode=ParseMode.HTML)

# ---------------- HANDLE LINK ----------------

@app.on_message(filters.text & ~filters.command(["start","stats","history"]))
async def link(_,m):
    if not await can_use_here(m): return
    link=m.text.strip()
    if not link.startswith("http"): return

    info=teradl_info(link)
    if not info or not info.get("download"):
        return await m.reply("❌ Invalid or unsupported link.")

    title=sanitize_filename(info["title"])
    size_text=info["size"]
    size_bytes=0
    try: size_bytes=float(size_text.split()[0])*1024*1024
    except: pass

    dl=info["download"]
    task=Task(m.from_user.id,m.chat.id,link)
    task.title=title
    task.filename=title
    task.download_url=dl
    task.size_bytes=size_bytes

    active_tasks[task.task_id]=task
    user_tasks.setdefault(task.user_id,set()).add(task.task_id)

    status_msg=await m.reply(f"📥 Starting...\n📄 {title}\n🆔 {task.task_id}")

    def status_cb(d,t):
        txt=(f"📥 Downloading…\n\n📄 {task.title}\n"
             f"📊 {human_bytes(d)}/{human_bytes(t)}\n"
             f"⚡ {human_bytes(task.last_speed)}/s\n"
             f"🆔 {task.task_id}")
        try:
            app.loop.create_task(status_msg.edit_text(txt))
        except: pass

    def worker():
        try:
            path,real=download_with_resume(task,status_cb)
            add_history(task.user_id,task.source_link,task.title,real)
            bump_stats(real)
            try:
                app.loop.create_task(status_msg.edit_text("⏫ Uploading..."))
            except: pass
            asyncio.run_coroutine_threadsafe(
                app.send_document(task.chat_id,path,caption=f"📄 {task.title}"), app.loop
            ).result()
            asyncio.run_coroutine_threadsafe(
                app.send_document(DUMP_CHANNEL_ID,path,
                    caption=f"User: {task.user_id}\n{task.source_link}"),
                app.loop
            ).result()
            os.remove(path)
            app.loop.create_task(status_msg.edit_text("✅ Done!"))
        except Exception as e:
            app.loop.create_task(status_msg.edit_text(f"❌ Error: {str(e)}"))
        finally:
            active_tasks.pop(task.task_id,None)
            user_tasks.get(task.user_id,set()).discard(task.task_id)

    threading.Thread(target=worker,daemon=True).start()

# ---------------- RUN BOT ----------------

if __name__=="__main__":
    print("AYU TeraBox Bot Running...")
    app.run()
