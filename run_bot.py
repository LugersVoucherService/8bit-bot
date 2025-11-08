import sys
import os
from pathlib import Path

# Get the directory containing this script
script_dir = Path(__file__).parent.absolute()

# Add the script directory to Python path (for app imports)
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

# Add app directory to path
app_dir = script_dir / "app"
if str(app_dir) not in sys.path:
    sys.path.insert(0, str(app_dir))

# Now import bot
from app.bot import main

if __name__ == "__main__":
    main()

