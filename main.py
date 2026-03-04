# main.py
import os
from context import post_discord_team, post_discord_props, post_discord_log

def _mask(v: str) -> str:
    v = (v or "").strip()
    if not v:
        return "MISSING"
    if len(v) < 12:
        return "SET(short)"
    return f"SET({v[:4]}...{v[-4:]})"

def main() -> None:
    # 1) Print env to Actions logs
    print("DEBUG ENV:",
          "TEAM=", _mask(os.getenv("DISCORD_TEAM_WEBHOOK", "")),
          "PROPS=", _mask(os.getenv("DISCORD_PROPS_WEBHOOK", "")),
          "LOG=", _mask(os.getenv("DISCORD_LOG_WEBHOOK", "")))

    # 2) Send 3 messages (plain text only)
    post_discord_log(content="✅ NBA BOT TEST: LOG webhook works")
    post_discord_team(content="✅ NBA BOT TEST: TEAM webhook works")
    post_discord_props(content="✅ NBA BOT TEST: PROPS webhook works")

    print("DEBUG: messages sent (if webhooks valid).")

if __name__ == "__main__":
    main()
