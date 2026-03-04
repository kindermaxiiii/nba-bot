"""injury_model.py (V7)

Lightweight injury / roster-volatility signal using free nba_api endpoints.

Why this exists:
- The "perfect" human analysis won primarily on roster shocks + script shifts.
- We can't perfectly quantify star impact with free data reliably.
- But we CAN:
  - detect how many OUT / DOUBTFUL / QUESTIONABLE per team
  - slightly adjust the team margin prior (mu)
  - inflate uncertainty (sigma)
  - drive the slate volatility class

This module is intentionally defensive: if the endpoint fails, returns {}.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass
class TeamInjury:
    out: int = 0
    doubtful: int = 0
    questionable: int = 0

    def volatility_score(self) -> float:
        # 0..10-ish
        return min(10.0, 2.0 * self.out + 1.0 * self.doubtful + 0.5 * self.questionable)

    def mu_points(self) -> float:
        # Conservative expected performance drag (points).
        return -0.8 * self.out - 0.4 * self.doubtful - 0.2 * self.questionable

    def sigma_mult(self) -> float:
        # Uncertainty inflation multiplier.
        return 1.0 + 0.03 * self.out + 0.04 * self.doubtful + 0.05 * self.questionable


def fetch_injuries() -> Dict[str, TeamInjury]:
    """Return {team_string: TeamInjury}.

    Team string comes from the NBA stats endpoint; we match later via normalization.
    """
    try:
        from nba_api.stats.endpoints import leagueinjuryreport

        df = leagueinjuryreport.LeagueInjuryReport().get_data_frames()[0]
    except Exception:
        return {}

    # Identify columns
    team_col = None
    for c in ("TEAM_NAME", "TEAM", "TEAM_ABBREVIATION"):
        if c in df.columns:
            team_col = c
            break

    status_col = None
    for c in ("INJURY_STATUS", "STATUS", "REPORT_STATUS"):
        if c in df.columns:
            status_col = c
            break

    if team_col is None or status_col is None:
        return {}

    out: Dict[str, TeamInjury] = {}

    for _, r in df.iterrows():
        team = str(r.get(team_col) or "").strip()
        if not team:
            continue
        status = str(r.get(status_col) or "").strip().lower()

        ti = out.get(team) or TeamInjury()

        if "out" in status:
            ti.out += 1
        elif "doubt" in status:
            ti.doubtful += 1
        elif "quest" in status or "prob" in status or "gtd" in status:
            ti.questionable += 1

        out[team] = ti

    return out
