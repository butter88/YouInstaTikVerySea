import os
import re
import json
import logging
import tempfile
import asyncio
import base64
import urllib.request
import urllib.parse

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import yt_dlp

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SUPPORTED_PATTERNS = [
    # TikTok
    re.compile(r"https?://(?:www\.)?(?:tiktok\.com|vm\.tiktok\.com)/\S+"),
    # Instagram Reels / Posts / Stories
    re.compile(r"https?://(?:www\.)?instagram\.com/(?:reel|p|stories)/\S+"),
    # YouTube (normal, shorts, youtu.be)
    re.compile(r"https?://(?:www\.)?youtube\.com/(?:watch|shorts)\S+"),
    re.compile(r"https?://youtu\.be/\S+"),
]

ANY_SUPPORTED_URL = re.compile(
    r"https?://(?:"
    r"(?:www\.)?(?:tiktok\.com|vm\.tiktok\.com)/\S+"
    r"|(?:www\.)?instagram\.com/(?:reel|p|stories)/\S+"
    r"|(?:www\.)?youtube\.com/(?:watch|shorts)[?/]\S+"
    r"|youtu\.be/\S+"
    r")"
)

MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

# --- Instagram cookies setup ---
_INSTAGRAM_COOKIES_PATH = None


def _setup_instagram_cookies() -> str | None:
    """Build a Netscape cookies file from individual Instagram cookie env vars.

    Required env vars (get from browser DevTools > Application > Cookies > instagram.com):
      - IG_SESSIONID   → sessionid cookie
      - IG_DS_USER_ID  → ds_user_id cookie
      - IG_CSRFTOKEN   → csrftoken cookie

    Alternatively, supports legacy INSTAGRAM_COOKIES (small base64/plain text).
    """
    sessionid = os.getenv("IG_SESSIONID", "").strip()
    ds_user_id = os.getenv("IG_DS_USER_ID", "").strip()
    csrftoken = os.getenv("IG_CSRFTOKEN", "").strip()

    if sessionid and ds_user_id:
        # Build minimal Netscape format cookies file
        lines = [
            "# Netscape HTTP Cookie File",
            f".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t{sessionid}",
            f".instagram.com\tTRUE\t/\tTRUE\t0\tds_user_id\t{ds_user_id}",
        ]
        if csrftoken:
            lines.append(f".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\t{csrftoken}")
        content = "\n".join(lines) + "\n"
        logger.info("Instagram cookies built from IG_SESSIONID + IG_DS_USER_ID env vars")
    else:
        # Fallback: try INSTAGRAM_COOKIES env var (small files only)
        raw = os.getenv("INSTAGRAM_COOKIES", "").strip()
        if not raw:
            return None
        try:
            content = base64.b64decode(raw).decode("utf-8")
        except Exception:
            content = raw

    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="ig_cookies_", delete=False
        )
        tmp.write(content)
        tmp.close()
        logger.info("Instagram cookies loaded (%d bytes)", len(content))
        return tmp.name
    except Exception as e:
        logger.error("Failed to setup Instagram cookies: %s", e)
        return None


def extract_supported_url(text: str) -> str | None:
    """Extract the first supported URL (TikTok/Instagram/YouTube) from text."""
    match = ANY_SUPPORTED_URL.search(text)
    return match.group(0) if match else None


def is_supported_url(url: str) -> bool:
    """Check if URL matches any supported platform."""
    return ANY_SUPPORTED_URL.match(url) is not None


def _is_tiktok(url: str) -> bool:
    return "tiktok.com" in url


def _is_instagram(url: str) -> bool:
    return "instagram.com" in url


def _download_with_ytdlp(url: str, output_path: str) -> dict | None:
    """Download video using yt-dlp (works for TikTok, Instagram, YouTube)."""
    ydl_opts = {
        "outtmpl": output_path,
        "format": "best[ext=mp4][filesize<50M]/best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {"tiktok": {"api_hostname": ["api22-normal-c-alisg.tiktokv.com"]}},
    }

    # Pass Instagram cookies if available
    if _is_instagram(url) and _INSTAGRAM_COOKIES_PATH:
        ydl_opts["cookiefile"] = _INSTAGRAM_COOKIES_PATH
        logger.info("Using Instagram cookies file for yt-dlp")
    elif _is_instagram(url):
        logger.warning("No Instagram cookies available - download may fail")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return info


