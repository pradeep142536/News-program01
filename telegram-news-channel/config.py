"""Configuration module for Telegram News Channel automation.

Loads environment variables from .env file and provides typed settings
for Telegram, Gemini API, Pexels API, database, and application server.
"""

from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

# Telegram Configuration
TELEGRAM_BOT_TOKEN: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID: str | None = os.getenv("TELEGRAM_CHANNEL_ID")  # e.g., @yourchannel

# Google Gemini API (supports multiple keys comma-separated in GEMINI_API_KEYS or single GEMINI_API_KEY)
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY")
_keys_env: str = os.getenv("GEMINI_API_KEYS", "")
GEMINI_API_KEYS: list[str] = [k.strip() for k in _keys_env.split(",") if k.strip()]
if GEMINI_API_KEY and GEMINI_API_KEY.strip() and GEMINI_API_KEY.strip() not in GEMINI_API_KEYS:
    GEMINI_API_KEYS.insert(0, GEMINI_API_KEY.strip())

# Pexels API (free, for fallback images)
PEXELS_API_KEY: str = os.getenv("PEXELS_API_KEY", "")

# Check interval in minutes
CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL", "5"))

# Server port
PORT: int = int(os.getenv("PORT", "8080"))

# Database path
DB_PATH: str = os.getenv("DB_PATH", "news_history.db")

# Logging level
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
