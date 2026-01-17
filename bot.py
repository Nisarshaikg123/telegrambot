import os
import shutil
import asyncio
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

# =========================
# CONFIG
# =========================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8593572876:AAGHni07-8nfM24hQep-OF8Aev1VXgDaSEI").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var not set (Railway -> Variables -> BOT_TOKEN)")

# Don't crash if ffmpeg missing at startup
FFMPEG_OK = bool(shutil.which("ffmpeg"))

# Use a writable directory (Windows Temp) to avoid permission issues
DOWNLOAD_DIR = os.path.join(os.environ.get("TEMP", os.getcwd()), "telegrambot_downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


USER = {}  # uid -> {"lang": "hi"/"en", "link": str}
QUALITIES = ["144", "240", "360", "480", "720", "1080"]

# =========================
# HELPERS
# =========================

def tr(uid: int, hi: str, en: str) -> str:
    lang = USER.get(uid, {}).get("lang", "en")
    return hi if lang == "hi" else en

def is_instagram(url: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
        return "instagram.com" in host or "instagr.am" in host
    except Exception:
        return False

def clean_temp(prefix: str):
    """Remove temp files inside DOWNLOAD_DIR starting with prefix."""
    try:
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(prefix):
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, f))
                except Exception:
                    pass
    except Exception:
        pass

def bar(pct: float, width: int = 18) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int((pct / 100.0) * width)
    return "█" * filled + "░" * (width - filled)

# =========================
# UI MENUS
# =========================

def menu_language() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇮🇳 Hindi", callback_data="lang_hi"),
         InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")]
    ])

def menu_format(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎵 MP3", callback_data="fmt_mp3"),
         InlineKeyboardButton("🎬 MP4", callback_data="fmt_mp4")],
        [InlineKeyboardButton(tr(uid, "🌐 भाषा बदलें", "🌐 Change language"), callback_data="go_lang")]
    ])

def menu_quality(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("144p", callback_data="q_144"),
         InlineKeyboardButton("240p", callback_data="q_240"),
         InlineKeyboardButton("360p", callback_data="q_360")],
        [InlineKeyboardButton("480p", callback_data="q_480"),
         InlineKeyboardButton("720p", callback_data="q_720"),
         InlineKeyboardButton("1080p", callback_data="q_1080")],
        [InlineKeyboardButton(tr(uid, "⬅️ पीछे", "⬅️ Back"), callback_data="back_fmt"),
         InlineKeyboardButton(tr(uid, "🏠 Home", "🏠 Home"), callback_data="home")]
    ])

# =========================
# PROGRESS (safe)
# =========================

class Prog:
    def __init__(self):
        self.lock = threading.Lock()
        self.text = None
        self.done = False

def make_hook(prog: Prog, prefix: str):
    def hook(d):
        st = d.get("status")
        if st == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                pct = (done / total) * 100.0
                eta = d.get("eta")
                speed = d.get("speed") or 0
                s_txt = f" | {speed/1024/1024:.2f} MB/s" if speed else ""
                e_txt = f" | ETA {int(eta)}s" if isinstance(eta, (int, float)) else ""
                txt = f"{prefix}\n{bar(pct)} {pct:.1f}%{s_txt}{e_txt}"
            else:
                txt = f"{prefix}\nDownloading…"

            with prog.lock:
                prog.text = txt

        elif st == "finished":
            with prog.lock:
                prog.text = f"{prefix}\n✅ Download finished. Processing…"
    return hook

async def poll_progress(bot, chat_id: int, msg_id: int, prog: Prog):
    last = None
    while True:
        with prog.lock:
            t = prog.text
            done = prog.done

        if t and t != last:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=t)
                last = t
            except Exception:
                pass

        if done:
            break

        await asyncio.sleep(1.0)

