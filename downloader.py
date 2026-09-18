import os
import re
import uuid
import logging
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any
import requests
import yt_dlp
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TDRC, TRCK, ID3NoHeaderError
from mutagen.mp3 import MP3
import config

logger = logging.getLogger(__name__)


class _YtDlpLogger:
    """Forward yt-dlp messages to logging AND stdout (Back4App system stream)."""

    def debug(self, msg):
        # yt-dlp debug is very noisy; keep as debug so INFO logs stay clean.
        logger.debug("yt-dlp: %s", msg)

    def warning(self, msg):
        logger.warning("yt-dlp: %s", msg)
        print(f"yt-dlp WARNING: {msg}", flush=True)

    def error(self, msg):
        logger.error("yt-dlp: %s", msg)
        print(f"yt-dlp ERROR: {msg}", flush=True)


def _log_yt_dlp_diagnostics() -> None:
    """Log one-line environment diagnostics to explain silent failures."""
    try:
        import shutil
        import importlib.util as _ilu

        yt_version = getattr(yt_dlp.version, "__version__", "unknown")
        deno_path = shutil.which("deno")
        node_path = shutil.which("node")
        ejs_spec = _ilu.find_spec("yt_dlp_ejs") or _ilu.find_spec("yt-dlp-ejs")
        cffi_spec = _ilu.find_spec("curl_cffi")
        env_msg = (
            f"yt-dlp env: version={yt_version} deno={deno_path or 'missing'} "
            f"node={node_path or 'missing'} ejs={'installed' if ejs_spec else 'missing'} "
            f"curl_cffi={'installed' if cffi_spec else 'missing'} ffmpeg={config.FFMPEG_EXECUTABLE}"
        )
        logger.info(env_msg)
        print(env_msg, flush=True)
        if not deno_path and not node_path:
            warn_msg = (
                "yt-dlp: no JS runtime (deno/node) found; "
                "signature/n challenges will fail. See https://github.com/yt-dlp/yt-dlp/wiki/EJS"
            )
            logger.warning(warn_msg)
            print(f"yt-dlp WARNING: {warn_msg}", flush=True)
    except Exception as diag_err:
        logger.debug("yt-dlp diagnostics failed: %s", diag_err)


def sanitize_filename(name: str) -> str:
    """Remove illegal characters from file names."""
    sanitized = re.sub(r'[\\/*?:"<>|]', "", name)
    # Remove leading/trailing periods or spaces
    return sanitized.strip(". ") or "audio"


