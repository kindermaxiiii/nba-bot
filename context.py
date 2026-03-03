# context.py
import os
import json
import urllib.request
from typing import Any, Dict, Optional


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return v


def post_discord(payload: Dict[str, Any], webhook_url: Optional[str] = None) -> bool:
    """
    Post a JSON payload to a Discord webhook.
    Returns True if sent, False otherwise.
    """
    url = webhook_url or _env("DISCORD_WEBHOOK_URL")
    if not url:
        print("[context] DISCORD_WEBHOOK_URL missing -> skip post")
        return False

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            _ = resp.read()
        return True
    except Exception as e:
        print(f"[context] Discord post failed: {e}")
        return False