# =========================
# HANDLERS
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    USER.setdefault(uid, {})

    # Ask language only first time
    if "lang" not in USER[uid]:
        await update.message.reply_text("🌐 Select Language / भाषा चुनें", reply_markup=menu_language())
    else:
        await update.message.reply_text(tr(uid, "🔗 वीडियो लिंक भेजो", "🔗 Send video link"))

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    USER.setdefault(uid, {})

    link = (update.message.text or "").strip()
    if not link:
        return

    # Store new link every time
    USER[uid]["link"] = link

    if "lang" not in USER[uid]:
        await update.message.reply_text("🌐 Select Language / भाषा चुनें", reply_markup=menu_language())
        return

    await update.message.reply_text(tr(uid, "📥 Format चुनें", "📥 Choose format"), reply_markup=menu_format(uid))

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    USER.setdefault(uid, {})
    data = q.data

    if data == "go_lang":
        await q.edit_message_text("🌐 Select Language / भाषा चुनें", reply_markup=menu_language())
        return

    if data.startswith("lang_"):
        USER[uid]["lang"] = data.split("_", 1)[1]
        await q.edit_message_text(tr(uid, "✅ भाषा सेट हो गई! अब लिंक भेजो 🔗",
                                     "✅ Language set! Now send link 🔗"))
        return

    if data == "home":
        await q.edit_message_text(tr(uid, "🔗 वीडियो लिंक भेजो", "🔗 Send video link"))
        return

    if data == "back_fmt":
        await q.edit_message_text(tr(uid, "📥 Format चुनें", "📥 Choose format"), reply_markup=menu_format(uid))
        return

    link = USER[uid].get("link", "").strip()
    if not link:
        await q.edit_message_text(tr(uid, "⚠️ पहले लिंक भेजो", "⚠️ Send a link first"))
        return

    # MP3
    if data == "fmt_mp3":
        if not FFMPEG_OK:
            await q.edit_message_text("❌ FFmpeg missing on server. Redeploy with ffmpeg enabled.")
            return

        msg = await context.bot.send_message(
            chat_id=q.message.chat_id,
            text=tr(uid, "🎵 MP3 डाउनलोड शुरू…", "🎵 MP3 download started…")
        )
        await download_mp3(link, q.message.chat_id, context, msg.message_id, uid)
        await q.edit_message_text(tr(uid, "✅ Done! अगला लिंक भेजो 🔗", "✅ Done! Send next link 🔗"))
        return

    # MP4: Instagram auto, others ask quality
    if data == "fmt_mp4":
        if is_instagram(link):
            if not FFMPEG_OK:
                await q.edit_message_text("❌ FFmpeg missing on server. Redeploy with ffmpeg enabled.")
                return

            msg = await context.bot.send_message(
                chat_id=q.message.chat_id,
                text=tr(uid, "🎬 Instagram MP4 (Auto) शुरू…", "🎬 Instagram MP4 (Auto) started…")
            )
            await download_mp4(link, None, q.message.chat_id, context, msg.message_id, uid)
            await q.edit_message_text(tr(uid, "✅ Done! अगला लिंक भेजो 🔗", "✅ Done! Send next link 🔗"))
            return

        await q.edit_message_text(tr(uid, "🎚 Quality चुनें", "🎚 Select quality"), reply_markup=menu_quality(uid))
        return

    # Quality chosen
    if data.startswith("q_"):
        if not FFMPEG_OK:
            await q.edit_message_text("❌ FFmpeg missing on server. Redeploy with ffmpeg enabled.")
            return

        qv = data.split("_", 1)[1]
        if qv not in QUALITIES:
            await q.edit_message_text("❌ Invalid quality")
            return

        msg = await context.bot.send_message(
            chat_id=q.message.chat_id,
            text=tr(uid, f"🎬 MP4 {qv}p डाउनलोड शुरू…", f"🎬 MP4 {qv}p download started…")
        )
        await download_mp4(link, qv, q.message.chat_id, context, msg.message_id, uid)
        await q.edit_message_text(tr(uid, "✅ Done! अगला लिंक भेजो 🔗", "✅ Done! Send next link 🔗"))
        return

