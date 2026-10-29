# ======================================================
#              TITAN VAULT  --  Combined
#  Scrapers    -> SerienStream, VOE, Lulu, Dood, KVS, etc
#  aria2c      -> Direct file & CDN acceleration
#  yt-dlp      -> Video platforms & encrypted HLS/DASH
#  Telethon    -> 8-worker parallel upload up to 2 GB
#  Batch Queue -> Auto-Name & Footprint for 200+ Ep
#  Media Engine-> Thumbnails, MP3, Remux & Compression
# ======================================================

import logging
import collections
import sqlite3
import os
import re
import socket
import time
import math
import struct
import random
import string
import tempfile
import asyncio
import subprocess
import shutil
import base64
import json
from urllib.parse import urlparse, urljoin, urlunsplit, parse_qs, unquote
from html import unescape as _hunesc          # ← added (sxyprn entity decoding)
import yt_dlp
import requests

from PIL import Image
from colorama import Fore, Style, init

# ── python-telegram-bot ───────────────────────────────
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.error import RetryAfter, TimedOut, NetworkError, BadRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)

# ── Telethon ──────────────────────────────────────────
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import (
    DocumentAttributeVideo, DocumentAttributeFilename,
    InputFileBig, InputFile
)
from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest
from telethon.errors import FloodWaitError


# ══════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════
BOT_TOKEN          = os.getenv("BOT_TOKEN", "")
API_ID             = int(os.getenv("API_ID", ""))
API_HASH           = os.getenv("API_HASH", "")
SESSION_STRING     = os.getenv("SESSION_STRING", "")
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", ""))
OWNER_ID           = int(os.getenv("OWNER_ID", ""))



FILES_PER_PAGE     = 10
FFMPEG_PATH        = "ffmpeg"
FFPROBE_PATH       = "ffprobe"
FFPROBE_TIMEOUT    = 10
MAX_UPLOAD_BYTES   = 2 * 1024 * 1024 * 1024   # 2 GB
STALL_WARN_SECS    = int(os.getenv("STALL_WARN_SECS", "25"))       # ffmpeg silent this long = flag it
AUDIO_EXTS         = (".mp3", ".m4a", ".opus", ".flac", ".aac")

# Download quality policy.
#   0  = best available, no ceiling (default)
#   N  = highest resolution up to N, and only the CLOSEST higher variant when nothing <= N exists.
# Applied at three places so it holds on every route: labelled quality lists, HLS variant
# pinning, and the yt-dlp format string. A ceiling also gates the post-download resize.
QUALITY_DEFAULT    = int(os.getenv("QUALITY_DEFAULT", "0"))
user_quality: dict = {}          # uid -> ceiling in px (0 = best)
_ACTIVE_CEILING    = [QUALITY_DEFAULT]   # ceiling of the job currently being resolved
_LAST_PAGE_SCAN    = [""]                # what the last page scan saw, for the failure diagnostics

def _q_ceiling(uid) -> int:
    return int(user_quality.get(uid, QUALITY_DEFAULT) or 0)

def _ydl_format(ceiling: int) -> str:
    """Highest quality up to the ceiling; codec is only a tie-break at equal resolution."""
    if not ceiling:
        return "bestvideo+bestaudio/best"
    return (f"bestvideo[height<={ceiling}]+bestaudio[acodec^=mp4a]/"
            f"bestvideo[height<={ceiling}]+bestaudio/"
            f"worstvideo[height>{ceiling}]+bestaudio/"
            f"bestvideo+bestaudio/best")

YDL_FORMAT = _ydl_format(1080)   # kept for reference; live code calls _ydl_format(ceiling)
TELETHON_UPLOAD_WORKERS = 8
SILENT_MODE        = [True]

ARIA2C_PATH         = "aria2c"
ARIA2C_DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", r"C:\Users\ManuelMania\Downloads\aria2c")
os.makedirs(ARIA2C_DOWNLOAD_DIR, exist_ok=True)

# ── Browser handoff bridge (127.0.0.1 only) ──
BRIDGE_ENABLE      = os.getenv("BRIDGE_ENABLE", "1") != "0"
BRIDGE_PORT        = int(os.getenv("BRIDGE_PORT", "8765"))
BRIDGE_EXT_ID      = os.getenv("BRIDGE_EXT_ID", "").strip()   # optional: pin your extension ID
# ── N_m3u8DL-RE (optional one-time external HLS/DASH engine, experimental) ──
# Used as an extra HLS/DASH leg AFTER yt-dlp and FFmpeg have failed (N_M3U8DL_MODE=off disables it). Binary lookup: N_M3U8DL_PATH, then next to this script, PATH,
# then ~/Downloads/N_m3u8dl-RE*win-x64*. Missing binary = leg silently skipped.
N_M3U8DL_MODE      = os.getenv("N_M3U8DL_MODE", "fallback").strip().lower()
N_M3U8DL_PATH      = os.getenv("N_M3U8DL_PATH", "").strip()
N_M3U8DL_THREADS   = int(os.getenv("N_M3U8DL_THREADS", "8"))
N_M3U8DL_TIMEOUT_SEC = int(os.getenv("N_M3U8DL_TIMEOUT_SEC", "7200"))

MAX_PARALLEL_DOWNLOADS = 3
DOWNLOAD_QUEUE_WORKERS = 2

MAX_RETRIES       = 3
RETRY_BASE_DELAY  = 1.0
RETRY_MAX_DELAY   = 30.0

_STANDARD_UA   = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
# Second request profile (Android Chrome, reduced UA format, same major version as the desktop one).
# Used only as a bounded fallback after the server refuses the desktop profile (see _do_dl_via_ffmpeg_hls).
_MOBILE_UA     = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
_UA_PREF: dict = {}   # host -> "mobile" | "desktop": the profile that last worked after a fallback (bounded)
_STANDARD_LANG = "en-US,en;q=0.9"

_SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
URL_CACHE_TTL  = 86400
URL_CACHE_FILE = os.path.join(_SCRIPT_DIR, "url_cache.json")
COOKIES_FILE   = os.path.join(_SCRIPT_DIR, "cookies.txt")
COOKIES_FILE   = COOKIES_FILE if os.path.isfile(COOKIES_FILE) else None

# ── CDP tier browser binary ───────────────────────────
# nodriver drives a REAL Chromium over CDP and has no browser of its own. On a machine without
# Chrome in a standard location it finds nothing and the tier can never start, which is exactly
# what "Failed to connect to browser" meant. CHROME_PATH wins; otherwise the usual install
# locations, then PATH. Edge counts: it is Chromium, it speaks CDP, and it is on every Windows
# box - far better than having no browser at all.
CHROME_PATH = os.getenv("CHROME_PATH", "").strip()
_CHROMIUM_HINTS = (
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
    r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
    r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
    r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%PROGRAMFILES%\Chromium\Application\chrome.exe",
)
_BROWSER_BIN = [None, False]   # [path, looked_up]

def _find_browser_bin() -> str:
    """Chromium-family binary for the CDP tier, or "". Cached: one lookup per process."""
    if _BROWSER_BIN[1]:
        return _BROWSER_BIN[0] or ""
    cands = []
    if CHROME_PATH:
        cands.append(CHROME_PATH)
    for h in _CHROMIUM_HINTS:
        cands.append(os.path.expandvars(h))
    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe",
                 "brave", "brave.exe", "chromium", "chromium.exe"):
        w = shutil.which(name)
        if w: cands.append(w)
    hit = next((c for c in cands if c and os.path.isfile(c)), "")
    _BROWSER_BIN[0], _BROWSER_BIN[1] = hit, True
    return hit

# ── In-session browser capture leg ────────────────────
# Runs after the sxyprn branch, before the direct requests legs. The browser that mints the
# token also pulls the bytes; the name comes from Content-Disposition, then og:title, never
# from the URL. Two tiers: nodriver (real Chrome over CDP, no WebDriver - the only stack that
# passes Turnstile in current benchmarks) then patchright/playwright. A launch failure puts the
# whole leg on a short cooldown instead of latching it off for the rest of the process.   ← changed
SNATCH_ENABLE      = os.getenv("SNATCH_ENABLE", "1") != "0"
NODRIVER_TIER      = os.getenv("NODRIVER_TIER", "1") != "0"
SNATCH_MAX_SEC     = int(os.getenv("SNATCH_MAX_SEC", "120"))   # whole-leg budget (a CF wall may need a click)
_SNATCH_RAM_CAP    = 400 * 1024 * 1024          # context.request fallback refuses anything bigger
_SNATCH_MAX_BYTES  = 3 * 1024 ** 3              # runaway chunked stream with no content-length
_SNATCH_AVAIL      = {"nodriver": None, "playwright": None}   # None untried / True / False
_SNATCH_SEM        = [None]                     # at most 2 browsers at once
_SNATCH_FAILS      = [0]                        # launch failures since the last success      ← added
_SNATCH_COOLDOWN   = [0.0]                      # start of the current pause                   ← added
_SNATCH_COOLDOWN_SEC = 120                      # pause length before the tiers are re-probed  ← added
_SNATCH_SKIP_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "twitch.tv", "twitter.com", "x.com",
                      "tiktok.com", "instagram.com", "facebook.com", "fb.watch", "dailymotion.com",
                      "soundcloud.com", "reddit.com", "bilibili.com", "ok.ru", "vk.com",
                      "rumble.com", "bitchute.com", "odysee.com")

def _snatch_avail() -> bool:                    # ← added
    """Tier availability with a cooldown instead of a permanent latch: two launch failures pause the
    browser leg, after which both tiers are re-probed on the next link. A missing driver is still
    permanent (set by snatch_save), but a stale profile lock or a transient start failure is not."""
    if not SNATCH_ENABLE:
        return False
    if _SNATCH_FAILS[0] < 2:
        return True
    if time.time() - _SNATCH_COOLDOWN[0] > _SNATCH_COOLDOWN_SEC:
        _SNATCH_FAILS[0] = 0
        _SNATCH_AVAIL["nodriver"] = None
        _SNATCH_AVAIL["playwright"] = None
        return True
    return False

def _snatch_note(ok: bool):                     # ← added
    if ok:
        _SNATCH_FAILS[0] = 0
    else:
        _SNATCH_FAILS[0] += 1
        _SNATCH_COOLDOWN[0] = time.time()

# ── Stealth: persistent per-host profile keeps cf_clearance across runs ──
# cf_clearance is bound to IP + User-Agent, so a throwaway context throws it away every time.
CF_PROFILE         = os.getenv("CF_PROFILE", "0") == "1"
CF_HEADFUL         = os.getenv("CF_HEADFUL", "0") == "1"
_CF_LOCK: dict     = {}
_CF_TITLES         = ("just a moment", "attention required", "checking your browser",
                      "einen moment", "bitte warten")
# ── DRM ───────────────────────────────────────────────
DRM_PROBE          = os.getenv("DRM_PROBE", "1") != "0"
WV_DEVICE          = os.getenv("WV_DEVICE", os.path.join(_SCRIPT_DIR, "device.wvd"))
# Reserved, unwired: the one guaranteed rung, one afternoon away if ever needed.
CAPSOLVER_KEY      = os.getenv("CAPSOLVER_KEY", "").strip()

# yt-dlp impersonation (curl_cffi under the hood). CF fingerprints the TLS stack, not just the
# User-Agent, so this is what makes a 403 on a plain page request go away. Gated: an older
# yt-dlp build without the feature must not be handed an option it will choke on.
try:
    from yt_dlp.networking.impersonate import ImpersonateTarget  # noqa: F401
    _YDL_IMPERSONATE = True
except Exception:
    _YDL_IMPERSONATE = False

# ══════════════════════════════════════════════════════
#  INIT & STATE
# ══════════════════════════════════════════════════════
init(autoreset=True)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)

_session = StringSession(SESSION_STRING) if SESSION_STRING else "vault"
tgclient = TelegramClient(
    _session, API_ID, API_HASH,
    device_model="Desktop",
    system_version="Windows 10",
    app_version="1.0",
    connection_retries=5,
)

tgbot = TelegramClient(StringSession(), API_ID, API_HASH)

_KEPT_SOURCES: set = set()   # sources kept because their upload was not confirmed
pending_rename: dict = {}
_uploading: set = set()    # chat ids whose job is in the upload step (Step 4/4) right now
pending_names: dict = {}   # (chat_id, prompt_message_id) -> files.id awaiting an optional name (opt-in "ask for name")
pending_thumb:  dict = {}
pending_mp3:    dict = {}
dl_tasks:       dict = {}
dl_cancel_flags: dict = {}
link_queues:    dict = {}   # chat_id -> asyncio.Queue of pending page URLs (link worker consumes one at a time)
link_workers:   dict = {}   # chat_id -> worker task
pending_links:  dict = {}   # chat_id -> [urls active or queued] for duplicate detection
LINK_RE = re.compile(r"https?://[^\s]+")

# ── Download diagnostics (experimental): per-leg failure facts, shareable by the user ──
dl_diag: dict = {}   # chat_id -> [(leg, stage, detail)]
_TITANVAULT_DEBUG = os.environ.get("TITANVAULT_DEBUG", "").lower() in ("1", "true", "yes")

# ── Leg hit rates: ok / fail per leg, for /stats. Cheap, no per-URL data. ──
_LEG_STATS: dict = {}

def _stat(leg: str, ok: bool):
    row = _LEG_STATS.setdefault(leg, [0, 0])
    row[0 if ok else 1] += 1

def _diag_reset(uid):
    dl_diag[uid] = []

def _redact_url(url: str) -> str:
    """Shareable form: scheme + host only - path and query may carry tokens."""
    try:
        p = urlparse(url or "")
        return f"{p.scheme}://{p.netloc}/..." if p.scheme and p.netloc else "<url>"
    except Exception:
        return "<url>"

def _http_status(err: str) -> str:
    m = re.search(r"HTTP (?:error )?(?:response )?(?:code )?[:# ]*\[?(\d{3})", str(err), re.I)
    return f"HTTP {m.group(1)}" if m else ""

def _exc_where(e) -> str:
    """Where an unexpected exception was raised, as 'function:line' of the innermost frame in this file.
    Code location only: no message text, so no URL or token can leak."""
    try:
        tb = e.__traceback__; here = os.path.abspath(__file__); where = ""
        while tb is not None:
            if os.path.abspath(tb.tb_frame.f_code.co_filename) == here:
                where = f"{tb.tb_frame.f_code.co_name}:{tb.tb_lineno}"
            tb = tb.tb_next
        return where
    except Exception:
        return ""

def _retry_after_from(err):
    """Seconds from a Retry-After header found on the exception (or its causes), or None."""
    import email.utils
    seen, stack = set(), [err]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen: continue
        seen.add(id(e))
        for holder in (getattr(e, "response", None), e):
            h = getattr(holder, "headers", None)
            try: v = h.get("Retry-After") if h is not None else None
            except Exception: v = None
            if v:
                v = str(v).strip()
                if v.isdigit(): return int(v)
                try:
                    dt = email.utils.parsedate_to_datetime(v)
                    return max(0, int(dt.timestamp() - time.time()))
                except Exception: pass
        stack += [getattr(e, "__cause__", None), getattr(e, "__context__", None)]
        ei = getattr(e, "exc_info", None)
        if ei and len(ei) > 1: stack.append(ei[1])
    return None

def _diag_detail(err) -> str:
    """Fixed-vocabulary detail plus HTTP status. Raw exception text never reaches
    diagnostics: it can embed the failing URL with its tokens."""
    low = str(err).lower()
    if "__cancelled__" in low: return "cancelled"
    status = _http_status(err)
    if status == "HTTP 429":
        ra = _retry_after_from(err)
        _rl_seen["ts"] = time.time()
        if ra is not None: _rl_seen["after"] = max(ra, _rl_seen["after"] or 0)
    label = ("timeout" if "timed out" in low or "timeout" in low
             else "connection error" if any(x in low for x in ("connection", "refused", "reset", "unreachable", "name or service"))
             else "download error")
    return status or label

def _classify_hls_stage(err) -> str:
    low = str(err).lower()
    if any(x in low for x in ("fragment", "segment", ".ts", ".m4s")): return "segment fetch"
    if ".m3u8" in low or "manifest" in low or "download webpage" in low: return "playlist fetch"
    return "download"

def _diag(uid, leg: str, stage: str, detail: str):
    if uid not in dl_diag: return
    dl_diag[uid].append((leg, stage, detail))
    if _TITANVAULT_DEBUG:
        logging.info(f"[diag] {leg} | {stage} | {detail}")

def _diag_summary(uid) -> str:
    entries = dl_diag.get(uid) or []
    if not entries: return ""
    lines = ["", "Diagnostics (safe to share - no links or tokens):"]
    eng = [i for i, e in enumerate(entries) if e[0].startswith("HLS via N_m3u8DL-RE")]
    keep = set(eng[-3:])                                   # engine entries are never trimmed away
    for i in range(len(entries) - 1, -1, -1):
        if len(keep) >= 8: break
        keep.add(i)
    for i in sorted(keep):
        leg, stage, detail = entries[i]
        lines.append(f"• {leg}: {stage} - {detail}")
    return "\n".join(lines)
save_queues:    dict = {}
save_workers:   dict = {}
user_thumbnails: dict = {}
user_convert: dict = {}          # uid -> convert spec (see /convert)

batch_queues: dict = {}
user_series_prefix: dict = {}
episode_counters: dict = {}

download_queue: asyncio.Queue = None
active_downloads: dict = {}
url_cache: dict = {}
retry_counts: dict = {}

_last_edit_times = {}
_flood_wait_until = [0.0]
_RL_MAX_TRIES = 3                # tries per link when the source says "slow down" (HTTP 429); after that the link is HELD, not dropped
_held_links: dict = {}           # chat_id -> links parked after repeated HTTP 429 (in memory; /held to resume or clear)
_upload_count: dict = {}         # chat_id -> number of confirmed uploads (to tell success from failure)
_host_cooldown: dict = {}        # host -> time before which no link of that host is started
_rl_tries: dict = {}             # (chat_id, url) -> tries so far
_rl_seen = {"ts": 0.0, "after": None}   # latest HTTP 429 seen by any leg, with the server's Retry-After seconds if given
_user_flood_until = [0.0]

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".avi", ".mov", ".ts", ".m4v", ".flv", ".m4s", ".vid")   # ← .vid added

# ══════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect("vault.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS files (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        msg_id   INTEGER,
        name     TEXT,
        added_at TEXT DEFAULT (datetime('now'))
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS file_footprints (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        file_size INTEGER,
        duration  INTEGER,
        name      TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS kept_jobs (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        path     TEXT,
        title    TEXT,
        chat_id  INTEGER,
        size     INTEGER,
        added_at TEXT DEFAULT (datetime('now'))
    )""")
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    try: c.execute("ALTER TABLE kept_jobs ADD COLUMN state TEXT DEFAULT ''")   # outcome-unknown marker, survives restarts
    except sqlite3.OperationalError: pass
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect("vault.db")

def _ask_name_on(chat_id: int) -> bool:
    """Opt-in per chat, off by default. Off = no naming question, behaviour unchanged."""
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (f"askname:{chat_id}",)).fetchone()
        return bool(row and row[0] == "1")
    except Exception:
        return False

def _ask_name_set(chat_id: int, on: bool):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (f"askname:{chat_id}", "1" if on else "0"))
        conn.commit()

async def _ask_name_prompt(bot, chat_id: int, db_id: int, title: str):
    """After a delivered upload (only when the toggle is on): ask for a visible name. Never blocks the queue;
    a reply to this message renames, Skip or silence keeps the current name."""
    try:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Skip (keep name)", callback_data="ASKN|skip")]])
        m = await bot.send_message(chat_id, f"✏️ Name for this video?\nReply to this message with the name, or tap Skip.\nCurrent: {title[:80]}", reply_markup=kb)
        if len(pending_names) > 200: pending_names.pop(next(iter(pending_names)))
        pending_names[(chat_id, m.message_id)] = db_id
    except Exception as e:
        logging.warning(f"Ask-for-name prompt failed: {type(e).__name__}")

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

# ── Kept sources: files whose upload was NOT confirmed. Bounded by KEEP_CAP_GB. ──
KEEP_CAP_BYTES = int(float(os.getenv("KEEP_CAP_GB", "20")) * 1024 ** 3)

def _inside_job_dir(path: str) -> bool:
    root = os.path.realpath(ARIA2C_DOWNLOAD_DIR)
    p = os.path.realpath(path)
    return p != root and os.path.commonpath([root, p]) == root

def _kept_register(path: str, title: str, chat_id: int, state: str = "") -> int:
    """state: "" for an ordinary failed upload; "unknown" / "notfound" / "candidate" when a cancelled final
    send left it unclear whether Telegram already stored the file (see _finish_upload)."""
    with get_db() as conn:
        cur = conn.execute("INSERT INTO kept_jobs (path, title, chat_id, size, state) VALUES (?,?,?,?,?)",
                           (path, title, chat_id, os.path.getsize(path) if os.path.exists(path) else 0, state))
        conn.commit()
        return cur.lastrowid

def _kept_rows():
    """Registered kept files that still exist (rows for vanished files are dropped)."""
    with get_db() as conn:
        rows = conn.execute("SELECT id, path, title, chat_id, size FROM kept_jobs ORDER BY id").fetchall()
        live = []
        for r in rows:
            if os.path.exists(r[1]): live.append(r)
            else: conn.execute("DELETE FROM kept_jobs WHERE id=?", (r[0],))
        conn.commit()
    return live

def _kept_total() -> int:
    return sum((os.path.getsize(r[1]) if os.path.exists(r[1]) else 0) for r in _kept_rows())

def _kept_blocked() -> bool:
    """Backpressure: new jobs are refused while kept files fill the cap. Nothing is auto-deleted."""
    return _kept_total() >= KEEP_CAP_BYTES

def _kept_state(row_id: int) -> str:
    with get_db() as conn:
        r = conn.execute("SELECT state FROM kept_jobs WHERE id=?", (row_id,)).fetchone()
    return (r[0] or "") if r else ""

def _kept_kb(row_id: int, state: str = ""):
    if state:   # outcome unknown: no plain Retry, only an explicit choice
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬆️ Upload anyway (may duplicate)", callback_data=f"KEPT_FORCE|{row_id}"),
                                      InlineKeyboardButton("🗑 Delete", callback_data=f"KEPT_DEL|{row_id}")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Retry upload", callback_data=f"KEPT_RETRY|{row_id}"),
                                  InlineKeyboardButton("🗑 Delete", callback_data=f"KEPT_DEL|{row_id}")]])

async def _kept_block_notice(reply):
    used = _kept_total()
    await reply(f"⛔ New downloads are paused: {fmt_size(used)} of unsent files are kept (limit {fmt_size(KEEP_CAP_BYTES)}).\n"
                f"Use /kept to Retry or Delete them. Nothing was deleted automatically.")

# ══════════════════════════════════════════════════════
#  HELPERS & SMART MESSAGE EDITOR
# ══════════════════════════════════════════════════════
async def resolve_tg_message(chat_id: int, message_id: int):
    for cl in (tgclient, tgbot):
        try:
            m = await cl.get_messages(chat_id, ids=message_id)
            if m and m.media:
                return m, cl
        except Exception:
            pass
    return None, tgclient

def clean_name(text: str) -> str:
    if not text: return ""
    t = re.sub(
        r"(\[.*?\]|\(.*?\)|1080p|720p|2160p|4k|h265|hevc|x264|x265|mp4|mkv|avi|https?\S+)",
        "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).replace(".", " ").strip()

def fmt_size(b: int) -> str:
    if b >= 1_073_741_824: return f"{b/1_073_741_824:.1f} GB"
    if b >= 1_048_576:     return f"{b/1_048_576:.1f} MB"
    return f"{b/1024:.0f} KB"

def fmt_clock(sec: float) -> str:
    sec = max(0, int(sec))
    if sec >= 3600:
        return f"{sec//3600}:{(sec%3600)//60:02d}:{sec%60:02d}"
    return f"{sec//60}:{sec%60:02d}"


def progress_bar(done: int, total: int, width: int = 16) -> str:
    if total <= 0: return "[" + "░" * width + "] ?"
    pct    = min(1.0, done / total)
    filled = int(width * pct)
    bar    = "▓" * filled + "░" * (width - filled)
    return f"[{bar}] {int(pct*100)}%  {done/1_048_576:.1f}/{total/1_048_576:.1f} MB"

def speed_eta(done: int, total: int, elapsed: float) -> str:
    if elapsed < 0.5 or done == 0: return ""
    spd = done / elapsed
    eta = (total - done) / spd if (spd and total > done) else 0
    s   = f"{spd/1_048_576:.1f} MB/s" if spd > 1_048_576 else f"{spd/1024:.0f} KB/s"
    return f"  {s}  ETA {int(eta)}s"

def prepare_telegram_thumb(image_path: str) -> str:
    if not image_path or not os.path.exists(image_path):
        return ""
    out_path = os.path.splitext(image_path)[0] + "_tgthumb.jpg"
    try:
        with Image.open(image_path) as im:
            im = im.convert("RGB")
            im.thumbnail((320, 320))
            im.save(out_path, "JPEG", quality=85, optimize=True)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path
    except Exception:
        try:
            cmd = [
                FFMPEG_PATH, "-y", "-i", image_path,
                "-vf", "scale='min(320,iw)':'min(320,ih)':force_original_aspect_ratio=decrease",
                "-q:v", "2", out_path
            ]
            subprocess.run(cmd, capture_output=True, timeout=15)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path
        except Exception:
            pass
    return image_path

_FINAL_MAX_WAIT = 30.0

_SEND_SETTLE_SECS = 20  # how long a Cancel waits for an already-sent final upload request to settle
_status_kb: dict = {}   # message key -> keyboard that every progress edit of that message keeps (Cancel button)

async def safe_edit(msg, text: str, reply_markup=None, parse_mode="Markdown", force: bool = False):
    now = time.time()
    if reply_markup is None:
        # edit_text without a markup removes the keyboard: keep the Cancel button through convert/upload edits
        reply_markup = _status_kb.get(_msg_key(msg))
    if now < _flood_wait_until[0] and not force:
        return

    if not force and time.time() - _finalized_at.get(_msg_key(msg), 0.0) < 60:
        return      # a late progress edit must not overwrite a delivered final message
    msg_id = getattr(msg, "message_id", None) or id(msg)
    last_time = _last_edit_times.get(msg_id, 0.0)

    if not force and (now - last_time < 2.0):
        return

    _last_edit_times[msg_id] = now
    try:
        await msg.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except RetryAfter as e:
        _flood_wait_until[0] = time.time() + e.retry_after
    except BadRequest as e:
        # BadRequest subclasses NetworkError, so it used to be swallowed by the clause below and the
        # plain-text retry never ran: a rejected Markdown message was silently dropped.
        if "not modified" in str(e).lower():
            return
        try:
            await msg.edit_text(text, reply_markup=reply_markup, parse_mode=None)
        except Exception:
            pass
    except (NetworkError, TimedOut):
        pass
    except Exception:
        try:
            await msg.edit_text(text, reply_markup=reply_markup, parse_mode=None)
        except Exception:
            pass

_finalized_at: dict = {}   # (chat_id, message_id) -> time a final message was delivered


def _msg_key(msg):
    return (getattr(msg, "chat_id", None), getattr(msg, "message_id", None) or id(msg))


_deferred_finals: set = set()
_FINAL_MAX_DEFER = 3600.0


async def safe_final(msg, text: str, reply_markup=None, parse_mode=None, _deferred: bool = False) -> bool:
    """Deliver a FINAL status message (failure/cancel). Never raises; True when delivered now.

    Plain text by default (parse_mode=None), so formatting characters such as the underscores in
    engine names can never get a final message rejected.
    - RetryAfter within _FINAL_MAX_WAIT -> wait the requested time and retry (max 3 tries).
    - RetryAfter beyond it -> NO further network call now (neither edit nor reply): one fixed
      warning, and a single deferred delivery after the server's deadline (if <= _FINAL_MAX_DEFER).
    - TimedOut/NetworkError -> bounded retries; an edit is idempotent, so a retry after an edit
      that actually landed answers 'not modified', which counts as delivered.
    - Edit still failing -> one new reply (same rules). If the edit had in fact landed but the
      network hid it, that can leave a duplicate final message (accepted, rare).
    Warnings carry only fixed text and exception class names - never exception text/URLs/paths.
    """
    _status_kb.pop(_msg_key(msg), None)   # a final message never keeps the Cancel button
    def _unmodified(exc):
        return "not modified" in str(exc).lower()

    async def _attempt(call, label):
        """-> ('ok', 0) | ('fail', 0) | ('defer', seconds)"""
        plain = False
        for n in range(3):
            try:
                await call(None if plain else parse_mode)
                return "ok", 0
            except RetryAfter as e:
                ra = float(getattr(e, "retry_after", 1) or 1)
                if ra > _FINAL_MAX_WAIT:
                    logging.warning(f"final message {label}: Telegram rate limit of {int(ra)}s is over the wait budget, deferring")
                    return "defer", ra
                logging.warning(f"final message {label}: Telegram rate limit, waiting {int(ra) + 1}s (try {n + 1}/3)")
                await asyncio.sleep(ra + 1)
            except BadRequest as e:      # BadRequest subclasses NetworkError: must be handled first
                if _unmodified(e):
                    return "ok", 0
                if not plain and parse_mode:
                    plain = True      # formatting rejected: retry the same text as plain
                    continue
                logging.warning(f"final message {label}: rejected by Telegram")
                return "fail", 0
            except (NetworkError, TimedOut) as e:
                logging.warning(f"final message {label}: network problem {type(e).__name__} (try {n + 1}/3)")
                await asyncio.sleep(1.0 + n)
            except Exception as e:
                logging.warning(f"final message {label}: failed {type(e).__name__}")
                return "fail", 0
        return "fail", 0

    async def _deferred_task(delay):
        try:
            await asyncio.sleep(delay + 1)
            await safe_final(msg, text, reply_markup, parse_mode, _deferred=True)
        except Exception:
            pass

    res, wait = await _attempt(lambda pm: msg.edit_text(text, reply_markup=reply_markup, parse_mode=pm), "edit")
    if res == "fail":
        logging.warning("final message: edit failed, sending a new reply instead")
        res, wait = await _attempt(lambda pm: msg.reply_text(text, parse_mode=pm), "reply")
    if res == "defer":
        if not _deferred and wait <= _FINAL_MAX_DEFER:
            logging.warning(f"final message: delivery deferred by {int(wait) + 1}s (no network call until then)")
            t = asyncio.ensure_future(_deferred_task(wait)); _deferred_finals.add(t)
            t.add_done_callback(_deferred_finals.discard)
        else:
            logging.warning("final message: rate limit too long, not delivered")
    elif res == "fail":
        logging.warning("final message: could not be delivered to Telegram")
    if len(_finalized_at) > 500:
        _finalized_at.clear()
    _finalized_at[_msg_key(msg)] = time.time()
    return res == "ok"


async def safe_copy(bot, to_chat_id: int, from_chat_id: int, message_id: int, **kwargs):
    for attempt in range(4):
        try:
            return await bot.copy_message(
                chat_id=to_chat_id,
                from_chat_id=from_chat_id,
                message_id=message_id,
                **kwargs,
            )
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except (TimedOut, NetworkError):
            await asyncio.sleep(2)
        except Exception as e:
            logging.warning(f"safe_copy error: {e}")
            break
    return None

def detect_direct_video_ext(url: str):
    parsed = urlparse(url)
    clean_path = unquote(parsed.path).rstrip("/")

    # KVS /get_file/ detection
    if "/get_file/" in url.lower():
        return ".mp4"

    # Standard path extension
    ext = os.path.splitext(clean_path)[1].lower()
    if ext in VIDEO_EXTS:
        return ext

    # Query string parameter values
    for values in parse_qs(parsed.query).values():
        for v in values:
            candidate = os.path.splitext(unquote(v).split("#")[0].rstrip("/"))[1].lower()
            if candidate in VIDEO_EXTS:
                return candidate

    # Anywhere in decoded URL
    low = unquote(url).lower()
    for e in VIDEO_EXTS:
        if (e + "&") in low or (e + "?") in low or (e + "/") in low or low.endswith(e) or (e + "=") in low:
            return e

    return None

def clean_referer(ref_url: str) -> str:
    """Strips tracking queries and fragments from Referer headers to avoid CDN 403 errors."""
    if not ref_url: return ""
    p = urlparse(ref_url)
    return f"{p.scheme}://{p.netloc}{p.path}"

def build_direct_headers(target_url: str, referer: str, ua: str) -> dict:
    ref = clean_referer(referer) or target_url
    p = urlparse(ref)
    hdrs = {"User-Agent": ua, "Referer": ref}
    if p.scheme and p.netloc:
        hdrs["Origin"] = f"{p.scheme}://{p.netloc}"
    return hdrs


# ══════════════════════════════════════════════════════
#  HIGH-SPEED PARALLEL UPLOADER (8 WORKERS)
# ══════════════════════════════════════════════════════
async def fast_upload(client, file_path, progress_callback=None):
    file_size = os.path.getsize(file_path)
    part_size = 512 * 1024
    part_count = math.ceil(file_size / part_size)
    is_big = file_size > 10 * 1024 * 1024
    file_id = struct.unpack('q', os.urandom(8))[0]

    queue = asyncio.Queue()
    for i in range(part_count):
        await queue.put(i)

    uploaded_bytes = [0]
    lock = asyncio.Lock()

    async def worker():
        with open(file_path, "rb") as f:
            while not queue.empty():
                try:
                    part_idx = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                f.seek(part_idx * part_size)
                chunk = f.read(part_size)

                req = (SaveBigFilePartRequest(file_id, part_idx, part_count, chunk)
                       if is_big else
                       SaveFilePartRequest(file_id, part_idx, chunk))

                success = False
                attempts = floods = 0
                while attempts < 3:
                    try:
                        await client(req)
                        success = True
                        break
                    except FloodWaitError as fe:
                        # a flood wait is a definite refusal: wait the full time asked, then send this part again
                        floods += 1
                        _user_flood_until[0] = max(_user_flood_until[0], time.time() + fe.seconds)
                        await asyncio.sleep(fe.seconds + 1)
                    except Exception:
                        attempts += 1
                        await asyncio.sleep(0.5)

                if not success:
                    raise RuntimeError(f"Failed to upload part {part_idx} to Telegram.")

                async with lock:
                    uploaded_bytes[0] += len(chunk)
                    cur_bytes = uploaded_bytes[0]

                if progress_callback:
                    res = progress_callback(cur_bytes, file_size)
                    if asyncio.iscoroutine(res):
                        await res
                queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(TELETHON_UPLOAD_WORKERS)]
    await asyncio.gather(*workers)

    file_name = os.path.basename(file_path)
    if is_big:
        return InputFileBig(file_id, part_count, file_name)
    return InputFile(file_id, part_count, file_name, "")


# ══════════════════════════════════════════════════════
#  PLAYWRIGHT & REQUEST HELPERS
# ══════════════════════════════════════════════════════
_PW_DEGRADED = [False]                          # ← added

def _pw():                                      # ← changed (reports the fallback)
    """Patched Playwright when present, stock Playwright otherwise. Same API, fewer fingerprints.
    Sets _PW_DEGRADED when patchright is missing, so the startup banner stops calling this merely
    "plain playwright" and the reason a tier underperforms is visible."""
    try:
        from patchright.sync_api import sync_playwright
        return sync_playwright
    except Exception:
        _PW_DEGRADED[0] = True
        from playwright.sync_api import sync_playwright
        return sync_playwright

class _NullBrowser:
    def close(self): pass

def _cf_lock(host: str):
    """One persistent profile per host, so same-host links serialise on it and other hosts stay parallel."""
    import threading
    return _CF_LOCK.setdefault(host, threading.Lock())

def _sync_launch_chrome(p, headless: bool = True, profile_host: str = ""):
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ]
    if CF_PROFILE and profile_host:
        d = os.path.join(_SCRIPT_DIR, "profiles", re.sub(r"[^a-z0-9.-]", "_", profile_host.lower()))
        os.makedirs(d, exist_ok=True)
        context = p.chromium.launch_persistent_context(
            d, headless=headless and not CF_HEADFUL, timeout=20000, args=args,
            user_agent=_STANDARD_UA, viewport={"width": 1280, "height": 720}, locale="en-US")
        return (context.browser or _NullBrowser()), context
    browser = p.chromium.launch(headless=headless, timeout=10000, args=args)
    context = browser.new_context(
        user_agent=_STANDARD_UA,
        viewport={"width": 1280, "height": 720},
        locale="en-US"
    )
    return browser, context

_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
window.alert = function() { return null; };
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
try {
    const _q = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (p) => p.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission }) : _q(p);
} catch (e) {}
"""

# ── Response-body capture (XHR + fetch) ───────────────
# Many hosts build the media URL in JS and hand it to fetch/XHR: the URL never appears in
# the markup, so a DOM scrape and a request-URL sniff both miss it. This hook records the
# RESPONSE bodies instead, which is the only place such a URL exists.
# Guards, because a cloned 200 MB video body is how this kills the machine:
#   - per-entry cap 200 KB, total budget 1.5 MB, max 40 entries
#   - a body is only read when its content-type looks textual OR its length is small
#   - XHR reads on loadend only; fetch clones lazily and never blocks the response
_CAP_HOOK = """
(() => {
  if (window.__cap) return;
  const MAX_ENTRIES = 40, MAX_TOTAL = 1500000, MAX_ONE = 200000;
  const TEXTY = /json|text|javascript|ecmascript|mpegurl|x-mpegurl/i;
  window.__cap = []; window.__capBytes = 0;
  const push = (url, ctype, len, body) => {
    try {
      if (!body || typeof body !== "string") return;
      if (body.length > MAX_ONE) return;
      if (len && len > MAX_ONE) return;
      if (ctype && !TEXTY.test(ctype) && body.length > 4096) return;
      if (window.__capBytes + body.length > MAX_TOTAL) return;
      if (window.__cap.length >= MAX_ENTRIES) return;
      window.__capBytes += body.length;
      window.__cap.push({u: String(url), ct: ctype || "", b: body});
    } catch (e) {}
  };
  try {
    const ox = XMLHttpRequest.prototype.open,
          os = XMLHttpRequest.prototype.send,
          oh = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.open = function (m, u) { this.__u = u; this.__h = {}; return ox.apply(this, arguments); };
    XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
      try { this.__h[k] = v; } catch (e) {}
      return oh.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      const self = this;
      self.addEventListener("loadend", () => {
        try {
          const ct = self.getResponseHeader("content-type") || "";
          const cl = parseInt(self.getResponseHeader("content-length") || "0", 10) || 0;
          if (self.responseType === "" || self.responseType === "text") push(self.__u, ct, cl, self.responseText);
          else if (self.responseType === "json") push(self.__u, ct, cl, JSON.stringify(self.response));
        } catch (e) {}
      });
      return os.apply(this, arguments);
    };
  } catch (e) {}
  try {
    const of = window.fetch;
    window.fetch = function (i, init) {
      const u = (typeof i === "string") ? i : (i && i.url);
      return of.apply(this, arguments).then((r) => {
        try {
          const ct = r.headers.get("content-type") || "";
          const cl = parseInt(r.headers.get("content-length") || "0", 10) || 0;
          if (TEXTY.test(ct) || (cl && cl <= MAX_ONE)) {
            r.clone().text().then((t) => push(u, ct, cl, t)).catch(() => {});
          }
        } catch (e) {}
        return r;
      });
    };
  } catch (e) {}
})();
"""

_CAP_READ_JS = "JSON.stringify(window.__cap || [])"

def _classify_capture(entry: dict):
    """A captured response body -> (kind, value).
    kind: 'hls' (body IS a manifest), 'hls_url', 'url', 'base_url' (bare CDN base needing a suffix),
    or (None, None). Domain-agnostic: nothing here knows a host name."""
    b = (entry.get("b") or "").strip()
    u = entry.get("u") or ""
    if not b:
        return None, None
    if b.startswith("#EXTM3U"):
        return "hls", u                      # the request URL of a manifest IS the manifest URL
    if re.search(r"\.m3u8(?:[?#]|$)", u, re.I):
        return "hls_url", u
    # dood pass_md5 shape: one bare base URL and nothing else
    if (not b.startswith("<") and " " not in b and "\n" not in b
            and re.fullmatch(r"https?://[^\s\"'<>]{8,400}", b)):
        return "base_url", b
    try:
        j = json.loads(b)
    except Exception:
        j = None
    if isinstance(j, dict):
        for k in ("file", "url", "src", "hls", "mp4", "sources", "contentUrl", "video_url", "playlist"):
            v = j.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return ("hls_url" if ".m3u8" in v else "url"), v
            if isinstance(v, list):
                for it in v[:5]:
                    if isinstance(it, str) and it.startswith("http"):
                        return ("hls_url" if ".m3u8" in it else "url"), it
                    if isinstance(it, dict):
                        f = it.get("file") or it.get("url")
                        if isinstance(f, str) and f.startswith("http"):
                            return ("hls_url" if ".m3u8" in f else "url"), f
    m = re.search(r'(https?://[^\s"\'<>\\]{10,400}\.(?:mp4|m3u8|mpd|vid|webm)(?:\?[^\s"\'<>\\]*)?)', b, re.I)
    if m:
        return ("hls_url" if ".m3u8" in m.group(1) else "url"), m.group(1)
    return None, None

def _expand_base(base: str, from_url: str = "") -> list:
    """A bare base URL needs a random suffix, and on the hosts that use this shape also the token
    from the path it came from. Three candidates, cheapest first; the probe decides which lives."""
    out = [base]
    rnd = "".join(random.choices(string.ascii_letters + string.digits, k=10))
    out.append(base + rnd)
    tok = urlparse(from_url).path.rstrip("/").rsplit("/", 1)[-1]
    if tok:
        out.append(f"{base}{rnd}?token={tok}&expiry={int(time.time() * 1000)}")
    return out

def _cf_wait(page, budget: float = 25.0) -> bool:
    """Sit out a Cloudflare interstitial, clicking the widget when it is a real checkbox."""
    t0 = time.time()
    while time.time() - t0 < budget:
        try: t = (page.title() or "").lower()
        except Exception: t = ""
        if t and not any(x in t for x in _CF_TITLES):
            return True
        try:
            page.frame_locator("iframe[src*='challenges.cloudflare.com']") \
                .locator("input[type=checkbox], .ctp-checkbox-label").click(timeout=1200)
        except Exception:
            pass
        page.wait_for_timeout(1000)
    return False

async def _cf_wait_nd(tab, budget: float = 45.0) -> bool:
    """nodriver twin of _cf_wait. nodriver has no frame_locator and the widget lives in a
    cross-origin iframe, so the iframe element itself is clicked (its box centre sits on the
    widget) and the title is polled until the wall falls. A challenge page carries no player
    and therefore no media URL, so probing it directly always reads nothing."""
    t0 = time.time()
    while time.time() - t0 < budget:
        try:
            t = (await tab.evaluate("document.title")) or ""
        except Exception:
            t = ""
        if t and not any(x in t.lower() for x in _CF_TITLES):
            return True
        try:
            el = await tab.select("iframe[src*='challenges.cloudflare.com']", timeout=1)
            if el:
                await el.click()
        except Exception:
            pass
        await tab.sleep(1.5)
    return False

_CF_MARKERS = ("just a moment", "checking your browser", "__cf_chl", "cf-browser-verification",
               "attention required", "einen moment", "ddos-guard")

def _looks_like_cf(resp_text: str, headers=None) -> bool:
    """True when a response is a Cloudflare interstitial rather than the page itself.
    A challenge carries no media, so the resolver must not read it as 'site has no stream'."""
    try:
        if (headers or {}).get("cf-mitigated", "").lower() == "challenge":
            return True
    except Exception:
        pass
    low = (resp_text or "")[:4000].lower()
    return any(m in low for m in _CF_MARKERS)

def _cf_get(url: str, referer: str = "", timeout: int = 15):
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers = {
        "User-Agent": _STANDARD_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": _STANDARD_LANG,
        "Referer": clean_referer(referer) or (origin + "/"),
        "Origin": origin,
    }
    try:
        from curl_cffi import requests as _cr
        r = _cr.get(url, impersonate="chrome124", headers=headers, timeout=timeout)
        # CF fingerprints the TLS stack, not the UA: a different profile often walks straight
        # past a challenge the first one drew, which is cheaper than waking a browser.
        if _looks_like_cf(getattr(r, "text", ""), getattr(r, "headers", {})):
            try:
                r2 = _cr.get(url, impersonate="safari17_0", headers=headers, timeout=timeout)
                if len(getattr(r2, "text", "") or "") > 400:
                    return r2
            except Exception:
                pass
        return r
    except Exception:
        headers2 = {"User-Agent": _STANDARD_UA, "Referer": clean_referer(referer) or url}
        return requests.get(url, headers=headers2, timeout=timeout)

def _scrape_with_playwright(url: str, timeout: int = 20) -> str:
    try:
        sync_playwright = _pw()
        with sync_playwright() as p:
            browser, context = _sync_launch_chrome(p, headless=True)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout*1000)
            html = page.content()
            context.close(); browser.close()
            return html
    except Exception as e:
        logging.warning(f"[playwright] failed: {e}")
        return ""

