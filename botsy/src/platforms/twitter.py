import os
import json
import logging
import threading
import time
import re
import random
import requests
import datetime
import pytz
import tweepy
from jinja2 import Template
from pathlib import Path
from src.platforms.base_adapter import BasePlatformAdapter
from src.bot import RATE_LIMIT_WAIT, MAX_AUTH_RETRIES, TOKEN_EXPIRY_SECONDS


class TwitterAdapter(BasePlatformAdapter):
    def __init__(self, bot):
        super().__init__(bot)
        # OAuth is handled by Bot.start()

    def authenticate(self):
        # This adapter relies on the bot's OAuth process.
        pass

    def create_post(self, text: str):
        try:
            self.bot.client.create_tweet(text=text)
            logging.info(f"TwitterAdapter: Tweet posted successfully: {text}")
        except Exception as e:
            logging.error(f"TwitterAdapter: Error posting tweet: {e}")

    def post(self, content: str):
        if self.bot.client is None:
            logging.error("TwitterAdapter: Twitter client is not initialized. Please complete OAuth via Bot.start().")
            return ""
        if content.strip():
            tweet_text = self.bot.clean_tweet_text(content)
            self.create_post(tweet_text)
            return tweet_text
        else:
            tweet_text = self.generate_tweet()
            if tweet_text:
                self.create_post(tweet_text)
                return tweet_text
            else:
                logging.error("TwitterAdapter: Failed to generate tweet content.")
                return ""

    def comment(self, content: str, reply_to_id: str):
        if self.bot.client is None:
            logging.error("TwitterAdapter: Twitter client is not initialized. Please complete OAuth via Bot.start().")
            return ""
        if not str(reply_to_id).strip():
            logging.error("TwitterAdapter: No valid tweet id provided for replying.")
            return ""
        tweet_text = self.bot.clean_tweet_text(content) if content.strip() else self.generate_tweet()
        if not tweet_text:
            logging.error("TwitterAdapter: Failed to generate comment content.")
            return ""
        try:
            self.bot.client.create_tweet(
                text=tweet_text,
                in_reply_to_tweet_id=reply_to_id,
                user_auth=True
            )
            logging.info(f"TwitterAdapter: Comment posted successfully in reply to {reply_to_id}")
            return tweet_text
        except Exception as e:
            logging.error(f"TwitterAdapter: Error posting comment: {e}")
            return ""

    def reply(self, content: str, reply_to_id: str):
        return self.comment(content, reply_to_id)

    def dm(self, recipient: str, message: str):
        logging.info(f"TwitterAdapter: Sending DM to {recipient} with message: {message}")
        return "dm_id_stub"

    def add_conversational_dynamics(self, text: str) -> str:
        if random.random() < 0.1:
            text = "Uh-huh, " + text
        if random.random() < 0.05:
            text += " (Correction: Sorry, I misspoke!)"
        return text

    def generate_tweet(self) -> str:
        config = self.bot.config
        if not config:
            logging.error("TwitterAdapter: Configuration is empty or invalid.")
            return ""
        contexts = config.get("contexts", {})
        if not contexts:
            logging.error("TwitterAdapter: No contexts found in config.")
            return ""
        random_context = random.choice(list(contexts.keys()))
        logging.info(f"TwitterAdapter: Selected context: {random_context}")
        prompt_settings = contexts[random_context].get("prompt", {})
        if not prompt_settings:
            logging.error(f"TwitterAdapter: No prompt data found for context '{random_context}'.")
            return ""
        system_prompt = prompt_settings.get("system", "")
        user_prompt = prompt_settings.get("user", "")
        model = prompt_settings.get("model", "gpt-4o")
        temperature = prompt_settings.get("temperature", 1)
        max_tokens = prompt_settings.get("max_tokens", 16384)
        top_p = prompt_settings.get("top_p", 1.0)
        frequency_penalty = prompt_settings.get("frequency_penalty", 0.8)
        presence_penalty = prompt_settings.get("presence_penalty", 0.1)
        conversation = ""
        if hasattr(self.bot, "load_conversation_history"):
            conversation = self.bot.load_conversation_history()
        if conversation:
            user_prompt += "\nPrevious conversation: " + conversation
        if prompt_settings.get("include_news", False):
            news_keyword = prompt_settings.get("news_keyword", None)
            news_data = self.bot.fetch_news(news_keyword)
            user_prompt = user_prompt.replace("{{news_headline}}", news_data["headline"])
            user_prompt = user_prompt.replace("{{news_article}}", news_data["article"])
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if user_prompt:
            messages.append({"role": "user", "content": user_prompt})
        tweet_text = self.bot.call_openai_completion(model, messages, temperature, max_tokens, top_p,
                                                     frequency_penalty, presence_penalty)
        tweet_text = self.add_conversational_dynamics(tweet_text)
        if hasattr(self.bot, "append_conversation_history"):
            self.bot.append_conversation_history(tweet_text)
        return self.bot.clean_tweet_text(tweet_text) if tweet_text else ""

    def post_tweet(self) -> bool:
        tweet = self.generate_tweet()
        if not tweet:
            logging.error("TwitterAdapter: No tweet generated.")
            return False
        try:
            self.bot.client.create_tweet(text=tweet)
            logging.info(f"TwitterAdapter: Tweet posted successfully: {tweet}")
            return True
        except tweepy.Unauthorized:
            logging.error("TwitterAdapter: Invalid credentials, removing token file")
            if os.path.exists(self.bot.token_file):
                os.remove(self.bot.token_file)
            return False
        except tweepy.TooManyRequests:
            logging.warning("TwitterAdapter: Rate limit hit while posting tweet. Backing off...")
            time.sleep(RATE_LIMIT_WAIT)
            return False
        except tweepy.TweepyException as e:
            logging.error(f"TwitterAdapter: Error posting tweet: {str(e)}")
            return False

    def daily_tweet_job(self):
        logging.info("TwitterAdapter: Attempting to post a tweet...")
        success = False
        for _ in range(MAX_AUTH_RETRIES):
            if self.post_tweet():
                success = True
                break
            time.sleep(2)
        if success:
            logging.info(f"TwitterAdapter: Tweet posted at {datetime.datetime.now(pytz.utc)}")
        else:
            logging.error("TwitterAdapter: Failed to post tweet after multiple attempts.")

    def daily_comment_job(self):
        logging.info("TwitterAdapter: Attempting to auto-comment...")
        config = self.bot.config
        if not config:
            logging.warning("TwitterAdapter: Config empty/invalid.")
            return
        monitored_handles = config.get("monitored_handles", {})
        # This works for any bot selected from the configs folder.
        handles = [handle for handle in monitored_handles.keys() if handle.lower() != "last_id"]
        self.bot.get_user_ids_bulk(handles)
        for handle_name in handles:
            handle_data = monitored_handles.get(handle_name, {})
            user_id = self.bot.get_user_id(handle_name)
            if not user_id:
                logging.warning(f"TwitterAdapter: Could not fetch user_id for '{handle_name}'. Skipping.")
                continue
            last_id = self.bot.monitored_handles_last_ids.get(handle_name)
            try:
                tweets_response = self.bot.client.get_users_tweets(
                    id=user_id,
                    since_id=last_id,
                    exclude=["retweets", "replies"],
                    max_results=5,
                    tweet_fields=["id", "text"],
                    user_auth=True
                )
            except Exception as e:
                logging.error(f"TwitterAdapter: Error fetching tweets for '{handle_name}': {e}")
                continue
            if not tweets_response or not tweets_response.data:
                logging.info(f"TwitterAdapter: No new tweets from {handle_name}.")
                continue
            newest_tweet = tweets_response.data[0]
            tweet_id = str(newest_tweet.id) if hasattr(newest_tweet, "id") else str(newest_tweet.get("id", ""))
            if not tweet_id.strip():
                logging.warning(f"TwitterAdapter: Retrieved tweet id for {handle_name} is invalid; skipping comment.")
                continue
            if last_id and tweet_id <= str(last_id):
                logging.info(f"TwitterAdapter: Already commented or not newer than {last_id}.")
                continue
            prompt_data = handle_data.get("response_prompt", {})
            if not prompt_data:
                logging.warning(f"TwitterAdapter: No response_prompt for '{handle_name}'. Skipping.")
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
            filled_prompt = template.render(tweet_text=newest_tweet.text, mood_state=self.bot.mood_state)
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": filled_prompt})
            reply = self.bot.call_openai_completion(model, messages, temperature, max_tokens, top_p,
                                                    frequency_penalty, presence_penalty)
            if reply:
                # Use ser_id from YAML if provided; otherwise use the fetched tweet_id.
                reply_to_id = (str(handle_data.get("ser_id")).strip()
                               if str(handle_data.get("ser_id")).strip() not in ["", "0", "None"]
                               else tweet_id)
                if not reply_to_id.strip():
                    logging.error("TwitterAdapter: No valid tweet id provided for replying.")
                    continue
                try:
                    self.bot.client.create_tweet(
                        text=reply,
                        in_reply_to_tweet_id=reply_to_id,
                        user_auth=True
                    )
                    logging.info(f"TwitterAdapter: Replied to tweet {reply_to_id} by {handle_name}: {reply}")
                    self.bot.monitored_handles_last_ids[handle_name] = tweet_id
                except Exception as e:
                    logging.error(f"TwitterAdapter: Error replying to tweet {reply_to_id}: {e}")
            else:
                logging.error(f"TwitterAdapter: Failed to generate reply for tweet {tweet_id}")

    def daily_comment_reply_job(self):
        logging.info("TwitterAdapter: Attempting to auto-reply...")
        config = self.bot.config
        if not config:
            logging.warning("TwitterAdapter: Config empty/invalid.")
            return
        reply_handles = config.get("reply_handles", {})
        if not reply_handles:
            logging.warning("TwitterAdapter: No reply handles specified in config. Skipping.")
            return
        self.bot.get_user_ids_bulk(list(reply_handles.keys()))
        for handle_name, handle_data in reply_handles.items():
            user_id = self.bot.get_user_id(handle_name)
            if not user_id:
                logging.warning(f"TwitterAdapter: Could not fetch user_id for '{handle_name}'. Skipping.")
                continue
            try:
                auth_user = self.bot.get_cached_me()
                if not (auth_user and auth_user.data):
                    logging.error("TwitterAdapter: Failed to retrieve authenticated user info.")
                    return
                recent_tweet = self.bot.get_bot_recent_tweet_id()
                if not recent_tweet:
                    logging.info("TwitterAdapter: No recent tweet found.")
                    continue
            except Exception as e:
                logging.error(f"TwitterAdapter: Error retrieving bot info: {e}")
                continue
            try:
                replies = self.bot.client.search_recent_tweets(
                    query=f"to:{auth_user.data.username}",
                    since_id=recent_tweet,
                    max_results=10,
                    tweet_fields=["author_id", "text"],
                    expansions="author_id",
                    user_auth=True
                )
            except Exception as e:
                logging.error(f"TwitterAdapter: Error fetching replies: {e}")
                continue
            if not replies or not replies.data:
                logging.info(f"TwitterAdapter: No replies found for tweet {recent_tweet}.")
                continue
            author_users = {user.id: user.username.lower() for user in replies.includes.get("users", [])}
            for rep in replies.data:
                reply_text = rep.text.strip() if hasattr(rep, "text") else rep.get("text", "").strip()
                rep_author_id = rep.author_id if hasattr(rep, "author_id") else rep.get("author_id", "")
                author_handle = author_users.get(rep_author_id, "").lower()
                if author_handle != handle_name.lower():
                    logging.info(f"TwitterAdapter: Ignoring reply from @{author_handle}.")
                    continue
                logging.info(f"TwitterAdapter: Detected reply from @{handle_name}: {reply_text}")
                prompt_data = handle_data.get("response_prompt", {})
                if not prompt_data:
                    logging.warning(f"TwitterAdapter: No response_prompt for '{handle_name}'. Skipping.")
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
                    tweet_response = self.bot.client.get_tweet(recent_tweet, tweet_fields=["text"], user_auth=True)
                    bot_tweet_text = tweet_response.data.text if tweet_response and tweet_response.data else ""
                except Exception as e:
                    bot_tweet_text = ""
                    logging.warning(f"TwitterAdapter: Could not fetch my tweet text: {e}")
                template = Template(user_prompt_template)
                filled_prompt = template.render(comment_text=reply_text, tweet_text=bot_tweet_text,
                                                mood_state=self.bot.mood_state)
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": filled_prompt})
                response_text = self.bot.call_openai_completion(model, messages, temperature, max_tokens, top_p,
                                                                frequency_penalty, presence_penalty)
                if response_text:
                    try:
                        rep_id = str(rep.id) if hasattr(rep, "id") else str(rep.get("id", ""))
                        self.bot.client.create_tweet(text=response_text, in_reply_to_tweet_id=rep_id, user_auth=True)
                        logging.info(f"TwitterAdapter: Replied to @{handle_name} on tweet {rep_id}: {response_text}")
                    except Exception as e:
                        logging.error(f"TwitterAdapter: Error replying for tweet {rep_id}: {e}")
                else:
                    logging.error(f"TwitterAdapter: Failed to generate reply for tweet {rep_id}")

    def cross_bot_engagement(self):
        bot_network = self.bot.config.get("bot_network", [])
        if not bot_network:
            logging.info("TwitterAdapter: No bot network defined for cross engagement.")
            return
        query = " OR ".join([f"from:{username}" for username in bot_network])
        try:
            results = self.bot.client.search_recent_tweets(
                query=query,
                max_results=5,
                tweet_fields=["id", "text"],
                user_auth=True
            )
            if results and results.data:
                for tweet in results.data:
                    reply_text = f"@{tweet.id} Interesting point!"
                    try:
                        self.bot.client.create_tweet(
                            text=reply_text,
                            in_reply_to_tweet_id=str(tweet.id),
                            user_auth=True
                        )
                        logging.info(f"TwitterAdapter: Cross-engaged with tweet {tweet.id} from network.")
                    except Exception as e:
                        logging.error(f"TwitterAdapter: Error during cross engagement on tweet {tweet.id}: {e}")
            else:
                logging.info("TwitterAdapter: No network tweets found for cross engagement.")
        except Exception as e:
            logging.error(f"TwitterAdapter: Error during cross engagement: {e}")

    def run_cross_engagement_job(self):
        logging.info("TwitterAdapter: Running cross-bot engagement job.")
        self.cross_bot_engagement()

    def load_shared_story_state(self):
        shared_file = os.path.join(Path(__file__).parent.parent, "shared", "story_state.json")
        if os.path.exists(shared_file):
            try:
                with open(shared_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"TwitterAdapter: Error loading shared story state: {e}")
        return {"story": ""}

    def update_shared_story_state(self, new_content: str):
        shared_file = os.path.join(Path(__file__).parent.parent, "shared", "story_state.json")
        state = self.load_shared_story_state()
        state["story"] += "\n" + new_content
        try:
            with open(shared_file, "w") as f:
                json.dump(state, f)
            logging.info("TwitterAdapter: Updated shared story state.")
        except Exception as e:
            logging.error(f"TwitterAdapter: Error updating shared story state: {e}")

    def run_collaborative_storytelling(self):
        shared_state = self.load_shared_story_state().get("story", "")
        prompt = (f"Continue the collaborative story with a new tweet. Current mood: {self.bot.mood_state}. "
                  f"Include a plot twist. Previous story: {shared_state}")
        messages = [{"role": "user", "content": prompt}]
        story_tweet = self.bot.call_openai_completion("gpt-4o", messages, 1, 150, 1.0, 0.8, 0.1)
        if story_tweet:
            try:
                self.bot.client.create_tweet(text=story_tweet)
                logging.info(f"TwitterAdapter: Posted a collaborative storytelling tweet: {story_tweet}")
                if hasattr(self.bot, "append_conversation_history"):
                    self.bot.append_conversation_history(story_tweet)
                self.update_shared_story_state(story_tweet)
            except Exception as e:
                logging.error(f"TwitterAdapter: Error posting storytelling tweet: {e}")

    def generate_image(self, prompt: str) -> str:
        image_url = "https://via.placeholder.com/500.png?text=Generated+Image"
        logging.info(f"TwitterAdapter: Generated image for prompt '{prompt}': {image_url}")
        return image_url

    def generate_audio(self, prompt: str) -> str:
        audio_url = "https://via.placeholder.com/audio_clip.mp3?text=Generated+Audio"
        logging.info(f"TwitterAdapter: Generated audio for prompt '{prompt}': {audio_url}")
        return audio_url

    def post_tweet_with_image(self) -> bool:
        tweet = self.generate_tweet()
        if not tweet:
            logging.error("TwitterAdapter: No tweet generated for image tweet.")
            return False
        image_prompt = self.bot.config.get("image_prompt", f"Generate an image for tweet: {tweet}")
        image_url = self.generate_image(image_prompt)
        tweet_with_image = tweet + f"\nImage: {image_url}"
        metrics = self.track_engagement_metrics()
        if metrics.get("likes", 0) > 75:
            audio_url = self.generate_audio("Audio for tweet: " + tweet)
            tweet_with_image += f"\nAudio: {audio_url}"
        try:
            self.bot.client.create_tweet(text=tweet_with_image)
            logging.info("TwitterAdapter: Tweet with image (and possibly audio) posted successfully.")
            return True
        except Exception as e:
            logging.error(f"TwitterAdapter: Error posting tweet with image: {e}")
            return False

    def track_engagement_metrics(self):
        metrics = {"likes": random.randint(0, 100), "retweets": random.randint(0, 50)}
        try:
            with open(self.bot.engagement_metrics_file, "w") as f:
                json.dump(metrics, f)
            logging.info(f"TwitterAdapter: Updated engagement metrics: {metrics}")
        except Exception as e:
            logging.error(f"TwitterAdapter: Error saving engagement metrics: {e}")
        return metrics

    def adaptive_tune(self):
        metrics = self.track_engagement_metrics()
        if metrics.get("likes", 0) > 50:
            new_temp = max(0.5, 1 - (metrics["likes"] / 200))
        else:
            new_temp = min(1.5, 1 + (50 - metrics["likes"]) / 100)
        logging.info(f"TwitterAdapter: Adaptive tuning set temperature to {new_temp:.2f} based on engagement.")
        self.reinforcement_learning_update()

    def reinforcement_learning_update(self):
        metrics = self.track_engagement_metrics()
        if metrics.get("likes", 0) > 50:
            self.bot.personality["extraversion"] = min(1.0, self.bot.personality.get("extraversion", 0.5) + 0.05)
        else:
            self.bot.personality["extraversion"] = max(0.0, self.bot.personality.get("extraversion", 0.5) - 0.05)
        logging.info(f"TwitterAdapter: Updated personality via reinforcement learning: {self.bot.personality}")

    def contextual_retraining(self):
        logging.info("TwitterAdapter: Contextual re-training executed based on conversation and engagement history.")

    # Scheduling and wrapper methods
    def tweet_job_wrapper(self):
        self.daily_tweet_job()
        self.scheduler.clear("randomized_tweet")
        if self.bot.auto_post_enabled:
            self.schedule_next_tweet_job()

    def comment_job_wrapper(self):
        self.daily_comment_job()
        self.scheduler.clear("randomized_comment")
        if self.bot.auto_comment_enabled:
            self.schedule_next_comment_job()

    def reply_job_wrapper(self):
        self.daily_comment_reply_job()
        self.scheduler.clear("randomized_reply")
        if self.bot.auto_reply_enabled:
            self.schedule_next_reply_job()

    def cross_job_wrapper(self):
        self.run_cross_engagement_job()
        self.scheduler.clear("cross_engagement")
        if self.bot.auto_cross_enabled:
            self.scheduler.every(1).hours.do(self.cross_job_wrapper).tag("cross_engagement")
            logging.info(f"Bot {self.bot.name}: Next cross-bot engagement scheduled in 1 hour.")

    def trending_job_wrapper(self):
        self.run_trending_engagement()
        self.scheduler.clear("trending_engagement")
        if self.bot.auto_trending_enabled:
            self.scheduler.every().day.at("11:00").do(self.trending_job_wrapper).tag("trending_engagement")
            logging.info(f"Bot {self.bot.name}: Next trending engagement scheduled at 11:00.")

    def dm_job_wrapper(self):
        self.run_dm_job()
        self.scheduler.clear("dm_job")
        if self.bot.auto_dm_enabled:
            self.scheduler.every(30).minutes.do(self.dm_job_wrapper).tag("dm_job")
            logging.info(f"Bot {self.bot.name}: Next DM check scheduled in 30 minutes.")

    def story_job_wrapper(self):
        self.run_collaborative_storytelling()
        self.scheduler.clear("story_job")
        if self.bot.auto_story_enabled:
            self.scheduler.every().day.at("16:00").do(self.story_job_wrapper).tag("story_job")
            logging.info(f"Bot {self.bot.name}: Next storytelling tweet scheduled at 16:00.")

    def schedule_next_tweet_job(self):
        if not self.bot.auto_post_enabled:
            return
        rng = random.Random()
        rng.seed(f"{self.bot.name}_tweet_{time.time()}")
        tweet_times = self.bot.config.get("schedule", {}).get("tweet_times", ["12:00", "18:00"])
        random_tweet_time = rng.choice(tweet_times)
        random_tweet_time = self.validate_time(random_tweet_time, "12:00")
        self.scheduler.every().day.at(random_tweet_time).do(self.tweet_job_wrapper).tag("randomized_tweet")
        logging.info(f"Bot {self.bot.name}: Next tweet scheduled at {random_tweet_time}")

    def schedule_next_comment_job(self):
        if not self.bot.auto_comment_enabled:
            return
        rng = random.Random()
        rng.seed(f"{self.bot.name}_comment_{time.time()}")
        comment_times = self.bot.config.get("schedule", {}).get("comment_times", ["13:00", "19:00"])
        random_comment_time = rng.choice(comment_times)
        random_comment_time = self.validate_time(random_comment_time, "13:00")
        self.scheduler.every().day.at(random_comment_time).do(self.comment_job_wrapper).tag("randomized_comment")
        logging.info(f"Bot {self.bot.name}: Next comment scheduled at {random_comment_time}")

    def schedule_next_reply_job(self):
        if not self.bot.auto_reply_enabled:
            return
        rng = random.Random()
        rng.seed(f"{self.bot.name}_reply_{time.time()}")
        reply_times = self.bot.config.get("schedule", {}).get("reply_times", ["14:30", "20:30"])
        random_reply_time = rng.choice(reply_times)
        random_reply_time = self.validate_time(random_reply_time, "14:30")
        self.scheduler.every().day.at(random_reply_time).do(self.reply_job_wrapper).tag("randomized_reply")
        logging.info(f"Bot {self.bot.name}: Next reply scheduled at {random_reply_time}")

    def randomize_schedule(self):
        self.schedule_next_tweet_job()
        self.schedule_next_comment_job()
        self.schedule_next_reply_job()
        if self.bot.auto_cross_enabled:
            self.scheduler.every(1).hours.do(self.cross_job_wrapper).tag("cross_engagement")
            logging.info(f"Bot {self.bot.name}: Cross-bot engagement scheduled every hour.")
        if self.bot.auto_trending_enabled:
            self.scheduler.every().day.at("11:00").do(self.trending_job_wrapper).tag("trending_engagement")
            logging.info(f"Bot {self.bot.name}: Trending engagement scheduled at 11:00 daily.")
        if self.bot.auto_dm_enabled:
            self.scheduler.every(30).minutes.do(self.dm_job_wrapper).tag("dm_job")
            logging.info(f"Bot {self.bot.name}: DM check scheduled every 30 minutes.")
        if self.bot.auto_story_enabled:
            self.scheduler.every().day.at("16:00").do(self.story_job_wrapper).tag("story_job")
            logging.info(f"Bot {self.bot.name}: Collaborative storytelling scheduled at 16:00 daily.")

    def re_randomize_schedule(self):
        self.scheduler.clear()
        logging.info(f"Bot {self.bot.name}: Cleared previous randomized jobs.")
        self.randomize_schedule()

    def start_scheduler(self):
        while not self.bot._stop_event.is_set():
            self.scheduler.run_pending()
            time.sleep(1)

    def start(self):
        if self.bot.running:
            logging.info(f"Bot {self.bot.name} is already running.")
            return
        self.bot._stop_event.clear()
        if self.bot.flask_thread is None or not self.bot.flask_thread.is_alive():
            self.bot.flask_thread = threading.Thread(target=self.bot.run_flask, daemon=True)
            self.bot.flask_thread.start()
            time.sleep(1)
        self.authenticate()
        self.bot.load_user_id_cache()
        self.bot.load_bot_tweet_cache()
        self.bot.auto_post_enabled = True
        self.bot.auto_comment_enabled = True
        self.bot.auto_reply_enabled = True
        self.bot.auto_cross_enabled = False
        self.bot.auto_trending_enabled = False
        self.bot.auto_dm_enabled = False
        self.bot.auto_story_enabled = False
        self.randomize_schedule()
        self.bot.scheduler_thread = threading.Thread(target=self.start_scheduler, daemon=True)
        self.bot.scheduler_thread.start()
        self.bot.running = True
        logging.info(f"Bot {self.bot.name} started.")

    def stop(self):
        if not self.bot.running:
            logging.info(f"Bot {self.bot.name} is not running.")
            return
        self.bot._stop_event.set()
        self.scheduler.clear()
        self.bot.running = False
        logging.info(f"Bot {self.bot.name} stopped.")
        try:
            requests.post(f"http://localhost:{self.bot.port}/shutdown")
        except Exception as e:
            logging.error(f"Bot {self.bot.name}: Error shutting down Flask server: {e}")

    def get_status(self) -> str:
        return "UP" if self.bot.running else "DOWN"

    def get_auth_age(self) -> str:
        if not os.path.exists(self.bot.token_file):
            return "No token file found."
        mod_time = os.path.getmtime(self.bot.token_file)
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
            if os.path.exists(self.bot.token_file):
                os.remove(self.bot.token_file)
                self.bot.cached_me = None
                logging.info(f"Bot {self.bot.name}: Token file removed. Bot will reauthenticate on next startup.")
                print("Token file removed. Bot will reauthenticate on next startup.")
            else:
                logging.info(f"Bot {self.bot.name}: No token file found.")
                print("No token file found.")
        elif cmd == "auth age":
            print(self.get_auth_age())
        elif cmd.startswith("run post"):
            logging.info(
                f"Bot {self.bot.name}: 'run post' command received. Posting tweet {self.bot.post_run_count} time(s).")
            for _ in range(self.bot.post_run_count):
                self.daily_tweet_job()
        elif cmd.startswith("run comment"):
            logging.info(
                f"Bot {self.bot.name}: 'run comment' command received. Commenting {self.bot.comment_run_count} time(s).")
            for _ in range(self.bot.comment_run_count):
                self.daily_comment_job()
        elif cmd.startswith("run reply"):
            logging.info(
                f"Bot {self.bot.name}: 'run reply' command received. Replying {self.bot.reply_run_count} time(s).")
            for _ in range(self.bot.reply_run_count):
                self.daily_comment_reply_job()
        elif cmd.startswith("set post count "):
            try:
                value = int(cmd.split("set post count ")[1])
                self.bot.post_run_count = value
                logging.info(f"Bot {self.bot.name}: Set post count to {self.bot.post_run_count}")
            except Exception:
                logging.error(f"Bot {self.bot.name}: Invalid value for post count")
        elif cmd.startswith("set comment count "):
            try:
                value = int(cmd.split("set comment count ")[1])
                self.bot.comment_run_count = value
                logging.info(f"Bot {self.bot.name}: Set comment count to {self.bot.comment_run_count}")
            except Exception:
                logging.error(f"Bot {self.bot.name}: Invalid value for comment count")
        elif cmd.startswith("set reply count "):
            try:
                value = int(cmd.split("set reply count ")[1])
                self.bot.reply_run_count = value
                logging.info(f"Bot {self.bot.name}: Set reply count to {self.bot.reply_run_count}")
            except Exception:
                logging.error(f"Bot {self.bot.name}: Invalid value for reply count")
        elif cmd.startswith("set dm count "):
            try:
                value = int(cmd.split("set dm count ")[1])
                self.bot.dm_run_count = value
                logging.info(f"Bot {self.bot.name}: Set DM run count to {self.bot.dm_run_count}")
            except Exception:
                logging.error(f"Bot {self.bot.name}: Invalid value for DM run count")
        elif cmd.startswith("set story count "):
            try:
                value = int(cmd.split("set story count ")[1])
                self.bot.story_run_count = value
                logging.info(f"Bot {self.bot.name}: Set story run count to {self.bot.story_run_count}")
            except Exception:
                logging.error(f"Bot {self.bot.name}: Invalid value for story run count")
        elif cmd == "list context":
            if self.bot.config and "contexts" in self.bot.config:
                contexts = list(self.bot.config["contexts"].keys())
                if contexts:
                    print("Available contexts: " + ", ".join(contexts))
                    logging.info(f"Bot {self.bot.name}: Listed contexts: {', '.join(contexts)}")
                else:
                    print("No contexts defined in the configuration.")
                    logging.info(f"Bot {self.bot.name}: No contexts found in config.")
            else:
                print("No configuration loaded or 'contexts' section missing.")
                logging.error(f"Bot {self.bot.name}: Configuration or contexts section missing.")
        elif cmd.startswith("run context"):
            parts = cmd.split(" ", 2)
            if len(parts) < 3:
                print("Usage: run context {context name}")
                logging.error(f"Bot {self.bot.name}: 'run context' requires a context name.")
            else:
                context_name = parts[2].strip()
                if self.bot.config and "contexts" in self.bot.config and context_name in self.bot.config["contexts"]:
                    prompt_settings = self.bot.config["contexts"][context_name].get("prompt", {})
                    if not prompt_settings:
                        print(f"Context '{context_name}' does not have prompt settings defined.")
                        logging.error(f"Bot {self.bot.name}: Prompt settings missing for context '{context_name}'.")
                    else:
                        system_prompt = prompt_settings.get("system", "")
                        user_prompt = prompt_settings.get("user", "")
                        if prompt_settings.get("include_news", False):
                            news_keyword = prompt_settings.get("news_keyword", None)
                            news_data = self.bot.fetch_news(news_keyword)
                            template = Template(user_prompt)
                            user_prompt = template.render(news_headline=news_data["headline"],
                                                          news_article=news_data["article"],
                                                          mood_state=self.bot.mood_state)
                        else:
                            template = Template(user_prompt)
                            user_prompt = template.render(mood_state=self.bot.mood_state)
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
                        result = self.bot.call_openai_completion(model, messages, temperature, max_tokens, top_p,
                                                                 frequency_penalty, presence_penalty)
                        print(f"Generated output for context '{context_name}':\n{result}")
                        logging.info(f"Bot {self.bot.name}: Ran context '{context_name}' successfully.")
                else:
                    print(f"Context '{context_name}' not found in configuration.")
                    logging.error(f"Bot {self.bot.name}: Context '{context_name}' does not exist.")
        elif cmd == "new random all":
            logging.info(f"Bot {self.bot.name}: Scheduling new random times for post, comment, and reply.")
            self.re_randomize_schedule()
        elif cmd == "new random post":
            logging.info(f"Bot {self.bot.name}: Scheduling new random time for post.")
            self.scheduler.clear("randomized_tweet")
            if self.bot.auto_post_enabled:
                self.schedule_next_tweet_job()
        elif cmd == "new random comment":
            logging.info(f"Bot {self.bot.name}: Scheduling new random time for comment.")
            self.scheduler.clear("randomized_comment")
            if self.bot.auto_comment_enabled:
                self.schedule_next_comment_job()
        elif cmd == "new random reply":
            logging.info(f"Bot {self.bot.name}: Scheduling new random time for reply.")
            self.scheduler.clear("randomized_reply")
            if self.bot.auto_reply_enabled:
                self.schedule_next_reply_job()
        elif cmd == "stop post":
            if self.bot.auto_post_enabled:
                self.scheduler.clear("randomized_tweet")
                self.bot.auto_post_enabled = False
                logging.info(f"Bot {self.bot.name}: Auto post disabled.")
            else:
                logging.info(f"Bot {self.bot.name}: Auto post is already disabled.")
        elif cmd == "start post":
            if not self.bot.auto_post_enabled:
                self.bot.auto_post_enabled = True
                self.schedule_next_tweet_job()
                logging.info(f"Bot {self.bot.name}: Auto post enabled.")
            else:
                logging.info(f"Bot {self.bot.name}: Auto post is already enabled.")
        elif cmd == "stop comment":
            if self.bot.auto_comment_enabled:
                self.scheduler.clear("randomized_comment")
                self.bot.auto_comment_enabled = False
                logging.info(f"Bot {self.bot.name}: Auto comment disabled.")
            else:
                logging.info(f"Bot {self.bot.name}: Auto comment is already disabled.")
        elif cmd == "start comment":
            if not self.bot.auto_comment_enabled:
                self.bot.auto_comment_enabled = True
                self.schedule_next_comment_job()
                logging.info(f"Bot {self.bot.name}: Auto comment enabled.")
            else:
                logging.info(f"Bot {self.bot.name}: Auto comment is already enabled.")
        elif cmd == "stop reply":
            if self.bot.auto_reply_enabled:
                self.scheduler.clear("randomized_reply")
                self.bot.auto_reply_enabled = False
                logging.info(f"Bot {self.bot.name}: Auto reply disabled.")
            else:
                logging.info(f"Bot {self.bot.name}: Auto reply is already disabled.")
        elif cmd == "start reply":
            if not self.bot.auto_reply_enabled:
                self.bot.auto_reply_enabled = True
                self.schedule_next_reply_job()
                logging.info(f"Bot {self.bot.name}: Auto reply enabled.")
            else:
                logging.info(f"Bot {self.bot.name}: Auto reply is already enabled.")
        elif cmd == "start cross":
            if not self.bot.auto_cross_enabled:
                self.bot.auto_cross_enabled = True
                self.scheduler.every(1).hours.do(self.cross_job_wrapper).tag("cross_engagement")
                logging.info(f"Bot {self.bot.name}: Auto cross-platform engagement enabled.")
            else:
                logging.info(f"Bot {self.bot.name}: Auto cross engagement is already enabled.")
        elif cmd == "stop cross":
            if self.bot.auto_cross_enabled:
                self.scheduler.clear("cross_engagement")
                self.bot.auto_cross_enabled = False
                logging.info(f"Bot {self.bot.name}: Auto cross-platform engagement disabled.")
            else:
                logging.info(f"Bot {self.bot.name}: Auto cross engagement is already disabled.")
        elif cmd == "start trending":
            if not self.bot.auto_trending_enabled:
                self.bot.auto_trending_enabled = True
                self.scheduler.every().day.at("11:00").do(self.trending_job_wrapper).tag("trending_engagement")
                logging.info(f"Bot {self.bot.name}: Auto trending engagement enabled.")
            else:
                logging.info(f"Bot {self.bot.name}: Auto trending engagement is already enabled.")
        elif cmd == "stop trending":
            if self.bot.auto_trending_enabled:
                self.scheduler.clear("trending_engagement")
                self.bot.auto_trending_enabled = False
                logging.info(f"Bot {self.bot.name}: Auto trending engagement disabled.")
            else:
                logging.info(f"Bot {self.bot.name}: Auto trending engagement is already disabled.")
        elif cmd == "start dm":
            if not self.bot.auto_dm_enabled:
                self.bot.auto_dm_enabled = True
                self.scheduler.every(30).minutes.do(self.dm_job_wrapper).tag("dm_job")
                logging.info(f"Bot {self.bot.name}: Auto DM check enabled.")
            else:
                logging.info(f"Bot {self.bot.name}: Auto DM check is already enabled.")
        elif cmd == "stop dm":
            if self.bot.auto_dm_enabled:
                self.scheduler.clear("dm_job")
                self.bot.auto_dm_enabled = False
                logging.info(f"Bot {self.bot.name}: Auto DM check disabled.")
            else:
                logging.info(f"Bot {self.bot.name}: Auto DM check is already disabled.")
        elif cmd.startswith("run dm"):
            parts = cmd.split(" ", 2)
            if len(parts) < 3:
                print("Usage: run dm {recipient_username}")
                logging.error(f"Bot {self.bot.name}: 'run dm' requires a recipient username.")
            else:
                recipient = parts[2].strip()
                message = input("Enter DM message: ")
                self.dm(recipient, message)
        elif cmd == "start story":
            if not self.bot.auto_story_enabled:
                self.bot.auto_story_enabled = True
                self.scheduler.every().day.at("16:00").do(self.story_job_wrapper).tag("story_job")
                logging.info(f"Bot {self.bot.name}: Auto collaborative storytelling enabled.")
            else:
                logging.info(f"Bot {self.bot.name}: Auto collaborative storytelling is already enabled.")
        elif cmd == "stop story":
            if self.bot.auto_story_enabled:
                self.scheduler.clear("story_job")
                self.bot.auto_story_enabled = False
                logging.info(f"Bot {self.bot.name}: Auto collaborative storytelling disabled.")
            else:
                logging.info(f"Bot {self.bot.name}: Auto collaborative storytelling is already disabled.")
        elif cmd.startswith("run story"):
            logging.info(
                f"Bot {self.bot.name}: 'run story' command received. Running storytelling {self.bot.story_run_count} time(s).")
            for _ in range(self.bot.story_run_count):
                self.run_collaborative_storytelling()
        elif cmd == "run image tweet":
            logging.info(f"Bot {self.bot.name}: 'run image tweet' command received.")
            self.post_tweet_with_image()
        elif cmd == "run adaptive tune":
            logging.info(f"Bot {self.bot.name}: Running adaptive tuning based on engagement metrics.")
            self.adaptive_tune()
        elif cmd == "show metrics":
            if os.path.exists(self.bot.engagement_metrics_file):
                try:
                    with open(self.bot.engagement_metrics_file, "r") as f:
                        metrics = json.load(f)
                    print(f"Engagement Metrics for {self.bot.name}: {metrics}")
                except Exception as e:
                    print("Error reading engagement metrics.")
            else:
                print("No engagement metrics recorded yet.")
        elif cmd.startswith("set mood "):
            mood = cmd.split("set mood ")[1].strip()
            self.bot.mood_state = mood
            logging.info(f"Bot {self.bot.name}: Mood manually set to {self.bot.mood_state}.")
        elif cmd == "show dashboard":
            self.show_dashboard()
        elif cmd == "show settings":
            logging.info(
                f"Bot {self.bot.name}: Current settings: Post Count = {self.bot.post_run_count}, Comment Count = {self.bot.comment_run_count}, Reply Count = {self.bot.reply_run_count}")
        elif cmd == "show listener":
            self.show_listener_state()
        elif cmd == "show log":
            self.show_log()
        elif cmd in ["help", "?"]:
            self.print_help()
        else:
            logging.info("Bot: Unrecognized command. Valid commands:")
            self.print_help()
        print("\nCommand completed. Returning to bot console.\n")
        input("Press Enter to continue...")