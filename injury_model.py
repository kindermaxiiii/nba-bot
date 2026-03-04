# injury_model.py (V8)
# Minimal, robust injury impact layer (no paid API required).
# - Reads optional data/injuries.json if you maintain it (recommended).
# - Produces per-team: mu_points (delta points) + sigma_mult (uncertainty).
# - Safe defaults if file missing.

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any
import os

from utils import load_json, norm_team, clamp


@dataclass
class TeamInjuryInfo:
    out: int = 0
    doubtful: int = 0
    questionable: int = 0

    # Optional: richer manual inputs if you keep them in injuries.json
    # Example per player: {"name":"X","status":"OUT","mins":34,"role":"STAR"}
    players: list | None = None

    def volatility_score(self) -> float:
        # Q adds most uncertainty; D medium; OUT low uncertainty but high impact.
        return float(self.questionable * 2.0 + self.doubtful * 1.5 + self.out * 0.8)

    def mu_points(self) -> float:
        """
        Points delta (negative if you have more absences).
        If you provide players with mins/role, we compute a better heuristic.
        Otherwise fallback to counts-only heuristic.
        """
        if self.players:
            # Role coefficients: STAR > STARTER > ROTATION > BENCH
            role_coeff = {"STAR": 0.28, "STARTER": 0.20, "ROTATION": 0.14, "BENCH": 0.08}
            delta = 0.0
            for p in self.players:
                status = str(p.get("status", "")).upper()
                mins = float(p.get("mins", 0.0) or 0.0)
                role = str(p.get("role", "ROTATION")).upper()
                c = role_coeff.get(role, 0.14)

                if status == "OUT":
                    delta -= mins * c
                elif status == "DOUBTFUL":
                    delta -= mins * c * 0.70
                elif status in ("QUESTIONABLE", "GTD"):
                    delta -= mins * c * 0.35
            # Clamp to avoid insane swings if file is noisy
            return float(clamp(delta, -10.0, 0.0))

        # Counts-only fallback (safe)
        delta = -1.6 * self.out - 0.9 * self.doubtful - 0.5 * self.questionable
        return float(clamp(delta, -10.0, 0.0))

    def sigma_mult(self) -> float:
        # Uncertainty multiplier (mainly driven by Q/D)
        vol = self.volatility_score()
        # 0 vol -> 1.00 ; 10 vol -> ~1.40
        return float(clamp(1.0 + 0.04 * vol, 1.0, 1.45))


def fetch_injuries(path: str = os.path.join("data", "injuries.json")) -> Dict[str, TeamInjuryInfo]:
    """
    Optional file-based ingestion.
    Format examples:

    1) Counts-only:
    {
      "Boston Celtics": {"out": 1, "doubtful": 0, "questionable": 2}
    }

    2) Player list (better):
    {
      "Boston Celtics": {
        "players": [
          {"name":"J. Tatum","status":"OUT","mins":36,"role":"STAR"},
          {"name":"D. White","status":"QUESTIONABLE","mins":32,"role":"STARTER"}
        ]
      }
    }
    """
    raw = load_json(path) or {}
    out: Dict[str, TeamInjuryInfo] = {}
    if not isinstance(raw, dict):
        return out

    for team_name, v in raw.items():
        if not isinstance(v, dict):
            continue
        info = TeamInjuryInfo(
            out=int(v.get("out", 0) or 0),
            doubtful=int(v.get("doubtful", 0) or 0),
            questionable=int(v.get("questionable", 0) or 0),
            players=v.get("players"),
        )
        out[norm_team(team_name)] = info

    return out


def build_injury_adjustments() -> Dict[str, Dict[str, float]]:
    """
    Normalized mapping:
      team_norm -> {"mu":..., "sigma_mult":..., "vol":..., "out":..., "doubtful":..., "questionable":...}
    """
    inj = fetch_injuries()
    adj: Dict[str, Dict[str, float]] = {}
    for t, info in inj.items():
        adj[t] = {
            "mu": float(info.mu_points()),
            "sigma_mult": float(info.sigma_mult()),
            "vol": float(info.volatility_score()),
            "out": float(info.out),
            "doubtful": float(info.doubtful),
            "questionable": float(info.questionable),
        }
    return adj
