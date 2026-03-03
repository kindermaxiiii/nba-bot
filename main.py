# main.py
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Union

from context import post_discord
from odds_api import fetch_odds_with_fallback
from engine import load_config, run_engine


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return v


def _flatten_games(obj: Any) -> List[Dict[str, Any]]:
    """
    odds_api.fetch_odds_with_fallback can return:
    - List[Dict] (ideal)
    - List[List[Dict]] (one list per region)
    - Dict with "data" holding list(s)
    We convert everything to List[Dict].
    """
    out: List[Dict[str, Any]] = []

    def rec(x: Any) -> None:
        if x is None:
            return
        if isinstance(x, dict):
            # if a wrapper dict
            if "data" in x:
                rec(x["data"])
                return
            # if it's a match dict (has home/away)
            if "home_team" in x and "away_team" in x:
                out.append(x)
                return
            # otherwise try to scan values
            for v in x.values():
                rec(v)
            return
        if isinstance(x, list):
            for it in x:
                rec(it)
            return
        # ignore other types

    rec(obj)
    return out


def _discord_embed_top(title: str, picks: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not picks:
        return {
            "title": title,
            "description": "Aucun pick (EV>=0 introuvable après filtre modèle + discipline).",
            "color": 15158332,
        }

    lines = []
    for i, p in enumerate(picks, 1):
        line = p.get("line")
        line_s = f" | Line: {line}" if line is not None else ""
        lines.append(
            f"**#{i}** — {p['match']}\n"
            f"Market: **{p['market']}**{line_s}\n"
            f"Pick: **{p['selection']}** @ **{p['odds']:.2f}** ({p['book']})\n"
            f"p_model: {p['p_model']*100:.2f}% | p_mkt: {p['p_mkt']*100:.2f}% | p_real: {p['fair_prob']*100:.2f}%\n"
            f"EV: {p['ev']*100:.2f}% | Edge: {p['edge']*100:.2f}% | Dev: {p['dev']*100:.2f}% | Score: {p['score']:.1f}/100\n"
        )

    return {
        "title": title,
        "description": "\n".join(lines),
        "color": 3066993,
    }


def main() -> None:
    cfg = load_config("config.json")

    api_key = _env("ODDS_API_KEY")
    if not api_key:
        payload = {"content": "❌ ODDS_API_KEY manquante dans les Secrets GitHub."}
        post_discord(payload)
        print(payload["content"])
        return

    raw_games = fetch_odds_with_fallback(
        markets=cfg.markets,
        regions_priority=cfg.regions_priority,
    )

    games = _flatten_games(raw_games)

    if not games:
        payload = {"content": "❌ Aucun match reçu depuis OddsAPI (team_games vide après flatten). Vérifie ODDS_API_KEY / régions / quota."}
        post_discord(payload)
        print(payload["content"])
        return

    result = run_engine(games, cfg)

    team_picks = result.get("team_picks", [])
    prop_picks = result.get("prop_picks", [])
    meta = result.get("meta", {})

    embeds = [
        {
            "title": "NBA BOT — META",
            "description": (
                f"Games(flat): {meta.get('games')} | model_weight: {meta.get('model_weight')} | "
                f"clip: {meta.get('clip_vs_market')} | maxML/day: {meta.get('max_ml_per_day')} | "
                f"maxMLodds: {meta.get('max_odds_ml')}"
            ),
            "color": 3447003,
        },
        _discord_embed_top("NBA — TOP 3 TEAM (MODEL-FIRST, anti-ML longshots)", team_picks),
        _discord_embed_top("NBA — TOP 3 PROPS (à câbler si props dispo)", prop_picks),
    ]

    payload = {"content": "", "embeds": embeds}
    post_discord(payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
