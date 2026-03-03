# formatting.py
from typing import Any, Dict, List, Optional


def _pct(x: float, digits: int = 2) -> str:
    try:
        return f"{float(x)*100:.{digits}f}%"
    except Exception:
        return "n/a"


def _fmt_money(x: float) -> str:
    try:
        return f"{float(x):.2f}€"
    except Exception:
        return "n/a"


def _maybe(v: Optional[float], fmt: str = "{:.2f}") -> str:
    if v is None:
        return "n/a"
    try:
        return fmt.format(float(v))
    except Exception:
        return "n/a"


def _bar(value: float, vmin: float, vmax: float, width: int = 22) -> str:
    """
    Simple ASCII bar. value clamped in [vmin,vmax]
    """
    try:
        v = float(value)
    except Exception:
        v = vmin
    if vmax <= vmin:
        vmax = vmin + 1e-9
    v = max(vmin, min(vmax, v))
    r = (v - vmin) / (vmax - vmin)
    filled = int(round(r * width))
    return "█" * filled + "░" * (width - filled)


def _clv_block(p: Dict[str, Any]) -> str:
    snaps = p.get("clv_snapshots") or []
    if not snaps:
        return ""
    lines = []
    for s in snaps[-4:]:
        tag = s.get("tag", "?")
        odds = s.get("odds")
        book = s.get("book", "")
        dt = s.get("ts_utc", "")
        if odds is None:
            lines.append(f"• {tag}: n/a @ {dt}")
        else:
            lines.append(f"• {tag}: {float(odds):.2f} ({book}) @ {dt}")
    return "\n\n📈 **CLV snapshots**\n" + "\n".join(lines)


def _flags_block(p: Dict[str, Any]) -> str:
    tier = str(p.get("tier", "STRICT"))
    haircut = bool(p.get("haircut_applied", False))
    flags = p.get("flags") or []

    out = []
    if tier == "WATCHLIST":
        out.append("🟠 WATCHLIST (below SAFE, stake ×0.30)")
    else:
        out.append(f"🟢 {tier}")

    if haircut:
        out.append("✂️ haircut -30%")

    for f in flags:
        out.append(f"⚠️ {f}")

    return "\n".join(f"• {x}" for x in out) if out else "• (aucun)"


def format_team_pick(p: Dict[str, Any], stake: float, bankroll: float, daily_budget: float, spent_after: float) -> str:
    match = p.get("match", "")
    market = p.get("market", "TEAM")
    selection = p.get("selection", "")
    line = p.get("line")
    odds = float(p.get("odds", 0.0))
    book = p.get("book", "Unknown")

    books_used = int(p.get("books_used", 0) or 0)
    total_books = int(p.get("total_books", 0) or 0)
    median_odds = p.get("median_odds")

    fair_raw = float(p.get("fair_prob_raw", p.get("fair_prob", 0.0)) or 0.0)
    fair_adj = float(p.get("fair_prob", 0.0) or 0.0)

    edge_raw = float(p.get("edge_raw", p.get("edge", 0.0)) or 0.0)
    edge_adj = float(p.get("edge", 0.0) or 0.0)
    dev = float(p.get("dev", 0.0) or 0.0)
    ev = float(p.get("ev", fair_adj * odds - 1.0) or 0.0)

    score = float(p.get("score", 0.0) or 0.0)
    score_base = float(p.get("score_base", 0.0) or 0.0)
    score_pen = float(p.get("score_penalty", 0.0) or 0.0)

    vig_median = p.get("vig_median")
    odds_sd = p.get("odds_stdev")

    pct_bk = (stake / bankroll) if bankroll > 0 else 0.0
    pct_day = (stake / daily_budget) if daily_budget > 0 else 0.0

    injury_note = p.get("injury_note")
    minutes_note = p.get("minutes_note")

    ctx = []
    if injury_note:
        ctx.append(f"**Injuries:** {injury_note}")
    if minutes_note:
        ctx.append(f"**Minutes proj.:** {minutes_note}")

    ctx_block = ("\n\n" + "\n".join(ctx)) if ctx else ""

    line_part = f"\nLine: {line}" if line is not None else ""
    clv = _clv_block(p)

    value_metrics = (
        "📊 **VALUE METRICS**\n"
        f"• Books (median calc): {books_used} | Total books: {total_books} | Median odds: {_maybe(median_odds)}\n"
        f"• p_fair: {_pct(fair_adj)} (raw {_pct(fair_raw)})\n"
        f"• EV: {_pct(ev)}\n"
        f"• Edge: {_pct(edge_adj)} (raw {_pct(edge_raw)})  {_bar(edge_adj, 0.0, 0.08)}\n"
        f"• Dev vs median: {_pct(dev)}  {_bar(dev, 0.0, 0.15)}\n"
        f"• Score: {score:.0f}/100  ({_bar(score, 0.0, 100.0, 18)}) | base={score_base:.0f} | pen={score_pen:.0f}\n"
        + (f"• Median vig proxy: {_pct(vig_median)}\n" if vig_median is not None else "")
        + (f"• Odds dispersion (stdev): {_maybe(odds_sd)}\n" if odds_sd is not None else "")
    )

    flags_block = "🧩 **FLAGS**\n" + _flags_block(p)

    stake_block = (
        "💰 **STAKE**\n"
        f"• Stake: {pct_bk*100:.2f}% BK ({_fmt_money(stake)}) — {pct_day*100:.2f}% day budget\n"
        f"• Day budget: {_fmt_money(daily_budget)} | Spent after: {_fmt_money(spent_after)}"
    )

    return (
        "✅ **NBA TEAM BET**\n"
        f"Match: {match}\n"
        f"Market: {market}\n"
        f"Pick: {selection}"
        + (f"{line_part}\n" if line is not None else "\n")
        + f"Best: {odds:.2f} ({book})\n\n"
        + value_metrics
        + "\n"
        + flags_block
        + "\n\n"
        + stake_block
        + ctx_block
        + clv
        + "\n\n_If odds moved a lot before clicking: skip._"
    )


