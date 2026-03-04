# context.py
import os
import json
import urllib.request
from typing import Optional, Dict, Any, List


def _post(url: str, payload: Dict[str, Any]) -> None:
    url = (url or "").strip()
    if not url:
        print("❌ DISCORD WEBHOOK URL MANQUANT")
        print("Payload:", json.dumps(payload, ensure_ascii=False)[:1200])
        return

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "nba-bot/1.0"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            status = getattr(resp, "status", None)
            body = resp.read().decode("utf-8", errors="ignore")
            print(f"✅ Discord webhook HTTP {status}")
            if body:
                print("Discord body:", body[:1200])
    except Exception as e:
        # IMPORTANT: ceci s'affiche DANS GitHub Actions logs
        print("❌ Erreur Discord:", repr(e))
        print("Webhook:", url[:60] + "..." if len(url) > 60 else url)
        print("Payload:", json.dumps(payload, ensure_ascii=False)[:1200])


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
