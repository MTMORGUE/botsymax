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
import requests
from flask import Flask, request
from jinja2 import Template
from textblob import TextBlob
from pathlib import Path

"""
Botsy: Twitter Bot Service (Multi-Bot Version) – Extended

This version adds new functionality:
  - Cross-bot engagement: bots can retweet/reply to tweets from fellow bots.
  - Persistent conversation memory: saving/retrieving conversation threads.
  - Mood & sentiment modulation: analyzing tweet sentiment and adjusting tone.
  - Trending & hashtag response: fetching trending topics and auto-engaging.
  - Direct messaging: sending and checking DMs for private interactions.
  - Collaborative storytelling: bots co-author narrative threads.
  - Visual/multimedia enhancements: generating images (e.g., via DALL·E) and tweeting with them.
  - Feedback & adaptive tuning: tracking engagement metrics and adjusting prompt parameters.
  - Enhanced dashboard: a simple console-based analytics summary.

Additionally, new capabilities include:
  1. Richer Personality & Emotional Modeling:
      • Dynamic Personality Frameworks (Big Five traits).
      • Complex Emotional States with decay.
  2. Enhanced Contextual Awareness:
      • Extended persistent memory for interactions.
      • Multi-Modal Inputs (weather, calendar, etc.).
  3. Advanced Adaptive Learning:
      • Reinforcement Learning for Engagement.
      • Contextual Re-Training.
  4. More Natural Interactions & Emergent Behavior:
      • Collaborative Multi-Bot Storytelling with shared story state.
      • Conversational Interruptions & Turn-Taking.
      • Simulated “Mistakes” and Corrections.
  5. Multi-Modal Content Generation:
      • Dynamic Visual & Audio Content.
      • Adaptive Multimedia Integration.
  6. Social Graph & Network Interactions:
      • Community-Aware Behavior.
      • Cross-Platform Interaction.

Each bot’s persistent files are stored in its own directory (../bots/<bot_name>).

Requirements: See requirements.txt. (May need additional libraries such as textblob.)
"""

# =============================================================================
# Global Constants (Updated for new directory structure)
# =============================================================================

CONFIGS_DIR = os.path.join(Path(__file__).parent.parent, "configs")  # folder containing each bot's config file
RATE_LIMIT_WAIT = 60         # seconds to wait when a rate limit is hit
ME_CACHE_DURATION = 300      # seconds to cache authenticated user info in memory
TOKEN_EXPIRY_SECONDS = 90 * 24 * 3600  # Tokens expire in 90 days
MAX_AUTH_RETRIES = 3

# Filenames for conversation history and engagement metrics
CONVO_HISTORY_FMT = "conversation_history_{}.json"
ENGAGEMENT_METRICS_FMT = "engagement_metrics_{}.json"


# =============================================================================
# Bot Class Definition
# =============================================================================