def format_prop_pick(p: Dict[str, Any], stake: float, bankroll: float, daily_budget: float, spent_after: float) -> str:
    match = p.get("match", "")
    market = p.get("market", "PROP")
    player = p.get("player", "")
    side = p.get("selection", "")
    line = p.get("line")
    odds = float(p.get("odds", 0.0))
    book = p.get("book", "Unknown")

    books_used = int(p.get("books_used", 0) or 0)
    total_books = int(p.get("total_books", 0) or 0)
    median_odds = p.get("median_odds")

    fair_raw = float(p.get("fair_prob_raw", p.get("fair_prob", 0.0)) or 0.0)
    fair_adj = float(p.get("fair_prob", 0.0) or 0.0)

    edge_raw = float(p.get("edge_raw", p.get("edge", 0.0)) or 0.0)
    edge_adj = float(p.get("edge", 0.0) or 0.0)
    dev = float(p.get("dev", 0.0) or 0.0)
    ev = float(p.get("ev", fair_adj * odds - 1.0) or 0.0)

    score = float(p.get("score", 0.0) or 0.0)
    score_base = float(p.get("score_base", 0.0) or 0.0)
    score_pen = float(p.get("score_penalty", 0.0) or 0.0)

    vig_median = p.get("vig_median")
    odds_sd = p.get("odds_stdev")

    pct_bk = (stake / bankroll) if bankroll > 0 else 0.0
    pct_day = (stake / daily_budget) if daily_budget > 0 else 0.0

    injury_note = p.get("injury_note")
    minutes_note = p.get("minutes_note")

    ctx = []
    if injury_note:
        ctx.append(f"**Injuries:** {injury_note}")
    if minutes_note:
        ctx.append(f"**Minutes proj.:** {minutes_note}")

    ctx_block = ("\n\n" + "\n".join(ctx)) if ctx else ""

    clv = _clv_block(p)

    sel = f"{player} — {side} {line}" if line is not None else f"{player} — {side}"

    value_metrics = (
        "📊 **VALUE METRICS**\n"
        f"• Books (median calc): {books_used} | Total books: {total_books} | Median odds: {_maybe(median_odds)}\n"
        f"• p_fair: {_pct(fair_adj)} (raw {_pct(fair_raw)})\n"
        f"• EV: {_pct(ev)}\n"
        f"• Edge: {_pct(edge_adj)} (raw {_pct(edge_raw)})  {_bar(edge_adj, 0.0, 0.08)}\n"
        f"• Dev vs median: {_pct(dev)}  {_bar(dev, 0.0, 0.15)}\n"
        f"• Score: {score:.0f}/100  ({_bar(score, 0.0, 100.0, 18)}) | base={score_base:.0f} | pen={score_pen:.0f}\n"
        + (f"• Median vig proxy: {_pct(vig_median)}\n" if vig_median is not None else "")
        + (f"• Odds dispersion (stdev): {_maybe(odds_sd)}\n" if odds_sd is not None else "")
    )

    flags_block = "🧩 **FLAGS**\n" + _flags_block(p)

    stake_block = (
        "💰 **STAKE**\n"
        f"• Stake: {pct_bk*100:.2f}% BK ({_fmt_money(stake)}) — {pct_day*100:.2f}% day budget\n"
        f"• Day budget: {_fmt_money(daily_budget)} | Spent after: {_fmt_money(spent_after)}"
    )

    return (
        "✅ **NBA PLAYER PROP**\n"
        f"Match: {match}\n"
        f"Market: {market}\n"
        f"Pick: {sel}\n"
        f"Best: {odds:.2f} ({book})\n\n"
        + value_metrics
        + "\n"
        + flags_block
        + "\n\n"
        + stake_block
        + ctx_block
        + clv
        + "\n\n_Props: 1 pick par joueur & 1 pick par match (si possible)._"
    )


def format_no_bet(
    title: str,
    reason: str,
    regions_used: List[str],
    games_analyzed: int,
    markets_tested: int,
    top_rejects: List[str],
    near_miss_lines: List[str],
    daily_budget: float,
    daily_spent: float,
) -> str:
    regions_txt = ", ".join([r for r in regions_used if r]) if regions_used else "n/a"
    rejects_block = "\n".join([f"• {x}" for x in top_rejects]) if top_rejects else "• (aucune donnée)"
    near_block = "\n".join(near_miss_lines) if near_miss_lines else "• Aucun near-miss."

    spend_ratio = (daily_spent / daily_budget) if daily_budget > 0 else 0.0
    spend_bar = _bar(spend_ratio, 0.0, 1.0, 18)

    return (
        f"❌ **{title}**\n"
        f"Reason: {reason}\n\n"
        "🧾 **RUN SUMMARY**\n"
        f"• Regions: {regions_txt}\n"
        f"• Games analyzed: {games_analyzed}\n"
        f"• Markets tested: {markets_tested}\n\n"
        "📦 **MAIN REJECTS**\n"
        f"{rejects_block}\n\n"
        "🎯 **NEAR MISSES (TOP 5)**\n"
        f"{near_block}\n\n"
        "💰 **BUDGET**\n"
        f"Day budget: {_fmt_money(daily_budget)} | Spent: {_fmt_money(daily_spent)} ({_pct(spend_ratio)}) {spend_bar}"
    )