def _unpack_js(packed: str) -> str:
    try:
        m = re.search(r"\}\s*\(\s*'((?:[^'\\]|\\.)*)',\s*(\d+),\s*(\d+),\s*'((?:[^'\\]|\\.)*)'\s*\.split\(", packed, re.S)
        if not m: return ""
        payload = m.group(1).replace("\\'", "'").replace("\\\\", "\\")
        radix   = int(m.group(2))
        words   = m.group(4).split("|")
        def decode_base(s, r):
            chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            val = 0
            for c in s: val = val * r + chars.index(c)
            return val
        def replace_word(m2):
            word = m2.group(0)
            try:
                idx = decode_base(word, radix)
                return words[idx] if idx < len(words) and words[idx] else word
            except Exception: return word
        return re.sub(r'\b\w+\b', replace_word, payload)
    except Exception: return ""

def decode_js_escapes(text: str) -> str:        # ← changed (also unescapes \/)
    """Decodes hex (\\x68), unicode (\\u0068) and JSON slash (\\/) escapes. The slash form is the
    one that matters on WordPress tube themes: the embed sits in a JSON blob as
    "https:\\/\\/voe.sx\\/e\\/id", so a plain https?:// regex matches nothing until it is undone."""
    if not text or ("\\x" not in text and "\\u" not in text and "\\/" not in text):
        return text
    try:
        text = text.replace("\\/", "/")
        text = re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), text)
        text = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), text)
    except Exception:
        pass
    return text

_ATOB_RE = re.compile(r"""atob\(\s*['"]([A-Za-z0-9+/=_\-]{16,})['"]\s*\)""")
_FCC_RE  = re.compile(r"fromCharCode\(([\d,\s]{20,})\)")

def _deobf_strings(texts: list, depth: int = 2) -> list:
    """Static deobfuscation, no browser: peel atob() and String.fromCharCode() blobs.
    Depth-capped and output-capped, so a pathological page cannot run away with it.
    Kills a whole class of lazy hiding before the browser leg is ever reached."""
    out = list(texts)
    cur = list(texts)
    for _ in range(max(0, depth)):
        nxt = []
        for t in cur:
            for m in _ATOB_RE.finditer(t):
                raw = m.group(1).replace("-", "+").replace("_", "/")
                try:
                    nxt.append(base64.b64decode(raw + "=" * ((4 - len(raw) % 4) % 4))
                               .decode("utf-8", errors="ignore"))
                except Exception:
                    pass
            for m in _FCC_RE.finditer(t):
                try:
                    nxt.append("".join(chr(int(c)) for c in m.group(1).split(",") if c.strip().isdigit()))
                except Exception:
                    pass
        if not nxt:
            break
        out.extend(nxt)
        cur = nxt
        if len(out) > 400:
            break
    return out

def _extract_title_from_html(html: str, fallback: str = "") -> str:
    if not html or len(html) < 50: return fallback
    for pat in [
        r'<h1[^>]*>([^<]{3,150})</h1>',
        r'"title"\s*:\s*"([^"]{3,150})"',
        r'title\s*:\s*["\']([^"\']{3,150})["\']',
        r'data-title=["\']([^"\']{3,150})["\']',
    ]:
        m = re.search(pat, html, re.I)
        if m and m.group(1).lower() not in ("undefined", "null", "video", "player"):
            return m.group(1).strip()
    m = re.search(r'<title[^>]*>([^<]{3,200})</title>', html, re.I)
    return m.group(1).strip() if m else fallback

def _parse_netscape_cookies(cookie_file):
    cookies = []
    try:
        with open(cookie_file, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"): continue
                parts = line.split("\t")
                if len(parts) < 7: continue
                domain, _, path, secure, expires, name, value = parts[:7]
                cookies.append({
                    "name": name, "value": value,
                    "domain": domain.lstrip("."), "path": path,
                    "secure": secure.upper() == "TRUE", "sameSite": "None",
                })
    except Exception: pass
    return cookies


# ══════════════════════════════════════════════════════
#  FILENAME RESOLUTION
#  The URL is a hash on every host worth naming, so the name comes from the
#  server's own header first, then the page, then a fallback. Never from the URL.
# ══════════════════════════════════════════════════════
_CD_STAR  = re.compile(r"filename\*\s*=\s*([^;]+)", re.I)
_CD_PLAIN = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.I)
_CD_CHARSET = re.compile(r"^[\w!#$%&+^`{}~-]*'[^']*'")
_ILLEGAL  = re.compile(r'[\\/:*?"<>|\r\n\t]')
_MEDIA_OK = (".mp4", ".mkv", ".webm", ".mov", ".ts", ".m4v", ".vid", ".flv")

def _filename_from_cd(cd: str) -> str:
    """The server's own filename (RFC 5987 first, then the plain form)."""
    if not cd: return ""
    m = _CD_STAR.search(cd)
    if m:
        raw = _CD_CHARSET.sub("", m.group(1).strip().strip('"'))
        return unquote(raw)
    m = _CD_PLAIN.search(cd)
    return m.group(1).strip() if m else ""

def _safe_media_name(raw: str, fallback: str) -> str:
    """Windows-safe, length-capped, forced media extension."""
    n = _ILLEGAL.sub(" ", raw or "")
    n = re.sub(r"\s+", " ", n).strip(" .")[:100]
    if not n: n = fallback
    root, ext = os.path.splitext(n)
    if ext.lower() not in _MEDIA_OK:
        # "My Clip 1.5 Hours" -> splitext eats ".5 Hours"; only a short alnum ext is replaced
        n = (root + ".mp4") if re.fullmatch(r"\.[A-Za-z0-9]{1,5}", ext or "") else (n + ".mp4")
    return n

def _page_title_sync(page) -> str:
    """og:title -> twitter:title -> h1 -> <title>, with the site suffix stripped."""
    for js in ("meta[property='og:title']", "meta[name='twitter:title']"):
        try:
            v = page.eval_on_selector(js, "e => e.content")
            if v and len(v.strip()) > 2: return v.strip()
        except Exception: pass
    try:
        h1 = page.eval_on_selector("h1", "e => e.innerText")
        if h1 and len(h1.strip()) > 2: return h1.strip()
    except Exception: pass
    try:
        t = page.title() or ""
        return re.sub(r"\s*[|\-–]\s*[^|\-–]{2,30}$", "", t).strip()
    except Exception:
        return ""


# ══════════════════════════════════════════════════════
#  DRM DETECTION
#  AES-128 and SAMPLE-AES are free: ffmpeg and yt-dlp decrypt them from the
#  playlist key. Only Widevine/PlayReady need a CDM. Detection is 20 lines and
#  turns three mystery failures into one honest diagnostic line.
# ══════════════════════════════════════════════════════
_WV_SYS = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
_PR_SYS = "urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95"

def _drm_kind(head: str) -> str:
    low = (head or "").lower()
    if _WV_SYS in low or "com.widevine.alpha" in low: return "widevine"
    if _PR_SYS in low or "com.microsoft.playready" in low: return "playready"
    if "sample-aes" in low: return "sample-aes"
    if "#ext-x-key:method=aes-128" in low or '"method":"aes-128"' in low: return "aes-128"
    return ""

def _manifest_head(url: str, hdrs: dict, cookie_hdr: str = "") -> str:
    """First 4 KB of a manifest, for the DRM sniff only."""
    try:
        from curl_cffi import requests as _cr
        h = dict(hdrs); h["Range"] = "bytes=0-4095"
        if cookie_hdr: h["Cookie"] = cookie_hdr
        r = _cr.get(url, headers=h, impersonate="chrome124", timeout=30)
        return (r.text or "")[:8192]
    except Exception:
        return ""

def _wv_cdm():
    """L3 CDM from a device.wvd next to the script (pywidevine create-device ...)."""
    if not os.path.isfile(WV_DEVICE): return None
    try:
        from pywidevine.cdm import Cdm
        from pywidevine.device import Device
        return Cdm.from_device(Device.load(WV_DEVICE))
    except Exception:
        return None


# ══════════════════════════════════════════════════════
#  QUALITY: LABELLED VARIANT LISTS  (playerjs & friends)
#
#  Many tube sites ship one player config listing every rendition as
#  "[label]url,[label]url,..." plus a default_quality that is usually NOT the
#  best. The page's JSON-LD contentUrl points at that same default, so trusting
#  it is the reason a download comes out soft. These helpers rank the list and
#  take the best entry instead.
# ══════════════════════════════════════════════════════
_QLABEL_RE = re.compile(r"\[([^\]\n]{1,24})\]\s*(https?://[^\s,\"'<>\\]+)", re.I)
_QRES_TOKENS = (
    (re.compile(r"\b(?:4k|2160p?|uhd)\b", re.I), 2160),
    (re.compile(r"\b(?:fullhd|fhd|1080p?)\b", re.I), 1080),
    (re.compile(r"\b(?:hd|720p?)\b", re.I), 720),
    (re.compile(r"\b(?:480p?|sd|dvd)\b", re.I), 480),
    (re.compile(r"\b360p?\b", re.I), 360),
    (re.compile(r"\b(?:240p?|low|mobile)\b", re.I), 240),
    (re.compile(r"\b144p?\b", re.I), 144),
)

def _label_rank(label: str, url: str = "") -> int:
    """Height a rendition claims, from its label first, then tokens in the URL. 0 = unknown."""
    for text in (label or "", unquote(url or "")):
        for rx, px in _QRES_TOKENS:
            if rx.search(text):
                return px
        m = re.search(r"(?:^|[^\d])(\d{3,4})\s*[xp](?:[^\d]|$)", text, re.I)
        if m:
            v = int(m.group(1))
            if 100 <= v <= 4320: return v
    # last resort: a numeric "_7.mp4" style suffix orders renditions on several CDNs
    m = re.search(r"_(\d{1,2})\.(?:mp4|mkv|webm|m3u8)(?:$|\?)", url or "", re.I)
    return int(m.group(1)) * 10 if m else 0

def _pick_variant(variants: list, ceiling: int = 0) -> str:
    """variants = [(rank, url)]. Best under the ceiling; the closest above only when nothing fits."""
    variants = [(r, u) for r, u in variants if u]
    if not variants: return ""
    if ceiling:
        low = [v for v in variants if 0 < v[0] <= ceiling]
        if low: return max(low, key=lambda v: v[0])[1]
        over = [v for v in variants if v[0] > ceiling]
        if over: return min(over, key=lambda v: v[0])[1]
    known = [v for v in variants if v[0] > 0]
    if known: return max(known, key=lambda v: v[0])[1]
    return variants[0][1]

def _labelled_variants(html: str, page_url: str = "") -> list:
    """Every '[label]url' rendition in the page text (playerjs file lists), ranked."""
    out, seen = [], set()
    for text in _texts_with_unpacked(html or ""):
        for m in _QLABEL_RE.finditer(text):
            u = m.group(2).replace("&amp;", "&").replace("\\/", "/").replace("\\", "").strip()
            if u in seen: continue
            seen.add(u)
            if page_url and not u.startswith("http"): u = urljoin(page_url, u)
            out.append((_label_rank(m.group(1), u), u))
    return out

def _best_from_html(html: str, page_url: str, ceiling: int = 0) -> str:
    """Best labelled rendition on a page, or ''."""
    v = _labelled_variants(html, page_url)
    return _pick_variant(v, ceiling) if v else ""


# ══════════════════════════════════════════════════════
#  PACKED PLAYER CONFIGS
#  jw8 and friends ship the setup call eval-packed, so a regex on the raw HTML
#  never matches "sources:[{file:...". Unpack first, then apply the patterns to
#  both the page text and the unpacked text.
# ══════════════════════════════════════════════════════
_M3U8_PATS = [
    r'(https?://[^\s"\'<>\\]+/hls2/[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*)',
    r'(https?://[^\s"\'<>\\]+/master\.m3u8[^\s"\'<>\\]*)',
    r'(https?://[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*)',
    r'sources\s*:\s*\[\s*\{[^}]*?["\']?file["\']?\s*:\s*["\']([^\'"]+\.(?:m3u8|mp4)[^\'"]*)["\']',
    r'["\']?file["\']?\s*:\s*["\']([^\'"]+\.(?:m3u8|mp4)[^\'"]*)["\']',
    r'(https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)',
]

def _texts_with_unpacked(html: str) -> list:
    """Page text, the unpacked form of every packed eval() blob on it, and the atob/fromCharCode
    decodings of both. The decoded layer is what catches lazily hidden links before the browser."""
    out = [html]
    for m in re.finditer(r"eval\s*\(\s*function\s*\(\s*p\s*,\s*a\s*,\s*c\s*,\s*k\s*,\s*e\s*", html or ""):
        u = _unpack_js(html[m.start():m.start() + 40000])
        if u and (".m3u8" in u or ".mp4" in u or "sources" in u):
            out.append(u)
    return _deobf_strings(out)

def _stream_from_html(html: str) -> str:
    """First usable media URL in the page, packed or not. Labelled lists win."""
    if not html: return ""
    best = _best_from_html(html, "")
    if best: return best
    for text in _texts_with_unpacked(html):
        for pat in _M3U8_PATS:
            m = re.search(pat, text, re.I)
            if not m: continue
            u = m.group(1).replace("&amp;", "&").replace("\\", "").strip()
            if u.startswith("http") and not any(x in u for x in ("img.", "thumb", ".jpg", ".png", "lulucdn.com/")):
                return u
    return ""


# ══════════════════════════════════════════════════════
#  IN-SESSION BROWSER CAPTURE LEG
#
#  The token is minted by the player and bound to the session's cookies, so the
#  browser that mints it pulls the bytes too. Two tiers, shared tail:
#    tier 1  nodriver   - real Chrome over CDP, no WebDriver, no automation flag.
#                         The only stack that passed the Turnstile target in the
#                         2026 benchmark; media found through resource timing.
#    tier 2  patchright - patched Chromium, request/response interception.
#  Both hand off to _snatch_finish, so naming, validation and the cookie/UA
#  lockstep exist once. Range is stripped (a replayed player header would give a
#  206 single chunk that still passes a size check), Cookie is stripped (the
#  context attaches its own), and the Content-Disposition header is the name.
#
#  Both tiers also install _CAP_HOOK, which records XHR/fetch RESPONSE bodies.
#  That is the only place a URL built in JS exists: a request-URL sniff and a DOM
#  scrape both miss it. Captures are classified into concrete URLs and merged into
#  the candidate list with the initiating request's headers attached.
# ══════════════════════════════════════════════════════
_DROP_HDRS = {"host", "content-length", "cookie", "connection",
              "range", "if-range", "accept-encoding", "transfer-encoding"}
_MEDIA_CT  = ("video/mp4", "video/mp2t", "video/webm", "video/x-matroska",
              "application/vnd.apple.mpegurl", "application/x-mpegurl", "mpegurl",
              "video/octet-stream", "application/octet-stream", "binary/octet-stream")

_PROBE_JS = """JSON.stringify(
  performance.getEntriesByType('resource').map(e => e.name)
    .filter(n => /\\.mp4|\\.m3u8|\\.mpd|\\/video~|get_file|doodcdn|voe-network|lulu|voe\\.[a-z]|dood\\.[a-z]|streamwish|vidhide|filemoon/.test(n)))"""

def _hdr_safe(v) -> str:
    return re.sub(r"[\r\n]", " ", str(v)).strip()[:1000]

def _replay_headers(req) -> dict:
    try: raw = dict(req.all_headers())
    except Exception: raw = dict(req.headers)
    return {k: _hdr_safe(v) for k, v in raw.items() if k.lower() not in _DROP_HDRS}

def _snatch_fetch(url: str, hdrs: dict, cookie_hdr: str, dest: str, cancel_flag) -> tuple:
    """Stream to disk in the captured request's own identity. Returns (ok, content_disposition).
    One request: no RAM cliff, and the CD header still arrives."""
    from curl_cffi import requests as _cr
    h = dict(hdrs)
    if cookie_hdr: h["Cookie"] = cookie_hdr
    try:
        r = _cr.get(url, headers=h, impersonate="chrome124", timeout=90, stream=True)
    except Exception:
        return False, ""
    try:
        if r.status_code not in (200, 206): return False, ""
        ctype = (r.headers.get("content-type") or "").lower()
        if ctype and not any(x in ctype for x in _MEDIA_CT): return False, ""
        try:
            if int(r.headers.get("content-length") or 0) > _SNATCH_MAX_BYTES: return False, ""
        except Exception: pass
        cd = r.headers.get("content-disposition", "") or ""
        got = 0
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(262144):
                if cancel_flag and cancel_flag[0]: return False, ""
                if not chunk: continue
                fh.write(chunk); got += len(chunk)
                if got > _SNATCH_MAX_BYTES: break
        return got > 200_000, cd
    finally:
        try: r.close()
        except Exception: pass

def _snatch_fetch_ctx(context, url, hdrs, dest, cancel_flag) -> tuple:
    """Fallback for hosts that refuse the impersonation fingerprint: let the live context do it,
    but only when the body is small enough to buffer."""
    try:
        r = context.request.get(url, headers=hdrs, timeout=120000)
        if r.status not in (200, 206): return False, ""
        try:
            if int(r.headers.get("content-length") or 0) > _SNATCH_RAM_CAP: return False, ""
        except Exception: return False, ""
        if cancel_flag and cancel_flag[0]: return False, ""
        body = r.body()
        if len(body) < 200_000: return False, ""
        with open(dest, "wb") as fh: fh.write(body)
        return True, r.headers.get("content-disposition", "") or ""
    except Exception:
        return False, ""

