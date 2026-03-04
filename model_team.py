# model_team.py
from __future__ import annotations

import json
import math
from typing import Any, Dict, Optional, Tuple

from utils import clamp, phi


HCA_PTS = 2.3  # home court advantage baseline


def _load_team_features() -> Dict[str, Dict[str, Any]]:
    try:
        with open("data/team_features.json", "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _script_weights(mu: float) -> Tuple[float, float, float]:
    """
    close/controlled/blowout weights based on expected margin.
    """
    a = abs(mu)
    close = clamp(1.0 - a / 12.0, 0.20, 0.80)
    blow = clamp((a - 8.0) / 10.0, 0.0, 0.40)
    controlled = clamp(1.0 - close - blow, 0.10, 0.70)
    s = close + controlled + blow
    return close / s, controlled / s, blow / s


def margin_model(home: Dict[str, Any], away: Dict[str, Any], inj_mu_home: float = 0.0, inj_mu_away: float = 0.0, inj_sigma_mult: float = 1.0) -> Tuple[float, float]:
    """
    Return (mu, sigma) for home margin.
    Uses net_rating if available; else fallback to 0 with wider sigma.
    """
    h_net = home.get("net_rating")
    a_net = away.get("net_rating")

    if h_net is None or a_net is None:
        # fallback: uncertain
        mu = HCA_PTS + float(inj_mu_home) - float(inj_mu_away)
        sigma = 13.5 * float(inj_sigma_mult)
        return mu, sigma

    mu = float(h_net) - float(a_net) + HCA_PTS + float(inj_mu_home) - float(inj_mu_away)

    # sigma baseline; inflate with pace (higher pace -> more variance)
    h_pace = home.get("pace")
    a_pace = away.get("pace")
    pace = None
    if h_pace is not None and a_pace is not None:
        pace = 0.5 * (float(h_pace) + float(a_pace))
    sigma = 12.0
    if pace:
        sigma *= math.sqrt(clamp(pace / 100.0, 0.85, 1.20))

    # script mixture increases variance
    w_close, w_ctrl, w_blow = _script_weights(mu)
    sig_close, sig_ctrl, sig_blow = 10.5, 12.0, 15.0
    sigma_eff = math.sqrt(w_close * sig_close**2 + w_ctrl * sig_ctrl**2 + w_blow * sig_blow**2)

    sigma_eff *= float(inj_sigma_mult)

    return mu, max(9.5, sigma_eff)


def expected_total(home: Dict[str, Any], away: Dict[str, Any], inj_total_shift: float = 0.0, inj_sigma_mult: float = 1.0) -> Optional[Tuple[float, float]]:
    """
    Return (mu_total, sigma_total) using pace + ORtg/DRtg if available.
    """
    h_pace = home.get("pace"); a_pace = away.get("pace")
    h_or = home.get("ortg"); a_or = away.get("ortg")
    h_dr = home.get("drtg"); a_dr = away.get("drtg")

    if None in (h_pace, a_pace, h_or, a_or, h_dr, a_dr):
        return None

    pace = 0.5 * (float(h_pace) + float(a_pace))
    home_ppp = 0.5 * (float(h_or) + float(a_dr)) / 100.0
    away_ppp = 0.5 * (float(a_or) + float(h_dr)) / 100.0
    mu = float(pace * (home_ppp + away_ppp)) + float(inj_total_shift)

    sigma = 22.0 * math.sqrt(clamp(pace / 100.0, 0.85, 1.25))
    sigma *= float(inj_sigma_mult)
    return mu, sigma


def p_home_win(mu: float, sigma: float) -> float:
    z = (mu - 0.0) / sigma
    return clamp(phi(z), 0.01, 0.99)


def p_home_cover(mu: float, sigma: float, spread_home: float) -> float:
    # cover if margin_home > -spread_home
    thresh = -float(spread_home)
    z = (mu - thresh) / sigma
    return clamp(phi(z), 0.01, 0.99)


def p_total_over(mu_total: float, sigma_total: float, total_line: float) -> float:
    z = (float(mu_total) - float(total_line)) / float(sigma_total)
    return clamp(phi(z), 0.01, 0.99)


def team_p_model(
    market: str,
    selection: str,
    line: Optional[float],
    away_team: str,
    home_team: str,
    features: Optional[Dict[str, Dict[str, Any]]] = None,
    inj_mu_home: float = 0.0,
    inj_mu_away: float = 0.0,
    inj_sigma_mult: float = 1.0,
) -> Optional[float]:
    features = features if features is not None else _load_team_features()
    home = features.get(home_team) or {}
    away = features.get(away_team) or {}

    mu, sigma = margin_model(home, away, inj_mu_home=inj_mu_home, inj_mu_away=inj_mu_away, inj_sigma_mult=inj_sigma_mult)

    if market == "H2H":
        p_h = p_home_win(mu, sigma)
        if selection == home_team:
            return p_h
        if selection == away_team:
            return 1.0 - p_h
        return None

    if market == "SPREAD" and line is not None:
        # line is signed for selection
        spread_home = float(line) if selection == home_team else -float(line)
        p_h = p_home_cover(mu, sigma, spread_home)
        return p_h if selection == home_team else (1.0 - p_h)

    if market == "TOTAL" and line is not None:
        et = expected_total(home, away, inj_total_shift=(inj_mu_home + inj_mu_away), inj_sigma_mult=inj_sigma_mult)
        if et is None:
            return None
        mu_t, sig_t = et
        p_over = p_total_over(mu_t, sig_t, float(line))
        return p_over if selection == "Over" else (1.0 - p_over)

    return None
