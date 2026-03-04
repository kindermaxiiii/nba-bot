# main.py (V8) — drop-in replacement for your current main.py
# Fixes:
# - passes sport_key to OddsAPI fallback call
# - uses V8 team model + V8 portfolio sizing
# - keeps NO BET PROPS behavior if 422 INVALID_MARKET

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from context import post_discord_log, post_discord_props, post_discord_team
from engine import build_team_candidates, build_team_portfolio, dump_artifacts
from formatting import embed_meta, embed_no_picks, embed_picks
from injury_model import build_injury_adjustments
from odds_api import fetch_odds, fetch_odds_with_fallback
from slate_volatility import classify_slate
from utils import load_json, norm_team


def utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ensure_games(x: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if isinstance(x, tuple) and len(x) == 2 and isinstance(x[0], list) and isinstance(x[1], dict):
        return x[0], x[1]
    if isinstance(x, list):
        return x, {}
    if isinstance(x, dict) and "data" in x and isinstance(x["data"], list):
        return x["data"], {k: v for k, v in x.items() if k != "data"}
    return [], {}


def filter_future_games(games: List[Dict[str, Any]], hours: int = 36) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    out: List[Dict[str, Any]] = []
    for g in games:
        ct = g.get("commence_time")
        if not ct:
            out.append(g)
            continue
        try:
            dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
        except Exception:
            out.append(g)
            continue
        if dt >= now and (dt - now).total_seconds() <= hours * 3600:
            out.append(g)
    return out


def props_probe(cfg: Dict[str, Any], sport_key: str) -> Tuple[bool, Dict[str, str], List[Dict[str, Any]]]:
    regions = cfg.get("regions_priority", ["us"]) or ["us"]
    prop_markets: List[str] = cfg.get("prop_markets", []) or []
    if not prop_markets:
        return False, {"(none)": "No prop_markets configured"}, []

    probe = prop_markets[0]
    try:
        games = fetch_odds(markets=[probe], sport_key=sport_key, regions=regions[0])
        return True, {}, games
    except Exception as e:
        msg = repr(e)
        if "HTTP 422" in msg or "INVALID_MARKET" in msg or "Markets not supported" in msg:
            return False, {m: msg for m in prop_markets}, []
        return False, {probe: msg}, []


def main() -> None:
    run_id = utc_run_id()

    try:
        cfg = load_json("config.json") or {}
        sport_key = str(cfg.get("sport_key", "basketball_nba"))

        odds_range = [float(cfg.get("min_odds", 1.5)), float(cfg.get("max_odds", 2.2))]
        team_cfg = {
            "odds_range": odds_range,
            "clip": float(cfg.get("clip_vs_market", 0.08)),
            "haircut_trigger": float(cfg.get("haircut_trigger", 0.06)),
            "haircut_rate": float(cfg.get("haircut_rate", 0.30)),
            "min_edge": float(cfg.get("min_edge", 0.02)),
            "edge_refuse": float(cfg.get("edge_refuse", 0.15)),
            "one_pick_per_match": bool(cfg.get("one_pick_per_match", True)),
            "max_ml_per_day": int(cfg.get("max_ml_per_day", 2)),
            "bankroll": float(cfg.get("bankroll", 1.0)),
            "bet_min_pct": float(cfg.get("bet_min_pct", 0.0025)),
            "bet_max_pct": float(cfg.get("bet_max_pct", 0.02)),
            "total_cap_pct": float(cfg.get("total_cap_pct", 0.10)),
        }

        features = load_json(os.path.join("data", "team_features.json")) or {}

        # Injury adjustments
        inj_adjust = build_injury_adjustments()

        # Fetch TEAM slate (sport_key required)
        team_markets = cfg.get("team_markets", ["spreads", "h2h", "totals"]) or ["spreads", "h2h", "totals"]
        raw = fetch_odds_with_fallback(
            sport_key=sport_key,
            markets=team_markets,
            regions_priority=cfg.get("regions_priority", ["us"]),
        )
        games, odds_meta = ensure_games(raw)
        games = filter_future_games(games, hours=int(cfg.get("slate_hours", 36)))

        if not games:
            raise RuntimeError("No games received from OddsAPI")

        # Feature coverage
        teams_in_slate = set()
        for g in games:
            if g.get("home_team"):
                teams_in_slate.add(norm_team(str(g.get("home_team"))))
            if g.get("away_team"):
                teams_in_slate.add(norm_team(str(g.get("away_team"))))

        feat_norm = {norm_team(k): v for k, v in (features or {}).items()}
        covered = sum(1 for t in teams_in_slate if t in feat_norm)
        coverage_pct = round(100.0 * covered / max(1, len(teams_in_slate)), 1)

        # TEAM candidates + portfolio
        team_candidates, team_meta, spread_map = build_team_candidates(games, team_cfg, features, inj_adjust=inj_adjust)

        # Slate volatility (Couche -2)
        injury_scores = []
        for t in teams_in_slate:
            injury_scores.append(float(inj_adjust.get(t, {}).get("vol", 0.0)))
        abs_spreads = list(spread_map.values())
        sv = classify_slate(injury_scores, abs_spreads)

        team_picks = build_team_portfolio(
            team_candidates,
            team_cfg,
            slate_kelly_mult=float(sv.kelly_mult),
            top_n=int(cfg.get("max_picks_team", 3)),
        )

        # PROPS probe (will be False if 422 INVALID_MARKET)
        props_supported, props_unsupported, props_games = props_probe(cfg, sport_key=sport_key)
        props_note = ""
        prop_picks: List[Dict[str, Any]] = []

        if not props_supported:
            props_note = "NO BET PROPS: OddsAPI ne fournit pas les marchés props sur ton plan/endpoint."
        else:
            try:
                from props_engine_v6 import build_prop_picks
                prop_picks = build_prop_picks(props_games, cfg, spread_map)[: int(cfg.get("max_picks_props", 3))]
            except Exception as e:
                props_note = f"NO BET PROPS: erreur props engine: {repr(e)}"

        meta = {
            "run_id": run_id,
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "sport_key": sport_key,
            "region_used": odds_meta.get("region_used"),
            "markets_used": odds_meta.get("markets_used", team_markets),
            "games": len(games),
            "team_candidates": len(team_candidates),
            "team_picks": len(team_picks),
            "props_supported": bool(props_supported),
            "props_picks": len(prop_picks),
            "odds_range": odds_range,
            "clip": team_cfg.get("clip"),
            "clip_hits": team_meta.get("clip_hits"),
            "clip_hit_rate": team_meta.get("clip_hit_rate"),
            "haircut": {"trigger": cfg.get("haircut_trigger", 0.06), "rate": cfg.get("haircut_rate", 0.30)},
            "feature_coverage_pct": coverage_pct,
            "slate": {
                "class": sv.slate_class,
                "injury_vol": sv.injury_vol,
                "blowout_index": sv.blowout_index,
                "kelly_mult": sv.kelly_mult,
                "props_mult": sv.props_mult,
            },
            "props_unsupported": props_unsupported,
            "props_note": props_note,
        }

        art_dir = dump_artifacts(
            run_id,
            {
                "meta": meta,
                "team_candidates": team_candidates,
                "team_picks": team_picks,
                "spread_map": spread_map,
                "injury_adjust": inj_adjust,
                "props_status": {"supported": props_supported, "unsupported": props_unsupported, "note": props_note},
            },
        )

        # Discord (non-blocking)
        post_discord_log(content="", embeds=[embed_meta(meta)])

        if team_picks:
            post_discord_team(content="", embeds=[embed_picks("NBA — TOP 3 TEAM (V8 Model-First + Kelly)", team_picks)])
        else:
            post_discord_team(content="", embeds=[embed_no_picks("NBA — TOP 3 TEAM (V8)", "Aucun pick (discipline/EV/edge/odds).")])

        if props_supported and prop_picks:
            post_discord_props(content="", embeds=[embed_picks("NBA — TOP 3 PROPS (V6)", prop_picks, color=10181046)])
        else:
            lines = []
            if props_note:
                lines.append(props_note)
            if props_unsupported:
                lines.append("Unsupported markets: " + ", ".join(list(props_unsupported.keys())[:10]))
            post_discord_props(
                content="\n".join(lines) if lines else "NO BET PROPS.",
                embeds=[embed_no_picks("NBA — TOP 3 PROPS (V6)", "Aucun pick.")],
            )

        print("Artifacts directory:", art_dir)
        print(json.dumps({"meta": meta, "team_picks": team_picks, "prop_picks": prop_picks}, ensure_ascii=False, indent=2))

    except Exception as e:
        err = f"❌ main.py fatal error: {repr(e)}"
        try:
            post_discord_log(content=err, embeds=[])
        except Exception:
            pass
        try:
            dump_artifacts(run_id, {"fatal": {"error": repr(e)}})
        except Exception:
            pass
        print(err)


if __name__ == "__main__":
    main()