def _snatch_finish(hits, title, ck, ua, page_url, dest_dir, uid, title_hint,
                   cancel_flag=None, context=None, captures=None, hdr_map=None) -> tuple:
    """Shared tail: merge captures, pick, fetch, name from Content-Disposition, probe-gate.
    Returns (path, name)."""
    def _dead():
        return bool(cancel_flag and cancel_flag[0])

    title = title or title_hint
    fallback = f"snatch_{uid}_{int(time.time())}_{urlparse(page_url).netloc.replace('.', '_')[:36]}"

    # Captured response bodies: the URL was assembled in JS, so the body is the only copy of it.
    # Each capture becomes a concrete URL carrying the initiating request's own headers; without
    # those, a signed URL 403s and the leg looks like it failed when it actually succeeded.
    if captures:
        seen_u = {h[0] for h in hits}
        added = 0
        for ent in captures:
            kind, val = _classify_capture(ent)
            if not kind:
                continue
            base_h = dict((hdr_map or {}).get(ent.get("u") or "", {}))
            if not base_h:
                # nodriver has no per-request header map: fall back to the page's own identity
                # rather than handing the CDN a bare request.
                base_h = {"Referer": clean_referer(page_url), "User-Agent": ua}
            if kind in ("hls", "hls_url"):
                cand = [ent.get("u") or ""]
            elif kind == "url":
                cand = [val]
            else:
                cand = _expand_base(val, ent.get("u") or "")
            for u_ in cand:
                if u_ and u_ not in seen_u:
                    seen_u.add(u_)
                    hits.append((u_, base_h))
                    added += 1
        if added:
            _diag(uid, "browser capture", "bodies", f"{added} candidate URL(s) recovered from JS traffic")

    if not hits:
        _stat("browser capture", False)
        return None, title
    hits.sort(key=lambda h: ("/video~" in h[0], ".mp4" in h[0], "doodcdn" in h[0],
                             ".m3u8" in h[0], ".ts" in h[0]), reverse=True)

    # An embed page (VOE, Dood, ...) is not media itself: its scraper knows the real URL.
    # Runs before the fetch loop so the resolved file sorts ahead of the embed page.
    for url, hdrs in list(hits):
        if _is_known_embed_host(url) and not detect_direct_video_ext(url):
            try:
                sub, sub_t, _sub_ref = _scrape_real_url(url)
            except Exception:
                continue
            if sub and sub != url and detect_direct_video_ext(sub):
                title = title or sub_t
                hits.insert(0, (sub, {"Referer": clean_referer(url), "User-Agent": ua}))

    # An embed is not media, and its host may be a mirror the lists have never seen: resolve
    # it through the deep resolver and put the stream in front of the queue. The resolver also
    # matches the player-shell shape (embed.php?video=N), which is not an embed id.   ← changed
    resolved = []
    for _u, _h in list(hits):
        if _is_known_embed_host(_u) or _EMBED_ID_RE.match(_u) or _api_endpoint_for(_u):
            st, _t, rf = _resolve_embed_deep(_u, _u, "", 0, uid)   # ← uid passed
            if st:
                _diag(uid, "snatch", "embed", f"resolved {redact_url(_u)}")
                resolved.append((st, {"Referer": clean_referer(rf or _u), "User-Agent": _STANDARD_UA}))
    if resolved:
        hits[:0] = resolved

    hls, ctx_ok = None, True
    for url, hdrs in hits:
        if _dead(): break
        if ".m3u8" in url or ".mpd" in url:
            hls = hls or (url, hdrs); continue
        hdrs.setdefault("User-Agent", ua)
        hdrs.setdefault("Referer", clean_referer(page_url))
        dest = os.path.join(dest_dir, _safe_media_name(title, fallback))
        if os.path.exists(dest):
            r0, e0 = os.path.splitext(dest)
            dest = f"{r0}_{uid}_{int(time.time())}{e0}"
        ok, cd = _snatch_fetch(url, hdrs, ck, dest, cancel_flag)
        if not ok and ctx_ok and context is not None:
            ok, cd = _snatch_fetch_ctx(context, url, hdrs, dest, cancel_flag)
            if not ok: ctx_ok = False        # one fingerprint refusal is enough to know
        if not ok:
            try: os.remove(dest)
            except Exception: pass
            continue
        name = _filename_from_cd(cd) or title
        final = os.path.join(dest_dir, _safe_media_name(name, fallback))
        if final != dest:
            try: os.replace(dest, final)
            except Exception: final = dest
        if _probe_has_media_stream(final):
            _stat("browser capture", True)
            return final, os.path.splitext(os.path.basename(final))[0]
        try: os.remove(final)
        except Exception: pass

    # HLS: replay the session's cookies into ffmpeg while the context is still open
    if hls and not _dead():
        url, hdrs = hls
        base = os.path.join(dest_dir, _safe_media_name(title, fallback))
        hdr_s = f"Cookie: {_hdr_safe(ck)}\r\n"
        for k in ("Referer", "Origin", "User-Agent", "Accept"):
            if hdrs.get(k): hdr_s += f"{k}: {_hdr_safe(hdrs[k])}\r\n"
        deadline = time.time() + max(60, SNATCH_MAX_SEC - 50)
        # mp4 copy first; HEVC/AC3 sources can refuse mp4, so mkv copy is tried before giving up
        for ext, extra in ((".mp4", ["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"]),
                           (".mkv", [])):
            dest = base + ext
            cmd = [FFMPEG_PATH, "-y", "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                   "-headers", hdr_s, "-i", url, "-c", "copy", *extra, dest]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                while proc.poll() is None:
                    if _dead() or time.time() > deadline:
                        proc.kill(); break
                    time.sleep(1.0)
            except Exception:
                pass
            if os.path.exists(dest) and _probe_has_media_stream(dest):
                _stat("browser capture", True)
                return dest, os.path.splitext(os.path.basename(dest))[0]
            if os.path.exists(dest):
                try: os.remove(dest)
                except Exception: pass
            if _dead() or time.time() > deadline: break
    _stat("browser capture", False)
    return None, title


def _snatch_save_sync(page_url: str, dest_dir: str, uid: int, title_hint: str = "",
                      cancel_flag: list = None) -> tuple:
    """Tier 2: patchright/playwright, request + response interception and body capture.
    Returns (path, name) or (None, "")."""
    sync_playwright = _pw()
    host = urlparse(page_url).netloc
    lock = _cf_lock(host) if CF_PROFILE else None
    if lock: lock.acquire()
    try:
        with sync_playwright() as p:
            browser, context = _sync_launch_chrome(p, headless=not CF_HEADFUL,
                                                   profile_host=host if CF_PROFILE else "")
            try:
                page = context.new_page()
                page.add_init_script(_STEALTH_INIT)
                page.add_init_script(_CAP_HOOK)
                hits, seen, title, hdr_map, embeds = [], set(), "", {}, []   # ← own embeds list

                def _record(req):
                    u = req.url
                    hdr_map[u] = _replay_headers(req)      # every request, filtered or not
                    low = u.split("?")[0].lower()
                    if any(x in low for x in ("thumb", "poster", "sprite", ".jpg", ".png", ".gif",
                                              "favicon", "analytics")):
                        return
                    if not any(x in low for x in (".mp4", ".m3u8", ".mpd", ".ts", "/video~",
                                                  "doodcdn", "voe-network", "/get_file/",
                                                  "voe.", "dood.", "streamwish", "vidhide", "filemoon")):
                        return
                    if u in seen: return
                    seen.add(u)
                    hits.append((u, hdr_map[u]))

                def _record_resp(resp):
                    # opaque media: cdn.x/video/abc with a video content-type and no extension
                    ctype = (resp.headers.get("content-type") or "").lower()
                    if not any(x in ctype for x in _MEDIA_CT): return
                    hdr_map[resp.url] = _replay_headers(resp.request)
                    if resp.url in seen: return
                    seen.add(resp.url)
                    hits.append((resp.url, hdr_map[resp.url]))

                context.on("request", _record)
                context.on("response", _record_resp)

                try: page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
                except Exception: pass
                try: _t0 = (page.title() or "").lower()
                except Exception: _t0 = ""
                if any(x in _t0 for x in _CF_TITLES):
                    if not _cf_wait(page):
                        _diag(uid, "browser leg", "challenge",
                              "Cloudflare wall did not clear within the wait")

                # Read the embed out of the page source first: no click, so nothing can
                # navigate the tab away from the video page.
                try:
                    _html_now = page.content()
                except Exception:
                    _html_now = ""
                _embeds = _scrape_player_config(_html_now, page_url) or _scrape_embed_urls(_html_now, loose=True)
                if _embeds:
                    _diag(uid, "browser leg", "embed", f"{len(_embeds)} embed url(s) in the page source")
                    for _e in _embeds:
                        if not any(u == _e for u, _h in embeds):
                            embeds.append((_e, {"Referer": clean_referer(page_url), "User-Agent": _STANDARD_UA}))

                _start_path = urlparse(page_url).path.rstrip("/")
                # Scoped to the player box: a bare a[href*='download'] is often a "Watch later"
                # anchor that navigates the whole tab away from the video page.   ← changed
                for sel in ("#video-player-wrap-control button.vjs-big-play-button",
                            "#video-player-wrap-control .jw-display-icon-display",
                            "#video-player-wrap-control .fp-play",
                            "#video-player-wrap-control video",
                            "video", "#kt_player", ".fp-play"):
                    if cancel_flag and cancel_flag[0]: break
                    try:
                        el = page.query_selector(sel)
                        if el:
                            el.click(timeout=700, no_wait_after=True)
                            page.wait_for_timeout(1200)
                    except Exception: pass
                # A click on an anchor can leave the page (a Watch later link is enough), so
                # the tab is put back on the video page unconditionally before probing.
                try:
                    if urlparse(page.url).path.rstrip("/") != _start_path:
                        _diag(uid, "browser leg", "activation", "clicks navigated away, returning to the page")
                    page.goto(page_url, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
                # Bounded settle, leaving as soon as a real hit or a page-source embed exists  ← changed
                for _ in range(14):
                    if (cancel_flag and cancel_flag[0]) or embeds \
                            or any(".mp4" in h[0] or "/video~" in h[0] for h in hits):
                        break
                    page.wait_for_timeout(500)

                title = _page_title_sync(page) or title_hint
                try:
                    ck = "; ".join(f"{c['name']}={c['value']}" for c in context.cookies())
                except Exception:
                    ck = ""
                try:
                    ua = page.evaluate("navigator.userAgent") or _STANDARD_UA
                except Exception:
                    ua = _STANDARD_UA
                try:
                    caps = json.loads(page.evaluate(_CAP_READ_JS) or "[]")
                except Exception:
                    caps = []
                if embeds:
                    hits[:0] = embeds
                return _snatch_finish(hits, title, ck, ua, page_url, dest_dir, uid,
                                      title_hint, cancel_flag, context, caps, hdr_map)
            finally:
                try: context.close()
                except Exception: pass
                try: browser.close()
                except Exception: pass
    finally:
        if lock: lock.release()


def _one_line(e) -> str:
    """Exception text on one line, with the ASCII banner dashes dropped."""
    parts = [l.strip() for l in str(e).splitlines() if l.strip() and set(l.strip()) - {"-"}]
    return " ".join(parts)[:150] or type(e).__name__

def _clear_profile_locks(d: str):
    """A crashed Chrome leaves a lock behind; the next launch then dies in 13 s with
    'Failed to connect to browser'. Clearing is safe: nothing is holding it."""
    for n in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        try: os.remove(os.path.join(d, n))
        except Exception: pass


def _snatch_nodriver_sync(page_url: str, dest_dir: str, uid: int, title_hint: str = "",
                          cancel_flag: list = None) -> tuple:
    """Tier 1: real Chromium over CDP. asyncio.run() is safe here, we are in a worker thread.
    The binary comes from _find_browser_bin() - nodriver has none of its own, and without an
    explicit path it cannot launch at all on a machine without Chrome in a standard location.
    Launch is attempted three times: stale locks cleared, profile as-is, throwaway profile."""
    import nodriver as uc
    host = urlparse(page_url).netloc
    exe = _find_browser_bin()

    async def _go():
        prof = os.path.join(_SCRIPT_DIR, "profiles", re.sub(r"[^a-z0-9.-]", "_", host.lower()))
        os.makedirs(prof, exist_ok=True)

        async def _launch(prof_dir):
            kw = dict(headless=not CF_HEADFUL, user_data_dir=prof_dir,
                      browser_args=["--no-sandbox", "--disable-dev-shm-usage",
                                    "--disable-blink-features=AutomationControlled"])
            if exe:
                kw["browser_executable_path"] = exe
            return await uc.start(**kw)

        browser, last_err = None, ""
        tmp_prof = tempfile.mkdtemp(prefix="tvnd_")
        for prof_dir, clear in ((prof, True), (prof, False), (tmp_prof, False)):
            if clear:
                _clear_profile_locks(prof_dir)
            try:
                browser = await _launch(prof_dir)
                break
            except Exception as e:
                last_err = _one_line(e)
                await asyncio.sleep(1.0)
        if browser is None:
            raise RuntimeError(f"launch: {last_err or 'no browser'}")
        try:
            try:
                tab = await browser.get(page_url)
            except Exception as e:
                logging.warning(f"[nodriver] navigation: {type(e).__name__}: {_one_line(e)}")
                tab = await browser.get("about:blank")
                try:
                    await tab.get(page_url)
                except Exception as e2:
                    logging.warning(f"[nodriver] retry navigation: {type(e2).__name__}")
            await tab.sleep(2)
            try:
                from nodriver_turnstile import solve_turnstile
                if await solve_turnstile(tab):        # no-op when there is no widget
                    await tab.sleep(1)
            except Exception:
                pass
            # A challenge page has no player and therefore no media URL: wait the wall out
            # before installing the hook and probing, or the probe just reads an empty page.
            if not await _cf_wait_nd(tab):
                _diag(uid, "browser leg (nodriver)", "challenge",
                      "Cloudflare wall did not clear within the wait")
            # install the body hook before the clicks: the token fetch happens on play
            try:
                await tab.evaluate(_CAP_HOOK)
            except Exception:
                pass
            # Page source first, then controls. nodriver has no :has-text(), so plain CSS only.
            embeds = []
            try:
                _html_now = await tab.get_content()
            except Exception:
                _html_now = ""
            _embeds = _scrape_player_config(_html_now, page_url) or _scrape_embed_urls(_html_now, loose=True)
            if _embeds:
                _diag(uid, "browser leg (nodriver)", "embed", f"{len(_embeds)} embed url(s) in the page source")
                for _e in _embeds:
                    if _e not in embeds:
                        embeds.append(_e)

            # Player box first; the unscoped selectors stay as fallbacks.   ← changed
            for sel in ("#video-player-wrap-control video",
                        "#video-player-wrap-control button.vjs-big-play-button",
                        "#video-player-wrap-control .fp-play",
                        "video", "#kt_player", ".fp-play", ".play-button"):
                if cancel_flag and cancel_flag[0]: break
                try:
                    el = await tab.select(sel, timeout=3)
                    if el:
                        await el.click(); await tab.sleep(1)
                except Exception: pass
            # Put the tab back on the video page before probing, whatever the clicks did.
            try:
                if urlparse(tab.url or "").path.rstrip("/") != urlparse(page_url).path.rstrip("/"):
                    _diag(uid, "browser leg (nodriver)", "activation", "clicks navigated away, returning to the page")
                await tab.get(page_url)
                await tab.sleep(1.5)
            except Exception:
                pass
            hits = []
            for _ in range(12):
                if cancel_flag and cancel_flag[0]: break
                try:
                    hits = json.loads(await tab.evaluate(_PROBE_JS) or "[]")
                except Exception:
                    hits = []
                if hits or embeds: break
                await tab.sleep(1)
            hits = embeds + hits
            try: title = (await tab.evaluate("document.title")) or title_hint
            except Exception: title = title_hint
            try: ck = "; ".join(f"{c.name}={c.value}" for c in await browser.cookies.get_all())
            except Exception: ck = ""
            try: ua = (await tab.evaluate("navigator.userAgent")) or _STANDARD_UA
            except Exception: ua = _STANDARD_UA
            try: caps = json.loads(await tab.evaluate(_CAP_READ_JS) or "[]")
            except Exception: caps = []
            return [(u, {}) for u in hits], title, ck, ua, caps
        finally:
            try: browser.stop()
            except Exception: pass

    lock = _cf_lock(host) if CF_PROFILE else None
    if lock: lock.acquire()
    try:
        hits, title, ck, ua, caps = asyncio.run(_go())
    except Exception as e:
        msg = _one_line(e)
        logging.warning(f"[nodriver] {type(e).__name__}: {msg}")
        if msg.startswith("launch:"):
            raise RuntimeError(msg)
        _diag(uid, "browser leg (nodriver)", "download", msg or type(e).__name__)
        return None, title_hint
    finally:
        if lock: lock.release()
    return _snatch_finish(hits, title, ck, ua, page_url, dest_dir, uid, title_hint,
                          cancel_flag, None, caps, None)


def _snatch_sem():
    if _SNATCH_SEM[0] is None:
        _SNATCH_SEM[0] = asyncio.Semaphore(2)
    return _SNATCH_SEM[0]


async def snatch_save(page_url: str, uid: int, title_hint: str = "", cancel_flag: list = None) -> tuple:
    """Try the tiers in order. A missing driver latches its tier off for the process; a launch
    failure only pauses the leg (see _snatch_avail), so one bad start no longer kills it for the
    whole run. Tier 1 is skipped outright when no Chromium binary exists."""
    if not SNATCH_ENABLE:
        return None, title_hint
    if not _snatch_avail():                                    # ← changed
        _diag(uid, "browser leg", "skipped", "paused after launch failures, tiers will be re-probed")
        return None, title_hint
    try:
        async with _snatch_sem():
            if NODRIVER_TIER and _SNATCH_AVAIL["nodriver"] is not False:
                if not _find_browser_bin():
                    _SNATCH_AVAIL["nodriver"] = False
                    _diag(uid, "browser leg (nodriver)", "launch",
                          "no Chrome/Edge binary found (install Chrome or set CHROME_PATH)")
                else:
                    try:
                        res = await asyncio.wait_for(
                            asyncio.to_thread(_snatch_nodriver_sync, page_url, ARIA2C_DOWNLOAD_DIR,
                                              uid, title_hint, cancel_flag), timeout=SNATCH_MAX_SEC)
                        _SNATCH_AVAIL["nodriver"] = True
                        _snatch_note(True)                     # ← added
                        if res[0]:
                            return res
                        _diag(uid, "browser leg (nodriver)", "download", "no media candidate on the page")
                    except asyncio.TimeoutError:
                        _diag(uid, "browser leg (nodriver)", "download", "timeout")
                    except Exception as e:
                        _m = _one_line(e)
                        if type(e).__name__ in ("ModuleNotFoundError", "ImportError"):
                            _SNATCH_AVAIL["nodriver"] = False
                            _diag(uid, "browser leg (nodriver)", "launch", "nodriver not installed")
                        elif _m.startswith("launch:"):
                            _SNATCH_AVAIL["nodriver"] = False
                            _snatch_note(False)                # ← pauses instead of latching   ← added
                            _diag(uid, "browser leg (nodriver)", "launch", _m)
                        else:
                            _diag(uid, "browser leg (nodriver)", "download", _m)
            if _SNATCH_AVAIL["playwright"] is not False:
                try:
                    res = await asyncio.wait_for(
                        asyncio.to_thread(_snatch_save_sync, page_url, ARIA2C_DOWNLOAD_DIR,
                                          uid, title_hint, cancel_flag), timeout=SNATCH_MAX_SEC)
                    _SNATCH_AVAIL["playwright"] = True
                    _snatch_note(True)                         # ← added
                    if res[0]:
                        return res
                    _diag(uid, "browser leg", "download", "no media candidate on the page")
                except asyncio.TimeoutError:
                    _diag(uid, "browser leg", "download", "timeout")
                except Exception as e:
                    _m = _one_line(e)
                    if type(e).__name__ in ("Error", "DriverNotFoundError") or "playwright" in str(e).lower():
                        _SNATCH_AVAIL["playwright"] = False
                        _snatch_note(False)                    # ← pauses instead of latching   ← added
                        _diag(uid, "browser leg", "launch", "browser unavailable")
                    else:
                        _diag(uid, "browser leg", "download", _m)
    except Exception as e:
        _diag(uid, "browser leg", "download", _diag_detail(e))
    return None, title_hint


# ── SHORTLINK & REDIRECT UNROLLER ─────────────────────
def unroll_shortener(url: str) -> str:
    """Follows a shortener or a redirector (/enlace/redireccion/, /redir, /go/) to its destination."""
    SHORTENER_DOMAINS = ("bit.ly", "tinyurl.com", "t.co", "is.gd", "buff.ly", "goo.gl", "ow.ly", "cutt.ly")
    try:
        domain = urlparse(url).netloc.lower().removeprefix("www.")
        triggers = (any(domain == d for d in SHORTENER_DOMAINS)
                    or any(k in url for k in ("/redirect", "/go/", "/redir", "/enlace/", "redireccion")))
        if triggers:
            for stream in (False, True):
                try:
                    r = requests.get(url, allow_redirects=True, timeout=8, stream=stream,
                                     headers={"User-Agent": _STANDARD_UA})
                    if r.url and r.url != url:
                        return r.url
                except Exception:
                    pass
    except Exception:
        pass
    return url


# ── AUTOMATIC BASE64 & PARAMETER UNPACKER ─────────────
def unwrap_redirect_parameters(target_url: str) -> str:
    """Automatically unpacks hidden links inside redirect parameters or Base64 strings."""
    try:
        parsed = urlparse(target_url)
        qs = parse_qs(parsed.query)

        for param_vals in qs.values():
            for val in param_vals:
                if val.startswith("http://") or val.startswith("https://"):
                    return val
                try:
                    padded = val + "=" * ((4 - len(val) % 4) % 4)
                    decoded = base64.b64decode(padded).decode("utf-8", errors="ignore").strip()
                    if decoded.startswith("http://") or decoded.startswith("https://"):
                        return decoded
                except Exception:
                    pass
    except Exception:
        pass
    return target_url


# ── METADATA & SCHEMA SNIFFER (NO EMBED URLs) ─────────
def _scrape_schema_and_og(html: str, page_url: str) -> tuple[str, str]:
    """Extracts direct media links from OpenGraph, Twitter Cards, and JSON-LD schemas.
    A labelled multi-quality list on the page outranks all of them: schema contentUrl is
    routinely the site's default_quality, not the best rendition."""
    if not html: return "", ""
    title = _extract_title_from_html(html)

    labelled = _best_from_html(html, page_url, _ACTIVE_CEILING[0])
    if labelled:
        return labelled, title

    og_pats = [
        r'<meta[^>]+property=["\']og:video(?::url|:secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video(?::url|:secure_url)?["\']',
        r'<meta[^>]+name=["\']twitter:player:stream["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pat in og_pats:
        m = re.search(pat, html, re.I)
        if m:
            candidate = urljoin(page_url, m.group(1).replace("&amp;", "&").strip())
            if any(ext in candidate.lower() for ext in (".mp4", ".m3u8", ".webm", ".ts", ".vid", "/get_file/")):
                if "/embed" not in candidate.lower():
                    return candidate, title

    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
        try:
            data = json.loads(m.group(1).strip())
            items = data if isinstance(data, list) else [data]
            for it in items:
                content_url = it.get("contentUrl")
                if content_url and isinstance(content_url, str):
                    full = urljoin(page_url, content_url)   # sxyprn & friends declare a RELATIVE path
                    if any(ext in full.lower() for ext in (".mp4", ".m3u8", ".webm", ".ts", ".vid", "/get_file/")):
                        return full, it.get("name") or title
        except Exception:
            pass

    return "", title


# ══════════════════════════════════════════════════════
#  KVS, FLASHVARS & UNIVERSAL EMBED SCRAPERS
# ══════════════════════════════════════════════════════
def _scrape_kvs_and_html5(html: str, page_url: str) -> tuple[str, str]:
    if not html: return "", ""
    title = _extract_title_from_html(html)

    # playerjs-style labelled list first: it carries every rendition explicitly
    best = _best_from_html(html, page_url, _ACTIVE_CEILING[0])
    if best:
        return best, title

    # KVS flashvars. The variable name IS the quality: video_url is the base copy,
    # video_alt_url is one step up, video_alt_url2/3 are the high ones.
    kvs_pats = [
        (r'''video_alt_url3\s*:\s*['"]((?:function/\d+/)?(?:https?://|/)[^'"]+)['"]''', 3),
        (r'''video_alt_url2\s*:\s*['"]((?:function/\d+/)?(?:https?://|/)[^'"]+)['"]''', 2),
        (r'''video_alt_url\s*:\s*['"]((?:function/\d+/)?(?:https?://|/)[^'"]+)['"]''', 1),
        (r'''video_url\s*:\s*['"]((?:function/\d+/)?(?:https?://|/)[^'"]+)['"]''', 0),
        (r'''['"]((?:https?://|/)[^'"]*?/get_file/[^'"]+)['"]''', 0),
    ]

    ranked, seen = [], set()
    for pat, rank in kvs_pats:
        for m in re.finditer(pat, html, re.I):
            raw = m.group(1).replace('\\/', '/').replace('&amp;', '&').strip()
            clean = re.sub(r'^function/\d+/', '', raw)
            if not clean: continue
            full = urljoin(page_url, clean)
            if full in seen: continue
            seen.add(full)
            ranked.append((rank, full))

    if ranked:
        def _score(x):
            low = x[1].lower()
            res = 100 if "1080" in low or "fullhd" in low else 80 if "720" in low else 50 if "480" in low else 10
            return (x[0], res, _label_rank("", x[1]))
        ranked.sort(key=_score, reverse=True)
        return ranked[0][1], title

    for tag_pat in [
        r'''<source[^>]+src=['"]([^'"]+)['"]''',
        r'''<video[^>]+src=['"]([^'"]+)['"]''',
    ]:
        cands = []
        for m in re.finditer(tag_pat, html, re.I):
            src = m.group(1).replace('&amp;', '&').strip()
            if any(ext in src.lower() for ext in ('.mp4', '.m3u8', '.webm', '.vid', '/get_file/')):
                full = urljoin(page_url, src)
                if full not in cands:
                    cands.append(full)
        if cands:
            return _pick_variant([(_label_rank("", c), c) for c in cands], _ACTIVE_CEILING[0]), title

    return "", title

# DLE-style lazyload puts the iframe src on a DIV and tags it data-original-tag="iframe".
# A meta http-equiv=refresh inside that wrapper carries the real embed. Both are needed:
# matching only <iframe src= finds nothing on those pages.
_LAZY_IFRAME_RE = re.compile(r'<(div|span|p|section)[^>]*data-original-tag=["\']iframe["\'][^>]*>', re.I)
_REFRESH_RE     = re.compile(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=([^"\'>\s]+)', re.I)
_IFRAME_BLOCK   = ("google", "facebook", "twitter", "disqus", "ads", "histats", "analytics",
                   "doubleclick", "yandex", "mail.ru", "matomo", "top100")

def _scrape_iframes(html: str, page_url: str) -> list[str]:
    """iframe src, DLE-style lazy wrappers (src on a div), and meta-refresh hops."""
    found = []

    def _add(src):
        src = src.replace("&amp;", "&").strip()
        if src.startswith("//"): src = "https:" + src
        elif not src.startswith("http"): src = urljoin(page_url, src)
        low = src.lower()
        if not low.startswith(("http://", "https://")): return
        if any(b in low for b in _IFRAME_BLOCK): return
        if src not in found: found.append(src)

    for m in re.finditer(r'<iframe[^>]+src=[\'"]([^\'"]+)[\'"]', html, re.I):
        _add(m.group(1))
    for m in _LAZY_IFRAME_RE.finditer(html):
        s = re.search(r'\ssrc=["\']([^"\']+)["\']', m.group(0), re.I)
        if s: _add(s.group(1))
    for m in _REFRESH_RE.finditer(html):
        _add(m.group(1))
    return found

# Embed URLs are frequently parked in a data-* attribute or a player config instead of an
# <iframe src>, and the player is injected on click. A src-only pass returns nothing on
# exactly the pages that need it, so the whole page text is swept for known embed hosts.
_EMBED_URL_RE = re.compile(r"https?://[^\s\"'<>\\]{10,300}", re.I)

def _is_known_embed_host(u: str) -> bool:
    """True when a URL sits on a host a specialist scraper already handles."""
    d = urlparse(u).netloc.lower().removeprefix("www.")
    if not d: return False
    for s in (_VOE_DOMAINS, _VIDMOLY_DOMAINS, _DOOD_DOMAINS, _LULU_DOMAINS, _FILEMOON_DOMAINS,
              _STREAMTAPE_DOMAINS, _STREAMWISH_DOMAINS, _MIXDROP_DOMAINS, _VIDGUARD_DOMAINS,
              _VIDHIDE_DOMAINS, _VIDNEST_DOMAINS, _NETU_DOMAINS, _VEEV_DOMAINS, _BIGWARP_DOMAINS,
              _SXYPRN_DOMAINS):
        if any(d == x or d.endswith("." + x) for x in s): return True
    return bool(_LULU_FAMILY.match(d))

def _scrape_embed_urls(html: str, loose: bool = False) -> list:
    """URLs on known embed hosts found ANYWHERE in the page text: attributes, player configs,
    packed blocks. Tube themes park the embed in data-src or a JS var rather than an
    <iframe src>, so the src-only pass returns nothing on exactly the pages that need it.
    loose=True also accepts the /e/<id> shape on hosts the domain lists do not know yet."""
    out, seen = [], set()
    for text in _texts_with_unpacked(html or ""):
        for m in _EMBED_URL_RE.finditer(text):
            u = m.group(0).replace("&amp;", "&").replace("\\/", "/").replace("\\", "").rstrip(".,;)]")
            if u in seen:
                continue
            seen.add(u)
            if (_is_known_embed_host(u) or (loose and _EMBED_ID_RE.match(u))) and u not in out:
                out.append(u)
                if len(out) >= 12:
                    return out
    return out


_PLAYER_CFG_RE = re.compile(r'["\'](?:single_video_url|single_video_source|single_video_streaming)'
                            r'["\']\s*:\s*"((?:[^"\\]|\\.)*)"', re.I)
_OG_VIDEO_RE = re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:video(?::secure_url|:url)?'
                          r'|twitter:player)["\'][^>]+content=["\']([^"\']+)["\']', re.I)
# The id shape and the WordPress player-shell shape. embed.php?video=N is a click-to-play stub,
# so it must enter the resolver too, not only the /e/<id> form.   ← changed
_EMBED_ID_RE = re.compile(r"https?://[^\s\"'<>\\]+(?:/[ev]/[A-Za-z0-9]{6,}|/(?:embed|api)\.php\?video=\d+)", re.I)
_VOE_MARKERS = ("voe-logo", "jwplayer/8.", "Content Delivery Network (CDN)")


def _api_endpoint_for(u: str):
    """WordPress player shells: /embed.php?video=N is a click-to-play stub, /api.php?video=N
    is the same call that returns the real iframe. Return the API twin, else None."""
    p = urlparse(u)
    if p.path.endswith("/embed.php") and p.query:
        return urlunsplit((p.scheme, p.netloc, p.path.replace("embed.php", "api.php"), p.query, ""))
    return None


def _scrape_player_config(html: str, base: str) -> list:
    """Embed URLs a theme keeps in a player config or an og:video meta, unescaped and
    absolutised. The best source on WordPress tube themes: no click, no browser, no drift."""
    out = []
    for m in _PLAYER_CFG_RE.finditer(html or ""):
        # the value arrives as "https:\/\/voe.sx\/e\/id": unescape the slashes first, or the
        # URL regex below matches nothing and only the og:video stub survives.   ← changed
        for u in re.findall(r"https?://[^\s\"'<>]+",
                            decode_js_escapes(m.group(1).replace("\\/", "/"))):
            u = u.replace("&amp;", "&").rstrip("\\/.,;")
            if u not in out:
                out.append(u)
    for m in _OG_VIDEO_RE.finditer(html or ""):
        u = urljoin(base, m.group(1).replace("&amp;", "&").strip())
        if u not in out:
            out.append(u)
    return [u for u in out if not re.search(r"\.(?:mp4|m3u8|mpd|webm|mov|mkv)(?:$|\?)", u, re.I)]


def _resolve_embed_deep(emb: str, base: str, title: str = "", depth: int = 0, uid: int = None):
    """Follow one embed URL to a stream. Player shells are rewritten to their API twin,
    VOE-family players (any mirror domain) go to _scrape_voe, everything else recurses.
    Every dead end is reported through _diag, so a silent "" is no longer invisible.   ← changed"""
    if not emb or depth > 3 or emb.rstrip("/") == base.rstrip("/"):
        _diag(uid, "embed resolve", "depth", f"{depth} ({redact_url(emb)})")
        return "", "", ""
    api = _api_endpoint_for(emb)
    if api:
        try:
            r = _cf_get(api, referer=emb, timeout=12)
            sub = r.text if hasattr(r, "text") else ""
        except Exception as e:
            _diag(uid, "embed resolve", "api shell", _diag_detail(e))
            sub = ""
        subs = _scrape_embed_urls(sub, loose=True)
        if not subs:
            _diag(uid, "embed resolve", "api shell", "no embed in the api response")
        for u in subs:
            st, t, rf = _resolve_embed_deep(u, api, title, depth + 1, uid)
            if st:
                return st, t, rf
        return "", "", ""
    if _is_known_embed_host(emb):
        st, t, r = _scrape_real_url(emb)
        if st and st != emb:
            return st, t, r
        _diag(uid, "embed resolve", "known host", f"scraper returned nothing ({redact_url(emb)})")
        return "", "", ""
    # Unknown host: decide VOE by content, because the mirror names rotate.
    try:
        r = _cf_get(emb, referer=base, timeout=15)
        html = r.text if hasattr(r, "text") else ""
    except Exception as e:
        _diag(uid, "embed resolve", "fetch", _diag_detail(e))
        return "", "", ""
    if any(m in html for m in _VOE_MARKERS):
        st, t, _ = _scrape_voe(emb)
        if st:
            return st, t or title, clean_referer(emb)
        _diag(uid, "embed resolve", "voe", "markers found, payload not decodable")
    st, t, r = _scrape_real_url(emb)
    if st and st != emb:
        return st, t, r
    _diag(uid, "embed resolve", "unknown host", f"nothing on {redact_url(emb)}")
    return "", "", ""


# ══════════════════════════════════════════════════════
#  PLAYWRIGHT UNIVERSAL NETWORK SNIFFER
# ══════════════════════════════════════════════════════
def _universal_snatch_sync(page_url: str) -> dict:
    sync_playwright = _pw()
    media_candidates = []
    page_title = ""

    try:
        with sync_playwright() as p:
            browser, context = _sync_launch_chrome(p, headless=True)
            if COOKIES_FILE:
                try: context.add_cookies(_parse_netscape_cookies(COOKIES_FILE))
                except Exception: pass

            page = context.new_page()

            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
                window.open = function() { return null; };
                window.alert = function() { return null; };
                document.addEventListener('DOMContentLoaded', () => {
                    try {
                        const style = document.createElement('style');
                        style.innerHTML = `
                            [id*="overlay"], [class*="overlay"], [id*="pop"], [class*="pop"],
                            div[style*="z-index: 2147483647"], div[style*="position: fixed"] {
                                pointer-events: none !important;
                            }
                            video, button, .vjs-big-play-button, .play {
                                pointer-events: auto !important;
                                z-index: 2147483648 !important;
                            }
                        `;
                        document.head.appendChild(style);
                    } catch(e) {}
                });
            """)

            def _full_headers(req):
                # req.headers omits browser-internal headers (Origin, sec-fetch-*);
                # all_headers() returns the complete wire set once available.
                try:
                    return dict(req.all_headers())
                except Exception:
                    return dict(req.headers)

            def on_request(req):
                u = req.url
                u_clean = u.split("?")[0].rstrip("/").lower()
                if any(x in u_clean for x in ("preview", "thumb", "poster", ".jpg", ".png", ".gif", "analytics", "favicon")):
                    return
                is_m3u8 = ".m3u8" in u_clean or "m3u8" in req.headers.get("accept", "").lower()
                is_mpd = ".mpd" in u_clean or "/manifest" in u_clean or "dash" in req.headers.get("accept", "").lower()
                is_direct = any(u_clean.endswith(ext) for ext in (".mp4", ".webm", ".ts", ".mkv", ".m4s", ".vid")) or ("/get_file/" in u)

                if is_m3u8 or is_mpd or is_direct or "voe-network.net" in u or "doodcdn" in u or "/video~" in u:
                    hdrs = _full_headers(req)
                    hdrs.setdefault("Referer", clean_referer(page.url))
                    media_candidates.append({
                        "url": u, "headers": hdrs,
                        "type": "hls" if is_m3u8 else ("dash" if is_mpd else "direct"),
                        "page_url": page.url,
                    })

            _GENERIC_CT = ("application/octet-stream", "binary/octet-stream", "text/plain")
            _SNIFF_MAX_BYTES = 5 * 1024 * 1024  # never read bodies larger than this

            def on_response(resp):
                ctype = resp.headers.get("content-type", "").lower()
                u = resp.url
                if any(x in ctype for x in ("mpegurl", "video/mp4", "video/mp2t", "application/x-mpegurl", "video/octet-stream", "dash+xml")) or "doodcdn" in u or ".mpd" in u:
                    hdrs = _full_headers(resp.request)
                    hdrs.setdefault("Referer", clean_referer(page.url))
                    media_candidates.append({
                        "url": u, "headers": hdrs,
                        "type": "hls" if ("mpegurl" in ctype or ".m3u8" in u) else ("dash" if (".mpd" in u or "dash" in ctype) else "direct"),
                        "page_url": page.url,
                    })
                    return
                # Body sniffing: servers that label media with a generic content type at
                # opaque URLs. Only small bodies are read (manifest-sized); large binary
                # downloads are never pulled just to sniff them.
                if ctype.split(";")[0].strip() not in _GENERIC_CT and ctype != "":
                    return
                u_clean = u.split("?")[0].rstrip("/").lower()
                if any(x in u_clean for x in ("preview", "thumb", "poster", ".jpg", ".png", ".gif", "analytics", "favicon")):
                    return
                if any(c["url"] == u for c in media_candidates):
                    return
                try:
                    cl = int(resp.headers.get("content-length", "") or 0)
                except Exception:
                    cl = 0
                if not cl or cl > _SNIFF_MAX_BYTES:
                    return
                try:
                    body = resp.body()
                except Exception:
                    return
                if not body:
                    return
                head = body[:2048]
                sniffed = None
                if body.startswith(b"#EXTM3U"):
                    sniffed = "hls"
                elif head.lstrip()[:1] == b"<" and b"<MPD" in head:
                    sniffed = "dash"
                elif len(body) >= 12 and body[4:8] == b"ftyp" and all(32 <= b < 127 for b in body[8:12]):
                    sniffed = "direct"
                if sniffed:
                    hdrs = _full_headers(resp.request)
                    hdrs.setdefault("Referer", clean_referer(page.url))
                    media_candidates.append({
                        "url": u, "headers": hdrs, "type": sniffed, "page_url": page.url,
                    })

            page.on("request", on_request)
            page.on("response", on_response)

            try: page.goto(page_url, wait_until="domcontentloaded", timeout=12000)
            except Exception: pass

            try: page_title = page.title()
            except Exception: page_title = ""

            for frame in page.frames:
                try:
                    f_content = decode_js_escapes(frame.content())
                    kvs_url, _ = _scrape_kvs_and_html5(f_content, frame.url or page_url)
                    if kvs_url:
                        media_candidates.append({
                            "url": kvs_url,
                            "headers": {"Referer": clean_referer(frame.url or page_url), "User-Agent": _STANDARD_UA},
                            "type": "direct",
                            "page_url": page_url
                        })
                except Exception: pass

            # Standard video metadata (og:video, twitter:player:stream, JSON-LD
            # VideoObject contentUrl) - catches pages that declare media without a
            # player. Only http(s) URLs with a real media extension are accepted.
            try:
                html_main = page.content()
                _meta_pats = (
                    r'''<meta[^>]+(?:property|name)=["'](?:og:video(?::url|:secure_url)?|twitter:player:stream)["'][^>]+content=["']([^"']+)["']''',
                    r'''<meta[^>]+content=["']([^"']+)["'][^>]+(?:property|name)=["'](?:og:video(?::url|:secure_url)?|twitter:player:stream)["']''',
                    r'''"contentUrl"\s*:\s*"([^"]+)"''',
                )
                for pat in _meta_pats:
                    for m in re.finditer(pat, html_main, re.I):
                        raw = m.group(1).replace('\\/', '/').replace('&amp;', '&').strip()
                        full = urljoin(page.url or page_url, raw)
                        low = full.lower()
                        if not low.startswith(('http://', 'https://')):
                            continue
                        if not any(e in low.split('?')[0] for e in ('.mp4', '.webm', '.m3u8', '.mov', '.m4v', '.mpd')):
                            continue
                        if any(c['url'] == full for c in media_candidates):
                            continue
                        media_candidates.append({
                            'url': full,
                            'headers': {'Referer': clean_referer(page.url or page_url), 'User-Agent': _STANDARD_UA},
                            'type': 'hls' if '.m3u8' in low else ('dash' if '.mpd' in low else 'direct'),
                            'page_url': page_url,
                        })
            except Exception: pass

            time.sleep(1.0)
            # Player activation: same player-control selectors as before, but tried in
            # every frame so players embedded in iframes start too. No generic buttons.
            for frame in page.frames:
                if media_candidates: break
                for sel in [
                    "button.vjs-big-play-button", ".jw-display-icon-display",
                    "button[aria-label='Play']", ".play-button", "#play", "video",
                    ".fp-play", ".fp-ui", "#kt_player", ".player-holder", ".play"
                ]:
                    try:
                        el = frame.query_selector(sel)
                        if el:
                            el.click(timeout=800, no_wait_after=True)
                            time.sleep(0.8)
                            if media_candidates: break
                    except Exception: pass

            # Poll for late-loading media; still bounded by the 20s async wrapper.
            for _ in range(14):
                if media_candidates: break
                time.sleep(0.8)

            context.close()
            browser.close()
    except Exception as e:
        logging.warning(f"[universal_snatch] error: {e}")

    if media_candidates:
        def _score(item):
            u = item["url"].lower()
            t = item.get("type", "")
            if "master.m3u8" in u or "playlist.m3u8" in u or ".mpd" in u: return 100
            if ".m3u8" in u: return 80
            if ".mp4" in u or "doodcdn" in u or "/video~" in u or "/get_file/" in u: return 60
            if t in ("hls", "dash"): return 70  # body-sniffed manifest at opaque URL
            if t == "direct": return 50         # body-sniffed or metadata-declared media
            return 10
        media_candidates.sort(key=_score, reverse=True)
        best = media_candidates[0]
        best["title"] = page_title
        return best
    return None

async def universal_snatch(page_url: str, status_msg=None) -> dict:
    if status_msg:
        await safe_edit(status_msg, "🌐 *Step 1/4: Sniffing stream (browser sniffer)...*", force=True)
    try:
        return await asyncio.wait_for(asyncio.to_thread(_universal_snatch_sync, page_url), timeout=20)
    except Exception as e:
        logging.warning(f"[universal_snatch skipped/timeout] {e}")
        return None


# ══════════════════════════════════════════════════════
#  ALL 13+ SPECIALIST SCRAPERS + VOE + SERIENSTREAM
# ══════════════════════════════════════════════════════
_VIDMOLY_DOMAINS    = {"vidmoly.me", "vidmoly.biz", "vidmoly.to", "vidmoly.net", "vidmoly.org"}
_LULU_DOMAINS       = {"lulustream.com", "luluvid.com", "luluvdo.com", "luluvdoo.com", "lulucdn.com", "hrnyvid.com", "hrnytube.com", "luluvids.com", "luluplayer.com", "luluvideo.com", "nsfwlulu.com", "luluembed.com",
                       "lulu0.ovh", "lulu1.ovh", "lulu2.ovh", "lulu3.ovh", "lulu4.ovh", "lulu5.ovh",
                       "lulu.ovh", "lulu.dlc.ovh"}   # ← added (vsex.in / dlc.ovh rotation family)
_LULU_FAMILY        = re.compile(r"^lulu\d*\.", re.I)   # lulu0.ovh, lulu.dlc.ovh, lulu7.sx …
_DOOD_DOMAINS       = {"doodstream.com", "doodstream.co", "dood.watch", "dood.to", "dood.so", "dood.cx", "dood.la", "dood.sh", "dood.pm", "dood.wf", "dood.re", "dood.yt", "dood.ws", "dood.vip", "dood.pro", "dood.li", "dood.one", "dood.run", "dood.video", "dood.buzz", "dood.best", "dood.fun", "doodcdn.com", "doodcdn.net", "doodcdn.io", "ds2play.com", "ds2video.com", "playmogo.com", "dooood.com", "doods.pro", "myvidplay.com", "dsvplay.com", "d000d.com", "vvide0.com"}
_FILEMOON_DOMAINS   = {"filemoon.sx", "filemoon.to", "filemoon.in", "filemoon.nl", "filemoon.cc", "filemoon.io", "moonplayer.to", "filemoon.art", "filemoon.fun", "filemoon.icu", "filemoon.site", "filemoon.xyz", "filemoon.top", "filemoon.wf", "kerapoxy.cc", "moonembed.pro", "filemoonembed.com"}
_STREAMTAPE_DOMAINS = {"streamtape.com", "streamtape.to", "streamtape.net", "streamtape.xyz", "streamtape.site", "streamtape.cc", "streamtape.ca", "streamtape.space", "tapecontent.net", "strcloud.in", "strcloud.link", "streamtape.co", "streamtape.biz", "streamtape.app", "streamtape.pw", "streamtape.online", "streamtape.live", "streamta.pe", "scloud.online", "strcloud.online"}
_STREAMWISH_DOMAINS = {"streamwish.com", "streamwish.to", "streamwish.site", "wishfast.top", "streamwish.net", "swdyu.com", "jwvid.tv", "asnimated.com", "stmruby.com", "streamruby.com", "streamwish.fun", "streamwish.space", "streamwish.xyz", "streamwish.pro", "dwish.tv", "awish.pro", "awish.top", "awish.live", "mwish.pro", "swiftload.io", "asianload.net", "playflows.net", "playertv.net", "playm4u.net"}
_MIXDROP_DOMAINS    = {"mixdrop.co", "mixdrop.to", "mixdrop.bz", "mixdrop.ch", "mixdrop.club", "mixdrop.gl", "mixdrop.sx", "mixdrop.ag", "mixdrop.ps", "mixdrop.is", "mixdrop.vc", "mixdrop.nu", "mixdrop.ms", "mixdrop.ws", "mixdrp.to"}
_VIDGUARD_DOMAINS   = {"vidguard.to", "vidguard.online", "vgfplay.com", "listeamed.net", "bembed.net", "vid142.site", "v6embed.xyz", "vgembed.com", "vidguard.pro", "vidguard.live", "vidguard.stream", "s1.vidguard.to", "s2.vidguard.to", "s3.vidguard.to", "vgplayer.com", "tomatomatela.club"}
_VIDHIDE_DOMAINS    = {"vidhide.com", "vidhide.net", "vidhide.to", "avhide.com", "avhide.to", "vidhidevip.com", "vidhide.pro", "vidhide.live", "vidhide.online", "vidhide.stream", "vidhidepro.com", "embedv.net", "sfastwish.com", "smoothpre.com", "ftswish.top", "guccihide.com", "anihdplay.com", "anihide.com"}
_VIDNEST_DOMAINS    = {"vidnest.net", "vidnest.org", "vidnest.site", "vidnest.to", "vidnest.xyz", "vidnest.live"}
_NETU_DOMAINS       = {"netu.ac", "netu.io", "hqq.tv", "hqq.ac", "hqq.to", "waaw.tv", "gomo.to", "netu.to", "netu.one", "netu.cc", "netuplayer.top", "hqq.li", "hqq.me", "hqq.la", "embed.hqq.tv"}
_VOE_DOMAINS        = {"voe.sx", "voe.to", "voe.net", "voe.ac", "voe.cc", "voeunblocked.net", "voe-unblock.com", "voe.vc", "voe.pm", "voe.ws", "voe.gg", "voe.gl", "voe.ms", "voe.sh", "voe.cx", "voe.buzz", "voe.run", "voe.tw", "voe.pro", "voe.media", "voe.stream", "voe.one"}
_VEEV_DOMAINS       = {"veev.to", "veev.cc", "veev.ac", "veev.net", "veev.io", "veev.tv"}
_BIGWARP_DOMAINS    = {"bigwarp.io", "bigwarp.net", "bigwarp.to", "bigwarp.cc", "bigwarp.site"}
_SXYPRN_DOMAINS     = {"sxyprn.com", "sxyprn.net", "sxyprn.io", "sxyprn.org", "sxyprn.xxx"}   # ← added

_SERIENSTREAM_DOMAINS = {
    "serienstream.to", "serienstream.cx", "s.to", "serien.sx", "serienstream.sx",
    "aniworld.to", "aniworld.info", "186.2.175.5"
}

def _is_serienstream_url(url: str) -> bool:
    d = urlparse(url).netloc.lower().removeprefix("www.")
    return any(d == x or d.endswith("." + x) for x in _SERIENSTREAM_DOMAINS) or ("serienstream" in d) or ("aniworld" in d)

def _is_voe_url(url: str) -> bool:
    d = urlparse(url).netloc.lower().removeprefix("www.")
    if any(d == x or d.endswith("." + x) for x in _VOE_DOMAINS) or "voe" in d: return True
    parts = urlparse(url).path.strip("/").split("/")
    slug = parts[-1] if parts else ""
    return bool(re.fullmatch(r"[a-zA-Z0-9]{10,14}", slug) and (len(parts) == 1 or (len(parts) == 2 and parts[0] in ("e", "v"))))

def _is_vidmoly_url(url: str) -> bool:
    d = urlparse(url).netloc.lower().removeprefix("www.")
    return any(d == x or d.endswith("." + x) for x in _VIDMOLY_DOMAINS)

def _is_lulu_url(url: str) -> bool:
    d = urlparse(url).netloc.lower().removeprefix("www.")
    if _LULU_FAMILY.match(d): return True          # lulu0.ovh, lulu.dlc.ovh, lulu7.sx …
    return any(d == x or d.endswith("." + x) for x in _LULU_DOMAINS)

def _is_doodstream_url(url: str) -> bool:
    d = urlparse(url).netloc.lower().removeprefix("www.")
    return any(d == x or d.endswith("." + x) for x in _DOOD_DOMAINS)

def _is_filemoon_url(url: str) -> bool:
    d = urlparse(url).netloc.lower().removeprefix("www.")
    return any(d == x or d.endswith("." + x) for x in _FILEMOON_DOMAINS)

def _is_streamtape_url(url: str) -> bool:
    d = urlparse(url).netloc.lower().removeprefix("www.")
    return any(d == x or d.endswith("." + x) for x in _STREAMTAPE_DOMAINS)

def _is_streamwish_url(url: str) -> bool:
    d = urlparse(url).netloc.lower().removeprefix("www.")
    return any(d == x or d.endswith("." + x) for x in _STREAMWISH_DOMAINS)

def _is_mixdrop_url(url: str) -> bool:
    d = urlparse(url).netloc.lower().removeprefix("www.")
    return any(d == x or d.endswith("." + x) for x in _MIXDROP_DOMAINS)

def _is_vidguard_url(url: str) -> bool:
    d = urlparse(url).netloc.lower().removeprefix("www.")
    return any(d == x or d.endswith("." + x) for x in _VIDGUARD_DOMAINS)

def _is_sxyprn_url(url: str) -> bool:                                   # ← added
    d = urlparse(url).netloc.lower().removeprefix("www.")
    return any(d == x or d.endswith("." + x) for x in _SXYPRN_DOMAINS)

# Hosts whose media token is minted per session and answers 403 to a cold request.
# On these the browser leg is worth running even when the target is already an HLS URL.
_SESSION_BOUND = re.compile(r"(^|\.)(lulu\d*\.|dood|doodcdn|voe|streamwish|vidhide|filemoon)", re.I)

def _snatch_worth(url: str, target: str, snatch_type) -> bool:
    """Escalation gate. The browser leg costs up to SNATCH_MAX_SEC, so it is not a rung that
    runs "just in case". It runs when EITHER

      (a) the host mints its token per session - a cold request from the requests legs 403s,
          so the browser is the only leg that can still win, or
      (b) nothing was resolved at all and the host is not one yt-dlp owns outright.

    It is skipped while the capture tiers are paused, for every platform yt-dlp handles better
    than a browser could, and for a plain resolved file the requests legs will take anyway."""
    if not _snatch_avail():                                     # ← changed
        return False
    page_host = urlparse(url).netloc.lower()
    host = page_host.removeprefix("www.")
    if any(host == h or host.endswith("." + h) for h in _SNATCH_SKIP_HOSTS):
        return False
    bound = bool(_SESSION_BOUND.search(page_host))
    low = (target or "").split("?")[0].lower()
    manifest = low.endswith((".m3u8", ".mpd")) or "/manifest" in low
    if snatch_type in ("hls", "dash") or manifest:
        return bound          # (a) only: a session-bound manifest is worthless cold
    # A plain file was resolved on an ordinary host: aria2c/ffmpeg take it, no browser needed.
    if target and target != url and not bound and detect_direct_video_ext(target):
        return False                                        # ← added (keeps cheap rungs first)
    return True               # (b) nothing usable resolved, or only a non-session URL

def _sniffer_worth(url: str) -> bool:                       # ← added
    """The sniffer is the stand-in for the capture leg, so it only runs when that leg cannot
    (tiers paused, or disabled) and the host is not one yt-dlp owns outright. This is what stops
    it burning 20 s ahead of yt-dlp on platform links."""
    if not SNATCH_ENABLE or _snatch_avail():
        return False
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return not any(host == h or host.endswith("." + h) for h in _SNATCH_SKIP_HOSTS)

def _is_generic_jwplayer_url(url: str) -> bool:
    d = urlparse(url).netloc.lower().removeprefix("www.")
    for s in (_VIDNEST_DOMAINS, _NETU_DOMAINS, _VOE_DOMAINS, _VEEV_DOMAINS, _BIGWARP_DOMAINS):
        if any(d == x or d.endswith("." + x) for x in s): return True
    return False

def _scrape_vidmoly(page_url: str) -> tuple:
    try:
        parsed = urlparse(page_url)
        raw = parsed.path.rstrip("/").split("/")[-1]
        slug = re.sub(r'^(?:embed-|w-|v-)', '', raw, flags=re.I)
        slug = re.sub(r'(?:\.html|\.mp4|\.mkv)$', '', slug, flags=re.I)
        domain = parsed.netloc
        embed_url = f"{parsed.scheme}://{domain}/embed-{slug}.html"
        resp = _cf_get(embed_url, referer=page_url or f"{parsed.scheme}://{domain}/", timeout=20)
        html = decode_js_escapes(resp.text)

        stream_url = _stream_from_html(html)

        title = _extract_title_from_html(html, fallback=f"Vidmoly_{slug}")
        return stream_url, title, embed_url
    except Exception as e:
        logging.warning("[vidmoly] failed: " + str(e))
    return "", "", page_url

def _scrape_lulustream(page_url: str) -> tuple:
    """Try the URL as given first (some hosts serve /online/mostrar/<hash>), follow the
    meta-refresh, and only construct /e/<id> as a fallback."""
    seen, queue = set(), [page_url]
    p = urlparse(page_url)
    if "/e/" not in p.path:
        vid = re.sub(r"(_[a-zA-Z0-9]+|\.[a-zA-Z0-9]+)$", "", p.path.rstrip("/").rsplit("/", 1)[-1])
        if vid: queue.append(f"{p.scheme}://{p.netloc}/e/{vid}")
    for _ in range(4):
        if not queue: break
        cand = queue.pop(0)
        if cand in seen: continue
        seen.add(cand)
        try:
            resp = _cf_get(cand, referer=page_url, timeout=20)
        except Exception:
            continue
        html = decode_js_escapes(resp.text)
        final = str(getattr(resp, "url", "")) or cand
        if final not in seen: seen.add(final)
        found = _stream_from_html(html)
        if found:
            return found, final
        for m in _REFRESH_RE.finditer(html):
            nxt = urljoin(final, m.group(1))
            if nxt not in seen: queue.append(nxt)
    return "", page_url

def _scrape_doodstream(page_url: str) -> tuple:
    try:
        parsed = urlparse(page_url)
        path = parsed.path
        if "/d/" in path: path = path.replace("/d/", "/e/")
        elif "/f/" in path: path = path.replace("/f/", "/e/")
        elif not path.startswith("/e/"):
            slug = path.rstrip("/").split("/")[-1]
            path = f"/e/{slug}"

        embed_url = f"{parsed.scheme}://{parsed.netloc}{path}"
        resp = _cf_get(embed_url, referer="https://www.google.com/", timeout=20)
        html = decode_js_escapes(resp.text)
        final_url = str(getattr(resp, "url", "")) or embed_url
        base = f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}"

        _PAT_PASS = [
            r"['\"](/pass_md5/[^'\"]+)['\"]",
            r"\$\.get\('(/pass_md5/[^']+)'",
            r"(/pass_md5/[a-zA-Z0-9_\-]+/[a-zA-Z0-9_\-]+)",
            r"(/pass_md5/[^\s'\"\\&]+)"
        ]
        def _find_pass(text):
            for pat in _PAT_PASS:
                m = re.search(pat, text)
                if m: return m.group(1).strip().rstrip("\\")
            return None

        pass_path = _find_pass(html)
        title = _extract_title_from_html(html, fallback=f"Dood_{path.rstrip('/').split('/')[-1]}")

        if pass_path:
            pass_url = urljoin(base, pass_path)
            token = pass_path.rstrip("/").rsplit("/", 1)[-1]
            pass_resp = _cf_get(pass_url, referer=final_url, timeout=15)
            raw_cdn = pass_resp.text.strip()
            if raw_cdn.startswith("http"):
                random_suffix = "".join(random.choices(string.ascii_letters + string.digits, k=10))
                expiry = int(time.time() * 1000)
                final_stream = f"{raw_cdn}{random_suffix}?token={token}&expiry={expiry}"
                return final_stream, title, final_url
    except Exception as e:
        logging.warning("[dood] failed: " + str(e))
    return "", "", page_url

def _scrape_filemoon(page_url: str) -> str:
    try:
        pu = urlparse(page_url)
        path_parts = [p for p in pu.path.split("/") if p]
        embed_url = f"{pu.scheme}://{pu.netloc}/e/{path_parts[1]}" if path_parts and path_parts[0] in ("d", "f", "v") else page_url
        html = decode_js_escapes(_cf_get(embed_url, referer=page_url, timeout=20).text)
        return _stream_from_html(html)
    except Exception as e:
        logging.warning("[filemoon] failed: " + str(e))
    return ""

def _scrape_streamtape(page_url: str) -> str:
    try:
        html = decode_js_escapes(_cf_get(page_url, referer=page_url, timeout=20).text)
        for pat in [
            r"innerHTML\s*=\s*['\"]([^'\"]+)['\"]\s*\+\s*\(['\"]([^'\"]+)['\"]",
            r"innerHTML\s*=\s*['\"]([^'\"]{20,})['\"]",
        ]:
            m = re.search(pat, html)
            if m:
                raw = m.group(1) + (m.group(2) if m.lastindex >= 2 else "")
                result = "https://" + raw.strip().lstrip("/") if not raw.startswith("http") else raw.strip()
                result = re.sub(r"(?<=https://)s[a-z]{3,20}tape\.[a-z]+", "streamtape.to", result)
                result = re.sub(r"/ge[a-z_]{0,15}vid[a-z_]{0,8}\?", "/get_video?", result)
                if "token" in result: return result
    except Exception as e:
        logging.warning("[streamtape] failed: " + str(e))
    return ""

def _scrape_streamwish(page_url: str) -> str:
    try:
        html = decode_js_escapes(_cf_get(page_url, referer=page_url, timeout=20).text)
        return _stream_from_html(html)
    except Exception as e:
        logging.warning("[streamwish] failed: " + str(e))
    return ""

def _scrape_mixdrop(page_url: str) -> str:
    try:
        html = decode_js_escapes(_cf_get(page_url, referer=page_url, timeout=20).text)
        for pat in [r'MDCore\.wurl\s*=\s*["\']([^\'"]+)["\']', r'(?:wurl|vurl)\s*=\s*["\']([^\'"]+\.mp4[^\'"]*)["\']']:
            m = re.search(pat, html)
            if m:
                raw = m.group(1).replace("\\", "")
                return ("https:" + raw) if raw.startswith("//") else raw
    except Exception as e:
        logging.warning("[mixdrop] failed: " + str(e))
    return ""

def _scrape_vidguard(page_url: str) -> str:
    import base64 as _b64
    try:
        html = decode_js_escapes(_cf_get(page_url, referer=page_url, timeout=20).text)
        m = re.search(r'asu\s*=\s*["\']([A-Za-z0-9+/=_\-]+)["\']', html)
        if m:
            rev = m.group(1)[::-1]
            shifted = "".join(chr(ord(c) - 2) for c in rev)
            decoded = _b64.b64decode(shifted + "==").decode("utf-8", errors="ignore")
            u = re.search(r'(https?://[^\s"\'<>]+\.(?:m3u8|mp4)[^\s"\'<>]*)', decoded)
            if u: return u.group(1).replace("&amp;", "&")
    except Exception as e:
        logging.warning("[vidguard] failed: " + str(e))
    return ""

def _scrape_jwplayer_sources(html: str) -> str:
    return _stream_from_html(decode_js_escapes(html))

def _scrape_vidnest(page_url: str) -> str:
    try:
        p    = urlparse(page_url)
        vid  = p.path.rstrip("/").rsplit("/", 1)[-1]
        html = decode_js_escapes(_cf_get(f"{p.scheme}://{p.netloc}/e/{vid}", referer=page_url, timeout=20).text)
        return _scrape_jwplayer_sources(html)
    except Exception: return ""

def _scrape_netu(page_url: str) -> str:
    try:
        p = urlparse(page_url)
        vid = (parse_qs(p.query).get("vid", [""])[0] or p.path.rstrip("/").rsplit("/", 1)[-1])
        if vid:
            from curl_cffi import requests as _cr
            r = _cr.post(f"{p.scheme}://{p.netloc}/player/index.php", params={"data": vid, "do": "getVideo"}, data={"hash": "", "r": page_url}, impersonate="chrome124", timeout=20)
            u = re.search(r'(https?://[^\s"\'<>]+\.(?:m3u8|mp4)[^\s"\'<>]*)', r.text)
            if u: return u.group(1).replace("&amp;", "&")
        html = decode_js_escapes(_cf_get(page_url, referer=page_url, timeout=20).text)
        return _scrape_jwplayer_sources(html)
    except Exception: return ""

def _scrape_veev_bigwarp(page_url: str, tag: str = "veev") -> str:
    try:
        p    = urlparse(page_url)
        vid  = p.path.rstrip("/").rsplit("/", 1)[-1]
        html = decode_js_escapes(_cf_get(f"{p.scheme}://{p.netloc}/e/{vid}", referer=page_url, timeout=20).text)
        return _scrape_jwplayer_sources(html)
    except Exception: return ""

def _scrape_voe(page_url: str) -> tuple:                    # ← changed (JSON payload + both layers)
    import base64 as _b64, json as _json
    curr_url, referer, html, title = page_url, page_url, "", ""

    for _ in range(4):
        try:
            resp = _cf_get(curr_url, referer=referer, timeout=15)
            html = decode_js_escapes(resp.text)
            final_url = str(getattr(resp, "url", "")) or curr_url
            if final_url != curr_url: referer, curr_url = curr_url, final_url
        except Exception: break

        m = re.search(r"""(?:window\.)?location(?:\.href|\.replace\()?['"]?(https?://[^'">\s]+)""", html, re.I)
        if m and m.group(1) != curr_url:
            referer, curr_url = curr_url, m.group(1)
            continue
        break

    title = _extract_title_from_html(html)
    for pat in [r'''['"]hls['"]\s*:\s*['"]([^'"]+\.m3u8[^'"]*)['"]''', r'''['"]file['"]\s*:\s*['"]([^'"]+\.(?:m3u8|mp4)[^\'"]*)['"]''']:
        m = re.search(pat, html, re.I)
        if m: return m.group(1).replace("&amp;", "&").replace("\\", "").strip(), title, curr_url

    def _rot13(s: str) -> str:
        return s.translate(str.maketrans("ABCDEFGHIJKLMabcdefghijklmNOPQRSTUVWXYZnopqrstuvwxyz", "NOPQRSTUVWXYZnopqrstuvwxyzABCDEFGHIJKLMabcdefghijklm"))

    # The payload is not always assigned to a variable: VOE keeps it in
    # <script type="application/json">["<blob>"]</script> with no sources= anywhere, so the
    # assignment pattern alone finds nothing. Sweep JSON blocks, then bare quoted blobs.
    _BLOB_RE = re.compile(r'"[A-Za-z0-9+/=@$^~%*!#&_\-]{40,}"')
    candidates = [m.group(1) for m in re.finditer(
        r'''(?:sources|payload|config)\s*=\s*['"]([A-Za-z0-9+/=@$^~%*!#&_\-]{40,})['"]''', html, re.I)]
    for m in re.finditer(r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
        candidates += [c.strip('"') for c in _BLOB_RE.findall(m.group(1))]
    if not candidates:
        candidates = [c.strip('"') for c in _BLOB_RE.findall(html)]

    for cand in candidates:
        # Mirrors differ in whether the rot13 layer is applied: try both, raw second.
        for layer in (True, False):
            try:
                s = cand.strip()
                if layer:
                    s = _rot13(s)
                for dummy in ("@$", "^^", "~@", "%?", "*~", "!!", "#&", "_", "-"): s = s.replace(dummy, "")
                dec1 = _b64.b64decode(s + "=" * ((4 - len(s) % 4) % 4)).decode("utf-8", errors="ignore")
                shifted = "".join(chr(ord(c) - 3) for c in dec1)[::-1]
                dec2 = _b64.b64decode(shifted + "=" * ((4 - len(shifted) % 4) % 4)).decode("utf-8", errors="ignore")
                data = _json.loads(dec2) if dec2.startswith("{") else {}
                for k in ("direct_access_url", "file", "hls", "mp4"):
                    if isinstance(data.get(k), str) and data[k].startswith("http"):
                        return data[k].replace("&amp;", "&"), title, curr_url
            except Exception: pass

    return "", title, curr_url

def _playwright_resolve_serienstream(target_url: str) -> tuple:
    try:
        sync_playwright = _pw()
        with sync_playwright() as p:
            browser, context = _sync_launch_chrome(p, headless=True)
            page = context.new_page()
            page.goto(target_url, wait_until="domcontentloaded", timeout=10000)

            for _ in range(6):
                if page.query_selector(".hosterSiteVideo, [data-link-target], a.watchEpisode"): break
                page.wait_for_timeout(600)

            title = ""
            el_title = page.query_selector(".episodeGermanTitle, h1")
            if el_title: title = el_title.inner_text().strip()

            items = page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('li[data-link-target], a[data-link-target], a.watchEpisode').forEach(el => {
                    const target = el.getAttribute('data-link-target') || el.getAttribute('href') || '';
                    const txt = (el.innerText || '') + ' ' + (el.innerHTML || '');
                    if (target) out.push({ target: target, text: txt });
                });
                return out;
            }""")

            chosen = next((it["target"] for it in items if "voe" in it["text"].lower()), items[0]["target"] if items else None)
            if not chosen:
                context.close(); browser.close()
                return "", title, target_url

            dest = urljoin(page.url, chosen)
            resolved = [None]
            page.on("request", lambda req: resolved.append(req.url) if _is_voe_url(req.url) or any(x in req.url for x in ("voe", "delivery-node", "streamtape", "dood")) else None)
            try: page.goto(dest, wait_until="domcontentloaded", timeout=10000)
            except Exception: pass

            res_url = next((u for u in resolved if u), page.url)
            context.close(); browser.close()
            return res_url, title, dest
    except Exception as e:
        logging.warning(f"[serienstream playwright] failed: {e}")
        return "", "", target_url

def _scrape_serienstream(page_url: str) -> tuple:
    pu = urlparse(page_url)
    m_slug = re.search(r'/(?:serie|anime)/(?:stream/)?([^/]+)/staffel-(\d+)/episode-(\d+)', pu.path, re.I)
    slug = m_slug.group(1) if m_slug else ""
    s_num = int(m_slug.group(2)) if m_slug else 1
    e_num = int(m_slug.group(3)) if m_slug else 1
    title = f"{slug.replace('-', ' ').title()} S{s_num:02d}E{e_num:02d}" if slug else ""

    clean_path = f"/serie/stream/{slug}/staffel-{s_num}/episode-{e_num}" if slug else pu.path
    candidates = [f"https://serienstream.to{clean_path}", f"https://serienstream.cx{clean_path}"]

    for cand in candidates:
        hoster_url, p_title, final_ref = _playwright_resolve_serienstream(cand)
        if hoster_url and hoster_url != cand:
            title = title or p_title
            if _is_voe_url(hoster_url):
                stream, voe_t, ref = _scrape_voe(hoster_url)
                return stream, title or voe_t, clean_referer(ref)
            else:
                stream, h_t, ref = _scrape_real_url(hoster_url)
                return stream, title or h_t, clean_referer(ref)
    return "", title, page_url


# ══════════════════════════════════════════════════════
#  SXYPRN  --  resolved through the real 302 chain
#
#  The media URL is NOT in the markup. getvsrc() in main2.js builds it
#  client-side and the server answers with a redirect:
#
#    1. data-vnfo holds  /cdn/s13/<t1>/<t2>/<ts>/<t3>/<file>.vid
#    2. transform:       t1 -> t1 + "5/" + b64("<sum6>-<host>-<sum7>")
#                        (b64 with + / = swapped for - _ .)
#                        ts -> ts - sum6 - sum7        (digit sums of t3 and the file)
#       -> /cdn5/<b64>/s13/...   is the only fetchable shape; "/cdn/" 404s
#    3. GET that        -> 302  Location: //s<N>.trafficdeposit.com/widi/<t1>/<newtok>/<ts>/<aid>/<pid>.vid
#    4. GET the Location-> 206 video/mp4, Content-Range total == data-mgfs
#
#  Facts that cost the most time to establish:
#   • *.sxyprn.net / *.sxyprn.com CDN subdomains are NXDOMAIN - the host is
#     trafficdeposit and only the 302 knows which node.
#   • the b64 and the s13 node number are decoration: a junk b64 still 302s.
#   • the second token and the timestamp are minted per request, so the final
#     URL is single-use-ish. Always resolve fresh, never cache it.
#   • post ids are hex(unix_ms) = exactly 13 chars; 14 chars serves a
#     "Post Not Found" wall with no player at all.
#   • *.trafficdeposit.com/pivi/.../vidthumb.mp4 in listings are ~300 KB hover
#     previews. /pivi/ serves the poster (full.jpg) but never the video.
# ══════════════════════════════════════════════════════
_SXYPRN_POST_RE = re.compile(r"/post/([0-9a-fA-F]+)", re.I)
_SXYPRN_ID_LEN  = 13
_SXYPRN_MGFS_RE = re.compile(r'data-mgfs=["\'](\d+)["\']', re.I)


def _sxyprn_post_id(url):
    m = _SXYPRN_POST_RE.search(url or "")
    return m.group(1) if m else None


def _sxyprn_id_variants(pid):
    """sxyprn ids are exactly 13 hex chars; tolerate a stray extra character."""
    out = []
    for c in (pid, pid[:_SXYPRN_ID_LEN], pid[-_SXYPRN_ID_LEN:]):
        if c and c not in out:
            out.append(c)
    return out


def _sxyprn_with_id(url: str, pid: str) -> str:
    p = urlparse(url)
    q = f"?{p.query}" if p.query else ""
    return f"{p.scheme}://{p.netloc}/post/{pid}.html{q}"


def _sxyprn_page_url(url: str) -> str:
    """Canonical post URL: 13-hex id + .html. The 14-char form serves a Post Not Found wall."""
    pid = _sxyprn_post_id(url)
    if not pid:
        return url
    good = next((v for v in _sxyprn_id_variants(pid) if len(v) == _SXYPRN_ID_LEN),
                pid[:_SXYPRN_ID_LEN])
    return _sxyprn_with_id(url, good)


def _sxyprn_ssut51(arg: str) -> int:
    """Sum of the digits in a path segment (port of ssut51)."""
    return sum(int(c) for c in re.sub(r"[^0-9]", "", arg or ""))


def _sxyprn_boo(ss: int, es: int, host: str) -> str:
    """Port of boo(): b64 of "<sum6>-<host>-<sum7>", + / = swapped for - _ ."""
    b = base64.b64encode(f"{ss}-{host}-{es}".encode()).decode()
    return b.replace("+", "-").replace("/", "_").replace("=", ".")


def _sxyprn_build(vpath: str, host: str) -> str:
    """Port of getvsrc()+preda(): the relative path the player actually requests."""
    t = vpath.split("/")
    if len(t) < 8:
        return vpath
    t[1] += "5/" + _sxyprn_boo(_sxyprn_ssut51(t[6]), _sxyprn_ssut51(t[7]), host)
    t[5] = str(int(t[5]) - _sxyprn_ssut51(t[6]) - _sxyprn_ssut51(t[7]))
    return "/".join(t)


def _sxyprn_vnfo(html: str, pid: str) -> str:
    """Raw data-vnfo value for this post (legacy /cdn/ path, pre-transform)."""
    for pat in (r"data-vnfo='([^']+)'", r'data-vnfo="([^"]+)"'):
        for m in re.finditer(pat, html or ""):
            try:
                d = json.loads(_hunesc(m.group(1)))
            except Exception:
                continue
            if isinstance(d, dict) and d:
                v = d.get(pid) or next(iter(d.values()))
                if isinstance(v, str) and len(v.split("/")) >= 8:
                    return v
    return ""


def _sxyprn_resolve(page_url: str):
    """post page -> (final_media_url, declared_bytes, title, canonical_page_url).
    (None, 0, title, page_url) when anything in the chain fails."""
    page_url = _sxyprn_page_url(page_url)
    pid = _sxyprn_post_id(page_url)
    if not pid:
        return None, 0, "", page_url
    p = urlparse(page_url)
    host = p.netloc

    try:
        resp = _cf_get(page_url, referer="https://sxyprn.com/", timeout=25)
        html = getattr(resp, "text", "") or ""
    except Exception as e:
        logging.warning("[sxyprn] page failed: " + type(e).__name__)
        html = ""
    if len(html) < 800:                 # the shell is JS-only; a real post page is far bigger
        alt = _scrape_with_playwright(page_url, timeout=20)
        if len(alt) > len(html):
            html = alt
    title = _extract_title_from_html(html)
    if not html:
        return None, 0, title, page_url

    vpath = _sxyprn_vnfo(html, pid)
    if not vpath:
        _diag_log = "[sxyprn] no data-vnfo on the page"
        logging.warning(_diag_log)
        return None, 0, title, page_url

    size = 0
    m = _SXYPRN_MGFS_RE.search(html)
    if m:
        try: size = int(m.group(1))
        except Exception: size = 0

    rel = _sxyprn_build(vpath, host)
    probe_url = f"{p.scheme}://{host}{rel}"
    h = {"User-Agent": _STANDARD_UA, "Referer": page_url, "Accept": "*/*",
         "Accept-Language": _STANDARD_LANG, "Range": "bytes=0-15"}
    loc = ""
    try:
        from curl_cffi import requests as _cr
        r = _cr.get(probe_url, headers=h, impersonate="chrome124", timeout=25, allow_redirects=False)
        loc = r.headers.get("Location", "") or ""
        _resp_close(r)
    except Exception as e:
        logging.warning("[sxyprn] redirect failed: " + type(e).__name__)
        try:
            r = requests.get(probe_url, headers=h, timeout=25, allow_redirects=False)
            loc = r.headers.get("Location", "") or ""
            r.close()
        except Exception:
            pass
    if not loc:
        return None, 0, title, page_url
    return urljoin(probe_url, loc), size, title, page_url


def _sxyprn_fetch(final_url: str, page_url: str, out_path: str, cancel_flag: list,
                  loop=None, status_msg=None, title: str = "", total: int = 0) -> bool:
    """Stream the resolved media, resumable via Range, verified against data-mgfs."""
    h = {"User-Agent": _STANDARD_UA, "Referer": page_url, "Accept": "*/*",
         "Accept-Encoding": "identity", "Accept-Language": _STANDARD_LANG}
    have = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    if have:
        h["Range"] = f"bytes={have}-"
    t0 = time.time()
    last = [0.0]

    def _tick(n, tot):
        if not (loop and status_msg):
            return
        now = time.time()
        if now - last[0] < 2.0:
            return
        last[0] = now
        bar = progress_bar(n, tot) if tot else fmt_size(n) + " downloaded"
        spd = speed_eta(n, tot, now - t0) if tot else ""
        try:
            asyncio.run_coroutine_threadsafe(
                safe_edit(status_msg, f"📥 *{(title or 'sxyprn')[:40]}*\n\n{bar}{spd}"), loop)
        except Exception:
            pass

    def _pump(resp):
        if resp.status_code not in (200, 206):
            return False
        cr = resp.headers.get("Content-Range", "") or ""
        if "/" in cr and total:
            try:
                if int(cr.rsplit("/", 1)[1]) != total:
                    return False
            except Exception:
                pass
        mode = "ab" if (have and resp.status_code == 206) else "wb"
        n = have if mode == "ab" else 0
        with open(out_path, mode) as fh:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if cancel_flag[0]:
                    return False
                if chunk:
                    fh.write(chunk)
                    n += len(chunk)
                    _tick(n, total)
        return True

    try:
        from curl_cffi import requests as _cr
        r = _cr.get(final_url, headers=h, impersonate="chrome124", timeout=300, stream=True)
        try:
            return _pump(r)
        finally:
            _resp_close(r)
    except Exception as e:
        logging.warning("[sxyprn] fetch failed: " + type(e).__name__)
    if cancel_flag[0]:
        return False
    try:
        r = requests.get(final_url, headers=h, timeout=300, stream=True)
        try:
            return _pump(r)
        finally:
            _resp_close(r)
    except Exception as e:
        logging.warning("[sxyprn] requests fetch failed: " + type(e).__name__)
    return False


def _resp_close(r):
    """Older curl_cffi builds have no context manager on Response: close explicitly instead."""
    try: r.close()
    except Exception: pass


def _sxyprn_scrape(page_url: str) -> tuple:
    """(media_url, title, canonical_post_url) for _scrape_real_url()."""
    final, _size, title, page = _sxyprn_resolve(page_url)
    return (final or ""), title, page


def _scrape_real_url(url: str) -> tuple:
    _LAST_PAGE_SCAN[0] = ""
    candidates_to_inspect = []
    # ── 1. SHORTLINK UNROLLING & PARAMETER UNPACKING ─────
    url = unroll_shortener(url)
    url = unwrap_redirect_parameters(url)

    def _with_title(stream_url, page_html="", page_url_for_fallback="", referer=""):
        title = _extract_title_from_html(page_html) if page_html else ""
        if not title and page_url_for_fallback:
            parts = [p for p in urlparse(page_url_for_fallback).path.split("/") if p]
            if parts: title = parts[-1].replace("-", " ").replace("_", " ").strip()
        return stream_url, title, clean_referer(referer)

    try:
        # sxyprn FIRST: its media is a .vid no generic extractor knows       ← added
        if _is_sxyprn_url(url):
            stream, s_title, ref = _sxyprn_scrape(url)
            if stream: return stream, s_title, clean_referer(ref)

        if _is_serienstream_url(url):
            stream, s_title, ref = _scrape_serienstream(url)
            if stream: return stream, s_title, clean_referer(ref)

        if _is_voe_url(url):
            stream, v_title, ref = _scrape_voe(url)
            if stream: return stream, v_title, clean_referer(ref)

        if _is_vidmoly_url(url):
            stream, vm_title, ref = _scrape_vidmoly(url)
            if stream: return stream, vm_title, clean_referer(ref)

        if _is_lulu_url(url):
            stream, ref_root = _scrape_lulustream(url)
            return _with_title(stream or url, "", url, referer=ref_root)

        if _is_doodstream_url(url):
            stream, d_title, ref = _scrape_doodstream(url)
            if stream: return stream, d_title, clean_referer(ref)

        if _is_filemoon_url(url): return _with_title(_scrape_filemoon(url) or url, "", url)
        if _is_streamtape_url(url): return _with_title(_scrape_streamtape(url) or url, "", url, referer=url)
        if _is_streamwish_url(url): return _with_title(_scrape_streamwish(url) or url, "", url)
        if _is_mixdrop_url(url): return _with_title(_scrape_mixdrop(url) or url, page_url_for_fallback=url)
        if _is_vidguard_url(url): return _with_title(_scrape_vidguard(url) or url, page_url_for_fallback=url)

        if _is_generic_jwplayer_url(url):
            d = urlparse(url).netloc.lower().removeprefix("www.")
            if any(d == x or d.endswith("."+x) for x in _VIDNEST_DOMAINS): stream = _scrape_vidnest(url) or url
            elif any(d == x or d.endswith("."+x) for x in _NETU_DOMAINS): stream = _scrape_netu(url) or url
            elif any(d == x or d.endswith("."+x) for x in _VOE_DOMAINS):
                _voe_s, _voe_t, _voe_r = _scrape_voe(url)
                return _voe_s or url, _voe_t, clean_referer(_voe_r or url)
            else: stream = _scrape_veev_bigwarp(url, d.split(".")[0]) or url
            return _with_title(stream, "", url)

        # Bunkr
        if any(h in url for h in ("bunkr.si", "bunkr.su", "bunkr.la", "bunkr.cr", "bunkr.is")):
            try:
                from curl_cffi import requests as _cr2
                _br = _cr2.get(url, impersonate="chrome124", timeout=15)
                _id_m = re.search(r'data-file-id="([^"]+)"', _br.text)
                if _id_m:
                    _data_id = _id_m.group(1)
                    _ref = "https://get.bunkrr.su/file/" + _data_id
                    _ar = _cr2.post("https://apidl.bunkr.ru/api/_001_v2", json={"id": _data_id}, headers={"Referer": _ref, "Origin": "https://get.bunkrr.su"}, impersonate="chrome124", timeout=15).json()
                    if _ar.get("url"): return _ar["url"], url.rstrip("/").split("/")[-1], _ref
            except Exception: pass

        # Fetch page HTML via curl_cffi
        resp = _cf_get(url, timeout=15)
        raw_html = resp.text if hasattr(resp, "text") else ""
        html = decode_js_escapes(raw_html)
        if not html:
            _LAST_PAGE_SCAN[0] = "the page returned no readable HTML"
            return url, "", ""

        # A challenge page carries no media, so it must not be read as "this site has no
        # stream". Return the page itself unchanged and let the caller escalate to the
        # browser leg, which is the only thing that can clear a challenge.
        if _looks_like_cf(raw_html, getattr(resp, "headers", {})):
            logging.warning(f"[cf] challenge on {redact_url(url)}")
            _LAST_PAGE_SCAN[0] = "the page answered with an anti-bot challenge instead of the player"
            return url, _extract_title_from_html(raw_html), ""

        title = _extract_title_from_html(html)

        # 0. Player config and og:video. A WordPress player theme puts the real embed in a JSON
        #    blob and a meta tag, so this resolves the page with no browser and no click.
        for emb in _scrape_player_config(html, url):
            cfg_stream, cfg_title, cfg_ref = _resolve_embed_deep(emb, url, title)
            if cfg_stream:
                return cfg_stream, title or cfg_title, clean_referer(cfg_ref or url)

        # 1. Labelled quality list or KVS flashvars / HTML5 tags (best rendition, ceiling applied)
        kvs_stream, kvs_title = _scrape_kvs_and_html5(html, url)
        if kvs_stream:
            return kvs_stream, title or kvs_title, clean_referer(url)

        # 2. OpenGraph / JSON-LD schema (strictly media files, not embed pages)
        og_stream, og_title = _scrape_schema_and_og(html, url)
        if og_stream:
            return og_stream, title or og_title, clean_referer(url)

        # 3. JWPlayer sources, packed configs included
        stream = _scrape_jwplayer_sources(html)
        if stream:
            return stream, title, clean_referer(url)

        # 4. Universal Iframe & Embed Page Inspector (also sees lazy div[src] + meta refresh)
        candidates_to_inspect = _scrape_iframes(html, url)

        # 5. Embeds that are not in an attribute at all: player configs and JS vars
        for emb in _scrape_embed_urls(html, loose=True):
            if emb not in candidates_to_inspect:
                candidates_to_inspect.append(emb)

        # Also inspect embed links found in JSON-LD or meta tags
        for m in re.finditer(r'["\'](?:embedUrl|player_url)["\']\s*:\s*["\']([^"\']+)["\']', html, re.I):
            emb = urljoin(url, m.group(1).replace("&amp;", "&").strip())
            if emb not in candidates_to_inspect:
                candidates_to_inspect.append(emb)

        for ifr in candidates_to_inspect:
            sub_stream, sub_title, sub_ref = _scrape_real_url(ifr)
            if sub_stream and sub_stream != ifr:
                return sub_stream, title or sub_title, clean_referer(sub_ref or url)
            try:
                ifr_resp = _cf_get(ifr, referer=url, timeout=12)
                ifr_html = decode_js_escapes(ifr_resp.text if hasattr(ifr_resp, "text") else "")
                if ifr_html:
                    ifr_kvs, _ = _scrape_kvs_and_html5(ifr_html, ifr)
                    if ifr_kvs: return ifr_kvs, title, clean_referer(ifr)
                    ifr_jw = _scrape_jwplayer_sources(ifr_html)
                    if ifr_jw: return ifr_jw, title, clean_referer(ifr)
            except Exception: pass

    except Exception as e:
        logging.warning("[scraper] error: " + str(e))

    if candidates_to_inspect:
        _hosts = ", ".join(sorted({urlparse(c).netloc for c in candidates_to_inspect if c})[:3])
        _LAST_PAGE_SCAN[0] = f"page scan checked {len(candidates_to_inspect)} embed(s) ({_hosts}), none gave a playable stream"
    elif not _LAST_PAGE_SCAN[0]:
        _LAST_PAGE_SCAN[0] = "page scan found no embed links at all"
    return url, "", ""


# ══════════════════════════════════════════════════════
#  ARIA2C & DIRECT FILE DOWNLOAD ENGINE
# ══════════════════════════════════════════════════════
async def _do_dl_via_aria2c(url: str, out_file: str, status_msg, cancel_flag: list, headers: dict = None, uid: int = None,
                            split: int = 8) -> bool:                      # ← split added
    cmd = [
        ARIA2C_PATH,
        url,
        "-d", os.path.dirname(out_file),
        "-o", os.path.basename(out_file),
        "-x", str(split),
        "-s", str(split),
        "-j", "4",
        "--min-split-size=1M",
        "--summary-interval=2",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
    ]
    if headers:
        for k, v in headers.items(): cmd.extend(["--header", f"{k}: {v}"])

    last_edit = [0.0]
    last_err = [""]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        while proc.returncode is None:
            if cancel_flag[0]:
                proc.kill()
                return False
            line = await proc.stdout.readline()
            if not line: break
            line_str = line.decode(errors="replace")
            if re.search(r"error|exception", line_str, re.I): last_err[0] = line_str.strip()[:300]
            m = re.search(r"\((\d+)%\).*?DL:([0-9.]+[A-Za-z]+).*?ETA:([0-9A-Za-z]+)", line_str)
            if m:
                now = time.time()
                if now - last_edit[0] >= 2.0:
                    last_edit[0] = now
                    pct, spd, eta = int(m.group(1)), m.group(2), m.group(3)
                    bar = "▓" * int(16 * pct / 100) + "░" * (16 - int(16 * pct / 100))
                    await safe_edit(status_msg, f"📥 *Downloading via aria2c...*\n\n[{bar}] {pct}%  •  {spd}/s  ETA {eta}")
            await asyncio.sleep(0.05)
        await proc.wait()
        ok = os.path.exists(out_file) and os.path.getsize(out_file) > 0 and _probe_has_media_stream(out_file)
        if not ok: _diag(uid, "direct via aria2c", "download", _diag_detail(last_err[0] or "no media in output"))
        _stat("aria2c", ok)
        return ok
    except Exception as e:
        _diag(uid, "direct via aria2c", "download", _diag_detail(e))
        _stat("aria2c", False)
        logging.warning(f"aria2c error: {e}")
        return False


# ══════════════════════════════════════════════════════
#  DOWNLOAD / UPLOAD PIPELINE
# ══════════════════════════════════════════════════════
def extract_thumb(video_path: str) -> str:
    thumb = video_path + "_thumb.jpg"
    try:
        subprocess.run(
            [FFMPEG_PATH, "-y", "-ss", "00:00:10", "-i", video_path,
             "-vframes", "1", "-vf", "scale=320:-1", "-q:v", "2", thumb],
            capture_output=True, timeout=60)
        if os.path.exists(thumb) and os.path.getsize(thumb) > 0:
            return thumb
    except Exception as e:
        logging.warning("Thumb: " + str(e))
    return ""

def _probe_has_media_stream(path: str) -> bool:
    """True when ffprobe finds at least one real video or audio stream."""
    try:
        r = subprocess.run([
            FFPROBE_PATH, "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "json", path
        ], capture_output=True, text=True, timeout=15)
        kinds = [s.get("codec_type") for s in json.loads(r.stdout).get("streams", [])]
        return "video" in kinds or "audio" in kinds
    except Exception:
        return False


def _probe_duration(path: str) -> float:
    try:
        r = subprocess.run(
            [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60)
        return float((r.stdout or "").strip())
    except Exception:
        return 0.0

async def _run_ffmpeg_live(cmd, total_dur: float, status_msg, label: str,
                           cancel_flag=None, timeout: int = 14400):
    """Run ffmpeg with '-progress pipe:1' and stream live progress into the status message.

    Shows: percent bar, position/total, speed, ETA, elapsed, bytes written, and an
    explicit "stalled Ns" flag when out_time stops advancing. Kills ffmpeg on cancel.
    """
    full = list(cmd)
    if "-progress" not in full:
        full = full[:-1] + ["-progress", "pipe:1", "-nostats", full[-1]]

    proc = await asyncio.create_subprocess_exec(
        *full,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.DEVNULL)

    t0 = time.time()
    st = {"pos": 0.0, "speed": "?", "size": 0, "last_move": time.time(), "last_edit": 0.0}

    async def _reader():
        while True:
            try:
                line = await proc.stdout.readline()
            except Exception:
                return
            if not line:
                return
            s = line.decode(errors="replace").strip()
            if s.startswith("out_time_us="):
                try:
                    pos = int(s.split("=", 1)[1]) / 1_000_000
                    if pos > st["pos"] + 0.05:
                        st["pos"] = pos
                        st["last_move"] = time.time()
                except Exception:
                    pass
            elif s.startswith("out_time="):
                try:
                    parts = s.split("=", 1)[1].split(":")
                    pos = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                    if pos > st["pos"] + 0.05:
                        st["pos"] = pos
                        st["last_move"] = time.time()
                except Exception:
                    pass
            elif s.startswith("total_size="):
                try: st["size"] = int(s.split("=", 1)[1])
                except Exception: pass
            elif s.startswith("speed="):
                st["speed"] = s.split("=", 1)[1].strip()

    async def _ticker():
        while proc.returncode is None:
            await asyncio.sleep(2.0)
            if proc.returncode is not None:
                break
            if cancel_flag and cancel_flag[0]:
                try: proc.kill()
                except Exception: pass
                return
            now = time.time()
            if now - st["last_edit"] < 2.0:
                continue
            st["last_edit"] = now
            el   = int(now - t0)
            idle = int(now - st["last_move"])
            stall = f"  ⚠️ *no progress {idle}s*" if idle > STALL_WARN_SECS else ""

            eta = ""
            try:
                sp = float(st["speed"].rstrip("x"))
                if sp > 0.01 and total_dur > 1:
                    eta = f" · ETA {fmt_clock((total_dur - st['pos']) / sp)}"
            except Exception:
                pass

            tail = f"{st['speed']}{eta} · {fmt_size(st['size'])} · elapsed {fmt_clock(el)}"
            if total_dur and total_dur > 1:
                pct = min(100.0, st["pos"] / total_dur * 100)
                bar = "▓" * int(16 * pct / 100) + "░" * (16 - int(16 * pct / 100))
                await safe_edit(status_msg,
                    f"{label}\n\n[{bar}] {pct:.1f}%\n"
                    f"_{fmt_clock(st['pos'])} / {fmt_clock(total_dur)} · {tail}_{stall}")
            else:
                await safe_edit(status_msg, f"{label}\n\n_{tail}_{stall}")

    rt = asyncio.ensure_future(_reader())
    tt = asyncio.ensure_future(_ticker())
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        try: await proc.wait()
        except Exception: pass
    except asyncio.CancelledError:
        try: proc.kill()
        except Exception: pass
        raise
    finally:
        tt.cancel()
        rt.cancel()
    return proc.returncode

def _remux_attempts(src: str, out: str) -> list:
    return [
        # 1: clean copy, drop data/subtitle streams MP4 cannot hold (e.g. timed_id3)
        [FFMPEG_PATH, "-y", "-i", src, "-map", "0:v?", "-map", "0:a?",
         "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", out],
        # 2: carry text subs as mov_text
        [FFMPEG_PATH, "-y", "-i", src, "-map", "0:v?", "-map", "0:a?", "-map", "0:s?",
         "-c", "copy", "-c:s", "mov_text", "-bsf:a", "aac_adtstoasc",
         "-movflags", "+faststart", out],
        # 3: re-encode audio only (mp2 / ac3 / pcm -> aac)
        [FFMPEG_PATH, "-y", "-i", src, "-map", "0:v?", "-map", "0:a?",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", out],
    ]

async def _remux_live(src: str, status_msg, label: str = "⚙️ *Optimising video format*",
                      cancel_flag=None) -> str:
    """Same three-attempt remux as remux_for_streaming, but with live progress."""
    base, _ = os.path.splitext(src)
    out = base + "_stream.mp4"
    if out == src: out = base + "_fs.mp4"
    dur = await asyncio.to_thread(_probe_duration, src)
    for i, cmd in enumerate(_remux_attempts(src, out), 1):
        try:
            if os.path.exists(out):
                try: os.remove(out)
                except Exception: pass
            rc = await _run_ffmpeg_live(cmd, dur, status_msg,
                                        f"{label} (attempt {i}/3)",
                                        cancel_flag=cancel_flag)
            if rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                try: os.remove(src)
                except Exception: pass
                return out
        except Exception as e:
            logging.warning(f"[remux live] attempt {i} failed: {e}")
    return src

async def _remux_to_live(src: str, container: str, status_msg=None, cancel_flag=None) -> str:
    base, _ = os.path.splitext(src)
    out = f"{base}_remux.{container}"
    cmd = [FFMPEG_PATH, "-y", "-i", src, "-map", "0:v?", "-map", "0:a?", "-c", "copy"]
    if container in ("mp4", "mov"):
        cmd += ["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"]
    cmd += [out]
    dur = await asyncio.to_thread(_probe_duration, src)
    try:
        if os.path.exists(out):
            try: os.remove(out)
            except Exception: pass
        rc = await _run_ffmpeg_live(cmd, dur, status_msg,
                                    f"⚙️ *Remuxing → {container} (stream copy)*",
                                    cancel_flag=cancel_flag)
        if rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            try: os.remove(src)
            except Exception: pass
            return out
    except Exception as e:
        logging.warning(f"[remux_to_live] {e}")
    return src

def parse_convert_spec(tokens) -> dict:
    """['720p','mkv','crf=21'] -> spec. Unknown tokens ignored, 'off' clears."""
    spec = {}
    for raw in tokens or []:
        t = str(raw).strip().lower()
        if not t:
            continue
        if t in ("off", "none", "reset", "clear"):
            return {}
        if t in ("mp4", "mkv", "mov", "ts"):
            spec["container"] = t
        elif t in ("h264", "avc", "x264"):
            spec["vcodec"] = "libx264"
        elif t in ("h265", "hevc", "x265"):
            spec["vcodec"] = "libx265"
        elif t in ("copy", "remux"):
            spec["vcodec"] = "copy"
        elif t in ("mp3", "m4a", "aac", "opus", "flac"):
            spec["audio_only"] = True
            spec["acodec"] = t
        elif t in ("1080p", "720p", "480p", "360p"):
            spec["scale"] = int(t[:-1])
        elif t.startswith("crf"):
            try:
                spec["crf"] = max(0, min(51, int(t.replace("crf", "").lstrip("=:"))))
            except ValueError:
                pass
        elif t.endswith("mb") or t.endswith("gb"):
            try:
                v = float(re.sub(r"[a-z]", "", t))
                spec["target_mb"] = int(v * 1024) if t.endswith("gb") else int(v)
            except ValueError:
                pass
        elif re.fullmatch(r"\d{1,4}m(?:in)?", t) or re.fullmatch(r"\d{1,2}h", t):
            spec["cap_min"] = int(re.sub(r"\D", "", t)) * (60 if t.endswith("h") else 1)
        elif t in ("full", "all", "complete", "entire"):
            spec["cap_min"] = 0
        elif re.fullmatch(r"\d{2,4}k", t):
            spec["abr"] = t
    return spec

def describe_convert(spec: dict) -> str:
    if not spec:
        return "off (stream-copy remux only)"
    if spec.get("audio_only"):
        return f"audio-only -> {spec.get('acodec', 'mp3')}" + (f" @ {spec['abr']}" if spec.get("abr") else "")
    bits = [spec.get("container") or "mp4"]
    if spec.get("vcodec") and spec["vcodec"] != "copy":
        bits.append(spec["vcodec"].replace("lib", ""))
    if spec.get("crf"):
        bits.append(f"crf{spec['crf']}")
    if spec.get("scale"):
        bits.append(f"{spec['scale']}p")
    if spec.get("target_mb"):
        bits.append(f"<={spec['target_mb']}MB")
    if spec.get("abr"):
        bits.append(f"abr {spec['abr']}")
    if "cap_min" in spec:
        bits.append("live full" if not spec["cap_min"] else f"live cap {spec['cap_min']}m")
    return " | ".join(bits)

async def apply_convert(src: str, spec: dict, status_msg=None, cancel_flag=None) -> str:
    """Apply a convert spec. Returns the file to upload (src if nothing applied)."""
    spec = spec or {}
    loop = asyncio.get_running_loop()
    base, _ = os.path.splitext(src)

    # ── audio only ──────────────────────────────────────
    if spec.get("audio_only"):
        codec = spec.get("acodec") or "mp3"
        ext = "m4a" if codec in ("m4a", "aac") else codec
        out = f"{base}_audio.{ext}"
        acodec = {"mp3": "libmp3lame", "m4a": "aac", "aac": "aac",
                  "opus": "libopus", "flac": "flac"}.get(codec, "libmp3lame")
        abr = spec.get("abr") or ("192k" if codec in ("mp3", "m4a", "aac") else "128k")
        cmd = [FFMPEG_PATH, "-y", "-i", src, "-vn", "-c:a", acodec, "-b:a", abr, out]
        dur = await asyncio.to_thread(_probe_duration, src)
        try:
            await _run_ffmpeg_live(cmd, dur, status_msg,
                                   f"🎵 *Extracting audio ({codec})*", cancel_flag=cancel_flag)
        except Exception:
            await loop.run_in_executor(None, lambda: subprocess.run(cmd, capture_output=True, timeout=7200))
        if os.path.exists(out) and os.path.getsize(out) > 0:
            try: os.remove(src)
            except Exception: pass
            return out
        return src

    target_bytes = int(spec["target_mb"] * 1024 * 1024) if spec.get("target_mb") else MAX_UPLOAD_BYTES
    need_encode = bool(spec.get("vcodec") not in (None, "copy")
                       or spec.get("scale") or spec.get("crf") or spec.get("target_mb"))

    if spec.get("target_mb") and not spec.get("vcodec") and not spec.get("scale") \
            and os.path.getsize(src) <= target_bytes:
        need_encode = False

    container = spec.get("container") or ("mp4" if need_encode else None)

    # ── remux only ──────────────────────────────────────
    if not need_encode:
        if container and not src.lower().endswith("." + container):
            return await _remux_to_live(src, container, status_msg, cancel_flag)
        return await _remux_live(src, status_msg, cancel_flag=cancel_flag)

    # ── encode ──────────────────────────────────────────
    out = f"{base}_conv.{container or 'mp4'}"
    vf = ["-vf", f"scale=-2:{spec['scale']}"] if spec.get("scale") else []
    vcodec = spec.get("vcodec") or "libx264"
    preset = "veryfast" if vcodec == "libx265" else "fast"
    abr = spec.get("abr") or "128k"
    duration = await asyncio.to_thread(_probe_duration, src)

    if spec.get("target_mb") and duration > 1:
        audio_bps = int(re.sub(r"\D", "", abr) or 128) * 1000
        vk = max(int(((target_bytes * 8) / duration - audio_bps) / 1000), 150)
        plog = base + "_cpass"
        p1 = [FFMPEG_PATH, "-y", "-i", src, *vf, "-c:v", vcodec, "-b:v", f"{vk}k",
              "-preset", preset, "-pass", "1", "-an", "-passlogfile", plog, "-f", "null", os.devnull]
        p2 = [FFMPEG_PATH, "-y", "-i", src, *vf, "-c:v", vcodec, "-b:v", f"{vk}k",
              "-preset", preset, "-pass", "2", "-passlogfile", plog,
              "-c:a", "aac", "-b:a", abr, "-movflags", "+faststart", out]
        await _run_ffmpeg_live(p1, duration, status_msg,
                               f"⚙️ *Encode · pass 1/2 (analysis)*\n_{vcodec.replace('lib','')} @ {vk}kbps_",
                               cancel_flag=cancel_flag)
        await _run_ffmpeg_live(p2, duration, status_msg,
                               f"⚙️ *Encode · pass 2/2 → <={spec['target_mb']}MB*\n_{vcodec.replace('lib','')} @ {vk}kbps_",
                               cancel_flag=cancel_flag)
        for sfx in ("", "-0.log", "-0.log.mbtree"):
            try: os.remove(plog + sfx)
            except Exception: pass
    else:
        crf = str(spec.get("crf", 20))
        cmd = [FFMPEG_PATH, "-y", "-i", src, *vf, "-c:v", vcodec, "-crf", crf,
               "-preset", preset, "-c:a", "aac", "-b:a", abr,
               "-movflags", "+faststart", out]
        await _run_ffmpeg_live(cmd, duration, status_msg,
                               f"⚙️ *Encoding {vcodec.replace('lib','')} crf{crf}"
                               + (f", {spec['scale']}p" if spec.get("scale") else "") + "*",
                               cancel_flag=cancel_flag)

    if os.path.exists(out) and os.path.getsize(out) > 0:
        try: os.remove(src)
        except Exception: pass
        return out
    return src

def get_video_info(path: str):
    try:
        r = subprocess.run([
            FFPROBE_PATH,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration",
            "-of", "default=noprint_wrappers=1:nokey=0",
            path
        ], capture_output=True, text=True, timeout=15)
        dur, w, h = 0, 0, 0
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                if k == "duration" and v not in ("N/A", ""):
                    try: dur = int(float(v))
                    except: pass
                elif k == "width" and v not in ("N/A", ""):
                    try: w = int(v)
                    except: pass
                elif k == "height" and v not in ("N/A", ""):
                    try: h = int(v)
                    except: pass
        return dur, w, h
    except Exception as e:
        logging.warning("get_video_info: " + str(e))
        return 0, 0, 0

async def compress_to_limit(src: str, status_msg) -> str:
    # 1. Check if compression is actually needed
    SAFE_LIMIT = 2000 * 1024 * 1024  # 1.907 GiB (safely under Telegram's 4000-part threshold)
    filesize = os.path.getsize(src)
    if filesize <= SAFE_LIMIT:
        return src

    loop = asyncio.get_running_loop()

    # 2. Extract Duration & Resolution
    def _probe_meta(path):
        try:
            r = subprocess.run([
                FFPROBE_PATH, "-v", "error",
                "-show_entries", "format=duration:stream=width,height",
                "-of", "json", path
            ], capture_output=True, text=True, timeout=60)
            data = json.loads(r.stdout)
            dur = float(data.get("format", {}).get("duration", 0))
            w, h = 0, 0
            for s in data.get("streams", []):
                if s.get("width"):
                    w, h = int(s["width"]), int(s["height"])
                    break
            return dur, w, h
        except Exception:
            return 0.0, 0, 0

    duration, width, height = await loop.run_in_executor(None, _probe_meta, src)
    if duration < 5:
        # If duration cannot be parsed, fail-safe to original to prevent corruption
        return src

    # 3. Calculate Bitrate with 3% Muxing Overhead Safety Margin
    # Target size: 1910 MB (~1.86 GiB) to guarantee room for MP4 atoms/subtitles
    TARGET_BYTES = 1910 * 1024 * 1024
    total_bits = TARGET_BYTES * 8

    # Reserve 128 kbps for AAC audio
    AUDIO_BITRATE = 128_000
    total_bps = int(total_bits / duration)
    video_bps = total_bps - AUDIO_BITRATE

    # Floor guard (minimum 250 kbps)
    if video_bps < 250_000:
        video_bps = 250_000

    video_kbps = video_bps // 1000
    audio_kbps = AUDIO_BITRATE // 1000

    base, ext = os.path.splitext(src)
    out = f"{base}_tg_compressed.mp4"
    pass_log = f"{base}_2pass_map"

    # Scale down 4K/2K sources to 1080p for optimal quality at low bitrates
    vf_filter = "scale='min(1920,iw)':-2,format=yuv420p"

    # ── PASS 1: Analysis (Fast, no audio, no file output) ──
    await safe_edit(
        status_msg,
        f"⚙️ *Compressing video to fit 2 GB...*\n"
        f"📦 Size: `{fmt_size(filesize)}` ➔ `~{fmt_size(TARGET_BYTES)}`\n"
        f"🎯 Bitrate: `{video_kbps} kbps`\n\n"
        f"🔄 *Phase 1/2: Analyzing frames...*",
        force=True
    )

    cmd_pass1 = [
        FFMPEG_PATH, "-y",
        "-i", src,
        "-c:v", "libx264",
        "-b:v", f"{video_kbps}k",
        "-pass", "1",
        "-passlogfile", pass_log,
        "-preset", "veryfast",
        "-vf", vf_filter,
        "-an",
        "-f", "null",
        "NUL" if os.name == "nt" else "/dev/null"
    ]

    # SAFETY (added): check pass-1 exit code; on failure keep the original untouched.
    p1_rc = await _run_ffmpeg_live(cmd_pass1, duration, status_msg,
        f"⚙️ *Compressing video to fit 2 GB · pass 1/2*\n📦 `{fmt_size(filesize)}` ➔ `~{fmt_size(TARGET_BYTES)}` · `{video_kbps} kbps`")
    if p1_rc != 0:
        logging.error(f"Compression pass 1 failed (rc={p1_rc})")
        for sfx in ["", "-0.log", "-0.log.mbtree"]:
            if os.path.exists(pass_log + sfx):
                try: os.remove(pass_log + sfx)
                except Exception: pass
        return src

    # ── PASS 2: Encode with Live Progress Bar ──
    cmd_pass2 = [
        FFMPEG_PATH, "-y",
        "-i", src,
        "-c:v", "libx264",
        "-b:v", f"{video_kbps}k",
        "-pass", "2",
        "-passlogfile", pass_log,
        "-preset", "fast",
        "-vf", vf_filter,
        "-c:a", "aac",
        "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        out
    ]

    try:
        rc2 = await _run_ffmpeg_live(cmd_pass2, duration, status_msg,
            f"⚙️ *Compressing video to fit 2 GB · pass 2/2*\n📦 Target: `~{fmt_size(TARGET_BYTES)}` (`{video_kbps} kbps`)")
        pass2_ok = (rc2 == 0)  # SAFETY (added)
    except Exception as e:
        pass2_ok = False
        logging.error(f"Compression error: {e}")
    finally:
        # Clean up 2-pass analysis logs
        for sfx in ["", "-0.log", "-0.log.mbtree"]:
            log_file = pass_log + sfx
            if os.path.exists(log_file):
                try: os.remove(log_file)
                except Exception: pass

    # 4. Verification
    # SAFETY (added): pass-2 exit code must be 0, output must be under the limit and
    # its duration must match the source (a partial file can still pass a size check).
    # The original is NOT deleted here: _finish_upload() removes the source and the
    # compressed copy only after the upload step has finished.
    if pass2_ok and os.path.exists(out) and 1024 * 1024 < os.path.getsize(out) <= SAFE_LIMIT:
        def _probe_streams(path):
            try:
                r = subprocess.run([FFPROBE_PATH, "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", path],
                                   capture_output=True, text=True, timeout=60)
                kinds = [s_.get("codec_type") for s_ in json.loads(r.stdout).get("streams", [])]
                return ("video" in kinds), ("audio" in kinds)
            except Exception:
                return False, False
        out_dur, _, _ = await loop.run_in_executor(None, _probe_meta, out)
        _, src_audio = await loop.run_in_executor(None, _probe_streams, src)
        out_video, out_audio = await loop.run_in_executor(None, _probe_streams, out)
        finite = math.isfinite(out_dur) and out_dur > 0
        if finite and out_video and (out_audio or not src_audio) and 0.98 * duration <= out_dur <= 1.02 * duration:
            return out
        logging.error(f"Compressed output rejected (dur {out_dur:.1f}s vs {duration:.1f}s, video={out_video}, audio={out_audio}, src_audio={src_audio}); keeping original")

    # If compression failed or file was invalid, discard the bad output and return original
    if os.path.exists(out):
        try: os.remove(out)
        except Exception: pass
    return src

async def _do_dl_via_cffi(url: str, status_msg, cancel_flag: list, uid: int, chat_id: int, headers: dict = None, title: str = "", page_url: str = "") -> bool:
    from curl_cffi import requests as _cr
    loop = asyncio.get_running_loop()
    t0 = time.time()
    tmp_path = os.path.join(ARIA2C_DOWNLOAD_DIR, f"dl_{uid}_{int(time.time())}.mp4")
    _progress = {"done": 0, "total": 0, "finished": False}

    _ref_url = clean_referer((headers or {}).get("Referer", "") or page_url or url)
    _hdrs = {
        "User-Agent": _STANDARD_UA,
        "Accept-Language": _STANDARD_LANG,
        "Accept": "*/*",
        "Connection": "keep-alive",
    }
    if _ref_url: _hdrs["Referer"] = _ref_url
    for _k, _v in (headers or {}).items(): _hdrs[_k] = _v
    if url.lower().endswith(".vid"):
        _hdrs["Accept-Encoding"] = "identity"      # never gzip an mp4

    def _stream_download():
        _sess = _cr.Session(impersonate="chrome124")
        try:
            # sxyprn's CDN is Referer- AND cookie-locked: load the post page in the same
            # session first, otherwise the .vid answers 404 even with a fresh token.
            if page_url and url.lower().endswith(".vid"):
                try:
                    _sess.get(_sxyprn_page_url(page_url), headers={**_hdrs,
                                                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                                                 "Referer": "https://sxyprn.com/"}, timeout=25)
                except Exception:
                    pass
            resp = _sess.get(url, headers=_hdrs, stream=True, timeout=300)
            try:
                resp.raise_for_status()
                _progress["total"] = int(resp.headers.get("Content-Length", 0))
                done = 0
                with open(tmp_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if cancel_flag[0]:
                            _progress["finished"] = True
                            return None
                        if chunk:
                            fh.write(chunk)
                            done += len(chunk)
                            _progress["done"] = done
            finally:
                _resp_close(resp)
        finally:
            _resp_close(_sess)
        _progress["finished"] = True
        return tmp_path

    async def _progress_updater():
        while not _progress["finished"]:
            await asyncio.sleep(2.5)
            done = _progress["done"]
            total = _progress["total"]
            if done > 0:
                now = time.time()
                label = title[:40] if title else url[:40]
                bar = progress_bar(done, total) if total else ""
                speed_str = speed_eta(done, total, now - t0) if total else fmt_size(done) + " downloaded"
                await safe_edit(status_msg, f"📥 *{label}*\n\n{bar}{speed_str}")

    try:
        dl_task = loop.run_in_executor(None, _stream_download)
        progress_task = asyncio.ensure_future(_progress_updater())
        result_file = await dl_task
        progress_task.cancel()
    except Exception as e:
        _diag(uid, "direct via curl", "download", _diag_detail(e))
        _stat("direct via curl", False)
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except Exception: pass
        await safe_edit(status_msg, f"❌ Download failed ({_diag_detail(e)}).", force=True)
        return False

    if cancel_flag[0] or not result_file or not os.path.exists(result_file):
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except Exception: pass
        if not cancel_flag[0]:
            _diag(uid, "direct via curl", "output check", "no file saved")
            _stat("direct via curl", False)
        await safe_edit(status_msg, "❌ Cancelled or no file saved.", force=True)
        return False

    ok = await _finish_upload(result_file, title, status_msg, uid, chat_id)
    _stat("direct via curl", ok)
    if not ok and os.path.exists(tmp_path) and tmp_path not in _KEPT_SOURCES:
        try: os.remove(tmp_path)
        except Exception: pass
    return ok

def _probe_content_duration(path: str) -> float:
    """Playing time actually present in the first video (else audio) stream: sum of the gaps between packet
    timestamps, where a gap far above the typical frame spacing (a skipped segment) counts as that spacing only.
    The container duration cannot be used: it keeps the original end time even when a middle segment is missing."""
    for sel in ("v:0", "a:0"):
        try:
            r = subprocess.run([FFPROBE_PATH, "-v", "error", "-select_streams", sel, "-show_entries", "packet=pts_time",
                                "-of", "csv=p=0", path], capture_output=True, text=True, timeout=180)
            ts = []
            for ln in (r.stdout or "").splitlines():
                try: ts.append(float(ln.strip().rstrip(",")))
                except ValueError: pass
            ts.sort()
            if len(ts) < 3: continue
            deltas = sorted(b - a for a, b in zip(ts, ts[1:]))
            med = deltas[len(deltas) // 2] or 0.04
            cap = max(med * 5, 0.5)
            return sum(min(d, cap) for d in deltas) + med
        except Exception:
            pass
    return 0.0

# yt-dlp's Python API defaults to NO retries and to silently SKIPPING a fragment that failed (the output is then a
# shorter file with exit status 0 - measured on a localhost fixture). Retry with growing waits (1,2,4,8,8s), and abort
# when a fragment still fails so the next engine runs instead of delivering an incomplete file.
_YDL_RELIABLE = {
    "retries": 3, "fragment_retries": 5, "skip_unavailable_fragments": False,
    "retry_sleep_functions": {"http": lambda n: min(2 ** n, 8), "fragment": lambda n: min(2 ** n, 8)},
}

def _ff_input_duration(ff_err: str) -> float:
    """Input duration FFmpeg prints when it opens the playlist ('Duration: HH:MM:SS.xx'), 0 when absent or N/A (live)."""
    m = re.search(r"Input #0.*?Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", ff_err or "", re.S)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0

def _ff_looks_truncated(ff_err: str, out_path: str) -> bool:
    """True when FFmpeg exited OK but the file is much shorter than the playlist it was reading (a segment
    that failed to download is skipped silently by FFmpeg's HLS reader). Only a candidate check for finite
    VOD playlists: no input duration (live/unknown) means no verdict, and a generous tolerance is applied."""
    inp = _ff_input_duration(ff_err)
    if inp < 5: return False
    out = _probe_content_duration(out_path)
    return bool(out) and out < inp * 0.95 and (inp - out) > 1.5

def _pin_best_variant(url: str, hdrs: dict, ceiling: int = 0) -> str:
    """Master playlist -> URL of the best variant under the ceiling (closest above only when none fits).
    ffmpeg's HLS reader takes the FIRST variant and most hosts list the low ones first: that is the soft copy.
    Returns `url` unchanged on any problem."""
    try:
        head = _manifest_head(url, hdrs)
        if "#EXT-X-STREAM-INF" not in head:
            return url
        q = urlparse(url).query
        v = []
        for m in re.finditer(r"#EXT-X-STREAM-INF:([^\n]*)\n([^\n#]+)", head):
            res = re.search(r"RESOLUTION=(\d+)x(\d+)", m.group(1), re.I)
            bw = re.search(r"BANDWIDTH=(\d+)", m.group(1), re.I)
            uri = urljoin(url, m.group(2).strip())
            if "?" not in uri and q:
                uri += "?" + q          # the token lives on the master: carry it to the variant
            rank = int(res.group(2)) if res else 0
            v.append((rank, int(bw.group(1)) if bw else 0, uri))
        if not v: return url
        if ceiling:
            low = [x for x in v if 0 < x[0] <= ceiling]
            if low: return max(low, key=lambda x: (x[0], x[1]))[2]
            over = [x for x in v if x[0] > ceiling]
            if over: return min(over, key=lambda x: x[0])[2]
        return max(v, key=lambda x: (x[0], x[1]))[2]
    except Exception:
        return url

async def _hls_chain(url: str, status_msg, cancel_flag: list, uid: int, chat_id: int, page_url: str = "", title: str = "", referer: str = "", extra_headers: dict = None,
                     ua: str = "", state: dict = None, early_exit: bool = False, skip_ytdlp: bool = False) -> bool:
    """One pass of the HLS/DASH legs (yt-dlp, FFmpeg, N_m3u8DL-RE) with ONE request profile (the User-Agent `ua`).
    state flags: refused (a leg was refused with HTTP 403), reached (a file was handed to upload), early (stopped
    after the first yt-dlp refusal so a fallback profile can be tried sooner). A User-Agent captured by the
    sniffer (extra_headers) still wins over `ua`."""
    ua = ua or _STANDARD_UA
    state = state if state is not None else {}
    _canon = {"user-agent": "User-Agent", "referer": "Referer", "origin": "Origin",
              "accept": "Accept", "accept-language": "Accept-Language"}
    extra_headers = {_canon[str(k).lower()]: re.sub(r"[\r\n]", "", str(v)).strip()[:500]
                     for k, v in (extra_headers or {}).items() if str(k).lower() in _canon}
    clean_url = url.replace("&amp;", "&").replace("\\", "").strip()
    _referer = clean_referer(referer or page_url or clean_url)
    base_referer = f"{urlparse(_referer).scheme}://{urlparse(_referer).netloc}/"
    _safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or f"video_{uid}"
    ydl_out = os.path.join(ARIA2C_DOWNLOAD_DIR, f"hls_{uid}_{int(time.time())}.mp4")
    loop = asyncio.get_running_loop()
    last = [0.0]
    ceiling = _ACTIVE_CEILING[0]

    # ── DRM sniff: AES-128 / SAMPLE-AES decrypt themselves, Widevine needs a CDM ──
    drm = ""
    if DRM_PROBE:
        drm = _drm_kind(await asyncio.to_thread(
            _manifest_head, clean_url, {"User-Agent": ua, "Referer": _referer}))
        if drm:
            _diag(uid, "DRM", "manifest", drm)
        if drm in ("playready",) or (drm == "widevine" and _wv_cdm() is None):
            _diag(uid, "DRM", "unsupported", f"{drm}, no CDM configured")
            _stat("HLS", False)
            return False
    _cdm = _wv_cdm() if drm == "widevine" else None

    def hook(d):
        if cancel_flag[0]: raise Exception("__CANCELLED__")
        st = d.get("status")
        if st not in ("downloading", "processing"): return
        done = d.get("fragment_index", 0) or d.get("downloaded_bytes", 0)
        total = d.get("fragment_count", 0) or d.get("total_bytes", 0) or d.get("total_bytes_estimate", 0)
        now_time = time.time()
        if now_time - last[0] < 2.0: return
        last[0] = now_time

        if d.get("fragment_count"):
            pct = int((done / total) * 100) if total else 0
            bar = "▓" * int(16 * pct / 100) + "░" * (16 - int(16 * pct / 100))
            text = f"📥 *Step 2/4: Streaming HLS/DASH Chunks...*\n*{_safe_title[:40]}*\n\n[{bar}] {pct}% ({done}/{total} parts)"
        else:
            text = f"📥 *Step 2/4: Streaming Stream...*\n*{_safe_title[:40]}*\n\n{progress_bar(done, total)}"
        asyncio.run_coroutine_threadsafe(safe_edit(status_msg, text), loop)

    def _run_ytdl_hls(ref_to_use):
        headers_dict = {
            "User-Agent": ua,
            "Accept-Language": _STANDARD_LANG,
            "Referer": clean_referer(ref_to_use),
        }
        headers_dict.update(extra_headers)  # allowlisted sniffer headers win
        ydl_opts = {
            "format": _ydl_format(ceiling),
            "outtmpl": ydl_out,
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "concurrent_fragment_downloads": 4,
            "progress_hooks": [hook],
            "http_headers": headers_dict,
        }
        if _YDL_IMPERSONATE:
            ydl_opts["impersonate"] = "chrome"
            ydl_opts["extractor_args"] = {"generic": {"impersonate": True}}
        if _cdm is not None:
            # decrypted fragments in one run: yt-dlp walks the license itself
            ydl_opts["allow_unplayable_formats"] = True
            ydl_opts["cdm"] = _cdm
            ydl_opts["format"] = "bestvideo+bestaudio/best"
        ydl_opts.update(_YDL_RELIABLE)
        if COOKIES_FILE: ydl_opts["cookiefile"] = COOKIES_FILE
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([clean_url])

    for active_ref in ([] if skip_ytdlp else [_referer, base_referer]):
        try:
            await loop.run_in_executor(None, lambda r=active_ref: _run_ytdl_hls(r))
            if os.path.exists(ydl_out) and _probe_has_media_stream(ydl_out):
                state["reached"] = True
                _stat("HLS", True)
                return await _finish_upload(ydl_out, _safe_title, status_msg, uid, chat_id)
            _diag(uid, "HLS via yt-dlp", "output check", "no media stream in output")
        except Exception as e:
            _diag(uid, "HLS via yt-dlp", _classify_hls_stage(e), _diag_detail(e))
            if os.path.exists(ydl_out):
                try: os.remove(ydl_out)
                except: pass
            if _http_status(e) == "HTTP 403":
                state["refused"] = True
                if early_exit:        # a fallback profile is available: try it now, the rest of this profile comes later
                    state["early"] = True
                    _stat("HLS", False)
                    return False

    # FFmpeg fallback with stream repair & resilience flags
    await safe_edit(status_msg, "⚡ *Retrying stream via FFmpeg core...*", force=True)
    # pin the wanted variant: the HLS reader would otherwise take the first (lowest) one
    ff_url = clean_url
    if drm in ("", "aes-128", "sample-aes"):
        ff_url = await asyncio.to_thread(_pin_best_variant, clean_url,
                                        {"User-Agent": ua, "Referer": _referer}, ceiling)
    header_str = f"User-Agent: {ua}\r\nAccept-Language: {_STANDARD_LANG}\r\nReferer: {_referer}\r\n"
    for _hk, _hv in extra_headers.items():
        header_str += f"{_hk}: {_hv}\r\n"
    ff_cmd = [
        FFMPEG_PATH, "-y",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
        "-headers", header_str,
        "-fflags", "+genpts+discardcorrupt",
        "-avoid_negative_ts", "make_zero",
        "-err_detect", "ignore_err",
        "-i", ff_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        ydl_out
    ]
    try:
        proc = await asyncio.create_subprocess_exec(*ff_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, ff_err = await proc.communicate()
        ff_text = (ff_err or b"").decode(errors="replace")
        if os.path.exists(ydl_out) and _probe_has_media_stream(ydl_out):
            if await asyncio.to_thread(_ff_looks_truncated, ff_text, ydl_out):
                _diag(uid, "HLS via ffmpeg", "output check", "incomplete - output much shorter than the playlist")
                try: os.remove(ydl_out)
                except Exception: pass
            else:
                state["reached"] = True
                _stat("HLS", True)
                return await _finish_upload(ydl_out, _safe_title, status_msg, uid, chat_id)
        err_tail = ff_text[-2000:]
        if re.search(r"\b403\b", err_tail) and "forbidden" in err_tail.lower(): state["refused"] = True
        if not any(e[0] == "HLS via ffmpeg" and e[1] == "output check" and e[2].startswith("incomplete") for e in (dl_diag.get(uid) or [])):
            _diag(uid, "HLS via ffmpeg", _classify_hls_stage(err_tail), _diag_detail(err_tail))
        if _TITANVAULT_DEBUG and err_tail:
            logging.info(f"[diag] ffmpeg stderr tail: {err_tail[-400:]}")
    except Exception as e:
        _diag(uid, "HLS via ffmpeg", "download", _diag_detail(e))
        logging.warning(f"[FFmpeg core error]: {e}")

    # Last HLS leg: external N_m3u8DL-RE (reports "disabled" / "not installed" itself).
    # The exe has no CDM, so a Widevine manifest it cannot help with is skipped outright.  ← changed
    if drm == "widevine":
        _diag(uid, "HLS via N_m3u8DL-RE", "skipped", "Widevine manifest, the exe has no CDM")
    else:
        if await _do_dl_via_nm3u8dl(url, status_msg, cancel_flag, uid, chat_id, page_url=page_url, title=title, referer=referer,
                                    extra_headers=extra_headers, ua=ua, state=state):
            _stat("HLS", True)
            return True

    _stat("HLS", False)
    return False

async def _do_dl_via_ffmpeg_hls(url: str, status_msg, cancel_flag: list, uid: int, chat_id: int, page_url: str = "", title: str = "", referer: str = "", extra_headers: dict = None) -> bool:
    """HLS/DASH with a bounded request-profile fallback. The desktop profile (the bot's usual User-Agent) goes first,
    so working downloads behave exactly as before. Only when a leg was refused with HTTP 403, the other profile
    (mobile User-Agent) is tried, carried through the playlist, segment and engine requests of that pass. The profile
    that worked is remembered per host, so the next link from it starts there and the other one is the fallback.
    At most one extra pass; no cookies, no TLS or challenge handling; a file that reached upload is never redone.
    A User-Agent captured by the sniffer is a real browser request and is used as is (no fallback)."""
    kw = dict(page_url=page_url, title=title, referer=referer, extra_headers=extra_headers)
    # the sniffer's own context uses _STANDARD_UA, so that value is not a captured browser UA; only a different one is
    if any(str(k).lower() == "user-agent" and str(v).strip() != _STANDARD_UA for k, v in (extra_headers or {}).items()):
        return await _hls_chain(url, status_msg, cancel_flag, uid, chat_id, **kw)
    host = urlparse(url.replace("&amp;", "&").strip()).netloc.lower()
    order = [("desktop", _STANDARD_UA), ("mobile", _MOBILE_UA)]
    if _UA_PREF.get(host) == "mobile": order.reverse()
    first, second = order
    st1 = {}
    if await _hls_chain(url, status_msg, cancel_flag, uid, chat_id, ua=first[1], state=st1, early_exit=True, **kw):
        return True
    if cancel_flag[0] or st1.get("reached") or not st1.get("refused"):
        return False
    _diag(uid, "Request profile", second[0], f"retrying after the {first[0]} profile was refused (HTTP 403)")
    await safe_edit(status_msg, f"🔁 *Retrying with a {second[0]} browser profile...*", force=True)
    st2 = {}
    if await _hls_chain(url, status_msg, cancel_flag, uid, chat_id, ua=second[1], state=st2, **kw):
        if len(_UA_PREF) > 200: _UA_PREF.clear()
        _UA_PREF[host] = second[0]
        return True
    if cancel_flag[0] or st2.get("reached"):
        return False
    if st1.get("early"):   # the first profile stopped after its first refusal: run its remaining legs too
        _diag(uid, "Request profile", first[0], "remaining engines of the first profile")
        return await _hls_chain(url, status_msg, cancel_flag, uid, chat_id, ua=first[1], state={}, skip_ytdlp=True, **kw)
    return False

async def _dl_hls_with_refresh(target: str, status_msg, cancel_flag: list, uid: int, chat_id: int,
                               page_url: str, title: str, referer: str, extra_headers: dict, refresh_cb) -> bool:
    """HLS download with ONE mid-flight token refresh. Signed stream URLs die in minutes, so a
    run that starts fine and 403s at segment 40 is a token problem, not a dead host: re-resolve
    the page once and continue on the fresh URL. Generic path only - the callback is whatever
    the caller already uses to resolve, and it is tried at most once per job."""
    if await _do_dl_via_ffmpeg_hls(target, status_msg, cancel_flag, uid, chat_id,
                                   page_url=page_url, title=title, referer=referer,
                                   extra_headers=extra_headers):
        return True
    if cancel_flag[0] or refresh_cb is None:
        return False
    if not any(e[2].startswith("HTTP 403") for e in (dl_diag.get(uid) or [])):
        return False
    await safe_edit(status_msg, "🔁 *Link expired (403) - resolving a fresh one...*", force=True)
    try:
        fresh = await asyncio.to_thread(refresh_cb)
    except Exception:
        fresh = ""
    if not fresh or fresh == target:
        _diag(uid, "Refresh", "resolve", "no fresh link")
        return False
    _diag(uid, "Refresh", "resolve", "fresh link obtained after HTTP 403")
    return await _do_dl_via_ffmpeg_hls(fresh, status_msg, cancel_flag, uid, chat_id,
                                       page_url=page_url, title=title, referer=referer,
                                       extra_headers=extra_headers)

# ══════════════════════════════════════════════════════
#  N_m3u8DL-RE LEG (experimental, optional)
# ══════════════════════════════════════════════════════
_NM_BIN_CACHE = [None, False]   # [path, looked_up]

def _nm_find_binary() -> str:
    if _NM_BIN_CACHE[1]:
        return _NM_BIN_CACHE[0] or ""
    import glob
    exe = "N_m3u8DL-RE.exe" if os.name == "nt" else "N_m3u8DL-RE"
    home = os.path.expanduser("~")
    cands = []
    if N_M3U8DL_PATH:
        cands.append(N_M3U8DL_PATH)
        if os.path.isdir(N_M3U8DL_PATH): cands.append(os.path.join(N_M3U8DL_PATH, exe))
    cands.append(os.path.join(_SCRIPT_DIR, exe))
    found = shutil.which(exe) or shutil.which("N_m3u8DL-RE")
    if found: cands.append(found)
    cands += sorted(glob.glob(os.path.join(home, "Downloads", "N_m3u8DL-RE*win-x64*", exe)), reverse=True)
    path = next((c for c in cands if c and os.path.isfile(c)), "")
    _NM_BIN_CACHE[0], _NM_BIN_CACHE[1] = path, True
    return path

def _nm_find_ffmpeg() -> str:
    """Absolute ffmpeg path to hand to the exe (--ffmpeg-binary-path), or "".
    The bot itself runs plain "ffmpeg"; on Windows that also resolves from the script/current folder,
    which a separately launched exe does not search - so resolve it here and pass it explicitly."""
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    cands = []
    w = shutil.which(FFMPEG_PATH)
    if w: cands.append(w)
    if os.path.isabs(FFMPEG_PATH): cands.append(FFMPEG_PATH)
    for d in (_SCRIPT_DIR, os.getcwd()):
        cands.append(os.path.join(d, exe))
    hit = next((c for c in cands if c and os.path.isfile(c)), "")
    return os.path.abspath(hit) if hit else ""

def _pick_policy_variant(variants: list, ceiling: int = 0):
    """Download policy: highest height under the ceiling; only when nothing fits, the CLOSEST higher
    height. Returns (width, height) or None. Same policy as _ydl_format."""
    if not variants: return None
    if ceiling:
        low = [v for v in variants if v[1] <= ceiling]
        if low: return max(low, key=lambda v: (v[1], v[2]))[:2]
        over = [v for v in variants if v[1] > ceiling]
        if over: return min(over, key=lambda v: (v[1], -v[2]))[:2]
    best = max(variants, key=lambda v: (v[1], v[2]))
    return best[0], best[1]

def _nm_build_args(url: str, job_dir: str, headers: dict, target, ffmpeg_bin: str = "") -> list:
    """Arguments after the executable. target = (w, h) from the policy picker or None.
    Selectors are always given (the tool prompts / crashes headless without them)."""
    args = [url,
            "--save-dir", job_dir, "--save-name", "nm", "--tmp-dir", os.path.join(job_dir, "tmp"),
            "--thread-count", str(max(1, N_M3U8DL_THREADS)), "--download-retry-count", "3",
            "--http-request-timeout", "30", "--no-log", "--no-ansi-color", "--del-after-done"]
    if ffmpeg_bin: args += ["--ffmpeg-binary-path", ffmpeg_bin]
    for k, v in headers.items():
        v = re.sub(r"[\r\n]", "", str(v)).strip()
        if v: args += ["-H", f"{k}: {v}"]
    if target:
        args += ["-sv", f'res="^{target[0]}x{target[1]}$":for=best']
    else:
        args += ["-sv", "best"]
    args += ["-sa", "best", "-ds", "for=all", "-M", "format=mp4:muxer=ffmpeg"]
    return args

def _nm_classify(output_tail: str, selected_seen: bool):
    """(stage, detail) from a fixed vocabulary - raw tool output (may hold URLs) is never reported."""
    m = re.search(r"status code[^:\n]*:\s*(\d{3})", output_tail, re.I) or re.search(r"HTTP (\d{3})", output_tail)
    low = output_tail.lower()
    stage = "segment fetch" if selected_seen else "playlist fetch"
    if m: return stage, f"HTTP {m.group(1)}"
    if "timed out" in low or "timeout" in low or "canceled" in low: return stage, "timeout"
    if any(x in low for x in ("name or service", "no such host", "connection", "refused", "unreachable")): return stage, "connection error"
    if "drm" in low or "encrypted" in low or "key" in low and "decrypt" in low: return "decrypt", "encrypted stream (not supported)"
    if "ffmpeg" in low and ("not found" in low or "not exist" in low): return "merge", "ffmpeg not found"
    if "merg" in low or "mux" in low: return "merge", "merge error"
    return stage, "download error"

def _kill_proc_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            proc.kill()
    except Exception:
        try: proc.kill()
        except Exception: pass

def _probe_stream_kinds(path: str) -> list:
    try:
        r = subprocess.run([FFPROBE_PATH, "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", path],
                           capture_output=True, text=True, timeout=30)
        return [s.get("codec_type") for s in json.loads(r.stdout).get("streams", [])]
    except Exception:
        return []

def _nm_probe_args(url: str, probe_dir: str, headers: dict, ffmpeg_bin: str = "") -> list:
    """Probe run: the exe fetches and parses the manifest itself, writes meta.json, downloads nothing."""
    args = [url, "--save-dir", probe_dir, "--save-name", "probe", "--tmp-dir", os.path.join(probe_dir, "tmp"),
            "--http-request-timeout", "30", "--no-log", "--no-ansi-color", "--skip-download", "--write-meta-json"]
    if ffmpeg_bin: args += ["--ffmpeg-binary-path", ffmpeg_bin]
    for k, v in headers.items():
        v = re.sub(r"[\r\n]", "", str(v)).strip()
        if v: args += ["-H", f"{k}: {v}"]
    args += ["-sv", "best", "-sa", "best", "-ds", "for=all"]   # selectors keep the tool headless
    return args

def _nm_policy_from_meta(meta_path: str):
    """("pin", (w, h)) | ("single", None) | ("unknown", None) | ("error", None) from the exe's own meta.json.
    Only the Resolution of video entries is used; URLs inside the file are never read or logged."""
    try:
        with open(meta_path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:
        return "error", None
    if not isinstance(data, list): return "error", None
    vids = [e for e in data if isinstance(e, dict) and str(e.get("MediaType") or "VIDEO").upper() == "VIDEO"]
    if not vids: return "unknown", None
    sizes = []
    for e in vids:
        m = re.match(r"^\s*(\d+)x(\d+)\s*$", str(e.get("Resolution") or ""))
        if not m:
            return ("single", None) if len(vids) == 1 else ("unknown", None)
        sizes.append((int(m.group(1)), int(m.group(2)), int(e.get("Bandwidth") or 0)))
    return "pin", _pick_policy_variant(sizes, _ACTIVE_CEILING[0])

_NM_REFUSAL_RE = re.compile(r"status code[^:\n]*:\s*(?:401|403)[^\n]*?\(2/\d+\)")
_NM_STOPPED_EARLY = -999   # returncode we report when we stopped the exe after repeated refusals

async def _nm_exec(binary: str, args: list, cwd: str, cancel_flag: list, on_chunk=None, timeout: int = 7200, stop_re=None):
    """Run the exe. Returns (returncode, output_tail, timed_out). Raises OSError/NotImplementedError when it
    cannot be launched and Exception("__CANCELLED__") on cancel (process tree killed either way)."""
    proc = await asyncio.create_subprocess_exec(
        binary, *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=cwd)
    tail = [""]
    stopped = [False]
    async def _reader():
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk: break
            t = chunk.decode("utf-8", errors="replace")
            tail[0] = (tail[0] + t)[-6000:]
            if stop_re is not None and not stopped[0] and stop_re.search(tail[0]):
                stopped[0] = True          # the same request was refused twice: do not let the exe repeat it 10 times
                _kill_proc_tree(proc)
            if on_chunk: await on_chunk(t)
    rtask = asyncio.ensure_future(_reader())
    try:
        t0 = time.time()
        while proc.returncode is None:
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), 1.0)
            except asyncio.TimeoutError:
                pass
            if cancel_flag[0]:
                raise Exception("__CANCELLED__")
            if time.time() - t0 > timeout:
                _kill_proc_tree(proc)
                return proc.returncode if proc.returncode is not None else -1, tail[0], True
        try: await asyncio.wait_for(rtask, 10)
        except Exception: rtask.cancel()
        if stopped[0]:
            return _NM_STOPPED_EARLY, tail[0], False
        return proc.returncode, tail[0], False
    finally:
        if proc.returncode is None:
            _kill_proc_tree(proc)
            try: await asyncio.wait_for(proc.wait(), 5)
            except Exception: pass
        if not rtask.done(): rtask.cancel()

async def _do_dl_via_nm3u8dl(url: str, status_msg, cancel_flag: list, uid: int, chat_id: int, page_url: str = "",
                             title: str = "", referer: str = "", extra_headers: dict = None, ua: str = "", state: dict = None) -> bool:
    """HLS/DASH via the external N_m3u8DL-RE binary, in two runs of the exe itself:
    1) probe (--skip-download --write-meta-json): the exe fetches the manifest with our headers, we read the
       variant sizes from its meta.json and apply the quality policy; no request is made by the bot itself.
    2) download with the chosen variant pinned. Output is validated with ffprobe and handed to _finish_upload.
    Returns False (with a fixed-vocabulary diagnostic) on any failure."""
    leg = "HLS via N_m3u8DL-RE"
    state = state if state is not None else {}
    if N_M3U8DL_MODE == "off":
        _diag(uid, leg, "disabled", "N_M3U8DL_MODE=off")
        return False
    binary = _nm_find_binary()
    if not binary:
        _diag(uid, leg, "not installed", "exe not found")
        return False
    _canon = {"user-agent": "User-Agent", "referer": "Referer", "origin": "Origin",
              "accept": "Accept", "accept-language": "Accept-Language"}
    clean_url = url.replace("&amp;", "&").replace("\\", "").strip()
    _ref = clean_referer(referer or page_url or clean_url)
    headers = {"User-Agent": ua or _STANDARD_UA, "Accept-Language": _STANDARD_LANG, "Referer": _ref}
    headers.update({_canon[str(k).lower()]: str(v)[:500] for k, v in (extra_headers or {}).items()
                    if str(k).lower() in _canon})
    _safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or f"video_{uid}"
    job_dir = os.path.join(ARIA2C_DOWNLOAD_DIR, f"nm_{uid}_{int(time.time())}")
    probe_dir = os.path.join(job_dir, "probe")
    os.makedirs(probe_dir, exist_ok=True)
    ffbin = _nm_find_ffmpeg()
    if not ffbin:
        _diag(uid, leg, "ffmpeg", "not located by the bot (the exe searches its own PATH)")
    last_edit = [0.0]
    try:
        # ---- 1) probe ----
        await safe_edit(status_msg, f"🔍 *Step 2/4: Checking stream qualities...*\n*{_safe_title[:40]}*", force=True)
        try:
            rc, tail, timed_out = await _nm_exec(binary, _nm_probe_args(clean_url, probe_dir, headers, ffbin), job_dir,
                                                 cancel_flag, timeout=180, stop_re=_NM_REFUSAL_RE)
        except (OSError, NotImplementedError) as e:
            code = getattr(e, "winerror", None) or getattr(e, "errno", None)
            _diag(uid, leg, "launch failed", f"{type(e).__name__}" + (f" {code}" if code else ""))
            return False
        meta_path = os.path.join(probe_dir, "tmp", "probe", "meta.json")
        if timed_out or rc != 0 or not os.path.isfile(meta_path):
            if timed_out:
                _diag(uid, leg, "probe failed", "timeout (exe request)")
            else:
                if rc == _NM_STOPPED_EARLY: state["refused"] = True
                stage, detail = _nm_classify(tail, False)
                _diag(uid, leg, "probe failed", f"{stage} - {detail} (exe request)" + (" (stopped after the request was refused twice)" if rc == _NM_STOPPED_EARLY else f" (exit code {rc})" if rc not in (0, None) else ""))
            return False
        kind, target = _nm_policy_from_meta(meta_path)
        shutil.rmtree(probe_dir, ignore_errors=True)       # meta.json holds full segment URLs/tokens
        if kind in ("unknown", "error"):
            _diag(uid, leg, "skipped before launch", "variant sizes unknown" if kind == "unknown" else "probe result unreadable")
            return False
        _diag(uid, leg, "probe ok", "single stream" if kind == "single" else "variant pinned")

        # ---- 2) download ----
        args = _nm_build_args(clean_url, job_dir, headers, target, ffbin)
        sel = [False]
        async def _on_chunk(t):
            if "Selected streams" in t: sel[0] = True
            ms = re.findall(r"(\d+)/(\d+)\s+(\d+(?:\.\d+)?)%", t)
            if ms:
                d, n, pc = ms[-1]
                now = time.time()
                if now - last_edit[0] >= 2.0:
                    last_edit[0] = now
                    pct = int(float(pc)); bar = "▓" * int(16 * pct / 100) + "░" * (16 - int(16 * pct / 100))
                    await safe_edit(status_msg, f"📥 *Step 2/4: Streaming HLS/DASH Chunks...*\n*{_safe_title[:40]}*\n\n[{bar}] {pct}% ({d}/{n} parts)")
        await safe_edit(status_msg, f"📥 *Step 2/4: Retrying stream with N_m3u8DL-RE...*\n*{_safe_title[:40]}*", force=True)
        _diag(uid, leg, "started", "exe launched")
        try:
            rc, tail, timed_out = await _nm_exec(binary, args, job_dir, cancel_flag, on_chunk=_on_chunk, timeout=N_M3U8DL_TIMEOUT_SEC)
        except (OSError, NotImplementedError) as e:
            code = getattr(e, "winerror", None) or getattr(e, "errno", None)
            _diag(uid, leg, "launch failed", f"{type(e).__name__}" + (f" {code}" if code else ""))
            return False
        if timed_out:
            _diag(uid, leg, "download", "timeout")
            return False
        outs = [os.path.join(job_dir, f) for f in os.listdir(job_dir)
                if os.path.isfile(os.path.join(job_dir, f)) and os.path.getsize(os.path.join(job_dir, f)) > 0]
        good = [f for f in outs if f.lower().endswith(".mp4") and "video" in _probe_stream_kinds(f)]
        if rc != 0 or not good:
            stage, detail = _nm_classify(tail, sel[0] or "Selected streams" in tail)
            if rc == 0: stage, detail = "output check", "no video stream in output"
            elif rc is not None: detail = f"{detail} (exit code {rc})"
            _diag(uid, leg, stage, detail)
            return False
        state["reached"] = True
        return await _finish_upload(max(good, key=os.path.getsize), _safe_title, status_msg, uid, chat_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if "__CANCELLED__" in str(e): raise
        _diag(uid, leg, "download", _diag_detail(e))
        return False
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
        try:
            if not any(k.startswith(job_dir) for k in _KEPT_SOURCES):
                shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass

async def _find_in_storage(title: str, size: int, since: float):
    """Look for our own just-sent upload in the storage channel (same caption, same size, recent).
    Returns the message, False when the recent messages were read and it is not there, or None when the
    check itself could not be made. 'Not there' is a snapshot, not proof the server never processed it."""
    try:
        msgs = await asyncio.wait_for(tgclient.get_messages(STORAGE_CHANNEL_ID, limit=20), 15)
    except Exception:
        return None
    for m in msgs or []:
        f = getattr(m, "file", None)
        try:
            when = m.date.timestamp()
        except Exception:
            when = 0
        if f and (size is None or getattr(f, "size", None) == size) and (getattr(m, "message", "") or "") == title and when >= since - 120:
            return m
    return False

async def _finish_upload(file_path: str, title: str, status_msg, uid: int, chat_id: int, skip_convert: bool = False) -> bool:
    loop  = asyncio.get_running_loop()
    t0    = time.time()
    last  = [0.0]
    clean_title = clean_name(title) or title
    ext      = os.path.splitext(file_path)[1].lower()
    is_video = ext in (".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".flv", ".vid")   # ← .vid added
    thumb = ""
    tmp   = file_path
    uploaded = False   # SAFETY: the original is only deleted after Telegram confirmed the upload
    cancelled = False  # the user pressed Cancel while this job was converting or uploading
    send_state = None  # None | "notfound" | "unknown": the final send was cancelled in flight, outcome unconfirmed
    late_note = ""
    media_exts = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".flv", ".m4v", ".vid",
                  ".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac")                # ← .vid added
    if ext in media_exts and not _probe_has_media_stream(file_path):
        # Server delivered a non-media payload (error page etc.) under a media name.
        logging.warning(f"[finish_upload] rejected non-media payload: {os.path.basename(file_path)}")
        await safe_final(status_msg, "❌ The site did not deliver a playable media file (likely an error page or expired link).")
        try: os.remove(file_path)
        except Exception: pass
        return False
    _status_kb[_msg_key(status_msg)] = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data=f"DL_CANCEL|{uid}")]])
    try:
        converted = False
        if is_video and not skip_convert:
            spec = user_convert.get(uid, {})
            if spec:
                await safe_edit(status_msg, f"⚙️ *Step 3/4: Converting ({describe_convert(spec)})...*", force=True)
                tmp = await apply_convert(tmp, spec, status_msg)
                converted = True
                if os.path.splitext(tmp)[1].lower() in AUDIO_EXTS:
                    is_video = False
        if is_video:
            ceiling = _q_ceiling(uid)
            if not converted and not skip_convert:
                _, _, src_h = await loop.run_in_executor(None, get_video_info, tmp)
                if ceiling and src_h > ceiling:
                    # Source only existed above the ceiling: resize down to it
                    # (aspect preserved via scale=-2:ceiling, crf 20), one output file.
                    await safe_edit(status_msg, f"⚙️ *Step 3/4: Source is {src_h}p - resizing to {ceiling}p...*", force=True)
                    tmp = await apply_convert(tmp, {"scale": ceiling}, status_msg)
                    converted = True
            tmp   = await compress_to_limit(tmp, status_msg)
            if not converted and not skip_convert:
                await safe_edit(status_msg, "⚙️ *Step 3/4: Optimising video format...*", force=True)
                tmp   = await _remux_live(tmp, status_msg)

            if uid in user_thumbnails and os.path.exists(user_thumbnails[uid]):
                thumb = user_thumbnails[uid]
            else:
                raw_thumb = await loop.run_in_executor(None, extract_thumb, tmp)
                thumb = prepare_telegram_thumb(raw_thumb) if raw_thumb else ""

            duration, width, height = await loop.run_in_executor(None, get_video_info, tmp)
        else:
            duration, width, height = 0, 0, 0

        filesize = os.path.getsize(tmp)
        _uploading.add(chat_id)
        await safe_edit(status_msg, f"☁️ *Step 4/4: Uploading {clean_title[:40]} ({fmt_size(filesize)})...*", force=True)

        async def ul_progress(current, total):
            now = time.time()
            if now - last[0] < 2.0: return
            last[0] = now
            await safe_edit(status_msg,
                f"☁️ *Step 4/4: Uploading to Vault...*\n*{clean_title[:40]}*\n\n" +
                progress_bar(current, total) +
                speed_eta(current, total, now - t0))

        attrs = [DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)] if is_video else []
        input_file = await fast_upload(tgclient, tmp, progress_callback=ul_progress)

        # Automatic FloodWait recovery loop (handles "SendMediaRequest" pauses)
        sent = None
        send_since = time.time()
        attempt = floods = 0
        while attempt < 5:
            try:
                send_task = asyncio.ensure_future(tgclient.send_file(
                    STORAGE_CHANNEL_ID,
                    file=input_file,
                    caption=clean_title,
                    supports_streaming=is_video,
                    thumb=thumb if (is_video and thumb) else None,
                    attributes=attrs,
                ))
                try:
                    sent = await asyncio.shield(send_task)
                except asyncio.CancelledError:
                    # Cancel arrived while the final send request was already on its way. It cannot be
                    # recalled: give it a short moment to settle, then record the real outcome.
                    try: sent = await asyncio.wait_for(asyncio.shield(send_task), _SEND_SETTLE_SECS)
                    except BaseException: sent = None
                    if sent:
                        late_note = "\nℹ️ Cancel arrived after Telegram had already received the upload, so it completed."
                    else:
                        send_task.cancel()
                        _cmp_size = filesize if tmp == file_path else None   # a converted copy's size differs from a later retry's
                        found = await _find_in_storage(clean_title, _cmp_size, send_since)
                        # A caption/size match is a hint, never proof: nothing is recorded or deleted from it.
                        send_state = "candidate" if found else ("unknown" if found is None else "notfound")
                        raise asyncio.CancelledError()
                break
            except FloodWaitError as fe:
                # definite refusal (nothing was accepted): hold the finished file, wait the full time, send again
                floods += 1   # no cap: every wait is the server's, it is waited out in full; Cancel stays available
                _user_flood_until[0] = max(_user_flood_until[0], time.time() + fe.seconds)
                for rem in range(fe.seconds + 1, 0, -5):
                    await safe_edit(status_msg, f"⏳ *Telegram rate limit active:* upload paused, {rem}s left{f' (wait {floods})' if floods > 1 else ''}. Your file is kept and sends automatically; waiting links stay queued.", force=True)
                    await asyncio.sleep(min(5, rem))
            except Exception as e:
                attempt += 1
                logging.error(f"Upload error attempt {attempt}: {e}")
                await asyncio.sleep(2)

        if not sent:
            await safe_final(status_msg, f"❌ Upload failed due to persistent Telegram rate limits.\nYour download was kept: `{os.path.basename(file_path)}`")
            return False

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO files (msg_id, name) VALUES (?,?)", (sent.id, clean_title))
            _new_file_id = cur.lastrowid
            conn.commit()

        uploaded = True
        _upload_count[chat_id] = _upload_count.get(chat_id, 0) + 1
        _status_kb.pop(_msg_key(status_msg), None)
        await safe_edit(status_msg, f"✅ *Completed!*\n*{clean_title[:60]}*\n💾 {fmt_size(filesize)}{late_note}", force=True)

        reply_kb = None
        if uid in user_thumbnails:
            reply_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Flush Thumbnail", callback_data="FLUSH_THUMB")]])

        try:
            await status_msg.get_bot().copy_message(
                chat_id=chat_id,
                from_chat_id=STORAGE_CHANNEL_ID,
                message_id=sent.id,
                reply_markup=reply_kb
            )
        except Exception:
            try: await tgclient.forward_messages(chat_id, sent.id, from_peer=STORAGE_CHANNEL_ID)
            except Exception: pass
        if _ask_name_on(chat_id):
            asyncio.ensure_future(_ask_name_prompt(status_msg.get_bot(), chat_id, _new_file_id, clean_title))
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        _uploading.discard(chat_id)
        _status_kb.pop(_msg_key(status_msg), None)
        # compressed / remuxed copies are always temporary
        if tmp and tmp != file_path and os.path.exists(tmp):
            try: os.remove(tmp)
            except Exception: pass
        # the original goes only after a confirmed upload; after a failure it stays so nothing is lost
        if uploaded and file_path and os.path.exists(file_path):
            try: os.remove(file_path)
            except Exception: pass
        elif not uploaded and file_path and os.path.exists(file_path):
            logging.warning(f"Upload not confirmed; kept source file: {os.path.basename(file_path)}")
            try:
                if _inside_job_dir(file_path):
                    _KEPT_SOURCES.add(file_path)
                    rid = _kept_register(file_path, clean_title, chat_id, send_state or "")
                    _lead = "⛔ Upload cancelled." if cancelled else "⚠️ Upload not confirmed."
                    if send_state:
                        _lead += {
                            "unknown": " The last step was interrupted and I could not check the vault: Telegram may already have this file.",
                            "notfound": " The last step was interrupted. I did not find it in the vault when I checked, but I cannot prove Telegram never processed it.",
                            "candidate": " The last step was interrupted. A message that looks like this file (same caption and size) is in the vault, but I cannot be sure it is this upload, so nothing was recorded and your file was not deleted.",
                        }[send_state] + " Check the vault before choosing."
                    await status_msg.reply_text(
                        f"{_lead} Your file is kept ({fmt_size(os.path.getsize(file_path))}): `{os.path.basename(file_path)[:50]}`",
                        parse_mode="Markdown", reply_markup=_kept_kb(rid, send_state or ""))
            except Exception as _e:
                logging.warning(f"Could not register kept file: {type(_e).__name__}")
    return uploaded

# ══════════════════════════════════════════════════════
#  LOG-SAFE URLS (never log query strings / tokens)
# ══════════════════════════════════════════════════════
def redact_url(url: str, max_len: int = 90) -> str:
    """scheme://host/path only. Query strings and fragments often carry signed tokens."""
    try:
        p = urlparse(url)
        base = f"{p.scheme}://{p.hostname or ''}{p.path}"
        if p.query: base += "?…"
        return base if len(base) <= max_len else base[:max_len - 1] + "…"
    except Exception:
        return "<url>"

def _valid_http_url(url: str, max_len: int = 4096) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.hostname) and len(url) <= max_len and not any(ch in url for ch in "\r\n\x00 ")
    except Exception:
        return False


# ══════════════════════════════════════════════════════
#  DIRECT STREAM INPUT  (/stream <media-or-playlist-url> [referer-page-url])
#  Skips page extraction: the URL you give is treated as the media itself.
#  No cookies are accepted; Referer is the only optional context.
# ══════════════════════════════════════════════════════
async def _run_direct_stream(url: str, status, uid: int, chat_id: int, referer: str = "", title: str = ""):
    cancel_flag = [False]
    dl_cancel_flags[uid] = cancel_flag
    _diag_reset(uid)
    _ACTIVE_CEILING[0] = _q_ceiling(uid)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"DL_CANCEL|{uid}")]])
    tmp_out = os.path.join(ARIA2C_DOWNLOAD_DIR, f"stream_{uid}_{int(time.time())}.mp4")
    clean_url = url.replace("&amp;", "&").strip()
    ref = clean_referer(referer) if referer else ""
    path_l = urlparse(clean_url).path.lower()
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title or "").strip() or clean_name(os.path.basename(path_l.rstrip("/"))) or f"stream_{uid}"
    try:
        if path_l.endswith((".m3u8", ".mpd")) or "/manifest" in path_l:
            await safe_edit(status, f"📥 *Stream URL received (HLS/DASH)*\n`{redact_url(clean_url)}`", reply_markup=kb, force=True)
            await _do_dl_via_ffmpeg_hls(clean_url, status, cancel_flag, uid, chat_id, page_url=ref, title=safe_title, referer=ref)
            return
        hdrs = build_direct_headers(clean_url, ref or clean_url, _STANDARD_UA)
        await safe_edit(status, f"📥 *Direct media URL received*\n`{redact_url(clean_url)}`", reply_markup=kb, force=True)
        ok = await _do_dl_via_aria2c(clean_url, tmp_out, status, cancel_flag, headers=hdrs, uid=uid)
        if cancel_flag[0]:
            await safe_final(status, "⛔ Cancelled.")
            return
        if ok:
            await _finish_upload(tmp_out, safe_title, status, uid, chat_id)
            return
        await _do_dl_via_cffi(clean_url, status, cancel_flag, uid, chat_id, headers=hdrs, title=safe_title, page_url=ref)
    except asyncio.CancelledError:
        await safe_final(status, "⛔ Cancelled.")
    except Exception as e:
        if "__CANCELLED__" in str(e):
            await safe_final(status, "⛔ Cancelled.")
        else:
            logging.error(f"Stream job error: {type(e).__name__}")
            _diag(uid, "stream job", "download", _diag_detail(e))
            await safe_final(status, f"❌ Error: {type(e).__name__}" + _diag_summary(uid))
    finally:
        dl_tasks.pop(uid, None)
        dl_cancel_flags.pop(uid, None)
        if os.path.exists(tmp_out) and tmp_out not in _KEPT_SOURCES:
            try: os.remove(tmp_out)
            except Exception: pass

