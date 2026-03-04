# formatting.py (V8)
# Discord embeds with stake display

from __future__ import annotations
from typing import Any, Dict, List, Optional


def _fmt_pct(x: float, digits: int = 2) -> str:
    try:
        return f"{100.0 * float(x):.{digits}f}%"
    except Exception:
        return "NA"


def embed_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    lines = []
    for k in [
        "run_id",
        "ts_utc",
        "sport_key",
        "region_used",
        "markets_used",
        "games",
        "team_candidates",
        "team_picks",
        "props_supported",
        "props_picks",
        "clip",
        "clip_hits",
        "clip_hit_rate",
        "feature_coverage_pct",
    ]:
        if k in meta:
            lines.append(f"{k}: {meta[k]}")
    if "slate" in meta:
        s = meta["slate"]
        lines.append(f"slate: {{class: {s.get('class')}, injury_vol: {s.get('injury_vol')}, blowout_index: {s.get('blowout_index')}, kelly_mult: {s.get('kelly_mult')}, props_mult: {s.get('props_mult')}}}")
    if meta.get("props_note"):
        lines.append(f"props_note: {meta['props_note']}")

    return {
        "title": "NBA BOT — META",
        "description": "```\n" + "\n".join(lines) + "\n```",
        "color": 3447003,
    }


def embed_no_picks(title: str, reason: str) -> Dict[str, Any]:
    return {"title": title, "description": reason, "color": 15158332}


def embed_picks(title: str, picks: List[Dict[str, Any]], color: int = 3066993) -> Dict[str, Any]:
    parts: List[str] = []
    for i, p in enumerate(picks[:10], start=1):
        parts.append(f"#{i} — {p.get('match')}")
        parts.append(f"Market: {p.get('market')} | Line: {p.get('line')}")
        parts.append(f"Pick: {p.get('side')} @ {p.get('odds')} ({p.get('book')})")
        parts.append(f"p_model: {_fmt_pct(p.get('p_model', 0.0))} | p_mkt: {_fmt_pct(p.get('p_mkt', 0.0))} | p_real: {_fmt_pct(p.get('p_real', 0.0))}")
        parts.append(f"EV: {_fmt_pct(p.get('ev', 0.0))} | Edge: {_fmt_pct(p.get('edge', 0.0))} | Dev: {_fmt_pct(p.get('dev', 0.0))} | Score: {float(p.get('score', 0.0)):.1f}/100")
        if "stake_pct" in p:
            parts.append(f"Stake: {_fmt_pct(p.get('stake_pct', 0.0), 2)} BR | Kelly_raw: {_fmt_pct(p.get('kelly_raw', 0.0), 2)}")
        if p.get("why"):
            parts.append(f"Why: {p.get('why')}")
        parts.append("")

    return {"title": title, "description": "\n".join(parts).strip(), "color": color}
