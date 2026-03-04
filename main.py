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


def _safe_preview(obj: Any, limit: int = 900) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    return (s[:limit] + "…") if len(s) > limit else s


def _maybe_json_load(x: Any) -> Any:
    if isinstance(x, str):
        s = x.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.loads(s)
            except Exception:
                return x
    return x


def _flatten_games(obj: Any) -> List[Dict[str, Any]]:
    """
    Supporte:
    - List[Dict] (idéal)
    - Tuple[List[...], ...] (ex: (games, meta) ou (region1_games, region2_games))
    - List[List[Dict]] (une liste par région)
    - Dict wrapper avec "data"
    """
    out: List[Dict[str, Any]] = []

    def rec(x: Any) -> None:
        if x is None:
            return

        # IMPORTANT: gérer tuple comme list
        if isinstance(x, (list, tuple)):
            for it in x:
                rec(it)
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

        # ignore autres types

    rec(obj)
    return out


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

    api_key = _env("ODDS_API_KEY")
    if not api_key:
        msg = "❌ ODDS_API_KEY manquante dans les Secrets GitHub."
        post_discord_log(content=msg)
        print(msg)
        return

    # Fetch odds
    try:
        raw = fetch_odds_with_fallback(
            markets=cfg.markets,
            regions_priority=cfg.regions_priority,
        )
    except Exception as e:
        msg = f"❌ Erreur fetch OddsAPI: {repr(e)}"
        post_discord_log(content=msg)
        print(msg)
        return

    raw = _maybe_json_load(raw)

    # Si OddsAPI renvoie un objet erreur
    if isinstance(raw, dict) and ("error_code" in raw or "message" in raw):
        msg = (
            "❌ OddsAPI a renvoyé une ERREUR.\n"
            f"error_code: {raw.get('error_code')}\n"
            f"message: {raw.get('message')}\n"
            f"details: {raw.get('details_url')}\n"
            f"preview: {_safe_preview(raw)}"
        )
        post_discord_log(content=msg)
        print(msg)
        return

    games = _flatten_games(raw)

    if not games:
        msg = (
            "❌ Aucun match reçu depuis OddsAPI (games vide après flatten).\n"
            f"type(raw)={type(raw).__name__}\n"
            f"preview(raw)={_safe_preview(raw)}\n"
            "À vérifier: regions dans config.json, marchés autorisés par ton plan OddsAPI, quota."
        )
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

    # META -> LOG
    meta_embed = {
        "title": "NBA BOT — META",
        "description": (
            f"Games(flat): {len(games)} | "
            f"markets_tested: {meta.get('markets_tested')} | "
            f"regions_priority: {getattr(cfg, 'regions_priority', None)} | "
            f"markets(cfg): {getattr(cfg, 'markets', None)}"
        ),
        "color": 3447003,
    }
    post_discord_log(content="", embeds=[meta_embed])

    # TEAM / PROPS
    post_discord_team(content="", embeds=[_discord_embed_top("NBA — TOP 3 TEAM", team_picks, 3066993)])
    post_discord_props(content="", embeds=[_discord_embed_top("NBA — TOP 3 PROPS", prop_picks, 10181046)])

    print(json.dumps({"meta": meta, "team_picks": team_picks, "prop_picks": prop_picks}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
