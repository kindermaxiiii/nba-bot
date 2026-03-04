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


def _discord_embed_top(title: str, picks: List[Dict[str, Any]], color: int = 3066993) -> Dict[str, Any]:
    if not picks:
        return {
            "title": title,
            "description": "Aucun pick (EV>=0 introuvable après filtre modèle + discipline).",
            "color": 15158332,
        }

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

        p_model = p.get("p_model")
        p_mkt = p.get("p_mkt")
        p_real = p.get("fair_prob")  # chez toi fair_prob = p_real final

        def pct(x: Any) -> str:
            try:
                return f"{float(x) * 100:.2f}%"
            except Exception:
                return "?"

        ev = p.get("ev")
        edge = p.get("edge")
        dev = p.get("dev")
        score = p.get("score")

        def pct2(x: Any) -> str:
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
            f"p_model: {pct(p_model)} | p_mkt: {pct(p_mkt)} | p_real: {pct(p_real)}\n"
            f"EV: {pct2(ev)} | Edge: {pct2(edge)} | Dev: {pct2(dev)} | Score: {num(score)}/100\n"
        )

    return {"title": title, "description": "\n".join(lines), "color": color}


def main() -> None:
    cfg = load_config("config.json")

    # 0) quick discord start ping (LOG only)
    try:
        post_discord_log(content="NBA BOT: main.py started ✅")
    except Exception as e:
        # Do not crash on Discord, but print clearly
        print(f"❌ Discord LOG send failed: {repr(e)}")

    # 1) Check API key (OddsAPI)
    api_key = _env("ODDS_API_KEY")
    if not api_key:
        msg = "❌ ODDS_API_KEY manquante dans les Secrets GitHub."
        try:
            post_discord_log(content=msg)
        except Exception as e:
            print(f"❌ Discord LOG send failed: {repr(e)}")
        print(msg)
        return

    # 2) Fetch games (AUTO SLATE) + fallback regions + fallback books
    # preferred_books optional: if in config, use it, else None
    preferred_books = getattr(cfg, "preferred_books", None)
    try:
        games = fetch_odds_with_fallback(
            markets=cfg.markets,
            regions_priority=cfg.regions_priority,
            sport_key=getattr(cfg, "sport_key", "basketball_nba"),
            preferred_books=preferred_books,
        )
    except Exception as e:
        msg = f"❌ Erreur fetch OddsAPI: {repr(e)}"
        try:
            post_discord_log(content=msg)
        except Exception as ee:
            print(f"❌ Discord LOG send failed: {repr(ee)}")
        print(msg)
        return

    if not games:
        msg = (
            "❌ Aucun match reçu depuis OddsAPI (games vide).\n"
            "Vérifie: ODDS_API_KEY / régions / quota / marchés.\n"
        )
        try:
            post_discord_log(content=msg)
        except Exception as e:
            print(f"❌ Discord LOG send failed: {repr(e)}")
        print(msg)
        return

    # 3) Run engine
    try:
        result = run_engine(games, cfg)
    except Exception as e:
        msg = f"❌ Erreur run_engine: {repr(e)}"
        try:
            post_discord_log(content=msg)
        except Exception as ee:
            print(f"❌ Discord LOG send failed: {repr(ee)}")
        print(msg)
        return

    team_picks = result.get("team_picks", []) or []
    prop_picks = result.get("prop_picks", []) or []
    meta = result.get("meta", {}) or {}

    # 4) Always send META to LOG webhook
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
    try:
        post_discord_log(content="", embeds=[meta_embed])
    except Exception as e:
        print(f"❌ Discord LOG send failed: {repr(e)}")

    # 5) Send TEAM picks to TEAM webhook
    try:
        team_embed = _discord_embed_top("NBA — TOP 3 TEAM", team_picks, color=3066993)
        post_discord_team(content="", embeds=[team_embed])
    except Exception as e:
        print(f"❌ Discord TEAM send failed: {repr(e)}")

    # 6) Send PROPS picks to PROPS webhook
    try:
        props_embed = _discord_embed_top("NBA — TOP 3 PROPS", prop_picks, color=10181046)
        post_discord_props(content="", embeds=[props_embed])
    except Exception as e:
        print(f"❌ Discord PROPS send failed: {repr(e)}")

    # Local print for Actions logs
    print(json.dumps({"meta": meta, "team_picks": team_picks, "prop_picks": prop_picks}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
