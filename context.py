# context.py
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error


def _normalize(v: Optional[str]) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    if v.startswith("{"):
        try:
            v = json.loads(v).get("url", "").strip()
        except Exception:
            pass
    if v.startswith('"') and v.endswith('"'):
        v = v[1:-1].strip()
    return v.strip()


def _post(url: str, payload: Dict[str, Any]) -> None:
    url = _normalize(url)
    if not url:
        raise RuntimeError("Webhook URL missing")

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status not in (200, 204):
                raise RuntimeError(f"Discord HTTP {r.status}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Discord HTTP {e.code}") from e


def post_discord_team(content: str = "", embeds: Optional[List[Dict[str, Any]]] = None) -> None:
    payload: Dict[str, Any] = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    _post(os.getenv("DISCORD_TEAM_WEBHOOK", ""), payload)


def post_discord_props(content: str = "", embeds: Optional[List[Dict[str, Any]]] = None) -> None:
    payload: Dict[str, Any] = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    _post(os.getenv("DISCORD_PROPS_WEBHOOK", ""), payload)


def post_discord_log(content: str = "", embeds: Optional[List[Dict[str, Any]]] = None) -> None:
    payload: Dict[str, Any] = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    _post(os.getenv("DISCORD_LOG_WEBHOOK", ""), payload)
