# main.py
from __future__ import annotations

import json
from typing import Any, Dict, List
from datetime import datetime, timezone

from context import post_discord_log, post_discord_team, post_discord_props
from odds_api import fetch_odds_with_fallback, fetch_odds
from engine import build_team_candidates, build_portfolio_team, dump_artifacts
from props_engine_v6 import build_prop_candidates
from formatting import meta_embed, picks_embed
from utils import now_iso


def load_cfg() -> Dict[str, Any]:
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def _props_fetch(cfg: Dict[str, Any]) -> tuple[list[dict], dict]:
    # Try each props market individually to avoid 422 killing all.
    sport_key = cfg.get("sport_key", "basketball_nba")
    regions = cfg.get("regions_priority") or ["us"]
    prop_markets = cfg.get("prop_markets") or [
        "player_points", "player_rebounds", "player_assists", "player_points_rebounds_assists"
    ]
    # Only keep supported keys known by our parser
    prop_markets = [m for m in prop_markets if m]

    all_games: List[Dict[str, Any]] = []
    supported: List[str] = []
    unsupported: Dict[str, str] = {}

    # Use first region that works for team; for props, try regions in order but stop at first 200 per market
    for pm in prop_markets:
        got = False
        last_err = None
        for region in regions:
            games, meta = fetch_odds(sport_key, region, [pm])
            if games:
                all_games.extend(games)
                supported.append(pm)
                got = True
                break
            if meta.get("status") == 422:
                last_err = meta.get("error") or "422 invalid_market"
                # no need to try other regions
                break
            last_err = meta.get("error") or f"HTTP {meta.get('status')}"
        if not got:
            unsupported[pm] = last_err or "no data"

    return all_games, {"supported": sorted(set(supported)), "unsupported": unsupported}


def main() -> None:
    cfg = load_cfg()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # TEAM fetch with fallback markets on 422
    team_markets = cfg.get("team_markets") or ["h2h", "spreads", "totals"]
    games, fetch_meta = fetch_odds_with_fallback(
        sport_key=cfg.get("sport_key", "basketball_nba"),
        markets=team_markets,
        regions_priority=cfg.get("regions_priority", ["us", "us2", "eu", "uk"]),
    )

    if not games:
        msg = f"❌ OddsAPI TEAM: aucun match. regions_tried={fetch_meta.get('regions_tried')} error={fetch_meta.get('error')}"
        post_discord_log(content=msg)
        print(msg)
        return

    team_candidates, team_meta, spread_map = build_team_candidates(games, cfg)
    team_picks = build_portfolio_team(team_candidates, cfg)

    # PROPS fetch (only if supported)
    props_games, props_meta = _props_fetch(cfg)
    prop_candidates: List[Dict[str, Any]] = []
    prop_picks: List[Dict[str, Any]] = []

    props_note = None
    if props_meta["supported"]:
        prop_candidates, pc_meta = build_prop_candidates(props_games, cfg, team_spread_map=spread_map)
        # diversify: 1 pick per player if possible
        used_players = set()
        for c in prop_candidates:
            if len(prop_picks) >= int(cfg.get("max_picks_props", 3)):
                break
            pl = c.get("player")
            if pl and pl in used_players:
                continue
            prop_picks.append(c)
            if pl:
                used_players.add(pl)

        if len(prop_picks) < int(cfg.get("max_picks_props", 3)):
            # fill allow duplicates
            for c in prop_candidates:
                if len(prop_picks) >= int(cfg.get("max_picks_props", 3)):
                    break
                if c in prop_picks:
                    continue
                prop_picks.append(c)

        if not prop_picks:
            props_note = pc_meta.get("reason") if isinstance(pc_meta, dict) else "No prop candidates passed discipline."
    else:
        # NO BET PROPS detailed
        props_note = (
            "NO BET PROPS: OddsAPI ne fournit pas les marchés props sur ton plan/endpoint.\n"
            f"Unsupported: {props_meta['unsupported']}"
        )

    # META
    meta = {
        "run_id": run_id,
        "ts_utc": now_iso(),
        "region_used": fetch_meta.get("region_used"),
        "markets_used": fetch_meta.get("markets_used"),
        "games": len(games),
        "team_candidates": team_meta.get("team_candidates"),
        "team_picks": len(team_picks),
        "props_supported": props_meta["supported"],
        "props_picks": len(prop_picks),
        "odds_range": f"[{cfg.get('min_odds')},{cfg.get('max_odds')}]",
        "clip": cfg.get("clip_vs_market"),
        "haircut": f"trigger={cfg.get('haircut_trigger')} rate={cfg.get('haircut_rate')}",
    }

    # dump artifacts for audit
    dump_artifacts(run_id, meta, team_candidates, team_picks, prop_candidates, prop_picks)

    # Discord outputs
    post_discord_log(content="", embeds=[meta_embed(meta)])
    post_discord_team(content="", embeds=[picks_embed("NBA — TOP 3 TEAM (Model-First)", team_picks, 3066993)])
    post_discord_props(content=props_note or "", embeds=[picks_embed("NBA — TOP 3 PROPS (V6)", prop_picks, 10181046)])

    # Print to logs
    print(json.dumps({"meta": meta, "team_picks": team_picks, "prop_picks": prop_picks}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
