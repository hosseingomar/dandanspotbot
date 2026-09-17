import atexit
import base64
import binascii
import gzip
import os
import sys
import tempfile
from pathlib import Path
from dotenv import load_dotenv

# Base Directory
BASE_DIR = Path(__file__).resolve().parent

# Load environment variables from .env if present
load_dotenv(BASE_DIR / ".env")

# Telegram Configuration (MANDATORY)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Optional comma-separated list of Telegram User IDs allowed to use the bot.
# If empty, any user who can reach the bot can use it.
_raw_allowed_users = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").strip()
ALLOWED_TELEGRAM_USER_IDS = [
    int(uid.strip()) for uid in _raw_allowed_users.split(",") if uid.strip().isdigit()
] if _raw_allowed_users else []

# Spotify Configuration (OPTIONAL - Free Mode operates without these)
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID", "").strip()
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET", "").strip()
SPOTIPY_REDIRECT_URI = os.getenv(
    "SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
).strip()

# Optional Telegram Proxy (e.g., http://127.0.0.1:10809 or socks5://127.0.0.1:10808)
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip()

# Optional YouTube cookies for cloud deployments. This must be the Base64-encoded
# content of a Netscape-format cookies.txt file, stored as a platform secret.
# A local file path is also supported for development.
YOUTUBE_COOKIES_B64 = "".join(
    part
    for part in [os.getenv("YOUTUBE_COOKIES_B64", "").strip()]
    + [os.getenv(f"YOUTUBE_COOKIES_B64_PART_{index}", "").strip() for index in range(1, 10)]
    if part
)
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
_runtime_cookies_path: Path | None = None


def get_youtube_cookies_path() -> Path | None:
    """Return a yt-dlp cookie file without ever logging its contents."""
    global _runtime_cookies_path

    if YOUTUBE_COOKIES_FILE:
        candidate = Path(YOUTUBE_COOKIES_FILE)
        if candidate.is_file():
            return candidate
        print("YOUTUBE_COOKIES_FILE does not exist; continuing without YouTube cookies.", file=sys.stderr)
        return None

    if not YOUTUBE_COOKIES_B64:
        return None

    if _runtime_cookies_path and _runtime_cookies_path.is_file():
        return _runtime_cookies_path

    try:
        cookie_data = base64.b64decode(YOUTUBE_COOKIES_B64, validate=True)
        # Back4App limits environment variables to 1,023 characters. Accept a
        # gzip-compressed Base64 payload as well as a plain Base64 cookie file.
        if cookie_data.startswith(b"\x1f\x8b"):
            cookie_data = gzip.decompress(cookie_data)
        if not cookie_data.strip():
            raise ValueError("decoded value is empty")
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix="yt-dlp-cookies-", suffix=".txt", delete=False
        ) as cookie_file:
            cookie_file.write(cookie_data)
            _runtime_cookies_path = Path(cookie_file.name)
        os.chmod(_runtime_cookies_path, 0o600)
        return _runtime_cookies_path
    except (binascii.Error, OSError, ValueError) as exc:
        print(f"YOUTUBE_COOKIES_B64 is invalid ({exc}); continuing without YouTube cookies.", file=sys.stderr)
        return None


def _remove_runtime_cookies() -> None:
    if _runtime_cookies_path:
        try:
            _runtime_cookies_path.unlink(missing_ok=True)
        except OSError:
            pass


atexit.register(_remove_runtime_cookies)

# Monitoring & Downloading Settings
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "256").strip()  # 192, 256, or 320
UPLOAD_TIMEOUT_SECONDS = int(os.getenv("UPLOAD_TIMEOUT_SECONDS", "300"))  # 5 minutes

DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Cloud & Database Settings
PORT = int(os.getenv("PORT", "8080"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "data.db")))


def get_ffmpeg_path() -> str:
    """Returns the path to an ffmpeg executable.
    Checks imageio_ffmpeg first, falls back to 'ffmpeg' in PATH."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return "ffmpeg"


FFMPEG_EXECUTABLE = get_ffmpeg_path()


def has_spotify_api_keys() -> bool:
    """Check if Spotify Developer API credentials are provided."""
    return bool(
        SPOTIPY_CLIENT_ID
        and SPOTIPY_CLIENT_ID != "YOUR_SPOTIFY_CLIENT_ID_HERE"
        and SPOTIPY_CLIENT_SECRET
        and SPOTIPY_CLIENT_SECRET != "YOUR_SPOTIFY_CLIENT_SECRET_HERE"
    )


def validate_config(exit_on_error: bool = False) -> bool:
    """Check whether mandatory configuration variables are provided."""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        msg = (
            "Configuration Error: Missing TELEGRAM_BOT_TOKEN in .env!\n"
            "Please open .env and set your TELEGRAM_BOT_TOKEN from @BotFather."
        )
        if exit_on_error:
            print(msg, file=sys.stderr)
            return False
        else:
            print(msg)
            return False
    return True
