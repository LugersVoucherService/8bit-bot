import os
from pathlib import Path

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
WEB_SERVER_SECRET = os.getenv("WEB_SERVER_SECRET", "").strip()

WEB_SERVER_URL_PRIMARY = os.getenv("WEB_SERVER_URL_PRIMARY", "").strip()
WEB_SERVER_URL_FALLBACK = os.getenv("WEB_SERVER_URL_FALLBACK", "").strip()

WEB_SERVER_URL = os.getenv("WEB_SERVER_URL", WEB_SERVER_URL_PRIMARY).strip()
if WEB_SERVER_URL != WEB_SERVER_URL_PRIMARY:
    WEB_SERVER_URL_PRIMARY = WEB_SERVER_URL

MAX_BUILD_FILE_SIZE = 30 * 1024 * 1024  # 30MB - maximum build file size for Discord uploads

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "")
R2_ENDPOINT_URL = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL", "")

import tempfile
TEMP_DIR = Path(tempfile.gettempdir()) / "8bit_bot"
TEMP_DIR.mkdir(exist_ok=True)

if not DISCORD_BOT_TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN environment variable must be set")

if not WEB_SERVER_URL_PRIMARY:
    raise ValueError("WEB_SERVER_URL_PRIMARY environment variable must be set")

if not WEB_SERVER_SECRET:
    raise ValueError("WEB_SERVER_SECRET environment variable must be set")

