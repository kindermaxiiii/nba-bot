# context.py
import os
import json
import urllib.request


def post_discord(payload: dict) -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

    if not url:
        # On log explicitement dans les logs GitHub Actions
        print("❌ DISCORD_WEBHOOK_URL manquant (Secret non défini ou non passé au workflow).")
        print("Payload à envoyer:", json.dumps(payload, ensure_ascii=False)[:1000])
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
        print("Payload:", json.dumps(payload, ensure_ascii=False)[:1000])
