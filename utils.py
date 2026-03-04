# utils.py (V8) — IMPORTANT: this fixes your previous ImportError issues (load_json / clamp / phi)
from __future__ import annotations

import json
import math
import os
import re
from typing import Any, Dict, Optional, Tuple, List


def load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def dump_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def clamp(x: float, lo: float, hi: float) -> float:
    try:
        return float(max(lo, min(hi, float(x))))
    except Exception:
        return float(lo)


def phi(z: float) -> float:
    # Standard normal CDF
    return 0.5 * (1.0 + math.erf(float(z) / math.sqrt(2.0)))


def norm_team(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def no_vig_2way(p_a: float, p_b: float) -> Tuple[float, float]:
    pa = max(0.0, float(p_a))
    pb = max(0.0, float(p_b))
    s = pa + pb
    if s <= 1e-12:
        return 0.5, 0.5
    return pa / s, pb / s


def best_price_for_side(offers: List[Dict[str, Any]], side: str, market: str) -> Optional[Dict[str, Any]]:
    """
    For spreads: choose the best price at the best line for the chosen side.
    Strategy:
      - Find best price for the same side across books.
      - If multiple lines exist, prefer the line that is better for that side:
          * If side is favorite (negative line), prefer closer to 0 (easier cover)
          * If side is underdog (positive line), prefer bigger positive
      - Then within that, choose best odds.
    """
    side = str(side)
    candidates = []
    for off in offers:
        book = off.get("book")
        for oc in off.get("outcomes", []):
            if str(oc.get("name")) != side:
                continue
            if "price" not in oc:
                continue
            try:
                price = float(oc["price"])
            except Exception:
                continue
            point = oc.get("point")
            try:
                point_f = float(point) if point is not None else 0.0
            except Exception:
                point_f = 0.0
            candidates.append({"book": book, "price": price, "point": point_f})

    if not candidates:
        return None

    # Rank by "best line" then odds
    # Better line score: underdog wants larger point; favorite wants closer to 0 (less negative)
    def line_score(c: Dict[str, Any]) -> float:
        pt = float(c["point"])
        if pt >= 0:
            return pt
        return -abs(pt)  # -4.5 > -9.5 (closer to 0)

    candidates.sort(key=lambda c: (line_score(c), c["price"]), reverse=True)
    return candidates[0]


def kelly_fraction(p: float, odds: float) -> float:
    """
    Kelly fraction for decimal odds.
    f* = (bp - q)/b where b = odds-1, q=1-p
    """
    p = float(p)
    odds = float(odds)
    if odds <= 1.0:
        return 0.0
    b = odds - 1.0
    q = 1.0 - p
    f = (b * p - q) / max(1e-12, b)
    return float(clamp(f, 0.0, 1.0))