def _download_tiktok_fallback(url: str, output_path: str) -> dict | None:
    """Fallback for TikTok using tikwm.com API."""
    api_url = "https://www.tikwm.com/api/?url=" + urllib.parse.quote(url, safe="")
    req = urllib.request.Request(
        api_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())

    if data.get("code") != 0:
        logger.error("tikwm API error: %s", data.get("msg"))
        return None

    video_data = data["data"]
    video_url = video_data.get("play") or video_data.get("hdplay")
    if not video_url:
        logger.error("tikwm API returned no video URL")
        return None

    vid_req = urllib.request.Request(
        video_url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    with urllib.request.urlopen(vid_req, timeout=60) as vid_resp:
        with open(output_path, "wb") as f:
            while True:
                chunk = vid_resp.read(1024 * 64)
                if not chunk:
                    break
                f.write(chunk)

    return {"title": video_data.get("title", "")}


def _download_instagram_fallback(url: str, output_path: str) -> dict | None:
    """Fallback for Instagram using direct GraphQL API with session cookies."""
    match = re.search(r"instagram\.com/(?:reel|p|stories)/([A-Za-z0-9_-]+)", url)
    if not match:
        logger.error("Could not extract Instagram shortcode from URL")
        return None

    shortcode = match.group(1)

    # We need the session cookies
    sessionid = os.getenv("IG_SESSIONID", "").strip()
    ds_user_id = os.getenv("IG_DS_USER_ID", "").strip()
    csrftoken = os.getenv("IG_CSRFTOKEN", "").strip()

    if not sessionid or not ds_user_id:
        logger.error("Instagram fallback requires IG_SESSIONID and IG_DS_USER_ID env vars")
        return None

    cookie_str = f"sessionid={sessionid}; ds_user_id={ds_user_id}"
    if csrftoken:
        cookie_str += f"; csrftoken={csrftoken}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_str,
        "X-IG-App-ID": "936619743392459",
        "X-Requested-With": "XMLHttpRequest",
    }
    if csrftoken:
        headers["X-CSRFToken"] = csrftoken

    # Try Instagram GraphQL API to get media info
    graphql_url = (
        "https://www.instagram.com/graphql/query/"
        f"?query_hash=b3055c01b4b222b8a47dc12b090e4e64"
        f"&variables=%7B%22shortcode%22%3A%22{shortcode}%22%7D"
    )

    req = urllib.request.Request(graphql_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        media = data["data"]["shortcode_media"]
        video_url = media.get("video_url")
        title = media.get("edge_media_to_caption", {}).get("edges", [{}])
        if title:
            title = title[0].get("node", {}).get("text", "")
        else:
            title = ""
    except Exception as e:
        logger.warning("Instagram GraphQL failed: %s, trying page scrape...", e)
        # Fallback: fetch the post page and extract video_url from page source
        page_url = f"https://www.instagram.com/p/{shortcode}/"
        req2 = urllib.request.Request(page_url, headers=headers)
        with urllib.request.urlopen(req2, timeout=30) as resp2:
            html = resp2.read().decode("utf-8", errors="replace")

        # Try to find video_url in the page HTML
        vid_match = re.search(r'"video_url"\s*:\s*"([^"]+)"', html)
        if not vid_match:
            logger.error("Could not find video_url in Instagram page")
            return None
        video_url = vid_match.group(1).replace("\\u0026", "&")
        title = ""

    if not video_url:
        logger.error("Instagram media has no video_url (might be a photo)")
        return None

    logger.info("Instagram fallback: downloading video from direct URL")
    vid_req = urllib.request.Request(
        video_url,
        headers={
            "User-Agent": headers["User-Agent"],
            "Cookie": cookie_str,
        },
    )
    with urllib.request.urlopen(vid_req, timeout=60) as vid_resp:
        with open(output_path, "wb") as f:
            while True:
                chunk = vid_resp.read(1024 * 64)
                if not chunk:
                    break
                f.write(chunk)

    return {"title": title or f"Instagram {shortcode}"}


def download_video(url: str, output_path: str) -> dict | None:
    """Download video from TikTok, Instagram, or YouTube."""
    # --- Attempt 1: yt-dlp (universal) ---
    try:
        info = _download_with_ytdlp(url, output_path)
        if os.path.exists(output_path):
            return info
    except Exception as e:
        logger.warning("yt-dlp failed: %s", e)

    # --- Attempt 2: TikTok-specific fallback ---
    if _is_tiktok(url):
        try:
            return _download_tiktok_fallback(url, output_path)
        except Exception as e:
            logger.error("TikTok fallback also failed: %s", e)

    # --- Attempt 3: Instagram-specific fallback ---
    if _is_instagram(url):
        try:
            return _download_instagram_fallback(url, output_path)
        except Exception as e:
            logger.error("Instagram fallback also failed: %s", e)

    return None


async def cmd_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /video command: download and send video from TikTok/Instagram/YouTube."""
    if not context.args:
        await update.message.reply_text(
            "Usa el comando asi:\n"
            "<code>/video https://www.tiktok.com/...</code>\n"
            "<code>/video https://www.instagram.com/reel/...</code>\n"
            "<code>/video https://www.youtube.com/shorts/...</code>",
            parse_mode="HTML",
        )
        return

    url = context.args[0]
    if not is_supported_url(url):
        await update.message.reply_text(
            "Enlace no soportado. Usa enlaces de TikTok, Instagram o YouTube."
        )
        return

    status_msg = await update.message.reply_text("Descargando video del pesado de @danikratos...")

    tmp_dir = tempfile.mkdtemp()
    output_path = os.path.join(tmp_dir, "video.mp4")

    try:
        info = download_video(url, output_path)

        if not os.path.exists(output_path):
            await status_msg.edit_text("No se pudo descargar el video.")
            return

        file_size = os.path.getsize(output_path)
        if file_size > MAX_TELEGRAM_FILE_SIZE:
            await status_msg.edit_text(
                "El video es demasiado grande para enviarlo por Telegram (max 50 MB)."
            )
            return

        caption = info.get("title", "") if info else ""
        if len(caption) > 1024:
            caption = caption[:1021] + "..."

        with open(output_path, "rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                caption=caption,
                read_timeout=120,
                write_timeout=120,
            )

        await status_msg.delete()

    except Exception as e:
        logger.error("Error descargando video: %s", e)
        await status_msg.edit_text(
            "Error al descargar el video. Comprueba que el enlace sea valido."
        )
    finally:
        if os.path.exists(output_path):
            os.remove(output_path)
        if os.path.exists(tmp_dir):
            os.rmdir(tmp_dir)


async def auto_detect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-detect TikTok/Instagram/YouTube links and reply with the video."""
    if not update.message or not update.message.text:
        return

    url = extract_supported_url(update.message.text)
    if not url:
        return

    status_msg = await update.message.reply_text("Descargando video...")

    tmp_dir = tempfile.mkdtemp()
    output_path = os.path.join(tmp_dir, "video.mp4")

    try:
        info = download_video(url, output_path)

        if not os.path.exists(output_path):
            await status_msg.edit_text("No se pudo descargar el video.")
            return

        file_size = os.path.getsize(output_path)
        if file_size > MAX_TELEGRAM_FILE_SIZE:
            await status_msg.edit_text(
                "El video es demasiado grande para enviarlo por Telegram (max 50 MB)."
            )
            return

        caption = info.get("title", "") if info else ""
        if len(caption) > 1024:
            caption = caption[:1021] + "..."

        with open(output_path, "rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                caption=caption,
                read_timeout=120,
                write_timeout=120,
            )

        await status_msg.delete()

    except Exception as e:
        logger.error("Error descargando video: %s", e)
        await status_msg.edit_text(
            "Error al descargar el video. Comprueba que el enlace sea valido."
        )
    finally:
        if os.path.exists(output_path):
            os.remove(output_path)
        if os.path.exists(tmp_dir):
            os.rmdir(tmp_dir)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hola! Envia /video + enlace y te devolvere el video.\n\n"
        "Plataformas soportadas:\n"
        "- TikTok\n"
        "- Instagram (Reels/Posts)\n"
        "- YouTube (Videos/Shorts)\n\n"
        "Tambien puedes simplemente pegar un enlace y lo detectare automaticamente."
    )


def main() -> None:
    global _INSTAGRAM_COOKIES_PATH
    _INSTAGRAM_COOKIES_PATH = _setup_instagram_cookies()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en .env")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("video", cmd_video))
    # Auto-detect TikTok/Instagram/YouTube links in any text message
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_detect))

    logger.info("Bot iniciado")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
