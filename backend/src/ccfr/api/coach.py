"""Coach analytics: avoidable spend breakdown.

Phase 1 of the Coach page -- tracking only. Pattern-driven skill
recommendations and skill-file generation land in a later phase.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from ccfr.analysis.pricing import TokenBreakdown, cost_usd, load_price_timeline, match_price
from ccfr.api.schemas import CoachAvoidableResponse, CoachSummary, CoachWeek
from ccfr.config import pricing_dir, pricing_path


_CATEGORY_COLUMNS = {
    "base_input": "base_input_tokens",
    "cache_write_5m": "cache_5m_tokens",
    "cache_write_1h": "cache_1h_tokens",
    "cache_read": "cache_read_tokens",
    "output": "output_tokens",
}
_CATEGORIES = tuple(_CATEGORY_COLUMNS)

_LOOP_REPEAT_THRESHOLD = 5
_HAIKU_MODEL = "claude-haiku-4-5-20251001"


def _day_label(date_key: str) -> str:
    """Convert 'YYYY-MM-DD' to a short display label like '9 Jun'."""
    try:
        dt = datetime.strptime(date_key, "%Y-%m-%d")
        return dt.strftime("%-d %b")
    except (ValueError, AttributeError):
        return date_key


def _fill_days(start_date: str, end_date: str) -> list[str]:
    """Return list of ISO date strings from start_date to end_date inclusive."""
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        return []
    result = []
    cur = start
    while cur <= end:
        result.append(cur.isoformat())
        cur += timedelta(days=1)
    return result


def _first_session_date(conn: sqlite3.Connection) -> str | None:
    """Return the date of the earliest session as 'YYYY-MM-DD', or None."""
    row = conn.execute("SELECT date(MIN(first_ts)) FROM sessions").fetchone()
    return row[0] if row and row[0] else None


def _error_retry_counts(conn: sqlite3.Connection, cutoff: str) -> dict[int, int]:
    """Count error tool results per session as a proxy for retries."""
    rows = conn.execute(
        """
        SELECT tr.session_id, COUNT(*) AS error_count
        FROM tool_results tr
        JOIN sessions s ON s.id = tr.session_id
        WHERE tr.is_error = 1 AND s.first_ts >= ?
        GROUP BY tr.session_id
        """,
        (cutoff,),
    ).fetchall()
    return {int(r["session_id"]): int(r["error_count"]) for r in rows}


def avoidable_spend(
    conn: sqlite3.Connection,
    *,
    weeks: int = 12,
    historical: bool = False,
) -> CoachAvoidableResponse:
    """Daily spend split into efficient vs. loop waste, error retries,
    risk-flagged sessions, and model overkill (Sonnet/Opus subagent work
    that would have sufficed on Haiku).

    Despite the CoachWeek naming (kept for continuity with the recovered
    design), buckets are daily, not ISO weeks -- _fill_days walks every day
    in the window so the chart has smooth daily granularity.
    """
    timeline = load_price_timeline(pricing_path(), pricing_dir())

    period_expr = timeline.sql_period_expr("e.timestamp", historical=historical) if timeline.has_prices else "'none'"
    col_exprs = ", ".join(
        f"COALESCE(SUM(m.{col}),0) AS {cat}" for cat, col in _CATEGORY_COLUMNS.items()
    )

    today = datetime.now(timezone.utc).date()
    today_str = today.isoformat()
    earliest_session_date = _first_session_date(conn) or today_str
    # Bound the window to `weeks` back from today -- without this, the range
    # always started at the first session ever, growing unbounded (and the
    # chart with it) as history accumulates.
    window_start = (today - timedelta(weeks=weeks)).isoformat()
    start_date = max(earliest_session_date, window_start)
    cutoff = f"{start_date}T00:00:00"

    rows = conn.execute(
        f"""
        SELECT
            s.id AS session_pk,
            date(e.timestamp) AS event_day,
            ss.loop_count,
            ss.max_repeat,
            ss.tool_call_count,
            m.model,
            ({period_expr}) AS price_period,
            {col_exprs}
        FROM sessions s
        JOIN session_stats ss ON ss.session_id = s.id
        JOIN events e ON e.session_id = s.id
        JOIN messages m ON m.event_id = e.id
        WHERE s.first_ts >= ?
        GROUP BY s.id, date(e.timestamp), m.model, price_period
        """,
        (cutoff,),
    ).fetchall()

    # Sessions with at least one risk finding stand in for "alerted sessions"
    # (the old design's session_alerts table doesn't exist in this schema --
    # risk_findings is the direct, already-populated equivalent).
    alerted_sessions: set[int] = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT rf.session_id FROM risk_findings rf "
            "JOIN sessions s ON s.id = rf.session_id WHERE s.first_ts >= ?",
            (cutoff,),
        ).fetchall()
    }

    # Accumulate per-(session, day) cost, keeping session-level waste metrics separate.
    session_day_costs: dict[tuple[int, str], float] = {}
    session_meta: dict[int, dict] = {}

    for row in rows:
        spk = int(row["session_pk"])
        day = row["event_day"] or ""
        if spk not in session_meta:
            session_meta[spk] = {
                "loop_count": int(row["loop_count"] or 0),
                "max_repeat": int(row["max_repeat"] or 0),
                "tool_call_count": int(row["tool_call_count"] or 0),
                "alerted": spk in alerted_sessions,
            }
        if timeline.has_prices:
            price = match_price(
                timeline.table_for_period(row["price_period"], historical=historical),
                row["model"],
            )
            if price:
                breakdown = TokenBreakdown(**{cat: int(row[cat]) for cat in _CATEGORIES})
                day_cost = cost_usd(price, breakdown)
                key = (spk, day)
                session_day_costs[key] = session_day_costs.get(key, 0.0) + day_cost

    # Per-session total cost (needed to distribute waste proportionally across days).
    session_total_cost: dict[int, float] = {}
    for (spk, _), cost in session_day_costs.items():
        session_total_cost[spk] = session_total_cost.get(spk, 0.0) + cost

    error_retry_counts = _error_retry_counts(conn, cutoff)

    day_keys = _fill_days(start_date, today_str)
    week_map: dict[str, CoachWeek] = {
        k: CoachWeek(
            week=k,
            week_label=_day_label(k),
            is_partial=(k == today_str),
            partial_days=7,  # Never project daily data -- partial_days=7 is a no-op in the chart
        )
        for k in day_keys
    }
    # Track which sessions contributed to each day to avoid double-counting session_count.
    day_sessions: dict[str, set[int]] = {k: set() for k in day_keys}

    for (spk, day), day_cost in session_day_costs.items():
        if day not in week_map:
            continue
        meta = session_meta[spk]
        session_total = session_total_cost.get(spk, 0.0)
        # Fraction of this session's total cost that falls on this day.
        frac = (day_cost / session_total) if session_total > 0 else 1.0

        tool_count = max(meta["tool_call_count"], 1)
        loop_count = meta["loop_count"]
        max_repeat = meta["max_repeat"]

        loop_waste = frac * (loop_count / tool_count) * session_total if max_repeat >= _LOOP_REPEAT_THRESHOLD else 0.0
        retry_count = error_retry_counts.get(spk, 0)
        error_retry = frac * (retry_count / tool_count) * session_total
        alerted = frac * (0.25 * session_total) if meta["alerted"] else 0.0

        avoidable = loop_waste + error_retry + alerted
        efficient = max(day_cost - avoidable, 0.0)

        wk_obj = week_map[day]
        wk_obj.total_usd += day_cost
        wk_obj.loop_waste_usd += loop_waste
        wk_obj.error_retry_usd += error_retry
        wk_obj.alerted_usd += alerted
        wk_obj.avoidable_usd += avoidable
        wk_obj.efficient_usd += efficient
        day_sessions[day].add(spk)

    for day, spk_set in day_sessions.items():
        if day in week_map:
            week_map[day].session_count = len(spk_set)

    # Model overkill: subagent sessions on Sonnet/Opus where Haiku would have sufficed.
    # Cost differential = actual_cost(model) - counterfactual_cost(haiku).
    sa_period_expr = timeline.sql_period_expr("e.timestamp", historical=historical) if timeline.has_prices else "'none'"
    sa_col_exprs = ", ".join(
        f"COALESCE(SUM(m.{col}),0) AS {cat}" for cat, col in _CATEGORY_COLUMNS.items()
    )
    sa_rows = conn.execute(
        f"""
        SELECT
            date(e.timestamp) AS event_day,
            m.model,
            ({sa_period_expr}) AS price_period,
            {sa_col_exprs}
        FROM subagents sa
        JOIN events e ON e.agent_id = sa.agent_id
        JOIN messages m ON m.event_id = e.id
        JOIN sessions s ON s.id = sa.parent_session_id
        WHERE m.model IS NOT NULL AND m.model != ''
          AND m.model NOT LIKE '%haiku%'
          AND s.first_ts >= ?
        GROUP BY sa.id, date(e.timestamp), m.model, price_period
        """,
        (cutoff,),
    ).fetchall()

    if timeline.has_prices:
        for row in sa_rows:
            wk = row["event_day"] or ""
            if wk not in week_map:
                continue
            period_table = timeline.table_for_period(row["price_period"], historical=historical)
            actual_price = match_price(period_table, row["model"])
            haiku_price = match_price(period_table, _HAIKU_MODEL)
            if actual_price is None or haiku_price is None:
                continue
            breakdown = TokenBreakdown(**{cat: int(row[cat]) for cat in _CATEGORIES})
            overkill = max(cost_usd(actual_price, breakdown) - cost_usd(haiku_price, breakdown), 0.0)
            if overkill > 0:
                wk_obj = week_map[wk]
                wk_obj.model_overkill_usd += overkill
                wk_obj.avoidable_usd += overkill
                wk_obj.efficient_usd = max(wk_obj.efficient_usd - overkill, 0.0)

    week_list = list(week_map.values())

    total_usd = sum(w.total_usd for w in week_list)
    avoidable_usd = sum(w.avoidable_usd for w in week_list)
    loop_waste_usd = sum(w.loop_waste_usd for w in week_list)
    error_retry_usd = sum(w.error_retry_usd for w in week_list)
    alerted_total = sum(w.alerted_usd for w in week_list)
    model_overkill_usd = sum(w.model_overkill_usd for w in week_list)

    avoidable_pct = (avoidable_usd / total_usd * 100) if total_usd > 0 else 0.0

    vs_prev = None
    if len(week_list) >= 4:
        mid = len(week_list) // 2
        first_half = sum(w.total_usd for w in week_list[:mid])
        second_half = sum(w.total_usd for w in week_list[mid:])
        if first_half > 0:
            vs_prev = (second_half - first_half) / first_half * 100

    summary = CoachSummary(
        total_usd=total_usd,
        avoidable_usd=avoidable_usd,
        avoidable_pct=avoidable_pct,
        loop_waste_usd=loop_waste_usd,
        error_retry_usd=error_retry_usd,
        alerted_usd=alerted_total,
        model_overkill_usd=model_overkill_usd,
        vs_prev_period_pct=vs_prev,
    )

    return CoachAvoidableResponse(weeks=week_list, summary=summary)
