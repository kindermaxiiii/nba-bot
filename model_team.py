# model_team.py (V8)
# Team "model-first" margin model with script mixture driven by mu + volatility + fragility proxies.

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any, Tuple
import math

from utils import norm_team, clamp, phi


@dataclass
class ScriptWeights:
    w_close: float
    w_controlled: float
    w_blowout: float


def _get_feat(features: Dict[str, Any], team_norm: str) -> Dict[str, Any]:
    # features can be keyed by raw team names; normalize keys
    if not features:
        return {}
    if "__norm_cache__" in features:
        cache = features["__norm_cache__"]
    else:
        cache = {norm_team(k): v for k, v in features.items() if isinstance(v, dict)}
        features["__norm_cache__"] = cache
    return cache.get(team_norm, {}) or {}


def margin_prior_mu_sigma(
    home_team: str,
    away_team: str,
    features: Dict[str, Any],
    inj_adjust: Dict[str, Dict[str, float]] | None = None,
) -> Tuple[float, float, Dict[str, float]]:
    """
    Returns (mu_points, sigma_points, debug)
    mu based on net rating differential + HCA + simple rest/travel if present.
    sigma base then adjusted by volatility.
    """
    h = norm_team(home_team)
    a = norm_team(away_team)

    hf = _get_feat(features, h)
    af = _get_feat(features, a)

    # Base net rating prior (very robust)
    h_net = float(hf.get("net_rating", hf.get("netrtg", 0.0)) or 0.0)
    a_net = float(af.get("net_rating", af.get("netrtg", 0.0)) or 0.0)

    # Optional rest/travel proxies if you store them
    h_rest = float(hf.get("rest_adj", 0.0) or 0.0)
    a_rest = float(af.get("rest_adj", 0.0) or 0.0)

    # HCA
    hca = float(hf.get("hca", 2.1) or 2.1)

    mu = (h_net - a_net) + (h_rest - a_rest) + hca

    # Injury adjustment
    inj_adjust = inj_adjust or {}
    h_inj = inj_adjust.get(h, {})
    a_inj = inj_adjust.get(a, {})
    mu += float(h_inj.get("mu", 0.0)) - float(a_inj.get("mu", 0.0))

    # Base sigma (NBA spread residual std tends to live ~11-13; keep stable)
    sigma = float(hf.get("sigma_base", 12.0) or 12.0)

    # Increase sigma if injuries uncertain (Q/D)
    sigma *= float(h_inj.get("sigma_mult", 1.0))
    sigma *= float(a_inj.get("sigma_mult", 1.0))

    dbg = {
        "h_net": h_net,
        "a_net": a_net,
        "h_rest": h_rest,
        "a_rest": a_rest,
        "hca": hca,
        "h_inj_mu": float(h_inj.get("mu", 0.0)),
        "a_inj_mu": float(a_inj.get("mu", 0.0)),
        "sigma": sigma,
    }
    return float(mu), float(clamp(sigma, 9.5, 16.5)), dbg


def script_weights(
    mu: float,
    injury_vol: float = 0.0,
    rotation_fragility: float = 0.0,
) -> ScriptWeights:
    """
    Close/controlled/blowout mixture.
    Depends on:
      - abs(mu)
      - injury volatility (more chaos -> more blowout/variance)
      - rotation fragility (more chaos -> more blowout/variance)
    """
    x = abs(mu)

    # Base blowout pressure from margin
    base_blow = clamp((x - 6.0) / 12.0, 0.0, 1.0)  # ~0 at 6, ~1 at 18
    # Chaos contributes to blowout probability and reduces "close"
    chaos = clamp(0.06 * injury_vol + 0.06 * rotation_fragility, 0.0, 0.35)

    w_blow = clamp(0.10 + 0.60 * base_blow + chaos, 0.08, 0.85)
    w_close = clamp(0.60 - 0.45 * base_blow - 0.60 * chaos, 0.08, 0.80)
    w_ctrl = clamp(1.0 - w_blow - w_close, 0.08, 0.70)

    # Renormalize
    s = w_blow + w_close + w_ctrl
    return ScriptWeights(w_close / s, w_ctrl / s, w_blow / s)


def sigma_effective(sigma: float, w: ScriptWeights) -> float:
    # In blowout scripts: outcome variance higher but starters minutes lower -> pricing noisy
    sigma_close = sigma * 0.95
    sigma_ctrl = sigma * 1.00
    sigma_blow = sigma * 1.15
    return float(w.w_close * sigma_close + w.w_controlled * sigma_ctrl + w.w_blowout * sigma_blow)


def p_real_spread(line: float, mu: float, sigma: float) -> float:
    # Probability home covers (home line could be -4.5 etc). For book lines we evaluate side explicitly elsewhere.
    z = (line - mu) / max(1e-9, sigma)
    return float(clamp(1.0 - phi(z), 0.01, 0.99))


def p_real_ml(mu: float, sigma: float) -> float:
    # Home win prob from margin normal approx
    z = (0.0 - mu) / max(1e-9, sigma)
    return float(clamp(1.0 - phi(z), 0.01, 0.99))


def p_real_total(line: float, base_total_mu: float, sigma_total: float) -> float:
    # Placeholder if you later model totals. For now: treat as normal around base_total_mu.
    z = (line - base_total_mu) / max(1e-9, sigma_total)
    return float(clamp(1.0 - phi(z), 0.01, 0.99))


def team_model(
    home_team: str,
    away_team: str,
    features: Dict[str, Any],
    inj_adjust: Dict[str, Dict[str, float]] | None = None,
    rotation_fragility: float = 0.0,
) -> Dict[str, Any]:
    """
    Returns a model bundle used by engine.py:
      {mu, sigma, sigma_eff, scripts, dbg}
    """
    mu, sigma, dbg = margin_prior_mu_sigma(home_team, away_team, features, inj_adjust=inj_adjust)

    # Combine injury volatility from both teams
    h = norm_team(home_team)
    a = norm_team(away_team)
    inj_adjust = inj_adjust or {}
    injury_vol = float(inj_adjust.get(h, {}).get("vol", 0.0)) + float(inj_adjust.get(a, {}).get("vol", 0.0))

    w = script_weights(mu, injury_vol=injury_vol, rotation_fragility=rotation_fragility)
    sig_eff = sigma_effective(sigma, w)

    return {
        "mu": float(mu),
        "sigma": float(sigma),
        "sigma_eff": float(sig_eff),
        "scripts": {"close": w.w_close, "controlled": w.w_controlled, "blowout": w.w_blowout},
        "dbg": dbg,
    }
