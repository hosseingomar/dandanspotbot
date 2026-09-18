import re
import json
import logging
from typing import Optional, Dict, Any, List, Tuple
import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheHandler
import config
import database

logger = logging.getLogger(__name__)

SPOTIFY_SCOPES = [
    "user-library-read",
    "playlist-read-private",
    "playlist-read-collaborative",
]

SPOTIFY_URL_REGEX = re.compile(
    r"(?:https?://open\.spotify\.com/(track|playlist|album)/|spotify:(track|playlist|album):)([a-zA-Z0-9]+)",
    re.IGNORECASE,
)

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


# =========================================================================
# 1. Zero-Credential Extractors (100% Free - Works without Developer Keys)
# =========================================================================

def extract_track_no_auth(track_id_or_url: str) -> Optional[Dict[str, Any]]:
    """Extract full track metadata directly from Spotify's public embed page without credentials."""
    parsed = parse_spotify_url(track_id_or_url)
    track_id = parsed[1] if parsed else track_id_or_url.strip()

    embed_url = f"https://open.spotify.com/embed/track/{track_id}"
    try:
        resp = requests.get(embed_url, headers=BROWSER_HEADERS, timeout=10)
        if resp.status_code != 200:
            logger.warning("Embed fetch returned HTTP %d for track %s", resp.status_code, track_id)
            return None

        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text)
        if match:
            data = json.loads(match.group(1))
            props = data.get("props", {}).get("pageProps", {})
            entity = props.get("state", {}).get("data", {}).get("entity", {})
            if entity:
                title = entity.get("title") or entity.get("name", "Unknown Title")
                artists_list = entity.get("artists", [])
                if artists_list:
                    artist = ", ".join(a.get("name", "Unknown") for a in artists_list)
                else:
                    artist = entity.get("subtitle", "Unknown Artist")

                # Cover artwork
                cover_url = None
                visual = entity.get("visualIdentity", {})
                images = visual.get("image", [])
                if images:
                    # Pick highest resolution
                    cover_url = images[-1].get("url")

                duration_ms = entity.get("duration", 0)
                release_raw = entity.get("releaseDate", "")
                if isinstance(release_raw, dict):
                    release_raw = release_raw.get("isoString", "")
                year = str(release_raw)[:4] if release_raw else ""

                return {
                    "id": track_id,
                    "title": title,
                    "artist": artist,
                    "album": entity.get("album", {}).get("name", ""),
                    "year": year,
                    "duration_ms": duration_ms,
                    "duration_sec": int(duration_ms / 1000),
                    "cover_url": cover_url,
                    "spotify_url": f"https://open.spotify.com/track/{track_id}",
                    "track_number": 1,
                }

        # Fallback to oEmbed if embed NEXT_DATA is not found
        oembed_url = f"https://open.spotify.com/oembed?url=https://open.spotify.com/track/{track_id}"
        oembed_resp = requests.get(oembed_url, headers=BROWSER_HEADERS, timeout=10)
        if oembed_resp.status_code == 200:
            oe = oembed_resp.json()
            title = oe.get("title", "Unknown Title")
            return {
                "id": track_id,
                "title": title,
                "artist": "Unknown Artist",
                "album": "",
                "year": "",
                "duration_ms": 0,
                "duration_sec": 0,
                "cover_url": oe.get("thumbnail_url"),
                "spotify_url": f"https://open.spotify.com/track/{track_id}",
                "track_number": 1,
            }

    except Exception as e:
        logger.error("Error extracting public track %s: %s", track_id, e)

    return None


