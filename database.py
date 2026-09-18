import sqlite3
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from contextlib import contextmanager
from config import DATABASE_PATH, DATABASE_URL

logger = logging.getLogger(__name__)

# Check if PostgreSQL is requested
USE_POSTGRES = bool(DATABASE_URL)
if USE_POSTGRES:
    try:
        import psycopg2
        import psycopg2.extras
        logger.info("Using PostgreSQL cloud database.")
    except ImportError:
        logger.warning(
            "psycopg2 is not installed. Falling back to local SQLite database."
        )
        USE_POSTGRES = False


@contextmanager
def get_connection():
    """Context manager that yields a connection with dict-like row access,

    supporting either PostgreSQL or SQLite.
    """
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _prep_sql(query: str) -> str:
    """Replaces '?' with '%s' for PostgreSQL queries."""
    if USE_POSTGRES:
        return query.replace("?", "%s")
    return query


def _get_cursor(conn):
    """Returns a cursor that produces dictionary-compatible rows."""
    if USE_POSTGRES:
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn.cursor()


def init_db() -> None:
    """Initialize database tables if they do not exist."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)

        if USE_POSTGRES:
            # Users table (PostgreSQL)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    spotify_token_json TEXT,
                    monitored_playlist_url TEXT,
                    is_monitoring INTEGER DEFAULT 1,
                    is_initialized INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_sync_at TEXT
                );
                """
            )
            # Processed tracks table (PostgreSQL)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_tracks (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    track_id TEXT NOT NULL,
                    title TEXT,
                    artist TEXT,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, track_id)
                );
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_processed_user_track 
                ON processed_tracks(user_id, track_id);
                """
            )
            logger.info("PostgreSQL database initialized successfully.")
            init_queue_table(conn)
        else:
            # Users table (SQLite)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    spotify_token_json TEXT,
                    monitored_playlist_url TEXT,
                    is_monitoring INTEGER DEFAULT 1,
                    is_initialized INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    last_sync_at TEXT
                )
                """
            )
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN monitored_playlist_url TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Processed tracks table (SQLite)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_tracks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    track_id TEXT NOT NULL,
                    title TEXT,
                    artist TEXT,
                    processed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, track_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_processed_user_track 
                ON processed_tracks(user_id, track_id)
                """
            )
            logger.info("SQLite database initialized successfully at %s", DATABASE_PATH)
            init_queue_table(conn)


def set_user_monitored_playlist(user_id: int, chat_id: int, playlist_url: str) -> None:
    """Register or update a monitored Spotify playlist URL for a user (No-API Free Mode)."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        sql = _prep_sql(
            """
            INSERT INTO users (user_id, chat_id, monitored_playlist_url, is_monitoring, is_initialized)
            VALUES (?, ?, ?, 1, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = EXCLUDED.chat_id,
                monitored_playlist_url = EXCLUDED.monitored_playlist_url,
                is_monitoring = 1,
                is_initialized = 0
            """
        )
        cursor.execute(sql, (user_id, chat_id, playlist_url))


def get_user_monitored_playlist(user_id: int) -> Optional[str]:
    """Get the currently monitored playlist URL for a user."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        sql = _prep_sql("SELECT monitored_playlist_url FROM users WHERE user_id = ?")
        cursor.execute(sql, (user_id,))
        row = cursor.fetchone()
        return row["monitored_playlist_url"] if row else None


def save_user_token(user_id: int, chat_id: int, token_info: Dict[str, Any]) -> None:
    """Save or update a user's Spotify token and chat info."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        token_str = json.dumps(token_info)
        sql = _prep_sql(
            """
            INSERT INTO users (user_id, chat_id, spotify_token_json, is_monitoring)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = EXCLUDED.chat_id,
                spotify_token_json = EXCLUDED.spotify_token_json,
                is_monitoring = 1
            """
        )
        cursor.execute(sql, (user_id, chat_id, token_str))


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve user record by Telegram user_id."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        sql = _prep_sql("SELECT * FROM users WHERE user_id = ?")
        cursor.execute(sql, (user_id,))
        row = cursor.fetchone()
        if row:
            data = dict(row)
            if data.get("spotify_token_json"):
                try:
                    data["token_info"] = json.loads(data["spotify_token_json"])
                except json.JSONDecodeError:
                    data["token_info"] = None
            else:
                data["token_info"] = None
            return data
    return None


