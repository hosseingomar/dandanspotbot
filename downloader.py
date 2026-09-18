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


def _progress_hook(status: Dict[str, Any]) -> None:
    """yt-dlp progress callback, mirrored to stdout so Back4App shows it."""
    try:
        state = status.get("status")
        if state == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
            downloaded = status.get("downloaded_bytes", 0)
            print(f"yt-dlp progress: downloading {downloaded}/{total} bytes", flush=True)
        elif state == "finished":
            print(f"yt-dlp progress: finished {status.get('filename', '')}", flush=True)
        elif state == "error":
            print("yt-dlp progress: error", flush=True)
    except Exception:
        pass


def _probe_search(query: str, ydl_opts: Dict[str, Any]) -> str:
    """Failure-path diagnostic: what does the search query actually return?

    Returns a one-line summary (entries found, first id/title, available
    formats) so 'MP3 not found, leftovers=[]' stops being a mystery.
    """
    try:
        probe_opts = {
            "quiet": True,
            "no_warnings": True,
            "logger": _YtDlpLogger(),
            "socket_timeout": 20,
            "extractor_args": ydl_opts.get("extractor_args"),
        }
        if ydl_opts.get("cookiefile"):
            probe_opts["cookiefile"] = ydl_opts["cookiefile"]
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(query, download=False)
        if not info:
            return "probe: search returned no info"
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                return "probe: search returned 0 entries"
            first = entries[0]
            return (
                f"probe: search returned {len(entries)} entries; "
                f"first id={first.get('id')} title={first.get('title', '')[:60]}"
            )
        formats = info.get("formats") or []
        return (
            f"probe: direct video id={info.get('id')} title={info.get('title', '')[:60]} "
            f"formats={len(formats)}"
        )
    except Exception as e:
        return f"probe failed: {type(e).__name__}: {e}"


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


# Fragments yt-dlp may leave behind when a download is interrupted.
_STALE_SUFFIXES = (".part", ".temp", ".ytdl", ".ytdlpart")


def cleanup_stale_downloads(output_dir: Optional[Path] = None) -> int:
    """Delete interrupted-download fragments and orphaned MP3s.

    MP3s here are always transient (deleted right after Telegram upload),
    so anything found at startup or before a download is an orphan from a
    crash/redeploy and safe to remove. Returns files removed.
    """
    if output_dir is None:
        output_dir = config.DOWNLOADS_DIR
    if not output_dir.is_dir():
        return 0
    removed = 0
    for path in output_dir.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(_STALE_SUFFIXES) or name.endswith(".mp3"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        logger.info("Cleaned %d stale file(s) from downloads.", removed)
        print(f"Cleaned {removed} stale file(s) from downloads.", flush=True)
    return removed


def _free_mb(path: Path) -> float:
    """Free disk space in MB for the filesystem containing path."""
    try:
        import shutil

        return shutil.disk_usage(path).free / (1024 * 1024)
    except OSError:
        return -1.0


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

    # The container disk is tiny; interrupted downloads leave .part files
    # that eventually fill it (Errno 28) and kill the server. Clear them
    # before every download.
    cleanup_stale_downloads(output_dir)
    free = _free_mb(output_dir)
    if 0 <= free < 200:
        logger.warning("Low disk space before download: %.0f MB free.", free)
        print(f"WARNING: low disk space: {free:.0f} MB free.", flush=True)

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
        "progress_hooks": [_progress_hook],
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

    # Impersonation is intentionally disabled: yt-dlp 2026.8.19 crashes
    # with a bare AssertionError when "impersonate" is passed as a plain
    # string ("chrome") via the Python API (see
    # yt_dlp/networking/impersonate.py: assert isinstance(target, ImpersonateTarget)).
    # tv/web_embedded clients + cookies + Deno/EJS are sufficient without it.
    # To re-enable later, pass an ImpersonateTarget object, not a string:
    #   from yt_dlp.networking.impersonate import ImpersonateTarget
    #   ydl_opts["impersonate"] = ImpersonateTarget.from_str("chrome")

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
    start_msg = (
        f"Starting download for '{artist} - {title}' with query '{query}' "
        f"(clients={ydl_opts['extractor_args']['youtube']['player_client']})"
    )
    logger.info(start_msg)
    print(start_msg, flush=True)
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
        # Disk-full leaves a .part fragment behind; clear it now so the
        # next queued song is not doomed by the same leftover.
        if isinstance(e, OSError) and e.errno == 28:
            cleanup_stale_downloads(output_dir)
            disk_msg = (
                f"Disk full while downloading '{artist} - {title}'; "
                "cleaned fragments, job will retry."
            )
            logger.error(disk_msg)
            print(disk_msg, flush=True)
        return None

    if not target_mp3.exists():
        try:
            leftovers = sorted(p.name for p in output_dir.glob(f"{file_stem}.*"))
        except Exception:
            leftovers = []
        missing_msg = f"Target MP3 file not found after download: {target_mp3} (leftovers={leftovers})"
        logger.error(missing_msg)
        print(missing_msg, flush=True)
        probe_msg = _probe_search(query, ydl_opts)
        logger.error(probe_msg)
        print(probe_msg, flush=True)
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
