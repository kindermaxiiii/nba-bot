from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, Optional


def post_discord(webhook_url: str, username: str, content: str) -> bool:
    """
    Envoie un message sur Discord via webhook.
    Retourne True si succès, False sinon.
    """
    if not webhook_url:
        return False

    payload: Dict[str, Any] = {
        "username": username,
        "content": content,
        "allowed_mentions": {"parse": []},
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            # Discord webhooks renvoient souvent 204 No Content
            return 200 <= resp.status < 300 or resp.status == 204
    except Exception:
        return False
