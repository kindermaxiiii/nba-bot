import os
import json
import requests
import inspect
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Tuple, Optional
from dateutil import parser

from odds_api import fetch_odds_with_fallback, OddsApiError
from engine import (
    collect_market_lines,
    collect_player_prop_lines,
    pick_consensus_line,
    pick_consensus_prop_line,
    analyze_two_way_market,
    diversify_team_picks,
    diversify_prop_picks,
    allocate_stakes_equal,
)
from formatting import format_team_pick, format_prop_pick, format_no_bet
from context import fetch_injuries, build_injury_note, search_player_id, fetch_player_season_minutes

# TON MODELE TEAM
from model_team import model_prob_for_team_market


TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")


DATA_TEAM_FEATURES_PATH = os.path.join("data", "team_features.json")


def post_discord(webhook: str, title: str, description: str):
    if not webhook:
        return
    data = {"embeds": [{"title": title, "description": description}]}
    r = requests.post(webhook, json=data, timeout=20)
    r.raise_for_status()


def load_json_file(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_file(path: str, obj: Any):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def reset_state_if_new_day(state: Dict[str, Any]):
    today_utc = datetime.now(timezone.utc).date().isoformat()
    if state.get("date_utc") != today_utc:
        state.clear()
        state.update({
            "date_utc": today_utc,
            "daily_spent_eur": 0.0,
            "team_bets_sent": 0,
            "prop_bets_sent": 0,
        })


def is_game_soon(commence_time: str, horizon_hours: int = 36) -> bool:
    try:
        start_dt = parser.isoparse(commence_time)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return now - timedelta(hours=1) <= start_dt <= now + timedelta(hours=horizon_hours)
    except Exception:
        return False


def safe_call_format_no_bet(**kwargs) -> str:
    """
    Compat si ton formatting.py a une signature différente.
    """
    try:
        sig = inspect.signature(format_no_bet)
        accepted = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return format_no_bet(**accepted)
    except Exception:
        # fallback minimal
        return f"NO BET\nReason: {kwargs.get('reason','n/a')}"


def safe_call_format_team_pick(p: Dict[str, Any]) -> str:
    try:
        sig = inspect.signature(format_team_pick)
        accepted = {k: v for k, v in {"p": p}.items() if k in sig.parameters}
        return format_team_pick(**accepted)
    except Exception:
        return json.dumps(p, indent=2, ensure_ascii=False)


def safe_call_format_prop_pick(p: Dict[str, Any]) -> str:
    try:
        sig = inspect.signature(format_prop_pick)
        accepted = {k: v for k, v in {"p": p}.items() if k in sig.parameters}
        return format_prop_pick(**accepted)
    except Exception:
        return json.dumps(p, indent=2, ensure_ascii=False)


def build_team_features_map() -> Dict[str, Dict[str, Any]]:
    features = load_json_file(DATA_TEAM_FEATURES_PATH, default={})
    if isinstance(features, dict):
        return features
    return {}


def analyze_team_game(
    g: Dict[str, Any],
    injuries: List[Dict[str, Any]],
    team_features: Dict[str, Dict[str, Any]],
    prefer_fr_books: bool,
    min_books: int,
    model_blend: float,
    max_ml_per_day: int,
) -> List[Dict[str, Any]]:
    home = g["home_team"]
    away = g["away_team"]
    match = f"{away} @ {home}"
    bookmakers = g.get("bookmakers", []) or []
    if not bookmakers:
        return []

    out_candidates: List[Dict[str, Any]] = []

    def run_two_way(market_label: str, line_val, a_key, b_key, a_entries, b_entries):
        # model probs (TEAM only)
        p_model_a = model_prob_for_team_market(
            market=market_label,
            selection=a_key,
            line=line_val,
            away_team=away,
            home_team=home,
            features=team_features,
        )
        p_model_b = model_prob_for_team_market(
            market=market_label,
            selection=b_key,
            line=line_val,
            away_team=away,
            home_team=home,
            features=team_features,
        )

        res = analyze_two_way_market(
            match=match,
            market_label=market_label,
            line=line_val,
            outcome_a=a_key,
            outcome_b=b_key,
            entries_a=a_entries,
            entries_b=b_entries,
            min_books=min_books,
            prefer_fr=prefer_fr_books,
            return_all=True,
            p_model_a=p_model_a,
            p_model_b=p_model_b,
            model_blend=model_blend,
        )

        items = res.get("all", []) or []
        for it in items:
            it["injury_note"] = build_injury_note(match, injuries)
        out_candidates.extend(items)

    # MONEYLINE
    h2h = collect_market_lines(bookmakers, "h2h")["lines"]
    lk = pick_consensus_line(h2h)
    if lk and lk in h2h:
        outs = list(h2h[lk].keys())
        if len(outs) >= 2:
            run_two_way("MONEYLINE", None, outs[0], outs[1], h2h[lk][outs[0]], h2h[lk][outs[1]])

    # TOTAL
    totals = collect_market_lines(bookmakers, "totals")["lines"]
    tlk = pick_consensus_line(totals)
    if tlk and tlk in totals:
        sides = totals[tlk]
        if "Over" in sides and "Under" in sides:
            run_two_way("TOTAL", float(tlk), "Over", "Under", sides["Over"], sides["Under"])

    # SPREAD
    spreads = collect_market_lines(bookmakers, "spreads")["lines"]
    slk = pick_consensus_line(spreads)
    if slk and slk in spreads:
        teams = spreads[slk]
        if home in teams and away in teams:
            run_two_way("SPREAD", float(slk), home, away, teams[home], teams[away])

    # TEAM TOTALS (si dispo)
    team_totals = collect_market_lines(bookmakers, "team_totals")["lines"]
    ttk = pick_consensus_line(team_totals)
    if ttk and ttk in team_totals:
        try:
            team_name, pt = ttk.split("|", 1)
            ptf = float(pt)
        except Exception:
            team_name, ptf = None, None

        if team_name and ptf is not None:
            sides = team_totals[ttk]
            if "Over" in sides and "Under" in sides:
                run_two_way(f"TEAM TOTAL ({team_name})", ptf, "Over", "Under", sides["Over"], sides["Under"])

    return out_candidates


def analyze_props(
    injuries: List[Dict[str, Any]],
    prefer_fr_books: bool,
    min_books: int,
) -> List[Dict[str, Any]]:
    # Props = marché no-vig uniquement (pour l’instant)
    prop_market_map = {
        "PROP PTS": "player_points",
        "PROP REB": "player_rebounds",
        "PROP AST": "player_assists",
        "PROP 3PT": "player_threes",
        "PROP PRA": "player_points_rebounds_assists",
        "PROP PR": "player_points_rebounds",
        "PROP PA": "player_points_assists",
        "PROP RA": "player_rebounds_assists",
    }

    allc: List[Dict[str, Any]] = []

    for label, market_key in prop_market_map.items():
        try:
            games, _ = fetch_odds_with_fallback(
                markets=market_key,
                regions_priority=["us", "us2", "uk", "eu", "au", "fr"],
            )
        except OddsApiError:
            continue

        for g in games:
            if not is_game_soon(g.get("commence_time", "")):
                continue

            match = f"{g['away_team']} @ {g['home_team']}"
            bookmakers = g.get("bookmakers", []) or []
            if not bookmakers:
                continue

            props_struct = collect_player_prop_lines(bookmakers, market_key)["props"]

            for player, player_lines in props_struct.items():
                lk = pick_consensus_prop_line(player_lines)
                if not lk or lk not in player_lines:
                    continue

                sides = player_lines[lk]
                if "Over" not in sides or "Under" not in sides:
                    continue

                res = analyze_two_way_market(
                    match=match,
                    market_label=label,
                    line=float(lk),
                    outcome_a="Over",
                    outcome_b="Under",
                    entries_a=sides["Over"],
                    entries_b=sides["Under"],
                    min_books=min_books,
                    prefer_fr=prefer_fr_books,
                    return_all=True,
                    # pas de modèle props ici => p_real = p_mkt
                    p_model_a=None,
                    p_model_b=None,
                    model_blend=0.0,
                )

                items = res.get("all", []) or []
                for it in items:
                    it["player"] = player
                    it["injury_note"] = build_injury_note(match, injuries)

                    pid = search_player_id(player)
                    mpg = fetch_player_season_minutes(pid) if pid else None
                    if mpg is not None:
                        it["minutes_note"] = f"{mpg:.1f} min (saison)"

                allc.extend(items)

    return allc


def main():
    config = load_json_file("config.json", default={})
    state = load_json_file("state.json", default={})

    reset_state_if_new_day(state)

    bankroll = float(config.get("bankroll_eur", 100.0))
    daily_budget = bankroll * float(config.get("daily_budget_pct", 0.10))

    prefer_fr_books = bool(config.get("prefer_fr_books", True))
    min_books = int(config.get("min_bookmakers", 2))

    max_team = 3
    max_props = 3
    max_ml = 2  # ta règle
    model_blend = float(config.get("model_blend", 0.65))  # modèle team vs marché

    # Injuries best-effort
    try:
        injuries = fetch_injuries()
    except Exception:
        injuries = []

    # Team features
    team_features = build_team_features_map()

    # Fetch TEAM odds
    try:
        team_games, meta = fetch_odds_with_fallback(
            markets="h2h,spreads,totals",
            regions_priority=["us", "us2", "uk", "eu", "au", "fr"],
        )
        chosen_region = meta.get("chosen_region", "n/a")
    except OddsApiError as e:
        desc = safe_call_format_no_bet(
            title="❌ NBA NO BET LOG",
            reason=f"OddsAPI error: {e}",
            regions_used=["n/a"],
            games_analyzed=0,
            markets_tested=0,
            top_rejects=[],
            near_miss_lines=[],
            daily_budget=daily_budget,
            daily_spent=float(state.get("daily_spent_eur", 0.0)),
        )
        post_discord(LOG_WEBHOOK, "NBA NO BET LOG", desc)
        save_json_file("state.json", state)
        return

    # Analyze team candidates
    team_candidates: List[Dict[str, Any]] = []
    games_analyzed = 0
    for g in team_games:
        if not is_game_soon(g.get("commence_time", "")):
            continue
        games_analyzed += 1
        team_candidates.extend(
            analyze_team_game(
                g=g,
                injuries=injuries,
                team_features=team_features,
                prefer_fr_books=prefer_fr_books,
                min_books=min_books,
                model_blend=model_blend,
                max_ml_per_day=max_ml,
            )
        )

    # Pick TOP TEAM (priorité EV>=0 ; si pas assez, on complète avec les "moins mauvais" EV)
    team_pos = [x for x in team_candidates if x.get("ev", -999) >= 0.0]
    team_neg = [x for x in team_candidates if x.get("ev", -999) < 0.0]
    team_pos.sort(key=lambda x: (x.get("score", 0), x.get("ev", 0), x.get("dev", 0)), reverse=True)
    team_neg.sort(key=lambda x: (x.get("score", 0), x.get("ev", 0), x.get("dev", 0)), reverse=True)
    team_pool = team_pos + team_neg
    team_picks = diversify_team_picks(team_pool, max_team, max_ml=max_ml, one_pick_per_match=True)

    # Analyze props
    prop_candidates = analyze_props(injuries, prefer_fr_books, min_books)

    prop_pos = [x for x in prop_candidates if x.get("ev", -999) >= 0.0]
    prop_neg = [x for x in prop_candidates if x.get("ev", -999) < 0.0]
    prop_pos.sort(key=lambda x: (x.get("score", 0), x.get("ev", 0), x.get("dev", 0)), reverse=True)
    prop_neg.sort(key=lambda x: (x.get("score", 0), x.get("ev", 0), x.get("dev", 0)), reverse=True)
    prop_pool = prop_pos + prop_neg
    prop_picks = diversify_prop_picks(prop_pool, max_props, one_pick_per_match=False, one_pick_per_player=True)

    # NO BET if truly no candidates
    if not team_picks and not prop_picks:
        desc = safe_call_format_no_bet(
            title="❌ NBA NO BET LOG",
            reason="Aucun candidat exploitable (games/markets vides ou min_books trop strict).",
            regions_used=[chosen_region],
            games_analyzed=games_analyzed,
            markets_tested=len(team_candidates),
            top_rejects=[],
            near_miss_lines=[],
            daily_budget=daily_budget,
            daily_spent=float(state.get("daily_spent_eur", 0.0)),
        )
        post_discord(LOG_WEBHOOK, "NBA NO BET LOG", desc)
        save_json_file("state.json", state)
        return

    # Budget allocation simple (égal)
    # Si tu veux enlever stakes affichés, ton formatting.py décidera.
    remaining = max(0.0, daily_budget - float(state.get("daily_spent_eur", 0.0)))
    total_picks = len(team_picks) + len(prop_picks)
    stakes = allocate_stakes_equal(remaining, total_picks) if total_picks > 0 else []
    i = 0

    # Send TEAM
    for p in team_picks:
        p["tier"] = "MODEL_BLEND" if model_blend > 0 else "MARKET_ONLY"
        p["injury_note"] = p.get("injury_note") or ""
        p["stake_eur"] = stakes[i] if i < len(stakes) else 0.0
        i += 1
        msg = safe_call_format_team_pick(p)
        post_discord(TEAM_WEBHOOK, "🏀 NBA — TOP 3 TEAM (MODEL)", msg)

    # Send PROPS
    for p in prop_picks:
        p["tier"] = "MARKET_ONLY"
        p["injury_note"] = p.get("injury_note") or ""
        p["stake_eur"] = stakes[i] if i < len(stakes) else 0.0
        i += 1
        msg = safe_call_format_prop_pick(p)
        post_discord(PROPS_WEBHOOK, "🏀 NBA — TOP 3 PROPS", msg)

    # Update state (spent)
    state["daily_spent_eur"] = float(state.get("daily_spent_eur", 0.0)) + sum([p.get("stake_eur", 0.0) for p in (team_picks + prop_picks)])
    save_json_file("state.json", state)


if __name__ == "__main__":
    main()
