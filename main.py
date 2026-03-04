# main.py
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from context import post_discord_team, post_discord_props, post_discord_log
from odds_api import fetch_odds_with_fallback
from engine import load_config, run_engine


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()


def _discord_embed_top(title: str, picks: List[Dict[str, Any]], color: int) -> Dict[str, Any]:
    if not picks:
        return {"title": title, "description": "Aucun pick.", "color": 15158332}

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

    lines: List[str] = []
    for i, p in enumerate(picks, 1):
        line = p.get("line")
        line_s = f" | Line: {line}" if line is not None else ""
        odds = p.get("odds")
        odds_s = f"{float(odds):.2f}" if odds is not None else "?"
        lines.append(
            f"**#{i}** — {p.get('match','?')}\n"
            f"Market: **{p.get('market','?')}**{line_s}\n"
            f"Pick: **{p.get('selection','?')}** @ **{odds_s}** ({p.get('book','?')})\n"
            f"p_model: {pct(p.get('p_model'))} | p_mkt: {pct(p.get('p_mkt'))} | p_real: {pct(p.get('fair_prob'))}\n"
            f"EV: {pct(p.get('ev'))} | Edge: {pct(p.get('edge'))} | Dev: {pct(p.get('dev'))} | Score: {num(p.get('score'))}/100\n"
        )

    return {"title": title, "description": "\n".join(lines), "color": color}


def main() -> None:
    cfg = load_config("config.json")

    # 0) sanity
    if not _env("ODDS_API_KEY"):
        post_discord_log(content="❌ ODDS_API_KEY manquante.")
        return

    # 1) fetch odds with slate auto + fallback regions/books
    games, meta_fetch = fetch_odds_with_fallback(
        markets=cfg.markets,
        regions_priority=cfg.regions_priority,
        sport_key=getattr(cfg, "sport_key", "basketball_nba"),
        preferred_books=getattr(cfg, "preferred_books", None),
    )

    if not games:
        post_discord_log(
            content=(
                "❌ Aucun match reçu depuis OddsAPI.\n"
                f"regions_tried={meta_fetch.get('regions_tried')}\n"
                f"error={meta_fetch.get('error')}"
            )
        )
        return

    # 2) run engine
    result = run_engine(games, cfg)

    team_picks = result.get("team_picks", []) or []
    prop_picks = result.get("prop_picks", []) or []
    meta = result.get("meta", {}) or {}

    # 3) log meta
    meta_embed = {
        "title": "NBA BOT — META",
        "description": (
            f"Games: {meta.get('games')} | markets_tested: {meta.get('markets_tested')} | "
            f"model_weight: {meta.get('model_weight')} | clip: {meta.get('clip_vs_market')} | "
            f"maxML/day: {meta.get('max_ml_per_day')} | maxMLodds: {meta.get('max_odds_ml')} | "
            f"region_used: {meta_fetch.get('region')} | regions_tried: {meta_fetch.get('regions_tried')}"
        ),
        "color": 3447003,
    }
    post_discord_log(content="", embeds=[meta_embed])

    # 4) send picks
    post_discord_team(content="", embeds=[_discord_embed_top("NBA — TOP 3 TEAM", team_picks, 3066993)])
    post_discord_props(content="", embeds=[_discord_embed_top("NBA — TOP 3 PROPS", prop_picks, 10181046)])

    print(json.dumps({"meta_fetch": meta_fetch, "meta": meta, "team_picks": team_picks, "prop_picks": prop_picks}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
