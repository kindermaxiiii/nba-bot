# context.py
import os
import json
import urllib.request
from typing import Optional, Dict, Any


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
            status = resp.status
            body = resp.read().decode("utf-8", errors="ignore")
            print(f"✅ Discord webhook response: HTTP {status}")
            if body:
                print("Discord response body:", body[:1000])
    except Exception as e:
        print("❌ Erreur en envoyant sur Discord:", repr(e))
        print("Webhook:", url[:60] + "..." if len(url) > 60 else url)
        print("Payload:", json.dumps(payload, ensure_ascii=False)[:1000])


def post_discord_team(content: str = "", embeds: Optional[list] = None) -> None:
    url = os.getenv("DISCORD_TEAM_WEBHOOK", "")
    payload = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    _post(url, payload)


def post_discord_props(content: str = "", embeds: Optional[list] = None) -> None:
    url = os.getenv("DISCORD_PROPS_WEBHOOK", "")
    payload = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    _post(url, payload)


def post_discord_log(content: str = "", embeds: Optional[list] = None) -> None:
    url = os.getenv("DISCORD_LOG_WEBHOOK", "")
    payload = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    _post(url, payload)