async def stream_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not c.args or not _valid_http_url(c.args[0]):
        return await u.message.reply_text(
            "Usage: `/stream [media or playlist url] [optional page url as referer]`\n"
            "For a direct .mp4 / .m3u8 / .mpd link you already captured. No cookies are accepted.",
            parse_mode="Markdown")
    uid = u.effective_chat.id
    if _kept_blocked():
        return await _kept_block_notice(u.message.reply_text)
    referer = c.args[1] if len(c.args) > 1 and _valid_http_url(c.args[1]) else ""
    if uid in dl_tasks and not dl_tasks[uid].done():
        dl_tasks[uid].cancel()
        await asyncio.sleep(0.3)
    status = await u.message.reply_text("🔍 *Direct stream input...*", parse_mode="Markdown")
    task = asyncio.create_task(_run_direct_stream(c.args[0], status, uid, u.effective_chat.id, referer=referer))
    dl_tasks[uid] = task
    await task

async def _do_dl(url: str, reply_msg, chat_id: int):
    uid = reply_msg.chat_id
    if _kept_blocked():
        return await _kept_block_notice(reply_msg.reply_text)
    _ACTIVE_CEILING[0] = _q_ceiling(uid)
    now = time.time()
    rate_hint = f"\n⚠️ _Rate-Limit active ({int(_flood_wait_until[0] - now)}s)_" if now < _flood_wait_until[0] else ""
    status = await reply_msg.reply_text(f"🔍 *Step 1/4: Analyzing source...*{rate_hint}", parse_mode="Markdown")

    tmp_out = os.path.join(ARIA2C_DOWNLOAD_DIR, f"dl_{uid}_{int(time.time())}.mp4")
    loop = asyncio.get_running_loop()
    t0 = time.time()
    last = [0.0]
    meta = {}

    cancel_flag = [False]
    dl_cancel_flags[uid] = cancel_flag
    _diag_reset(uid)
    CANCEL_KB = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"DL_CANCEL|{uid}")]])

    def hook(d):
        if cancel_flag[0]: raise Exception("__CANCELLED__")
        st = d.get("status")
        if st not in ("downloading", "processing"): return

        done = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0

        if not total and d.get("fragment_count"):
            done = d.get("fragment_index", 0)
            total = d.get("fragment_count", 1)

        meta.update(done=done, total=total)
        now_time = time.time()
        if now_time - last[0] < 2.0: return
        last[0] = now_time

        title_lbl = meta.get("title", url)[:40]
        if total > 0 and d.get("downloaded_bytes"):
            bar_str = progress_bar(done, total)
            speed_str = speed_eta(done, total, now_time - t0)
            text = f"📥 *Step 2/4: Downloading video...*\n*{title_lbl}*\n\n{bar_str}\n{speed_str}"
        elif d.get("fragment_count"):
            pct = int((done / total) * 100)
            bar = "▓" * int(16 * pct / 100) + "░" * (16 - int(16 * pct / 100))
            text = f"📥 *Step 2/4: Downloading stream...*\n*{title_lbl}*\n\n[{bar}] {pct}% ({done}/{total} chunks)"
        else:
            text = f"📥 *Step 2/4: Downloading video...*\n*{title_lbl}*\n\n{fmt_size(done)} downloaded"

        asyncio.run_coroutine_threadsafe(safe_edit(status, text, reply_markup=CANCEL_KB), loop)

    def _refresh_target():
        """Re-resolve the page once, for a token that expired mid-download. Generic: whatever
        _scrape_real_url returns, as long as it is a different direct media URL."""
        try:
            u2, _t2, _r2 = _scrape_real_url(url)
            if u2 and u2 != url and detect_direct_video_ext(u2):
                return u2
        except Exception:
            pass
        return ""

    try:
        await safe_edit(status, "🔍 *Step 1/4: Resolving stream link...*", reply_markup=CANCEL_KB, force=True)
        real_url, scraped_title, scraper_ref = await asyncio.to_thread(_scrape_real_url, url)

        snatch_type = None
        snatch_hdrs = None
        # The sniffer is the stand-in for the capture leg, never a rung ahead of it: it only runs
        # when that leg cannot run (tiers paused/disabled) and the host is not a yt-dlp platform.
        if (not real_url or real_url == url) and _sniffer_worth(url):     # ← re-gated
            snatch_res = await universal_snatch(url, status)
            if snatch_res and snatch_res.get("url"):
                _diag(uid, "resolver", "detection", "stream candidate found")
                real_url = snatch_res["url"]
                scraped_title = snatch_res.get("title") or scraped_title
                scraper_ref = snatch_res.get("headers", {}).get("Referer", url)
                snatch_type = snatch_res.get("type")
                snatch_hdrs = snatch_res.get("headers")

        real_url = real_url or url
        clean_target_url = real_url.replace("&amp;", "&").replace("\\", "").strip()
        ref_header = clean_referer(scraper_ref or url)

        def run_ydl():
            ydl_opts = {
                "format": _ydl_format(_ACTIVE_CEILING[0]),
                "outtmpl": tmp_out,
                "quiet": True,
                "noplaylist": True,
                "progress_hooks": [hook],
                "merge_output_format": "mp4",
                "nocheckcertificate": True,
                "geo_bypass": True,
                **_YDL_RELIABLE,
                "http_headers": {
                    "User-Agent": _STANDARD_UA,
                    "Accept-Language": _STANDARD_LANG,
                    "Referer": clean_referer(ref_header),
                },
                "extractor_args": {
                    "youtube": {
                        "player_client": ["default", "web_embedded"],
                    },
                },
            }
            if _YDL_IMPERSONATE:
                ydl_opts["impersonate"] = "chrome"
                ydl_opts["extractor_args"]["generic"] = {"impersonate": True}
            if COOKIES_FILE: ydl_opts["cookiefile"] = COOKIES_FILE
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                meta["title"] = scraped_title or info.get("title", "Video")
                meta["duration"] = info.get("duration", 0)
                meta["width"] = info.get("width", 0)
                meta["height"] = info.get("height", 0)
                return info

        async def _try_native_ytdlp() -> bool:
            await safe_edit(status, "📥 *Step 2/4: Starting video downloader...*", reply_markup=CANCEL_KB, force=True)
            try:
                await loop.run_in_executor(None, run_ydl)
            except Exception as e:
                if cancel_flag[0] or "__CANCELLED__" in str(e): raise
                _diag(uid, "yt-dlp native", "page extract", _diag_detail(e))
                _stat("yt-dlp native", False)
                if clean_target_url != url and detect_direct_video_ext(clean_target_url):
                    return await _do_dl_via_cffi(clean_target_url, status, cancel_flag, uid, chat_id, title=scraped_title, page_url=url)
                return False
            final_title = meta.get("title") or scraped_title or "Video"
            ok = await _finish_upload(tmp_out, final_title, status, uid, chat_id)
            _stat("yt-dlp native", ok)
            return ok

        async def _last_resorts(ytdlp_done: bool = False) -> bool:        # ← yt-dlp no longer twice
            """A resolved stream failed. yt-dlp on the page is only tried when the leg that just
            ran was not yt-dlp itself."""
            if not ytdlp_done and await _try_native_ytdlp():
                return True
            await safe_final(status, "❌ Could not download the detected stream (host error, expired link, or unsupported site)." + _diag_summary(uid))
            return False

        # ── sxyprn: the media URL does not exist in the markup. getvsrc() builds it
        #    client-side and the server answers with a 302 to a trafficdeposit node.
        #    Resolve the chain, then stream the Location with the post page as Referer.
        #    Tokens are minted per request, so the chain is always walked fresh.   ← whole branch rewritten
        resolved_ext = detect_direct_video_ext(clean_target_url)
        sxyprn_page = _sxyprn_page_url(url) if _is_sxyprn_url(url) else ""
        if not sxyprn_page and _is_sxyprn_url(ref_header or ""):
            sxyprn_page = _sxyprn_page_url(ref_header)
        if sxyprn_page or resolved_ext == ".vid":
            page_for_resolve = sxyprn_page or _sxyprn_page_url(ref_header or url)
            await safe_edit(status, "📥 *Step 2/4: Resolving sxyprn stream...*", reply_markup=CANCEL_KB, force=True)

            ok = False
            declared = 0
            for attempt in (1, 2):
                if cancel_flag[0]:
                    break
                if os.path.exists(tmp_out):
                    try: os.remove(tmp_out)
                    except Exception: pass
                final, declared, ttl, pg = await asyncio.to_thread(_sxyprn_resolve, page_for_resolve)
                if ttl and not scraped_title:
                    scraped_title = ttl
                if not final:
                    _diag(uid, "sxyprn resolve", "redirect", "no media url")
                    continue
                ok = await asyncio.to_thread(_sxyprn_fetch, final, pg, tmp_out, cancel_flag,
                                             loop, status, scraped_title, declared)
                if ok and declared and os.path.getsize(tmp_out) < declared * 0.5:
                    _diag(uid, "sxyprn fetch", "size",
                          f"{fmt_size(os.path.getsize(tmp_out))} of {fmt_size(declared)} - likely a teaser")
                    ok = False
                if ok:
                    break
                if attempt == 1 and not cancel_flag[0]:
                    await safe_edit(status, "🔁 *Fresh token, retrying...*", force=True)

            if ok and os.path.exists(tmp_out) and await _finish_upload(tmp_out, scraped_title or "Video", status, uid, chat_id):
                return
            if cancel_flag[0]:
                await safe_final(status, "⛔ Cancelled."); return
            # nothing playable: hand it to the generic engines with the post page as Referer
            if os.path.exists(tmp_out) and await _finish_upload(tmp_out, scraped_title or "Video", status, uid, chat_id):
                return
            _diag(uid, "sxyprn", "download", "resolver and direct fetch failed")
            if await _do_dl_via_cffi(clean_target_url, status, cancel_flag, uid, chat_id,
                                     headers={"Referer": page_for_resolve, "Accept-Encoding": "identity"},
                                     title=scraped_title, page_url=page_for_resolve):
                return
            if cancel_flag[0]:
                await safe_final(status, "⛔ Cancelled."); return
            # Still nothing: the post page itself goes to the capture leg instead of ending here.  ← added
            _diag(uid, "sxyprn", "escalate", "handing the post page to the capture leg")
            if _snatch_avail():
                await safe_edit(status, "🌐 *Step 2/4: Capturing the stream inside the browser session...*",
                                reply_markup=CANCEL_KB, force=True)
                path, name = await snatch_save(page_for_resolve, uid, scraped_title, cancel_flag)
                if path and await _finish_upload(path, name or scraped_title or "Video", status, uid, chat_id):
                    return
                if cancel_flag[0]:
                    await safe_final(status, "⛔ Cancelled."); return
            await safe_final(status, "❌ Could not fetch the sxyprn stream." + _diag_summary(uid))
            return

        # ── universal browser leg: the session that mints the token also pulls the bytes,
        #    and the name comes from Content-Disposition / og:title, never from the URL.
        #    The PAGE url is opened (the player mints the token there), not the CDN url.
        #    _snatch_worth is false for a plain resolved file, so the cheap rungs below run
        #    first and this only fires where a cold request cannot win.   ← re-gated
        if _snatch_worth(url, clean_target_url, snatch_type):
            await safe_edit(status, "🌐 *Step 2/4: Capturing the stream inside the browser session...*",
                            reply_markup=CANCEL_KB, force=True)
            path, name = await snatch_save(url, uid, scraped_title, cancel_flag)
            if path:
                if await _finish_upload(path, name or scraped_title or "Video", status, uid, chat_id):
                    return
                if path in _KEPT_SOURCES:
                    return      # file is on disk and registered: re-downloading would be waste
            if cancel_flag[0]:
                await safe_final(status, "⛔ Cancelled."); return

        # 1. Direct HLS / DASH Stream routing
        # snatch_type covers body-sniffed manifests at opaque URLs (no .m3u8/.mpd in URL)
        if (".m3u8" in clean_target_url or clean_target_url.split("?")[0].endswith(".m3u8")
            or ".mpd" in clean_target_url or "/manifest" in clean_target_url
            or snatch_type in ("hls", "dash")):
            await safe_edit(status, "📥 *Step 2/4: Stream found (HLS/DASH)!*\nStarting downloader...", reply_markup=CANCEL_KB, force=True)
            ok = await _dl_hls_with_refresh(clean_target_url, status, cancel_flag, uid, chat_id,
                                            page_url=url, title=scraped_title, referer=ref_header,
                                            extra_headers=snatch_hdrs, refresh_cb=_refresh_target)
            if ok: return
            await _last_resorts(ytdlp_done=True)     # the HLS chain already ran yt-dlp   ← changed
            return

        # 2. Check if a real CDN / video link was resolved
        is_dood = ("doodcdn" in clean_target_url) or ("/video~" in clean_target_url) or ("pass_md5" in clean_target_url)
        path_ext = detect_direct_video_ext(clean_target_url)
        is_resolved_stream = (clean_target_url != url) and not any(clean_target_url.split("?")[0].endswith(ext) for ext in (".html", ".htm", ".php"))

        if is_dood or path_ext or is_resolved_stream:
            direct_hdrs = build_direct_headers(clean_target_url, ref_header, _STANDARD_UA)
            await safe_edit(status, "📥 *Step 2/4: Direct video stream detected (downloading)...*", reply_markup=CANCEL_KB, force=True)
            if is_dood:
                if await _do_dl_via_cffi(clean_target_url, status, cancel_flag, uid, chat_id, headers=direct_hdrs, title=scraped_title, page_url=ref_header):
                    return
                await _last_resorts()
                return
            ok = await _do_dl_via_aria2c(clean_target_url, tmp_out, status, cancel_flag, headers=direct_hdrs, uid=uid)
            if ok and await _finish_upload(tmp_out, scraped_title or clean_name(os.path.basename(clean_target_url.rstrip("/"))), status, uid, chat_id):
                return
            if await _do_dl_via_cffi(clean_target_url, status, cancel_flag, uid, chat_id, headers=direct_hdrs, title=scraped_title, page_url=ref_header):
                return
            await _last_resorts()
            return

        if clean_target_url == url:
            _diag(uid, "resolver", "detection", "no stream found" + (f" - {_LAST_PAGE_SCAN[0]}" if _LAST_PAGE_SCAN[0] else ""))
            if await _try_native_ytdlp():
                return
            await safe_final(status, "❌ Could not extract video stream from this URL (unsupported or protected site)." + _diag_summary(uid))
            return

        # Native yt-dlp core for standard platforms
        if await _try_native_ytdlp():
            return
        await safe_final(status, "❌ Could not download video from this URL." + _diag_summary(uid))
        return

    except asyncio.CancelledError:
        await safe_final(status, "⛔ Cancelled.")
    except Exception as e:
        if "__CANCELLED__" in str(e):
            await safe_final(status, "⛔ Cancelled.")
        else:
            logging.error(f"DL error: {e}", exc_info=True)
            _w = _exc_where(e)
            if _w: _diag(uid, "internal", type(e).__name__, f"raised in {_w}")
            await safe_final(status, f"❌ Error: {type(e).__name__} ({_diag_detail(e)})" + _diag_summary(uid))
    finally:
        dl_tasks.pop(uid, None)
        dl_cancel_flags.pop(uid, None)
        if os.path.exists(tmp_out) and tmp_out not in _KEPT_SOURCES:
            try: os.remove(tmp_out)
            except Exception: pass

