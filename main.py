import logging
import sys
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
import config
import database
import bot

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler to satisfy cloud health checks and keep-alive pings."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        payload = json.dumps(
            {
                "status": "ok",
                "service": "Spotify Downloader Telegram Bot",
                "database": "PostgreSQL" if database.USE_POSTGRES else "SQLite",
            }
        ).encode("utf-8")
        self.wfile.write(payload)

    def log_message(self, format, *args):
        # Suppress routine health check request logs to keep terminal logs clean
        return


def start_health_server(port: int) -> None:
    """Start the lightweight health check server in a background daemon thread."""
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info("Cloud Health-Check HTTP server listening on 0.0.0.0:%d", port)
    except Exception as e:
        logger.warning("Could not bind health-check server on port %d: %s", port, e)


def main() -> None:
    """Main entry point for Spotify Downloader & Auto-Sync Telegram Bot."""
    print("=" * 60)
    print(" Spotify Downloader & Liked Songs Telegram Bot")
    print("=" * 60)

    # Initialize database
    database.init_db()

    # Validate environment variables
    if not config.validate_config():
        print(
            "\n[!] Please create or edit your '.env' file with your real bot token and Spotify API keys."
        )
        print("    See README.md or .env.example for guidance.\n")
        sys.exit(1)

    # Start cloud health-check server for platforms like Render, Koyeb, Hugging Face
    start_health_server(config.PORT)

    logger.info("FFmpeg binary detected at: %s", config.FFMPEG_EXECUTABLE)
    logger.info("Starting Telegram Bot application...")

    try:
        app = bot.build_application()
        logger.info("Bot is polling for updates. Press Ctrl+C to stop.")
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.critical("Fatal error running bot: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
