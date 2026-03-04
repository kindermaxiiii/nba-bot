# context.py
import os
import json
import urllib.request
from typing import Optional, Dict, Any, List


def _normalize_webhook(value: str) -> str:
    """
    Accept:
    - raw Discord webhook URL
    - JSON object copied from Discord (contains "url")
    - accidentally quoted string
    """
    v = (value or "").strip()
    if not v:
        return ""

    # If it's a JSON blob, extract "url"
    if v.startswith("{") and v.endswith("}"):
        try:
            obj = json.loads(v)
            v = str(obj.get("url", "")).strip()
        except Exception:
            pass

    # Strip accidental wrapping quotes
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1].strip()

    return v.strip()


def _post(url: str, payload: Dict[str, Any]) -> None:
    url = _normalize_webhook(url)
    if not url:
        print("❌ Webhook URL manquant/invalid. Payload:", json.dumps(payload, ensure_ascii=False)[:1000])
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
        print("Webhook (masked):", (url[:60] + "...") if len(url) > 60 else url)
        print("Payload:", json.dumps(payload, ensure_ascii=False)[:1000])


def post_discord_team(content: str = "", embeds: Optional[List[dict]] = None) -> None:
    url = os.getenv("DISCORD_TEAM_WEBHOOK", "")
    payload: Dict[str, Any] = {"content": content}
    if embeds is not None:
        payload["embeds"] = embeds
    _post(url, payload)


def post_discord_props(content: str = "", embeds: Optional[List[dict]] = None) -> None:
    url = os.getenv("DISCORD_PROPS_WEBHOOK", "")
    payload: Dict[str, Any] = {"content": content}
    if embeds is not None:
        payload["embeds"] = embeds
    _post(url, payload)


def post_discord_log(content: str = "", embeds: Optional[List[dict]] = None) -> None:
    url = os.getenv("DISCORD_LOG_WEBHOOK", "")
    payload: Dict[str, Any] = {"content": content}
    if embeds is not None:
        payload["embeds"] = embeds
    _post(url, payload)
