import os
import shutil
import asyncio
import time
import threading
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadError

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

TOKEN = "8593572876:AAGHni07-8nfM24hQep-OF8Aev1VXgDaSEI"

# FFmpeg is needed for merging formats and MP3 conversion
if not shutil.which("ffmpeg"):
    raise RuntimeError("FFmpeg not found in PATH. Install FFmpeg and add it to PATH.")

users = {}  # uid -> dict(lang, link, quality)

# ---------------- HELPERS ----------------

def is_instagram(url: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
        return "instagram.com" in host or "instagr.am" in host
    except Exception:
        return False

def safe_cleanup(prefix: str):
    for f in os.listdir():
        if f.startswith(prefix):
            try:
                os.remove(f)
            except:
                pass

def t(uid: int, hi: str, en: str) -> str:
    lang = users.get(uid, {}).get("lang", "en")
    return hi if lang == "hi" else en

def progress_bar(pct: float, width: int = 18) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int((pct / 100.0) * width)
    return "█" * filled + "░" * (width - filled)

# ---------------- PROGRESS (NO WARNINGS) ----------------

class ProgressState:
    def __init__(self):
        self.lock = threading.Lock()
        self.text = None
        self.done = False

def make_progress_hook(state: ProgressState, prefix: str):
    def hook(d):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0

            if total and total > 0:
                pct = (downloaded / total) * 100.0
                bar = progress_bar(pct)
                speed = d.get("speed") or 0
                eta = d.get("eta")

                speed_txt = f" | {speed/1024/1024:.2f} MB/s" if speed else ""
                eta_txt = f" | ETA {int(eta)}s" if isinstance(eta, (int, float)) else ""
                msg = f"{prefix}\n{bar} {pct:.1f}%{speed_txt}{eta_txt}"
            else:
                msg = f"{prefix}\nDownloading…"

            with state.lock:
                state.text = msg

        elif status == "finished":
            with state.lock:
                state.text = f"{prefix}\n✅ Download finished. Processing…"
    return hook

async def progress_poller(bot, chat_id: int, message_id: int, state: ProgressState):
    last_sent = None
    while True:
        with state.lock:
            txt = state.text
            done = state.done

        if txt and txt != last_sent:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=txt)
                last_sent = txt
            except Exception:
                pass

        if done:
            break

        await asyncio.sleep(1.0)

# ---------------- UI MENUS ----------------

def lang_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇮🇳 Hindi", callback_data="lang_hi"),
            InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
        ]
    ])

def format_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎵 MP3", callback_data="mp3"),
            InlineKeyboardButton("🎬 MP4", callback_data="mp4"),
        ],
        [InlineKeyboardButton("🏠 Home (Language)", callback_data="home")],
    ])

def quality_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("144p", callback_data="q_144"),
            InlineKeyboardButton("240p", callback_data="q_240"),
            InlineKeyboardButton("360p", callback_data="q_360"),
        ],
        [
            InlineKeyboardButton("480p", callback_data="q_480"),
            InlineKeyboardButton("720p HD", callback_data="q_720"),
            InlineKeyboardButton("1080p FHD", callback_data="q_1080"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_format"),
         InlineKeyboardButton("🏠 Home", callback_data="home")],
    ])

# ---------------- HANDLERS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users.setdefault(uid, {})

    # Language only first time
    if "lang" in users[uid]:
        await update.message.reply_text(t(uid, "🔗 Video link भेजो", "🔗 Send video link"))
    else:
        await update.message.reply_text("🌐 Select Language / भाषा चुनें", reply_markup=lang_menu())

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    users.setdefault(uid, {})
    text = update.message.text.strip()

    # If language not set yet, ask language (store link anyway)
    if "lang" not in users[uid]:
        users[uid]["link"] = text
        await update.message.reply_text("🌐 Select Language / भाषा चुनें", reply_markup=lang_menu())
        return

    # Every new text = new link; reset quality
    users[uid]["link"] = text
    users[uid].pop("quality", None)

    await update.message.reply_text(t(uid, "📥 Format चुनें", "📥 Choose format"), reply_markup=format_menu())

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    users.setdefault(uid, {})
    data = q.data

    # Home -> change language
    if data == "home":
        await q.edit_message_text("🌐 Change Language / भाषा बदलें", reply_markup=lang_menu())
        return

    # Set language
    if data.startswith("lang_"):
        users[uid]["lang"] = data.split("_")[1]
        await q.edit_message_text(t(uid, "✅ Language set. अब video link भेजो 🔗",
                                    "✅ Language set. Now send video link 🔗"))
        return

    if data == "back_format":
        await q.edit_message_text(t(uid, "📥 Format चुनें", "📥 Choose format"), reply_markup=format_menu())
        return

    link = users[uid].get("link")
    if data in ("mp3", "mp4") and not link:
        await q.edit_message_text(t(uid, "⚠️ पहले video link भेजो", "⚠️ Send a video link first"))
        return

    # MP3
    if data == "mp3":
        msg = await context.bot.send_message(
            chat_id=q.message.chat_id,
            text=t(uid, "🎵 MP3 डाउनलोड शुरू…", "🎵 MP3 download started…")
        )
        await download_mp3_with_progress(
            url=link,
            chat_id=q.message.chat_id,
            context=context,
            progress_message_id=msg.message_id,
            uid=uid
        )
        await q.edit_message_text(t(uid, "✅ Done! नया link भेजो 🔗", "✅ Done! Send a new link 🔗"))
        return

    # MP4 -> quality menu
    if data == "mp4":
        await q.edit_message_text(t(uid, "🎚 Quality चुनें", "🎚 Select quality"), reply_markup=quality_menu())
        return

    # Quality chosen -> download MP4 with progress
    if data.startswith("q_"):
        users[uid]["quality"] = data.split("_")[1]

        prefix = t(uid, "🎬 MP4 डाउनलोड…", "🎬 MP4 downloading…")
        if is_instagram(link):
            prefix += t(uid, " (Instagram Auto quality)", " (Instagram Auto quality)")

        msg = await context.bot.send_message(chat_id=q.message.chat_id, text=prefix)

        await download_mp4_with_progress(
            url=link,
            quality=users[uid]["quality"],
            chat_id=q.message.chat_id,
            context=context,
            progress_message_id=msg.message_id,
            uid=uid
        )

        await q.edit_message_text(t(uid, "✅ Done! नया link भेजो 🔗", "✅ Done! Send a new link 🔗"))
        return

