# context.py
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional


def _normalize_webhook(v: str) -> str:
    v = (v or "").strip()
    if v.startswith("{"):
        try:
            v = json.loads(v).get("url", "").strip()
        except Exception:
            pass
    if v.startswith('"') and v.endswith('"'):
        v = v[1:-1]
    return v.strip()


def _post(url: str, payload: Dict[str, Any], timeout: int = 20) -> bool:
    url = _normalize_webhook(url)
    if not url:
        return False
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status in (200, 204)
    except urllib.error.HTTPError as e:
        # Never crash the bot due to webhook errors; just print.
        print(f"❌ Discord webhook HTTP {e.code}: {e.reason}")
        return False
    except Exception as e:
        print(f"❌ Discord webhook error: {repr(e)}")
        return False


def post_discord_team(content: str = "", embeds: Optional[List[Dict[str, Any]]] = None) -> bool:
    url = os.getenv("DISCORD_TEAM_WEBHOOK", "")
    payload: Dict[str, Any] = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    return _post(url, payload)


def post_discord_props(content: str = "", embeds: Optional[List[Dict[str, Any]]] = None) -> bool:
    url = os.getenv("DISCORD_PROPS_WEBHOOK", "")
    payload: Dict[str, Any] = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    return _post(url, payload)


def post_discord_log(content: str = "", embeds: Optional[List[Dict[str, Any]]] = None) -> bool:
    url = os.getenv("DISCORD_LOG_WEBHOOK", "")
    payload: Dict[str, Any] = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    return _post(url, payload)
