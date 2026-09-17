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
