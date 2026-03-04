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


def _mask(s: Optional[str]) -> str:
    if not s:
        return "MISSING"
    s = str(s)
    if len(s) <= 8:
        return "SET(too_short)"
    return f"SET({s[:4]}...{s[-4:]})"


def _flatten_games(obj: Any) -> List[Dict[str, Any]]:
    """
    fetch_odds_with_fallback can return:
    - List[Dict] (ideal)
    - List[List[Dict]] (one list per region)
    - Dict with "data" holding list(s)
    Convert everything to List[Dict(match)].
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


def _pct(x: Any) -> str:
    try:
        return f"{float(x) * 100:.2f}%"
    except Exception:
        return "?"


def _num(x: Any) -> str:
    try:
        return f"{float(x):.1f}"
    except Exception:
        return "?"


def _odds_s(x: Any) -> str:
    try:
        return f"{float(x):.2f}"
    except Exception:
        return "?"


def _discord_embed_top(title: str, picks: List[Dict[str, Any]], color: int) -> Dict[str, Any]:
    if not picks:
        return {
            "title": title,
            "description": "Aucun pick (EV>=0 introuvable ou data/props indisponibles).",
            "color": 15158332,
        }

    lines: List[str] = []
    for i, p in enumerate(picks, 1):
        line = p.get("line")
        line_s = f" | Line: {line}" if line is not None else ""

        book = p.get("book") or "?"
        match = p.get("match") or "?"
        market = p.get("market") or "?"
        selection = p.get("selection") or "?"

        p_model = p.get("p_model")
        p_mkt = p.get("p_mkt")
        p_real = p.get("fair_prob")  # ton engine utilise fair_prob comme p_real final

        ev = p.get("ev")
        edge = p.get("edge")
        dev = p.get("dev")
        score = p.get("score")
        odds = p.get("odds")

        lines.append(
            f"**#{i}** — {match}\n"
            f"Market: **{market}**{line_s}\n"
            f"Pick: **{selection}** @ **{_odds_s(odds)}** ({book})\n"
            f"p_model: {_pct(p_model)} | p_mkt: {_pct(p_mkt)} | p_real: {_pct(p_real)}\n"
            f"EV: {_pct(ev)} | Edge: {_pct(edge)} | Dev: {_pct(dev)} | Score: {_num(score)}/100\n"
        )

    return {"title": title, "description": "\n".join(lines), "color": color}


def _safe_post(which: str, fn, content: str = "", embeds: Optional[list] = None) -> None:
    try:
        fn(content=content, embeds=embeds)
        print(f"DEBUG discord: posted {which}")
    except Exception as e:
        print(f"ERROR discord post {which}: {repr(e)}")


def main() -> None:
    # --- BOOT LOGS (Actions) ---
    print("DEBUG main: start")
    print("DEBUG env:",
          "ODDS_API_KEY=", _mask(_env("ODDS_API_KEY")),
          "| TEAM_WEBHOOK=", _mask(_env("DISCORD_TEAM_WEBHOOK")),
          "| PROPS_WEBHOOK=", _mask(_env("DISCORD_PROPS_WEBHOOK")),
          "| LOG_WEBHOOK=", _mask(_env("DISCORD_LOG_WEBHOOK")))

    # Send a boot message to LOG (proves runtime reaches main)
    _safe_post("LOG(boot)", post_discord_log, content="NBA BOT DEBUG: main.py started ✅")

    # --- LOAD CONFIG ---
    try:
        cfg = load_config("config.json")
    except Exception as e:
        tb = traceback.format_exc()
        _safe_post("LOG(config_error)", post_discord_log, content=f"❌ load_config error: {repr(e)}\n```{tb[-1500:]}```")
        raise

    print("DEBUG cfg:", "markets=", getattr(cfg, "markets", None), "| regions_priority=", getattr(cfg, "regions_priority", None))

    # --- CHECK ODDS API KEY ---
    api_key = _env("ODDS_API_KEY")
    if not api_key:
        msg = "❌ ODDS_API_KEY manquante dans les Secrets GitHub."
        _safe_post("LOG(no_key)", post_discord_log, content=msg)
        print(msg)
        return

    # --- FETCH ODDS ---
    try:
        raw_games = fetch_odds_with_fallback(
            markets=cfg.markets,
            regions_priority=cfg.regions_priority,
        )
    except Exception as e:
        tb = traceback.format_exc()
        msg = f"❌ Erreur fetch OddsAPI: {repr(e)}\n```{tb[-1500:]}```"
        _safe_post("LOG(fetch_error)", post_discord_log, content=msg)
        print(msg)
        return

    print("DEBUG raw_games type:", type(raw_games))
    games = _flatten_games(raw_games)
    print("DEBUG games(flat) =", len(games))

    # Quick sample in logs (first game keys)
    if games:
        g0 = games[0]
        print("DEBUG game[0] keys:", list(g0.keys())[:25])
        print("DEBUG game[0] matchup:", g0.get("away_team"), "@", g0.get("home_team"), "| markets in bookmakers?",
              "bookmakers" in g0)

    if not games:
        msg = "❌ Aucun match reçu depuis OddsAPI (games vide après flatten). Vérifie ODDS_API_KEY / régions / quota."
        _safe_post("LOG(no_games)", post_discord_log, content=msg)
        print(msg)
        return

    # --- RUN ENGINE ---
    try:
        result = run_engine(games, cfg)
    except Exception as e:
        tb = traceback.format_exc()
        msg = f"❌ Erreur run_engine: {repr(e)}\n```{tb[-1500:]}```"
        _safe_post("LOG(engine_error)", post_discord_log, content=msg)
        print(msg)
        return

    if not isinstance(result, dict):
        msg = f"❌ run_engine returned non-dict: {type(result)}"
        _safe_post("LOG(engine_bad_return)", post_discord_log, content=msg)
        print(msg)
        return

    team_picks = result.get("team_picks", []) or []
    prop_picks = result.get("prop_picks", []) or []
    meta = result.get("meta", {}) or {}

    print("DEBUG result meta:", meta)
    print("DEBUG picks:", "team=", len(team_picks), "| props=", len(prop_picks))

    # --- SEND DISCORD OUTPUTS (ALWAYS) ---
    meta_embed = {
        "title": "NBA BOT — META",
        "description": (
            f"Games(flat): {meta.get('games', len(games))} | "
            f"markets_tested: {meta.get('markets_tested', '?')} | "
            f"model_weight: {meta.get('model_weight', '?')} | "
            f"clip: {meta.get('clip_vs_market', '?')} | "
            f"maxML/day: {meta.get('max_ml_per_day', '?')} | "
            f"maxMLodds: {meta.get('max_odds_ml', '?')}"
        ),
        "color": 3447003,
    }

    _safe_post("LOG(meta)", post_discord_log, content="", embeds=[meta_embed])

    team_embed = _discord_embed_top("NBA — TOP 3 TEAM", team_picks, color=3066993)
    _safe_post("TEAM(picks)", post_discord_team, content="", embeds=[team_embed])

    props_embed = _discord_embed_top("NBA — TOP 3 PROPS", prop_picks, color=10181046)
    _safe_post("PROPS(picks)", post_discord_props, content="", embeds=[props_embed])

    # Local print for Actions logs
    print(json.dumps({"meta": meta, "team_picks": team_picks, "prop_picks": prop_picks}, indent=2, ensure_ascii=False))
    _safe_post("LOG(done)", post_discord_log, content="NBA BOT DEBUG: run completed ✅")


if __name__ == "__main__":
    main()