def extract_playlist_no_auth(playlist_id_or_url: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract playlist name and all track items directly without credentials."""
    parsed = parse_spotify_url(playlist_id_or_url)
    playlist_id = parsed[1] if parsed else playlist_id_or_url.strip()

    embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    try:
        resp = requests.get(embed_url, headers=BROWSER_HEADERS, timeout=10)
        if resp.status_code != 200:
            logger.warning("Embed fetch returned HTTP %d for playlist %s", resp.status_code, playlist_id)
            return "Spotify Playlist", []

        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text)
        if not match:
            return "Spotify Playlist", []

        data = json.loads(match.group(1))
        props = data.get("props", {}).get("pageProps", {})
        entity = props.get("state", {}).get("data", {}).get("entity", {})
        
        playlist_name = entity.get("name") or entity.get("title", "Spotify Playlist")
        playlist_cover = None
        cover_art = entity.get("coverArt", {})
        sources = cover_art.get("sources", [])
        if sources:
            playlist_cover = sources[0].get("url")
        else:
            visual = entity.get("visualIdentity", {})
            images = visual.get("image", [])
            if images:
                playlist_cover = images[-1].get("url")

        tracks = []
        for raw_track in entity.get("trackList", []):
            uri = raw_track.get("uri", "")
            t_id = uri.split(":")[-1] if uri else ""
            if not t_id:
                continue

            dur_ms = raw_track.get("duration", 0)
            tracks.append({
                "id": t_id,
                "title": raw_track.get("title", "Unknown Title"),
                "artist": raw_track.get("subtitle", "Unknown Artist"),
                # Playlist embed trackList has no per-track artwork, so this
                # is the playlist mosaic (4-tile). Resolved per-track later
                # via enrich_track_cover() before download/upload.
                "album": playlist_name,
                "year": "",
                "duration_ms": dur_ms,
                "duration_sec": int(dur_ms / 1000),
                "cover_url": playlist_cover,
                "cover_is_playlist": True,
                "playlist_cover": playlist_cover,
                "spotify_url": f"https://open.spotify.com/track/{t_id}",
                "track_number": len(tracks) + 1,
            })

        return playlist_name, tracks

    except Exception as e:
        logger.error("Error extracting public playlist %s: %s", playlist_id, e)
        return "Spotify Playlist", []


def extract_album_no_auth(album_id_or_url: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract album name and tracks without credentials."""
    parsed = parse_spotify_url(album_id_or_url)
    album_id = parsed[1] if parsed else album_id_or_url.strip()

    embed_url = f"https://open.spotify.com/embed/album/{album_id}"
    try:
        resp = requests.get(embed_url, headers=BROWSER_HEADERS, timeout=10)
        if resp.status_code == 200:
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text)
            if match:
                data = json.loads(match.group(1))
                props = data.get("props", {}).get("pageProps", {})
                entity = props.get("state", {}).get("data", {}).get("entity", {})
                album_name = entity.get("name") or entity.get("title", "Spotify Album")
                
                # Cover
                cover_url = None
                visual = entity.get("visualIdentity", {})
                images = visual.get("image", [])
                if images:
                    cover_url = images[-1].get("url")

                tracks = []
                for idx, raw_track in enumerate(entity.get("trackList", []), start=1):
                    uri = raw_track.get("uri", "")
                    t_id = uri.split(":")[-1] if uri else ""
                    dur_ms = raw_track.get("duration", 0)
                    tracks.append({
                        "id": t_id,
                        "title": raw_track.get("title", "Unknown Title"),
                        "artist": raw_track.get("subtitle", "Unknown Artist"),
                        "album": album_name,
                        "year": "",
                        "duration_ms": dur_ms,
                        "duration_sec": int(dur_ms / 1000),
                        "cover_url": cover_url,
                        "spotify_url": f"https://open.spotify.com/track/{t_id}",
                        "track_number": idx,
                    })
                return album_name, tracks
    except Exception as e:
        logger.error("Error extracting public album %s: %s", album_id, e)

    return "Spotify Album", []


# =========================================================================
# 2. Spotify OAuth2 Handlers (Used if Developer Credentials are provided)
# =========================================================================

class DBCacheHandler(CacheHandler):
    """Custom Spotipy CacheHandler that persists token info in the SQLite database."""

    def __init__(self, user_id: int, chat_id: int):
        self.user_id = user_id
        self.chat_id = chat_id

    def get_cached_token(self) -> Optional[Dict[str, Any]]:
        user = database.get_user(self.user_id)
        if user and user.get("token_info"):
            return user["token_info"]
        return None

    def save_token_to_cache(self, token_info: Dict[str, Any]) -> None:
        database.save_user_token(self.user_id, self.chat_id, token_info)


def get_oauth_manager(user_id: int, chat_id: int) -> Optional[SpotifyOAuth]:
    """Create a SpotifyOAuth instance if Spotify API credentials exist."""
    if not config.has_spotify_api_keys():
        return None
    cache_handler = DBCacheHandler(user_id, chat_id)
    return SpotifyOAuth(
        client_id=config.SPOTIPY_CLIENT_ID,
        client_secret=config.SPOTIPY_CLIENT_SECRET,
        redirect_uri=config.SPOTIPY_REDIRECT_URI,
        scope=" ".join(SPOTIFY_SCOPES),
        cache_handler=cache_handler,
        show_dialog=True,
        open_browser=False,
    )


