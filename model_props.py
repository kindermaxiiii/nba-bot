import math
from typing import Dict, Any, Optional

def _clamp(p: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, p))

def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

def over_prob(line: float, mean: float, sd: float) -> float:
    if sd <= 1e-6:
        return 0.5
    z = (float(mean) - float(line)) / float(sd)
    return _clamp(_phi(z))

def model_prob_over(
    market_label: str,
    player_features: Dict[str, Any],
    minutes_proj: Optional[float],
    line: float
) -> Optional[float]:
    if not player_features or minutes_proj is None:
        return None

    rates = player_features.get("rates") or {}
    sds = player_features.get("sd") or {}

    def ms(rate_key: str, sd_key: str, base_sd: float):
        r = rates.get(rate_key)
        if r is None: return None
        mean = float(r) * float(minutes_proj)
        sdpm = sds.get(sd_key)
        sd = float(sdpm) * math.sqrt(max(1.0, float(minutes_proj))) if sdpm is not None else base_sd
        return mean, sd

    if market_label == "PROP PTS": pair = ms("pts_per_min", "pts_sd_per_min", 6.5)
    elif market_label == "PROP REB": pair = ms("reb_per_min", "reb_sd_per_min", 3.5)
    elif market_label == "PROP AST": pair = ms("ast_per_min", "ast_sd_per_min", 3.0)
    elif market_label == "PROP 3PT": pair = ms("threes_per_min", "threes_sd_per_min", 2.0)
    else:
        return None

    if pair is None:
        return None
    mean, sd = pair
    return over_prob(float(line), float(mean), float(sd))
