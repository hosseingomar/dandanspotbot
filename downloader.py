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
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "ffmpeg_location": config.FFMPEG_EXECUTABLE,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": config.AUDIO_BITRATE,
            }
        ],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch",
    }

    logger.info("Starting download for '%s - %s' with query '%s'", artist, title, query)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([query])
    except Exception as e:
        logger.error("yt-dlp error downloading '%s - %s': %s", artist, title, e)
        return None

    if not target_mp3.exists():
        logger.error("Target MP3 file not found after download: %s", target_mp3)
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
