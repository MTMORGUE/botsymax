import os
import sys
import logging
import datetime
from pathlib import Path

def setup_logging():
    # Store logs in the project root alongside logs.txt
    log_file = os.path.join(Path(__file__).parent.parent, "logs.txt")
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

def load_environment():
    """
    Loads .env from the project root directory.
    """
    from dotenv import load_dotenv
    env_path = Path(__file__).parent.parent / ".env"
    load_dotenv(dotenv_path=env_path)
    # Enable insecure transport for OAuth if needed.
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

def exit_with_error(message: str):
    logging.error(message)
    sys.exit(1)