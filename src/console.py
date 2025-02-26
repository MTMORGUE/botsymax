import sys
import logging
from bot import Bot

def print_help_master():
    help_text = (
        "Available master console commands:\n"
        "  list                 - List available bots and their status.\n"
        "  start all            - Start all bots.\n"
        "  start {bot name}     - Start the specified bot.\n"
        "  stop all             - Stop all bots.\n"
        "  stop {bot name}      - Stop the specified bot.\n"
        "  <bot name>           - Enter control mode for the specified bot.\n"
        "  show log all         - Show scheduled log info for all bots with status.\n"
        "  help or ?            - Show this help message.\n"
        "  exit                 - Exit the master console."
    )
    print(help_text)

def print_master_prompt():
    print("\nMaster Console: Enter command ('list', 'start', 'stop', bot name, 'show log all', 'help' or 'exit'):")

def print_bot_prompt(bot):
    print(f"\n[Bot {bot.name}] ", end='')

def bot_menu(bot: Bot):
    logging.info(f"🔔 Now controlling bot '{bot.name}'.")
    print(f"\nNow controlling bot '{bot.name}'. Type commands for this bot (type 'back' to return to master console).")
    bot.show_listener_state()
    while True:
        print_bot_prompt(bot)
        cmd = input().strip().lower()
        if cmd == "back":
            logging.info(f"🔔 Exiting control of bot '{bot.name}'. Returning to master console.")
            break
        try:
            bot.process_console_command(cmd)
        except Exception as e:
            logging.error(f"❌ Error executing command '{cmd}': {e}")
            input("Press Enter to continue...")

def master_console(bots: dict):
    while True:
        print_master_prompt()
        selection = input().strip()
        if selection.lower() == "exit":
            logging.info("Exiting master console.")
            sys.exit(0)
        elif selection.lower() == "list":
            logging.info("Available bots:")
            for bot_name, bot in bots.items():
                logging.info(f" - {bot_name} (Status: {bot.get_status()})")
            input("Press Enter to continue in Master Console...")
        elif selection.lower() in ["help", "?"]:
            print_help_master()
            input("Press Enter to continue in Master Console...")
        elif selection.lower() == "show log all":
            for bot in bots.values():
                print(f"--- {bot.name} (Status: {bot.get_status()}) ---")
                bot.show_log()
                print()
            input("Press Enter to continue in Master Console...")
        elif selection.lower() == "start all":
            for bot in bots.values():
                bot.start()
            logging.info("All bots started.")
            input("Press Enter to continue in Master Console...")
        elif selection.lower() == "stop all":
            for bot in bots.values():
                bot.stop()
            logging.info("All bots stopped.")
            input("Press Enter to continue in Master Console...")
        elif selection.lower().startswith("start "):
            bot_name = selection[6:].strip()
            if bot_name in bots:
                bots[bot_name].start()
            else:
                logging.info(f"Bot '{bot_name}' not found.")
            input("Press Enter to continue in Master Console...")
        elif selection.lower().startswith("stop "):
            bot_name = selection[5:].strip()
            if bot_name in bots:
                bots[bot_name].stop()
            else:
                logging.info(f"Bot '{bot_name}' not found.")
            input("Press Enter to continue in Master Console...")
        elif selection in bots:
            bot_menu(bots[selection])
        else:
            logging.info("Invalid selection. Try again. (Type 'help' for a list of commands.)")
            input("Press Enter to continue in Master Console...")