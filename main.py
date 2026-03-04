# main.py
from __future__ import annotations

import json
import os
import traceback
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
            return

    rec(obj)
    return out


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
        p_real = p.get("fair_prob")  # fair_prob = p_real final

        ev = p.get("ev")
        edge = p.get("edge")
        dev = p.get("dev")
        score = p.get("score")

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
            f"p_model: {pct(p_model)} | p_mkt: {pct(p_mkt)} | p_real: {pct(p_real)}\n"
            f"EV: {pct(ev)} | Edge: {pct(edge)} | Dev: {pct(dev)} | Score: {num(score)}/100\n"
        )

    return {"title": title, "description": "\n".join(lines), "color": color}


def _preview(raw_games: Any, n_chars: int = 900) -> str:
    try:
        s = json.dumps(raw_games, ensure_ascii=False)
    except Exception:
        s = repr(raw_games)
    return s[:n_chars]


def main() -> None:
    # Always ping LOG at start (proves the workflow reached Python)
    post_discord_log(content="NBA BOT: main.py started ✅")

    cfg = load_config("config.json")

    # 1) Check Odds API key
    api_key = _env("ODDS_API_KEY")
    if not api_key:
        msg = "❌ ODDS_API_KEY manquante dans les Secrets GitHub."
        post_discord_log(content=msg)
        # Also notify other channels so you see it even if LOG channel muted
        post_discord_team(content=msg)
        post_discord_props(content=msg)
        print(msg)
        return

    # 2) Fetch games
    try:
        raw_games = fetch_odds_with_fallback(
            markets=cfg.markets,
            regions_priority=cfg.regions_priority,
        )
    except Exception as e:
        msg = f"❌ Erreur fetch OddsAPI: {repr(e)}"
        post_discord_log(content=msg + "\n" + traceback.format_exc()[:1500])
        post_discord_team(content=msg)
        post_discord_props(content=msg)
        print(msg)
        return

    games = _flatten_games(raw_games)

    if not games:
        msg = (
            "❌ Aucun match reçu depuis OddsAPI (games vide après flatten).\n"
            "Vérifie: ODDS_API_KEY / régions / quota / marchés.\n\n"
            f"type(raw)={type(raw_games).__name__}\n"
            f"preview(raw)={_preview(raw_games)}"
        )
        post_discord_log(content=msg)
        post_discord_team(content="NBA — Aucun match reçu (voir channel LOG).")
        post_discord_props(content="NBA — Aucun match reçu (voir channel LOG).")
        print(msg)
        return

    # 3) Run engine
    try:
        result = run_engine(games, cfg)
    except Exception as e:
        msg = f"❌ Erreur run_engine: {repr(e)}"
        post_discord_log(content=msg + "\n" + traceback.format_exc()[:1500])
        post_discord_team(content=msg)
        post_discord_props(content=msg)
        print(msg)
        return

    team_picks = result.get("team_picks", []) or []
    prop_picks = result.get("prop_picks", []) or []
    meta = result.get("meta", {}) or {}

    # 4) Always send META (LOG)
    meta_embed = {
        "title": "NBA BOT — META",
        "description": (
            f"Games(flat): {meta.get('games')} | "
            f"markets_tested: {meta.get('markets_tested')} | "
            f"regions_used: {meta.get('regions_used', meta.get('regions_priority'))} | "
            f"model_weight: {meta.get('model_weight')} | "
            f"clip: {meta.get('clip_vs_market')} | "
            f"maxML/day: {meta.get('max_ml_per_day')} | "
            f"maxMLodds: {meta.get('max_odds_ml')}"
        ),
        "color": 3447003,
    }
    post_discord_log(content="", embeds=[meta_embed])

    # 5) Always send TEAM channel message (even if empty)
    team_embed = _discord_embed_top("NBA — TOP 3 TEAM", team_picks, color=3066993)
    post_discord_team(content="", embeds=[team_embed])

    # 6) Always send PROPS channel message (even if empty)
    props_embed = _discord_embed_top("NBA — TOP 3 PROPS", prop_picks, color=10181046)
    post_discord_props(content="", embeds=[props_embed])

    # 7) End ping (proves the run finished)
    post_discord_log(content="NBA BOT: main.py finished ✅")

    # Local print for Actions logs
    print(json.dumps({"meta": meta, "team_picks": team_picks, "prop_picks": prop_picks}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