# ══════════════════════════════════════════════════════
#  FAST MULTI-WORKER COVER CHANGER PIPELINE
# ══════════════════════════════════════════════════════
async def _update_video_cover(msg: Update, custom_thumb_path: str):
    uid = msg.chat_id
    status = await msg.reply_text("⚡ Processing video cover...")
    raw_video = os.path.join(ARIA2C_DOWNLOAD_DIR, f"cover_swap_{uid}_{int(time.time())}.mp4")
    staged_msg_id = None

    try:
        raw_name = (msg.caption or (msg.video.file_name if msg.video else None) or (msg.document.file_name if msg.document else None) or "Video")
        title = clean_name(raw_name) or raw_name

        await safe_edit(status, "🔄 Refreshing media security token...", force=True)
        staged = await safe_copy(msg.get_bot(), STORAGE_CHANNEL_ID, msg.chat_id, msg.message_id, disable_notification=True)

        if staged:
            staged_msg_id = staged.message_id
            tg_msg = await tgclient.get_messages(STORAGE_CHANNEL_ID, ids=staged_msg_id)
        else:
            tg_msg, _ = await resolve_tg_message(msg.chat_id, msg.message_id)

        if not tg_msg or not tg_msg.media:
            await status.edit_text("❌ Could not read video file.")
            return

        doc = getattr(tg_msg.media, "document", None)
        if not doc:
            await status.edit_text("❌ Not a valid video document.")
            return

        t0 = time.time()
        last_edit = [0.0]

        async def dl_prog(current, total):
            now = time.time()
            if now - last_edit[0] < 2.0: return
            last_edit[0] = now
            text = (
                f"📥 *Downloading video...*\n*{title[:40]}*\n\n"
                f"{progress_bar(current, total)}\n"
                f"{speed_eta(current, total, now - t0)}"
            )
            await safe_edit(status, text)

        downloaded = await tgclient.download_media(tg_msg, file=raw_video, progress_callback=dl_prog)
        if not downloaded or not os.path.exists(raw_video):
            await status.edit_text("❌ Video download failed.")
            return

        duration, width, height = 0, 1280, 720
        for a in getattr(doc, "attributes", []):
            if isinstance(a, DocumentAttributeVideo):
                duration, width, height = a.duration, a.w, a.h
                break
        if duration == 0: duration, width, height = get_video_info(raw_video)

        attrs = [DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)]
        valid_thumb = prepare_telegram_thumb(custom_thumb_path)
        t1 = time.time()
        last_edit[0] = 0.0

        async def ul_prog(current, total):
            now = time.time()
            if now - last_edit[0] < 2.0: return
            last_edit[0] = now
            text = (
                f"☁️ *Uploading with new cover...*\n*{title[:40]}*\n\n"
                f"{progress_bar(current, total)}\n"
                f"{speed_eta(current, total, now - t1)}"
            )
            await safe_edit(status, text)

        input_file = await fast_upload(tgclient, raw_video, progress_callback=ul_prog)

        sent = await tgclient.send_file(
            STORAGE_CHANNEL_ID,
            file=input_file,
            caption=title,
            supports_streaming=True,
            thumb=valid_thumb,
            attributes=attrs,
        )

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO files (msg_id, name) VALUES (?,?)", (sent.id, title))
            conn.commit()

        if staged_msg_id:
            try: await tgclient.delete_messages(STORAGE_CHANNEL_ID, staged_msg_id)
            except Exception: pass

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Flush Thumbnail", callback_data="FLUSH_THUMB")]])
        try:
            await msg.get_bot().copy_message(
                chat_id=msg.chat_id,
                from_chat_id=STORAGE_CHANNEL_ID,
                message_id=sent.id,
                reply_markup=kb,
            )
        except Exception:
            await tgclient.forward_messages(msg.chat_id, sent.id, from_peer=STORAGE_CHANNEL_ID)

        await status.edit_text("✅ Cover updated!")
    except Exception as e:
        logging.error(f"Cover update error: {e}")
        await status.edit_text(f"❌ Failed to update cover: {e}")
    finally:
        if os.path.exists(raw_video):
            try: os.remove(raw_video)
            except Exception: pass