def get_all_monitored_users() -> List[Dict[str, Any]]:
    """Retrieve all users who have active monitoring enabled (either via OAuth or Monitored Playlist)."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        cursor.execute(
            """
            SELECT * FROM users 
            WHERE is_monitoring = 1 
              AND (spotify_token_json IS NOT NULL OR monitored_playlist_url IS NOT NULL)
            """
        )
        rows = cursor.fetchall()
        users = []
        for row in rows:
            data = dict(row)
            if data.get("spotify_token_json"):
                try:
                    data["token_info"] = json.loads(data["spotify_token_json"])
                except (json.JSONDecodeError, TypeError):
                    data["token_info"] = None
            else:
                data["token_info"] = None
            users.append(data)
        return users


def update_last_sync(user_id: int) -> None:
    """Update last sync timestamp for a user."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        sql = _prep_sql("UPDATE users SET last_sync_at = ? WHERE user_id = ?")
        cursor.execute(sql, (now_str, user_id))


def set_user_monitoring(user_id: int, active: bool) -> None:
    """Enable or disable monitoring for a user."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        sql = _prep_sql("UPDATE users SET is_monitoring = ? WHERE user_id = ?")
        cursor.execute(sql, (1 if active else 0, user_id))


def set_user_initialized(user_id: int, initialized: bool) -> None:
    """Set the initialization flag (baseline established)."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        sql = _prep_sql("UPDATE users SET is_initialized = ? WHERE user_id = ?")
        cursor.execute(sql, (1 if initialized else 0, user_id))


def is_track_processed(user_id: int, track_id: str) -> bool:
    """Check if a track has already been marked as processed for this user."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        sql = _prep_sql(
            "SELECT 1 FROM processed_tracks WHERE user_id = ? AND track_id = ? LIMIT 1"
        )
        cursor.execute(sql, (user_id, track_id))
        return cursor.fetchone() is not None


def mark_track_processed(user_id: int, track_id: str, title: str = "", artist: str = "") -> None:
    """Mark a single track as processed."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        sql = _prep_sql(
            """
            INSERT INTO processed_tracks (user_id, track_id, title, artist)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (user_id, track_id) DO NOTHING
            """
        )
        cursor.execute(sql, (user_id, track_id, title, artist))


def mark_multiple_tracks_processed(user_id: int, tracks: List[Tuple[str, str, str]]) -> None:
    """Mark multiple tracks as processed in a single transaction."""
    if not tracks:
        return
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        sql = _prep_sql(
            """
            INSERT INTO processed_tracks (user_id, track_id, title, artist)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (user_id, track_id) DO NOTHING
            """
        )
        data = [(user_id, t[0], t[1], t[2]) for t in tracks]
        cursor.executemany(sql, data)


def get_processed_count(user_id: int) -> int:
    """Get the total count of processed tracks for a user."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        sql = _prep_sql("SELECT COUNT(*) AS total FROM processed_tracks WHERE user_id = ?")
        cursor.execute(sql, (user_id,))
        row = cursor.fetchone()
        if row:
            if isinstance(row, dict):
                return row.get("total", 0)
            return row[0]
        return 0


def delete_user_session(user_id: int) -> None:
    """Delete a user's Spotify authorization and monitored playlist."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        sql = _prep_sql(
            "UPDATE users SET spotify_token_json = NULL, monitored_playlist_url = NULL, is_initialized = 0 WHERE user_id = ?"
        )
        cursor.execute(sql, (user_id,))


# =========================================================================
# Persistent download queue (survives redeploys when the DB is persistent)
# =========================================================================

def init_queue_table(conn=None) -> None:
    """Create the download_queue table (called from init_db)."""
    def _create(cursor):
        if USE_POSTGRES:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS download_queue (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    track_id TEXT NOT NULL,
                    title TEXT,
                    artist TEXT,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, track_id)
                );
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_queue_status
                ON download_queue(status, id);
                """
            )
        else:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS download_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    track_id TEXT NOT NULL,
                    title TEXT,
                    artist TEXT,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, track_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_queue_status
                ON download_queue(status, id)
                """
            )

    if conn is not None:
        _create(_get_cursor(conn))
    else:
        with get_connection() as conn2:
            _create(_get_cursor(conn2))


