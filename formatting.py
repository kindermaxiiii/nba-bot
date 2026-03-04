# formatting.py
from __future__ import annotations

from typing import Any, Dict, List
from utils import pct


def _fmt_pick(i: int, p: Dict[str, Any]) -> str:
    return (
        f"#{i} — {p['match']}\n"
        f"Market: **{p['market']}**\n"
        f"Pick: **{p['selection']} @ {p['odds']:.2f}** ({p['book']})\n"
        f"p_model: {pct(p['p_model'])} | p_mkt: {pct(p['p_mkt'])} | p_real: {pct(p['p_real'])}\n"
        f"EV: {pct(p['ev'])} | Edge: {pct(p['edge'])} | Dev: {pct(p['dev'])} | Score: {p['score']:.1f}/100\n"
        f"Why: {p.get('why','')}\n"
    )


def format_team_message(picks: List[Dict[str, Any]]) -> str:
    if not picks:
        return "NBA — TOP 3 TEAM\nAucun pick (EV>=0 introuvable après filtre modèle + discipline)."
    lines = ["NBA — TOP 3 TEAM\n"]
    for i, p in enumerate(picks, 1):
        lines.append(_fmt_pick(i, p))
    return "\n".join(lines).strip()


def format_props_message(picks: List[Dict[str, Any]], note: str | None = None) -> str:
    if not picks:
        msg = "NBA — TOP 3 PROPS\nAucun pick."
        if note:
            msg += f"\n({note})"
        return msg
    lines = ["NBA — TOP 3 PROPS\n"]
    for i, p in enumerate(picks, 1):
        lines.append(_fmt_pick(i, p))
    if note:
        lines.append(f"Note: {note}")
    return "\n".join(lines).strip()


def meta_embed(meta: Dict[str, Any]) -> Dict[str, Any]:
    # Discord embed payload
    desc = []
    for k, v in meta.items():
        desc.append(f"**{k}**: {v}")
    return {"title": "NBA BOT — META", "description": "\n".join(desc)[:3900]}