# ══════════════════════════════════════════════════════
#  HIGH-SPEED BATCH AUTO-NAMING PIPELINE
# ══════════════════════════════════════════════════════
async def extract_episode_name(msg, uid: int) -> str:
    found_name = None

    if msg.video and getattr(msg.video, "file_name", None):
        found_name = msg.video.file_name
    elif msg.document and getattr(msg.document, "file_name", None):
        found_name = msg.document.file_name

    if not found_name and msg.caption:
        found_name = msg.caption.strip().split("\n")[0]

    if not found_name:
        size = (msg.video.file_size if msg.video else (msg.document.file_size if msg.document else 0)) or 0
        duration = (msg.video.duration if msg.video else 0) or 0

        if size > 0:
            try:
                conn = get_db(); cur = conn.cursor()
                if duration > 0:
                    cur.execute("SELECT name FROM file_footprints WHERE file_size=? AND duration=? LIMIT 1", (size, duration))
                else:
                    cur.execute("SELECT name FROM file_footprints WHERE file_size=? LIMIT 1", (size,))
                row = cur.fetchone(); conn.close()
                if row and row[0]:
                    found_name = row[0]
            except Exception: pass

    if not found_name or found_name.lower() in ("video", "file", "unknown"):
        prefix = user_series_prefix.get(uid, "Episode")
        curr_ep = episode_counters.get(uid, 1)
        found_name = f"{prefix} E{curr_ep:02d}.mp4"
        episode_counters[uid] = curr_ep + 1

    return found_name