def enqueue_tracks(user_id: int, chat_id: int, tracks: List[Dict[str, Any]]) -> int:
    """Queue tracks for download. Returns new rows.

    Skips tracks already queued, delivered, or failed-and-waiting.
    A previously FAILED job is reset to pending so resending a link retries it.
    """
    if not tracks:
        return 0
    added = 0
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        insert_sql = _prep_sql(
            """
            INSERT INTO download_queue (user_id, chat_id, track_id, title, artist, payload, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            ON CONFLICT (user_id, track_id) DO NOTHING
            """
        )
        retry_sql = _prep_sql(
            "UPDATE download_queue SET status = 'pending', payload = ?, title = ?, artist = ?,"
            " chat_id = ?, attempts = 0, last_error = NULL, updated_at = CURRENT_TIMESTAMP"
            " WHERE user_id = ? AND track_id = ? AND status = 'failed'"
        )
        for t in tracks:
            if not t.get("id"):
                continue
            try:
                cursor.execute(
                    insert_sql,
                    (
                        user_id,
                        chat_id,
                        t["id"],
                        t.get("title", ""),
                        t.get("artist", ""),
                        json.dumps(t),
                    ),
                )
                if cursor.rowcount and cursor.rowcount > 0:
                    added += 1
                else:
                    cursor.execute(
                        retry_sql,
                        (
                            json.dumps(t),
                            t.get("title", ""),
                            t.get("artist", ""),
                            chat_id,
                            user_id,
                            t["id"],
                        ),
                    )
                    if cursor.rowcount and cursor.rowcount > 0:
                        added += 1
            except Exception as e:
                logger.debug("Enqueue skipped for track %s: %s", t.get("id"), e)
    return added


def clear_user_queue(user_id: int) -> int:
    """Remove all queued jobs for a user (used by /clear)."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        sql = _prep_sql("DELETE FROM download_queue WHERE user_id = ? AND status IN ('pending', 'processing', 'failed')")
        cursor.execute(sql, (user_id,))
        return cursor.rowcount or 0


def claim_next_pending() -> Optional[Dict[str, Any]]:
    """Atomically claim the oldest pending job (pending -> processing)."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        sel = _prep_sql(
            "SELECT * FROM download_queue WHERE status = 'pending' ORDER BY id ASC LIMIT 1"
        )
        cursor.execute(sel)
        row = cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        upd = _prep_sql(
            "UPDATE download_queue SET status = 'processing', attempts = attempts + 1,"
            " updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'"
        )
        cursor.execute(upd, (data["id"],))
        if cursor.rowcount == 0:
            return None  # lost the race
        try:
            data["track"] = json.loads(data.get("payload") or "{}")
        except (json.JSONDecodeError, TypeError):
            data["track"] = {}
        data["status"] = "processing"
        return data


def mark_queue_done(queue_id: int) -> None:
    """Mark a queue job delivered."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        sql = _prep_sql(
            "UPDATE download_queue SET status = 'done', updated_at = CURRENT_TIMESTAMP WHERE id = ?"
        )
        cursor.execute(sql, (queue_id,))


def mark_queue_failed(queue_id: int, error: str = "", max_attempts: int = 3) -> str:
    """Record a failed attempt; 'failed' after max_attempts else back to pending."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        sel = _prep_sql("SELECT attempts FROM download_queue WHERE id = ?")
        cursor.execute(sel, (queue_id,))
        row = cursor.fetchone()
        attempts = row["attempts"] if row else max_attempts
        status = "failed" if attempts >= max_attempts else "pending"
        sql = _prep_sql(
            "UPDATE download_queue SET status = ?, last_error = ?,"
            " updated_at = CURRENT_TIMESTAMP WHERE id = ?"
        )
        cursor.execute(sql, (status, (error or "")[:500], queue_id))
        return status


def reset_processing_to_pending() -> int:
    """Crash recovery: jobs stuck in 'processing' (redeploy mid-download) go back to pending."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        sql = _prep_sql(
            "UPDATE download_queue SET status = 'pending', updated_at = CURRENT_TIMESTAMP"
            " WHERE status = 'processing'"
        )
        cursor.execute(sql)
        return cursor.rowcount or 0


def get_queue_counts(user_id: Optional[int] = None) -> Dict[str, int]:
    """Count jobs by status, optionally for one user."""
    with get_connection() as conn:
        cursor = _get_cursor(conn)
        if user_id is not None:
            sql = _prep_sql(
                "SELECT status, COUNT(*) AS total FROM download_queue WHERE user_id = ? GROUP BY status"
            )
            cursor.execute(sql, (user_id,))
        else:
            cursor.execute("SELECT status, COUNT(*) AS total FROM download_queue GROUP BY status")
        counts: Dict[str, int] = {}
        for row in cursor.fetchall():
            d = dict(row)
            counts[d["status"]] = d["total"] if isinstance(d["total"], int) else int(d["total"])
        return counts