class Bot:
    def __init__(self, name: str, config_path: str, port: int):
        self.name = name
        self.config_file = config_path
        self.port = port

        # Create a dedicated directory for this bot in ../bots/<bot_name>
        bots_root = os.path.join(Path(__file__).parent.parent, "bots")
        self.bot_dir = os.path.join(bots_root, self.name)
        if not os.path.exists(self.bot_dir):
            os.makedirs(self.bot_dir)
            logging.info(f"✅ Created directory for bot '{self.name}': {self.bot_dir}")

        # All persistence files will be stored inside self.bot_dir
        self.token_file = os.path.join(self.bot_dir, f"token_{self.name}.json")
        self.user_id_cache_file = os.path.join(self.bot_dir, f"user_id_cache_{self.name}.json")
        self.bot_tweet_cache_file = os.path.join(self.bot_dir, f"bot_tweet_cache_{self.name}.json")
        self.convo_history_file = os.path.join(self.bot_dir, CONVO_HISTORY_FMT.format(self.name))
        self.engagement_metrics_file = os.path.join(self.bot_dir, ENGAGEMENT_METRICS_FMT.format(self.name))
        self.interaction_history_file = os.path.join(self.bot_dir, f"interaction_history_{self.name}.json")

        self.config = {}
        self.client = None

        # OAuth-specific state
        self.oauth_verifier = None
        self.request_token = None

        # Manual run counts
        self.post_run_count = 1
        self.comment_run_count = 1
        self.reply_run_count = 1

        # NEW: Additional run counts for new functionalities
        self.dm_run_count = 1
        self.story_run_count = 1

        # In-memory caches
        self.user_id_cache = {}
        self.bot_tweet_cache = {"tweet_id": None, "timestamp": 0}
        self.monitored_handles_last_ids = {}

        # Scheduler for automated jobs
        self.scheduler = schedule.Scheduler()
        self.app = None

        # Cached authenticated user info
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

        # NEW: Additional auto flags
        self.auto_cross_enabled = False  # cross-bot engagement
        self.auto_trending_enabled = False  # trending topics engagement
        self.auto_dm_enabled = False  # auto DM-checking
        self.auto_story_enabled = False  # collaborative storytelling

        # NEW: Original mood state (used as a summary)
        self.mood_state = "neutral"

        # NEW: For smoothing and momentum in sentiment analysis
        self.sentiment_history = []
        self.sentiment_window = 5

        # NEW: Count consecutive forbidden errors to avoid looping
        self.consecutive_forbidden_count = 0

        # NEW: Rich Personality & Emotional Modeling
        # Default personality parameters inspired by the Big Five
        self.personality = {
            "openness": 0.5,
            "conscientiousness": 0.5,
            "extraversion": 0.5,
            "agreeableness": 0.5,
            "neuroticism": 0.5
        }
        # Multi-dimensional emotional state
        self.emotions = {
            "excitement": 0.0,
            "anxiety": 0.0,
            "nostalgia": 0.0,
            "happiness": 0.0,
            "sadness": 0.0
        }

    @staticmethod
    def clean_tweet_text(text: str) -> str:
        """Clean and truncate tweet text to 280 characters."""
        cleaned = text.strip(" '\"")
        cleaned = re.sub(r'\n+', ' ', cleaned)
        return cleaned[:280]

    def validate_time(self, time_str: str, default: str) -> str:
        """Validate a time string in HH:MM format; return the string if valid, otherwise return the default."""
        pattern = r"^\d{1,2}:\d{2}$"
        if re.match(pattern, time_str):
            return time_str
        else:
            return default

    # -----------------------------
    # TOKEN & CONFIG METHODS
    # -----------------------------
    def save_token(self, token_data: dict) -> None:
        try:
            token_data["created_at"] = time.time()
            with open(self.token_file, "w") as f:
                json.dump(token_data, f)
            logging.info(f"✅ Bot {self.name}: Token saved successfully to {self.token_file}")
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error saving token: {str(e)}")

    def load_token(self) -> dict:
        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Error loading token: {str(e)}")
        return {}

    def call_openai_completion(self, model: str, messages: list, temperature: float,
                               max_tokens: int, top_p: float, frequency_penalty: float,
                               presence_penalty: float) -> str:
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
            # Inject conversational dynamics (interruptions and corrections)
            raw_text = self.add_conversational_dynamics(raw_text)
            return Bot.clean_tweet_text(raw_text)
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error generating OpenAI completion: {str(e)}")
            return ""

    def load_config(self) -> None:
        if not os.path.exists(self.config_file):
            logging.error(f"❌ Bot {self.name}: Config file '{self.config_file}' not found.")
            self.config = {}
            return
        try:
            with open(self.config_file, "r") as file:
                self.config = yaml.safe_load(file)
            logging.info(f"✅ Bot {self.name}: Loaded config from {self.config_file}")
            # If personality settings are provided in the config, update them
            if "personality" in self.config:
                self.personality = self.config["personality"]
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error loading config file: {str(e)}")
            self.config = {}

    def run_flask(self) -> None:
        """Starts a dedicated Flask server for OAuth callback and shutdown."""
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

    def authenticate(self):
        """Authenticate with Twitter, using stored tokens or initiating OAuth flow."""
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
                        access_token=token_data.get("access_token", ""),
                        access_token_secret=token_data.get("access_token_secret", ""),
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

    # -----------------------------
    # USER ID CACHE
    # -----------------------------
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

    def get_user_id(self, username: str):
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

    def get_user_ids_bulk(self, usernames: list):
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

    # -----------------------------
    # BOT TWEET CACHE
    # -----------------------------
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

    def get_bot_recent_tweet_id(self, cache_duration: int = 300):
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

    # -----------------------------
    # EXTERNAL DATA FETCHING (news, weather, etc.)
    # -----------------------------
    def fetch_news(self, keyword: str = None) -> dict:
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

    def fetch_weather(self, location=""):
        # Placeholder weather API simulation
        weather_data = {"temperature": "20°C", "condition": "Sunny"}
        logging.info(f"🔎 Bot {self.name}: Fetched weather data: {weather_data}")
        return weather_data

    def fetch_calendar_events(self):
        # Placeholder for calendar events
        events = [{"event": "Meeting", "time": "15:00"}]
        logging.info(f"🔎 Bot {self.name}: Fetched calendar events: {events}")
        return events

    # -----------------------------
    # PROMPT / TEXT GENERATION
    # -----------------------------
    def add_conversational_dynamics(self, text: str) -> str:
        # Introduce random interruptions or self-corrections
        if random.random() < 0.1:
            text = "Uh-huh, " + text
        if random.random() < 0.05:
            text += " (Correction: Sorry, I misspoke!)"
        return text

    def generate_tweet(self) -> str:
        if not self.config:
            logging.error(f"❌ Bot {self.name}: Configuration is empty or invalid.")
            return ""
        contexts = self.config.get("contexts", {})
        if not contexts:
            logging.error(f"❌ Bot {self.name}: No contexts found in config.")
            return ""

        random_context = random.choice(list(contexts.keys()))
        logging.info(f"🔎 Bot {self.name}: Selected context: {random_context}")
        prompt_settings = contexts[random_context].get("prompt", {})
        if not prompt_settings:
            logging.error(f"❌ Bot {self.name}: No prompt data found for context '{random_context}'.")
            return ""

        system_prompt = prompt_settings.get("system", "")
        user_prompt = prompt_settings.get("user", "")
        model = prompt_settings.get("model", "gpt-4o")
        temperature = prompt_settings.get("temperature", 1)
        max_tokens = prompt_settings.get("max_tokens", 16384)
        top_p = prompt_settings.get("top_p", 1.0)
        frequency_penalty = prompt_settings.get("frequency_penalty", 0.8)
        presence_penalty = prompt_settings.get("presence_penalty", 0.1)

        # Append conversation history (if any)
        conversation = self.load_conversation_history()
        if conversation:
            user_prompt += "\nPrevious conversation: " + conversation

        # Optionally include news, weather, calendar, personality info
        if prompt_settings.get("include_news", False):
            news_keyword = prompt_settings.get("news_keyword", None)
            news_data = self.fetch_news(news_keyword)
            template = Template(user_prompt)
            user_prompt = template.render(news_headline=news_data["headline"],
                                          news_article=news_data["article"],
                                          mood_state=self.mood_state,
                                          personality=self.personality)
        elif prompt_settings.get("include_weather", False):
            weather = self.fetch_weather()
            template = Template(user_prompt)
            user_prompt = template.render(weather=weather,
                                          mood_state=self.mood_state,
                                          personality=self.personality)
        else:
            template = Template(user_prompt)
            user_prompt = template.render(mood_state=self.mood_state, personality=self.personality)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if user_prompt:
            messages.append({"role": "user", "content": user_prompt})

        tweet_text = self.call_openai_completion(
            model, messages, temperature, max_tokens, top_p, frequency_penalty, presence_penalty
        )

        # Save the generated tweet to conversation history
        self.append_conversation_history(tweet_text)
        return self.clean_tweet_text(tweet_text) if tweet_text else ""

    def handle_forbidden_error(self, error) -> bool:
        """
        Handler for 403 Forbidden errors when posting tweets.
        Increments a counter and, if the threshold is exceeded,
        disables auto-posting to prevent an infinite loop.
        """
        self.consecutive_forbidden_count += 1
        logging.error(f"❌ Bot {self.name}: Tweet forbidden by Twitter. (Forbidden count: {self.consecutive_forbidden_count})")
        if self.consecutive_forbidden_count >= 3:
            logging.error(f"❌ Bot {self.name}: Repeated forbidden errors encountered. Disabling auto-posting temporarily.")
            self.auto_post_enabled = False
        return False

    def post_tweet(self) -> bool:
        tweet = self.generate_tweet()
        if not tweet:
            logging.error(f"❌ Bot {self.name}: No tweet generated")
            return False
        try:
            self.client.create_tweet(text=tweet)
            logging.info(f"✅ Bot {self.name}: Tweet posted successfully: {tweet}")
            self.consecutive_forbidden_count = 0  # reset on success
            return True
        except tweepy.TweepyException as e:
            if "403" in str(e):
                return self.handle_forbidden_error(e)
            else:
                logging.error(f"❌ Bot {self.name}: Error posting tweet: {str(e)}")
                return False

    # -----------------------------
    # SCHEDULED TASKS (daily tweet, comment, reply, etc.)
    # -----------------------------
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

    def daily_comment(self):
        logging.info(f"🔎 Bot {self.name}: Checking monitored handles for new tweets...")
        config = self.config
        if not config:
            logging.warning(f"❌ Bot {self.name}: Config empty/invalid.")
            return
        monitored_handles = config.get("monitored_handles", {})
        handles = [handle for handle in monitored_handles.keys() if handle.lower() != "last_id"]
        self.get_user_ids_bulk(handles)
        for handle_name in handles:
            handle_data = monitored_handles.get(handle_name, {})
            user_id = self.get_user_id(handle_name)
            if not user_id:
                logging.warning(f"❌ Bot {self.name}: Could not fetch user_id for '{handle_name}'. Skipping.")
                continue
            last_id = self.monitored_handles_last_ids.get(handle_name, None)
            try:
                tweets_response = self.client.get_users_tweets(
                    id=user_id,
                    since_id=last_id,
                    exclude=["retweets", "replies"],
                    max_results=5,
                    tweet_fields=["id", "text"],
                    user_auth=True
                )
            except tweepy.TooManyRequests:
                logging.warning(f"⚠️ Bot {self.name}: Rate limit hit while fetching tweets for '{handle_name}'. Backing off...")
                time.sleep(RATE_LIMIT_WAIT)
                break
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Error fetching tweets for '{handle_name}': {str(e)}")
                continue
            if not tweets_response or not tweets_response.data:
                logging.info(f"📭 Bot {self.name}: No new tweets from {handle_name}.")
                continue

            newest_tweet = tweets_response.data[0]
            if last_id and newest_tweet.id <= last_id:
                logging.info(f"📭 Bot {self.name}: Already commented or not newer than {last_id}. Skipping.")
                continue
            prompt_data = handle_data.get("response_prompt", {})
            if not prompt_data:
                logging.warning(f"❌ Bot {self.name}: No response_prompt for '{handle_name}'. Skipping.")
                continue

            system_prompt = prompt_data.get("system", "")
            user_prompt_template = prompt_data.get("user", "")
            model = prompt_data.get("model", "gpt-4o")
            temperature = prompt_data.get("temperature", 1)
            max_tokens = prompt_data.get("max_tokens", 16384)
            top_p = prompt_data.get("top_p", 1.0)
            frequency_penalty = prompt_data.get("frequency_penalty", 0.8)
            presence_penalty = prompt_data.get("presence_penalty", 0.1)

            template = Template(user_prompt_template)
            filled_prompt = template.render(tweet_text=newest_tweet.text, mood_state=self.mood_state)
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": filled_prompt})

            reply = self.call_openai_completion(model, messages, temperature, max_tokens, top_p,
                                                frequency_penalty, presence_penalty)
            if reply:
                try:
                    self.client.create_tweet(text=reply, in_reply_to_tweet_id=newest_tweet.id, user_auth=True)
                    logging.info(f"✅ Bot {self.name}: Replied to tweet {newest_tweet.id} by {handle_name}: {reply}")
                    self.monitored_handles_last_ids[handle_name] = newest_tweet.id
                except Exception as e:
                    logging.error(f"❌ Bot {self.name}: Error replying to tweet {newest_tweet.id}: {str(e)}")
            else:
                logging.error(f"❌ Bot {self.name}: Failed to generate reply for tweet {newest_tweet.id}")

    def daily_comment_job(self):
        logging.info(f"⏰ Bot {self.name}: Attempting to auto-comment (scheduled).")
        self.daily_comment()

    def daily_comment_reply(self):
        logging.info(f"🔎 Bot {self.name}: Checking for replies to my tweet...")
        config = self.config
        if not config:
            logging.warning(f"❌ Bot {self.name}: Config empty/invalid.")
            return
        reply_handles = config.get("reply_handles", {})
        if not reply_handles:
            logging.warning(f"❌ Bot {self.name}: No reply handles specified in config. Skipping.")
            return
        self.get_user_ids_bulk(list(reply_handles.keys()))
        for handle_name, handle_data in reply_handles.items():
            user_id = self.get_user_id(handle_name)
            if not user_id:
                logging.warning(f"❌ Bot {self.name}: Could not fetch user_id for '{handle_name}'. Skipping.")
                continue
            try:
                auth_user = self.get_cached_me()
                if not (auth_user and auth_user.data):
                    logging.error(f"❌ Bot {self.name}: Failed to retrieve authenticated user info.")
                    return
                recent_tweet_id = self.get_bot_recent_tweet_id()
                if not recent_tweet_id:
                    logging.info(f"📭 Bot {self.name}: No recent tweet found.")
                    continue
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Error retrieving bot info: {e}")
                continue

            try:
                replies = self.client.search_recent_tweets(
                    query=f"to:{auth_user.data.username}",
                    since_id=recent_tweet_id,
                    max_results=10,
                    tweet_fields=["author_id", "text"],
                    expansions="author_id",
                    user_auth=True
                )
            except tweepy.TooManyRequests:
                logging.warning(f"⚠️ Bot {self.name}: Rate limit hit while fetching replies. Backing off...")
                time.sleep(RATE_LIMIT_WAIT)
                return
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Error fetching replies: {str(e)}")
                return

            if not replies or not replies.data:
                logging.info(f"📭 Bot {self.name}: No replies found for tweet {recent_tweet_id}.")
                continue

            author_users = {user.id: user.username.lower() for user in replies.includes.get("users", [])}
            for reply in replies.data:
                reply_text = reply.text.strip()
                author_handle = author_users.get(reply.author_id, "").lower()
                if author_handle != handle_name.lower():
                    logging.info(f"👀 Bot {self.name}: Ignoring reply from @{author_handle}.")
                    continue
                logging.info(f"📢 Bot {self.name}: Detected reply from @{handle_name}: {reply_text}")

                prompt_data = handle_data.get("response_prompt", {})
                if not prompt_data:
                    logging.warning(f"❌ Bot {self.name}: No response_prompt for '{handle_name}'. Skipping.")
                    continue

                system_prompt = prompt_data.get("system", "")
                user_prompt_template = prompt_data.get("user", "")
                model = prompt_data.get("model", "gpt-4o")
                temperature = prompt_data.get("temperature", 1)
                max_tokens = prompt_data.get("max_tokens", 16384)
                top_p = prompt_data.get("top_p", 1.0)
                frequency_penalty = prompt_data.get("frequency_penalty", 0.8)
                presence_penalty = prompt_data.get("presence_penalty", 0.1)

                try:
                    tweet_response = self.client.get_tweet(recent_tweet_id, tweet_fields=["text"], user_auth=True)
                    bot_tweet_text = tweet_response.data.text if tweet_response and tweet_response.data else ""
                except Exception as e:
                    bot_tweet_text = ""
                    logging.warning(f"❌ Bot {self.name}: Could not fetch my tweet text: {e}")

                template = Template(user_prompt_template)
                filled_prompt = template.render(comment_text=reply_text,
                                                tweet_text=bot_tweet_text,
                                                mood_state=self.mood_state)
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": filled_prompt})

                response_text = self.call_openai_completion(model, messages, temperature, max_tokens, top_p,
                                                            frequency_penalty, presence_penalty)
                if response_text:
                    try:
                        self.client.create_tweet(text=response_text, in_reply_to_tweet_id=reply.id, user_auth=True)
                        logging.info(f"✅ Bot {self.name}: Replied to @{handle_name} on tweet {reply.id}: {response_text}")
                    except Exception as e:
                        logging.error(f"❌ Bot {self.name}: Error replying for tweet {reply.id}: {e}")
                else:
                    logging.error(f"❌ Bot {self.name}: Failed to generate reply for tweet {reply.id}")

    def daily_comment_reply_job(self):
        logging.info(f"⏰ Bot {self.name}: Attempting to auto-reply (scheduled).")
        self.daily_comment_reply()

    # -----------------------------
    # NEW FUNCTIONALITY: Cross-Bot Engagement
    # -----------------------------
    def cross_bot_engagement(self):
        """
        Engage with tweets from fellow bots.
        Looks for tweets from usernames listed under 'bot_network' in the config
        and replies or retweets them to simulate conversation.
        """
        bot_network = self.config.get("bot_network", [])
        if not bot_network:
            logging.info(f"ℹ️ Bot {self.name}: No bot network defined for cross engagement.")
            return
        query = " OR ".join([f"from:{username}" for username in bot_network])
        try:
            results = self.client.search_recent_tweets(
                query=query, max_results=5, tweet_fields=["id", "text"], user_auth=True
            )
            if results and results.data:
                for tweet in results.data:
                    reply_text = f"@{tweet.id} Interesting point!"
                    try:
                        self.client.create_tweet(text=reply_text, in_reply_to_tweet_id=tweet.id, user_auth=True)
                        logging.info(f"✅ Bot {self.name}: Cross-engaged with tweet {tweet.id} from network.")
                    except Exception as e:
                        logging.error(f"❌ Bot {self.name}: Error during cross engagement on tweet {tweet.id}: {e}")
            else:
                logging.info(f"ℹ️ Bot {self.name}: No network tweets found for cross engagement.")
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error during cross engagement: {e}")

    def run_cross_engagement_job(self):
        logging.info(f"⏰ Bot {self.name}: Running cross-bot engagement job.")
        self.cross_bot_engagement()

    # -----------------------------
    # PERSISTENT CONVERSATION MEMORY
    # -----------------------------
    def load_conversation_history(self) -> str:
        if os.path.exists(self.convo_history_file):
            try:
                with open(self.convo_history_file, "r") as f:
                    return f.read()
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Error loading conversation history: {e}")
        return ""

    def append_conversation_history(self, text: str):
        try:
            with open(self.convo_history_file, "a") as f:
                f.write(text + "\n")
            logging.info(f"✅ Bot {self.name}: Appended new text to conversation history.")
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error appending to conversation history: {e}")

    def load_interaction_history(self) -> list:
        if os.path.exists(self.interaction_history_file):
            try:
                with open(self.interaction_history_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Error loading interaction history: {e}")
        return []

    def append_interaction_history(self, entry: dict):
        history = self.load_interaction_history()
        history.append(entry)
        try:
            with open(self.interaction_history_file, "w") as f:
                json.dump(history, f)
            logging.info(f"✅ Bot {self.name}: Appended new interaction history entry.")
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error appending to interaction history: {e}")

    # -----------------------------
    # MOOD & SENTIMENT MODULATION
    # -----------------------------
    def analyze_sentiment(self, text: str) -> float:
        """Return sentiment polarity (-1 to 1) using TextBlob."""
        try:
            blob = TextBlob(text)
            return blob.sentiment.polarity
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error analyzing sentiment: {e}")
            return 0.0

    def get_mood_thresholds(self):
        """
        Retrieve mood thresholds from the YAML configuration if available,
        otherwise return default thresholds.
        """
        default_thresholds = {
            "elated": 0.5,
            "uplifted": 0.3,
            "happy": 0.1,
            "neutral": -0.1,
            "wistful": -0.2,
            "pensive": -0.3,
            "reflective": -0.5,
            "somber": -1.0
        }
        return self.config.get("mood_thresholds", default_thresholds)

    def determine_target_mood(self, avg_sentiment: float) -> str:
        thresholds = self.get_mood_thresholds()
        if avg_sentiment >= thresholds["elated"]:
            return "elated"
        elif avg_sentiment >= thresholds["uplifted"]:
            return "uplifted"
        elif avg_sentiment >= thresholds["happy"]:
            return "happy"
        elif avg_sentiment >= thresholds["neutral"]:
            return "neutral"
        elif avg_sentiment >= thresholds["wistful"]:
            return "wistful"
        elif avg_sentiment >= thresholds["pensive"]:
            return "pensive"
        elif avg_sentiment >= thresholds["reflective"]:
            return "reflective"
        else:
            return "somber"

    def decay_emotions(self, decay_rate=0.1):
        for emotion in self.emotions:
            self.emotions[emotion] *= (1 - decay_rate)

    def compute_overall_mood(self):
        pos = self.emotions["excitement"] + self.emotions["happiness"]
        neg = self.emotions["anxiety"] + self.emotions["sadness"]
        if pos > neg + 0.1:
            return "happy"
        elif neg > pos + 0.1:
            return "sad"
        else:
            return "neutral"

    def update_mood_based_on_input(self, input_text: str):
        sentiment = self.analyze_sentiment(input_text)
        emotion_list = [
            "happy", "sad", "angry", "fearful", "surprised", "disgusted", "calm",
            "sarcastic", "vindictive", "vengeful", "jealous", "spiteful", "embarrassed",
            "playful", "anxious", "excited", "nostalgic", "hopeful", "confused", "proud",
            "envious", "apologetic"
        ]
        new_emotions = {emotion: 0.0 for emotion in emotion_list}
        lower_text = input_text.lower()

        if "lol" in lower_text or "haha" in lower_text:
            new_emotions["playful"] = 0.8
        if "yeah right" in lower_text or "as if" in lower_text:
            new_emotions["sarcastic"] = 0.7
        if "stupid" in lower_text or "idiot" in lower_text:
            new_emotions["angry"] = 0.5
        if "can't believe" in lower_text:
            new_emotions["surprised"] = 0.6
        if "i'm so jealous" in lower_text:
            new_emotions["jealous"] = 0.9
        if "revenge" in lower_text or "get even" in lower_text:
            new_emotions["vengeful"] = 0.8
        if "spite" in lower_text:
            new_emotions["spiteful"] = 0.7
        if "embarrassed" in lower_text:
            new_emotions["embarrassed"] = 0.6

        alpha = 0.7
        growth_factor = 1.05
        for emotion in emotion_list:
            current_val = self.emotions.get(emotion, 0.0)
            detected = new_emotions[emotion]
            blended = alpha * current_val + (1 - alpha) * detected
            if detected > 0.7:
                blended *= growth_factor
            self.emotions[emotion] = min(blended, 1.0)

        self.append_interaction_history({
            "timestamp": datetime.datetime.now().isoformat(),
            "input": input_text,
            "detected_emotions": new_emotions,
            "updated_emotions": self.emotions.copy()
        })
        previous_mood = self.mood_state
        self.mood_state = self.compute_overall_mood()
        logging.info(f"Updated emotions: {self.emotions} (Overall mood: {self.mood_state})")
        self.append_mood_history_entry(input_text, sentiment, previous_mood, self.mood_state)

    def update_behavioral_profile(self):
        if self.emotions.get("vindictive", 0.0) > 0.5 or self.emotions.get("vengeful", 0.0) > 0.5:
            self.personality["agreeableness"] = max(0.0, self.personality["agreeableness"] - 0.05)
        if self.emotions.get("playful", 0.0) > 0.5 or self.emotions.get("excited", 0.0) > 0.5:
            self.personality["extraversion"] = min(1.0, self.personality["extraversion"] + 0.05)
        if self.emotions.get("jealous", 0.0) > 0.6:
            self.personality["neuroticism"] = min(1.0, self.personality["neuroticism"] + 0.05)
        logging.info(f"Updated personality: {self.personality}")

    def update_emotional_and_behavioral_state(self, input_text: str):
        self.update_mood_based_on_input(input_text)
        self.update_behavioral_profile()

    def append_mood_history_entry(self, input_text: str, avg_sentiment: float, previous_mood: str, new_mood: str):
        entry = {
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "input_text": input_text,
            "average_sentiment": avg_sentiment,
            "previous_mood": previous_mood,
            "new_mood": new_mood
        }
        mood_history_file = os.path.join(self.bot_dir, f"mood_history_{self.name}.json")
        try:
            with open(mood_history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            logging.info(f"✅ Bot {self.name}: Logged mood transition at {entry['timestamp']}")
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error logging mood history: {e}")

    def load_mood_history_entries(self) -> list:
        entries = []
        mood_history_file = os.path.join(self.bot_dir, f"mood_history_{self.name}.json")
        if os.path.exists(mood_history_file):
            try:
                with open(mood_history_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            entries.append(json.loads(line))
                logging.info(f"✅ Bot {self.name}: Loaded {len(entries)} mood history entries.")
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Error loading mood history: {e}")
        return entries

    # -----------------------------
    # TRENDING TOPICS ENGAGEMENT
    # -----------------------------
    def fetch_trending_topics(self) -> list:
        trending = ["#AI", "#OpenAI", "#TechNews", "#Python"]
        logging.info(f"🔎 Bot {self.name}: Fetched trending topics: {', '.join(trending)}")
        return trending

    def run_trending_engagement(self):
        trending = self.fetch_trending_topics()
        if not trending:
            logging.info(f"ℹ️ Bot {self.name}: No trending topics available.")
            return
        topic = random.choice(trending)
        prompt = (f"Write an insightful tweet about the trending topic {topic}. "
                  f"Current mood: {self.mood_state}. Personality: {self.personality}")
        messages = [{"role": "user", "content": prompt}]
        tweet_text = self.call_openai_completion("gpt-4o", messages, 1, 100, 1.0, 0.8, 0.1)
        if tweet_text:
            try:
                self.client.create_tweet(text=tweet_text)
                logging.info(f"✅ Bot {self.name}: Tweeted about trending topic {topic}: {tweet_text}")
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Error tweeting trending topic: {e}")

    # -----------------------------
    # DIRECT MESSAGING (DM)
    # -----------------------------
    def send_dm(self, recipient_username: str, message: str):
        """
        Placeholder DM sending method.
        """
        try:
            logging.info(f"✅ Bot {self.name}: Sent DM to @{recipient_username}: {message}")
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error sending DM: {e}")

    def check_dms(self):
        """
        Placeholder for checking new direct messages.
        """
        try:
            logging.info(f"🔎 Bot {self.name}: Checked DMs – no new messages found.")
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error checking DMs: {e}")

    def run_dm_job(self):
        logging.info(f"⏰ Bot {self.name}: Running DM check job.")
        self.check_dms()

    # -----------------------------
    # COLLABORATIVE STORYTELLING
    # -----------------------------
    def load_shared_story_state(self):
        shared_file = os.path.join(Path(__file__).parent.parent, "shared", "story_state.json")
        if os.path.exists(shared_file):
            try:
                with open(shared_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Error loading shared story state: {e}")
        return {"story": ""}

    def update_shared_story_state(self, new_content: str):
        shared_file = os.path.join(Path(__file__).parent.parent, "shared", "story_state.json")
        state = self.load_shared_story_state()
        state["story"] += "\n" + new_content
        try:
            with open(shared_file, "w") as f:
                json.dump(state, f)
            logging.info(f"✅ Bot {self.name}: Updated shared story state.")
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error updating shared story state: {e}")

    def run_collaborative_storytelling(self):
        shared_state = self.load_shared_story_state().get("story", "")
        prompt = (f"Continue the collaborative story with a new tweet. "
                  f"Current mood: {self.mood_state}. Include a plot twist. "
                  f"Previous story: {shared_state}")
        messages = [{"role": "user", "content": prompt}]
        story_tweet = self.call_openai_completion("gpt-4o", messages, 1, 150, 1.0, 0.8, 0.1)
        if story_tweet:
            try:
                self.client.create_tweet(text=story_tweet)
                logging.info(f"✅ Bot {self.name}: Posted a collaborative storytelling tweet: {story_tweet}")
                self.append_conversation_history(story_tweet)
                self.update_shared_story_state(story_tweet)
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Error posting storytelling tweet: {e}")

    # -----------------------------
    # VISUAL/MULTIMEDIA ENHANCEMENTS
    # -----------------------------
    def generate_image(self, prompt: str) -> str:
        """
        Placeholder image-generation method.
        """
        image_url = "https://via.placeholder.com/500.png?text=Generated+Image"
        logging.info(f"🔗 Bot {self.name}: Generated image for prompt '{prompt}': {image_url}")
        return image_url

    def generate_audio(self, prompt: str) -> str:
        """
        Placeholder audio-generation method.
        """
        audio_url = "https://via.placeholder.com/audio_clip.mp3?text=Generated+Audio"
        logging.info(f"🔗 Bot {self.name}: Generated audio for prompt '{prompt}': {audio_url}")
        return audio_url

    def post_tweet_with_image(self):
        tweet = self.generate_tweet()
        if not tweet:
            logging.error(f"❌ Bot {self.name}: No tweet generated for image tweet")
            return False
        image_prompt = self.config.get("image_prompt", f"Generate an image for tweet: {tweet}")
        image_url = self.generate_image(image_prompt)
        tweet_with_image = tweet + f"\nImage: {image_url}"
        # Adaptive Multimedia Integration: If engagement is high, add an audio clip.
        metrics = self.track_engagement_metrics()
        if metrics["likes"] > 75:
            audio_url = self.generate_audio("Audio for tweet: " + tweet)
            tweet_with_image += f"\nAudio: {audio_url}"
        try:
            self.client.create_tweet(text=tweet_with_image)
            logging.info(f"✅ Bot {self.name}: Tweet with image (and possibly audio) posted successfully.")
            return True
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error posting tweet with image: {e}")
            return False

    # -----------------------------
    # FEEDBACK LOOP & ADAPTIVE LEARNING
    # -----------------------------
    def track_engagement_metrics(self):
        """
        Track engagement metrics for the most recent tweet (simulation).
        """
        metrics = {"likes": random.randint(0, 100), "retweets": random.randint(0, 50)}
        try:
            with open(self.engagement_metrics_file, "w") as f:
                json.dump(metrics, f)
            logging.info(f"✅ Bot {self.name}: Updated engagement metrics: {metrics}")
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error saving engagement metrics: {e}")
        return metrics

    def adaptive_tune(self):
        metrics = self.track_engagement_metrics()
        if metrics["likes"] > 50:
            new_temp = max(0.5, 1 - (metrics["likes"] / 200))
        else:
            new_temp = min(1.5, 1 + (50 - metrics["likes"]) / 100)
        logging.info(f"🔄 Bot {self.name}: Adaptive tuning set temperature to {new_temp:.2f} based on engagement.")
        self.reinforcement_learning_update()

    def reinforcement_learning_update(self):
        metrics = self.track_engagement_metrics()
        if metrics["likes"] > 50:
            self.personality["extraversion"] = min(1.0, self.personality["extraversion"] + 0.05)
        else:
            self.personality["extraversion"] = max(0.0, self.personality["extraversion"] - 0.05)
        logging.info(f"🔄 Bot {self.name}: Updated personality via reinforcement learning: {self.personality}")

    def contextual_retraining(self):
        logging.info(f"🔄 Bot {self.name}: Contextual re-training executed based on conversation and engagement history.")

    # -----------------------------
    # MONITORING & ANALYTICS DASHBOARD
    # -----------------------------
    def show_dashboard(self):
        now = datetime.datetime.now()
        dashboard = [f"--- Dashboard for Bot {self.name} ---"]
        dashboard.append(f"Status: {self.get_status()}")
        dashboard.append(f"Mood: {self.mood_state}")
        dashboard.append(f"Personality: {self.personality}")
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

    # -----------------------------
    # SCHEDULING & WRAPPER METHODS
    # -----------------------------
    def tweet_job_wrapper(self):
        self.daily_tweet_job()
        self.scheduler.clear("randomized_tweet")
        if self.auto_post_enabled:
            self.schedule_next_tweet_job()

    def comment_job_wrapper(self):
        self.daily_comment_job()
        self.scheduler.clear("randomized_comment")
        if self.auto_comment_enabled:
            self.schedule_next_comment_job()

    def reply_job_wrapper(self):
        self.daily_comment_reply_job()
        self.scheduler.clear("randomized_reply")
        if self.auto_reply_enabled:
            self.schedule_next_reply_job()

    def cross_job_wrapper(self):
        self.run_cross_engagement_job()
        self.scheduler.clear("cross_engagement")
        if self.auto_cross_enabled:
            self.scheduler.every(1).hours.do(self.cross_job_wrapper).tag("cross_engagement")
            logging.info(f"Bot {self.name}: Next cross-bot engagement scheduled in 1 hour.")

    def trending_job_wrapper(self):
        self.run_trending_engagement()
        self.scheduler.clear("trending_engagement")
        if self.auto_trending_enabled:
            self.scheduler.every().day.at("11:00").do(self.trending_job_wrapper).tag("trending_engagement")
            logging.info(f"Bot {self.name}: Next trending engagement scheduled at 11:00.")

    def dm_job_wrapper(self):
        self.run_dm_job()
        self.scheduler.clear("dm_job")
        if self.auto_dm_enabled:
            self.scheduler.every(30).minutes.do(self.dm_job_wrapper).tag("dm_job")
            logging.info(f"Bot {self.name}: Next DM check scheduled in 30 minutes.")

    def story_job_wrapper(self):
        self.run_collaborative_storytelling()
        self.scheduler.clear("story_job")
        if self.auto_story_enabled:
            self.scheduler.every().day.at("16:00").do(self.story_job_wrapper).tag("story_job")
            logging.info(f"Bot {self.name}: Next storytelling tweet scheduled at 16:00.")

    def schedule_next_tweet_job(self):
        if not self.auto_post_enabled:
            return
        rng = random.Random()
        rng.seed(f"{self.name}_tweet_{time.time()}")
        tweet_times = self.config.get("schedule", {}).get("tweet_times", ["12:00", "18:00"])
        random_tweet_time = rng.choice(tweet_times)
        random_tweet_time = self.validate_time(random_tweet_time, "12:00")
        self.scheduler.every().day.at(random_tweet_time).do(self.tweet_job_wrapper).tag("randomized_tweet")
        logging.info(f"Bot {self.name}: Next tweet scheduled at {random_tweet_time}")

    def schedule_next_comment_job(self):
        if not self.auto_comment_enabled:
            return
        rng = random.Random()
        rng.seed(f"{self.name}_comment_{time.time()}")
        comment_times = self.config.get("schedule", {}).get("comment_times", ["13:00", "19:00"])
        random_comment_time = rng.choice(comment_times)
        random_comment_time = self.validate_time(random_comment_time, "13:00")
        self.scheduler.every().day.at(random_comment_time).do(self.comment_job_wrapper).tag("randomized_comment")
        logging.info(f"Bot {self.name}: Next comment scheduled at {random_comment_time}")

    def schedule_next_reply_job(self):
        if not self.auto_reply_enabled:
            return
        rng = random.Random()
        rng.seed(f"{self.name}_reply_{time.time()}")
        reply_times = self.config.get("schedule", {}).get("reply_times", ["14:30", "20:30"])
        random_reply_time = rng.choice(reply_times)
        random_reply_time = self.validate_time(random_reply_time, "14:30")
        self.scheduler.every().day.at(random_reply_time).do(self.reply_job_wrapper).tag("randomized_reply")
        logging.info(f"Bot {self.name}: Next reply scheduled at {random_reply_time}")

    def randomize_schedule(self):
        self.schedule_next_tweet_job()
        self.schedule_next_comment_job()
        self.schedule_next_reply_job()
        if self.auto_cross_enabled:
            self.scheduler.every(1).hours.do(self.cross_job_wrapper).tag("cross_engagement")
            logging.info(f"Bot {self.name}: Cross-bot engagement scheduled every hour.")
        if self.auto_trending_enabled:
            self.scheduler.every().day.at("11:00").do(self.trending_job_wrapper).tag("trending_engagement")
            logging.info(f"Bot {self.name}: Trending engagement scheduled at 11:00 daily.")
        if self.auto_dm_enabled:
            self.scheduler.every(30).minutes.do(self.dm_job_wrapper).tag("dm_job")
            logging.info(f"Bot {self.name}: DM check scheduled every 30 minutes.")
        if self.auto_story_enabled:
            self.scheduler.every().day.at("16:00").do(self.story_job_wrapper).tag("story_job")
            logging.info(f"Bot {self.name}: Collaborative storytelling scheduled at 16:00 daily.")

    def re_randomize_schedule(self):
        self.scheduler.clear()
        logging.info(f"Bot {self.name}: Cleared previous randomized jobs.")
        self.randomize_schedule()

    def start_scheduler(self):
        while not self._stop_event.is_set():
            self.scheduler.run_pending()
            time.sleep(1)

    # -----------------------------
    # START / STOP
    # -----------------------------
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
        self.auto_cross_enabled = False
        self.auto_trending_enabled = False
        self.auto_dm_enabled = False
        self.auto_story_enabled = False
        self.randomize_schedule()
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

    def get_status(self) -> str:
        return "UP" if self.running else "DOWN"

    def get_auth_age(self) -> str:
        if not os.path.exists(self.token_file):
            return "No token file found."
        mod_time = os.path.getmtime(self.token_file)
        age = time.time() - mod_time
        remaining = TOKEN_EXPIRY_SECONDS - age
        if remaining < 0:
            return "Token has expired."
        days = int(remaining // (24 * 3600))
        hours = int((remaining % (24 * 3600)) // 3600)
        minutes = int((remaining % 3600) // 60)
        seconds = int(remaining % 60)
        return f"Token will expire in {days}d {hours}h {minutes}m {seconds}s."

    # -----------------------------
    # CONSOLE COMMAND PROCESSING
    # -----------------------------
    def process_console_command(self, cmd: str):
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
            logging.info(f"🚀 Bot {self.name}: 'run post' command received. Posting tweet {self.post_run_count} time(s).")
            for _ in range(self.post_run_count):
                self.daily_tweet_job()
        elif cmd.startswith("run comment"):
            logging.info(f"🚀 Bot {self.name}: 'run comment' command received. Commenting {self.comment_run_count} time(s).")
            for _ in range(self.comment_run_count):
                self.daily_comment_job()
        elif cmd.startswith("run reply"):
            logging.info(f"🚀 Bot {self.name}: 'run reply' command received. Replying {self.reply_run_count} time(s).")
            for _ in range(self.reply_run_count):
                self.daily_comment_reply_job()
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
        elif cmd.startswith("set dm count "):
            try:
                value = int(cmd.split("set dm count ")[1])
                self.dm_run_count = value
                logging.info(f"✅ Bot {self.name}: Set DM run count to {self.dm_run_count}")
            except Exception:
                logging.error(f"❌ Bot {self.name}: Invalid value for DM run count")
        elif cmd.startswith("set story count "):
            try:
                value = int(cmd.split("set story count ")[1])
                self.story_run_count = value
                logging.info(f"✅ Bot {self.name}: Set story run count to {self.story_run_count}")
            except Exception:
                logging.error(f"❌ Bot {self.name}: Invalid value for story run count")
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
                            user_prompt = template.render(news_headline=news_data["headline"],
                                                          news_article=news_data["article"],
                                                          mood_state=self.mood_state)
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
            self.scheduler.clear("randomized_tweet")
            if self.auto_post_enabled:
                self.schedule_next_tweet_job()
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
                self.scheduler.clear("randomized_tweet")
                self.auto_post_enabled = False
                logging.info(f"🚫 Bot {self.name}: Auto post disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto post is already disabled.")
        elif cmd == "start post":
            if not self.auto_post_enabled:
                self.auto_post_enabled = True
                self.schedule_next_tweet_job()
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
            if not self.auto_cross_enabled:
                self.auto_cross_enabled = True
                self.scheduler.every(1).hours.do(self.cross_job_wrapper).tag("cross_engagement")
                logging.info(f"✅ Bot {self.name}: Auto cross-bot engagement enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto cross engagement is already enabled.")
        elif cmd == "stop cross":
            if self.auto_cross_enabled:
                self.scheduler.clear("cross_engagement")
                self.auto_cross_enabled = False
                logging.info(f"🚫 Bot {self.name}: Auto cross-bot engagement disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto cross engagement is already disabled.")
        elif cmd == "start trending":
            if not self.auto_trending_enabled:
                self.auto_trending_enabled = True
                self.scheduler.every().day.at("11:00").do(self.trending_job_wrapper).tag("trending_engagement")
                logging.info(f"✅ Bot {self.name}: Auto trending engagement enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto trending engagement is already enabled.")
        elif cmd == "stop trending":
            if self.auto_trending_enabled:
                self.scheduler.clear("trending_engagement")
                self.auto_trending_enabled = False
                logging.info(f"🚫 Bot {self.name}: Auto trending engagement disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto trending engagement is already disabled.")
        elif cmd == "start dm":
            if not self.auto_dm_enabled:
                self.auto_dm_enabled = True
                self.scheduler.every(30).minutes.do(self.dm_job_wrapper).tag("dm_job")
                logging.info(f"✅ Bot {self.name}: Auto DM check enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto DM check is already enabled.")
        elif cmd == "stop dm":
            if self.auto_dm_enabled:
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
                self.send_dm(recipient, message)
        elif cmd == "start story":
            if not self.auto_story_enabled:
                self.auto_story_enabled = True
                self.scheduler.every().day.at("16:00").do(self.story_job_wrapper).tag("story_job")
                logging.info(f"✅ Bot {self.name}: Auto collaborative storytelling enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto collaborative storytelling is already enabled.")
        elif cmd == "stop story":
            if self.auto_story_enabled:
                self.scheduler.clear("story_job")
                self.auto_story_enabled = False
                logging.info(f"🚫 Bot {self.name}: Auto collaborative storytelling disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto collaborative storytelling is already disabled.")
        elif cmd.startswith("run story"):
            logging.info(f"🚀 Bot {self.name}: 'run story' command received. Running storytelling {self.story_run_count} time(s).")
            for _ in range(self.story_run_count):
                self.run_collaborative_storytelling()
        elif cmd == "run image tweet":
            logging.info(f"🚀 Bot {self.name}: 'run image tweet' command received.")
            self.post_tweet_with_image()
        elif cmd == "run adaptive tune":
            logging.info(f"🚀 Bot {self.name}: Running adaptive tuning based on engagement metrics.")
            self.adaptive_tune()
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
            logging.info(f"🔧 Bot {self.name}: Current settings: Post Count = {self.post_run_count}, Comment Count = {self.comment_run_count}, Reply Count = {self.reply_run_count}")
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
            "  run post             - Post a tweet immediately.\n"
            "  run comment          - Check and comment on new tweets.\n"
            "  run reply            - Check and reply to replies.\n"
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
            "  start cross          - Enable auto cross-bot engagement.\n"
            "  stop cross           - Disable auto cross-bot engagement.\n"
            "  start trending       - Enable auto trending engagement.\n"
            "  stop trending        - Disable auto trending engagement.\n"
            "  start dm             - Enable auto DM checking.\n"
            "  stop dm              - Disable auto DM checking.\n"
            "  run dm {username}    - Send a DM to a specified user.\n"
            "  start story          - Enable auto collaborative storytelling.\n"
            "  stop story           - Disable auto collaborative storytelling.\n"
            "  run story            - Run a storytelling tweet immediately.\n"
            "  run image tweet      - Post a tweet with an auto-generated image (and audio if engagement is high).\n"
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
            post_jobs = [job for job in self.scheduler.jobs if "randomized_tweet" in job.tags]
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

# End of Bot class