def get_auth_url(user_id: int, chat_id: int) -> Optional[str]:
    """Generate Spotify authorization URL for the user."""
    sp_oauth = get_oauth_manager(user_id, chat_id)
    if sp_oauth:
        return sp_oauth.get_authorize_url()
    return None


def complete_auth(user_id: int, chat_id: int, code_or_url: str) -> bool:
    """Exchange authorization code or redirected callback URL for tokens."""
    sp_oauth = get_oauth_manager(user_id, chat_id)
    if not sp_oauth:
        return False
    try:
        code_or_url = code_or_url.strip()
        if "code=" in code_or_url:
            code = sp_oauth.parse_response_code(code_or_url)
        else:
            code = code_or_url

        token_info = sp_oauth.get_access_token(code, as_dict=True, check_cache=False)
        return bool(token_info)
    except Exception as e:
        logger.error("Failed to complete Spotify authorization for user %d: %s", user_id, e)
        return False


def get_spotify_client(user_id: int, chat_id: int) -> Optional[spotipy.Spotify]:
    """Retrieve an authenticated Spotipy client for a user."""
    sp_oauth = get_oauth_manager(user_id, chat_id)
    if not sp_oauth:
        return None
    token_info = sp_oauth.validate_token(sp_oauth.cache_handler.get_cached_token())
    if not token_info:
        return None
    return spotipy.Spotify(auth=token_info["access_token"])


def fetch_track_cover_oembed(track_id: str) -> Optional[str]:
    """Lightweight per-track artwork lookup (single small JSON request)."""
    try:
        oembed_url = f"https://open.spotify.com/oembed?url=https://open.spotify.com/track/{track_id}"
        resp = requests.get(oembed_url, headers=BROWSER_HEADERS, timeout=10)
        if resp.status_code == 200:
            thumb = resp.json().get("thumbnail_url")
            if thumb:
                return thumb
    except Exception as e:
        logger.debug("oEmbed cover fetch failed for track %s: %s", track_id, e)
    return None


def fetch_track_cover_itunes(artist: str, title: str) -> Optional[str]:
    """Per-track artwork via Apple's free search API (no key needed).

    Returns a 600px artwork URL or None. Used when Spotify's own
    endpoints don't return per-track art.
    """
    try:
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={"term": f"{artist} {title}", "media": "music", "entity": "song", "limit": 5},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        for item in resp.json().get("results", []):
            art = item.get("artworkUrl100")
            if art:
                return art.replace("100x100bb.jpg", "600x600bb.jpg")
    except Exception as e:
        logger.debug("iTunes cover fetch failed for %s - %s: %s", artist, title, e)
    return None


