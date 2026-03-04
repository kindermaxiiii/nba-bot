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


def _safe_preview(x: Any, max_chars: int = 1200) -> str:
    try:
        s = json.dumps(x, ensure_ascii=False)
    except Exception:
        s = repr(x)
    return s[:max_chars]


def _normalize_fetch_result(raw: Any) -> Tuple[Any, List[str]]:
    """
    Normalise le retour de fetch_odds_with_fallback car chez toi il peut être:
      - list[dict]                      (OK)
      - dict{"data": ...}               (wrapper)
      - list[list[dict]]                (multi-regions)
      - tuple(data, regions_used)       (CAS IMPORTANT chez toi)
      - tuple(data, meta_dict)          (parfois)
    On renvoie: (data, regions_used[])
    """
    regions_used: List[str] = []

    # Cas tuple (data, regions/meta)
    if isinstance(raw, tuple) and len(raw) >= 2:
        data = raw[0]
        meta = raw[1]

        if isinstance(meta, list) and all(isinstance(r, str) for r in meta):
            regions_used = meta
        elif isinstance(meta, dict):
            ru = meta.get("regions_used") or meta.get("regions") or meta.get("regions_priority")
            if isinstance(ru, list):
                regions_used = [str(r) for r in ru]
        return data, regions_used

    return raw, regions_used


def _flatten_games(data: Any) -> List[Dict[str, Any]]:
    """
    Transforme n'importe quel format en List[Dict match] (dict contenant home_team & away_team).
    """
    out: List[Dict[str, Any]] = []

    def rec(x: Any) -> None:
        if x is None:
            return
        if isinstance(x, dict):
            # wrapper OddsAPI
            if "data" in x:
                rec(x["data"])
                return
            # match dict
            if "home_team" in x and "away_team" in x:
                out.append(x)
                return
            # sinon on explore
            for v in x.values():
                rec(v)
            return
        if isinstance(x, list):
            for it in x:
                rec(it)
            return
        if isinstance(x, tuple):
            for it in x:
                rec(it)
            return

    rec(data)
    return out


def _discord_embed_top(title: str, picks: List[Dict[str, Any]], color: int) -> Dict[str, Any]:
    if not picks:
        return {"title": title, "description": "Aucun pick.", "color": 15158332}

    lines: List[str] = []
    for i, p in enumerate(picks, 1):
        match = p.get("match") or "?"
        market = p.get("market") or "?"
        selection = p.get("selection") or "?"
        book = p.get("book") or "?"
        line = p.get("line", None)

        odds = p.get("odds", None)
        odds_s = "?" if odds is None else f"{float(odds):.2f}"

        def pct(v: Any) -> str:
            try:
                return f"{float(v) * 100:.2f}%"
            except Exception:
                return "?"

        p_model = p.get("p_model")
        p_mkt = p.get("p_mkt")
        p_real = p.get("fair_prob")  # ton champ final
        ev = p.get("ev")
        edge = p.get("edge")
        dev = p.get("dev")
        score = p.get("score")

        line_s = f" | Line: {line}" if line is not None else ""

        lines.append(
            f"**#{i}** — {match}\n"
            f"Market: **{market}**{line_s}\n"
            f"Pick: **{selection}** @ **{odds_s}** ({book})\n"
            f"p_model: {pct(p_model)} | p_mkt: {pct(p_mkt)} | p_real: {pct(p_real)}\n"
            f"EV: {pct(ev)} | Edge: {pct(edge)} | Dev: {pct(dev)} | Score: {('?' if score is None else f'{float(score):.1f}')}/100"
        )

    return {"title": title, "description": "\n\n".join(lines), "color": color}


def main() -> None:
    cfg = load_config("config.json")

    # Vérifs secrets
    if not _env("ODDS_API_KEY"):
        post_discord_log(content="❌ ODDS_API_KEY manquante dans les Secrets GitHub.")
        return

    if not _env("DISCORD_LOG_WEBHOOK"):
        # Sans log webhook tu ne verras jamais les erreurs
        print("❌ DISCORD_LOG_WEBHOOK manquant.")
        return

    # Ping debug (tu dois voir ce message à CHAQUE run)
    post_discord_log(content="DEBUG: deps installed, about to run main.py")

    # Fetch Odds
    try:
        raw = fetch_odds_with_fallback(
            markets=getattr(cfg, "markets", None),
            regions_priority=getattr(cfg, "regions_priority", None),
        )
    except Exception as e:
        post_discord_log(content=f"❌ Erreur fetch OddsAPI: {repr(e)}")
        return

    data, regions_used = _normalize_fetch_result(raw)
    games = _flatten_games(data)

    # Si vide: on log le type + preview pour diagnostiquer vite
    if not games:
        msg = (
            "❌ Aucun match reçu depuis OddsAPI (games vide après flatten).\n"
            f"type(raw)={type(raw).__name__}\n"
            f"preview(raw)={_safe_preview(raw)}\n"
            "À vérifier: markets/regions dans config.json, quota OddsAPI."
        )
        post_discord_log(content=msg)
        return

    # Run engine
    try:
        result = run_engine(games, cfg)
    except Exception as e:
        post_discord_log(content=f"❌ Erreur run_engine: {repr(e)}")
        return

    team_picks = (result.get("team_picks") or [])[:3]
    prop_picks = (result.get("prop_picks") or [])[:3]
    meta = result.get("meta") or {}

    # META (LOG)
    meta_embed = {
        "title": "NBA BOT — META",
        "description": (
            f"Games(flat): {len(games)} | "
            f"regions_used: {regions_used or meta.get('regions_priority') or 'n/a'} | "
            f"markets(cfg): {getattr(cfg, 'markets', None)}\n"
            f"markets_tested: {meta.get('markets_tested', 'n/a')} | "
            f"model_weight: {meta.get('model_weight', 'n/a')} | "
            f"clip: {meta.get('clip_vs_market', 'n/a')} | "
            f"maxML/day: {meta.get('max_ml_per_day', 'n/a')} | "
            f"maxMLodds: {meta.get('max_odds_ml', 'n/a')}"
        ),
        "color": 3447003,
    }
    post_discord_log(content="", embeds=[meta_embed])

    # TEAM
    post_discord_team(
        content="",
        embeds=[_discord_embed_top("NBA — TOP 3 TEAM", team_picks, color=3066993)],
    )

    # PROPS
    post_discord_props(
        content="",
        embeds=[_discord_embed_top("NBA — TOP 3 PROPS", prop_picks, color=10181046)],
    )

    # Print Actions log
    print(json.dumps({"meta": meta, "team_picks": team_picks, "prop_picks": prop_picks}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
