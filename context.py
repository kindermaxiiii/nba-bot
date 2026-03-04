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
    # Allow secrets to be either JSON blob or plain URL
    if v.startswith("{"):
        try:
            v = json.loads(v).get("url", "").strip()
        except Exception:
            pass
    # Strip accidental wrapping quotes
    if v.startswith('"') and v.endswith('"'):
        v = v[1:-1].strip()
    return v.strip()


def _mask_url(url: str) -> str:
    # Keep only first part to help debug without leaking token
    if not url:
        return ""
    if "/api/webhooks/" in url:
        a = url.split("/api/webhooks/")[0] + "/api/webhooks/"
        b = url.split("/api/webhooks/")[1]
        # b looks like "{id}/{token...}"
        parts = b.split("/")
        if len(parts) >= 2:
            return a + parts[0] + "/" + parts[1][:6] + "..."
    return url[:35] + "..."


def _post(url: str, payload: Dict[str, Any], timeout: int = 20) -> Tuple[bool, str]:
    url = _normalize(url)
    if not url:
        return False, "EMPTY_URL"

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "nba-bot/1.0 (+github-actions)",
    }

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if r.status_code in (200, 204):
            return True, f"HTTP_{r.status_code}"
        # Return a short response snippet for diagnostics
        snippet = (r.text or "").strip().replace("\n", " ")[:200]
        return False, f"HTTP_{r.status_code} RESP={snippet}"
    except Exception as e:
        return False, f"ERR_{type(e).__name__}:{e!s}"


def post_discord_team(content: str = "", embeds: Optional[List[Dict[str, Any]]] = None, fail_hard: bool = False) -> None:
    payload: Dict[str, Any] = {}
    if content is not None:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds

    url = os.getenv("DISCORD_TEAM_WEBHOOK", "")
    ok, st = _post(url, payload)
    if not ok:
        print(f"[DISCORD][TEAM] post failed: {st} url={_mask_url(_normalize(url))}")
        if fail_hard:
            raise RuntimeError(f"Discord TEAM failed: {st}")


def post_discord_props(content: str = "", embeds: Optional[List[Dict[str, Any]]] = None, fail_hard: bool = False) -> None:
    payload: Dict[str, Any] = {}
    if content is not None:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds

    url = os.getenv("DISCORD_PROPS_WEBHOOK", "")
    ok, st = _post(url, payload)
    if not ok:
        print(f"[DISCORD][PROPS] post failed: {st} url={_mask_url(_normalize(url))}")
        if fail_hard:
            raise RuntimeError(f"Discord PROPS failed: {st}")


def post_discord_log(content: str = "", embeds: Optional[List[Dict[str, Any]]] = None, fail_hard: bool = False) -> None:
    payload: Dict[str, Any] = {}
    if content is not None:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds

    url = os.getenv("DISCORD_LOG_WEBHOOK", "")
    ok, st = _post(url, payload)
    if not ok:
        print(f"[DISCORD][LOG] post failed: {st} url={_mask_url(_normalize(url))}")
        if fail_hard:
            raise RuntimeError(f"Discord LOG failed: {st}")
