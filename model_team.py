import math
from typing import Dict, Any, Optional

def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))

def _clamp(p: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, p))

def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

def _get_any(d: Dict[str, Any], keys, default=None):
    for k in keys:
        if k in d and d.get(k) is not None:
            return d.get(k)
    return default

def team_win_prob(away: Dict[str, Any], home: Dict[str, Any], home_adv_pts: float = 2.3) -> float:
    # Supporte net_rtg (team_features.json) et net_rating (autres)
    a_net = _get_any(away, ["net_rtg", "net_rating", "net_rating_pts"])
    h_net = _get_any(home, ["net_rtg", "net_rating", "net_rating_pts"])
    if a_net is None or h_net is None:
        return 0.5

    margin = float(h_net) - float(a_net) + home_adv_pts
    return _clamp(_sigmoid(margin / 7.5))

def expected_total_points(away: Dict[str, Any], home: Dict[str, Any]) -> Optional[float]:
    # Supporte pace/off_rtg/def_rtg (team_features.json) et ortg/drtg (autres)
    a_pace = _get_any(away, ["pace"])
    h_pace = _get_any(home, ["pace"])

    a_or = _get_any(away, ["off_rtg", "ortg"])
    h_or = _get_any(home, ["off_rtg", "ortg"])

    a_dr = _get_any(away, ["def_rtg", "drtg"])
    h_dr = _get_any(home, ["def_rtg", "drtg"])

    if None in (a_pace, h_pace, a_or, h_or, a_dr, h_dr):
        return None

    pace = 0.5 * (float(a_pace) + float(h_pace))
    home_ppp = 0.5 * (float(h_or) + float(a_dr)) / 100.0
    away_ppp = 0.5 * (float(a_or) + float(h_dr)) / 100.0
    return float(pace * (home_ppp + away_ppp))

def total_over_prob(total_line: float, exp_total: float, sigma_total: float = 22.0) -> float:
    z = (float(exp_total) - float(total_line)) / float(sigma_total)
    return _clamp(_phi(z))

def spread_cover_prob(mean_margin: float, spread_home: float, sigma_pts: float = 12.0) -> float:
    # margin > -spread_home
    threshold = -float(spread_home)
    z = (float(mean_margin) - threshold) / float(sigma_pts)
    return _clamp(_phi(z))

def model_prob_for_team_market(
    market: str,
    selection: str,
    line: Optional[float],
    away_team: str,
    home_team: str,
    features: Dict[str, Dict[str, Any]],
) -> Optional[float]:
    away = features.get(away_team) or {}
    home = features.get(home_team) or {}

    p_home = team_win_prob(away, home)

    a_net = _get_any(away, ["net_rtg", "net_rating", "net_rating_pts"])
    h_net = _get_any(home, ["net_rtg", "net_rating", "net_rating_pts"])
    mean_margin = (float(h_net) - float(a_net) + 2.3) if (a_net is not None and h_net is not None) else 0.0

    if market in ("MONEYLINE", "MONEYLINE 1H"):
        if selection == home_team:
            return p_home
        if selection == away_team:
            return 1.0 - p_home
        return None

    if market in ("SPREAD", "SPREAD 1H") and line is not None:
        # line = handicap affiché pour "selection"
        spread_home = float(line) if selection == home_team else -float(line)
        p_home_cover = spread_cover_prob(mean_margin, spread_home)
        return p_home_cover if selection == home_team else (1.0 - p_home_cover)

    if market in ("TOTAL", "TOTAL 1H") and line is not None:
        exp_total = expected_total_points(away, home)
        if exp_total is None:
            return None
        p_over = total_over_prob(float(line), float(exp_total))
        return p_over if selection == "Over" else (1.0 - p_over)

    if market.startswith("TEAM TOTAL") and line is not None:
        exp_total = expected_total_points(away, home)
        if exp_total is None:
            return None

        a_or = _get_any(away, ["off_rtg", "ortg"])
        h_or = _get_any(home, ["off_rtg", "ortg"])
        if a_or is None or h_or is None:
            return None

        share_home = float(h_or) / (float(h_or) + float(a_or))
        exp_home = exp_total * share_home
        exp_away = exp_total - exp_home

        team = market.split("(", 1)[1].split(")", 1)[0].strip() if "(" in market else None
        exp = exp_home if team == home_team else exp_away if team == away_team else None
        if exp is None:
            return None

        p_over = total_over_prob(float(line), float(exp), sigma_total=14.0)
        return p_over if selection == "Over" else (1.0 - p_over)

    return None
