# utils.py
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Tuple


def dec_to_prob(odds: float) -> float:
    if odds is None or odds <= 0:
        return 0.0
    return 1.0 / float(odds)


def implied_prob_no_vig_two_way(odds_a: float, odds_b: float) -> Tuple[float, float]:
    pa = dec_to_prob(odds_a)
    pb = dec_to_prob(odds_b)
    s = pa + pb
    if s <= 0:
        return 0.5, 0.5
    return pa / s, pb / s


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()
