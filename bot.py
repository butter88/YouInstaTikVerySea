import os
import re
import json
import logging
import tempfile
import asyncio
import base64
import subprocess
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
    # Twitter / X
    re.compile(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/\S+/status/\S+"),
]

ANY_SUPPORTED_URL = re.compile(
    r"https?://(?:"
    r"(?:www\.)?(?:tiktok\.com|vm\.tiktok\.com)/\S+"
    r"|(?:www\.)?instagram\.com/(?:reel|p|stories)/\S+"
    r"|(?:www\.)?youtube\.com/(?:watch|shorts)[?/]\S+"
    r"|youtu\.be/\S+"
    r"|(?:www\.)?(?:twitter\.com|x\.com)/\S+/status/\S+"
    r")"
)

MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

SHARE_GOOGLE_RE = re.compile(r"https?://share\.google/\S+")

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


def _generate_thumbnail(video_path: str) -> str | None:
    """Extract a thumbnail from the video using ffmpeg."""
    thumb_path = video_path + ".thumb.jpg"
    try:
        subprocess.run(
            [
                "ffmpeg", "-i", video_path,
                "-ss", "00:00:01", "-vframes", "1",
                "-vf", "scale=320:-1",
                "-q:v", "5", thumb_path,
            ],
            capture_output=True, timeout=15,
        )
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception as e:
        logger.warning("Thumbnail generation failed: %s", e)
    return None


def _compress_video(input_path: str, target_size_mb: float = 49.0) -> str | None:
    """Re-encode video with ffmpeg to fit under target size."""
    output_path = input_path + ".compressed.mp4"
    try:
        # Get video duration
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", input_path],
            capture_output=True, text=True, timeout=15,
        )
        duration = float(probe.stdout.strip())
        if duration <= 0:
            return None

        # Calculate target bitrate (bits/s). Reserve 128kbps for audio.
        target_bits = target_size_mb * 8 * 1024 * 1024
        audio_bitrate = 128_000
        video_bitrate = int(target_bits / duration - audio_bitrate)
        if video_bitrate < 200_000:  # Less than 200kbps = unwatchable
            logger.warning("Video too long to compress under %sMB (would need %dkbps)",
                           target_size_mb, video_bitrate // 1000)
            return None

        logger.info("Compressing video: duration=%.1fs, target_vbitrate=%dkbps",
                     duration, video_bitrate // 1000)

        subprocess.run(
            [
                "ffmpeg", "-i", input_path,
                "-c:v", "libx264", "-preset", "fast",
                "-b:v", str(video_bitrate),
                "-maxrate", str(int(video_bitrate * 1.5)),
                "-bufsize", str(int(video_bitrate * 2)),
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                "-y", output_path,
            ],
            capture_output=True, timeout=300,
        )

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            final_size = os.path.getsize(output_path)
            logger.info("Compressed: %dMB -> %dMB",
                        os.path.getsize(input_path) // (1024*1024),
                        final_size // (1024*1024))
            if final_size <= MAX_TELEGRAM_FILE_SIZE:
                return output_path
            logger.warning("Compressed file still too large: %dMB", final_size // (1024*1024))
    except Exception as e:
        logger.error("Video compression failed: %s", e)

    if os.path.exists(output_path):
        os.remove(output_path)
    return None


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
    actual_path = output_path
    compressed_path = None

    if file_size > MAX_TELEGRAM_FILE_SIZE:
        await status_msg.edit_text("Video demasiado grande, comprimiendo...")
        compressed_path = _compress_video(output_path)
        if not compressed_path:
            await status_msg.edit_text(
                "El video es demasiado grande y no se pudo comprimir (max 50 MB)."
            )
            return True
        actual_path = compressed_path

    # Generate thumbnail so Telegram shows a preview instead of black
    thumb_path = _generate_thumbnail(actual_path)
    thumb_file = None
    try:
        if thumb_path:
            thumb_file = open(thumb_path, "rb")
        with open(actual_path, "rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                thumbnail=thumb_file,
                caption=caption,
                read_timeout=120,
                write_timeout=120,
            )
    finally:
        if thumb_file:
            thumb_file.close()
        if thumb_path and os.path.exists(thumb_path):
            os.remove(thumb_path)
        if compressed_path and os.path.exists(compressed_path):
            os.remove(compressed_path)
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
            "Enlace no soportado. Usa enlaces de TikTok, Instagram, YouTube o Twitter/X."
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


def _resolve_share_google(share_url: str) -> str | None:
    """Follow share.google redirect(s) to get the final real URL."""
    try:
        req = urllib.request.Request(
            share_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        final_url = resp.url
        resp.close()
        if final_url and final_url != share_url:
            return final_url
    except Exception as e:
        logger.warning("Failed to resolve share.google URL: %s", e)
    return None


async def _handle_share_google(update: Update, share_url: str, original_text: str) -> None:
    """Replace share.google link with the real URL, repost crediting the user."""
    user = update.message.from_user
    real_url = await asyncio.to_thread(_resolve_share_google, share_url)

    if not real_url:
        logger.warning("Could not resolve share.google link: %s", share_url)
        return  # Don't touch the message if we can't resolve it

    # Build the new text with the real URL
    new_text = original_text.replace(share_url, real_url)

    # Credit the original user
    if user.username:
        credit = f"@{user.username}"
    else:
        credit = f'<a href="tg://user?id={user.id}">{user.first_name}</a>'

    new_message = f"{new_text}\n\n— Compartido por {credit}"

    # Try to delete the original message (needs admin permissions)
    try:
        await update.message.delete()
    except Exception as e:
        logger.warning("Could not delete original message: %s", e)
        # If we can't delete, just reply instead of reposting
        await update.message.reply_text(
            f"Enlace real: {real_url}",
            disable_web_page_preview=False,
        )
        return

    # Send the new message with the real URL
    await update.message.chat.send_message(
        text=new_message,
        parse_mode="HTML",
        disable_web_page_preview=False,
    )


async def auto_detect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-detect supported links and share.google URLs."""
    if not update.message or not update.message.text:
        return

    text = update.message.text

    # --- Handle share.google links ---
    share_match = SHARE_GOOGLE_RE.search(text)
    if share_match:
        await _handle_share_google(update, share_match.group(0), text)
        return

    # --- Handle video links ---
    url = extract_supported_url(text)
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
        "- YouTube (Videos/Shorts)\n"
        "- Twitter/X (Videos)\n\n"
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
    # Auto-detect TikTok/Instagram/YouTube/Twitter links in any text message
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_detect))

    logger.info("Bot iniciado")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
