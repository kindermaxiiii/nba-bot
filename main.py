# main.py
from __future__ import annotations

import json
from typing import Any, Dict, List

from context import post_discord_team, post_discord_props, post_discord_log
from odds_api import fetch_odds_with_fallback
from engine import load_config, run_engine


def _embed_top(title: str, picks: List[Dict[str, Any]], color: int) -> Dict[str, Any]:
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
        lines.append(
            f"**#{i}** — {p.get('match','?')}\n"
            f"Market: **{p.get('market','?')}**{line_s}\n"
            f"Pick: **{p.get('selection','?')}** @ **{p.get('odds',0):.2f}** ({p.get('book','?')})\n"
            f"p_model: {pct(p.get('p_model'))} | p_mkt: {pct(p.get('p_mkt'))} | p_real: {pct(p.get('fair_prob'))}\n"
            f"EV: {pct(p.get('ev'))} | Edge: {pct(p.get('edge'))} | Dev: {pct(p.get('dev'))} | Score: {num(p.get('score'))}/100\n"
            f"Why: {p.get('why','')}\n"
        )

    return {"title": title, "description": "\n".join(lines), "color": color}


def main() -> None:
    cfg = load_config("config.json")

    # 1) Fetch TEAM odds with region/book fallback
    team_games, meta_fetch = fetch_odds_with_fallback(
        sport_key=cfg.sport_key,
        markets=cfg.team_markets,
        regions_priority=cfg.regions_priority,
        preferred_books=cfg.preferred_books,
    )

    if not team_games:
        # Discord might fail; still print for logs
        msg = f"❌ OddsAPI: aucun match. regions_tried={meta_fetch.get('regions_tried')} error={meta_fetch.get('error')}"
        print(msg)
        post_discord_log(content=msg, embeds=None, fail_hard=False)
        return

    # 2) Run engine (team + props)
    result = run_engine(team_games, cfg)
    meta = result["meta"]
    team_picks = result["team_picks"]
    prop_picks = result["prop_picks"]

    # 3) Discord outputs (non-blocking)
    meta_embed = {
        "title": "NBA BOT — META",
        "description": (
            f"games={meta.get('games')} | team_candidates={meta.get('markets_tested')} | "
            f"clip={meta.get('clip_vs_market')} | odds_range=[{cfg.odds_min},{cfg.odds_max}] | "
            f"maxML/day={cfg.max_ml_per_day} | region_used={meta_fetch.get('region')} | "
            f"props_games={meta.get('props_games')} | props_note={meta.get('props_note')}"
        ),
        "color": 3447003,
    }

    # Send LOG first, but never fail the job on Discord issues
    post_discord_log(content="", embeds=[meta_embed], fail_hard=False)
    post_discord_team(content="", embeds=[_embed_top("NBA — TOP 3 TEAM (Institutional)", team_picks, 3066993)], fail_hard=False)
    post_discord_props(content="", embeds=[_embed_top("NBA — TOP 3 PROPS (Stat-only)", prop_picks, 10181046)], fail_hard=False)

    # 4) Always print full JSON to Actions logs for debugging
    print(json.dumps({"fetch": meta_fetch, "meta": meta, "team_picks": team_picks, "prop_picks": prop_picks}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
