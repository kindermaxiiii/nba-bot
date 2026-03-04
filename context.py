# context.py
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

import requests


def _normalize(v: Optional[str]) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    if v.startswith("{"):
        try:
            v = json.loads(v).get("url", "").strip()
        except Exception:
            pass
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].strip()
    return v.strip()


def _mask(url: str) -> str:
    url = url or ""
    if "/api/webhooks/" in url:
        head, tail = url.split("/api/webhooks/", 1)
        parts = tail.split("/")
        if len(parts) >= 2:
            return head + "/api/webhooks/" + parts[0] + "/" + parts[1][:6] + "..."
    return (url[:32] + "...") if len(url) > 35 else url


def _post(url: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
    url = _normalize(url)
    if not url:
        return False, "EMPTY_URL"
    try:
        r = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json", "User-Agent": "nba-bot/definitive"},
            timeout=20,
        )
        if r.status_code in (200, 204):
            return True, f"HTTP_{r.status_code}"
        snippet = (r.text or "").strip().replace("\n", " ")[:180]
        return False, f"HTTP_{r.status_code} {snippet}"
    except Exception as e:
        return False, f"ERR_{type(e).__name__}:{e!s}"


def post_discord_log(content: str = "", embeds: Optional[List[Dict[str, Any]]] = None) -> None:
    url = os.getenv("DISCORD_LOG_WEBHOOK", "")
    payload: Dict[str, Any] = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    ok, st = _post(url, payload)
    if not ok:
        print(f"[DISCORD][LOG] failed: {st} url={_mask(_normalize(url))}")


def post_discord_team(content: str = "", embeds: Optional[List[Dict[str, Any]]] = None) -> None:
    url = os.getenv("DISCORD_TEAM_WEBHOOK", "")
    payload: Dict[str, Any] = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    ok, st = _post(url, payload)
    if not ok:
        print(f"[DISCORD][TEAM] failed: {st} url={_mask(_normalize(url))}")


def post_discord_props(content: str = "", embeds: Optional[List[Dict[str, Any]]] = None) -> None:
    url = os.getenv("DISCORD_PROPS_WEBHOOK", "")
    payload: Dict[str, Any] = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    ok, st = _post(url, payload)
    if not ok:
        print(f"[DISCORD][PROPS] failed: {st} url={_mask(_normalize(url))}")
