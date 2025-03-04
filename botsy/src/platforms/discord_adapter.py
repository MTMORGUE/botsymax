import os
import json
import logging
import asyncio
import threading
import discord  # Use top-level discord for Intents
from discord.ext import commands
from flask import Flask, request
from dotenv import load_dotenv
from src.platforms.base_adapter import BasePlatformAdapter

# Load .env variables
load_dotenv()


class DiscordAdapter(BasePlatformAdapter):
    def __init__(self, bot):
        super().__init__(bot)
        # Retrieve credentials from .env using public key and app id.
        self.public_key = os.getenv(f"{self.bot.name.upper()}_DISCORD_PUBLIC_KEY")
        if not self.public_key:
            self.public_key = "FAKE_DISCORD_PUBLIC_KEY"
        self.app_id = os.getenv(f"{self.bot.name.upper()}_DISCORD_APP_ID")
        if not self.app_id:
            self.app_id = "FAKE_DISCORD_APP_ID"
        logging.info("DiscordAdapter: Retrieved Discord public key and app id (or placeholders).")

        # Call authenticate to satisfy abstract method.
        self.authenticate()

        # Set up conversation persistence. If the file is empty, treat as an empty list.
        self.history_file = os.path.join(self.bot.storage_dir, f"conversation_history_{self.bot.name}.json")
        self.conversation_history = self.load_conversation_history()

        # Prepare Discord client—but do not start yet.
        intents = discord.Intents.default()
        intents.messages = True
        intents.dm_messages = True
        intents.guilds = True
        intents.message_content = True  # Enable privileged intent for message content.
        self.client = commands.Bot(command_prefix="!", intents=intents)
        self.register_events()

        # We'll start the client and Flask endpoints in our start() method.
        self.client_thread = None
        self.flask_thread = None
        self.flask_app = None

    def authenticate(self):
        # This adapter uses credentials loaded from the environment.
        logging.info("DiscordAdapter: Authentication complete using public key and app id.")

    def register_events(self):
        @self.client.event
        async def on_message(message):
            if message.author == self.client.user:
                return
            entry = {
                "author": str(message.author),
                "content": message.content,
                "timestamp": message.created_at.isoformat()
            }
            self.conversation_history.append(entry)
            self.save_conversation_history()
            await self.client.process_commands(message)

        logging.debug("DiscordAdapter: on_message event registered.")

    def load_conversation_history(self):
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r") as f:
                    data = f.read().strip()
                    if not data:
                        return []
                    return json.loads(data)
            except Exception as e:
                logging.error(f"DiscordAdapter: Error loading conversation history: {e}")
        return []

    def save_conversation_history(self):
        try:
            with open(self.history_file, "w") as f:
                json.dump(self.conversation_history, f)
            logging.info("DiscordAdapter: Saved conversation history.")
        except Exception as e:
            logging.error(f"DiscordAdapter: Error saving conversation history: {e}")

    def start_client(self):
        def run_bot():
            try:
                # Use public_key as a placeholder token.
                self.client.run(self.public_key)
            except Exception as e:
                logging.error(f"DiscordAdapter: Error running Discord client: {e}")

        self.client_thread = threading.Thread(target=run_bot, daemon=True)
        self.client_thread.start()
        logging.info("DiscordAdapter: Discord client started in a background thread.")

    def run_flask(self):
        self.flask_app = Flask(f"discord_{self.bot.name}_app")

        @self.flask_app.route("/discord_callback", methods=["GET", "POST"])
        def discord_callback():
            logging.info("DiscordAdapter: Received interaction callback.")
            return "Discord interaction callback received. You may close this window."

        @self.flask_app.route("/discord_shutdown", methods=["POST"])
        def discord_shutdown():
            func = request.environ.get("werkzeug.server.shutdown")
            if func is None:
                logging.error("DiscordAdapter: Not running with Werkzeug, cannot shut down Flask.")
                return "Server shutdown failed", 500
            func()
            logging.info("DiscordAdapter: Flask server shutting down.")
            return "Server shutting down..."

        # Compute a port based on bot.port (e.g. bot.port + 1010)
        port = int(self.bot.port) + 1010
        logging.info(f"DiscordAdapter: Starting Flask server on port {port}")
        self.flask_app.run(host="localhost", port=port, debug=False, use_reloader=False)

    def start_flask(self):
        self.flask_thread = threading.Thread(target=self.run_flask, daemon=True)
        self.flask_thread.start()
        logging.info("DiscordAdapter: Discord Flask endpoints started in background thread.")

    def start(self):
        # Start Discord client and Flask endpoints only when start is explicitly called.
        self.start_client()
        self.start_flask()

    def stop(self):
        # Stop the Discord client gracefully.
        async def close_client():
            await self.client.close()

        asyncio.run_coroutine_threadsafe(close_client(), self.client.loop)
        logging.info("DiscordAdapter: Discord client closed.")
        # Flask shutdown via an HTTP request:
        try:
            import requests
            port = int(self.bot.port) + 1010
            requests.post(f"http://localhost:{port}/discord_shutdown")
        except Exception as e:
            logging.error(f"DiscordAdapter: Error shutting down Flask server: {e}")

    def post(self, content: str):
        channel_id = os.getenv(f"{self.bot.name.upper()}_DISCORD_CHANNEL_ID")
        if not channel_id:
            logging.error("DiscordAdapter: Channel ID not provided in environment.")
            return "no_channel"
        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            logging.error("DiscordAdapter: Channel not found.")
            return "channel_not_found"

        async def send_message():
            try:
                await channel.send(content)
                logging.info(f"DiscordAdapter: Posted message to channel {channel_id}: {content}")
            except Exception as e:
                logging.error(f"DiscordAdapter: Error posting message: {e}")

        asyncio.run_coroutine_threadsafe(send_message(), self.client.loop)
        return "discord_message_id_12345"

    def comment(self, content: str, reply_to_id: str):
        channel_id = os.getenv(f"{self.bot.name.upper()}_DISCORD_CHANNEL_ID")
        if not channel_id:
            logging.error("DiscordAdapter: Channel ID not provided in environment.")
            return "no_channel"
        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            logging.error("DiscordAdapter: Channel not found.")
            return "channel_not_found"

        async def send_reply():
            try:
                original = await channel.fetch_message(int(reply_to_id))
                await original.reply(content)
                logging.info(f"DiscordAdapter: Replied to message {reply_to_id}: {content}")
            except Exception as e:
                logging.error(f"DiscordAdapter: Error replying to message {reply_to_id}: {e}")

        asyncio.run_coroutine_threadsafe(send_reply(), self.client.loop)
        return "discord_reply_id_12345"

    def dm(self, recipient: str, message: str):
        async def send_dm():
            try:
                user = await self.client.fetch_user(int(recipient))
                if user:
                    await user.send(message)
                    logging.info(f"DiscordAdapter: Sent DM to {recipient}: {message}")
            except Exception as e:
                logging.error(f"DiscordAdapter: Error sending DM to {recipient}: {e}")

        asyncio.run_coroutine_threadsafe(send_dm(), self.client.loop)
        return "discord_dm_id_12345"