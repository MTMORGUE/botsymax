import os
import sys
import json
import logging
import threading
import time
import re
import random
import signal
import datetime
import tweepy
import schedule
import pytz
import openai
import yaml
import requests  # Used for stopping the Flask server via HTTP request
from flask import Flask, request
from dotenv import load_dotenv
from jinja2 import Template

# Global Constants
MAX_AUTH_RETRIES = 3
CONFIGS_DIR = "configs"  # folder containing each bot's config file
RATE_LIMIT_WAIT = 60     # seconds to wait when a rate limit is hit
ME_CACHE_DURATION = 300  # seconds to cache authenticated user info in memory
TOKEN_EXPIRY_SECONDS = 90 * 24 * 3600  # tokens “expire” in 90 days

def setup_logging():
    log_file = "logs.txt"
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, mode="a", encoding="utf-8")
        ]
    )
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("\n" + "=" * 50 + "\n")
        f.write(f"📝 NEW SESSION STARTED: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 50 + "\n\n")

def print_master_prompt():
    print("\nMaster Console: Enter command ('list', 'start', 'stop', bot name, 'show log all', 'help' or 'exit'):")

def print_bot_prompt(bot):
    print(f"\n[Bot {bot.name}] ", end='')

class Bot:
    def __init__(self, name, config_path, port):
        self.name = name
        self.config_file = config_path
        self.port = port
        self.token_file = f"token_{self.name}.json"
        self.user_id_cache_file = f"user_id_cache_{self.name}.json"
        self.bot_tweet_cache_file = f"bot_tweet_cache_{self.name}.json"
        self.config = {}
        self.client = None

        # OAuth state specific to this bot
        self.oauth_verifier = None
        self.request_token = None

        # Manual run counts (default = 1)
        self.post_run_count = 1
        self.comment_run_count = 1
        self.reply_run_count = 1

        # Instance caches for user IDs and bot tweet info
        self.user_id_cache = {}
        self.bot_tweet_cache = {"tweet_id": None, "timestamp": 0}

        # Dictionary for storing last tweet IDs for monitored handles
        self.monitored_handles_last_ids = {}

        # Scheduler
        self.scheduler = schedule.Scheduler()

        # Flask app for OAuth callback
        self.app = None

        # In-memory cache for authenticated user info ("me")
        self.cached_me = None
        self.me_cache_timestamp = 0

        # Control flags and thread handles
        self.running = False
        self._stop_event = threading.Event()
        self.flask_thread = None
        self.scheduler_thread = None

        # Auto functions enabled flags
        self.auto_post_enabled = True
        self.auto_comment_enabled = True
        self.auto_reply_enabled = True

        # Platform adapters container
        self.platform_adapters = {}
        # For conversation history (if needed)
        self.conversation_history = ""

        # For mood (if used in prompts)
        self.mood_state = "neutral"
        # For engagement metrics (file to be created later)
        self.engagement_metrics_file = f"engagement_metrics_{self.name}.json"

    # Method to add a platform adapter (e.g., twitter, facebook, etc.)
    def add_platform_adapter(self, platform, adapter):
        self.platform_adapters[platform] = adapter

    # ----- Utility Methods -----
    @staticmethod
    def clean_tweet_text(text):
        cleaned = text.strip(" '\"")
        cleaned = re.sub(r'\n+', ' ', cleaned)
        return cleaned[:280]

    def save_token(self, token_data):
        try:
            token_data["created_at"] = time.time()
            with open(self.token_file, "w") as f:
                json.dump(token_data, f)
            logging.info(f"✅ Bot {self.name}: Token saved successfully to {self.token_file}")
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error saving token: {str(e)}")

    def load_token(self):
        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Error loading token: {str(e)}")
        return None

    def call_openai_completion(self, model, messages, temperature, max_tokens, top_p, frequency_penalty, presence_penalty):
        try:
            response = openai.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty
            )
            raw_text = response.choices[0].message.content.strip()
            return Bot.clean_tweet_text(raw_text)
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error generating OpenAI completion: {str(e)}")
            return None

    def load_config(self):
        if not os.path.exists(self.config_file):
            logging.error(f"❌ Bot {self.name}: Config file '{self.config_file}' not found.")
            self.config = {}
            return
        try:
            with open(self.config_file, "r") as file:
                self.config = yaml.safe_load(file)
            logging.info(f"✅ Bot {self.name}: Loaded config from {self.config_file}")
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error loading config file: {str(e)}")
            self.config = {}

    # ----- Flask Server (OAuth Callback) -----
    def run_flask(self):
        self.app = Flask(f"bot_{self.name}_app")

        @self.app.route("/callback")
        def callback():
            self.oauth_verifier = request.args.get("oauth_verifier")
            if self.oauth_verifier:
                logging.info(f"✅ Bot {self.name}: OAuth verifier received successfully")
                return "Authorization successful! You may close this window."
            logging.error(f"❌ Bot {self.name}: Missing OAuth verifier parameter!")
            return "Missing OAuth verifier parameter!", 400

        @self.app.route("/shutdown", methods=["POST"])
        def shutdown():
            func = request.environ.get("werkzeug.server.shutdown")
            if func is None:
                logging.error(f"❌ Bot {self.name}: Not running with the Werkzeug Server, cannot shut down Flask.")
                return "Server shutdown failed", 500
            func()
            logging.info(f"Bot {self.name}: Flask server shutting down.")
            return "Server shutting down..."

        logging.info(f"🚀 Bot {self.name}: Starting Flask server on port {self.port}")
        self.app.run(host="localhost", port=self.port, debug=False, use_reloader=False)

    # ----- Authentication (Using OAuth 1.0a) -----
    def authenticate(self):
        self.oauth_verifier = None
        self.request_token = None
        consumer_key = os.getenv(f"{self.name.upper()}_TWITTER_CONSUMER_KEY")
        consumer_secret = os.getenv(f"{self.name.upper()}_TWITTER_CONSUMER_SECRET")
        if not consumer_key or not consumer_secret:
            logging.error(f"❌ Bot {self.name}: Twitter API keys not found in .env")
            sys.exit(1)
        for attempt in range(MAX_AUTH_RETRIES):
            token_data = self.load_token()
            if token_data:
                try:
                    client = tweepy.Client(
                        consumer_key=consumer_key,
                        consumer_secret=consumer_secret,
                        access_token=token_data["access_token"],
                        access_token_secret=token_data["access_token_secret"],
                        wait_on_rate_limit=False
                    )
                    me = client.get_me()
                    self.cached_me = me
                    self.me_cache_timestamp = time.time()
                    logging.info(f"✅ Bot {self.name}: Using stored authentication token")
                    self.client = client
                    return client
                except tweepy.Unauthorized:
                    logging.warning(f"⚠️ Bot {self.name}: Stored token invalid, starting fresh auth")
                    if os.path.exists(self.token_file):
                        os.remove(self.token_file)
            auth = tweepy.OAuth1UserHandler(
                consumer_key,
                consumer_secret,
                callback=f"http://localhost:{self.port}/callback"
            )
            try:
                auth_url = auth.get_authorization_url()
                self.request_token = auth.request_token
                logging.info(f"🔗 Bot {self.name}: Authentication URL: {auth_url}")
                print(f"\nBot {self.name}: Open this URL to authorize: {auth_url}\n")
                while self.oauth_verifier is None:
                    time.sleep(1)
                access_token, access_token_secret = auth.get_access_token(self.oauth_verifier)
                self.save_token({
                    "access_token": access_token,
                    "access_token_secret": access_token_secret
                })
                logging.info(f"✅ Bot {self.name}: New authentication token stored")
                client = tweepy.Client(
                    consumer_key=consumer_key,
                    consumer_secret=consumer_secret,
                    access_token=access_token,
                    access_token_secret=access_token_secret,
                    wait_on_rate_limit=False
                )
                me = client.get_me()
                self.cached_me = me
                self.me_cache_timestamp = time.time()
                self.client = client
                return client
            except tweepy.TweepyException as e:
                logging.error(f"❌ Bot {self.name}: Authentication failed: {str(e)}")
                if attempt < MAX_AUTH_RETRIES - 1:
                    logging.info(f"🔁 Bot {self.name}: Retrying auth...")
                    time.sleep(2)
                    continue
                logging.error(f"❌ Bot {self.name}: Max auth attempts reached")
                sys.exit(1)
        return None

    def get_cached_me(self):
        if self.cached_me and (time.time() - self.me_cache_timestamp) < ME_CACHE_DURATION:
            return self.cached_me
        try:
            me = self.client.get_me()
            self.cached_me = me
            self.me_cache_timestamp = time.time()
            return me
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error refreshing authenticated user info: {e}")
            return None

    # ----- Caching for User IDs -----
    def load_user_id_cache(self):
        if os.path.exists(self.user_id_cache_file):
            try:
                with open(self.user_id_cache_file, "r") as f:
                    self.user_id_cache = json.load(f)
                logging.info(f"✅ Bot {self.name}: Loaded user_id cache from {self.user_id_cache_file}")
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Could not load user_id cache: {e}")

    def save_user_id_cache(self):
        try:
            with open(self.user_id_cache_file, "w") as f:
                json.dump(self.user_id_cache, f)
            logging.info(f"✅ Bot {self.name}: Saved user_id cache to {self.user_id_cache_file}")
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Could not save user_id cache: {e}")

    def get_user_id(self, username):
        username_lower = username.lower()
        if username_lower in self.user_id_cache:
            logging.info(f"🔄 Bot {self.name}: Using cached user_id for {username}: {self.user_id_cache[username_lower]}")
            return self.user_id_cache[username_lower]
        try:
            response = self.client.get_user(username=username, user_auth=True)
            if response and response.data:
                user_id = response.data.id
                self.user_id_cache[username_lower] = user_id
                self.save_user_id_cache()
                logging.info(f"🔗 Bot {self.name}: Fetched and cached user_id for {username}: {user_id}")
                return user_id
            else:
                logging.warning(f"⚠️ Bot {self.name}: No data returned for username {username}")
        except tweepy.TooManyRequests:
            logging.warning(f"⚠️ Bot {self.name}: Rate limit reached while fetching user_id for {username}. Backing off...")
            time.sleep(RATE_LIMIT_WAIT)
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error fetching user id for {username}: {e}")
        return None

    def get_user_ids_bulk(self, usernames):
        usernames = [u for u in usernames if u.lower() not in self.user_id_cache]
        if usernames:
            try:
                response = self.client.get_users(usernames=usernames, user_auth=True, user_fields=["id"])
                if response and response.data:
                    for user in response.data:
                        self.user_id_cache[user.username.lower()] = user.id
                    self.save_user_id_cache()
                else:
                    logging.warning(f"⚠️ Bot {self.name}: No data returned for bulk user lookup")
            except tweepy.TooManyRequests:
                logging.warning(f"⚠️ Bot {self.name}: Rate limit hit during bulk user lookup. Backing off...")
                time.sleep(RATE_LIMIT_WAIT)
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Error during bulk user lookup: {e}")
        return {u: self.user_id_cache.get(u.lower()) for u in usernames}

    # ----- Caching Bot's Recent Tweet ID -----
    def load_bot_tweet_cache(self):
        if os.path.exists(self.bot_tweet_cache_file):
            try:
                with open(self.bot_tweet_cache_file, "r") as f:
                    self.bot_tweet_cache = json.load(f)
                logging.info(f"✅ Bot {self.name}: Loaded bot tweet cache from {self.bot_tweet_cache_file}")
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Could not load bot tweet cache: {e}")

    def save_bot_tweet_cache(self):
        try:
            with open(self.bot_tweet_cache_file, "w") as f:
                json.dump(self.bot_tweet_cache, f)
            logging.info(f"✅ Bot {self.name}: Saved bot tweet cache to {self.bot_tweet_cache_file}")
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Could not save bot tweet cache: {e}")

    def get_bot_recent_tweet_id(self, cache_duration=300):
        current_time = time.time()
        self.load_bot_tweet_cache()
        if (self.bot_tweet_cache["tweet_id"] is not None and
                (current_time - self.bot_tweet_cache["timestamp"]) < cache_duration):
            logging.info(f"🔄 Bot {self.name}: Using cached bot tweet id.")
            return self.bot_tweet_cache["tweet_id"]
        try:
            me = self.get_cached_me()
            if not (me and me.data):
                logging.error(f"❌ Bot {self.name}: Unable to retrieve authenticated user info.")
                return None
            response = self.client.get_users_tweets(
                id=me.data.id,
                max_results=5,
                tweet_fields=["id"],
                user_auth=True
            )
            if response and response.data:
                tweet_id = response.data[0].id
                self.bot_tweet_cache["tweet_id"] = tweet_id
                self.bot_tweet_cache["timestamp"] = current_time
                self.save_bot_tweet_cache()
                logging.info(f"🔗 Bot {self.name}: Fetched and cached bot tweet id: {tweet_id}")
                return tweet_id
            else:
                logging.warning(f"⚠️ Bot {self.name}: No recent tweets found.")
        except tweepy.TooManyRequests:
            logging.warning(f"⚠️ Bot {self.name}: Rate limit reached while fetching bot's recent tweet id. Backing off...")
            time.sleep(RATE_LIMIT_WAIT)
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error fetching bot's recent tweet id: {e}")
        return None

    # ----- New Method: Fetch Latest News -----
    def fetch_news(self, keyword=None):
        news_api_key = os.getenv("NEWS_API_KEY")
        if not news_api_key:
            logging.error(f"❌ Bot {self.name}: NEWS_API_KEY not found in .env")
            return {"headline": "", "article": ""}
        base_url = f"https://newsdata.io/api/1/latest?apikey={news_api_key}"
        if keyword:
            base_url += f"&q={keyword}"
        try:
            response = requests.get(base_url)
            if response.status_code != 200:
                logging.error(f"❌ Bot {self.name}: News API request failed: {response.status_code} {response.text}")
                return {"headline": "", "article": ""}
            data = response.json()
            articles = data.get("results", [])
            if articles:
                article = articles[0]
                headline = article.get("title", "")
                article_text = article.get("description", "")
                logging.info(f"🔗 Bot {self.name}: Fetched news headline: {headline}")
                return {"headline": headline, "article": article_text}
            else:
                logging.info(f"⚠️ Bot {self.name}: No articles found for keyword: {keyword}")
                return {"headline": "", "article": ""}
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error fetching news: {e}")
            return {"headline": "", "article": ""}

    # ----- Updated Tweet Generation -----
    def generate_tweet(self):
        if not self.config:
            logging.error(f"❌ Bot {self.name}: Configuration is empty or invalid.")
            return None
        contexts = self.config.get("contexts", {})
        if not contexts:
            logging.error(f"❌ Bot {self.name}: No contexts found in config.")
            return None
        random_context = random.choice(list(contexts.keys()))
        logging.info(f"🔎 Bot {self.name}: Selected context: {random_context}")
        prompt_settings = contexts[random_context].get("prompt", {})
        if not prompt_settings:
            logging.error(f"❌ Bot {self.name}: No prompt data found for context '{random_context}'.")
            return None

        system_prompt = prompt_settings.get("system", "")
        user_prompt = prompt_settings.get("user", "")
        model = prompt_settings.get("model", "gpt-4o")
        temperature = prompt_settings.get("temperature", 1)
        max_tokens = prompt_settings.get("max_tokens", 16384)
        top_p = prompt_settings.get("top_p", 1.0)
        frequency_penalty = prompt_settings.get("frequency_penalty", 0.8)
        presence_penalty = prompt_settings.get("presence_penalty", 0.1)

        if prompt_settings.get("include_news", False):
            news_keyword = prompt_settings.get("news_keyword", None)
            news_data = self.fetch_news(news_keyword)
            user_prompt = user_prompt.replace("{{news_headline}}", news_data["headline"])
            user_prompt = user_prompt.replace("{{news_article}}", news_data["article"])

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if user_prompt:
            messages.append({"role": "user", "content": user_prompt})

        tweet_text = self.call_openai_completion(model, messages, temperature, max_tokens, top_p,
                                                 frequency_penalty, presence_penalty)
        return self.clean_tweet_text(tweet_text) if tweet_text else None

    def post_tweet(self):
        tweet = self.generate_tweet()
        if not tweet:
            logging.error(f"❌ Bot {self.name}: No tweet generated")
            return False
        try:
            self.client.create_tweet(text=tweet)
            logging.info(f"✅ Bot {self.name}: Tweet posted successfully: {tweet}")
            return True
        except tweepy.Unauthorized:
            logging.error(f"❌ Bot {self.name}: Invalid credentials, removing token file")
            if os.path.exists(self.token_file):
                os.remove(self.token_file)
            return False
        except tweepy.TooManyRequests:
            logging.warning(f"⚠️ Bot {self.name}: Rate limit hit while posting tweet. Backing off...")
            time.sleep(RATE_LIMIT_WAIT)
            return False
        except tweepy.TweepyException as e:
            logging.error(f"❌ Bot {self.name}: Error posting tweet: {str(e)}")
            return False

    def daily_tweet_job(self):
        logging.info(f"⏰ Bot {self.name}: Attempting to post a tweet...")
        success = False
        for _ in range(MAX_AUTH_RETRIES):
            if self.post_tweet():
                success = True
                break
            time.sleep(2)
        if success:
            logging.info(f"✅ Bot {self.name}: Tweet posted at {datetime.datetime.now(pytz.utc)}")
        else:
            logging.error(f"❌ Bot {self.name}: Failed to post tweet after multiple attempts")

    # ----- Bot Console Commands -----
    def process_console_command(self, cmd):
        if cmd == "start":
            self.start()
        elif cmd == "stop":
            self.stop()
        elif cmd == "new auth":
            if os.path.exists(self.token_file):
                os.remove(self.token_file)
                self.cached_me = None
                logging.info(f"✅ Bot {self.name}: Token file removed. Bot will reauthenticate on next startup.")
                print("Token file removed. Bot will reauthenticate on next startup.")
            else:
                logging.info(f"✅ Bot {self.name}: No token file found.")
                print("No token file found.")
        elif cmd == "auth age":
            print(self.get_auth_age())
        elif cmd.startswith("run post"):
            logging.info(f"🚀 Bot {self.name}: 'run post' command received. Posting on all platforms.")
            self.post_job_wrapper()
        elif cmd.startswith("run comment"):
            logging.info(f"🚀 Bot {self.name}: 'run comment' command received. Commenting on all platforms.")
            self.comment_job_wrapper()
        elif cmd.startswith("run reply"):
            logging.info(f"🚀 Bot {self.name}: 'run reply' command received. Replying on all platforms.")
            self.reply_job_wrapper()
        elif cmd.startswith("set post count "):
            try:
                value = int(cmd.split("set post count ")[1])
                self.post_run_count = value
                logging.info(f"✅ Bot {self.name}: Set post count to {self.post_run_count}")
            except Exception:
                logging.error(f"❌ Bot {self.name}: Invalid value for post count")
        elif cmd.startswith("set comment count "):
            try:
                value = int(cmd.split("set comment count ")[1])
                self.comment_run_count = value
                logging.info(f"✅ Bot {self.name}: Set comment count to {self.comment_run_count}")
            except Exception:
                logging.error(f"❌ Bot {self.name}: Invalid value for comment count")
        elif cmd.startswith("set reply count "):
            try:
                value = int(cmd.split("set reply count ")[1])
                self.reply_run_count = value
                logging.info(f"✅ Bot {self.name}: Set reply count to {self.reply_run_count}")
            except Exception:
                logging.error(f"❌ Bot {self.name}: Invalid value for reply count")
        elif cmd == "list context":
            if self.config and "contexts" in self.config:
                contexts = list(self.config["contexts"].keys())
                if contexts:
                    print("Available contexts: " + ", ".join(contexts))
                    logging.info(f"🔍 Bot {self.name}: Listed contexts: {', '.join(contexts)}")
                else:
                    print("No contexts defined in the configuration.")
                    logging.info(f"🔍 Bot {self.name}: No contexts found in config.")
            else:
                print("No configuration loaded or 'contexts' section missing.")
                logging.error(f"❌ Bot {self.name}: Configuration or contexts section missing.")
        elif cmd.startswith("run context"):
            parts = cmd.split(" ", 2)
            if len(parts) < 3:
                print("Usage: run context {context name}")
                logging.error(f"❌ Bot {self.name}: 'run context' requires a context name.")
            else:
                context_name = parts[2].strip()
                if self.config and "contexts" in self.config and context_name in self.config["contexts"]:
                    prompt_settings = self.config["contexts"][context_name].get("prompt", {})
                    if not prompt_settings:
                        print(f"Context '{context_name}' does not have prompt settings defined.")
                        logging.error(f"❌ Bot {self.name}: Prompt settings missing for context '{context_name}'.")
                    else:
                        system_prompt = prompt_settings.get("system", "")
                        user_prompt = prompt_settings.get("user", "")
                        if prompt_settings.get("include_news", False):
                            news_keyword = prompt_settings.get("news_keyword", None)
                            news_data = self.fetch_news(news_keyword)
                            template = Template(user_prompt)
                            user_prompt = template.render(
                                news_headline=news_data.get("headline", ""),
                                news_article=news_data.get("article", ""),
                                mood_state=self.mood_state
                            )
                        else:
                            template = Template(user_prompt)
                            user_prompt = template.render(mood_state=self.mood_state)
                        messages = []
                        if system_prompt:
                            messages.append({"role": "system", "content": system_prompt})
                        if user_prompt:
                            messages.append({"role": "user", "content": user_prompt})
                        model = prompt_settings.get("model", "gpt-4o")
                        temperature = prompt_settings.get("temperature", 1)
                        max_tokens = prompt_settings.get("max_tokens", 16384)
                        top_p = prompt_settings.get("top_p", 1.0)
                        frequency_penalty = prompt_settings.get("frequency_penalty", 0.8)
                        presence_penalty = prompt_settings.get("presence_penalty", 0.1)
                        result = self.call_openai_completion(model, messages, temperature, max_tokens, top_p,
                                                             frequency_penalty, presence_penalty)
                        print(f"Generated output for context '{context_name}':\n{result}")
                        logging.info(f"✅ Bot {self.name}: Ran context '{context_name}' successfully.")
                else:
                    print(f"Context '{context_name}' not found in configuration.")
                    logging.error(f"❌ Bot {self.name}: Context '{context_name}' does not exist.")
        elif cmd == "new random all":
            logging.info(f"🚀 Bot {self.name}: Scheduling new random times for post, comment, and reply.")
            self.re_randomize_schedule()
        elif cmd == "new random post":
            logging.info(f"🚀 Bot {self.name}: Scheduling new random time for post.")
            self.scheduler.clear("randomized_post")
            if self.auto_post_enabled:
                self.schedule_next_post_job()
        elif cmd == "new random comment":
            logging.info(f"🚀 Bot {self.name}: Scheduling new random time for comment.")
            self.scheduler.clear("randomized_comment")
            if self.auto_comment_enabled:
                self.schedule_next_comment_job()
        elif cmd == "new random reply":
            logging.info(f"🚀 Bot {self.name}: Scheduling new random time for reply.")
            self.scheduler.clear("randomized_reply")
            if self.auto_reply_enabled:
                self.schedule_next_reply_job()
        elif cmd == "stop post":
            if self.auto_post_enabled:
                self.scheduler.clear("randomized_post")
                self.auto_post_enabled = False
                logging.info(f"🚫 Bot {self.name}: Auto post disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto post is already disabled.")
        elif cmd == "start post":
            if not self.auto_post_enabled:
                self.auto_post_enabled = True
                self.schedule_next_post_job()
                logging.info(f"✅ Bot {self.name}: Auto post enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto post is already enabled.")
        elif cmd == "stop comment":
            if self.auto_comment_enabled:
                self.scheduler.clear("randomized_comment")
                self.auto_comment_enabled = False
                logging.info(f"🚫 Bot {self.name}: Auto comment disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto comment is already disabled.")
        elif cmd == "start comment":
            if not self.auto_comment_enabled:
                self.auto_comment_enabled = True
                self.schedule_next_comment_job()
                logging.info(f"✅ Bot {self.name}: Auto comment enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto comment is already enabled.")
        elif cmd == "stop reply":
            if self.auto_reply_enabled:
                self.scheduler.clear("randomized_reply")
                self.auto_reply_enabled = False
                logging.info(f"🚫 Bot {self.name}: Auto reply disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto reply is already disabled.")
        elif cmd == "start reply":
            if not self.auto_reply_enabled:
                self.auto_reply_enabled = True
                self.schedule_next_reply_job()
                logging.info(f"✅ Bot {self.name}: Auto reply enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto reply is already enabled.")
        elif cmd == "start cross":
            if not hasattr(self, 'auto_cross_enabled') or not self.auto_cross_enabled:
                self.auto_cross_enabled = True
                self.scheduler.every(1).hours.do(self.cross_job_wrapper).tag("cross_engagement")
                logging.info(f"✅ Bot {self.name}: Auto cross-platform engagement enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto cross-platform engagement is already enabled.")
        elif cmd == "stop cross":
            if hasattr(self, 'auto_cross_enabled') and self.auto_cross_enabled:
                self.scheduler.clear("cross_engagement")
                self.auto_cross_enabled = False
                logging.info(f"🚫 Bot {self.name}: Auto cross-platform engagement disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto cross-platform engagement is already disabled.")
        elif cmd == "start trending":
            if not hasattr(self, 'auto_trending_enabled') or not self.auto_trending_enabled:
                self.auto_trending_enabled = True
                self.scheduler.every().day.at("11:00").do(self.trending_job_wrapper).tag("trending_engagement")
                logging.info(f"✅ Bot {self.name}: Auto trending engagement enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto trending engagement is already enabled.")
        elif cmd == "stop trending":
            if hasattr(self, 'auto_trending_enabled') and self.auto_trending_enabled:
                self.scheduler.clear("trending_engagement")
                self.auto_trending_enabled = False
                logging.info(f"🚫 Bot {self.name}: Auto trending engagement disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto trending engagement is already disabled.")
        elif cmd == "start dm":
            if not hasattr(self, 'auto_dm_enabled') or not self.auto_dm_enabled:
                self.auto_dm_enabled = True
                self.scheduler.every(30).minutes.do(self.dm_job_wrapper).tag("dm_job")
                logging.info(f"✅ Bot {self.name}: Auto DM check enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto DM check is already enabled.")
        elif cmd == "stop dm":
            if hasattr(self, 'auto_dm_enabled') and self.auto_dm_enabled:
                self.scheduler.clear("dm_job")
                self.auto_dm_enabled = False
                logging.info(f"🚫 Bot {self.name}: Auto DM check disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto DM check is already disabled.")
        elif cmd.startswith("run dm"):
            parts = cmd.split(" ", 2)
            if len(parts) < 3:
                print("Usage: run dm {recipient_username}")
                logging.error(f"❌ Bot {self.name}: 'run dm' requires a recipient username.")
            else:
                recipient = parts[2].strip()
                message = input("Enter DM message: ")
                for adapter in self.platform_adapters.values():
                    adapter.dm(recipient, message)
        elif cmd == "start story":
            if not hasattr(self, 'auto_story_enabled') or not self.auto_story_enabled:
                self.auto_story_enabled = True
                self.scheduler.every().day.at("16:00").do(self.story_job_wrapper).tag("story_job")
                logging.info(f"✅ Bot {self.name}: Auto collaborative storytelling enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto collaborative storytelling is already enabled.")
        elif cmd == "stop story":
            if hasattr(self, 'auto_story_enabled') and self.auto_story_enabled:
                self.scheduler.clear("story_job")
                self.auto_story_enabled = False
                logging.info(f"🚫 Bot {self.name}: Auto collaborative storytelling disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto collaborative storytelling is already disabled.")
        elif cmd.startswith("run story"):
            logging.info(f"🚀 Bot {self.name}: 'run story' command received. Running storytelling.")
            self.story_job_wrapper()
        elif cmd == "run image tweet":
            logging.info(f"🚀 Bot {self.name}: 'run image tweet' command received.")
            for adapter in self.platform_adapters.values():
                adapter.post_tweet_with_image()
        elif cmd == "run adaptive tune":
            logging.info(f"🚀 Bot {self.name}: Running adaptive tuning based on engagement metrics.")
            for adapter in self.platform_adapters.values():
                adapter.adaptive_tune()
        elif cmd == "show metrics":
            if os.path.exists(self.engagement_metrics_file):
                try:
                    with open(self.engagement_metrics_file, "r") as f:
                        metrics = json.load(f)
                    print(f"Engagement Metrics for {self.name}: {metrics}")
                except Exception as e:
                    print("Error reading engagement metrics.")
            else:
                print("No engagement metrics recorded yet.")
        elif cmd.startswith("set mood "):
            mood = cmd.split("set mood ")[1].strip()
            self.mood_state = mood
            logging.info(f"✅ Bot {self.name}: Mood manually set to {self.mood_state}.")
        elif cmd == "show dashboard":
            self.show_dashboard()
        elif cmd == "show settings":
            logging.info(f"🔧 Bot {self.name}: Current settings: Post Count = {getattr(self, 'post_run_count', 'N/A')}, Comment Count = {getattr(self, 'comment_run_count', 'N/A')}, Reply Count = {getattr(self, 'reply_run_count', 'N/A')}")
        elif cmd == "show listener":
            self.show_listener_state()
        elif cmd == "show log":
            self.show_log()
        elif cmd in ["help", "?"]:
            self.print_help()
        else:
            logging.info("❓ Unrecognized command. Valid commands:")
            self.print_help()

        print("\nCommand completed. Returning to bot console.\n")
        input("Press Enter to continue...")

    def print_help(self):
        help_text = (
            "Available bot commands:\n"
            "  start                - Start this bot\n"
            "  stop                 - Stop this bot\n"
            "  new auth             - Delete token file (force reauthentication on next startup)\n"
            "  auth age             - Show time remaining until token expiration (assumed 90 days)\n"
            "  run post             - Post on all platforms immediately.\n"
            "  run comment          - Comment on all platforms immediately.\n"
            "  run reply            - Reply on all platforms immediately.\n"
            "  set post count <num> - Set number of posts to run.\n"
            "  set comment count <num> - Set number of comments to run.\n"
            "  set reply count <num>   - Set number of replies to run.\n"
            "  list context         - List available contexts from the configuration.\n"
            "  run context {name}   - Run a specific context for testing.\n"
            "  new random all       - Randomize times for post, comment, and reply.\n"
            "  new random post      - Randomize time for post.\n"
            "  new random comment   - Randomize time for comment.\n"
            "  new random reply     - Randomize time for reply.\n"
            "  stop post            - Stop the auto post function.\n"
            "  start post           - Start the auto post function.\n"
            "  stop comment         - Stop the auto comment function.\n"
            "  start comment        - Start the auto comment function.\n"
            "  stop reply           - Stop the auto reply function.\n"
            "  start reply          - Start the auto reply function.\n"
            "  start cross          - Enable auto cross-platform engagement.\n"
            "  stop cross           - Disable auto cross-platform engagement.\n"
            "  start trending       - Enable auto trending engagement.\n"
            "  stop trending        - Disable auto trending engagement.\n"
            "  start dm             - Enable auto DM checking.\n"
            "  stop dm              - Disable auto DM checking.\n"
            "  run dm {username}    - Send a DM to a specified user.\n"
            "  start story          - Enable auto collaborative storytelling.\n"
            "  stop story           - Disable auto collaborative storytelling.\n"
            "  run story            - Run a collaborative storytelling post immediately.\n"
            "  run image tweet      - Post on all platforms with an auto-generated image (and audio if engagement is high).\n"
            "  run adaptive tune    - Adjust parameters based on engagement metrics.\n"
            "  show metrics         - Display last engagement metrics.\n"
            "  set mood {mood}      - Manually set the bot's mood (e.g., happy, neutral, serious).\n"
            "  show dashboard       - Display a summary dashboard.\n"
            "  show settings        - Display current settings.\n"
            "  show listener        - Display next scheduled job times.\n"
            "  show log             - Display scheduled log info.\n"
            "  help or ?            - Show this help message.\n"
            "  back                 - Return to master console."
        )
        print(help_text)

    def print_next_scheduled_times(self):
        now = datetime.datetime.now()
        if not self.scheduler.jobs:
            logging.info(f"🗓️ Bot {self.name}: No scheduled jobs.")
            return
        for job in self.scheduler.jobs:
            if job.next_run:
                diff = job.next_run - now
                logging.info(f"🗓️ Bot {self.name}: Job {job.tags} scheduled to run in {diff} (at {job.next_run.strftime('%Y-%m-%d %H:%M:%S')}).")

    def show_listener_state(self):
        logging.info(f"👂 Bot {self.name}: Console listener active.")
        logging.info("Type 'help' or '?' for command list.")
        self.print_next_scheduled_times()

    def show_log(self):
        now = datetime.datetime.now()
        output = [f"Status: {self.get_status()}"]
        if self.auto_post_enabled:
            post_jobs = [job for job in self.scheduler.jobs if "randomized_post" in job.tags]
            if post_jobs and post_jobs[0].next_run:
                diff_post = post_jobs[0].next_run - now
                output.append(f"📝 Bot {self.name}: Auto post ENABLED; Next post in: {diff_post} (at {post_jobs[0].next_run.strftime('%Y-%m-%d %H:%M:%S')})")
            else:
                output.append(f"📝 Bot {self.name}: Auto post ENABLED but no scheduled job.")
        else:
            output.append(f"📝 Bot {self.name}: Auto post DISABLED.")
        if self.auto_comment_enabled:
            comment_jobs = [job for job in self.scheduler.jobs if "randomized_comment" in job.tags]
            if comment_jobs and comment_jobs[0].next_run:
                diff_comment = comment_jobs[0].next_run - now
                output.append(f"💬 Bot {self.name}: Auto comment ENABLED; Next comment in: {diff_comment} (at {comment_jobs[0].next_run.strftime('%Y-%m-%d %H:%M:%S')})")
            else:
                output.append(f"💬 Bot {self.name}: Auto comment ENABLED but no scheduled job.")
        else:
            output.append(f"💬 Bot {self.name}: Auto comment DISABLED.")
        if self.auto_reply_enabled:
            reply_jobs = [job for job in self.scheduler.jobs if "randomized_reply" in job.tags]
            if reply_jobs and reply_jobs[0].next_run:
                diff_reply = reply_jobs[0].next_run - now
                output.append(f"🗓️ Bot {self.name}: Auto reply ENABLED; Next reply in: {diff_reply} (at {reply_jobs[0].next_run.strftime('%Y-%m-%d %H:%M:%S')})")
            else:
                output.append(f"🗓️ Bot {self.name}: Auto reply ENABLED but no scheduled job.")
        else:
            output.append(f"🗓️ Bot {self.name}: Auto reply DISABLED.")
        print("\n".join(output))

    # END Bot Console Commands

    # Wrapper methods for scheduling
    def post_job_wrapper(self):
        for _ in range(self.post_run_count):
            self.daily_tweet_job()

    def comment_job_wrapper(self):
        for _ in range(self.comment_run_count):
            # Assuming a method daily_comment_job exists similar to tweet job
            self.daily_comment_job()

    def reply_job_wrapper(self):
        for _ in range(self.reply_run_count):
            # Assuming a method daily_comment_reply_job exists similar to tweet job
            self.daily_comment_reply_job()

    def schedule_next_post_job(self):
        # For simplicity, schedule a post job at a random time between 12:00 and 18:00
        post_times = self.config.get("schedule", {}).get("tweet_times", ["12:00", "18:00"])
        random_post_time = random.choice(post_times)
        self.scheduler.every().day.at(random_post_time).do(self.post_job_wrapper).tag("randomized_post")
        logging.info(f"Bot {self.name}: Next post scheduled at {random_post_time}")

    def schedule_next_comment_job(self):
        comment_times = self.config.get("schedule", {}).get("comment_times", ["13:00", "19:00"])
        random_comment_time = random.choice(comment_times)
        self.scheduler.every().day.at(random_comment_time).do(self.comment_job_wrapper).tag("randomized_comment")
        logging.info(f"Bot {self.name}: Next comment scheduled at {random_comment_time}")

    def schedule_next_reply_job(self):
        reply_times = self.config.get("schedule", {}).get("reply_times", ["14:30", "20:30"])
        random_reply_time = random.choice(reply_times)
        self.scheduler.every().day.at(random_reply_time).do(self.reply_job_wrapper).tag("randomized_reply")
        logging.info(f"Bot {self.name}: Next reply scheduled at {random_reply_time}")

    def re_randomize_schedule(self):
        self.scheduler.clear()
        logging.info(f"Bot {self.name}: Cleared previous randomized jobs.")
        self.schedule_next_post_job()
        self.schedule_next_comment_job()
        self.schedule_next_reply_job()

    def start_scheduler(self):
        while not self._stop_event.is_set():
            self.scheduler.run_pending()
            time.sleep(1)

    def start(self):
        if self.running:
            logging.info(f"Bot {self.name} is already running.")
            return
        self._stop_event.clear()
        if self.flask_thread is None or not self.flask_thread.is_alive():
            self.flask_thread = threading.Thread(target=self.run_flask, daemon=True)
            self.flask_thread.start()
            time.sleep(1)
        self.authenticate()
        self.load_user_id_cache()
        self.load_bot_tweet_cache()
        self.auto_post_enabled = True
        self.auto_comment_enabled = True
        self.auto_reply_enabled = True
        self.re_randomize_schedule()
        self.scheduler_thread = threading.Thread(target=self.start_scheduler, daemon=True)
        self.scheduler_thread.start()
        self.running = True
        logging.info(f"Bot {self.name} started.")

    def stop(self):
        if not self.running:
            logging.info(f"Bot {self.name} is not running.")
            return
        self._stop_event.set()
        self.scheduler.clear()
        self.running = False
        logging.info(f"Bot {self.name} stopped.")
        try:
            requests.post(f"http://localhost:{self.port}/shutdown")
        except Exception as e:
            logging.error(f"Bot {self.name}: Error shutting down Flask server: {e}")

    def get_status(self):
        return "UP" if self.running else "DOWN"

    def get_auth_age(self):
        if not os.path.exists(self.token_file):
            return "No token file found."
        mod_time = os.path.getmtime(self.token_file)
        age = time.time() - mod_time
        remaining = TOKEN_EXPIRY_SECONDS - age
        if remaining < 0:
            return "Token has expired."
        days = int(remaining // (24*3600))
        hours = int((remaining % (24*3600)) // 3600)
        minutes = int((remaining % 3600) // 60)
        seconds = int(remaining % 60)
        return f"Token will expire in {days}d {hours}h {minutes}m {seconds}s."

    def show_dashboard(self):
        now = datetime.datetime.now()
        dashboard = [f"--- Dashboard for Bot {self.name} ---"]
        dashboard.append(f"Status: {self.get_status()}")
        dashboard.append(f"Mood: {self.mood_state}")
        dashboard.append(f"Platform Adapters: {', '.join(self.platform_adapters.keys())}")
        for job in self.scheduler.jobs:
            if job.next_run:
                dashboard.append(f"Job {job.tags} scheduled at {job.next_run.strftime('%Y-%m-%d %H:%M:%S')}")
        if os.path.exists(self.engagement_metrics_file):
            try:
                with open(self.engagement_metrics_file, "r") as f:
                    metrics = json.load(f)
                dashboard.append(f"Last Tweet - Likes: {metrics.get('likes', 0)}, Retweets: {metrics.get('retweets', 0)}")
            except Exception as e:
                dashboard.append("Engagement metrics unavailable.")
        else:
            dashboard.append("No engagement metrics recorded yet.")
        print("\n".join(dashboard))
        logging.info(f"✅ Bot {self.name}: Displayed dashboard.")

if __name__ == "__main__":
    import glob
    from src.platforms.twitter import TwitterAdapter
    from src.platforms.facebook import FacebookAdapter
    from src.platforms.instagram import InstagramAdapter
    from src.platforms.telegram import TelegramAdapter
    from src.platforms.discord import DiscordAdapter

    load_dotenv()
    setup_logging()
    config_files = glob.glob(os.path.join(CONFIGS_DIR, "*.yaml"))
    start_port = 5050
    bots = []
    for idx, config_file in enumerate(config_files):
        bot_name = os.path.splitext(os.path.basename(config_file))[0]
        port = start_port + idx
        bot_instance = Bot(bot_name, config_file, port)
        bot_instance.load_config()
        bot_instance.add_platform_adapter("twitter", TwitterAdapter(bot_instance))
        bot_instance.add_platform_adapter("facebook", FacebookAdapter(bot_instance))
        bot_instance.add_platform_adapter("instagram", InstagramAdapter(bot_instance))
        bot_instance.add_platform_adapter("telegram", TelegramAdapter(bot_instance))
        bot_instance.add_platform_adapter("discord", DiscordAdapter(bot_instance))
        bot_instance.start()
        bots.append(bot_instance)
    print("Loaded bots:")
    for bot in bots:
        print(f"Bot {bot.name} running on port {bot.port}")