def download_track(track_info: Dict[str, Any], output_dir: Optional[Path] = None) -> Optional[Path]:
    """Download audio for a Spotify track using yt-dlp and embed official metadata and album art.
    
    Args:
        track_info: Dict with keys 'title', 'artist', 'album', 'year', 'cover_url', 'duration_sec'
        output_dir: Destination folder (defaults to config.DOWNLOADS_DIR)

    Returns:
        Path to the completed .mp3 file, or None if download failed.
    """
    if output_dir is None:
        output_dir = config.DOWNLOADS_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    title = track_info.get("title", "Unknown Title")
    artist = track_info.get("artist", "Unknown Artist")
    album = track_info.get("album", "")
    year = str(track_info.get("year", ""))
    cover_url = track_info.get("cover_url")
    track_num = str(track_info.get("track_number", 1))

    # Construct unique filename
    safe_base = sanitize_filename(f"{artist} - {title}")
    unique_suffix = uuid.uuid4().hex[:6]
    file_stem = f"{safe_base}_{unique_suffix}"
    output_template = str(output_dir / f"{file_stem}.%(ext)s")
    target_mp3 = output_dir / f"{file_stem}.mp3"

    # Search query
    query = f"ytsearch1:{artist} - {title} audio"

    ydl_opts = {
        "format": "bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": output_template,
        "ffmpeg_location": config.FFMPEG_EXECUTABLE,
        "logger": _YtDlpLogger(),
        "verbose": False,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": config.AUDIO_BITRATE,
            }
        ],
        "noplaylist": True,
        "default_search": "ytsearch",
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "quiet": False,
        "no_warnings": False,
        "geo_bypass": True,
        # JS challenge solving (signature + n) requires Deno + EJS.
        # pip installs need explicit opt-in for remote EJS components.
        "remote_components": ["ejs:npm"],
        "extractor_args": {
            "youtube": {
                # NOTE: android/ios do NOT support cookies (warnings in logs)
                # and web/mweb REQUIRE a GVS PO Token for https formats.
                # tv / web_embedded do NOT require a PO Token and DO
                # support cookies, so try them first.
                "player_client": ["tv_downgraded", "tv", "web_embedded", "web", "mweb"],
                "player_skip": ["webpage"],
            }
        },
    }

    # Impersonate Chrome to look less like a datacenter bot.
    # Only enabled when curl_cffi is installed, otherwise yt-dlp
    # raises "Impersonate target chrome is not available".
    try:
        import importlib.util as _ilu

        if _ilu.find_spec("curl_cffi") is not None:
            ydl_opts["impersonate"] = "chrome"
    except Exception:
        pass

    # Optional manual PO Token override for datacenter IPs:
    # set YOUTUBE_PO_TOKEN="web.gvs+XXX:mweb.gvs+YYY" to force it.
    _po_token_env = os.getenv("YOUTUBE_PO_TOKEN", "").strip()
    if _po_token_env:
        ydl_opts["extractor_args"]["youtube"]["po_token"] = _po_token_env.split(";")

    cookies_path = config.get_youtube_cookies_path()
    if cookies_path:
        ydl_opts["cookiefile"] = str(cookies_path)
        logger.info("Using configured YouTube cookies for yt-dlp.")
    else:
        logger.warning("No YouTube cookies configured; tv clients will return LOGIN_REQUIRED.")

    _log_yt_dlp_diagnostics()
    logger.info(
        "Starting download for '%s - %s' with query '%s' (clients=%s)",
        artist,
        title,
        query,
        ydl_opts["extractor_args"]["youtube"]["player_client"],
    )
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([query])
    except Exception as e:
        import traceback as _tb

        err_msg = f"yt-dlp error downloading '{artist} - {title}': {type(e).__name__}: {e or '<empty message>'}"
        logger.error(err_msg, exc_info=True)
        # Duplicate to stdout so Back4App system stream shows it (no error filter needed).
        print(err_msg, flush=True)
        print(_tb.format_exc(), flush=True)
        return None

    if not target_mp3.exists():
        try:
            leftovers = sorted(p.name for p in output_dir.glob(f"{file_stem}.*"))
        except Exception:
            leftovers = []
        missing_msg = f"Target MP3 file not found after download: {target_mp3} (leftovers={leftovers})"
        logger.error(missing_msg)
        print(missing_msg, flush=True)
        return None

    # Embed ID3 tags and album cover art
    try:
        try:
            tags = ID3(str(target_mp3))
        except ID3NoHeaderError:
            tags = ID3()

        tags.delall("TIT2")
        tags.add(TIT2(encoding=3, text=title))

        tags.delall("TPE1")
        tags.add(TPE1(encoding=3, text=artist))

        if album:
            tags.delall("TALB")
            tags.add(TALB(encoding=3, text=album))

        if year:
            tags.delall("TDRC")
            tags.add(TDRC(encoding=3, text=year))

        if track_num:
            tags.delall("TRCK")
            tags.add(TRCK(encoding=3, text=track_num))

        # Download and embed album cover
        if cover_url:
            try:
                resp = requests.get(cover_url, timeout=10)
                if resp.status_code == 200:
                    tags.delall("APIC")
                    tags.add(
                        APIC(
                            encoding=3,
                            mime="image/jpeg",
                            type=3,  # Front cover
                            desc="Cover",
                            data=resp.content,
                        )
                    )
            except Exception as img_err:
                logger.warning("Could not attach album cover for '%s': %s", title, img_err)

        tags.save(str(target_mp3), v2_version=3)
        logger.info("Successfully tagged and prepared MP3: %s", target_mp3)
    except Exception as tag_err:
        logger.warning("Tagging error for '%s' (audio still usable): %s", target_mp3, tag_err)

    return target_mp3


async def download_track_async(
    track_info: Dict[str, Any], output_dir: Optional[Path] = None
) -> Optional[Path]:
    """Asynchronous wrapper for downloading a track without blocking the event loop."""
    return await asyncio.to_thread(download_track, track_info, output_dir)


def download_cover_thumbnail(cover_url: str, output_path: Path) -> Optional[Path]:
    """Download cover image locally to use as Telegram audio thumbnail."""
    try:
        resp = requests.get(cover_url, timeout=10)
        if resp.status_code == 200:
            with open(output_path, "wb") as f:
                f.write(resp.content)
            return output_path
    except Exception as e:
        logger.warning("Failed to download thumbnail from %s: %s", cover_url, e)
    return None
