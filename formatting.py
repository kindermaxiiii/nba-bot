from typing import Any, Dict, List, Optional

def _pct(x: float) -> str:
    return f"{x*100:.2f}%"

def _fmt_money(x: float) -> str:
    return f"{x:.2f}€"

def _maybe(v: Optional[float], fmt: str = "{:.2f}") -> str:
    if v is None:
        return "n/a"
    try:
        return fmt.format(float(v))
    except Exception:
        return "n/a"

def _bar(x: float, width: int = 18) -> str:
    # x in [0,100]
    x = max(0.0, min(100.0, float(x)))
    filled = int(round((x / 100.0) * width))
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
        lines.append(f"• {tag}: {odds:.2f} ({book}) @ {dt}" if odds else f"• {tag}: n/a @ {dt}")
    return "\n\n📈 **CLV snapshots**\n" + "\n".join(lines)

def _flags_block(p: Dict[str, Any]) -> str:
    flags = p.get("flags") or []
    tier = p.get("tier", "STRICT")
    s = f"🧩 **FLAGS**\n• Tier: **{tier}**"
    if flags:
        for f in flags[:6]:
            s += f"\n• {f}"
    return s

def _stats_block(p: Dict[str, Any]) -> str:
    # For props: optional hitrate block
    st = p.get("stat_context") or {}
    if not st:
        return ""
    lines = []
    if "last10_hit" in st:
        lines.append(f"• Last10 hit rate: **{st['last10_hit']}**")
    if "last5_hit" in st:
        lines.append(f"• Last5 hit rate: **{st['last5_hit']}**")
    if "avg10" in st:
        lines.append(f"• Avg (10): **{st['avg10']}**")
    if "n" in st:
        lines.append(f"• Sample: n={st['n']}")
    return "\n\n📊 **STATS QUICKCHECK**\n" + "\n".join(lines)

