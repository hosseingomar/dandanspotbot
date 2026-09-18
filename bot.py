import os
import asyncio
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict, Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config
import database
import spotify_service
import downloader

logger = logging.getLogger(__name__)

# Serializes queue workers so two triggers (manual + scheduler) never
# download the same job twice. The queue itself lives in the database,
# so it survives redeploys; this lock only guards in-process overlap.
_QUEUE_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class SendAudioResult:
    success: bool
    user_message: str = ""


def is_user_allowed(user_id: int) -> bool:
    """Check if the user is authorized to use the bot."""
    if not config.ALLOWED_TELEGRAM_USER_IDS:
        return True
    return user_id in config.ALLOWED_TELEGRAM_USER_IDS


async def check_access(update: Update) -> bool:
    """Helper to verify access and alert unauthorized users."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.effective_message:
            await update.effective_message.reply_text(
                "⛔ You are not authorized to use this bot.",
                parse_mode=ParseMode.HTML,
            )
        return False
    return True


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    if not await check_access(update):
        return

    user = update.effective_user
    if not user:
        return

    welcome_text = (
        f"👋 <b>Welcome {user.first_name}!</b>\n\n"
        "🎵 <b>Spotify Downloader & Auto-Sync Bot</b>\n\n"
        "This bot works <b>100% FREE without needing Spotify Premium</b> or Developer API keys!\n\n"
        "<b>🔥 How to Auto-Sync Your Liked Songs:</b>\n"
        "1. Open Spotify and create a playlist named <b>Liked</b> (or any name).\n"
        "2. Copy the playlist link (Share ➔ Copy link).\n"
        "3. Send it to me using: <code>/setplaylist YOUR_PLAYLIST_LINK</code>\n"
        "4. <b>That's it!</b> Whenever you add a song to that playlist, I'll automatically download and send it to you here in 320kbps MP3 with album artwork! 🎶\n\n"
        "<b>📥 On-Demand Downloads:</b>\n"
        "Simply send me <b>any Spotify link</b> (track, playlist, or album) and I'll download it for you immediately!\n\n"
        "Use /help to see all commands."
    )
    await update.effective_message.reply_text(welcome_text, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    if not await check_access(update):
        return

    help_text = (
        "📖 <b>Bot Commands & Guide</b>\n\n"
        "<b>Auto-Sync Commands:</b>\n"
        "• <code>/setplaylist &lt;link&gt;</code> - Monitor a Spotify playlist for new songs (No Premium/API keys required!)\n"
        "• /status - View monitoring status, playlist, and synced songs\n"
        "• /sync - Manually check for new songs right now\n"
        "• /toggle - Pause or resume automatic monitoring\n"
        "• /clear - Stop monitoring your current playlist\n\n"
        "<b>Direct Downloading:</b>\n"
        "Simply paste any Spotify link directly into this chat:\n"
        "• Track: <code>https://open.spotify.com/track/...</code>\n"
        "• Playlist: <code>https://open.spotify.com/playlist/...</code>\n"
        "• Album: <code>https://open.spotify.com/album/...</code>\n\n"
        "<b>Spotify Developer Mode (Optional):</b>\n"
        "• /login - Link via Spotify Developer OAuth (only for users with developer keys)\n"
        "• /logout - Clear developer authorization"
    )
    await update.effective_message.reply_text(help_text, parse_mode=ParseMode.HTML)


async def set_playlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /setplaylist <url> to monitor a playlist without needing Spotify Developer API keys."""
    if not await check_access(update):
        return

    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    args = context.args
    if not args:
        await update.effective_message.reply_text(
            "⚠️ <b>Please provide your Spotify playlist link!</b>\n\n"
            "Example:\n<code>/setplaylist https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    playlist_url = args[0].strip()
    parsed = spotify_service.parse_spotify_url(playlist_url)
    if not parsed or parsed[0] != "playlist":
        await update.effective_message.reply_text(
            "❌ <b>Invalid playlist URL!</b> Please send a valid Spotify playlist link.",
            parse_mode=ParseMode.HTML,
        )
        return

    status_msg = await update.effective_message.reply_text(
        "🔍 <i>Connecting to your playlist and setting up baseline...</i>",
        parse_mode=ParseMode.HTML,
    )

    playlist_name, tracks = spotify_service.extract_playlist_no_auth(parsed[1])
    if not tracks and playlist_name == "Spotify Playlist":
        await status_msg.edit_text(
            "⚠️ Could not load playlist. Please make sure the playlist is public/accessible via link."
        )
        return

    # Save user playlist in database
    database.set_user_monitored_playlist(user.id, chat.id, playlist_url)

    # Establish baseline so existing songs aren't spammed
    baseline_tuples = [(t["id"], t["title"], t["artist"]) for t in tracks]
    database.mark_multiple_tracks_processed(user.id, baseline_tuples)
    database.set_user_initialized(user.id, True)
    database.update_last_sync(user.id)

    await status_msg.edit_text(
        f"✅ <b>Successfully monitoring:</b> <i>{playlist_name}</i>\n\n"
        f"🎧 Indexed <b>{len(baseline_tuples)}</b> existing songs as baseline.\n\n"
        "From now on, whenever you add a new song to this playlist in Spotify, "
        "I will automatically download it in 320kbps and send it to you here! 🚀",
        parse_mode=ParseMode.HTML,
    )


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear monitored playlist or session."""
    if not await check_access(update):
        return

    user = update.effective_user
    if not user:
        return

    database.delete_user_session(user.id)
    cleared = database.clear_user_queue(user.id)
    queue_note = f" Cleared {cleared} queued download(s)." if cleared else ""
    await update.effective_message.reply_text(
        "🗑️ <b>Monitoring stopped.</b> Monitored playlist and session data cleared."
        f"{queue_note}\n"
        "Use <code>/setplaylist &lt;link&gt;</code> to set a new playlist.",
        parse_mode=ParseMode.HTML,
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    if not await check_access(update):
        return

    user = update.effective_user
    if not user:
        return

    user_data = database.get_user(user.id)
    if not user_data or (not user_data.get("monitored_playlist_url") and not user_data.get("token_info")):
        await update.effective_message.reply_text(
            "ℹ️ <b>No playlist currently monitored.</b>\n\n"
            "Use <code>/setplaylist &lt;spotify_playlist_link&gt;</code> to start monitoring a playlist!",
            parse_mode=ParseMode.HTML,
        )
        return

    monitoring_status = "🟢 Active" if user_data.get("is_monitoring") else "🔴 Paused"
    processed_count = database.get_processed_count(user.id)
    queue_counts = database.get_queue_counts(user.id)
    pending = queue_counts.get("pending", 0) + queue_counts.get("processing", 0)
    failed = queue_counts.get("failed", 0)
    last_sync = user_data.get("last_sync_at") or "Never"
    playlist_url = user_data.get("monitored_playlist_url")

    source_info = f"<code>{playlist_url}</code>" if playlist_url else "Spotify OAuth (Liked Songs)"

    status_msg = (
        "📊 <b>Bot Monitoring Status</b>\n\n"
        f"• <b>Monitored Target:</b> {source_info}\n"
        f"• <b>Auto-Sync:</b> {monitoring_status}\n"
        f"• <b>Check Frequency:</b> Every {config.CHECK_INTERVAL_SECONDS} seconds\n"
        f"• <b>Processed Songs:</b> {processed_count} tracks\n"
        f"• <b>Queue:</b> {pending} pending, {failed} failed\n"
        f"• <b>Last Check:</b> {last_sync} UTC\n\n"
        "<i>Use /toggle to pause/resume auto-sync or /sync to check now.</i>"
    )
    await update.effective_message.reply_text(status_msg, parse_mode=ParseMode.HTML)


async def toggle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle auto-sync on/off."""
    if not await check_access(update):
        return

    user = update.effective_user
    if not user:
        return

    user_data = database.get_user(user.id)
    if not user_data:
        await update.effective_message.reply_text(
            "Use <code>/setplaylist &lt;link&gt;</code> first to set up monitoring.",
            parse_mode=ParseMode.HTML,
        )
        return

    current = bool(user_data.get("is_monitoring", 1))
    new_state = not current
    database.set_user_monitoring(user.id, new_state)

    state_text = "🟢 <b>Auto-sync resumed.</b> New songs will be sent automatically." if new_state else "🔴 <b>Auto-sync paused.</b>"
    await update.effective_message.reply_text(state_text, parse_mode=ParseMode.HTML)


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger manual check right now."""
    if not await check_access(update):
        return

    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    user_data = database.get_user(user.id)
    if not user_data or (not user_data.get("monitored_playlist_url") and not user_data.get("token_info")):
        await update.effective_message.reply_text(
            "Use <code>/setplaylist &lt;link&gt;</code> first to configure your playlist.",
            parse_mode=ParseMode.HTML,
        )
        return

    status_msg = await update.effective_message.reply_text(
        "🔄 <i>Checking for new songs right now...</i>",
        parse_mode=ParseMode.HTML,
    )

    new_count = await check_and_sync_user(context.application, user.id, chat.id, user_data)
    if new_count > 0:
        await status_msg.edit_text(
            f"✅ <b>Found {new_count} new song(s)!</b> Queued for download — I'll send each here as it's ready.",
            parse_mode=ParseMode.HTML,
        )
    else:
        counts = database.get_queue_counts(user.id)
        pending = counts.get("pending", 0) + counts.get("processing", 0)
        if pending:
            await status_msg.edit_text(
                f"⏳ <b>No new songs, but {pending} still queued.</b> Continuing downloads...",
                parse_mode=ParseMode.HTML,
            )
            kick_queue_worker(context.application)
        else:
            await status_msg.edit_text(
                "✅ <b>All caught up!</b> No new songs found.",
                parse_mode=ParseMode.HTML,
            )


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Optional OAuth login for users who have Spotify Developer credentials."""
    if not await check_access(update):
        return

    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if not config.has_spotify_api_keys():
        await update.effective_message.reply_text(
            "💡 <b>You do not need to /login!</b>\n\n"
            "This bot works without Spotify API keys.\n"
            "Simply create a playlist in Spotify (e.g. named 'Liked') and run:\n"
            "<code>/setplaylist YOUR_PLAYLIST_LINK</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        auth_url = spotify_service.get_auth_url(user.id, chat.id)
        if not auth_url:
            await update.effective_message.reply_text("Could not generate authorization URL.")
            return
        login_text = (
            "🔐 <b>Spotify Developer OAuth:</b>\n\n"
            f"1. <a href=\"{auth_url}\"><b>Click here to Authorize</b></a>\n"
            "2. Copy the redirect URL and paste it here."
        )
        await update.effective_message.reply_text(login_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        await update.effective_message.reply_text(f"Error: {e}")


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logout / clear session."""
    await clear_command(update, context)


async def send_audio_track(
    application: Application,
    chat_id: int,
    track_info: Dict[str, Any],
    status_message: Optional[Any] = None,
) -> SendAudioResult:
    """Download and dispatch an audio file to Telegram chat with rich metadata, generous upload timeout, and retry."""
    # Playlist extractor (no-auth) initially stamps every track with the
    # playlist mosaic. Resolve the track's own artwork before download so
    # both the Telegram thumbnail and the embedded MP3 cover are correct.
    try:
        track_info = spotify_service.enrich_track_cover(track_info)
    except Exception as enrich_err:
        logger.debug("Cover enrichment skipped: %s", enrich_err)

    # Attach a real genre (Spotify artist data, else iTunes metadata).
    try:
        track_info = spotify_service.enrich_track_genre(track_info)
    except Exception as genre_err:
        logger.debug("Genre enrichment skipped: %s", genre_err)

    title = track_info.get("title", "Unknown Title")
    artist = track_info.get("artist", "Unknown Artist")
    cover_url = track_info.get("cover_url")
    duration_sec = track_info.get("duration_sec", 0)

    thumb_path: Optional[Path] = None
    mp3_file: Optional[Path] = None

    try:
        mp3_file = await downloader.download_track_async(track_info)
        if not mp3_file or not mp3_file.exists():
            logger.error("Download failed for track: %s - %s", artist, title)
            return SendAudioResult(
                False,
                "❌ Could not download this audio from YouTube. The hosting service may be blocking the server; please try again later.",
            )

        if status_message:
            try:
                await status_message.edit_text(
                    f"📤 <i>Uploading to Telegram:</i> <b>{artist} - {title}</b>...",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        if cover_url:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
                thumb_target = Path(tf.name)
            thumb_path = downloader.download_cover_thumbnail(cover_url, thumb_target)

        caption = f"🎵 <b>{title}</b>\n👤 <i>{artist}</i>"
        # Third line: real genre when known, album name as fallback.
        if track_info.get("genre"):
            caption += f"\n🎧 <i>{track_info['genre']}</i>"
        elif track_info.get("album"):
            caption += f"\n💿 <i>{track_info['album']}</i>"

        # Attempt upload with retry
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                with open(mp3_file, "rb") as audio_fh:
                    thumb_fh = open(thumb_path, "rb") if thumb_path and thumb_path.exists() else None
                    try:
                        await application.bot.send_audio(
                            chat_id=chat_id,
                            audio=audio_fh,
                            title=title,
                            performer=artist,
                            duration=duration_sec,
                            thumbnail=thumb_fh,
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                            write_timeout=float(config.UPLOAD_TIMEOUT_SECONDS),
                            read_timeout=float(config.UPLOAD_TIMEOUT_SECONDS),
                            connect_timeout=60.0,
                        )
                    finally:
                        if thumb_fh:
                            thumb_fh.close()

                logger.info("Successfully sent '%s - %s' to chat %d", artist, title, chat_id)
                return SendAudioResult(True)

            except Exception as upload_err:
                logger.warning(
                    "Upload attempt %d/%d failed for '%s - %s': %s",
                    attempt, max_attempts, artist, title, upload_err
                )
                if attempt < max_attempts:
                    await asyncio.sleep(2.0)
                else:
                    raise upload_err

    except Exception as e:
        logger.exception("Failed to send audio '%s - %s'", artist, title)
        return SendAudioResult(
            False,
            "❌ Download completed, but Telegram could not receive the audio. Please try again later.",
        )
    finally:
        if mp3_file and mp3_file.exists():
            try:
                mp3_file.unlink()
            except Exception:
                pass
        if thumb_path and thumb_path.exists():
            try:
                thumb_path.unlink()
            except Exception:
                pass


async def process_download_queue(application: Application) -> int:
    """Drain the persistent download queue in FIFO order.

    Claims jobs one by one, downloads + sends each, marks done/failed.
    Safe to call from handlers and the scheduler; concurrent calls are
    serialized. Survives redeploys: unclaimed jobs stay pending, jobs
    stuck in 'processing' are reset to pending on startup.
    Returns the number of jobs completed (successfully) this run.
    """
    delivered = 0
    async with _QUEUE_LOCK:
        while True:
            job = await asyncio.to_thread(database.claim_next_pending)
            if not job:
                break
            track = job.get("track") or {}
            chat_id = job.get("chat_id")
            user_id = job.get("user_id")
            title = track.get("title", "Unknown Title")
            artist = track.get("artist", "Unknown Artist")

            progress_msg = None
            try:
                progress_msg = await application.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏳ Downloading: <b>{artist} - {title}</b>...",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                progress_msg = None

            result = await send_audio_track(
                application, chat_id, track, status_message=progress_msg
            )
            if result.success:
                delivered += 1
                await asyncio.to_thread(database.mark_queue_done, job["id"])
                await asyncio.to_thread(
                    database.mark_track_processed,
                    user_id,
                    track.get("id", ""),
                    title,
                    artist,
                )
                if progress_msg:
                    try:
                        await progress_msg.delete()
                    except Exception:
                        pass
            else:
                await asyncio.to_thread(
                    database.mark_queue_failed, job["id"], result.user_message
                )
                if progress_msg:
                    try:
                        await progress_msg.edit_text(
                            f"⚠️ Could not download <b>{title}</b> - <i>{artist}</i>.\n{result.user_message}",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
                else:
                    try:
                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=f"⚠️ Could not download <b>{title}</b> - <i>{artist}</i>.\n{result.user_message}",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass

            await asyncio.sleep(1.0)
    return delivered


def kick_queue_worker(application: Application) -> None:
    """Fire-and-forget queue drain (never blocks a Telegram handler)."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(process_download_queue(application))
    except RuntimeError:
        # No running loop (should not happen inside handlers); run inline.
        logger.debug("No running loop for queue worker; skipping kick.")


async def check_and_sync_user(
    application: Application,
    user_id: int,
    chat_id: int,
    user_data: Dict[str, Any],
) -> int:
    """Detect new songs for a user and queue them for download.

    Detection only enqueues; the queue worker delivers. Returns the number
    of newly queued tracks.
    """
    playlist_url = user_data.get("monitored_playlist_url")
    token_info = user_data.get("token_info")

    tracks: List[Dict[str, Any]] = []

    # 1. Monitored Playlist (No-API Free Mode)
    if playlist_url:
        parsed = spotify_service.parse_spotify_url(playlist_url)
        if parsed and parsed[0] == "playlist":
            _, tracks = spotify_service.extract_playlist_no_auth(parsed[1])

    # 2. Spotify OAuth Mode (if user used /login with Spotify Developer app)
    elif token_info:
        sp = spotify_service.get_spotify_client(user_id, chat_id)
        if sp:
            tracks = spotify_service.get_recent_liked_songs(sp, limit=20)

    if not tracks:
        return 0

    is_initialized = bool(user_data.get("is_initialized", 0))
    if not is_initialized:
        # Establish baseline
        baseline_tuples = [(t["id"], t["title"], t["artist"]) for t in tracks]
        database.mark_multiple_tracks_processed(user_id, baseline_tuples)
        database.set_user_initialized(user_id, True)
        database.update_last_sync(user_id)
        return 0

    # Normal polling check: queue tracks not yet delivered.
    # Delivered = present in processed_tracks (marked when a queue job
    # completes) or already sitting in the queue (pending/processing/done).
    new_tracks = [
        t for t in tracks if not database.is_track_processed(user_id, t["id"])
    ]

    if not new_tracks:
        database.update_last_sync(user_id)
        return 0

    queued = await asyncio.to_thread(
        database.enqueue_tracks, user_id, chat_id, new_tracks
    )
    database.update_last_sync(user_id)
    if queued:
        kick_queue_worker(application)
    return queued


async def background_liked_songs_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled background job: queue new songs, then drain the download queue."""
    users = database.get_all_monitored_users()
    for user_data in users:
        user_id = user_data["user_id"]
        chat_id = user_data["chat_id"]
        try:
            await check_and_sync_user(
                context.application, user_id, chat_id, user_data
            )
        except Exception as e:
            logger.error("Error in background sync job for user %d: %s", user_id, e)
    # Drain any backlog (including jobs restored after a redeploy).
    try:
        await process_download_queue(context.application)
    except Exception as e:
        logger.error("Error draining download queue: %s", e)


async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages: Spotify links or OAuth codes."""
    if not await check_access(update):
        return

    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not message.text or not user or not chat:
        return

    text = message.text.strip()

    # 1. Check if user sent an OAuth callback URL
    if "code=" in text:
        status_msg = await message.reply_text("⏳ <i>Verifying Spotify authorization...</i>", parse_mode=ParseMode.HTML)
        success = spotify_service.complete_auth(user.id, chat.id, text)
        if success:
            await status_msg.edit_text("✅ <b>Spotify authorization successful!</b>", parse_mode=ParseMode.HTML)
        else:
            await status_msg.edit_text("❌ <b>Authorization failed.</b>", parse_mode=ParseMode.HTML)
        return

    # 2. Check if user sent a Spotify URL (Track, Playlist, Album)
    parsed_spotify = spotify_service.parse_spotify_url(text)
    if parsed_spotify:
        item_type, item_id = parsed_spotify
        await handle_spotify_download(update, context, item_type, item_id)
        return

    # Fallback
    await message.reply_text(
        "💡 <b>Send me any Spotify link</b> (track, playlist, or album) to download it,\n"
        "or use <code>/setplaylist &lt;link&gt;</code> to automatically sync new songs!",
        parse_mode=ParseMode.HTML,
    )


async def handle_spotify_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    item_type: str,
    item_id: str,
) -> None:
    """Process on-demand download for Spotify Track, Playlist, or Album."""
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    user = update.effective_user
    user_id = user.id if user else None
    if not user_id:
        await status_msg.edit_text("❌ Could not identify you. Please try again.")
        return

    # Handle Single Track
    if item_type == "track":
        status_msg = await message.reply_text(
            "🔍 <i>Fetching track details...</i>",
            parse_mode=ParseMode.HTML,
        )
        track_info = spotify_service.get_track_by_id(None, item_id)
        if not track_info:
            await status_msg.edit_text("❌ Track not found or could not be loaded.")
            return

        database.enqueue_tracks(user_id, chat.id, [track_info])
        counts = database.get_queue_counts(user_id)
        await status_msg.edit_text(
            f"⏳ Queued: <b>{track_info['artist']} - {track_info['title']}</b>\n"
            f"📥 <i>{counts.get('pending', 1)} song(s) ahead in your queue — I'll send each here as it's ready.</i>",
            parse_mode=ParseMode.HTML,
        )
        kick_queue_worker(context.application)
        return

    # Handle Playlist or Album
    if item_type in ("playlist", "album"):
        status_msg = await message.reply_text(
            f"🔍 <i>Fetching {item_type} info...</i>",
            parse_mode=ParseMode.HTML,
        )

        if item_type == "playlist":
            collection_name, tracks = spotify_service.get_playlist_tracks(None, item_id)
        else:
            collection_name, tracks = spotify_service.get_album_tracks(None, item_id)

        if not tracks:
            await status_msg.edit_text(
                f"❌ No tracks found in this {item_type}.",
                parse_mode=ParseMode.HTML,
            )
            return

        # --- Playlists: first paste queues everything + enables auto-sync,
        # --- next pastes only queue what's new. Delivery resumes from the
        # --- persistent queue after redeploys (requires DATABASE_URL).
        if item_type == "playlist":
            canonical_url = f"https://open.spotify.com/playlist/{item_id}"
            existing = database.get_user(user_id)
            existing_url = (existing.get("monitored_playlist_url") if existing else None) or ""
            same_playlist = False
            if existing_url:
                parsed_existing = spotify_service.parse_spotify_url(existing_url)
                if parsed_existing and parsed_existing[1] == item_id:
                    same_playlist = True

            if same_playlist and existing.get("is_initialized"):
                new_tracks = [
                    t for t in tracks
                    if not database.is_track_processed(user_id, t["id"])
                ]
                if not new_tracks:
                    database.update_last_sync(user_id)
                    await status_msg.edit_text(
                        f"✅ <b>Already up to date!</b> <i>{collection_name}</i> has no new songs.\n"
                        "Auto-sync is still ON - I'll send new adds automatically. 🚀",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                tracks = new_tracks
                await status_msg.edit_text(
                    f"📋 <b>Found {len(tracks)} new track(s)</b> in <i>{collection_name}</i>.\n"
                    "Queued — sending each here as it's ready...",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await status_msg.edit_text(
                    f"📋 <b>Found {len(tracks)} tracks</b> in <i>{collection_name}</i>.\n"
                    "Queued — first time: I'll work through all of them in order, then auto-sync ON for future adds...",
                    parse_mode=ParseMode.HTML,
                )

            database.set_user_monitored_playlist(user_id, chat.id, canonical_url)
            database.set_user_initialized(user_id, True)
            database.set_user_monitoring(user_id, True)
            database.update_last_sync(user_id)
            auto_sync_note = (
                "\n\n🔔 <b>Auto-sync ON:</b> just add songs to this playlist in Spotify "
                "and I'll send them here automatically. No need to resend the link."
            )
        else:
            total_tracks = len(tracks)
            await status_msg.edit_text(
                f"📋 <b>Found {total_tracks} tracks</b> in <i>{collection_name}</i>.\n"
                "Queued — sending each here as it's ready...",
                parse_mode=ParseMode.HTML,
            )
            auto_sync_note = ""

        queued = database.enqueue_tracks(user_id, chat.id, tracks)
        counts = database.get_queue_counts(user_id)
        pending = counts.get("pending", 0)
        await status_msg.edit_text(
            f"📥 Queued <b>{queued} new</b> track(s) from <i>{collection_name}</i>"
            f" ({pending} pending in your queue).{auto_sync_note}",
            parse_mode=ParseMode.HTML,
        )
        kick_queue_worker(context.application)


def build_application() -> Application:
    """Build and configure the python-telegram-bot Application with generous timeouts and proxy support."""
    request = HTTPXRequest(
        connection_pool_size=10,
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        media_write_timeout=float(config.UPLOAD_TIMEOUT_SECONDS),
        pool_timeout=60.0,
        proxy=config.TELEGRAM_PROXY if config.TELEGRAM_PROXY else None,
    )

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .request(request)
        .build()
    )

    # Command Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("setplaylist", set_playlist_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("toggle", toggle_command))
    app.add_handler(CommandHandler("sync", sync_command))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("logout", logout_command))

    # General Text Handler (Spotify links)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))

    # Register Background Polling Job
    if app.job_queue:
        app.job_queue.run_repeating(
            background_liked_songs_job,
            interval=config.CHECK_INTERVAL_SECONDS,
            first=10,
            name="liked_songs_sync_job",
        )
        logger.info("Registered background sync job (interval: %ds)", config.CHECK_INTERVAL_SECONDS)

    return app
