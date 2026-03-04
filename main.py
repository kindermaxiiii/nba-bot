# main.py
from __future__ import annotations

import json
import os

from odds_api import fetch_odds_with_fallback
from engine import build_team_candidates
from props_engine_v6 import build_prop_candidates_v6
from formatting import format_team_message, format_props_message, meta_embed
from context import post_discord_team, post_discord_props, post_discord_log


def load_cfg() -> dict:
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def select_top(picks, n):
    return picks[: max(0, int(n))]


def main():
    cfg = load_cfg()

    # 1) Fetch slate + odds (auto slate)
    games, fetch_meta = fetch_odds_with_fallback(cfg)
    markets_used = fetch_meta.get("markets_used") or fetch_meta.get("markets_requested") or cfg.get("markets")

    # 2) Build TEAM candidates
    team_candidates, team_meta, spread_map = build_team_candidates(games, cfg)
    team_picks = select_top(team_candidates, cfg.get("max_picks_team", 3))

    # 3) Build PROPS candidates (only if props markets exist in response)
    props_picks = []
    props_note = None
    if isinstance(markets_used, list) and any(m.startswith("player_") for m in markets_used):
        props_candidates, props_meta = build_prop_candidates_v6(games, cfg, team_spread_map=spread_map)
        props_picks = select_top(props_candidates, cfg.get("max_picks_props", 3))
        if not props_picks:
            props_note = props_meta.get("reason")
    else:
        props_note = fetch_meta.get("note") or "Props odds indisponibles (endpoint/plan OddsAPI) — bot continue en TEAM only."

    # 4) Post to Discord (non-fatal)
    team_msg = format_team_message(team_picks)
    props_msg = format_props_message(props_picks, note=props_note)

    # META embed
    meta = {
        "games": len(games),
        "region_used": fetch_meta.get("region_used"),
        "markets_used": markets_used,
        "team_candidates": len(team_candidates),
        "props_candidates": len(props_picks) if props_picks else 0,
        "clip": cfg.get("clip_vs_market"),
        "odds_range": f"[{cfg.get('min_odds')},{cfg.get('max_odds')}]",
        "maxML/day": cfg.get("max_ml_per_day"),
    }
    # Add fetch note/error if any
    if fetch_meta.get("error"):
        meta["odds_error"] = str(fetch_meta.get("error"))[:160]
    if fetch_meta.get("note"):
        meta["odds_note"] = str(fetch_meta.get("note"))[:160]

    post_discord_log(content="", embeds=[meta_embed(meta)])
    post_discord_team(team_msg)
    post_discord_props(props_msg)

    # Also print JSON for Actions logs
    print(json.dumps({
        "meta": meta,
        "team_picks": team_picks,
        "prop_picks": props_picks,
    }, indent=2))


if __name__ == "__main__":
    main()
