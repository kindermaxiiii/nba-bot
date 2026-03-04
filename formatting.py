# formatting.py
from __future__ import annotations

from typing import Any, Dict, List


def _pct(x: Any) -> str:
    try:
        return f"{float(x)*100:.2f}%"
    except Exception:
        return "?"


def _num(x: Any) -> str:
    try:
        return f"{float(x):.1f}"
    except Exception:
        return "?"


def picks_embed(title: str, picks: List[Dict[str, Any]], color: int) -> Dict[str, Any]:
    if not picks:
        return {"title": title, "description": "Aucun pick.", "color": 15158332}

    lines: List[str] = []
    for i, p in enumerate(picks, 1):
        line = p.get("line")
        line_s = f" | Line: {line}" if line is not None else ""
        lines.append(
            f"**#{i}** — {p.get('match','?')}\n"
            f"Market: **{p.get('market','?')}**{line_s}\n"
            f"Pick: **{p.get('selection','?')}** @ **{float(p.get('odds',0)):.2f}** ({p.get('book','?')})\n"
            f"p_model: {_pct(p.get('p_model'))} | p_mkt: {_pct(p.get('p_mkt'))} | p_real: {_pct(p.get('p_real'))}\n"
            f"EV: {_pct(p.get('ev'))} | Edge: {_pct(p.get('edge'))} | Dev: {_pct(p.get('dev'))} | Score: {_num(p.get('score'))}/100\n"
            f"Why: {p.get('why','')}\n"
        )
    return {"title": title, "description": "\n".join(lines)[:3900], "color": color}


def meta_embed(meta: Dict[str, Any]) -> Dict[str, Any]:
    desc = "\n".join([f"**{k}**: {v}" for k, v in meta.items()])[:3900]
    return {"title": "NBA BOT — META", "description": desc, "color": 3447003}
