"""slate_volatility.py (V7)

Couche -2 — Volatility & Stability Index

Produces:
- slate_class: STABLE / MIXTE / CHAOTIQUE
- multipliers used to downweight props and/or reduce aggressiveness.

We keep it intentionally simple and robust.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class SlateVolatility:
    injury_vol: float
    blowout_index: float
    slate_class: str
    kelly_mult: float
    props_mult: float


def classify_slate(injury_scores: List[float], abs_spreads: List[float]) -> SlateVolatility:
    injury_vol = round(sum(injury_scores) / len(injury_scores), 2) if injury_scores else 0.0
    blowout_index = round(sum(abs_spreads) / len(abs_spreads), 2) if abs_spreads else 0.0

    if injury_vol >= 4.0 or blowout_index >= 10.0:
        return SlateVolatility(injury_vol, blowout_index, "CHAOTIQUE", 0.75, 0.70)
    if injury_vol >= 2.0 or blowout_index >= 7.0:
        return SlateVolatility(injury_vol, blowout_index, "MIXTE", 1.00, 0.90)
    return SlateVolatility(injury_vol, blowout_index, "STABLE", 1.05, 1.00)
