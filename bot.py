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
from telegram import Update, InputMediaPhoto
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


def _shortcode_to_media_id(shortcode: str) -> int:
    """Convert Instagram shortcode to numeric media ID."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    media_id = 0
    for char in shortcode:
        media_id = media_id * 64 + alphabet.index(char)
    return media_id


def _download_instagram_fallback(url: str, output_path: str) -> dict | None:
    """Fallback for Instagram using direct API with session cookies.
    Supports videos, single images, and carousels.
    Returns dict with 'title', and optionally 'image_urls' for photo posts.
    """
    match = re.search(r"instagram\.com/(?:reel|p|stories)/([A-Za-z0-9_-]+)", url)
    if not match:
        logger.error("Could not extract Instagram shortcode from URL")
        return None

    shortcode = match.group(1)

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
        "User-Agent": "Instagram 275.0.0.27.98 Android",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_str,
        "X-IG-App-ID": "936619743392459",
    }
    if csrftoken:
        headers["X-CSRFToken"] = csrftoken

    video_url = None
    image_urls = []
    title = ""

    # --- Method 1: Instagram mobile API (i.instagram.com) ---
    try:
        media_id = _shortcode_to_media_id(shortcode)
        api_url = f"https://i.instagram.com/api/v1/media/{media_id}/info/"
        logger.info("Instagram fallback: trying mobile API for media %s", media_id)
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        items = data.get("items", [])
        if items:
            item = items[0]
            # Get caption
            caption = item.get("caption")
            if caption and isinstance(caption, dict):
                title = caption.get("text", "")

            # Check for carousel
            carousel = item.get("carousel_media", [])
            if carousel:
                logger.info("Instagram fallback: detected carousel with %d items", len(carousel))
                for slide in carousel:
                    # Each slide can be video or image
                    vid_versions = slide.get("video_versions", [])
                    if vid_versions:
                        image_urls.append(vid_versions[0]["url"])
                    else:
                        # Get best quality image
                        candidates = slide.get("image_versions2", {}).get("candidates", [])
                        if candidates:
                            image_urls.append(candidates[0]["url"])
            else:
                # Single post - check video first, then image
                versions = item.get("video_versions", [])
                if versions:
                    video_url = versions[0].get("url")
                else:
                    candidates = item.get("image_versions2", {}).get("candidates", [])
                    if candidates:
                        image_urls.append(candidates[0]["url"])
    except Exception as e:
        logger.warning("Instagram mobile API failed: %s", e)

    # --- Method 2: page scrape (for video only) ---
    if not video_url and not image_urls:
        try:
            page_url = f"https://www.instagram.com/p/{shortcode}/"
            logger.info("Instagram fallback: trying page scrape")
            web_headers = dict(headers)
            web_headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
            req3 = urllib.request.Request(page_url, headers=web_headers)
            with urllib.request.urlopen(req3, timeout=30) as resp3:
                html = resp3.read().decode("utf-8", errors="replace")

            # Try video patterns
            for pattern in [
                r'"video_url"\s*:\s*"([^"]+)"',
                r'"contentUrl"\s*:\s*"([^"]+)"',
                r'<meta\s+property="og:video"\s+content="([^"]+)"',
            ]:
                vid_match = re.search(pattern, html)
                if vid_match:
                    video_url = vid_match.group(1).replace("\\u0026", "&").replace("\\/", "/")
                    logger.info("Instagram fallback: found video URL via page scrape")
                    break

            # If no video, try og:image as last resort
            if not video_url:
                img_match = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
                if img_match:
                    image_urls.append(img_match.group(1).replace("\\u0026", "&").replace("\\/", "/"))
                    logger.info("Instagram fallback: found image URL via og:image")
        except Exception as e:
            logger.warning("Instagram page scrape failed: %s", e)

    # --- Return images (carousel/single photo) ---
    if image_urls and not video_url:
        logger.info("Instagram fallback: returning %d image(s)", len(image_urls))
        return {"title": title or f"Instagram {shortcode}", "image_urls": image_urls}

    # --- Download video ---
    if not video_url:
        logger.error("Instagram: no media found for %s", shortcode)
        return None

    logger.info("Instagram fallback: downloading video...")
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
            result = _download_instagram_fallback(url, output_path)
            if result:
                return result
        except Exception as e:
            logger.error("Instagram fallback also failed: %s", e)

    return None


async def _send_media(update: Update, status_msg, info: dict | None, output_path: str) -> bool:
    """Send downloaded media (video file or Instagram images). Returns True on success."""
    if not info:
        return False

    caption = info.get("title", "")
    if len(caption) > 1024:
        caption = caption[:1021] + "..."

    # --- Instagram images (carousel or single photo) ---
    image_urls = info.get("image_urls", [])
    if image_urls:
        try:
            if len(image_urls) == 1:
                await update.message.reply_photo(
                    photo=image_urls[0],
                    caption=caption,
                    read_timeout=120,
                    write_timeout=120,
                )
            else:
                # Send as media group (max 10 per group in Telegram)
                for i in range(0, len(image_urls), 10):
                    batch = image_urls[i:i + 10]
                    media_group = []
                    for j, img_url in enumerate(batch):
                        media_group.append(InputMediaPhoto(
                            media=img_url,
                            caption=caption if (i == 0 and j == 0) else "",
                        ))
                    await update.message.reply_media_group(
                        media=media_group,
                        read_timeout=120,
                        write_timeout=120,
                    )
            await status_msg.delete()
            return True
        except Exception as e:
            logger.error("Error sending Instagram images: %s", e)
            return False

    # --- Video file ---
    if not os.path.exists(output_path):
        return False

    file_size = os.path.getsize(output_path)
    if file_size > MAX_TELEGRAM_FILE_SIZE:
        await status_msg.edit_text(
            "El video es demasiado grande para enviarlo por Telegram (max 50 MB)."
        )
        return True  # handled, don't show generic error

    with open(output_path, "rb") as video_file:
        await update.message.reply_video(
            video=video_file,
            caption=caption,
            read_timeout=120,
            write_timeout=120,
        )
    await status_msg.delete()
    return True


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
        sent = await _send_media(update, status_msg, info, output_path)
        if not sent:
            await status_msg.edit_text("No se pudo descargar el contenido.")

    except Exception as e:
        logger.error("Error descargando: %s", e)
        await status_msg.edit_text(
            "Error al descargar. Comprueba que el enlace sea valido."
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
        sent = await _send_media(update, status_msg, info, output_path)
        if not sent:
            await status_msg.edit_text("No se pudo descargar el contenido.")

    except Exception as e:
        logger.error("Error descargando: %s", e)
        await status_msg.edit_text(
            "Error al descargar. Comprueba que el enlace sea valido."
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
