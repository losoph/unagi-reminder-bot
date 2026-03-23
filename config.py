import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_TIMEZONE_NAME = os.getenv("APP_TIMEZONE", "Europe/Moscow")
DEFAULT_USER_TIMEZONE_NAME = os.getenv("DEFAULT_USER_TIMEZONE", APP_TIMEZONE_NAME)
DB_TIMEZONE_NAME = "UTC"

APP_TIMEZONE = ZoneInfo(APP_TIMEZONE_NAME)
DEFAULT_USER_TIMEZONE = ZoneInfo(DEFAULT_USER_TIMEZONE_NAME)
DB_TIMEZONE = ZoneInfo(DB_TIMEZONE_NAME)
