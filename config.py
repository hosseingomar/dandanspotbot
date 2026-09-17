import os
import sys
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
