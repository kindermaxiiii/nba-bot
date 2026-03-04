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
    return v


def _flatten_games(obj: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def rec(x: Any) -> None:
        if x is None:
            return
        if isinstance(x, dict):
            if "data" in x:
                rec(x["data"])
                return
            if "home_team" in x and "away_team" in x:
                out.append(x)
                return
            for v in x.values():
                rec(v)
            return
        if isinstance(x, list):
            for it in x:
                rec(it)

    rec(obj)
    return out


def _discord_embed_top(title: str, picks: List[Dict[str, Any]], color: int) -> Dict[str, Any]:
    if not picks:
        return {
            "title": title,
            "description": "Aucun pick (EV>=0 introuvable après filtres).",
            "color": 15158332,
        }

    lines: List[str] = []

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

    for i, p in enumerate(picks, 1):
        line = p.get("line")
        line_s = f" | Line: {line}" if line is not None else ""

        odds = p.get("odds")
        odds_s = f"{float(odds):.2f}" if odds is not None else "?"

        book = p.get("book") or "?"
        match = p.get("match") or "?"
        market = p.get("market") or "?"
        selection = p.get("selection") or "?"

        p_model = p.get("p_model")
        p_mkt = p.get("p_mkt")
        p_real = p.get("fair_prob")

        ev = p.get("ev")
        edge = p.get("edge")
        dev = p.get("dev")
        score = p.get("score")

        lines.append(
            f"**#{i}** — {match}\n"
            f"Market: **{market}**{line_s}\n"
            f"Pick: **{selection}** @ **{odds_s}** ({book})\n"
            f"p_model: {pct(p_model)} | p_mkt: {pct(p_mkt)} | p_real: {pct(p_real)}\n"
            f"EV: {pct(ev)} | Edge: {pct(edge)} | Dev: {pct(dev)} | Score: {num(score)}/100\n"
        )

    return {"title": title, "description": "\n".join(lines), "color": color}


def main() -> None:
    cfg = load_config("config.json")

    api_key = _env("ODDS_API_KEY")
    if not api_key:
        msg = "❌ ODDS_API_KEY manquante dans les Secrets GitHub."
        post_discord_log(content=msg)
        print(msg)
        return

    # Fetch odds
    try:
        raw_games = fetch_odds_with_fallback(
            markets=cfg.markets,
            regions_priority=cfg.regions_priority,
        )
    except Exception as e:
        msg = f"❌ Erreur fetch OddsAPI: {repr(e)}"
        post_discord_log(content=msg)
        print(msg)
        return

    games = _flatten_games(raw_games)
    if not games:
        msg = "❌ Aucun match reçu depuis OddsAPI (games vide après flatten). Vérifie ODDS_API_KEY / régions / quota."
        post_discord_log(content=msg)
        print(msg)
        return

    # Run engine
    try:
        result = run_engine(games, cfg)
    except Exception as e:
        msg = f"❌ Erreur run_engine: {repr(e)}"
        post_discord_log(content=msg)
        print(msg)
        return

    team_picks = result.get("team_picks", []) or []
    prop_picks = result.get("prop_picks", []) or []
    meta = result.get("meta", {}) or {}

    # LOG META
    meta_embed = {
        "title": "NBA BOT — META",
        "description": (
            f"Games(flat): {meta.get('games')} | "
            f"markets_tested: {meta.get('markets_tested')} | "
            f"model_weight: {meta.get('model_weight')} | "
            f"clip: {meta.get('clip_vs_market')} | "
            f"maxML/day: {meta.get('max_ml_per_day')} | "
            f"maxMLodds: {meta.get('max_odds_ml')}"
        ),
        "color": 3447003,
    }
    post_discord_log(content="", embeds=[meta_embed])

    # TEAM
    team_embed = _discord_embed_top("NBA — TOP 3 TEAM", team_picks, color=3066993)
    post_discord_team(content="", embeds=[team_embed])

    # PROPS
    props_embed = _discord_embed_top("NBA — TOP 3 PROPS", prop_picks, color=10181046)
    post_discord_props(content="", embeds=[props_embed])

    print(json.dumps({"meta": meta, "team_picks": team_picks, "prop_picks": prop_picks}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
