---
title: Spotify Downloader Bot
emoji: 🎵
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# 🎵 Spotify to Telegram Downloader & Auto-Sync Bot

An automated Telegram bot that connects to your Spotify account to:
1. **Auto-Sync Liked Songs**: Detects newly liked tracks in real-time, downloads high-fidelity 320kbps MP3s with official album artwork & ID3 metadata, and delivers them directly into your Telegram chat.
2. **Download Playlists & Albums On-Demand**: Send any Spotify playlist, album, or track URL to receive all songs in your chat with live progress.
3. **Smart Baseline Detection**: Automatically indexes your existing library on initial connect so you are not spammed with old songs.
4. **Zero Hassle Setup**: Uses an embedded FFmpeg engine (no complex PATH configurations or extra software needed).

---

## 🚀 Quick Setup Guide

### Step 1: Create a Telegram Bot
1. Open Telegram and search for [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts to choose a name and username.
3. Copy the **HTTP API Token** provided (e.g. `123456789:ABCdefGhIJKlmNoPQRstuVWXyz`).

---

### Step 2: Get Spotify API Credentials
1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and log in.
2. Click **Create App**.
3. Fill in:
   - **App name**: `Spotify Telegram Bot` (or any name)
   - **App description**: `Personal Telegram music bot`
   - **Redirect URIs**: `http://127.0.0.1:8888/callback`  *(Important!)*
   - Which API/SDKs are you planning to use? Select **Web API**.
4. Check the terms agreement and click **Save**.
5. In your app page, click **Settings** to see your **Client ID** and **Client Secret** (click *View client secret*).

---

### Step 3: Configure `.env`
Copy `.env.example` to `.env`:
```powershell
copy .env.example .env
```

Open `.env` in any text editor and fill in your values:
```ini
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
SPOTIPY_CLIENT_ID=your_spotify_client_id_here
SPOTIPY_CLIENT_SECRET=your_spotify_client_secret_here
SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback
CHECK_INTERVAL_SECONDS=60
```

*(Optional: If you want only yourself to be able to use the bot, set `ALLOWED_TELEGRAM_USER_IDS` to your numeric Telegram user ID from [@userinfobot](https://t.me/userinfobot)).*

---

### Step 4: Run the Bot

You can simply double-click **`run.bat`** or run via command line:

```powershell
.\.venv\Scripts\python.exe main.py
```

---

### Step 5: Connect Spotify & Use the Bot

1. Open your bot on Telegram and press `/start`.
2. Send `/login`. The bot will give you a Spotify authorization link.
3. Click the link and authorize access.
4. When redirected to `http://127.0.0.1:8888/callback?code=...`, **copy the full address from your browser's address bar** and **paste it into the bot chat**.
5. The bot will confirm connection and index your current library.
6. **Done!** Whenever you tap ❤️ (Like) on a song in Spotify, the bot will download and send it to your Telegram!

---

## 🤖 Bot Commands

| Command | Description |
| :--- | :--- |
| `/start` | Welcome message and instructions |
| `/login` | Link your Spotify account via OAuth2 |
| `/status` | View Spotify connection status, synced track count, and last check |
| `/sync` | Manually check for newly liked songs immediately |
| `/toggle` | Pause or resume automatic background monitoring |
| `/logout` | Disconnect and clear your Spotify authorization |
| `/help` | Detailed help and usage information |

---

## 📥 On-Demand Downloads

Simply paste any Spotify URL into the chat:
- **Track**: `https://open.spotify.com/track/...`
- **Playlist**: `https://open.spotify.com/playlist/...`
- **Album**: `https://open.spotify.com/album/...`

The bot will download the tracks and send them directly to you.

---

## 📁 Project Structure

```
Spotify Downloader/
├── .env                  # Your secret tokens and credentials
├── .env.example          # Template for environment configuration
├── .dockerignore         # Docker build ignore rules
├── Dockerfile            # Production container configuration (Python 3.11 + FFmpeg)
├── render.yaml           # Render deployment blueprint
├── bot.py                # Telegram bot handlers & background job
├── config.py             # Configuration loader & cloud environment settings
├── database.py           # Universal SQLite / PostgreSQL database engine
├── downloader.py         # yt-dlp & mutagen audio download and tagging engine
├── main.py               # Main application entry point & cloud health check server
├── requirements.txt      # Python dependencies
├── run.bat               # Windows one-click launcher
└── spotify_service.py    # Spotify Web API and OAuth integration
```

---

## ☁️ 100% Free 24/7 Cloud Deployment Guide

You can run this bot 24/7 in the cloud for free with zero local resource usage.

### Option 1: Hugging Face Spaces (Recommended — Easiest & Most Powerful)
- **100% Free Forever** (No credit card required).
- **Specs**: 2 vCPU, 16 GB RAM, 50 GB persistent space.
- **24/7 Uptime**: Docker Spaces run continuously.

**Deployment Steps:**
1. Create a free account at [Hugging Face](https://huggingface.co/).
2. Click **New Space** (or go to `huggingface.co/new-space`).
3. Set:
   - **Space Name**: `spotify-telegram-bot`
   - **License**: `mit`
   - **Space SDK**: Select **Docker** -> **Blank**.
   - **Space hardware**: Free (2 vCPU · 16 GB RAM).
4. Click **Create Space**.
5. Go to the **Files** tab and upload all files from this project (or push via Git).
6. Go to **Settings** -> **Variables and secrets** -> **New secret**:
   - `TELEGRAM_BOT_TOKEN`: your Telegram bot token
   - `DATABASE_URL` (optional): PostgreSQL URL if you want cloud DB sync (see below)
   - `ALLOWED_TELEGRAM_USER_IDS` (optional): your numeric Telegram ID
7. The Space will automatically build the Docker image and launch the bot!

---

### Option 2: Render (Free Web Service) + Neon (Free PostgreSQL)
1. **Get Free PostgreSQL (30 Seconds, No Credit Card):**
   - Go to [Neon.tech](https://neon.tech/) and sign up.
   - Click **Create Project**. Copy your **Connection String** (`postgresql://...`).
2. **Deploy on Render:**
   - Go to [Render.com](https://render.com/) and click **New +** -> **Web Service**.
   - Connect your GitHub repository (or use the Public Git URL).
   - Select **Docker** as the Environment.
   - Set **Plan** to **Free**.
   - Under **Environment Variables**, add:
     - `TELEGRAM_BOT_TOKEN`: your bot token
     - `DATABASE_URL`: your Neon PostgreSQL connection string
   - Click **Create Web Service**.
3. **Keep Render Awake:**
   - Free Render web services sleep after 15 minutes of inactivity.
   - Go to [cron-job.org](https://cron-job.org/) or [UptimeRobot](https://uptimerobot.com/) and set up a free HTTP ping to your Render app URL (e.g. `https://your-bot.onrender.com/`) every 10 minutes.
   - The bot's built-in health-check server will answer with `200 OK` and keep it alive 24/7!
