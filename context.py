import os
import json
import urllib.request
import urllib.error


def _clean(url):
    url = (url or "").strip()

    if url.startswith("{"):
        try:
            url = json.loads(url)["url"]
        except Exception:
            pass

    if url.startswith('"') and url.endswith('"'):
        url = url[1:-1]

    return url.strip()


def _send(url, payload):

    url = _clean(url)

    if not url:
        raise RuntimeError("Webhook missing")

    data = json.dumps(payload).encode()

    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=20) as r:
        if r.status not in (200, 204):
            raise RuntimeError(f"Discord HTTP {r.status}")


def post_log(msg):
    _send(os.getenv("DISCORD_LOG_WEBHOOK"), {"content": msg})


def post_team(msg):
    _send(os.getenv("DISCORD_TEAM_WEBHOOK"), {"content": msg})


def post_props(msg):
    _send(os.getenv("DISCORD_PROPS_WEBHOOK"), {"content": msg})