def enrich_track_cover(track: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
    """Replace a playlist-mosaic cover with the track's own album artwork.

    Only acts when the track came from the no-auth playlist extractor
    (cover_is_playlist) unless force=True. Falls back to the full embed
    extractor when oEmbed has no thumbnail. Never raises; keeps the
    original cover on failure.
    """
    if not track or not track.get("id"):
        return track
    if not force and not track.get("cover_is_playlist"):
        return track

    track_id = track["id"]
    try:
        cover = fetch_track_cover_oembed(track_id)
        if cover:
            track["cover_url"] = cover
            track.pop("cover_is_playlist", None)
            return track
        # Fallback: full track page has title/artist/album/year + art.
        full = extract_track_no_auth(track_id)
        if full and full.get("cover_url"):
            track["cover_url"] = full["cover_url"]
            track.pop("cover_is_playlist", None)
            # Fill in real album/year when playlist extractor left them blank.
            if full.get("album"):
                track["album"] = full["album"]
            if full.get("year") and not track.get("year"):
                track["year"] = full["year"]
            return track
        # Last resort: iTunes Search API (free, no key) by artist/title.
        itunes_cover = fetch_track_cover_itunes(track.get("artist", ""), track.get("title", ""))
        if itunes_cover:
            track["cover_url"] = itunes_cover
            track.pop("cover_is_playlist", None)
    except Exception as e:
        logger.debug("Cover enrichment failed for track %s: %s", track_id, e)
    return track


def parse_spotify_url(url: str) -> Optional[Tuple[str, str]]:
    """Extract item type ('track', 'playlist', 'album') and ID from Spotify URL or URI."""
    match = SPOTIFY_URL_REGEX.search(url)
    if match:
        item_type = match.group(1) or match.group(2)
        item_id = match.group(3)
        return item_type.lower(), item_id
    return None


def parse_track_item(track: Dict[str, Any], added_at: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Format raw Spotify track JSON into a structured dictionary."""
    if not track or not track.get("id"):
        return None
    
    artists = ", ".join(a.get("name", "Unknown") for a in track.get("artists", []))
    album_data = track.get("album", {})
    album_name = album_data.get("name", "")
    release_date = album_data.get("release_date", "")
    year = release_date[:4] if release_date else ""
    
    images = album_data.get("images", [])
    cover_url = images[0]["url"] if images else None
    
    return {
        "id": track.get("id"),
        "title": track.get("name", "Unknown Title"),
        "artist": artists,
        "album": album_name,
        "year": year,
        "duration_ms": track.get("duration_ms", 0),
        "duration_sec": int(track.get("duration_ms", 0) / 1000),
        "cover_url": cover_url,
        "spotify_url": track.get("external_urls", {}).get("spotify", ""),
        "track_number": track.get("track_number", 1),
        "added_at": added_at,
    }


def get_recent_liked_songs(sp: spotipy.Spotify, limit: int = 20) -> List[Dict[str, Any]]:
    """Fetch the most recent liked songs from user's library."""
    results = sp.current_user_saved_tracks(limit=limit)
    tracks = []
    for item in results.get("items", []):
        parsed = parse_track_item(item.get("track"), added_at=item.get("added_at"))
        if parsed:
            tracks.append(parsed)
    return tracks


def get_all_liked_songs(sp: spotipy.Spotify, max_limit: int = 1000) -> List[Dict[str, Any]]:
    """Fetch all liked songs up to max_limit."""
    tracks = []
    limit = 50
    offset = 0

    while offset < max_limit:
        results = sp.current_user_saved_tracks(limit=limit, offset=offset)
        items = results.get("items", [])
        if not items:
            break
        for item in items:
            parsed = parse_track_item(item.get("track"), added_at=item.get("added_at"))
            if parsed:
                tracks.append(parsed)
        offset += len(items)
        if len(items) < limit:
            break

    return tracks


def get_track_by_id(sp: Optional[spotipy.Spotify], track_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve details for a single track (uses API if client given, falls back to zero-key extractor)."""
    if sp:
        try:
            raw_track = sp.track(track_id)
            return parse_track_item(raw_track)
        except Exception as e:
            logger.warning("Spotipy track fetch failed, trying no-auth extractor: %s", e)
    return extract_track_no_auth(track_id)


def get_playlist_tracks(sp: Optional[spotipy.Spotify], playlist_id: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Retrieve playlist title and tracks (uses API if client given, falls back to zero-key extractor)."""
    if sp:
        try:
            playlist_data = sp.playlist(playlist_id)
            playlist_name = playlist_data.get("name", "Spotify Playlist")
            tracks = []
            results = playlist_data.get("tracks", {})
            while results:
                for item in results.get("items", []):
                    t = item.get("track")
                    if t and t.get("id"):
                        parsed = parse_track_item(t)
                        if parsed:
                            tracks.append(parsed)
                if results.get("next"):
                    results = sp.next(results)
                else:
                    break
            return playlist_name, tracks
        except Exception as e:
            logger.warning("Spotipy playlist fetch failed, trying no-auth extractor: %s", e)
    return extract_playlist_no_auth(playlist_id)


def get_album_tracks(sp: Optional[spotipy.Spotify], album_id: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Retrieve album title and tracks."""
    if sp:
        try:
            album_data = sp.album(album_id)
            album_name = album_data.get("name", "Spotify Album")
            images = album_data.get("images", [])
            cover_url = images[0]["url"] if images else None
            release_date = album_data.get("release_date", "")
            year = release_date[:4] if release_date else ""
            tracks = []
            for item in album_data.get("tracks", {}).get("items", []):
                if not item.get("id"):
                    continue
                artists = ", ".join(a.get("name", "Unknown") for a in item.get("artists", []))
                tracks.append({
                    "id": item.get("id"),
                    "title": item.get("name", "Unknown Title"),
                    "artist": artists,
                    "album": album_name,
                    "year": year,
                    "duration_ms": item.get("duration_ms", 0),
                    "duration_sec": int(item.get("duration_ms", 0) / 1000),
                    "cover_url": cover_url,
                    "spotify_url": item.get("external_urls", {}).get("spotify", ""),
                    "track_number": item.get("track_number", 1),
                })
            return album_name, tracks
        except Exception as e:
            logger.warning("Spotipy album fetch failed, trying no-auth extractor: %s", e)
    return extract_album_no_auth(album_id)