async def batch_resend_worker(uid: int, bot):
    q = batch_queues[uid]
    try:
        while not q.empty():
            msg = await q.get()
            try:
                resolved_name = await extract_episode_name(msg, uid)
                size = (msg.video.file_size if msg.video else (msg.document.file_size if msg.document else 0)) or 0
                dur = (msg.video.duration if msg.video else 0) or 0

                stored = None
                try:
                    stored = await safe_copy(
                        bot, STORAGE_CHANNEL_ID,
                        msg.chat_id, msg.message_id,
                        caption=f"📁 `{resolved_name}`",
                        parse_mode="Markdown",
                        disable_notification=True
                    )
                except Exception: pass

                stored_msg_id = None
                if stored is not None:
                    stored_msg_id = stored.message_id
                else:
                    tg_msg, dl_client = await resolve_tg_message(msg.chat_id, msg.message_id)
                    if tg_msg and tg_msg.media:
                        sent = await dl_client.send_file(
                            STORAGE_CHANNEL_ID,
                            tg_msg,
                            caption=f"📁 `{resolved_name}`"
                        )
                        stored_msg_id = sent.id

                if stored_msg_id:
                    with get_db() as conn:
                        cur = conn.cursor()
                        cur.execute("INSERT INTO files (msg_id, name) VALUES (?,?)", (stored_msg_id, resolved_name))
                        if size > 0:
                            cur.execute("INSERT INTO file_footprints (file_size, duration, name) VALUES (?,?,?)", (size, dur, resolved_name))
                        conn.commit()

                    sent_user = None
                    try:
                        sent_user = await safe_copy(
                            bot, uid,
                            STORAGE_CHANNEL_ID, stored_msg_id,
                            caption=f"📁 `{resolved_name}`",
                            parse_mode="Markdown"
                        )
                    except Exception: pass

                    if sent_user is None:
                        try:
                            await tgclient.forward_messages(uid, stored_msg_id, from_peer=STORAGE_CHANNEL_ID)
                        except Exception: pass

            except Exception as e:
                logging.error(f"batch_resend_worker error: {e}", exc_info=True)
            finally:
                q.task_done()
                await asyncio.sleep(0.05)
    finally:
        batch_queues.pop(uid, None)


# ══════════════════════════════════════════════════════
#  FORWARD FILE SAVE WORKER
# ══════════════════════════════════════════════════════
async def _save_worker(uid: int):
    q = save_queues[uid]
    try:
        while True:
            job = await asyncio.wait_for(q.get(), timeout=60)
            try: await _do_save(**job)
            except Exception as e: logging.error("save_worker: " + str(e))
            finally: q.task_done()
            await asyncio.sleep(0.5)
    except asyncio.TimeoutError: pass
    finally:
        save_queues.pop(uid, None)
        save_workers.pop(uid, None)

async def _do_save(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str):
    msg = update.message
    try:
        stored = None
        try:
            stored = await safe_copy(context.bot, STORAGE_CHANNEL_ID, msg.chat_id, msg.message_id, disable_notification=True)
        except Exception: pass

        if stored is None:
            tg_msg, dl_client = await resolve_tg_message(msg.chat_id, msg.message_id)
            if tg_msg is None or tg_msg.media is None:
                await msg.reply_text("Could not access this file.")
                return
            sent = await dl_client.send_file(STORAGE_CHANNEL_ID, tg_msg, caption=name)
            stored_id = sent.id
        else:
            stored_id = stored.message_id

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO files (msg_id, name) VALUES (?,?)", (stored_id, name))
            conn.commit()
            new_id = cur.lastrowid

        await safe_copy(context.bot, msg.chat_id, STORAGE_CHANNEL_ID, stored_id, disable_notification=True)
        if not SILENT_MODE[0]:
            await msg.reply_text(f"Saved {name} (ID {new_id})")
    except Exception as e:
        logging.error(f"_do_save error: {e}")
        await msg.reply_text("Save failed: " + str(e))


# ══════════════════════════════════════════════════════
#  MP3 EXTRACTION
# ══════════════════════════════════════════════════════
async def mp3_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if c.args:
        await _do_mp3_url(c.args[0], u.message, u.effective_chat.id)
    else:
        pending_mp3[u.effective_user.id] = True
        await u.message.reply_text("Send or forward the video you want the MP3 extracted from:")

async def _do_mp3_url(url: str, reply_msg, chat_id: int):
    uid    = reply_msg.chat_id
    status = await reply_msg.reply_text("🔍 *Resolving audio stream...*")
    tmp    = os.path.join(ARIA2C_DOWNLOAD_DIR, f"mp3_{uid}.mp3")
    loop   = asyncio.get_running_loop()
    t0     = time.time()
    last   = [0.0]
    meta   = {}

    def hook(d):
        if d.get("status") != "downloading": return
        done  = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        meta.update(done=done, total=total)
        now = time.time()
        if now - last[0] < 2.0: return
        last[0] = now
        asyncio.run_coroutine_threadsafe(
            safe_edit(status, "🎵 *Extracting MP3:*\n" + meta.get("title", url)[:40] + "\n\n" + progress_bar(done, total) + speed_eta(done, total, now - t0)),
            loop)

    def run_ydl():
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(ARIA2C_DOWNLOAD_DIR, f"mp3_{uid}.%(ext)s"),
            "quiet": True,
            "noplaylist": True,
            "progress_hooks": [hook],
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "nocheckcertificate": True,
            "geo_bypass": True,
        }
        if _YDL_IMPERSONATE: ydl_opts["impersonate"] = "chrome"
        if COOKIES_FILE: ydl_opts["cookiefile"] = COOKIES_FILE
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            meta["title"] = info.get("title", "Audio")
            return info

    try:
        await safe_edit(status, "🎵 *Starting audio extraction...*", force=True)
        await loop.run_in_executor(None, run_ydl)
        if not os.path.exists(tmp): return await safe_edit(status, "❌ MP3 extraction failed.", force=True)
        title    = meta.get("title", "Audio")
        filesize = os.path.getsize(tmp)
        await safe_edit(status, f"☁️ *Uploading {title[:40]} ({fmt_size(filesize)})...*", force=True)

        input_file = await fast_upload(tgclient, tmp)
        sent = await tgclient.send_file(STORAGE_CHANNEL_ID, file=input_file, caption=title, attributes=[])

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO files (msg_id, name) VALUES (?,?)", (sent.id, title))
            conn.commit()

        await status.get_bot().copy_message(chat_id=chat_id, from_chat_id=STORAGE_CHANNEL_ID, message_id=sent.id)
        await safe_edit(status, f"✅ *{title}*", force=True)
    except Exception as e:
        await safe_edit(status, "❌ " + str(e), force=True)
    finally:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except Exception: pass

