# context.py
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict, Optional, List


def _post(url: str, payload: Dict[str, Any]) -> None:
    url = (url or "").strip()
    if not url:
        print("❌ Webhook URL manquant. Payload:", json.dumps(payload, ensure_ascii=False)[:1000])
        return

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            # Discord webhooks usually return 204
            print(f"✅ Discord webhook HTTP {resp.status}")
    except Exception as e:
        print("❌ Erreur en envoyant sur Discord:", repr(e))
        print("Webhook:", url[:60] + "..." if len(url) > 60 else url)
        print("Payload:", json.dumps(payload, ensure_ascii=False)[:1000])


def post_discord_team(content: str = "", embeds: Optional[List[dict]] = None) -> None:
    url = os.getenv("DISCORD_TEAM_WEBHOOK", "")
    payload: Dict[str, Any] = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    _post(url, payload)


def post_discord_props(content: str = "", embeds: Optional[List[dict]] = None) -> None:
    url = os.getenv("DISCORD_PROPS_WEBHOOK", "")
    payload: Dict[str, Any] = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    _post(url, payload)


def post_discord_log(content: str = "", embeds: Optional[List[dict]] = None) -> None:
    url = os.getenv("DISCORD_LOG_WEBHOOK", "")
    payload: Dict[str, Any] = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    _post(url, payload)
