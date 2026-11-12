"""
config.py - settings shared by the whole project.

Secrets (API keys) are read from the .env file in the project root.
The .env file is listed in .gitignore, so it is never committed.
"""
import os

from dotenv import load_dotenv

from app.core.paths import PROJECT_ROOT

# Read the .env file once, when this module is first imported.
load_dotenv(PROJECT_ROOT / ".env")


def get_required_setting(setting_name):
    """Return a setting from the environment, or stop with a clear message if it is missing."""
    setting_value = os.getenv(setting_name)
    if not setting_value:
        raise RuntimeError(f"Missing setting {setting_name}. Add it to the .env file.")
    return setting_value
