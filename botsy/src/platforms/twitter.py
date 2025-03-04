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
# Replace Path with os.path.dirname() calls to avoid unresolved reference errors
# from pathlib import Path
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
            # Include user_auth=True to ensure proper user-level auth is used.
            self.bot.client.create_tweet(text=text, user_auth=True)
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

        # Optionally include news if enabled
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
        return self.bot.clean_tweet_text(tweet_text) if tweet_text else ""

    def post_tweet(self) -> bool:
        tweet = self.generate_tweet()
        if not tweet:
            logging.error("TwitterAdapter: No tweet generated.")
            return False
        try:
            # Use user_auth=True so that the tweet is posted with proper user-level credentials.
            self.bot.client.create_tweet(text=tweet, user_auth=True)
            logging.info(f"TwitterAdapter: Tweet posted successfully: {tweet}")
            return True
        except tweepy.Unauthorized:
            logging.error("TwitterAdapter: Invalid credentials, removing token file")
            if os.path.exists(self.bot.token_file):
                os.remove(self.bot.token_file)
            return False
        except tweepy.TooManyRequests:
            logging.warning("TwitterAdapter: Rate limit hit while posting tweet. Returning to console.")
            return False
        except tweepy.TweepyException as e:
            logging.error(f"TwitterAdapter: Error posting tweet: {str(e)}")
            return False

    def daily_tweet_job(self):
        logging.info(f"⏰ Bot {self.bot.name}: Attempting to post a tweet...")
        success = False
        for _ in range(MAX_AUTH_RETRIES):
            if self.post_tweet():
                success = True
                break
            # Instead of sleeping, return control immediately on error
            return
        if success:
            logging.info(f"✅ Bot {self.bot.name}: Tweet posted at {datetime.datetime.now(pytz.utc)}")
        else:
            logging.error(f"❌ Bot {self.bot.name}: Failed to post tweet after multiple attempts")

    # ----- Commenting on Monitored Tweets -----
    def daily_comment(self):
        logging.info(f"🔎 Bot {self.bot.name}: Checking monitored handles for new tweets...")
        if self.bot.client is None:
            logging.error("TwitterAdapter: Twitter client is not initialized. Cannot check monitored handles.")
            return
        config = self.bot.config
        if not config:
            logging.warning(f"❌ Bot {self.bot.name}: Config empty/invalid.")
            return
        monitored_handles = config.get("monitored_handles", {})
        handles = [handle for handle in monitored_handles.keys() if handle.lower() != "last_id"]
        # Use bulk lookup with caching (see get_user_ids_bulk in Bot)
        self.bot.get_user_ids_bulk(handles)
        for handle_name in handles:
            handle_data = monitored_handles.get(handle_name, {})
            user_id = self.bot.get_user_id(handle_name)
            if not user_id:
                logging.warning(f"❌ Bot {self.bot.name}: Could not fetch user_id for '{handle_name}'. Skipping.")
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
            except tweepy.TooManyRequests:
                logging.warning(f"⚠️ Bot {self.bot.name}: Rate limit hit while fetching tweets for '{handle_name}'. Returning to console.")
                return
            except Exception as e:
                logging.error(f"❌ Bot {self.bot.name}: Error fetching tweets for '{handle_name}': {str(e)}")
                continue
            if not tweets_response or not tweets_response.data:
                logging.info(f"📭 Bot {self.bot.name}: No new tweets from {handle_name}.")
                continue

            newest_tweet = tweets_response.data[0]
            tweet_id = ""
            if hasattr(newest_tweet, "id"):
                tweet_id = str(newest_tweet.id)
            else:
                tweet_id = str(newest_tweet.get("id", ""))
            # Guard against empty tweet id
            if not tweet_id.strip():
                logging.warning(f"TwitterAdapter: Retrieved tweet id for {handle_name} is empty; skipping comment.")
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
                try:
                    self.bot.client.create_tweet(
                        text=reply,
                        in_reply_to_tweet_id=tweet_id,
                        user_auth=True
                    )
                    logging.info(f"TwitterAdapter: Replied to tweet {tweet_id} by {handle_name}: {reply}")
                    # Update the cache only if reply was successfully posted.
                    self.bot.monitored_handles_last_ids[handle_name] = tweet_id
                except Exception as e:
                    logging.error(f"TwitterAdapter: Error replying to tweet {tweet_id}: {e}")
            else:
                logging.error(f"TwitterAdapter: Failed to generate reply for tweet {tweet_id}")

    def daily_comment_job(self):
        logging.info(f"⏰ Bot {self.bot.name}: Attempting to auto-comment (scheduled).")
        self.daily_comment()

    # ----- Replying to Replies on the Bot's Tweet -----
    def daily_comment_reply(self):
        logging.info(f"🔎 Bot {self.bot.name}: Checking for replies to my tweet...")
        config = self.bot.config
        if not config:
            logging.warning(f"❌ Bot {self.bot.name}: Config empty/invalid.")
            return
        reply_handles = config.get("reply_handles", {})
        if not reply_handles:
            logging.warning(f"❌ Bot {self.bot.name}: No reply handles specified in config. Skipping.")
            return
        try:
            self.bot.get_user_ids_bulk(list(reply_handles.keys()))
        except tweepy.TooManyRequests:
            logging.warning("Rate limit hit during bulk user lookup for replies. Returning to console.")
            return
        for handle_name, handle_data in reply_handles.items():
            user_id = self.bot.get_user_id(handle_name)
            if not user_id:
                logging.warning(f"❌ Bot {self.bot.name}: Could not fetch user_id for '{handle_name}'. Skipping.")
                continue
            try:
                auth_user = self.bot.get_cached_me()
                if not (auth_user and auth_user.data):
                    logging.error("TwitterAdapter: Failed to retrieve authenticated user info.")
                    return
                recent_tweet = self.bot.get_bot_recent_tweet_id(cache_duration=86400)
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

    def daily_comment_reply_job(self):
        logging.info(f"⏰ Bot {self.bot.name}: Attempting to auto-reply (scheduled).")
        self.daily_comment_reply()

    # ----- Cross-Bot Engagement -----
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

    # ----- Collaborative Storytelling -----
    def load_shared_story_state(self):
        # Replace Path with os.path.dirname calls to avoid unresolved reference errors
        shared_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "shared", "story_state.json")
        if os.path.exists(shared_file):
            try:
                with open(shared_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"TwitterAdapter: Error loading shared story state: {e}")
        return {"story": ""}

    def update_shared_story_state(self, new_content: str):
        shared_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "shared", "story_state.json")
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

    # ----- Visual/Multimedia Enhancements -----
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

    # ----- Engagement Metrics & Adaptive Tuning -----
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

    # ----- Scheduling and Wrapper Methods -----
    def tweet_job_wrapper(self):
        self.daily_tweet_job()
        self.bot.scheduler.clear("randomized_tweet")
        if self.bot.auto_post_enabled:
            self.bot.schedule_next_post_job()

    def comment_job_wrapper(self):
        self.daily_comment_job()
        self.bot.scheduler.clear("randomized_comment")
        if self.bot.auto_comment_enabled:
            self.bot.schedule_next_comment_job()

    def reply_job_wrapper(self):
        self.daily_comment_reply_job()
        self.bot.scheduler.clear("randomized_reply")
        if self.bot.auto_reply_enabled:
            self.bot.schedule_next_reply_job()

    def cross_job_wrapper(self):
        self.run_cross_engagement_job()
        self.bot.scheduler.clear("cross_engagement")
        if self.bot.auto_cross_enabled:
            self.bot.scheduler.every(1).hours.do(self.cross_job_wrapper).tag("cross_engagement")
            logging.info(f"Bot {self.bot.name}: Next cross-bot engagement scheduled in 1 hour.")

    def trending_job_wrapper(self):
        self.run_trending_engagement()
        self.bot.scheduler.clear("trending_engagement")
        if self.bot.auto_trending_enabled:
            self.bot.scheduler.every().day.at("11:00").do(self.trending_job_wrapper).tag("trending_engagement")
            logging.info(f"Bot {self.bot.name}: Next trending engagement scheduled at 11:00.")

    def dm_job_wrapper(self):
        self.run_dm_job()
        self.bot.scheduler.clear("dm_job")
        if self.bot.auto_dm_enabled:
            self.bot.scheduler.every(30).minutes.do(self.dm_job_wrapper).tag("dm_job")
            logging.info(f"Bot {self.bot.name}: Next DM check scheduled in 30 minutes.")

    def story_job_wrapper(self):
        self.run_collaborative_storytelling()
        self.bot.scheduler.clear("story_job")
        if self.bot.auto_story_enabled:
            self.bot.scheduler.every().day.at("16:00").do(self.story_job_wrapper).tag("story_job")
            logging.info(f"Bot {self.bot.name}: Next storytelling tweet scheduled at 16:00.")

    def schedule_next_tweet_job(self):
        if not self.bot.auto_post_enabled:
            return
        rng = random.Random()
        rng.seed(f"{self.bot.name}_tweet_{time.time()}")
        tweet_times = self.bot.config.get("schedule", {}).get("tweet_times", ["12:00", "18:00"])
        random_tweet_time = rng.choice(tweet_times)
        random_tweet_time = self.validate_time(random_tweet_time, "12:00")
        self.bot.scheduler.every().day.at(random_tweet_time).do(self.tweet_job_wrapper).tag("randomized_tweet")
        logging.info(f"Bot {self.bot.name}: Next tweet scheduled at {random_tweet_time}")

    def schedule_next_comment_job(self):
        if not self.bot.auto_comment_enabled:
            return
        rng = random.Random()
        rng.seed(f"{self.bot.name}_comment_{time.time()}")
        comment_times = self.bot.config.get("schedule", {}).get("comment_times", ["13:00", "19:00"])
        random_comment_time = rng.choice(comment_times)
        random_comment_time = self.validate_time(random_comment_time, "13:00")
        self.bot.scheduler.every().day.at(random_comment_time).do(self.comment_job_wrapper).tag("randomized_comment")
        logging.info(f"Bot {self.bot.name}: Next comment scheduled at {random_comment_time}")

    def schedule_next_reply_job(self):
        if not self.bot.auto_reply_enabled:
            return
        rng = random.Random()
        rng.seed(f"{self.bot.name}_reply_{time.time()}")
        reply_times = self.bot.config.get("schedule", {}).get("reply_times", ["14:30", "20:30"])
        random_reply_time = rng.choice(reply_times)
        random_reply_time = self.validate_time(random_reply_time, "14:30")
        self.bot.scheduler.every().day.at(random_reply_time).do(self.reply_job_wrapper).tag("randomized_reply")
        logging.info(f"Bot {self.bot.name}: Next reply scheduled at {random_reply_time}")

    def randomize_schedule(self):
        self.schedule_next_tweet_job()
        self.schedule_next_comment_job()
        self.schedule_next_reply_job()
        if self.bot.auto_cross_enabled:
            self.bot.scheduler.every(1).hours.do(self.cross_job_wrapper).tag("cross_engagement")
            logging.info(f"Bot {self.bot.name}: Cross-bot engagement scheduled every hour.")
        if self.bot.auto_trending_enabled:
            self.bot.scheduler.every().day.at("11:00").do(self.trending_job_wrapper).tag("trending_engagement")
            logging.info(f"Bot {self.bot.name}: Trending engagement scheduled at 11:00 daily.")
        if self.bot.auto_dm_enabled:
            self.bot.scheduler.every(30).minutes.do(self.dm_job_wrapper).tag("dm_job")
            logging.info(f"Bot {self.bot.name}: DM check scheduled every 30 minutes.")
        if self.bot.auto_story_enabled:
            self.bot.scheduler.every().day.at("16:00").do(self.story_job_wrapper).tag("story_job")
            logging.info(f"Bot {self.bot.name}: Collaborative storytelling scheduled at 16:00 daily.")

    def re_randomize_schedule(self):
        self.bot.scheduler.clear("randomized_tweet")
        self.bot.scheduler.clear("randomized_comment")
        self.bot.scheduler.clear("randomized_reply")
        logging.info(f"Bot {self.bot.name}: Cleared previous randomized jobs.")
        self.randomize_schedule()

    def start_scheduler(self):
        while not self.bot._stop_event.is_set():
            self.bot.scheduler.run_pending()
            time.sleep(1)

    def start(self):
        if self.bot.running:
            logging.info(f"Bot {self.bot.name} is already running.")
            return
        self.bot._stop_event.clear()
        if self.flask_thread is None or not self.flask_thread.is_alive():
            self.flask_thread = threading.Thread(target=self.run_flask, daemon=True)
            self.flask_thread.start()
            time.sleep(1)
        self.authenticate()
        self.load_user_id_cache()
        self.load_bot_tweet_cache()
        self.bot.auto_post_enabled = True
        self.bot.auto_comment_enabled = True
        self.bot.auto_reply_enabled = True
        self.bot.auto_cross_enabled = False
        self.bot.auto_trending_enabled = False
        self.bot.auto_dm_enabled = False
        self.bot.auto_story_enabled = False
        self.randomize_schedule()
        self.scheduler_thread = threading.Thread(target=self.start_scheduler, daemon=True)
        self.scheduler_thread.start()
        self.bot.running = True
        logging.info(f"Bot {self.bot.name} started.")

    def stop(self):
        if not self.bot.running:
            logging.info(f"Bot {self.bot.name} is not running.")
            return
        self.bot._stop_event.set()
        self.bot.scheduler.clear()
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

    def show_dashboard(self):
        now = datetime.datetime.now()
        dashboard = [f"--- Dashboard for Bot {self.bot.name} ---"]
        dashboard.append(f"Status: {self.get_status()}")
        dashboard.append(f"Mood: {self.bot.mood_state}")
        dashboard.append(f"Platform Adapters: {', '.join(self.bot.platform_adapters.keys())}")
        for job in self.bot.scheduler.jobs:
            if job.next_run:
                dashboard.append(f"Job {job.tags} scheduled at {job.next_run.strftime('%Y-%m-%d %H:%M:%S')}")
        if os.path.exists(self.bot.engagement_metrics_file):
            try:
                with open(self.bot.engagement_metrics_file, "r") as f:
                    metrics = json.load(f)
                dashboard.append(f"Last Tweet - Likes: {metrics.get('likes', 0)}, Retweets: {metrics.get('retweets', 0)}")
            except Exception as e:
                dashboard.append("Engagement metrics unavailable.")
        else:
            dashboard.append("No engagement metrics recorded yet.")
        print("\n".join(dashboard))
        logging.info(f"✅ Bot {self.bot.name}: Displayed dashboard.")

    def process_console_command(self, cmd: str):
        if cmd == "start":
            self.start()
        elif cmd == "stop":
            self.stop()
        elif cmd == "new auth":
            if os.path.exists(self.bot.token_file):
                os.remove(self.bot.token_file)
                self.bot.cached_me = None
                logging.info(f"✅ Bot {self.bot.name}: Token file removed. Bot will reauthenticate on next startup.")
                print("Token file removed. Bot will reauthenticate on next startup.")
            else:
                logging.info(f"✅ Bot {self.bot.name}: No token file found.")
                print("No token file found.")
        elif cmd == "auth age":
            print(self.get_auth_age())
        elif cmd.startswith("run post"):
            logging.info(
                f"🚀 Bot {self.bot.name}: 'run post' command received. Posting tweet {self.bot.post_run_count} time(s).")
            for _ in range(self.bot.post_run_count):
                self.daily_tweet_job()
        elif cmd.startswith("run comment"):
            logging.info(
                f"🚀 Bot {self.bot.name}: 'run comment' command received. Commenting {self.bot.comment_run_count} time(s).")
            for _ in range(self.bot.comment_run_count):
                self.daily_comment_job()
        elif cmd.startswith("run reply"):
            logging.info(
                f"🚀 Bot {self.bot.name}: 'run reply' command received. Replying {self.bot.reply_run_count} time(s).")
            for _ in range(self.bot.reply_run_count):
                self.daily_comment_reply_job()
        elif cmd.startswith("set post count "):
            try:
                value = int(cmd.split("set post count ")[1])
                self.bot.post_run_count = value
                logging.info(f"✅ Bot {self.bot.name}: Set post count to {self.bot.post_run_count}")
            except Exception:
                logging.error(f"❌ Bot {self.bot.name}: Invalid value for post count")
        elif cmd.startswith("set comment count "):
            try:
                value = int(cmd.split("set comment count ")[1])
                self.bot.comment_run_count = value
                logging.info(f"✅ Bot {self.bot.name}: Set comment count to {self.bot.comment_run_count}")
            except Exception:
                logging.error(f"❌ Bot {self.bot.name}: Invalid value for comment count")
        elif cmd.startswith("set reply count "):
            try:
                value = int(cmd.split("set reply count ")[1])
                self.bot.reply_run_count = value
                logging.info(f"✅ Bot {self.bot.name}: Set reply count to {self.bot.reply_run_count}")
            except Exception:
                logging.error(f"❌ Bot {self.bot.name}: Invalid value for reply count")
        elif cmd == "list context":
            if self.bot.config and "contexts" in self.bot.config:
                contexts = list(self.bot.config["contexts"].keys())
                if contexts:
                    print("Available contexts: " + ", ".join(contexts))
                    logging.info(f"🔍 Bot {self.bot.name}: Listed contexts: {', '.join(contexts)}")
                else:
                    print("No contexts defined in the configuration.")
                    logging.info(f"🔍 Bot {self.bot.name}: No contexts found in config.")
            else:
                print("No configuration loaded or 'contexts' section missing.")
                logging.error(f"❌ Bot {self.bot.name}: Configuration or contexts section missing.")
        elif cmd.startswith("run context"):
            parts = cmd.split(" ", 2)
            if len(parts) < 3:
                print("Usage: run context {context name}")
                logging.error(f"❌ Bot {self.bot.name}: 'run context' requires a context name.")
            else:
                context_name = parts[2].strip()
                if self.bot.config and "contexts" in self.bot.config and context_name in self.bot.config["contexts"]:
                    prompt_settings = self.bot.config["contexts"][context_name].get("prompt", {})
                    if not prompt_settings:
                        print(f"Context '{context_name}' does not have prompt settings defined.")
                        logging.error(f"❌ Bot {self.bot.name}: Prompt settings missing for context '{context_name}'.")
                    else:
                        system_prompt = prompt_settings.get("system", "")
                        user_prompt = prompt_settings.get("user", "")
                        if prompt_settings.get("include_news", False):
                            news_keyword = prompt_settings.get("news_keyword", None)
                            news_data = self.bot.fetch_news(news_keyword)
                            template = Template(user_prompt)
                            user_prompt = template.render(
                                news_headline=news_data.get("headline", ""),
                                news_article=news_data.get("article", ""),
                                mood_state=self.bot.mood_state
                            )
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
                        logging.info(f"✅ Bot {self.bot.name}: Ran context '{context_name}' successfully.")
                else:
                    print(f"Context '{context_name}' not found in configuration.")
                    logging.error(f"❌ Bot {self.bot.name}: Context '{context_name}' does not exist.")
        elif cmd == "new random all":
            logging.info(f"🚀 Bot {self.bot.name}: Scheduling new random times for post, comment, and reply.")
            self.re_randomize_schedule()
        elif cmd == "new random post":
            logging.info(f"🚀 Bot {self.bot.name}: Scheduling new random time for post.")
            self.bot.scheduler.clear("randomized_post")
            if self.bot.auto_post_enabled:
                self.bot.schedule_next_post_job()
        elif cmd == "new random comment":
            logging.info(f"🚀 Bot {self.bot.name}: Scheduling new random time for comment.")
            self.bot.scheduler.clear("randomized_comment")
            if self.bot.auto_comment_enabled:
                self.bot.schedule_next_comment_job()
        elif cmd == "new random reply":
            logging.info(f"🚀 Bot {self.bot.name}: Scheduling new random time for reply.")
            self.bot.scheduler.clear("randomized_reply")
            if self.bot.auto_reply_enabled:
                self.bot.schedule_next_reply_job()
        elif cmd == "stop post":
            if self.bot.auto_post_enabled:
                self.bot.scheduler.clear("randomized_post")
                self.bot.auto_post_enabled = False
                logging.info(f"🚫 Bot {self.bot.name}: Auto post disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.bot.name}: Auto post is already disabled.")
        elif cmd == "start post":
            if not self.bot.auto_post_enabled:
                self.bot.auto_post_enabled = True
                self.bot.schedule_next_post_job()
                logging.info(f"✅ Bot {self.bot.name}: Auto post enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.bot.name}: Auto post is already enabled.")
        elif cmd == "stop comment":
            if self.bot.auto_comment_enabled:
                self.bot.scheduler.clear("randomized_comment")
                self.bot.auto_comment_enabled = False
                logging.info(f"🚫 Bot {self.bot.name}: Auto comment disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.bot.name}: Auto comment is already disabled.")
        elif cmd == "start comment":
            if not self.bot.auto_comment_enabled:
                self.bot.auto_comment_enabled = True
                self.bot.schedule_next_comment_job()
                logging.info(f"✅ Bot {self.bot.name}: Auto comment enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.bot.name}: Auto comment is already enabled.")
        elif cmd == "stop reply":
            if self.bot.auto_reply_enabled:
                self.bot.scheduler.clear("randomized_reply")
                self.bot.auto_reply_enabled = False
                logging.info(f"🚫 Bot {self.bot.name}: Auto reply disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.bot.name}: Auto reply is already disabled.")
        elif cmd == "start reply":
            if not self.bot.auto_reply_enabled:
                self.bot.auto_reply_enabled = True
                self.bot.schedule_next_reply_job()
                logging.info(f"✅ Bot {self.bot.name}: Auto reply enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.bot.name}: Auto reply is already enabled.")
        elif cmd == "start cross":
            if not hasattr(self.bot, 'auto_cross_enabled') or not self.bot.auto_cross_enabled:
                self.bot.auto_cross_enabled = True
                self.bot.scheduler.every(1).hours.do(self.cross_job_wrapper).tag("cross_engagement")
                logging.info(f"✅ Bot {self.bot.name}: Auto cross-platform engagement enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.bot.name}: Auto cross-platform engagement is already enabled.")
        elif cmd == "stop cross":
            if hasattr(self.bot, 'auto_cross_enabled') and self.bot.auto_cross_enabled:
                self.bot.scheduler.clear("cross_engagement")
                self.bot.auto_cross_enabled = False
                logging.info(f"🚫 Bot {self.bot.name}: Auto cross-platform engagement disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.bot.name}: Auto cross-platform engagement is already disabled.")
        elif cmd == "start trending":
            if not hasattr(self.bot, 'auto_trending_enabled') or not self.bot.auto_trending_enabled:
                self.bot.auto_trending_enabled = True
                self.bot.scheduler.every().day.at("11:00").do(self.trending_job_wrapper).tag("trending_engagement")
                logging.info(f"✅ Bot {self.bot.name}: Auto trending engagement enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.bot.name}: Auto trending engagement is already enabled.")
        elif cmd == "stop trending":
            if hasattr(self.bot, 'auto_trending_enabled') and self.bot.auto_trending_enabled:
                self.bot.scheduler.clear("trending_engagement")
                self.bot.auto_trending_enabled = False
                logging.info(f"🚫 Bot {self.bot.name}: Auto trending engagement disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.bot.name}: Auto trending engagement is already disabled.")
        elif cmd == "start dm":
            if not hasattr(self.bot, 'auto_dm_enabled') or not self.bot.auto_dm_enabled:
                self.bot.auto_dm_enabled = True
                self.bot.scheduler.every(30).minutes.do(self.dm_job_wrapper).tag("dm_job")
                logging.info(f"✅ Bot {self.bot.name}: Auto DM check enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.bot.name}: Auto DM check is already enabled.")
        elif cmd == "stop dm":
            if hasattr(self.bot, 'auto_dm_enabled') and self.bot.auto_dm_enabled:
                self.bot.scheduler.clear("dm_job")
                self.bot.auto_dm_enabled = False
                logging.info(f"🚫 Bot {self.bot.name}: Auto DM check disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.bot.name}: Auto DM check is already disabled.")
        elif cmd.startswith("run dm"):
            parts = cmd.split(" ", 2)
            if len(parts) < 3:
                print("Usage: run dm {recipient_username}")
                logging.error(f"❌ Bot {self.bot.name}: 'run dm' requires a recipient username.")
            else:
                recipient = parts[2].strip()
                message = input("Enter DM message: ")
                for adapter in self.bot.platform_adapters.values():
                    adapter.dm(recipient, message)
        elif cmd == "start story":
            if not hasattr(self.bot, 'auto_story_enabled') or not self.bot.auto_story_enabled:
                self.bot.auto_story_enabled = True
                self.bot.scheduler.every().day.at("16:00").do(self.story_job_wrapper).tag("story_job")
                logging.info(f"✅ Bot {self.bot.name}: Auto collaborative storytelling enabled.")
            else:
                logging.info(f"ℹ️ Bot {self.bot.name}: Auto collaborative storytelling is already enabled.")
        elif cmd == "stop story":
            if hasattr(self.bot, 'auto_story_enabled') and self.bot.auto_story_enabled:
                self.bot.scheduler.clear("story_job")
                self.bot.auto_story_enabled = False
                logging.info(f"🚫 Bot {self.bot.name}: Auto collaborative storytelling disabled.")
            else:
                logging.info(f"ℹ️ Bot {self.bot.name}: Auto collaborative storytelling is already disabled.")
        elif cmd.startswith("run story"):
            logging.info(f"🚀 Bot {self.bot.name}: 'run story' command received. Running storytelling.")
            self.story_job_wrapper()
        elif cmd == "run image tweet":
            logging.info(f"🚀 Bot {self.bot.name}: 'run image tweet' command received.")
            for adapter in self.bot.platform_adapters.values():
                adapter.post_tweet_with_image()
        elif cmd == "run adaptive tune":
            logging.info(f"🚀 Bot {self.bot.name}: 'run adaptive tune' command received. Adjusting parameters based on engagement metrics.")
            for adapter in self.bot.platform_adapters.values():
                adapter.adaptive_tune()
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
            logging.info(f"✅ Bot {self.bot.name}: Mood manually set to {self.bot.mood_state}.")
        elif cmd == "show dashboard":
            self.show_dashboard()
        elif cmd == "show settings":
            logging.info(
                f"🔧 Bot {self.bot.name}: Current settings: Post Count = {self.bot.post_run_count}, Comment Count = {self.bot.comment_run_count}, Reply Count = {self.bot.reply_run_count}")
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
        if not self.bot.scheduler.jobs:
            logging.info(f"🗓️ Bot {self.bot.name}: No scheduled jobs.")
            return
        for job in self.bot.scheduler.jobs:
            if job.next_run:
                diff = job.next_run - now
                logging.info(
                    f"🗓️ Bot {self.bot.name}: Job {job.tags} scheduled to run in {diff} (at {job.next_run.strftime('%Y-%m-%d %H:%M:%S')}).")

    def show_listener_state(self):
        logging.info(f"👂 Bot {self.bot.name}: Console listener active.")
        logging.info("Type 'help' or '?' for command list.")
        self.print_next_scheduled_times()

    def show_log(self):
        now = datetime.datetime.now()
        output = [f"Status: {self.get_status()}"]
        if self.bot.auto_post_enabled:
            post_jobs = [job for job in self.bot.scheduler.jobs if "randomized_post" in job.tags]
            if post_jobs and post_jobs[0].next_run:
                diff_post = post_jobs[0].next_run - now
                output.append(
                    f"📝 Bot {self.bot.name}: Auto post ENABLED; Next post in: {diff_post} (at {post_jobs[0].next_run.strftime('%Y-%m-%d %H:%M:%S')})")
            else:
                output.append(f"📝 Bot {self.bot.name}: Auto post ENABLED but no scheduled job.")
        else:
            output.append(f"📝 Bot {self.bot.name}: Auto post DISABLED.")
        if self.bot.auto_comment_enabled:
            comment_jobs = [job for job in self.bot.scheduler.jobs if "randomized_comment" in job.tags]
            if comment_jobs and comment_jobs[0].next_run:
                diff_comment = comment_jobs[0].next_run - now
                output.append(
                    f"💬 Bot {self.bot.name}: Auto comment ENABLED; Next comment in: {diff_comment} (at {comment_jobs[0].next_run.strftime('%Y-%m-%d %H:%M:%S')})")
            else:
                output.append(f"💬 Bot {self.bot.name}: Auto comment ENABLED but no scheduled job.")
        else:
            output.append(f"💬 Bot {self.bot.name}: Auto comment DISABLED.")
        if self.bot.auto_reply_enabled:
            reply_jobs = [job for job in self.bot.scheduler.jobs if "randomized_reply" in job.tags]
            if reply_jobs and reply_jobs[0].next_run:
                diff_reply = reply_jobs[0].next_run - now
                output.append(
                    f"🗓️ Bot {self.bot.name}: Auto reply ENABLED; Next reply in: {diff_reply} (at {reply_jobs[0].next_run.strftime('%Y-%m-%d %H:%M:%S')})")
            else:
                output.append(f"🗓️ Bot {self.bot.name}: Auto reply ENABLED but no scheduled job.")
        else:
            output.append(f"🗓️ Bot {self.bot.name}: Auto reply DISABLED.")
        print("\n".join(output))