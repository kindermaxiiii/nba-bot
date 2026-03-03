import os
import json
import requests
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from dateutil import parser

from odds_api import fetch_odds_with_fallback, OddsApiError

from engine import (
    collect_market_lines,
    collect_player_prop_lines,
    collect_team_totals_lines,
    pick_consensus_line,
    pick_consensus_prop_line,
    analyze_two_way_market,
    diversify_team_picks,
    diversify_prop_picks,
    allocate_stakes_capped,
)

from formatting import format_team_pick, format_prop_pick, format_no_bet

# context.py optional
try:
    from context import (
        fetch_injuries,
        build_injury_note,
        search_player_id,
        fetch_player_season_minutes,
    )
except Exception:
    def fetch_injuries():
        return []

    def build_injury_note(match: str, injuries: List[Dict[str, Any]]):
        return None

    def search_player_id(name: str):
        return None

    def fetch_player_season_minutes(pid: Any):
        return None

from model_team import model_prob_for_team_market
from model_props import model_prob_over


TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")


def load_json_file(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            js = json.load(f)
        return js if isinstance(js, dict) else {}
    except Exception:
        return {}


def load_state(path: str = "state.json") -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            js = json.load(f)
        return js if isinstance(js, dict) else {}
    except Exception:
        return {}


CONFIG = load_json_file("config.json")
STATE = load_state("state.json")

BANKROLL = float(CONFIG.get("bankroll_eur", 100.0))
DAILY_BUDGET = BANKROLL * float(CONFIG.get("daily_budget_pct", 0.10))

MAX_TEAM_PER_DAY = int(CONFIG.get("max_team_bets_per_day", 3))
MAX_PROPS_PER_DAY = int(CONFIG.get("max_prop_bets_per_day", 3))

MIN_BOOKMAKERS = int(CONFIG.get("min_bookmakers", 2))
PREFER_FR_BOOKS = bool(CONFIG.get("prefer_fr_books", True))

MAX_ML_PER_SLATE = int(CONFIG.get("max_ml_per_slate", 2))
ONE_PICK_PER_MATCH_TEAM = True

EV_NONNEG_REQUIRED = True  # EV >= 0 obligatoire

ALPHA_TEAM_DEFAULT = float(CONFIG.get("alpha_team", 0.70))
ALPHA_PROPS_DEFAULT = float(CONFIG.get("alpha_props", 0.75))


def save_state():
    with open("state.json", "w", encoding="utf-8") as f:
        json.dump(STATE, f, indent=2, ensure_ascii=False)


def post_discord(webhook: str, title: str, description: str):
    if not webhook:
        return
    data = {"embeds": [{"title": title, "description": description}]}
    r = requests.post(webhook, json=data, timeout=20)
    r.raise_for_status()


def reset_state_if_new_day():
    today_utc = datetime.now(timezone.utc).date().isoformat()
    if STATE.get("date_utc") != today_utc:
        STATE.clear()
        STATE.update({
            "date_utc": today_utc,
            "daily_spent_eur": 0.0,
            "team_bets_sent": 0,
            "prop_bets_sent": 0,
            "team_spent_eur": 0.0,
            "props_spent_eur": 0.0,
        })


def is_game_soon(commence_time: str, horizon_hours: int = 96) -> bool:
    try:
        start_dt = parser.isoparse(commence_time)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return now - timedelta(hours=1) <= start_dt <= now + timedelta(hours=horizon_hours)
    except Exception:
        return False


def _clamp(p: float, lo: float = 0.01, hi: float = 0.99) -> float:
    try:
        p = float(p)
    except Exception:
        return 0.5
    return max(lo, min(hi, p))


def _alpha_props(minutes_proj: Optional[float]) -> float:
    if minutes_proj is None:
        return 0.40
    if minutes_proj < 18:
        return 0.55
    if minutes_proj < 26:
        return 0.65
    return ALPHA_PROPS_DEFAULT


def match_str(g: Dict[str, Any]) -> str:
    return f"{g.get('away_team')} @ {g.get('home_team')}"


def load_features():
    team_features = load_json_file("data/team_features.json")
    player_features = load_json_file("data/player_features.json")
    return team_features, player_features


def find_player_features(player_features: Dict[str, Any], player_name: str) -> Optional[Dict[str, Any]]:
    if not player_features or not player_name:
        return None
    if player_name in player_features:
        return player_features[player_name]
    key = player_name.strip().lower()
    for k, v in player_features.items():
        if str(k).strip().lower() == key:
            return v
    return None


def add_two_way_team(
    out: List[Dict[str, Any]],
    g: Dict[str, Any],
    injuries: List[Dict[str, Any]],
    team_features: Dict[str, Any],
    market_label: str,
    line: Optional[float],
    a: str,
    b: str,
    ent_a: List[Dict[str, Any]],
    ent_b: List[Dict[str, Any]],
):
    home = g.get("home_team")
    away = g.get("away_team")
    if not home or not away:
        return

    match = f"{away} @ {home}"

    base = analyze_two_way_market(
        match=match,
        market_label=market_label,
        line=line,
        outcome_a=a,
        outcome_b=b,
        entries_a=ent_a,
        entries_b=ent_b,
        min_books=MIN_BOOKMAKERS,
        prefer_fr=PREFER_FR_BOOKS,
        p_real_a=None,
        p_real_b=None,
    )
    if len(base) != 2:
        return

    pmkt = {
        base[0]["selection"]: float(base[0]["fair_prob_raw"]),
        base[1]["selection"]: float(base[1]["fair_prob_raw"]),
    }

    p_model_a = model_prob_for_team_market(market_label, a, line, away, home, team_features)
    p_model_b = model_prob_for_team_market(market_label, b, line, away, home, team_features)

    alpha = ALPHA_TEAM_DEFAULT if (p_model_a is not None and p_model_b is not None) else 0.0

    if alpha <= 0.0 or p_model_a is None or p_model_b is None:
        p_real_a = pmkt.get(a, 0.5)
        p_real_b = pmkt.get(b, 0.5)
    else:
        p_real_a = _clamp(alpha * float(p_model_a) + (1.0 - alpha) * pmkt.get(a, 0.5))
        p_real_b = _clamp(alpha * float(p_model_b) + (1.0 - alpha) * pmkt.get(b, 0.5))

    final = analyze_two_way_market(
        match=match,
        market_label=market_label,
        line=line,
        outcome_a=a,
        outcome_b=b,
        entries_a=ent_a,
        entries_b=ent_b,
        min_books=MIN_BOOKMAKERS,
        prefer_fr=PREFER_FR_BOOKS,
        p_real_a=p_real_a,
        p_real_b=p_real_b,
    )

    for it in final:
        it["injury_note"] = build_injury_note(match, injuries)
        it["why_stats"] = {
            "alpha": alpha,
            "p_model": float(p_model_a) if it["selection"] == a and p_model_a is not None else
                       float(p_model_b) if it["selection"] == b and p_model_b is not None else None,
            "p_mkt": float(it.get("fair_prob_raw", 0.0)),
        }
    out.extend(final)


def build_team_candidates(
    g: Dict[str, Any],
    injuries: List[Dict[str, Any]],
    team_features: Dict[str, Any],
) -> List[Dict[str, Any]]:
    home = g.get("home_team")
    away = g.get("away_team")
    if not home or not away:
        return []

    bookmakers = g.get("bookmakers", []) or []
    if not bookmakers:
        return []

    out: List[Dict[str, Any]] = []

    # ML
    h2h = collect_market_lines(bookmakers, "h2h")["lines"]
    lk = pick_consensus_line(h2h)
    if lk and lk in h2h:
        outs = list(h2h[lk].keys())
        if len(outs) >= 2:
            add_two_way_team(out, g, injuries, team_features, "MONEYLINE", None, outs[0], outs[1], h2h[lk][outs[0]], h2h[lk][outs[1]])

    # TOTAL
    totals = collect_market_lines(bookmakers, "totals")["lines"]
    tlk = pick_consensus_line(totals)
    if tlk and tlk in totals:
        sides = totals[tlk]
        if "Over" in sides and "Under" in sides:
            add_two_way_team(out, g, injuries, team_features, "TOTAL", float(tlk), "Over", "Under", sides["Over"], sides["Under"])

    # SPREAD
    spreads = collect_market_lines(bookmakers, "spreads")["lines"]
    slk = pick_consensus_line(spreads)
    if slk and slk in spreads:
        teams = spreads[slk]
        if home in teams and away in teams:
            add_two_way_team(out, g, injuries, team_features, "SPREAD", float(slk), home, away, teams[home], teams[away])

    # TEAM TOTALS (optional) : works only if this market exists in this payload
    tt = collect_team_totals_lines(bookmakers)["lines"]
    ttk = pick_consensus_line(tt)
    if ttk and ttk in tt:
        try:
            team_name, pt = ttk.split("|", 1)
            ptf = float(pt)
        except Exception:
            team_name, ptf = None, None
        if team_name and ptf is not None:
            sides = tt[ttk]
            if "Over" in sides and "Under" in sides:
                add_two_way_team(out, g, injuries, team_features, f"TEAM TOTAL ({team_name})", ptf, "Over", "Under", sides["Over"], sides["Under"])

    # 1H markets (optional)
    h2h1 = collect_market_lines(bookmakers, "h2h_h1")["lines"]
    lk1 = pick_consensus_line(h2h1)
    if lk1 and lk1 in h2h1:
        outs = list(h2h1[lk1].keys())
        if len(outs) >= 2:
            add_two_way_team(out, g, injuries, team_features, "MONEYLINE 1H", None, outs[0], outs[1], h2h1[lk1][outs[0]], h2h1[lk1][outs[1]])

    totals1 = collect_market_lines(bookmakers, "totals_h1")["lines"]
    tlk1 = pick_consensus_line(totals1)
    if tlk1 and tlk1 in totals1:
        sides = totals1[tlk1]
        if "Over" in sides and "Under" in sides:
            add_two_way_team(out, g, injuries, team_features, "TOTAL 1H", float(tlk1), "Over", "Under", sides["Over"], sides["Under"])

    spreads1 = collect_market_lines(bookmakers, "spreads_h1")["lines"]
    slk1 = pick_consensus_line(spreads1)
    if slk1 and slk1 in spreads1:
        teams = spreads1[slk1]
        if home in teams and away in teams:
            add_two_way_team(out, g, injuries, team_features, "SPREAD 1H", float(slk1), home, away, teams[home], teams[away])

    return out


def build_prop_candidates(
    g: Dict[str, Any],
    injuries: List[Dict[str, Any]],
    player_features: Dict[str, Any],
    market_label: str,
    market_key: str,
) -> List[Dict[str, Any]]:
    bookmakers = g.get("bookmakers", []) or []
    if not bookmakers:
        return []

    out: List[Dict[str, Any]] = []
    props_struct = collect_player_prop_lines(bookmakers, market_key)["props"]

    for player, player_lines in props_struct.items():
        lk = pick_consensus_prop_line(player_lines)
        if not lk or lk not in player_lines:
            continue

        sides = player_lines[lk]
        if "Over" not in sides or "Under" not in sides:
            continue

        line = float(lk)
        match = match_str(g)

        pid = search_player_id(player)
        mpg = fetch_player_season_minutes(pid) if pid else None

        pfeat = find_player_features(player_features, player)
        p_over_model = model_prob_over(market_label, pfeat, mpg, line) if pfeat and mpg is not None else None

        alpha = _alpha_props(mpg) if p_over_model is not None else 0.0

        base = analyze_two_way_market(
            match=match,
            market_label=market_label,
            line=line,
            outcome_a="Over",
            outcome_b="Under",
            entries_a=sides["Over"],
            entries_b=sides["Under"],
            min_books=MIN_BOOKMAKERS,
            prefer_fr=PREFER_FR_BOOKS,
            p_real_a=None,
            p_real_b=None,
        )
        if len(base) != 2:
            continue

        pmkt_over = float(base[0]["fair_prob_raw"]) if base[0]["selection"] == "Over" else float(base[1]["fair_prob_raw"])

        if alpha <= 0.0 or p_over_model is None:
            p_real_over = pmkt_over
        else:
            p_real_over = _clamp(alpha * float(p_over_model) + (1.0 - alpha) * pmkt_over)

        p_real_under = _clamp(1.0 - p_real_over)

        final = analyze_two_way_market(
            match=match,
            market_label=market_label,
            line=line,
            outcome_a="Over",
            outcome_b="Under",
            entries_a=sides["Over"],
            entries_b=sides["Under"],
            min_books=MIN_BOOKMAKERS,
            prefer_fr=PREFER_FR_BOOKS,
            p_real_a=p_real_over,
            p_real_b=p_real_under,
        )

        for it in final:
            it["player"] = player
            it["injury_note"] = build_injury_note(match, injuries)
            if mpg is not None:
                it["minutes_note"] = f"{mpg:.1f} min (saison)"
            it["why_stats"] = {
                "alpha": alpha,
                "p_model_over": float(p_over_model) if p_over_model is not None else None,
                "p_mkt": float(it.get("fair_prob_raw", 0.0)),
                "mpg": mpg,
            }

        out.extend(final)

    return out


def main():
    reset_state_if_new_day()

    remaining_budget_total = max(0.0, DAILY_BUDGET - float(STATE.get("daily_spent_eur", 0.0)))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE.get("team_bets_sent", 0)))
    remaining_props_slots = max(0, MAX_PROPS_PER_DAY - int(STATE.get("prop_bets_sent", 0)))

    try:
        injuries = fetch_injuries()
    except Exception:
        injuries = []

    team_features, player_features = load_features()

    # ✅ IMPORTANT: fetch ONLY base markets in one call (always stable)
    try:
        team_games, _meta = fetch_odds_with_fallback(
            markets="h2h,spreads,totals",
            regions_priority=["us"],
        )
    except OddsApiError as e:
        team_games = []
        # log reason
        desc = format_no_bet(
            "NBA NO BET LOG",
            f"OddsAPI erreur sur fetch base markets (h2h,spreads,totals): {str(e)[:200]}",
            ["us"],
            0,
            0,
            [],
            [],
            DAILY_BUDGET,
            float(STATE.get("daily_spent_eur", 0.0)),
        )
        post_discord(LOG_WEBHOOK, "NBA NO BET LOG", desc)
        save_state()
        return

    if not team_games:
        desc = format_no_bet(
            "NBA NO BET LOG",
            "Aucun match reçu depuis OddsAPI sur base markets (h2h,spreads,totals).",
            ["us"],
            0,
            0,
            [],
            [],
            DAILY_BUDGET,
            float(STATE.get("daily_spent_eur", 0.0)),
        )
        post_discord(LOG_WEBHOOK, "NBA NO BET LOG", desc)
        save_state()
        return

    # ✅ OPTIONAL: try extra markets ONE BY ONE, do not fail if 422
    extra_markets = ["team_totals", "h2h_h1", "spreads_h1", "totals_h1"]
    extras_by_id: Dict[str, Dict[str, Any]] = {}

    for mk in extra_markets:
        try:
            gextra, _ = fetch_odds_with_fallback(markets=mk, regions_priority=["us"])
            for ge in gextra or []:
                extras_by_id[str(ge.get("id"))] = ge
        except Exception:
            continue

    # Merge extras bookmakers into base games if available
    for g in team_games:
        gid = str(g.get("id"))
        if gid in extras_by_id:
            # prefer extra bookmakers for extra markets (they may include more markets)
            g["bookmakers"] = extras_by_id[gid].get("bookmakers", g.get("bookmakers", []))

    # Build TEAM candidates
    team_candidates: List[Dict[str, Any]] = []
    games_analyzed = 0

    for g in team_games:
        if not is_game_soon(g.get("commence_time", "")):
            continue
        games_analyzed += 1
        team_candidates.extend(build_team_candidates(g, injuries, team_features))

    if EV_NONNEG_REQUIRED:
        team_candidates = [c for c in team_candidates if float(c.get("ev", -999.0)) >= 0.0]

    team_top = diversify_team_picks(
        team_candidates,
        max_picks=min(3, remaining_team_slots),
        max_ml=MAX_ML_PER_SLATE,
        one_pick_per_match=ONE_PICK_PER_MATCH_TEAM,
    )

    # PROPS (force us)
    prop_market_map = {
        "PROP PTS": "player_points",
        "PROP REB": "player_rebounds",
        "PROP AST": "player_assists",
        "PROP 3PT": "player_threes",
    }

    prop_candidates: List[Dict[str, Any]] = []

    if remaining_props_slots > 0:
        for label, key in prop_market_map.items():
            try:
                games, _ = fetch_odds_with_fallback(markets=key, regions_priority=["us"])
            except Exception:
                continue

            for g in games or []:
                if not is_game_soon(g.get("commence_time", "")):
                    continue
                prop_candidates.extend(build_prop_candidates(g, injuries, player_features, label, key))

    if EV_NONNEG_REQUIRED:
        prop_candidates = [c for c in prop_candidates if float(c.get("ev", -999.0)) >= 0.0]

    prop_top = diversify_prop_picks(
        prop_candidates,
        max_picks=min(3, remaining_props_slots),
        one_pick_per_match=False,
        one_pick_per_player=True,
    )

    if not team_top and not prop_top:
        desc = format_no_bet(
            "NBA NO BET LOG",
            "EV>=0 introuvable sur TEAM + PROPS.",
            ["us"],
            games_analyzed,
            len(team_candidates) + len(prop_candidates),
            [],
            [],
            DAILY_BUDGET,
            float(STATE.get("daily_spent_eur", 0.0)),
        )
        post_discord(LOG_WEBHOOK, "NBA NO BET LOG", desc)
        save_state()
        return

    # Budget split 50/50
    if team_top and prop_top:
        team_budget = remaining_budget_total * 0.50
        props_budget = remaining_budget_total * 0.50
    elif team_top:
        team_budget = remaining_budget_total
        props_budget = 0.0
    else:
        team_budget = 0.0
        props_budget = remaining_budget_total

    # SEND TEAM
    if team_top and team_budget > 0:
        stakes = allocate_stakes_capped(team_budget, len(team_top), max_single_share=0.30)
        for pick, stake in zip(team_top, stakes):
            spent_after = float(STATE.get("daily_spent_eur", 0.0)) + float(stake)
            msg = format_team_pick(pick, float(stake), BANKROLL, DAILY_BUDGET, spent_after)
            post_discord(TEAM_WEBHOOK, "🏀 NBA — TOP 3 TEAM (MODEL)", msg)

            STATE["daily_spent_eur"] = spent_after
            STATE["team_bets_sent"] = int(STATE.get("team_bets_sent", 0)) + 1
            STATE["team_spent_eur"] = float(STATE.get("team_spent_eur", 0.0)) + float(stake)

    # SEND PROPS
    if prop_top and props_budget > 0:
        stakes = allocate_stakes_capped(props_budget, len(prop_top), max_single_share=0.30)
        for pick, stake in zip(prop_top, stakes):
            spent_after = float(STATE.get("daily_spent_eur", 0.0)) + float(stake)
            msg = format_prop_pick(pick, float(stake), BANKROLL, DAILY_BUDGET, spent_after)
            post_discord(PROPS_WEBHOOK, "✅ NBA PLAYER PROPS (MODEL)", msg)

            STATE["daily_spent_eur"] = spent_after
            STATE["prop_bets_sent"] = int(STATE.get("prop_bets_sent", 0)) + 1
            STATE["props_spent_eur"] = float(STATE.get("props_spent_eur", 0.0)) + float(stake)

    save_state()


if __name__ == "__main__":
    main()