# =========================
# DOWNLOADS
# =========================

async def download_mp3(url: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE, progress_msg_id: int, uid: int):
    clean_temp("audio_")

    prog = Prog()
    prefix = tr(uid, "🎵 MP3 डाउनलोड…", "🎵 MP3 downloading…")
    hook = make_hook(prog, prefix)
    poll_task = asyncio.create_task(poll_progress(context.bot, chat_id, progress_msg_id, prog))

    def _run():
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(DOWNLOAD_DIR, "audio_%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "progress_hooks": [hook],
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    try:
        await asyncio.to_thread(_run)
    except Exception:
        with prog.lock:
            prog.text = "❌ Download failed."
            prog.done = True
        await poll_task
        return

    mp3_file = None
    for f in os.listdir(DOWNLOAD_DIR):
        if f.startswith("audio_") and f.endswith(".mp3"):
            mp3_file = f
            break

    if not mp3_file:
        with prog.lock:
            prog.text = "❌ MP3 not found."
            prog.done = True
        await poll_task
        return

    full = os.path.join(DOWNLOAD_DIR, mp3_file)
    await context.bot.send_document(chat_id=chat_id, document=open(full, "rb"))
    os.remove(full)

    with prog.lock:
        prog.text = "✅ MP3 Ready!"
        prog.done = True
    await poll_task

async def download_mp4(url: str, quality: str | None, chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                       progress_msg_id: int, uid: int):
    clean_temp("video_")

    prog = Prog()
    prefix = tr(uid, "🎬 MP4 डाउनलोड…", "🎬 MP4 downloading…")
    hook = make_hook(prog, prefix)
    poll_task = asyncio.create_task(poll_progress(context.bot, chat_id, progress_msg_id, prog))

    if is_instagram(url) or not quality:
        formats = ["best", "b"]
    else:
        formats = [
            f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]",
            f"best[height<={quality}]",
            "best",
        ]

    def _try(fmt: str):
        ydl_opts = {
            "format": fmt,
            "merge_output_format": "mp4",
            "outtmpl": os.path.join(DOWNLOAD_DIR, "video_%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "progress_hooks": [hook],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    ok = False
    for fmt in formats:
        try:
            await asyncio.to_thread(_try, fmt)
            ok = True
            break
        except DownloadError:
            continue
        except Exception:
            continue

    if not ok:
        with prog.lock:
            prog.text = "❌ Download failed. Try another public link."
            prog.done = True
        await poll_task
        return

    mp4_file = None
    for f in os.listdir(DOWNLOAD_DIR):
        if f.startswith("video_") and f.endswith(".mp4"):
            mp4_file = f
            break

    if not mp4_file:
        with prog.lock:
            prog.text = "❌ MP4 not created."
            prog.done = True
        await poll_task
        return

    full = os.path.join(DOWNLOAD_DIR, mp4_file)
    try:
     await context.bot.send_video(chat_id=chat_id, video=open(full, "rb"))
    except Exception:
     await context.bot.send_message(
        chat_id=chat_id,
        text="❌ Upload failed (timeout). Video file bahut bada ho sakta hai ya internet slow hai. Chhoti quality try karo."
    )
    try:
        os.remove(full)
    except Exception:
        pass
    with prog.lock:
        prog.text = "❌ Upload timed out."
        prog.done = True
    await poll_task
    return

    os.remove(full)

    with prog.lock:
        prog.text = "✅ MP4 Ready!"
        prog.done = True
    await poll_task

# =========================
# MAIN
# =========================

def main():
   app = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .read_timeout(180)
    .write_timeout(180)
    .connect_timeout(60)
    .pool_timeout(60)
    .build()
    )

   app.add_handler(CommandHandler("start", cmd_start))
   app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
   app.add_handler(CallbackQueryHandler(on_button))

   print("✅ Bot running")
   app.run_polling()

if __name__ == "__main__":
    main()
