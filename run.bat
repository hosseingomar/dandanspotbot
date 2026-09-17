@echo off
title Spotify Telegram Bot
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [!] Virtual environment not found. Setting up .venv...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)

if not exist ".env" (
    echo [!] Warning: .env file not found!
    echo Creating .env from .env.example...
    copy .env.example .env
    echo Please edit .env with your credentials and run again.
    notepad .env
    pause
    exit /b
)

echo [*] Launching Spotify Telegram Downloader Bot...
python main.py
pause