# ---------------- DOWNLOADS (WITH PROGRESS) ----------------

async def download_mp3_with_progress(url: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                                     progress_message_id: int, uid: int):
    safe_cleanup("audio_")

    state = ProgressState()
    prefix = t(uid, "🎵 MP3 डाउनलोड…", "🎵 MP3 downloading…")
    hook = make_progress_hook(state, prefix)

    poll_task = asyncio.create_task(progress_poller(context.bot, chat_id, progress_message_id, state))

    def _run():
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": "audio_%(id)s.%(ext)s",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
            "noplaylist": True,
            "quiet": True,
            "progress_hooks": [hook],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    try:
        await asyncio.to_thread(_run)
    except Exception:
        with state.lock:
            state.text = "❌ Download failed."
            state.done = True
        await poll_task
        return

    sent = False
    for f in os.listdir():
        if f.startswith("audio_") and f.endswith(".mp3"):
            await context.bot.send_document(chat_id=chat_id, document=open(f, "rb"))
            os.remove(f)
            sent = True
            break

    with state.lock:
        state.text = t(uid, "✅ MP3 Ready!", "✅ MP3 Ready!") if sent else "❌ MP3 not found."
        state.done = True

    await poll_task

async def download_mp4_with_progress(url: str, quality: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                                     progress_message_id: int, uid: int):
    safe_cleanup("video_")

    if is_instagram(url):
        format_fallbacks = ["best", "b"]
        prefix = t(uid, "🎬 MP4 डाउनलोड… (Auto quality)", "🎬 MP4 downloading… (Auto quality)")
    else:
        format_fallbacks = [
            f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]",
            f"best[height<={quality}]",
            "best",
        ]
        prefix = t(uid, "🎬 MP4 डाउनलोड…", "🎬 MP4 downloading…")

    state = ProgressState()
    hook = make_progress_hook(state, prefix)
    poll_task = asyncio.create_task(progress_poller(context.bot, chat_id, progress_message_id, state))

    def _try_download(fmt: str):
        ydl_opts = {
            "format": fmt,
            "merge_output_format": "mp4",
            "outtmpl": "video_%(id)s.%(ext)s",
            "noplaylist": True,
            "quiet": True,
            "progress_hooks": [hook],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    ok = False
    for fmt in format_fallbacks:
        try:
            await asyncio.to_thread(_try_download, fmt)
            ok = True
            break
        except DownloadError:
            continue
        except Exception:
            continue

    if not ok:
        with state.lock:
            state.text = "❌ Download failed. Try another public link."
            state.done = True
        await poll_task
        return

    mp4_file = None
    for f in os.listdir():
        if f.startswith("video_") and f.endswith(".mp4"):
            mp4_file = f
            break

    if not mp4_file:
        with state.lock:
            state.text = "❌ Failed to create MP4."
            state.done = True
        await poll_task
        return

    await context.bot.send_video(chat_id=chat_id, video=open(mp4_file, "rb"))
    os.remove(mp4_file)

    with state.lock:
        state.text = t(uid, "✅ MP4 Ready!", "✅ MP4 Ready!")
        state.done = True

    await poll_task

# ---------------- ERROR HANDLER ----------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    return

# ---------------- MAIN ----------------

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    print("✅ Bot running (Progress % enabled, no warnings)")
    app.run_polling()

if __name__ == "__main__":
    main()