async def _do_mp3_from_file(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg    = u.message
    uid    = u.effective_user.id
    status = await msg.reply_text("📥 *Downloading file for MP3 conversion...*")
    src_path = None
    out_path = os.path.join(ARIA2C_DOWNLOAD_DIR, f"mp3_{uid}.mp3")
    try:
        tg_msg, dl_client = await resolve_tg_message(msg.chat_id, msg.message_id)
        if tg_msg is None or tg_msg.media is None:
            await status.edit_text("Could not access this file.")
            return

        raw  = (msg.caption or (msg.video.file_name if msg.video else None) or (msg.document.file_name if msg.document else None) or "Audio")
        title = clean_name(raw) or raw or "Audio"
        title = re.sub(r"\.(mp4|mkv|avi|mov|webm)$", "", title, flags=re.IGNORECASE).strip()

        loop = asyncio.get_running_loop()
        src_path = await dl_client.download_media(tg_msg, file=f"mp3_{uid}_src")
        if not src_path: return await status.edit_text("Download failed.")

        await safe_edit(status, "⚙️ *Converting to MP3 via FFmpeg...*", force=True)
        cmd = [FFMPEG_PATH, "-y", "-i", src_path, "-vn", "-acodec", "mp3", "-ab", "192k", out_path]
        await loop.run_in_executor(None, lambda: subprocess.run(cmd, capture_output=True, timeout=600))
        if not os.path.exists(out_path): return await status.edit_text("MP3 conversion failed.")

        filesize = os.path.getsize(out_path)
        await safe_edit(status, f"☁️ *Uploading {title[:40]} ({fmt_size(filesize)})...*", force=True)

        input_file = await fast_upload(tgclient, out_path)
        sent = await tgclient.send_file(STORAGE_CHANNEL_ID, file=input_file, caption=title, attributes=[])

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO files (msg_id, name) VALUES (?,?)", (sent.id, title))
            conn.commit()

        await c.bot.copy_message(chat_id=u.effective_chat.id, from_chat_id=STORAGE_CHANNEL_ID, message_id=sent.id)
        await safe_edit(status, f"✅ *{title}*", force=True)
    except Exception as e:
        await status.edit_text("❌ " + str(e))
    finally:
        for f in [src_path, out_path]:
            if f and os.path.exists(f):
                try: os.remove(f)
                except Exception: pass


# ══════════════════════════════════════════════════════
#  BOT COMMANDS & HANDLERS
# ══════════════════════════════════════════════════════
async def series_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not c.args:
        curr = user_series_prefix.get(uid, "Episode")
        await u.message.reply_text(f"Current series prefix: `{curr}`\nUsage: `/series [Show Name]`\nExample: `/series Naruto Shippuden S10`", parse_mode="Markdown")
        return
    prefix = " ".join(c.args)
    user_series_prefix[uid] = prefix
    episode_counters[uid] = 1
    await u.message.reply_text(f"✅ Series set to: *{prefix}*\nNext unnamed files will start at: *E01*", parse_mode="Markdown")

async def resetep_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    num = int(c.args[0]) if (c.args and c.args[0].isdigit()) else 1
    episode_counters[uid] = num
    await u.message.reply_text(f"✅ Next episode counter set to: *E{num:02d}*", parse_mode="Markdown")

async def limits_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    bot_wait = max(0, int(_flood_wait_until[0] - now))
    user_wait = max(0, int(_user_flood_until[0] - now))

    lines = ["📊 *Telegram & Bot Status Report*\n"]
    lines.append(f"🤖 *Bot Message-Edits:* ⚠️ Paused for *{bot_wait}s* ({bot_wait // 60}m {bot_wait % 60}s)" if bot_wait > 0 else "🤖 *Bot Message-Edits:* ✅ *Ready* (No restrictions)")
    lines.append(f"👤 *Telethon User Client:* ⚠️ FloodWait for *{user_wait}s*" if user_wait > 0 else "👤 *Telethon User Client:* ✅ *Ready* (Uploads up to 2 GB active)")
    lines.append(f"\n📥 *Active Downloads:* {len(dl_tasks)}")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def stats_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Per-leg hit rate. The ranked list is the thing to look at before trimming any rung."""
    if not _LEG_STATS:
        return await u.message.reply_text("No leg statistics yet.")
    rows = sorted(_LEG_STATS.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))
    lines = ["*Leg hit rate* (ok / attempts)\n"]
    for leg, (ok, fail) in rows:
        n = ok + fail
        lines.append(f"`{leg}`: {ok}/{n} ({int(ok * 100 / n)}%)")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def diag_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """The last job's diagnostic trail, on demand (not only on failure)."""
    txt = _diag_summary(u.effective_chat.id).strip() or "No diagnostics recorded for the last job."
    await u.message.reply_text(txt)

async def quality_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Output ceiling for every download. 'best' (default) keeps whatever the source offers."""
    uid = u.effective_user.id
    arg = (c.args[0].lower() if c.args else "")

    def _show():
        cur = _q_ceiling(uid)
        return f"*Quality:* {'best available (no ceiling)' if not cur else f'up to {cur}p'}\n" \
               f"`/quality best` · `/quality 2160` · `/quality 1080` · `/quality 720`"

    if not arg:
        return await u.message.reply_text(_show(), parse_mode="Markdown")

    if arg in ("best", "max", "off", "0"):
        user_quality.pop(uid, None)
    elif arg.isdigit():
        user_quality[uid] = max(144, min(4320, int(arg)))
    else:
        return await u.message.reply_text(_show(), parse_mode="Markdown")
    await u.message.reply_text(_show(), parse_mode="Markdown")

async def send_vault_page(target, page: int, edit: bool = False):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM files")
        total = cur.fetchone()[0]
        cur.execute("SELECT id, name FROM files ORDER BY id DESC LIMIT ? OFFSET ?", (FILES_PER_PAGE, page * FILES_PER_PAGE))
        files = cur.fetchall()

    if not files:
        txt = "Vault is empty."
        if edit: await target.edit_message_text(txt)
        else: await target.reply_text(txt)
        return

    pages = max(1, (total - 1) // FILES_PER_PAGE + 1)
    text = f"Vault — {total} file(s) (Page {page+1}/{pages})"
    rows = [[InlineKeyboardButton(f"  {r[1][:40]}", callback_data=f"FILE|{r[0]}")] for r in files]
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("◀️", callback_data=f"PAGE|{page-1}"))
    if (page+1)*FILES_PER_PAGE < total: nav.append(InlineKeyboardButton("▶️", callback_data=f"PAGE|{page+1}"))
    if nav: rows.append(nav)
    kb = InlineKeyboardMarkup(rows)

    if edit: await target.edit_message_text(text, reply_markup=kb)
    else: await target.reply_text(text, reply_markup=kb)

def file_kb(tid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Get", callback_data=f"GET|{tid}"),
         InlineKeyboardButton("✏️ Rename", callback_data=f"REN|{tid}"),
         InlineKeyboardButton("🗑 Delete", callback_data=f"DEL|{tid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="BACK")],
    ])

async def start_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "*Titan Vault & Batch Auto-Renamer*\n\n"
        "Forward up to 200+ episodes at once to instantly receive them with real filenames!\n\n"
        "`/series [name]` — Set series name for unnamed files\n"
        "`/ep [num]` — Set starting episode number\n"
        "`/quality` — Best-quality ceiling (default: best available)\n"
        "`/files` — browse vault\n"
        "`/dl [url]` — download link up to 2 GB\n"
        "`/thumb` — set custom thumbnail\n"
        "`/viewthumb` — preview active thumbnail\n"
        "`/flush` — clear thumbnail\n"
        "`/limits` — check rate limit status\n"
        "`/search [q]` — find files\n"
        "`/help` — show all commands",
        parse_mode="Markdown"
    )

async def help_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    owner = u.effective_user.id == OWNER_ID
    lines = [
        "*Titan Vault Commands*\n",
        "`/series [name]` — Set show name for unnamed batches",
        "`/ep [num]` — Set starting episode number",
        "`/quality [best|2160|1080|720]` — Output quality ceiling (default best)",
        "`/files` — browse vault",
        "`/dl [url]` — download & store (up to 2 GB)",
        "`/stream [media url] [page url]` — direct .mp4/.m3u8/.mpd link, no page extraction",
        "`/kept` — files kept after a failed upload (Retry / Delete)",
        "`/held` — links held after rate limiting (Continue / Clear)",
        "`/limits` — check Telegram rate limit & timers",
        "`/stats` — per-leg hit rate",
        "`/diag` — diagnostics of the last job",
        "`/mp3 [url]` — extract & store as MP3 from URL",
        "`/mp3` — send/forward a video to extract MP3 from",
        "`/thumb` — set custom thumbnail for videos",
        "`/viewthumb` — preview active custom thumbnail",
        "`/flush` or `/delthumb` — clear custom thumbnail",
        "`/search [q]` — search files",
        "`/rename [name]` — rename last file",
        "`/convert [spec]` — output format/size default (720p, mkv, mp3, 500mb, crf20 h265, off)",
        "`/convert 720p` — reply to a video to convert that file",
        "`/status` — vault stats",
        "`/quiet` — toggle silent saves",
    ]
    if owner: lines += ["\n*Owner*", "`/session` — get Telethon session string", "`/pair` — pair the browser extension", "`/unpair` — revoke paired browsers"]
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def convert_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    toks = c.args or []
    reply = u.message.reply_to_message

    if reply and (reply.video or (reply.document and
                                  getattr(reply.document, "mime_type", "").startswith("video/"))):
        if not toks:
            return await u.message.reply_text(
                f"Reply spec needed. Default: `{describe_convert(user_convert.get(uid, {}))}`",
                parse_mode="Markdown")
        return await _convert_reply(reply, parse_convert_spec(toks), u)

    if not toks:
        return await u.message.reply_text(
            f"*Converter default:* `{describe_convert(user_convert.get(uid, {}))}`\n\n"
            "`/convert 720p` · `/convert mkv` · `/convert mp3` · `/convert 500mb` · `/convert crf20 h265` · `/convert off`\n"
                        "Reply to a video with `/convert 720p` to convert that file.",
            parse_mode="Markdown")

    spec = parse_convert_spec(toks)
    if spec:
        user_convert[uid] = spec
    else:
        user_convert.pop(uid, None)
    await u.message.reply_text(f"✅ Converter set to: `{describe_convert(user_convert.get(uid, {}))}`",
                               parse_mode="Markdown")

async def _convert_reply(reply_msg, spec: dict, u: Update):
    uid = u.effective_user.id
    if not spec:
        return await reply_msg.reply_text("Give me a spec, e.g. `/convert 720p`", parse_mode="Markdown")
    status = await reply_msg.reply_text(f"⚙️ *Converting ({describe_convert(spec)})...*", parse_mode="Markdown")
    src = None
    try:
        tg_msg, cl = await resolve_tg_message(reply_msg.chat_id, reply_msg.message_id)
        if tg_msg is None or tg_msg.media is None:
            return await status.edit_text("Could not access that file.")
        raw = (reply_msg.caption
               or (reply_msg.video.file_name if reply_msg.video else None)
               or (reply_msg.document.file_name if reply_msg.document else None) or "video")
        title = clean_name(raw) or raw
        src = await cl.download_media(tg_msg, file=os.path.join(ARIA2C_DOWNLOAD_DIR, f"conv_{uid}"))
        if not src:
            return await status.edit_text("Download failed.")
        out = await apply_convert(src, spec, status)
        await _finish_upload(out, title, status, reply_msg.chat_id, reply_msg.chat_id, skip_convert=True)
    except Exception as e:
        await status.edit_text(f"❌ Convert failed: {str(e)[:150]}")
    finally:
        if src and os.path.exists(src):
            try: os.remove(src)
            except Exception: pass

# ── DRM COMMANDS ──────────────────────────────────────

async def status_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), MAX(added_at) FROM files")
        count, latest = cur.fetchone()
    thumb_status = "✅ Custom Set" if u.effective_user.id in user_thumbnails else "Auto-generated"
    q = _q_ceiling(u.effective_user.id)
    await u.message.reply_text(f"*Vault Status*\n\nFiles: *{count}*\nLast added: {(latest or 'never')[:16]}\nUpload limit: 2 GB\nQuality: *{'best' if not q else str(q) + 'p'}*\nThumbnail: *{thumb_status}*\nConverter: `{describe_convert(user_convert.get(u.effective_user.id, {}))}`", parse_mode="Markdown")

async def files_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await send_vault_page(u.message, 0)

async def search_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not c.args: return await u.message.reply_text("Usage: `/search query`", parse_mode="Markdown")
    q = " ".join(c.args)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM files WHERE LOWER(name) LIKE ? ORDER BY id DESC LIMIT 25", (f"%{q.lower()}%",))
        rows = cur.fetchall()
    if not rows: return await u.message.reply_text(f'No results for "{q}".')
    kb = [[InlineKeyboardButton(f"  {r[1][:40]}", callback_data=f"FILE|{r[0]}")] for r in rows]
    await u.message.reply_text(f'*{len(rows)} result(s)* for "{q}"', reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def quiet_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    SILENT_MODE[0] = not SILENT_MODE[0]
    await u.message.reply_text("Quiet mode: " + ("ON" if SILENT_MODE[0] else "OFF"))

async def done_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    pending_rename.pop(u.effective_user.id, None)
    pending_thumb.pop(u.effective_user.id, None)
    await u.message.reply_text("Cleared pending actions.")

async def rename_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM files ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    if not row: return await u.message.reply_text("No files saved yet.")
    pending_rename[uid] = {"db_id": row[0]}
    await u.message.reply_text("Send the new name for: " + row[1])

async def session_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    string = tgclient.session.save()
    await u.message.reply_text(f"*Your session string*:\n\n`{string}`", parse_mode="Markdown")

async def _do_rename(bot, reply_target, db_id: int, new_name: str, echo_chat=None):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT msg_id FROM files WHERE id=?", (db_id,))
        row = cur.fetchone()
    if not row: return await reply_target.reply_text("File not found.")
    try:
        old_msg = await tgclient.get_messages(STORAGE_CHANNEL_ID, ids=row[0])
        new_msg = await tgclient.send_message(STORAGE_CHANNEL_ID, new_name, file=old_msg.media)
        await tgclient.delete_messages(STORAGE_CHANNEL_ID, row[0])
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE files SET msg_id=?, name=? WHERE id=?", (new_msg.id, new_name, db_id))
            conn.commit()
        await reply_target.reply_text("✅ " + new_name)
        if echo_chat:
            await bot.copy_message(chat_id=echo_chat, from_chat_id=STORAGE_CHANNEL_ID, message_id=new_msg.id)
    except Exception as e:
        await reply_target.reply_text("Rename failed: " + str(e))

async def dl_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not c.args: return await u.message.reply_text("Usage: `/dl [url]`", parse_mode="Markdown")
    uid = u.effective_chat.id
    if uid in dl_tasks and not dl_tasks[uid].done():
        dl_tasks[uid].cancel()
        await asyncio.sleep(0.3)
    task = asyncio.create_task(_do_dl(c.args[0], u.message, u.effective_chat.id))
    dl_tasks[uid] = task
    await task

async def thumb_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    reply = u.message.reply_to_message

    if reply and (reply.photo or (reply.document and getattr(reply.document, "mime_type", "").startswith("image/"))):
        raw_thumb = f"thumb_{uid}.jpg"
        f = await (reply.photo[-1] if reply.photo else reply.document).get_file()
        await f.download_to_drive(raw_thumb)
        scaled = prepare_telegram_thumb(raw_thumb)
        user_thumbnails[uid] = scaled
        pending_thumb.pop(uid, None)

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("👁 View Thumbnail", callback_data="VIEW_THUMB"), InlineKeyboardButton("🗑 Flush Thumbnail", callback_data="FLUSH_THUMB")]])
        await u.message.reply_text("✅ Custom thumbnail saved from reply!", reply_markup=kb)
        return

    pending_thumb[uid] = True
    await u.message.reply_text("🖼 Send me the photo you want to use as your thumbnail.")

async def viewthumb_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if uid in user_thumbnails and os.path.exists(user_thumbnails[uid]):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Flush Thumbnail", callback_data="FLUSH_THUMB")]])
        with open(user_thumbnails[uid], "rb") as fh:
            await u.message.reply_photo(photo=fh, caption="Active thumbnail:", reply_markup=kb)
    else:
        await u.message.reply_text("No custom thumbnail is active.")

async def flush_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    pending_thumb.pop(uid, None)
    old = user_thumbnails.pop(uid, None)
    if old and os.path.exists(old):
        try: os.remove(old)
        except Exception: pass
    await u.message.reply_text("🗑 Thumbnail flushed!")


def _link_host(url: str) -> str:
    try: return urlparse(url).netloc.lower()
    except Exception: return ""

def _fmt_wait(sec: float) -> str:
    sec = int(max(0, sec)); h, r = divmod(sec, 3600); m, s2 = divmod(r, 60)
    return (f"{h}h " if h else "") + (f"{m}m " if m or h else "") + f"{s2}s"

async def _wait_flood_clear(uid: int, reply_msg):
    """Do not start the next download while Telegram's upload flood wait is active (the finished file would only
    be refused again). Waits the full time; Cancel still works because the queue is cleared and the loop ends."""
    left = _user_flood_until[0] - time.time()
    if left <= 0: return
    q = link_queues.get(uid)
    try:
        await reply_msg.reply_text(f"⏸ Telegram upload limit active. Next download starts in about {_fmt_wait(left)} "
                                   f"({q.qsize() if q else 0} waiting). Nothing is lost.")
    except Exception: pass
    while _user_flood_until[0] - time.time() > 0:
        await asyncio.sleep(min(30, max(1, _user_flood_until[0] - time.time())))
        q = link_queues.get(uid)
        if q is None or q.empty(): return

async def _next_link(uid: int, reply_msg):
    """Pick the next waiting link whose host is not cooling down after an HTTP 429. Returns None when the queue is
    empty. If every waiting link is cooling down, waits (the links stay in the queue, so Cancel still clears them)."""
    q = link_queues[uid]; noted = False
    while True:
        items = []
        while True:
            try: items.append(q.get_nowait())
            except asyncio.QueueEmpty: break
        if not items: return None
        now = time.time()
        pick = next((x for x in items if _host_cooldown.get(_link_host(x), 0) <= now), None)
        if pick is not None:
            items.remove(pick)
            for x in items: q.put_nowait(x)
            return pick
        for x in items: q.put_nowait(x)
        soonest = min(_host_cooldown.get(_link_host(x), 0) for x in items) - now
        if not noted:
            noted = True
            try: await reply_msg.reply_text(f"⏸ The sites asked to slow down. Waiting links resume in about {_fmt_wait(soonest)}. Nothing is lost.")
            except Exception: pass
        await asyncio.sleep(min(30, max(1, soonest)))

async def _say(msg, text: str, **kw):
    try: await msg.reply_text(text, **kw)
    except Exception: pass

async def _link_worker(uid: int, reply_msg):
    """Sequentially downloads every URL in link_queues[uid]. One download at a
    time per chat; a Cancel button press clears the queue in button_handler.
    Rate limits: Telegram's upload flood wait pauses the whole queue; a source's HTTP 429 puts that source's
    links on cooldown (the server's Retry-After is waited out in full) and the link is retried, bounded."""
    q = link_queues[uid]
    try:
        while True:
            await _wait_flood_clear(uid, reply_msg)
            url = await _next_link(uid, reply_msg)
            if url is None: break
            before = _upload_count.get(uid, 0); started = time.time(); _rl_seen["after"] = None
            requeued = False
            task = asyncio.create_task(_do_dl(url, reply_msg, uid))
            dl_tasks[uid] = task
            try:
                await task
            except asyncio.CancelledError:
                break   # DL_CANCEL cancels the active task and clears the queue
            except Exception:
                logging.exception(f"[link_worker] download failed: {url[:80]}")
            else:
                if _upload_count.get(uid, 0) > before: _rl_tries.pop((uid, url), None)
                if (_upload_count.get(uid, 0) == before
                        and any(e[2] == "HTTP 429" for e in (dl_diag.get(uid) or []))):
                    host = _link_host(url); key = (uid, url)
                    tries = _rl_tries[key] = _rl_tries.get(key, 0) + 1
                    given = _rl_seen["after"] if _rl_seen["ts"] >= started else None
                    wait = given if given is not None else min(60 * 2 ** (tries - 1), 900)
                    _host_cooldown[host] = max(_host_cooldown.get(host, 0), time.time() + wait + 1)   # a server-stated wait is never shortened
                    if tries >= _RL_MAX_TRIES:
                        # repeated refusals: park the link instead of dropping it; /held resumes or clears it
                        _rl_tries.pop(key, None)
                        _held_links.setdefault(uid, []).append(url); requeued = True   # stays in pending_links (no duplicates)
                        await _say(reply_msg, f"⏸ {host} is still rate limiting (HTTP 429) after {tries} tries. The link is held, not lost "
                                              f"({len(_held_links[uid])} held). Use /held to resume it or clear it. "
                                              f"{'The site asked for ' + _fmt_wait(given) + '; that wait is respected.' if given is not None else ''}".strip(),
                                reply_markup=_held_kb())
                    else:
                        q.put_nowait(url); requeued = True
                        await _say(reply_msg, f"⏸ {host} asked to slow down (HTTP 429). This link is retried in about {_fmt_wait(wait)} "
                                              f"(try {tries + 1} of {_RL_MAX_TRIES}); other sites continue meanwhile.")
            finally:
                pend = pending_links.get(uid)
                if not requeued and pend and url in pend: pend.remove(url)
    finally:
        link_workers.pop(uid, None)


# ══════════════════════════════════════════════════════
#  MESSAGE DISPATCHER & BUTTON HANDLER
# ══════════════════════════════════════════════════════
async def on_message(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = u.message
    if not msg: return
    uid = u.effective_user.id

    is_image = bool(msg.photo or (msg.document and getattr(msg.document, "mime_type", "").startswith("image/")))
    if pending_thumb.get(uid) and is_image:
        raw_thumb = f"thumb_raw_{uid}.jpg"
        f = await (msg.photo[-1] if msg.photo else msg.document).get_file()
        await f.download_to_drive(raw_thumb)
        scaled = prepare_telegram_thumb(raw_thumb)
        user_thumbnails[uid] = scaled
        pending_thumb.pop(uid, None)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("👁 View", callback_data="VIEW_THUMB"), InlineKeyboardButton("🗑 Flush", callback_data="FLUSH_THUMB")]])
        await msg.reply_text("✅ Custom thumbnail saved!", reply_markup=kb)
        return

    if pending_mp3.get(uid) and (msg.video or msg.document):
        pending_mp3.pop(uid)
        await _do_mp3_from_file(u, c)
        return

    if pending_rename.get(uid, {}).get("mode") == "search" and msg.text:
        pending_rename.pop(uid)
        q = msg.text.strip()
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name FROM files WHERE LOWER(name) LIKE ? ORDER BY id DESC LIMIT 25", (f"%{q.lower()}%",))
            rows = cur.fetchall()
        if not rows: return await msg.reply_text(f'No results for "{q}".')
        kb = [[InlineKeyboardButton(f"  {r[1][:40]}", callback_data=f"FILE|{r[0]}")] for r in rows]
        await msg.reply_text(f'*{len(rows)} result(s)* for "{q}"', reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return

    _rt = getattr(msg, "reply_to_message", None)
    if msg.text and _rt is not None and (u.effective_chat.id, getattr(_rt, "message_id", None)) in pending_names:
        _fid = pending_names.pop((u.effective_chat.id, _rt.message_id))
        _nm = re.sub(r"[\r\n\t]+", " ", msg.text).strip()[:200]
        if _nm:
            await _do_rename(c.bot, msg, _fid, _nm, echo_chat=u.effective_chat.id)
        return

    if "db_id" in pending_rename.get(uid, {}) and msg.text:
        data = pending_rename.pop(uid)
        await _do_rename(c.bot, msg, data["db_id"], msg.text.strip(), echo_chat=u.effective_chat.id)
        return

    if msg.text and not getattr(msg, "forward_origin", None):
        urls = [x.rstrip(".,;!?)>") for x in LINK_RE.findall(msg.text)]
        if urls:
            uid = u.effective_chat.id
            pend = pending_links.setdefault(uid, [])
            fresh = [x for x in urls if x not in pend]
            dupes = len(urls) - len(fresh)
            if fresh:
                q = link_queues.setdefault(uid, asyncio.Queue())
                for x in fresh:
                    pend.append(x)
                    await q.put(x)   # unbounded queue: put() never yields, worker cannot race the enqueue
                w = link_workers.get(uid)
                need_worker = not w or w.done()
                if need_worker:
                    link_workers[uid] = asyncio.create_task(_link_worker(uid, msg))
                queued_n = len(fresh) - (1 if need_worker else 0)
                notes = []
                if queued_n > 0:
                    # waiting = links in the queue not yet started; the one being downloaded is counted separately
                    waiting = max(q.qsize() - (1 if need_worker else 0), queued_n)
                    notes.append(f"➕ {queued_n} link{'s' if queued_n != 1 else ''} queued - one download at a time.\n"
                                 f"📋 Waiting now: {waiting} (plus 1 downloading).")
                if dupes:
                    notes.append(f"⏭️ {dupes} duplicate{'s' if dupes != 1 else ''} skipped (already downloading or queued).")
                if notes:
                    await msg.reply_text("\n".join(notes))
            elif dupes:
                await msg.reply_text("⏭️ Already downloading or queued - duplicate skipped.")
            return

    if msg.video or (msg.document and getattr(msg.document, "mime_type", "").startswith("video/")):
        if uid in user_thumbnails and os.path.exists(user_thumbnails[uid]):
            await _update_video_cover(msg, user_thumbnails[uid])
            return

        if uid not in batch_queues:
            batch_queues[uid] = asyncio.Queue()
            asyncio.create_task(batch_resend_worker(uid, c.bot))

        await batch_queues[uid].put(msg)
        return

    if msg.photo or msg.audio or msg.animation or msg.document or (msg.text and msg.forward_origin):
        raw = (msg.caption
               or (msg.document.file_name if msg.document else None)
               or (msg.audio.file_name if msg.audio else None)
               or "File")
        name = clean_name(raw) or raw or "File"

        if uid not in save_queues: save_queues[uid] = asyncio.Queue()
        if uid not in save_workers or save_workers[uid].done(): save_workers[uid] = asyncio.create_task(_save_worker(uid))
        await save_queues[uid].put({"update": u, "context": c, "name": name})
        return

def _cancel_chat(uid: int):
    """The Cancel button's action, shared with /cancelupload: stop the chat's active job and clear its waiting links."""
    flag = dl_cancel_flags.get(uid)
    if flag: flag[0] = True
    q_links = link_queues.get(uid)
    if q_links:
        while not q_links.empty():
            try: q_links.get_nowait()
            except asyncio.QueueEmpty: break
    if uid in pending_links: pending_links[uid].clear()
    task = dl_tasks.pop(uid, None)
    if task: task.cancel()

def _resume_held(uid: int, reply_msg) -> int:
    """Put held links back in the queue. A cooldown the site asked for is kept, so they wait it out first."""
    links = _held_links.pop(uid, [])
    if not links: return 0
    q = link_queues.setdefault(uid, asyncio.Queue())
    for x in links:
        _rl_tries.pop((uid, x), None); q.put_nowait(x)
    w = link_workers.get(uid)
    if not w or w.done():
        link_workers[uid] = asyncio.create_task(_link_worker(uid, reply_msg))
    return len(links)

def _held_text(uid: int) -> str:
    links = _held_links.get(uid) or []
    if not links: return "No held links."
    hosts = collections.Counter(_link_host(x) for x in links)
    now = time.time()
    lines = [f"⏸ {len(links)} held link{'s' if len(links) != 1 else ''} (rate limited, kept in memory; a restart loses them):"]
    for h, n in hosts.items():
        cd = _host_cooldown.get(h, 0) - now
        lines.append(f"• {h}: {n}" + (f" - the site asked to wait, about {_fmt_wait(cd)} left" if cd > 0 else ""))
    return "\n".join(lines)

def _held_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("▶ Continue", callback_data="HELD|resume"),
                                  InlineKeyboardButton("🗑 Clear held", callback_data="HELD|clear")]])

async def held_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_chat.id
    txt = _held_text(uid)
    await u.message.reply_text(txt, reply_markup=_held_kb() if _held_links.get(uid) else None)

async def cancelupload_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Upload-only command: acts only while this chat's job is in the upload step. Same effect as the inline Cancel
    button there (downloaded file kept, waiting links cleared); nothing happens at other steps."""
    cid = u.effective_chat.id
    if cid not in _uploading:
        return await u.message.reply_text("No upload is running right now, so there is nothing to cancel with this command.")
    _cancel_chat(cid)
    await u.message.reply_text("⛔ Cancelling the upload... The downloaded file is kept (see /kept), and waiting links are cleared like the Cancel button does.")

async def button_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    try: await q.answer()
    except Exception: pass
    uid  = q.from_user.id
    data = q.data or ""

    if data in ("HELD|resume", "HELD|clear"):
        cid = q.message.chat_id
        if data == "HELD|resume":
            _hosts = {_link_host(x) for x in (_held_links.get(cid) or [])}
            _left = max([_host_cooldown.get(h, 0) - time.time() for h in _hosts] or [0])
            n = _resume_held(cid, q.message)
            if not n: msg_ = "No held links."
            elif _left > 0: msg_ = (f"▶ {n} held link{'s' if n != 1 else ''} back in the queue, but the site asked to wait: "
                                    f"about {_fmt_wait(_left)} left. They start by themselves when it ends. Nothing to resend.")
            else: msg_ = f"▶ {n} held link{'s' if n != 1 else ''} back in the queue and starting. Nothing to resend."
        else:
            links = _held_links.pop(cid, [])
            pend = pending_links.get(cid) or []
            for x in links:
                if x in pend: pend.remove(x)
            msg_ = f"🗑 {len(links)} held link{'s' if len(links) != 1 else ''} cleared." if links else "No held links."
        try: await q.edit_message_text(msg_)
        except Exception: pass
        return
    if data == "ASKN|skip":
        pending_names.pop((q.message.chat_id, q.message.message_id), None)
        try: await q.edit_message_text("⏭ Skipped - name kept.")
        except Exception: pass
        return
    if await kept_button(q, data): return
    if data == "BACK": return await send_vault_page(q, 0, edit=True)
    if data == "SEARCH":
        pending_rename[uid] = {"mode": "search"}
        return await q.edit_message_text("Send your search query:")

    if data == "FLUSH_THUMB":
        pending_thumb.pop(uid, None)
        old = user_thumbnails.pop(uid, None)
        if old and os.path.exists(old):
            try: os.remove(old)
            except Exception: pass
        await q.message.reply_text("🗑 Custom thumbnail flushed!")
        return

    if data == "VIEW_THUMB":
        if uid in user_thumbnails and os.path.exists(user_thumbnails[uid]):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Flush Thumbnail", callback_data="FLUSH_THUMB")]])
            with open(user_thumbnails[uid], "rb") as fh:
                await q.message.reply_photo(photo=fh, caption="Active thumbnail:", reply_markup=kb)
        else:
            await q.message.reply_text("No custom thumbnail currently active.")
        return

    if "|" not in data: return
    act, tid = data.split("|", 1)

    if act == "PAGE": return await send_vault_page(q, int(tid), edit=True)
    if act == "DL_CANCEL":
        _cancel_chat(int(tid))
        return

    if act == "FILE":
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, added_at FROM files WHERE id=?", (int(tid),))
            row = cur.fetchone()
        if not row: return await q.edit_message_text("Not found.")
        await q.edit_message_text(f"*{row[0]}*\nID {tid} • {(row[1] or '')[:16]}", reply_markup=file_kb(tid), parse_mode="Markdown")
        return

    if act == "GET":
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT msg_id FROM files WHERE id=?", (int(tid),))
            row = cur.fetchone()
        if not row: return await q.answer("Not found!", show_alert=True)
        try:
            await c.bot.copy_message(chat_id=q.message.chat_id, from_chat_id=STORAGE_CHANNEL_ID, message_id=row[0])
        except Exception:
            try: await tgclient.forward_messages(q.message.chat_id, row[0], from_peer=STORAGE_CHANNEL_ID)
            except Exception as e: await q.answer(str(e), show_alert=True)
        return

    if act == "DEL":
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM files WHERE id=?", (int(tid),))
            conn.commit()
        await q.edit_message_text("🗑 Deleted.")
        return

    if act == "REN":
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT msg_id, name FROM files WHERE id=?", (int(tid),))
            row = cur.fetchone()
        if not row: return await q.answer("Not found.", show_alert=True)
        pending_rename[uid] = {"db_id": int(tid), "msg_id": row[0]}
        await q.edit_message_text(f"*Rename*\n\nCurrent: _{row[1]}_\n\nSend the new name:", parse_mode="Markdown")
        return

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, (NetworkError, TimedOut)):
        logging.warning(f"[Telegram Network] Transient connection blip: {context.error}")
        return
    logging.error(f"Update error: {context.error}", exc_info=context.error)


# ══════════════════════════════════════════════════════
#  MAIN ENTRYPOINT
# ══════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════
#  BROWSER HANDOFF  (loopback receiver for the TitanVault browser extension)
#   - binds 127.0.0.1 only; disabled with BRIDGE_ENABLE=0
#   - /pair in Telegram (owner only) -> short-lived code -> extension swaps it for a token
#   - every request: Host check, extension Origin check, Bearer token, small JSON body
#   - the only thing it accepts is an http(s) media/page URL. No file paths, no commands, no cookies.
# ══════════════════════════════════════════════════════
import hashlib, hmac, secrets, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BRIDGE_CTX = {"loop": None, "bot": None}
_pair_codes: dict = {}          # code -> {"chat_id", "exp", "fails"}
_send_times: list = []          # recent /send timestamps (rate limit)
_bridge_lock = threading.Lock()

def _hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()

def _bridge_db_init():
    with get_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS bridge_tokens (token_hash TEXT PRIMARY KEY, chat_id INTEGER, created_at TEXT DEFAULT (datetime('now')))")
        conn.commit()

def _bridge_token_chat(tok: str):
    if not tok: return None
    h = _hash_token(tok)
    with get_db() as conn:
        for row in conn.execute("SELECT token_hash, chat_id FROM bridge_tokens"):
            if hmac.compare_digest(row[0], h):
                return row[1]
    return None

def _origin_allowed(origin: str) -> bool:
    if not origin: return False
    if BRIDGE_EXT_ID:
        return origin in (f"chrome-extension://{BRIDGE_EXT_ID}", f"moz-extension://{BRIDGE_EXT_ID}")
    return origin.startswith(("chrome-extension://", "moz-extension://"))

class _BridgeHandler(BaseHTTPRequestHandler):
    server_version = "TitanVaultBridge"
    protocol_version = "HTTP/1.1"
    def log_message(self, *a, **k): pass   # never log request lines (they could contain URLs)

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        origin = self.headers.get("Origin", "")
        if _origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _guard(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{BRIDGE_PORT}", f"localhost:{BRIDGE_PORT}"):
            self._send(403, {"ok": False, "error": "bad host"}); return False
        if not _origin_allowed(self.headers.get("Origin", "")):
            self._send(403, {"ok": False, "error": "bad origin"}); return False
        return True

    def _json_body(self):
        try: n = int(self.headers.get("Content-Length", "0"))
        except ValueError: n = -1
        if n < 0 or n > 8192:
            self._send(413, {"ok": False, "error": "body too large"}); return None
        try: return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._send(400, {"ok": False, "error": "bad json"}); return None

    def _auth_chat(self):
        auth = self.headers.get("Authorization", "")
        chat = _bridge_token_chat(auth[7:].strip()) if auth.startswith("Bearer ") else None
        if chat is None: self._send(401, {"ok": False, "error": "not paired"})
        return chat

    def do_OPTIONS(self):
        if not self._guard(): return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", ""))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        # Browsers omit Origin on simple extension GETs; a token is still required, and
        # an Origin that IS present must belong to an extension. Host is always checked.
        host = (self.headers.get("Host") or "").lower()
        origin = self.headers.get("Origin", "")
        if host not in (f"127.0.0.1:{BRIDGE_PORT}", f"localhost:{BRIDGE_PORT}") or (origin and not _origin_allowed(origin)):
            return self._send(403, {"ok": False, "error": "bad host or origin"})
        if self.path == "/ping":
            if self._auth_chat() is not None: self._send(200, {"ok": True})
            return
        self._send(404, {"ok": False})

    def do_POST(self):
        if not self._guard(): return
        data = self._json_body()
        if data is None or not isinstance(data, dict): return
        if self.path == "/pair":
            code = str(data.get("code", "")).strip()
            with _bridge_lock:
                entry = _pair_codes.get(code)
                now = time.time()
                if not entry or entry["exp"] < now or entry["fails"] >= 5:
                    for e in _pair_codes.values(): e["fails"] += 1     # slow down guessing
                    for k in [k for k, e in _pair_codes.items() if e["exp"] < now]: _pair_codes.pop(k, None)
                    self._send(403, {"ok": False, "error": "invalid or expired code"}); return
                _pair_codes.pop(code, None)
            tok = secrets.token_urlsafe(32)
            with get_db() as conn:
                conn.execute("INSERT INTO bridge_tokens (token_hash, chat_id) VALUES (?,?)", (_hash_token(tok), entry["chat_id"]))
                conn.commit()
            self._send(200, {"ok": True, "token": tok}); return
        if self.path == "/send":
            chat = self._auth_chat()
            if chat is None: return
            url = str(data.get("url", "")).strip()
            page = str(data.get("page_url", "")).strip()
            title = re.sub(r"[\r\n\t]", " ", str(data.get("title", "")))[:120]
            if not _valid_http_url(url) or (page and not _valid_http_url(page)):
                self._send(400, {"ok": False, "error": "need an http(s) url"}); return
            with _bridge_lock:
                now = time.time()
                _send_times[:] = [t for t in _send_times if now - t < 60]
                if len(_send_times) >= 10:
                    self._send(429, {"ok": False, "error": "rate limited"}); return
                _send_times.append(now)
            loop, bot = BRIDGE_CTX["loop"], BRIDGE_CTX["bot"]
            if not loop or not bot:
                self._send(503, {"ok": False, "error": "bot not ready"}); return
            asyncio.run_coroutine_threadsafe(_bridge_launch(bot, chat, url, page, title), loop)
            self._send(202, {"ok": True}); return
        self._send(404, {"ok": False})

async def _bridge_launch(bot, chat_id: int, url: str, page_url: str, title: str):
    if _kept_blocked():
        return await _kept_block_notice(lambda t: bot.send_message(chat_id, t))
    if chat_id in dl_tasks and not dl_tasks[chat_id].done():
        await bot.send_message(chat_id, "⏳ A download is still running here. Send it again from the browser when it finishes (or cancel it).")
        return
    status = await bot.send_message(chat_id, f"🌐 *Received from browser*\n`{redact_url(url)}`", parse_mode="Markdown")
    task = asyncio.create_task(_run_direct_stream(url, status, chat_id, chat_id, referer=page_url, title=title))
    dl_tasks[chat_id] = task

def start_bridge(loop, bot):
    if not BRIDGE_ENABLE:
        return None
    _bridge_db_init()
    BRIDGE_CTX["loop"], BRIDGE_CTX["bot"] = loop, bot
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), _BridgeHandler)
    except OSError as e:
        logging.warning(f"Browser bridge could not bind 127.0.0.1:{BRIDGE_PORT}: {e}")
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="tv-bridge").start()
    return srv

async def askname_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    cid = u.effective_chat.id
    arg = (c.args[0].lower() if c.args else "")
    if arg in ("on", "off"):
        _ask_name_set(cid, arg == "on")
    on = _ask_name_on(cid)
    await u.message.reply_text(
        f"Ask for name is {'ON' if on else 'OFF'}.\n"
        + ("After each finished upload I will ask for a visible name (reply to the question, or Skip). Downloads keep going meanwhile.\n" if on
           else "No naming questions; uploads behave as usual.\n")
        + "Use /askname on or /askname off.")

async def kept_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    rows = [r for r in _kept_rows() if r[3] == u.effective_chat.id]
    if not rows:
        return await u.message.reply_text("No kept files. Nothing is waiting for a retry.")
    await u.message.reply_text(f"Kept: {fmt_size(_kept_total())} of {fmt_size(KEEP_CAP_BYTES)} limit")
    for rid, path, title, chat_id, size in rows:
        await u.message.reply_text(f"{os.path.basename(path)[:50]} ({fmt_size(os.path.getsize(path))})", reply_markup=_kept_kb(rid, _kept_state(rid)))

async def kept_button(q, data: str) -> bool:
    """Handles KEPT_RETRY|id, KEPT_FORCE|id and KEPT_DEL|id. Only acts on files registered by a failed job
    inside the download folder. A file whose last upload outcome is unknown (cancelled in flight) is never
    retried by the plain Retry: only Delete or the explicit "Upload anyway"."""
    if not data.startswith(("KEPT_RETRY|", "KEPT_FORCE|", "KEPT_DEL|")): return False
    act, rid = data.split("|", 1)
    with get_db() as conn:
        row = conn.execute("SELECT id, path, title, chat_id, state FROM kept_jobs WHERE id=?", (int(rid),)).fetchone()
    valid = bool(row) and row[3] == q.message.chat_id and os.path.exists(row[1]) and _inside_job_dir(row[1])
    if valid and act == "KEPT_RETRY" and row[4]:
        await q.edit_message_text("⚠️ The earlier upload of this file may already be in the vault, so a plain retry could create a duplicate. "
                                  "Check the vault: Delete this file if it is there, or choose Upload anyway.",
                                  reply_markup=_kept_kb(row[0], row[4]))
        return True
    if row:
        with get_db() as conn:
            conn.execute("DELETE FROM kept_jobs WHERE id=?", (row[0],)); conn.commit()
    if not valid:
        await q.edit_message_text("That file is no longer available.")
        return True
    _KEPT_SOURCES.discard(row[1])
    if act == "KEPT_DEL":
        try: os.remove(row[1])
        except Exception: pass
        await q.edit_message_text("🗑 Deleted.")
        return True
    await q.edit_message_text("🔁 Retrying upload...")
    _cid = q.message.chat_id
    async def _retry():
        try: return await _finish_upload(row[1], row[2], q.message, _cid, _cid)
        except asyncio.CancelledError:
            await safe_final(q.message, "⛔ Upload cancelled.")
    _t = asyncio.create_task(_retry())
    if _cid not in dl_tasks or dl_tasks[_cid].done():
        dl_tasks[_cid] = _t   # lets the Cancel button reach a retry too
        _t.add_done_callback(lambda t, c=_cid: dl_tasks.pop(c, None) if dl_tasks.get(c) is t else None)
    return True

async def pair_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    if not BRIDGE_ENABLE:
        return await u.message.reply_text("Browser bridge is disabled (BRIDGE_ENABLE=0).")
    code = secrets.token_urlsafe(6)
    with _bridge_lock:
        _pair_codes[code] = {"chat_id": u.effective_chat.id, "exp": time.time() + 300, "fails": 0}
    await u.message.reply_text(
        f"🔗 *Pairing code* (valid 5 min, single use):\n`{code}`\n\n"
        f"Open the TitanVault browser extension popup, enter this code and press *Pair*. "
        f"The extension only talks to this computer (127.0.0.1:{BRIDGE_PORT}).",
        parse_mode="Markdown")

async def unpair_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    with get_db() as conn:
        n = conn.execute("DELETE FROM bridge_tokens").rowcount
        conn.commit()
    await u.message.reply_text(f"🔌 Revoked {n} paired browser(s).")

def _nm_startup_line() -> str:
    if N_M3U8DL_MODE == "off": return "N_m3u8DL-RE: disabled (N_M3U8DL_MODE=off)"
    b = _nm_find_binary()
    if b: return f"N_m3u8DL-RE: found (folder {os.path.basename(os.path.dirname(b)) or '.'})"
    return "N_m3u8DL-RE: not found (looked in the Downloads folder for a win-x64 folder, next to this script, and on PATH)"

def _snatch_startup_lines() -> list:
    """One line per optional piece, so a missing driver is visible at boot instead of at the first link."""
    if not SNATCH_ENABLE:
        return ["Capture leg: disabled (SNATCH_ENABLE=0)"]
    out = []
    _bb = _find_browser_bin()
    out.append(f"browser for CDP tier: {_bb}" if _bb else
               "browser for CDP tier: NONE - install Chrome, or set CHROME_PATH to a chromium binary")
    try:
        import nodriver  # noqa: F401
        out.append("Capture tier 1 (nodriver): installed" if NODRIVER_TIER else "Capture tier 1 (nodriver): off (NODRIVER_TIER=0)")
    except Exception:
        out.append("Capture tier 1 (nodriver): not installed (pip install nodriver)")
    _pw()   # resolves the driver once so the line below reports the one actually in use   ← changed
    if _PW_DEGRADED[0]:
        out.append("Capture tier 2: plain playwright - PATCHRIGHT IS MISSING, tier 2 is weaker without it "
                   "(pip install patchright && python -m patchright install chromium)")
    else:
        out.append("Capture tier 2: patchright (patched Chromium)")
    try:
        import nodriver_turnstile  # noqa: F401
        out.append("Turnstile solver: nodriver-turnstile present")
    except Exception:
        out.append("Turnstile solver: absent (widget clicks only)")
    if os.path.isfile(WV_DEVICE):
        out.append(f"DRM: Widevine CDM present ({os.path.basename(WV_DEVICE)})")
    else:
        out.append("DRM: detection only, no CDM (WV_DEVICE not set)")
    out.append(f"cf profile: {'on (cf_clearance is kept in profiles/<host>)' if CF_PROFILE else 'off (CF_PROFILE=1 to keep cf_clearance)'}"
               + (", headful" if CF_HEADFUL else ", headless"))
    out.append(f"yt-dlp impersonation: {'available' if _YDL_IMPERSONATE else 'UNAVAILABLE - update yt-dlp'}")
    out.append(f"capture budget: {SNATCH_MAX_SEC}s per leg, body cap 200 KB/entry, "
               f"{_SNATCH_COOLDOWN_SEC}s pause after 2 launch failures")
    return out

async def main():
    init_db()
    print(f"{Fore.CYAN}{_nm_startup_line()}{Style.RESET_ALL}")
    for line in _snatch_startup_lines():
        print(f"{Fore.CYAN}{line}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}Quality ceiling: {'best available' if not QUALITY_DEFAULT else str(QUALITY_DEFAULT) + 'p'}"
          f" (per user via /quality){Style.RESET_ALL}")
    if _TITANVAULT_DEBUG:
        print(f"{Fore.YELLOW}TITANVAULT_DEBUG is on - extra [diag]/[sxyprn] lines will be printed{Style.RESET_ALL}")

    await tgclient.start()
    me = await tgclient.get_me()
    print(f"{Fore.CYAN}Telethon user logged in as: {me.first_name}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}Connected to DC{tgclient.session.dc_id} (Parallel upload acceleration enabled){Style.RESET_ALL}")

    await tgbot.start(bot_token=BOT_TOKEN)
    bot_me = await tgbot.get_me()
    print(f"{Fore.CYAN}Telethon bot logged in as: {bot_me.username}{Style.RESET_ALL}")

    app = (ApplicationBuilder()
           .token(BOT_TOKEN)
           .read_timeout(30)
           .write_timeout(300)
           .connect_timeout(30)
           .build())

    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("series", "Set series name for auto-naming"),
        BotCommand("ep", "Set starting episode counter"),
        BotCommand("quality", "Output quality ceiling (best by default)"),
        BotCommand("files", "Browse vault files"),
        BotCommand("dl", "Download video from URL"),
        BotCommand("stream", "Direct media/playlist URL input"),
        BotCommand("kept", "Files kept after a failed upload"),
        BotCommand("cancelupload", "Cancel the upload that is running now"),
        BotCommand("askname", "Ask for a video name after each upload: on / off"),
        BotCommand("pair", "Pair the browser extension (owner)"),
        BotCommand("unpair", "Revoke paired browsers (owner)"),
        BotCommand("rename", "Rename last file"),
        BotCommand("delthumb", "Same as /flush"),
        BotCommand("held", "Links held after rate limiting"),
        BotCommand("stats", "Per-leg hit rate"),
        BotCommand("diag", "Diagnostics of the last job"),
        BotCommand("mp3", "Extract MP3"),
        BotCommand("convert", "Set output format / size"),
        BotCommand("thumb", "Set custom thumbnail"),
        BotCommand("viewthumb", "Preview thumbnail"),
        BotCommand("flush", "Clear thumbnail"),
        BotCommand("search", "Search the vault"),
        BotCommand("status", "Vault stats"),
        BotCommand("limits", "Rate limit status"),
        BotCommand("help", "Show all commands"),
    ]
    try: await app.bot.set_my_commands(commands)
    except Exception as e: logging.warning(f"set_my_commands failed: {type(e).__name__}")

    app.add_handler(CommandHandler("start",     start_cmd))
    app.add_handler(CommandHandler("help",      help_cmd))
    app.add_handler(CommandHandler("series",    series_cmd))
    app.add_handler(CommandHandler(["ep", "resetep"], resetep_cmd))
    app.add_handler(CommandHandler("status",    status_cmd))
    app.add_handler(CommandHandler("limits",    limits_cmd))
    app.add_handler(CommandHandler("stats",     stats_cmd))
    app.add_handler(CommandHandler("diag",      diag_cmd))
    app.add_handler(CommandHandler("quality",   quality_cmd))
    app.add_handler(CommandHandler("files",     files_cmd))
    app.add_handler(CommandHandler("search",    search_cmd))
    app.add_handler(CommandHandler(["dl", "snatch"], dl_cmd))
    app.add_handler(CommandHandler("stream",    stream_cmd))
    app.add_handler(CommandHandler("kept",      kept_cmd))
    app.add_handler(CommandHandler("cancelupload", cancelupload_cmd))
    app.add_handler(CommandHandler("held",      held_cmd))
    app.add_handler(CommandHandler("askname",   askname_cmd))
    app.add_handler(CommandHandler("pair",      pair_cmd))
    app.add_handler(CommandHandler("unpair",    unpair_cmd))
    app.add_handler(CommandHandler("mp3",       mp3_cmd))
    app.add_handler(CommandHandler("convert",   convert_cmd))
    app.add_handler(CommandHandler("thumb",     thumb_cmd))
    app.add_handler(CommandHandler("viewthumb", viewthumb_cmd))
    app.add_handler(CommandHandler(["flush", "flushthumb", "delthumb"], flush_cmd))
    app.add_handler(CommandHandler("done",      done_cmd))
    app.add_handler(CommandHandler("rename",    rename_cmd))
    app.add_handler(CommandHandler("session",   session_cmd))
    app.add_handler(CommandHandler("quiet",     quiet_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_message))
    app.add_error_handler(error_handler)

    print(f"{Fore.GREEN}+----------------------------------+")
    print(f"|   TITAN VAULT  --  online        |")
    print(f"+----------------------------------+{Style.RESET_ALL}")

    async with app:
        await app.start()
        start_bridge(asyncio.get_running_loop(), app.bot)
        await app.updater.start_polling()
        try:
            await tgclient.run_until_disconnected()
        finally:
            await app.updater.stop()
            await app.stop()
    await tgbot.disconnect()

if __name__ == "__main__":
    asyncio.run(main())