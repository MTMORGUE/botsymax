import os
import sys
import threading
import logging
import time
import requests

from utils import setup_logging, load_environment, exit_with_error
from bot import Bot
from console import master_console
import gui  # Our gui.py module

CONFIGS_DIR = os.path.join(os.path.dirname(__file__), "..", "configs")

def load_config_files():
    if not os.path.exists(CONFIGS_DIR):
        exit_with_error(f"❌ Config folder '{CONFIGS_DIR}' not found. Exiting.")
    config_files = [os.path.join(CONFIGS_DIR, f) for f in os.listdir(CONFIGS_DIR)
                    if f.endswith(".yaml") or f.endswith(".yml")]
    if not config_files:
        exit_with_error(f"❌ No config files found in '{CONFIGS_DIR}'. Exiting.")
    return config_files

def initialize_bots(config_files):
    bots = {}
    port_start = 5050
    from src.platforms.twitter_adapter import TwitterAdapter

    from src.platforms.facebook import FacebookAdapter
    from src.platforms.instagram import InstagramAdapter
    from src.platforms.telegram import TelegramAdapter
    from src.platforms.discord_adapter import DiscordAdapter
    for i, cfg in enumerate(config_files):
        bot_name = os.path.splitext(os.path.basename(cfg))[0]
        port = port_start + i
        bot = Bot(name=bot_name, config_path=cfg, port=port)
        bot.load_config()
        bot.add_platform_adapter("twitter", TwitterAdapter(bot))
        bot.add_platform_adapter("facebook", FacebookAdapter(bot))
        bot.add_platform_adapter("instagram", InstagramAdapter(bot))
        bot.add_platform_adapter("telegram", TelegramAdapter(bot))
        bot.add_platform_adapter("discord", DiscordAdapter(bot))
        bots[bot_name] = bot
    return bots

def start_gui(bots):
    gui.set_bots(bots)
    gui_thread = threading.Thread(target=gui.run_gui, daemon=True)
    gui_thread.start()
    logging.info("GUI started on http://localhost:8760")

def main():
    setup_logging()
    load_environment()
    import openai
    openai.api_key = os.getenv("OPENAI_API_KEY")
    config_files = load_config_files()
    bots = initialize_bots(config_files)
    start_gui(bots)
    input("Press Enter to start the Master Console...")
    bots_dict = {bot.name.lower(): bot for bot in bots.values()}
    master_console(bots_dict)

if __name__ == "__main__":
    main()

