import os
import sys
import json
import logging
import threading
import time
import re
import random
import datetime
import schedule
import pytz
import openai
import yaml
import requests
from flask import Flask, request
from pathlib import Path
from src.utils import setup_logging
from src.platforms.base_adapter import BasePlatformAdapter
from jinja2 import Template
from textblob import TextBlob

# Setup logging
setup_logging()

# Global constants
CONFIGS_DIR = os.path.join(Path(__file__).parent.parent, "configs")
TOKEN_EXPIRY_SECONDS = 90 * 24 * 3600  # Tokens expire in 90 days
MAX_AUTH_RETRIES = 3
CONVO_HISTORY_FMT = "conversation_history_{}.json"
ENGAGEMENT_METRICS_FMT = "engagement_metrics_{}.json"

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

        # Persistence files
        self.token_file = os.path.join(self.bot_dir, f"token_{self.name}.json")
        self.convo_history_file = os.path.join(self.bot_dir, CONVO_HISTORY_FMT.format(self.name))
        self.engagement_metrics_file = os.path.join(self.bot_dir, ENGAGEMENT_METRICS_FMT.format(self.name))
        self.interaction_history_file = os.path.join(self.bot_dir, f"interaction_history_{self.name}.json")
        self.mood_history_file = os.path.join(self.bot_dir, f"mood_history_{self.name}.json")

        self.config = {}
        self.platform_adapters = {}

        # Scheduler and Flask server
        self.scheduler = schedule.Scheduler()
        self.app = None

        # Control flags and thread handles
        self.running = False
        self._stop_event = threading.Event()
        self.flask_thread = None
        self.scheduler_thread = None

        # Auto functions enabled flags (for post, comment, reply, etc.)
        self.auto_post_enabled = True
        self.auto_comment_enabled = True
        self.auto_reply_enabled = True
        self.auto_cross_enabled = False
        self.auto_trending_enabled = False
        self.auto_dm_enabled = False
        self.auto_story_enabled = False

        # Mood and sentiment state
        self.mood_state = "neutral"
        self.sentiment_history = []
        self.sentiment_window = 5
        self.consecutive_forbidden_count = 0

        # Personality and emotional state (for adaptive learning)
        self.personality = {
            "openness": 0.5,
            "conscientiousness": 0.5,
            "extraversion": 0.5,
            "agreeableness": 0.5,
            "neuroticism": 0.5
        }
        self.emotions = {
            "excitement": 0.0,
            "anxiety": 0.0,
            "nostalgia": 0.0,
            "happiness": 0.0,
            "sadness": 0.0
        }

    # =============================
    # Universal Utility Functions
    # =============================
    @staticmethod
    def clean_text(text: str) -> str:
        """Clean and truncate text to 280 characters."""
        cleaned = text.strip(" '\"")
        cleaned = re.sub(r'\n+', ' ', cleaned)
        return cleaned[:280]

    def validate_time(self, time_str: str, default: str) -> str:
        pattern = r"^\d{1,2}:\d{2}$"
        return time_str if re.match(pattern, time_str) else default

    def save_token(self, token_data: dict) -> None:
        try:
            token_data["created_at"] = time.time()
            with open(self.token_file, "w") as f:
                json.dump(token_data, f)
            logging.info(f"✅ Bot {self.name}: Token saved successfully to {self.token_file}")
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error saving token: {e}")

    def load_token(self) -> dict:
        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Error loading token: {e}")
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
            raw_text = self.add_conversational_dynamics(raw_text)
            return Bot.clean_text(raw_text)
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error generating OpenAI completion: {e}")
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
            if "personality" in self.config:
                self.personality = self.config["personality"]
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error loading config file: {e}")
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

    # =============================
    # Mood & Sentiment Modulation
    # =============================
    def add_conversational_dynamics(self, text: str) -> str:
        if random.random() < 0.1:
            text = "Uh-huh, " + text
        if random.random() < 0.05:
            text += " (Correction: Sorry, I misspoke!)"
        return text

    def analyze_sentiment(self, text: str) -> float:
        try:
            blob = TextBlob(text)
            return blob.sentiment.polarity
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error analyzing sentiment: {e}")
            return 0.0

    def get_mood_thresholds(self):
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
        try:
            with open(self.mood_history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            logging.info(f"✅ Bot {self.name}: Logged mood transition at {entry['timestamp']}")
        except Exception as e:
            logging.error(f"❌ Bot {self.name}: Error logging mood history: {e}")

    def load_mood_history_entries(self) -> list:
        entries = []
        if os.path.exists(self.mood_history_file):
            try:
                with open(self.mood_history_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            entries.append(json.loads(line))
                logging.info(f"✅ Bot {self.name}: Loaded {len(entries)} mood history entries.")
            except Exception as e:
                logging.error(f"❌ Bot {self.name}: Error loading mood history: {e}")
        return entries

    # =============================
    # Scheduling & Wrapper Methods
    # (These call the corresponding methods on each platform adapter)
    # =============================
    def add_platform_adapter(self, platform: str, adapter: BasePlatformAdapter):
        self.platform_adapters[platform.lower()] = adapter
        logging.info(f"✅ Bot {self.name}: Added adapter for platform '{platform}'.")

    def post_job_wrapper(self):
        for adapter in self.platform_adapters.values():
            adapter.post()
        self.scheduler.clear("randomized_post")
        if self.auto_post_enabled:
            self.schedule_next_post_job()

    def comment_job_wrapper(self):
        for adapter in self.platform_adapters.values():
            adapter.comment()
        self.scheduler.clear("randomized_comment")
        if self.auto_comment_enabled:
            self.schedule_next_comment_job()

    def reply_job_wrapper(self):
        for adapter in self.platform_adapters.values():
            adapter.reply()
        self.scheduler.clear("randomized_reply")
        if self.auto_reply_enabled:
            self.schedule_next_reply_job()

    def cross_job_wrapper(self):
        for adapter in self.platform_adapters.values():
            adapter.cross_engage()
        self.scheduler.clear("cross_engagement")
        if self.auto_cross_enabled:
            self.scheduler.every(1).hours.do(self.cross_job_wrapper).tag("cross_engagement")
            logging.info(f"Bot {self.name}: Next cross-platform engagement scheduled in 1 hour.")

    def trending_job_wrapper(self):
        for adapter in self.platform_adapters.values():
            adapter.trending_engage()
        self.scheduler.clear("trending_engagement")
        if self.auto_trending_enabled:
            self.scheduler.every().day.at("11:00").do(self.trending_job_wrapper).tag("trending_engagement")
            logging.info(f"Bot {self.name}: Next trending engagement scheduled at 11:00.")

    def dm_job_wrapper(self):
        for adapter in self.platform_adapters.values():
            adapter.check_dms()
        self.scheduler.clear("dm_job")
        if self.auto_dm_enabled:
            self.scheduler.every(30).minutes.do(self.dm_job_wrapper).tag("dm_job")
            logging.info(f"Bot {self.name}: Next DM check scheduled in 30 minutes.")

    def story_job_wrapper(self):
        for adapter in self.platform_adapters.values():
            adapter.story()
        self.scheduler.clear("story_job")
        if self.auto_story_enabled:
            self.scheduler.every().day.at("16:00").do(self.story_job_wrapper).tag("story_job")
            logging.info(f"Bot {self.name}: Next collaborative storytelling scheduled at 16:00.")

    def schedule_next_post_job(self):
        if not self.auto_post_enabled:
            return
        rng = random.Random()
        rng.seed(f"{self.name}_post_{time.time()}")
        post_times = self.config.get("schedule", {}).get("post_times", ["12:00", "18:00"])
        random_post_time = rng.choice(post_times)
        random_post_time = self.validate_time(random_post_time, "12:00")
        self.scheduler.every().day.at(random_post_time).do(self.post_job_wrapper).tag("randomized_post")
        logging.info(f"Bot {self.name}: Next post scheduled at {random_post_time}")

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
        self.schedule_next_post_job()
        self.schedule_next_comment_job()
        self.schedule_next_reply_job()
        if self.auto_cross_enabled:
            self.scheduler.every(1).hours.do(self.cross_job_wrapper).tag("cross_engagement")
            logging.info(f"Bot {self.name}: Cross-platform engagement scheduled every hour.")
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

    # =============================
    # START / STOP & Console Commands
    # =============================
    def start(self):
        if self.running:
            logging.info(f"Bot {self.name} is already running.")
            return
        self._stop_event.clear()
        if self.flask_thread is None or not self.flask_thread.is_alive():
            self.flask_thread = threading.Thread(target=self.run_flask, daemon=True)
            self.flask_thread.start()
            time.sleep(1)
        self.load_config()
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

    def process_console_command(self, cmd: str):
        if cmd == "start":
            self.start()
        elif cmd == "stop":
            self.stop()
        elif cmd == "new auth":
            if os.path.exists(self.token_file):
                os.remove(self.token_file)
                logging.info(f"✅ Bot {self.name}: Token file removed. Bot will reauthenticate on next startup.")
                print("Token file removed. Bot will reauthenticate on next startup.")
            else:
                logging.info(f"✅ Bot {self.name}: No token file found.")
                print("No token file found.")
        elif cmd == "auth age":
            print(self.get_auth_age())
        elif cmd.startswith("run post"):
            logging.info(f"🚀 Bot {self.name}: 'run post' command received. Posting on all platforms {self.post_run_count} time(s).")
            for _ in range(self.post_run_count):
                self.post_job_wrapper()
        elif cmd.startswith("run comment"):
            logging.info(f"🚀 Bot {self.name}: 'run comment' command received. Commenting on all platforms {self.comment_run_count} time(s).")
            for _ in range(self.comment_run_count):
                self.comment_job_wrapper()
        elif cmd.startswith("run reply"):
            logging.info(f"🚀 Bot {self.name}: 'run reply' command received. Replying on all platforms {self.reply_run_count} time(s).")
            for _ in range(self.reply_run_count):
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
                        # For contexts that include news, delegate fetching to a specific adapter (e.g. Twitter)
                        if prompt_settings.get("include_news", False):
                            news_keyword = prompt_settings.get("news_keyword", None)
                            # Here we assume the "twitter" adapter is available to fetch news
                            news_data = self.platform_adapters["twitter"].fetch_news(news_keyword)
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
            if not self.auto_cross_enabled:
                self.auto_cross_enabled = True
                self.scheduler.every(1).hours.do(self.cross_job_wrapper).tag("cross_engagement")
                logging.info(f"✅ Bot {self.name}: Auto cross-platform engagement enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto cross-platform engagement is already enabled.")
        elif cmd == "stop cross":
            if self.auto_cross_enabled:
                self.scheduler.clear("cross_engagement")
                self.auto_cross_enabled = False
                logging.info(f"🚫 Bot {self.name}: Auto cross-platform engagement disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.name}: Auto cross-platform engagement is already disabled.")
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
                for adapter in self.platform_adapters.values():
                    adapter.send_dm(recipient, message)
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
                self.story_job_wrapper()
        elif cmd == "run image tweet":
            logging.info(f"🚀 Bot {self.name}: 'run image tweet' command received.")
            for adapter in self.platform_adapters.values():
                adapter.post_with_image()
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

# End of Bot class

if __name__ == "__main__":
    import glob
    # Load all configuration files from CONFIGS_DIR (assumed to be YAML files)
    config_files = glob.glob(os.path.join(CONFIGS_DIR, "*.yaml"))
    start_port = 5050
    bots = []
    for idx, config_file in enumerate(config_files):
        bot_name = os.path.splitext(os.path.basename(config_file))[0]
        port = start_port + idx
        bot_instance = Bot(bot_name, config_file, port)
        bot_instance.load_config()
        from src.platforms.twitter import TwitterAdapter
        from src.platforms.facebook import FacebookAdapter
        from src.platforms.instagram import InstagramAdapter
        from src.platforms.telegram import TelegramAdapter
        from src.platforms.discord import DiscordAdapter
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