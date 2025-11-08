import os
from pathlib import Path

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
WEB_SERVER_SECRET = os.getenv("WEB_SERVER_SECRET", "").strip()

WEB_SERVER_URL_PRIMARY = os.getenv("WEB_SERVER_URL_PRIMARY", "").strip()
WEB_SERVER_URL_FALLBACK = os.getenv("WEB_SERVER_URL_FALLBACK", "").strip()

WEB_SERVER_URL = os.getenv("WEB_SERVER_URL", WEB_SERVER_URL_PRIMARY).strip()
if WEB_SERVER_URL != WEB_SERVER_URL_PRIMARY:
    WEB_SERVER_URL_PRIMARY = WEB_SERVER_URL

MAX_BUILD_FILE_SIZE = 5 * 1024 * 1024

import tempfile
TEMP_DIR = Path(tempfile.gettempdir()) / "8bit_bot"
TEMP_DIR.mkdir(exist_ok=True)

if not DISCORD_BOT_TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN environment variable must be set")

if not WEB_SERVER_URL_PRIMARY:
    raise ValueError("WEB_SERVER_URL_PRIMARY environment variable must be set")

if not WEB_SERVER_SECRET:
    raise ValueError("WEB_SERVER_SECRET environment variable must be set")
