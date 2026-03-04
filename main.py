# main.py
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from context import post_discord_team, post_discord_props, post_discord_log
from odds_api import fetch_odds_with_fallback
from engine import load_config, run_engine


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return v


def _discord_embed_top(title: str, picks: List[Dict[str, Any]], color: int = 3066993) -> Dict[str, Any]:
    if not picks:
        return {"title": title, "description": "Aucun pick.", "color": 15158332}

    lines: List[str] = []
    for i, p in enumerate(picks, 1):
        line = p.get("line")
        line_s = f" | Line: {line}" if line is not None else ""

        odds = p.get("odds")
        odds_s = f"{float(odds):.2f}" if odds is not None else "?"

        book = p.get("book") or "?"
        match = p.get("match") or "?"
        market = p.get("market") or "?"
        selection = p.get("selection") or "?"

        def pct(x: Any) -> str:
            try:
                return f"{float(x) * 100:.2f}%"
            except Exception:
                return "?"

        def num(x: Any) -> str:
            try:
                return f"{float(x):.1f}"
            except Exception:
                return "?"

        lines.append(
            f"**#{i}** — {match}\n"
            f"Market: **{market}**{line_s}\n"
            f"Pick: **{selection}** @ **{odds_s}** ({book})\n"
            f"p_model: {pct(p.get('p_model'))} | p_mkt: {pct(p.get('p_mkt'))} | p_real: {pct(p.get('fair_prob'))}\n"
            f"EV: {pct(p.get('ev'))} | Edge: {pct(p.get('edge'))} | Dev: {pct(p.get('dev'))} | Score: {num(p.get('score'))}/100\n"
        )

    return {"title": title, "description": "\n".join(lines), "color": color}


def main() -> None:
    cfg = load_config("config.json")

    # Secrets sanity
    if not _env("ODDS_API_KEY"):
        post_discord_log(content="❌ ODDS_API_KEY manquante (GitHub Secrets).")
        return

    # Fetch odds
    try:
        games, fetch_meta = fetch_odds_with_fallback(
            markets=cfg.markets,
            regions_priority=cfg.regions_priority,
        )
    except Exception as e:
        post_discord_log(content=f"❌ Erreur fetch OddsAPI: {repr(e)}")
        return

    if not games:
        post_discord_log(content="❌ Aucun match reçu depuis OddsAPI (liste vide).")
        return

    # Run engine
    try:
        result = run_engine(games, cfg)
    except Exception as e:
        post_discord_log(content=f"❌ Erreur run_engine: {repr(e)}")
        return

    team_picks = result.get("team_picks", []) or []
    prop_picks = result.get("prop_picks", []) or []
    meta = result.get("meta", {}) or {}

    # META embed always to LOG
    meta_embed = {
        "title": "NBA BOT — META",
        "description": (
            f"Games: {meta.get('games')} | markets_tested: {meta.get('markets_tested')} | "
            f"regions_used: {fetch_meta.get('regions_used')} | markets(cfg): {cfg.markets} | "
            f"odds_range: [{meta.get('min_odds')}, {meta.get('max_odds')}] | "
            f"clip: {meta.get('clip_vs_market')} | model_w: {meta.get('model_weight')} | "
            f"maxML/day: {meta.get('max_ml_per_day')}"
        ),
        "color": 3447003,
    }
    post_discord_log(content="", embeds=[meta_embed])

    # TEAM
    post_discord_team(content="", embeds=[_discord_embed_top("NBA — TOP 3 TEAM", team_picks, color=3066993)])

    # PROPS
    if not prop_picks:
        post_discord_props(content="", embeds=[{"title": "NBA — TOP 3 PROPS", "description": "Aucun pick (props non câblés / markets indisponibles).", "color": 15158332}])
    else:
        post_discord_props(content="", embeds=[_discord_embed_top("NBA — TOP 3 PROPS", prop_picks, color=10181046)])

    # Actions log
    print(json.dumps({"fetch_meta": fetch_meta, "meta": meta, "team_picks": team_picks, "prop_picks": prop_picks}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
