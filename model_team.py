import math
from typing import Dict, Any, Optional

def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))

def _clamp(p: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, p))

def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

def team_win_prob(away: Dict[str, Any], home: Dict[str, Any], home_adv_pts: float = 2.3) -> float:
    a_net = away.get("net_rating")
    h_net = home.get("net_rating")
    if a_net is None or h_net is None:
        return 0.5
    margin = float(h_net) - float(a_net) + home_adv_pts
    return _clamp(_sigmoid(margin / 7.5))

def expected_total_points(away: Dict[str, Any], home: Dict[str, Any]) -> Optional[float]:
    a_pace = away.get("pace"); h_pace = home.get("pace")
    a_or = away.get("ortg"); h_or = home.get("ortg")
    a_dr = away.get("drtg"); h_dr = home.get("drtg")
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
    a_net = away.get("net_rating"); h_net = home.get("net_rating")
    mean_margin = (float(h_net) - float(a_net) + 2.3) if (a_net is not None and h_net is not None) else 0.0

    if market in ("H2H",):
        if selection == home_team: return p_home
        if selection == away_team: return 1.0 - p_home
        return None

    if market in ("SPREAD",) and line is not None:
        spread_home = float(line) if selection == home_team else -float(line)
        p_home_cover = spread_cover_prob(mean_margin, spread_home)
        return p_home_cover if selection == home_team else (1.0 - p_home_cover)

    if market in ("TOTAL",) and line is not None:
        exp_total = expected_total_points(away, home)
        if exp_total is None:
            return None
        p_over = total_over_prob(float(line), float(exp_total))
        return p_over if selection == "Over" else (1.0 - p_over)

    return None
