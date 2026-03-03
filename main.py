import os
import json
from typing import Any, Dict, List, Tuple

from odds_api import fetch_odds_with_fallback
from engine import (
    collect_market_lines,
    analyze_two_way_market,
    finalize_two_way_pair,
    diversify_team_picks,
)

from formatting import format_no_bet, format_team_pick
from context import post_discord


TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")


def load_json(path: str, default: Any):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def main():
    config = load_json("config.json", {})
    state = load_json("state.json", {})

    # Team features
    tf = load_json("data/team_features.json", {})
    team_features = tf.get("by_team_name", {}) if isinstance(tf, dict) else {}

    # Fetch odds (NBA)
    games = fetch_odds_with_fallback(
        sport_key="basketball_nba",
        markets=["h2h", "spreads", "totals"],
        regions=config.get("regions_team", "us"),
        odds_format="decimal",
    )

    if not games:
        desc = format_no_bet(
            title="❌ NBA NO BET LOG",
            reason="Aucun match reçu depuis OddsAPI (team_games vide). Vérifie ODDS_API_KEY / regions / quota.",
            regions_used=[config.get("regions_team", "us")],
            games_analyzed=0,
            markets_tested=0,
            top_rejects=[],
            near_miss_lines=[],
            daily_budget=0.0,
            daily_spent=0.0,
        )
        if LOG_WEBHOOK:
            post_discord(LOG_WEBHOOK, "NBA NO BET LOG", desc)
        return

    # Collect candidates
    raw_candidates: List[Dict[str, Any]] = []
    for mk in ["h2h", "spreads", "totals"]:
        raw_candidates += collect_market_lines(games, mk)

    # Build dict to pair two-way markets
    # Pair key = (match_id, market, line)
    pairs: Dict[Tuple[str, str, Any], List[Dict[str, Any]]] = {}

    enriched: List[Dict[str, Any]] = []
    for c in raw_candidates:
        base = analyze_two_way_market(c, team_features)
        if base is None:
            continue

        k = (base["match_id"], base["market"], base["line"])
        pairs.setdefault(k, []).append(base)

    # Finalize only if exactly 2 sides (2-way)
    finalized: List[Dict[str, Any]] = []
    for _, lst in pairs.items():
        if len(lst) != 2:
            continue
        a, b = lst[0], lst[1]
        fa, fb = finalize_two_way_pair(a, b, team_features)
        if fa is not None:
            finalized.append(fa)
        if fb is not None:
            finalized.append(fb)

    # Keep only EV>0 (institutionnel)
    finalized = [p for p in finalized if (p.get("ev") is not None and p["ev"] > 0)]

    # “ML seulement si meilleure option du match”
    # -> on garde pour chaque match l’option la mieux scorée, MAIS on garde aussi les non-ML
    best_per_match: Dict[str, Dict[str, Any]] = {}
    for p in finalized:
        mid = p["match_id"]
        if mid not in best_per_match or p["score"] > best_per_match[mid]["score"]:
            best_per_match[mid] = p

    # Autorise ML uniquement si c’est le best du match
    logical_pool: List[Dict[str, Any]] = []
    for p in finalized:
        if p["market"] != "MONEYLINE":
            logical_pool.append(p)
        else:
            if best_per_match.get(p["match_id"]) is p:
                logical_pool.append(p)

    # Pick top 3 team
    team_picks = diversify_team_picks(
        logical_pool,
        max_picks=3,
        one_pick_per_match=True,
        max_ml=int(config.get("max_ml_per_day", 2)),
    )

    if not team_picks:
        desc = format_no_bet(
            title="❌ NBA NO BET LOG",
            reason="Aucune sélection logique EV>0 (après blend cadré + anti-ML longshot + ML seulement si meilleur du match).",
            regions_used=[config.get("regions_team", "us")],
            games_analyzed=len(games),
            markets_tested=len(finalized),
            top_rejects=[],
            near_miss_lines=[],
            daily_budget=0.0,
            daily_spent=0.0,
        )
        if LOG_WEBHOOK:
            post_discord(LOG_WEBHOOK, "NBA NO BET LOG", desc)
        return

    # Post team picks
    for p in team_picks:
        msg = format_team_pick(
            p=p,
            stake=0.0,
            bankroll=float(config.get("bankroll_eur", 0.0)),
            daily_budget=0.0,
            spent_after=0.0,
        )
        if TEAM_WEBHOOK:
            post_discord(TEAM_WEBHOOK, "NBA TEAM BET", msg)

    # Save state (minimal)
    state["last_run_team_picks"] = team_picks
    try:
        with open("state.json", "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


if __name__ == "__main__":
    main()
