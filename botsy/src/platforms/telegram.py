import os
import logging
from dotenv import load_dotenv
from src.platforms.base_adapter import BasePlatformAdapter

# Load .env variables
load_dotenv()

class TelegramAdapter(BasePlatformAdapter):
    def __init__(self, bot):
        super().__init__(bot)
        self.bot_token = None
        self.authenticate()

    def authenticate(self):
        # Attempt to load the Telegram bot token from the environment
        self.bot_token = os.getenv(f"{self.bot.name.upper()}_TELEGRAM_BOT_TOKEN")
        if not self.bot_token:
            # Fallback to a placeholder value if not provided
            self.bot_token = "FAKE_TELEGRAM_BOT_TOKEN"
        logging.info("TelegramAdapter: Authenticated using .env token (or placeholder).")

    def post(self, content: str):
        logging.info(f"TelegramAdapter: Posting content: {content}")
        return "telegram_message_id_12345"

    def comment(self, content: str, reply_to_id: str):
        logging.info(f"TelegramAdapter: Replying to {reply_to_id}: {content}")
        return "telegram_reply_id_12345"

    def dm(self, recipient: str, message: str):
        logging.info(f"TelegramAdapter: Sending DM to {recipient}: {message}")
        return "telegram_dm_id_12345"