def format_team_pick(p: Dict[str, Any], stake: float, bankroll: float, daily_budget: float, spent_after: float) -> str:
    match = p.get("match", "")
    market = p.get("market", "TEAM")
    selection = p.get("selection", "")
    line = p.get("line")
    odds = float(p.get("odds", 0.0))
    book = p.get("book", "Unknown")

    median_odds = p.get("median_odds")
    books_used = p.get("books_used")
    total_books = p.get("total_books")

    fair_raw = float(p.get("fair_prob_raw", p.get("fair_prob", 0.0)))
    fair_adj = float(p.get("fair_prob", 0.0))
    edge_raw = float(p.get("edge_raw", p.get("edge", 0.0)))
    edge_adj = float(p.get("edge", 0.0))
    dev = float(p.get("dev", 0.0))
    ev = float(p.get("ev", fair_adj * odds - 1.0))
    score = float(p.get("score", 0.0))
    tier = p.get("tier", "STRICT")
    haircut = bool(p.get("haircut_applied", False))

    vig_med = p.get("vig_median")
    sd = p.get("odds_stdev")

    pct_bk = (stake / bankroll) if bankroll > 0 else 0.0
    pct_day = (stake / daily_budget) if daily_budget > 0 else 0.0

    injury_note = p.get("injury_note")
    minutes_note = p.get("minutes_note")

    ctx = []
    if injury_note:
        ctx.append(f"• Injuries: {injury_note}")
    if minutes_note:
        ctx.append(f"• Minutes proj.: {minutes_note}")

    ctx_block = ("\n\n🧠 **CONTEXT**\n" + "\n".join(ctx)) if ctx else ""

    quality = "✅" if score >= 80 else "⚠️"

    return (
        f"✅ **NBA TEAM BET**\n"
        f"**Match:** {match}\n"
        f"**Market:** {market}\n"
        + (f"**Line:** {line}\n" if line is not None else "")
        + f"**Pick:** **{selection}**\n"
        f"**Best:** {odds:.2f} (**{book}**)\n\n"
        f"📌 **VALUE METRICS**\n"
        f"• Books (median calc): **{books_used}** | Total books: **{total_books}** | Median odds: **{_maybe(median_odds)}**\n"
        f"• p_fair: **{_pct(fair_adj)}** (raw {_pct(fair_raw)})\n"
        f"• EV: **{_pct(ev)}**\n"
        f"• Edge: **{_pct(edge_adj)}** (raw {_pct(edge_raw)}) {'✂️ haircut' if haircut else ''}\n"
        f"• Dev vs median: **{_pct(dev)}** | σ_odds: {_maybe(sd)} | vig(median): {_maybe(vig_med)}\n"
        f"• Score: **{score:.0f}/100** {quality}  {_bar(score)}\n\n"
        f"{_flags_block(p)}\n\n"
        f"💰 **STAKE**\n"
        f"• Stake: **{pct_bk*100:.2f}% BK** ({_fmt_money(stake)}) — {pct_day*100:.2f}% day budget\n"
        f"• Day budget: {_fmt_money(daily_budget)} | Spent after: {_fmt_money(spent_after)}"
        + ctx_block
        + _clv_block(p)
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

    fair_raw = float(p.get("fair_prob_raw", p.get("fair_prob", 0.0)))
    fair_adj = float(p.get("fair_prob", 0.0))
    edge_raw = float(p.get("edge_raw", p.get("edge", 0.0)))
    edge_adj = float(p.get("edge", 0.0))
    dev = float(p.get("dev", 0.0))
    ev = float(p.get("ev", fair_adj * odds - 1.0))
    score = float(p.get("score", 0.0))
    tier = p.get("tier", "STRICT")
    haircut = bool(p.get("haircut_applied", False))

    median_odds = p.get("median_odds")
    books_used = p.get("books_used")
    total_books = p.get("total_books")

    injury_note = p.get("injury_note")
    minutes_note = p.get("minutes_note")

    pct_bk = (stake / bankroll) if bankroll > 0 else 0.0
    pct_day = (stake / daily_budget) if daily_budget > 0 else 0.0

    sel = f"{player} — {side} {line}" if line is not None else f"{player} — {side}"

    ctx = []
    if injury_note:
        ctx.append(f"• Injuries: {injury_note}")
    if minutes_note:
        ctx.append(f"• Minutes proj.: {minutes_note}")
    ctx_block = ("\n\n🧠 **CONTEXT**\n" + "\n".join(ctx)) if ctx else ""

    quality = "✅" if score >= 80 else "⚠️"

    return (
        f"✅ **NBA PLAYER PROP**\n"
        f"**Match:** {match}\n"
        f"**Market:** {market}\n"
        f"**Pick:** **{sel}**\n"
        f"**Best:** {odds:.2f} (**{book}**)\n\n"
        f"📌 **VALUE METRICS**\n"
        f"• Books (median calc): **{books_used}** | Total books: **{total_books}** | Median odds: **{_maybe(median_odds)}**\n"
        f"• p_fair: **{_pct(fair_adj)}** (raw {_pct(fair_raw)})\n"
        f"• EV: **{_pct(ev)}**\n"
        f"• Edge: **{_pct(edge_adj)}** (raw {_pct(edge_raw)}) {'✂️ haircut' if haircut else ''}\n"
        f"• Dev vs median: **{_pct(dev)}**\n"
        f"• Score: **{score:.0f}/100** {quality}  {_bar(score)}\n\n"
        f"{_flags_block(p)}\n\n"
        f"💰 **STAKE**\n"
        f"• Stake: **{pct_bk*100:.2f}% BK** ({_fmt_money(stake)}) — {pct_day*100:.2f}% day budget\n"
        f"• Day budget: {_fmt_money(daily_budget)} | Spent after: {_fmt_money(spent_after)}"
        + _stats_block(p)
        + ctx_block
        + _clv_block(p)
        + "\n\n_Props: 1 pick/player & 1 pick/match (as possible)._"
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
    rejects_block = "\n".join([f"• {x}" for x in top_rejects]) if top_rejects else "• (aucune donnée)"
    near_block = "\n".join(near_miss_lines) if near_miss_lines else "Aucun near-miss."
    regions_txt = ", ".join([r for r in regions_used if r]) if regions_used else "n/a"

    return (
        f"❌ **{title}**\n"
        f"Reason: {reason}\n\n"
        f"🧾 **RUN SUMMARY**\n"
        f"• Regions: {regions_txt}\n"
        f"• Games analyzed: {games_analyzed}\n"
        f"• Markets tested: {markets_tested}\n\n"
        f"📦 **MAIN REJECTS**\n{rejects_block}\n\n"
        f"🎯 **NEAR MISSES (TOP 5)**\n{near_block}\n\n"
        f"💰 **BUDGET**\n"
        f"Day budget: **{_fmt_money(daily_budget)}** | Spent: **{_fmt_money(daily_spent)}** ({(daily_spent/daily_budget*100 if daily_budget else 0):.2f}%)"
    )
