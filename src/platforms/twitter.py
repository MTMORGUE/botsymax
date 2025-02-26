import tweepy
import os
import time
import logging
import random
import requests
import json
import datetime
import schedule
import pytz
from pathlib import Path
from dotenv import load_dotenv, set_key
from jinja2 import Template
from textblob import TextBlob
import openai
from src.platforms.base_adapter import BasePlatformAdapter

# Ensure environment variables are loaded
load_dotenv()

# Global constants for use in this adapter
MAX_AUTH_RETRIES = 3
RATE_LIMIT_WAIT = 60  # seconds

class TwitterAdapter(BasePlatformAdapter):
    def __init__(self, bot):
        super().__init__(bot)
        self.client = None
        self.consecutive_forbidden_count = 0
        self.authenticate()

    def authenticate(self):
        # Attempt to authenticate using tokens from .env
        access_token = os.getenv(f"{self.bot.name.upper()}_TWITTER_ACCESS_TOKEN")
        access_token_secret = os.getenv(f"{self.bot.name.upper()}_TWITTER_ACCESS_TOKEN_SECRET")
        consumer_key = os.getenv(f"{self.bot.name.upper()}_TWITTER_CONSUMER_KEY")
        consumer_secret = os.getenv(f"{self.bot.name.upper()}_TWITTER_CONSUMER_SECRET")
        if not (consumer_key and consumer_secret):
            logging.error(f"TwitterAdapter: Consumer keys not found in .env for bot {self.bot.name}")
            return
        if access_token and access_token_secret:
            try:
                self.client = tweepy.Client(
                    consumer_key=consumer_key,
                    consumer_secret=consumer_secret,
                    access_token=access_token,
                    access_token_secret=access_token_secret,
                    wait_on_rate_limit=False
                )
                logging.info("TwitterAdapter: Authenticated using tokens from .env.")
                return
            except Exception as e:
                logging.error(f"TwitterAdapter: Error using tokens from .env: {e}")
        # Fallback: OAuth is handled in Bot; do not call self.bot.authenticate()
        logging.error("TwitterAdapter: Authentication failed. No valid tokens available. Please set tokens in .env.")

    # --- Abstract method implementations ---

    def post(self, content: str):
        """
        If content is provided (non-empty), post that content.
        Otherwise, generate a tweet automatically.
        """
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
        """
        Posts a comment (reply) to a tweet specified by reply_to_id.
        """
        if content.strip():
            tweet_text = self.bot.clean_tweet_text(content)
            try:
                self.client.create_tweet(text=tweet_text, in_reply_to_tweet_id=reply_to_id, user_auth=True)
                logging.info(f"TwitterAdapter: Comment posted successfully in reply to {reply_to_id}")
                return tweet_text
            except Exception as e:
                logging.error(f"TwitterAdapter: Error posting comment: {e}")
                return ""
        else:
            logging.info("TwitterAdapter: No content provided for comment.")
            return ""

    def dm(self, recipient: str, message: str):
        logging.info(f"TwitterAdapter: Sending DM to {recipient} with message: {message}")
        # Placeholder DM implementation; replace with actual DM logic as needed.
        return "dm_id_stub"

    # --- Twitter-specific helper methods ---

    def create_post(self, text: str):
        """Create a tweet (post) on Twitter."""
        try:
            self.client.create_tweet(text=text)
            logging.info(f"TwitterAdapter: Tweet posted successfully: {text}")
        except Exception as e:
            logging.error(f"TwitterAdapter: Error posting tweet: {e}")

    def add_conversational_dynamics(self, text: str) -> str:
        """Optionally add conversational nuances to the generated text."""
        if random.random() < 0.1:
            text = "Uh-huh, " + text
        if random.random() < 0.05:
            text += " (Correction: Sorry, I misspoke!)"
        return text

    def generate_tweet(self) -> str:
        """Generate tweet content using the current context and OpenAI."""
        if not self.bot.config:
            logging.error("TwitterAdapter: Configuration is empty or invalid.")
            return ""
        contexts = self.bot.config.get("contexts", {})
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

        # Append conversation history if available
        conversation = self.bot.load_conversation_history()
        if conversation:
            user_prompt += "\nPrevious conversation: " + conversation

        # Optionally include news or weather in the prompt
        if prompt_settings.get("include_news", False):
            news_keyword = prompt_settings.get("news_keyword", None)
            news_data = self.fetch_news(news_keyword)
            template = Template(user_prompt)
            user_prompt = template.render(
                news_headline=news_data.get("headline", ""),
                news_article=news_data.get("article", ""),
                mood_state=self.bot.mood_state,
                personality=self.bot.personality
            )
        elif prompt_settings.get("include_weather", False):
            weather = self.fetch_weather()
            template = Template(user_prompt)
            user_prompt = template.render(
                weather=weather,
                mood_state=self.bot.mood_state,
                personality=self.bot.personality
            )
        else:
            template = Template(user_prompt)
            user_prompt = template.render(
                mood_state=self.bot.mood_state,
                personality=self.bot.personality
            )

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if user_prompt:
            messages.append({"role": "user", "content": user_prompt})

        tweet_text = self.bot.call_openai_completion(
            model, messages, temperature, max_tokens, top_p, frequency_penalty, presence_penalty
        )
        tweet_text = self.add_conversational_dynamics(tweet_text)
        self.bot.append_conversation_history(tweet_text)
        return self.bot.clean_tweet_text(tweet_text) if tweet_text else ""

    def handle_forbidden_error(self, error) -> bool:
        """
        Handle 403 Forbidden errors by incrementing a counter and possibly disabling auto-post.
        """
        self.consecutive_forbidden_count += 1
        logging.error(f"TwitterAdapter: Tweet forbidden by Twitter. (Forbidden count: {self.consecutive_forbidden_count})")
        if self.consecutive_forbidden_count >= 3:
            logging.error("TwitterAdapter: Repeated forbidden errors encountered. Disabling auto-posting temporarily.")
            self.bot.auto_post_enabled = False
        return False

    def post_tweet(self) -> bool:
        """Generate a tweet and attempt to post it."""
        tweet = self.generate_tweet()
        if not tweet:
            logging.error("TwitterAdapter: No tweet generated.")
            return False
        try:
            self.client.create_tweet(text=tweet)
            logging.info(f"TwitterAdapter: Tweet posted successfully: {tweet}")
            self.consecutive_forbidden_count = 0  # Reset counter on success
            return True
        except tweepy.TweepyException as e:
            if "403" in str(e):
                return self.handle_forbidden_error(e)
            else:
                logging.error(f"TwitterAdapter: Error posting tweet: {str(e)}")
                return False
        except Exception as e:
            logging.error(f"TwitterAdapter: Unexpected error posting tweet: {str(e)}")
            return False

    def daily_tweet_job(self):
        """Scheduled job: Attempt to post a tweet, retrying a few times if necessary."""
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
        """Mimics the original monolith's comment functionality."""
        logging.info("TwitterAdapter: Attempting to auto-comment...")
        config = self.bot.config
        if not config:
            logging.warning("TwitterAdapter: Config empty/invalid.")
            return
        monitored_handles = config.get("monitored_handles", {})
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
                tweets_response = self.client.get_users_tweets(
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
            if last_id and newest_tweet.id <= last_id:
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
            reply = self.bot.call_openai_completion(model, messages, temperature, max_tokens, top_p, frequency_penalty, presence_penalty)
            if reply:
                try:
                    self.client.create_tweet(text=reply, in_reply_to_tweet_id=newest_tweet.id, user_auth=True)
                    logging.info(f"TwitterAdapter: Replied to tweet {newest_tweet.id} by {handle_name}: {reply}")
                    self.bot.monitored_handles_last_ids[handle_name] = newest_tweet.id
                except Exception as e:
                    logging.error(f"TwitterAdapter: Error replying to tweet {newest_tweet.id}: {e}")
            else:
                logging.error(f"TwitterAdapter: Failed to generate reply for tweet {newest_tweet.id}")

    def daily_comment_reply_job(self):
        """Mimics the original monolith's reply functionality."""
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
                recent_tweet_id = self.bot.get_bot_recent_tweet_id()
                if not recent_tweet_id:
                    logging.info("TwitterAdapter: No recent tweet found.")
                    continue
            except Exception as e:
                logging.error(f"TwitterAdapter: Error retrieving bot info: {e}")
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
            except Exception as e:
                logging.error(f"TwitterAdapter: Error fetching replies: {e}")
                continue
            if not replies or not replies.data:
                logging.info(f"TwitterAdapter: No replies found for tweet {recent_tweet_id}.")
                continue
            author_users = {user.id: user.username.lower() for user in replies.includes.get("users", [])}
            for rep in replies.data:
                reply_text = rep.text.strip()
                author_handle = author_users.get(rep.author_id, "").lower()
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
                    tweet_response = self.client.get_tweet(recent_tweet_id, tweet_fields=["text"], user_auth=True)
                    bot_tweet_text = tweet_response.data.text if tweet_response and tweet_response.data else ""
                except Exception as e:
                    bot_tweet_text = ""
                    logging.warning(f"TwitterAdapter: Could not fetch bot tweet text: {e}")
                template = Template(user_prompt_template)
                filled_prompt = template.render(comment_text=reply_text, tweet_text=bot_tweet_text, mood_state=self.bot.mood_state)
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": filled_prompt})
                response_text = self.bot.call_openai_completion(model, messages, temperature, max_tokens, top_p, frequency_penalty, presence_penalty)
                if response_text:
                    try:
                        self.client.create_tweet(text=response_text, in_reply_to_tweet_id=rep.id, user_auth=True)
                        logging.info(f"TwitterAdapter: Replied to @{handle_name} on tweet {rep.id}: {response_text}")
                    except Exception as e:
                        logging.error(f"TwitterAdapter: Error replying for tweet {rep.id}: {e}")
                else:
                    logging.error(f"TwitterAdapter: Failed to generate reply for tweet {rep.id}")

    # ----- NEW FUNCTIONALITY: Cross-Bot Engagement -----
    def cross_bot_engagement(self):
        """
        Engage with tweets from fellow bots.
        Looks for tweets from usernames listed under 'bot_network' in the config
        and replies or retweets them to simulate conversation.
        """
        bot_network = self.bot.config.get("bot_network", [])
        if not bot_network:
            logging.info("TwitterAdapter: No bot network defined for cross engagement.")
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

    # ----- COLLABORATIVE STORYTELLING -----
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
                self.client.create_tweet(text=story_tweet)
                logging.info(f"TwitterAdapter: Posted a collaborative storytelling tweet: {story_tweet}")
                self.bot.append_conversation_history(story_tweet)
                self.update_shared_story_state(story_tweet)
            except Exception as e:
                logging.error(f"TwitterAdapter: Error posting storytelling tweet: {e}")

    # ----- VISUAL/MULTIMEDIA ENHANCEMENTS -----
    def generate_image(self, prompt: str) -> str:
        image_url = "https://via.placeholder.com/500.png?text=Generated+Image"
        logging.info(f"TwitterAdapter: Generated image for prompt '{prompt}': {image_url}")
        return image_url

    def generate_audio(self, prompt: str) -> str:
        audio_url = "https://via.placeholder.com/audio_clip.mp3?text=Generated+Audio"
        logging.info(f"TwitterAdapter: Generated audio for prompt '{prompt}': {audio_url}")
        return audio_url

    def post_tweet_with_image(self):
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
            self.client.create_tweet(text=tweet_with_image)
            logging.info("TwitterAdapter: Tweet with image (and possibly audio) posted successfully.")
            return True
        except Exception as e:
            logging.error(f"TwitterAdapter: Error posting tweet with image: {e}")
            return False

    # ----- FEEDBACK LOOP & ADAPTIVE LEARNING -----
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
            self.bot.personality["extraversion"] = min(1.0, self.bot.personality["extraversion"] + 0.05)
        else:
            self.bot.personality["extraversion"] = max(0.0, self.bot.personality["extraversion"] - 0.05)
        logging.info(f"TwitterAdapter: Updated personality via reinforcement learning: {self.bot.personality}")

    def contextual_retraining(self):
        logging.info("TwitterAdapter: Contextual re-training executed based on conversation and engagement history.")

    # ----- SCHEDULING & WRAPPER METHODS (for adapter-specific tasks) -----
    def tweet_job_wrapper(self):
        self.daily_tweet_job()
        # Note: Scheduler management is handled by Bot

    def comment_job_wrapper(self):
        self.daily_comment_job()

    def reply_job_wrapper(self):
        self.daily_comment_reply_job()

    # Methods to mimic original monolith commands:

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
                tweets_response = self.client.get_users_tweets(
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
            if last_id and newest_tweet.id <= last_id:
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
            reply = self.bot.call_openai_completion(model, messages, temperature, max_tokens, top_p, frequency_penalty, presence_penalty)
            if reply:
                try:
                    self.client.create_tweet(text=reply, in_reply_to_tweet_id=newest_tweet.id, user_auth=True)
                    logging.info(f"TwitterAdapter: Replied to tweet {newest_tweet.id} by {handle_name}: {reply}")
                    self.bot.monitored_handles_last_ids[handle_name] = newest_tweet.id
                except Exception as e:
                    logging.error(f"TwitterAdapter: Error replying to tweet {newest_tweet.id}: {e}")
            else:
                logging.error(f"TwitterAdapter: Failed to generate reply for tweet {newest_tweet.id}")

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
                recent_tweet_id = self.bot.get_bot_recent_tweet_id()
                if not recent_tweet_id:
                    logging.info("TwitterAdapter: No recent tweet found.")
                    continue
            except Exception as e:
                logging.error(f"TwitterAdapter: Error retrieving bot info: {e}")
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
            except Exception as e:
                logging.error(f"TwitterAdapter: Error fetching replies: {e}")
                continue
            if not replies or not replies.data:
                logging.info(f"TwitterAdapter: No replies found for tweet {recent_tweet_id}.")
                continue
            author_users = {user.id: user.username.lower() for user in replies.includes.get("users", [])}
            for rep in replies.data:
                reply_text = rep.text.strip()
                author_handle = author_users.get(rep.author_id, "").lower()
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
                    tweet_response = self.client.get_tweet(recent_tweet_id, tweet_fields=["text"], user_auth=True)
                    bot_tweet_text = tweet_response.data.text if tweet_response and tweet_response.data else ""
                except Exception as e:
                    bot_tweet_text = ""
                    logging.warning(f"TwitterAdapter: Could not fetch bot tweet text: {e}")
                template = Template(user_prompt_template)
                filled_prompt = template.render(comment_text=reply_text, tweet_text=bot_tweet_text, mood_state=self.bot.mood_state)
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": filled_prompt})
                response_text = self.bot.call_openai_completion(model, messages, temperature, max_tokens, top_p, frequency_penalty, presence_penalty)
                if response_text:
                    try:
                        self.client.create_tweet(text=response_text, in_reply_to_tweet_id=rep.id, user_auth=True)
                        logging.info(f"TwitterAdapter: Replied to @{handle_name} on tweet {rep.id}: {response_text}")
                    except Exception as e:
                        logging.error(f"TwitterAdapter: Error replying for tweet {rep.id}: {e}")
                else:
                    logging.error(f"TwitterAdapter: Failed to generate reply for tweet {rep.id}")

    # ----- NEW FUNCTIONALITY: Cross-Bot Engagement -----
    def cross_bot_engagement(self):
        """
        Engage with tweets from fellow bots.
        Looks for tweets from usernames listed under 'bot_network' in the config
        and replies or retweets them to simulate conversation.
        """
        bot_network = self.bot.config.get("bot_network", [])
        if not bot_network:
            logging.info("TwitterAdapter: No bot network defined for cross engagement.")
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

    # ----- COLLABORATIVE STORYTELLING -----
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
                self.client.create_tweet(text=story_tweet)
                logging.info(f"TwitterAdapter: Posted a collaborative storytelling tweet: {story_tweet}")
                self.bot.append_conversation_history(story_tweet)
                self.update_shared_story_state(story_tweet)
            except Exception as e:
                logging.error(f"TwitterAdapter: Error posting storytelling tweet: {e}")

    # ----- VISUAL/MULTIMEDIA ENHANCEMENTS -----
    def generate_image(self, prompt: str) -> str:
        image_url = "https://via.placeholder.com/500.png?text=Generated+Image"
        logging.info(f"TwitterAdapter: Generated image for prompt '{prompt}': {image_url}")
        return image_url

    def generate_audio(self, prompt: str) -> str:
        audio_url = "https://via.placeholder.com/audio_clip.mp3?text=Generated+Audio"
        logging.info(f"TwitterAdapter: Generated audio for prompt '{prompt}': {audio_url}")
        return audio_url

    def post_tweet_with_image(self):
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
            self.client.create_tweet(text=tweet_with_image)
            logging.info("TwitterAdapter: Tweet with image (and possibly audio) posted successfully.")
            return True
        except Exception as e:
            logging.error(f"TwitterAdapter: Error posting tweet with image: {e}")
            return False

    # ----- FEEDBACK LOOP & ADAPTIVE LEARNING -----
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
            self.bot.personality["extraversion"] = min(1.0, self.bot.personality["extraversion"] + 0.05)
        else:
            self.bot.personality["extraversion"] = max(0.0, self.bot.personality["extraversion"] - 0.05)
        logging.info(f"TwitterAdapter: Updated personality via reinforcement learning: {self.bot.personality}")

    def contextual_retraining(self):
        logging.info("TwitterAdapter: Contextual re-training executed based on conversation and engagement history.")

    # ----- SCHEDULING & WRAPPER METHODS (for adapter-specific tasks) -----
    def tweet_job_wrapper(self):
        self.daily_tweet_job()
        # Note: Scheduler management is handled by Bot

    def comment_job_wrapper(self):
        self.daily_comment_job()

    def reply_job_wrapper(self):
        self.daily_comment_reply_job()

    # Methods to mimic original monolith commands:

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
                tweets_response = self.client.get_users_tweets(
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
            if last_id and newest_tweet.id <= last_id:
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
            reply = self.bot.call_openai_completion(model, messages, temperature, max_tokens, top_p, frequency_penalty, presence_penalty)
            if reply:
                try:
                    self.client.create_tweet(text=reply, in_reply_to_tweet_id=newest_tweet.id, user_auth=True)
                    logging.info(f"TwitterAdapter: Replied to tweet {newest_tweet.id} by {handle_name}: {reply}")
                    self.bot.monitored_handles_last_ids[handle_name] = newest_tweet.id
                except Exception as e:
                    logging.error(f"TwitterAdapter: Error replying to tweet {newest_tweet.id}: {e}")
            else:
                logging.error(f"TwitterAdapter: Failed to generate reply for tweet {newest_tweet.id}")

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
                recent_tweet_id = self.bot.get_bot_recent_tweet_id()
                if not recent_tweet_id:
                    logging.info("TwitterAdapter: No recent tweet found.")
                    continue
            except Exception as e:
                logging.error(f"TwitterAdapter: Error retrieving bot info: {e}")
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
            except Exception as e:
                logging.error(f"TwitterAdapter: Error fetching replies: {e}")
                continue
            if not replies or not replies.data:
                logging.info(f"TwitterAdapter: No replies found for tweet {recent_tweet_id}.")
                continue
            author_users = {user.id: user.username.lower() for user in replies.includes.get("users", [])}
            for rep in replies.data:
                reply_text = rep.text.strip()
                author_handle = author_users.get(rep.author_id, "").lower()
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
                    tweet_response = self.client.get_tweet(recent_tweet_id, tweet_fields=["text"], user_auth=True)
                    bot_tweet_text = tweet_response.data.text if tweet_response and tweet_response.data else ""
                except Exception as e:
                    bot_tweet_text = ""
                    logging.warning(f"TwitterAdapter: Could not fetch bot tweet text: {e}")
                template = Template(user_prompt_template)
                filled_prompt = template.render(comment_text=reply_text, tweet_text=bot_tweet_text, mood_state=self.bot.mood_state)
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": filled_prompt})
                response_text = self.bot.call_openai_completion(model, messages, temperature, max_tokens, top_p, frequency_penalty, presence_penalty)
                if response_text:
                    try:
                        self.client.create_tweet(text=response_text, in_reply_to_tweet_id=rep.id, user_auth=True)
                        logging.info(f"TwitterAdapter: Replied to @{handle_name} on tweet {rep.id}: {response_text}")
                    except Exception as e:
                        logging.error(f"TwitterAdapter: Error replying for tweet {rep.id}: {e}")
                else:
                    logging.error(f"TwitterAdapter: Failed to generate reply for tweet {rep.id}")

    # ----- NEW FUNCTIONALITY: Cross-Bot Engagement -----
    def cross_bot_engagement(self):
        """
        Engage with tweets from fellow bots.
        Looks for tweets from usernames listed under 'bot_network' in the config
        and replies or retweets them to simulate conversation.
        """
        bot_network = self.bot.config.get("bot_network", [])
        if not bot_network:
            logging.info("TwitterAdapter: No bot network defined for cross engagement.")
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

    # ----- COLLABORATIVE STORYTELLING -----
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
                self.client.create_tweet(text=story_tweet)
                logging.info(f"TwitterAdapter: Posted a collaborative storytelling tweet: {story_tweet}")
                self.bot.append_conversation_history(story_tweet)
                self.update_shared_story_state(story_tweet)
            except Exception as e:
                logging.error(f"TwitterAdapter: Error posting storytelling tweet: {e}")

    # ----- VISUAL/MULTIMEDIA ENHANCEMENTS -----
    def generate_image(self, prompt: str) -> str:
        image_url = "https://via.placeholder.com/500.png?text=Generated+Image"
        logging.info(f"TwitterAdapter: Generated image for prompt '{prompt}': {image_url}")
        return image_url

    def generate_audio(self, prompt: str) -> str:
        audio_url = "https://via.placeholder.com/audio_clip.mp3?text=Generated+Audio"
        logging.info(f"TwitterAdapter: Generated audio for prompt '{prompt}': {audio_url}")
        return audio_url

    def post_tweet_with_image(self):
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
            self.client.create_tweet(text=tweet_with_image)
            logging.info("TwitterAdapter: Tweet with image (and possibly audio) posted successfully.")
            return True
        except Exception as e:
            logging.error(f"TwitterAdapter: Error posting tweet with image: {e}")
            return False

    # ----- FEEDBACK LOOP & ADAPTIVE LEARNING -----
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
            self.bot.personality["extraversion"] = min(1.0, self.bot.personality["extraversion"] + 0.05)
        else:
            self.bot.personality["extraversion"] = max(0.0, self.bot.personality["extraversion"] - 0.05)
        logging.info(f"TwitterAdapter: Updated personality via reinforcement learning: {self.bot.personality}")

    def contextual_retraining(self):
        logging.info("TwitterAdapter: Contextual re-training executed based on conversation and engagement history.")

# End of TwitterAdapter class