# utils.py
from __future__ import annotations

import math
from datetime import datetime, timezone


def dec_to_prob(odds: float) -> float:
    if odds <= 0:
        return 0.0
    return 1.0 / odds


def implied_prob_no_vig_two_way(odds_a: float, odds_b: float) -> tuple[float, float]:
    pa = dec_to_prob(odds_a)
    pb = dec_to_prob(odds_b)
    s = pa + pb
    if s <= 0:
        return 0.5, 0.5
    return pa / s, pb / s


def pct(x: float) -> str:
    return f"{x*100:.2f}%"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
