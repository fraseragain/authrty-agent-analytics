"""Coach analytics: avoidable spend breakdown and pattern discovery.

Skill-file generation (writing real files to a skills/agents directory) is a
later phase with its own scope and sign-off -- findings here are display-only.
"""
from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from ccfr.analysis.pricing import TokenBreakdown, cost_usd, load_price_timeline, match_price
from ccfr.api.schemas import (
    CoachAvoidableResponse,
    CoachFinding,
    CoachPatternsResponse,
    CoachSummary,
    CoachWeek,
)
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


# ---------------------------------------------------------------------------
# Pattern discovery: three independent sources, concatenated (not re-ranked
# against each other -- session_count/score/occurrence_count aren't
# comparable across sources).
# ---------------------------------------------------------------------------

_ERROR_PATTERNS = [
    {
        "match": "File has not been read yet",
        "title": "Edit-before-read errors",
        "body": "Claude attempted to edit or write files without reading them first in {sessions} sessions ({occurrences} times). This is a consistent behaviour — each instance causes an immediate tool error.",
        "recommendation": "Add 'always Read a file before editing it' to CLAUDE.md. A PostToolUse hook on Edit/Write could enforce this automatically.",
        "severity": "warning",
        "skill_key": None,
    },
    {
        "match": "Upgrade to GitHub Pro",
        "title": "GitHub private repo access failures",
        "body": "The GitHub MCP returned 'Upgrade to GitHub Pro' in {sessions} sessions ({occurrences} times). It can't access private repositories without proper authentication.",
        "recommendation": "Use `gh auth login` and the gh CLI instead of the GitHub MCP for private repos. Never paste a PAT into conversation — add this to CLAUDE.md.",
        "severity": "warning",
        "skill_key": "gh-context",
    },
    {
        "match": "WebFetch has been denied",
        "title": "WebFetch permission retries",
        "body": "Claude kept retrying WebFetch after it was denied — {occurrences} times across {sessions} sessions. Each retry is a wasted tool call that counts toward error retry cost.",
        "recommendation": "Pre-authorise WebFetch in your permission settings, or instruct Claude to stop after a denial rather than retry.",
        "severity": "warning",
        "skill_key": None,
    },
    {
        "match": "WebSearch has been denied",
        "title": "WebSearch permission retries",
        "body": "Claude kept retrying WebSearch after it was denied — {occurrences} times across {sessions} sessions.",
        "recommendation": "Pre-authorise WebSearch or set it as always-allowed to avoid repeated prompts.",
        "severity": "warning",
        "skill_key": "research-agent",
    },
    {
        "match": "doesn't want to proceed",
        "title": "User-rejected tool calls",
        "body": "You rejected tool permission prompts {occurrences} times across {sessions} sessions. Each rejection costs a round-trip and interrupts the session.",
        "recommendation": "Review which tools you regularly allow and add them to the pre-authorised list to reduce interruptions.",
        "severity": "info",
        "skill_key": None,
    },
    {
        "match": "File does not exist",
        "title": "Phantom file references",
        "body": "Claude referenced file paths that don't exist in {sessions} sessions ({occurrences} times). This usually signals hallucinated or stale paths.",
        "recommendation": "Add 'verify paths with ls or test -e before referencing them' to CLAUDE.md.",
        "severity": "warning",
        "skill_key": None,
    },
    {
        "match": "failed to resolve git reference",
        "title": "Git/MCP reference failures",
        "body": "Git or MCP failed to resolve repository references in {sessions} sessions ({occurrences} times).",
        "recommendation": "Check MCP git configuration — the MCP may be pointing to a repo it can't reach from the container.",
        "severity": "info",
        "skill_key": None,
    },
    {
        "match": "Tool permission stream closed",
        "title": "Permission stream timeouts",
        "body": "Tool permission requests timed out before getting a response in {sessions} sessions ({occurrences} times).",
        "recommendation": "Check for connectivity issues with the permission service, or pre-authorise frequently used tools.",
        "severity": "warning",
        "skill_key": None,
    },
]


def _heuristic_findings(conn: sqlite3.Connection, cutoff: str) -> list[CoachFinding]:
    """Error-signature matching plus coarse session/project heuristics.

    Ported from the old (deleted, dangling-commit-recoverable) Efficiency
    dashboard branch's pattern_analysis(). Unlike the other two sources, this
    one works in text/count space, not sequence or risk-score space.
    """
    findings: list[CoachFinding] = []

    # --- error pattern clustering: known error signatures in tool_results ---
    err_preview_rows = conn.execute(
        """
        SELECT tr.output_preview,
               COUNT(DISTINCT tr.session_id) AS session_count,
               COUNT(*) AS occurrence_count,
               MAX(COALESCE(s.last_ts, s.first_ts)) AS last_seen
        FROM tool_results tr
        JOIN sessions s ON s.id = tr.session_id
        WHERE tr.is_error = 1
          AND tr.output_preview IS NOT NULL
          AND length(tr.output_preview) > 5
          AND s.first_ts >= ?
        GROUP BY tr.output_preview
        """,
        (cutoff,),
    ).fetchall()

    for pat in _ERROR_PATTERNS:
        rows = [r for r in err_preview_rows if pat["match"] in (r["output_preview"] or "")]
        if not rows:
            continue
        session_count = sum(r["session_count"] for r in rows)
        occurrence_count = sum(r["occurrence_count"] for r in rows)
        if session_count < 2:
            continue
        last_seen = max((r["last_seen"] for r in rows if r["last_seen"]), default=None)
        findings.append(CoachFinding(
            title=pat["title"],
            body=pat["body"].format(sessions=session_count, occurrences=occurrence_count),
            recommendation=pat["recommendation"],
            metric=f"{occurrence_count}× across {session_count} sessions",
            severity=pat["severity"],
            session_count=session_count,
            occurrence_count=occurrence_count,
            skill_key=pat.get("skill_key"),
            source="heuristic",
            last_seen_at=last_seen,
        ))

    findings.sort(key=lambda f: f.occurrence_count, reverse=True)

    # --- error concentration: a small minority of sessions holding most errors ---
    error_rows = conn.execute(
        """
        SELECT tr.session_id, COUNT(*) AS err_count, MAX(COALESCE(s.last_ts, s.first_ts)) AS last_seen
        FROM tool_results tr
        JOIN sessions s ON s.id = tr.session_id
        WHERE tr.is_error = 1 AND s.first_ts >= ?
        GROUP BY tr.session_id
        ORDER BY err_count DESC
        """,
        (cutoff,),
    ).fetchall()

    if error_rows:
        total_errors = sum(r["err_count"] for r in error_rows)
        running = 0
        top_n = 0
        top_rows: list[sqlite3.Row] = []
        for r in error_rows:
            running += r["err_count"]
            top_n += 1
            top_rows.append(r)
            if running / total_errors >= 0.8:
                break
        # Only surface if a small minority of sessions dominates.
        if top_n <= max(1, len(error_rows) // 3) and top_n <= 5:
            pct = int(running / total_errors * 100)
            last_seen = max((r["last_seen"] for r in top_rows if r["last_seen"]), default=None)
            findings.append(CoachFinding(
                title="Error concentration",
                body=(
                    f"Just {top_n} session{'s' if top_n > 1 else ''} account for {pct}% "
                    f"of all {total_errors} tool errors. "
                    f"These sessions had {running} errors between them — "
                    f"the rest of your sessions are relatively clean."
                ),
                recommendation="Investigate these specific sessions to find the root cause. A single fix could eliminate most of your error retry cost.",
                metric=f"{top_n} sessions · {pct}% of errors",
                severity="warning",
                session_count=top_n,
                occurrence_count=running,
                source="heuristic",
                last_seen_at=last_seen,
            ))

    # --- deep loop sessions ---
    deep = conn.execute(
        """
        SELECT COUNT(*) AS n, MAX(COALESCE(s.last_ts, s.first_ts)) AS last_seen
        FROM session_stats ss
        JOIN sessions s ON s.id = ss.session_id
        WHERE ss.max_repeat >= 8 AND s.first_ts >= ?
        """,
        (cutoff,),
    ).fetchone()

    if deep and int(deep["n"]) >= 2:
        n = int(deep["n"])
        findings.append(CoachFinding(
            title="Deep loop sessions",
            body=(
                f"{n} sessions repeated the same tool call 8 or more times consecutively. "
                f"These are the primary driver of loop waste — each run past the first is a wasted token spend."
            ),
            recommendation="A bash-verifier subagent that checks the output after each Bash call would break these loops early.",
            metric=f"{n} sessions",
            severity="warning",
            session_count=n,
            skill_key="bash-verifier",
            source="heuristic",
            last_seen_at=deep["last_seen"],
        ))

    # --- project efficiency hotspot ---
    proj_rows = conn.execute(
        """
        SELECT s.cwd,
               COUNT(DISTINCT s.id) AS session_count,
               SUM(ss.loop_count) AS total_loops,
               SUM(ss.tool_call_count) AS total_tools,
               MAX(COALESCE(s.last_ts, s.first_ts)) AS last_seen
        FROM sessions s
        JOIN session_stats ss ON ss.session_id = s.id
        WHERE s.first_ts >= ? AND s.cwd IS NOT NULL
        GROUP BY s.cwd
        HAVING session_count >= 2
        ORDER BY session_count DESC
        """,
        (cutoff,),
    ).fetchall()

    if len(proj_rows) >= 2:
        def _proj_name(cwd: str) -> str:
            return os.path.basename(cwd.rstrip("/")) or cwd

        def _loop_rate(r: sqlite3.Row) -> float:
            return r["total_loops"] / max(r["total_tools"], 1)

        ranked = sorted(proj_rows, key=_loop_rate, reverse=True)
        worst = ranked[0]
        best = ranked[-1]
        worst_rate = _loop_rate(worst)
        best_rate = _loop_rate(best)

        if worst_rate > best_rate * 2 and worst_rate > 0.02:
            findings.append(CoachFinding(
                title="Project loop hotspot",
                body=(
                    f'"{_proj_name(worst["cwd"])}" has a loop rate of {worst_rate*100:.1f}% '
                    f'across {int(worst["session_count"])} sessions — '
                    f'{worst_rate / max(best_rate, 0.001):.1f}× higher than '
                    f'"{_proj_name(best["cwd"])}" ({best_rate*100:.1f}%). '
                    f"This project is driving a disproportionate share of loop waste."
                ),
                recommendation="Look for a repeating pattern specific to this project — a failing test, a tool that always needs retrying, or a prompt that reliably causes loops.",
                metric=f'{_proj_name(worst["cwd"])} · {worst_rate*100:.1f}% loop rate',
                severity="info",
                session_count=int(worst["session_count"]),
                skill_key="bash-verifier",
                source="heuristic",
                last_seen_at=worst["last_seen"],
            ))

    return findings


def _risk_pattern_findings(conn: sqlite3.Connection, cutoff: str, *, limit: int = 5) -> list[CoachFinding]:
    """Corpus-wide recurring risk findings.

    Reuses analysis/risk_patterns.py's risk_findings table -- already
    populated on every import (rebuild_risk_patterns(), called from
    importer.py) and, until now, only ever queried per-session.
    """
    rows = conn.execute(
        """
        SELECT rf.category, rf.title,
               COUNT(DISTINCT rf.session_id) AS session_count,
               COUNT(*) AS occurrence_count,
               MAX(CASE rf.severity WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END) AS severity_rank,
               MAX(COALESCE(s.last_ts, s.first_ts)) AS last_seen
        FROM risk_findings rf
        JOIN sessions s ON s.id = rf.session_id
        WHERE s.first_ts >= ?
        GROUP BY rf.category, rf.title
        HAVING session_count >= 2
        ORDER BY session_count DESC, severity_rank DESC
        LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()

    findings: list[CoachFinding] = []
    for row in rows:
        session_count = int(row["session_count"])
        occurrence_count = int(row["occurrence_count"])
        category = (row["category"] or "risk").replace("_", " ")
        title = row["title"] or "Recurring finding"
        findings.append(CoachFinding(
            title=f"Recurring finding: {category.title()} · {title}",
            body=(
                f"This risk finding appears in {session_count} sessions "
                f"({occurrence_count} times) over the selected window."
            ),
            recommendation="Review these sessions for a shared root cause — a flaky test, a tool that regularly needs retrying, or a prompt pattern that reliably triggers this.",
            metric=f"{session_count} sessions",
            severity="warning" if int(row["severity_rank"]) >= 2 else "info",
            session_count=session_count,
            occurrence_count=occurrence_count,
            source="risk_patterns",
            last_seen_at=row["last_seen"],
        ))
    return findings


def _workflow_findings(
    conn: sqlite3.Connection,
    cutoff: str,
    *,
    n: int = 3,
    min_sessions: int = 3,
    limit: int = 3,
) -> list[CoachFinding]:
    """Tool-name n-grams that recur across multiple *different* sessions.

    None of the other two sources look for repeatable workflows -- the thing
    actually worth turning into a skill. Each session counts once per gram
    (deduped) so one busy session can't inflate a gram's session-count.
    """
    rows = conn.execute(
        """
        SELECT tc.session_id, tc.tool_name
        FROM tool_calls tc
        JOIN events e ON e.id = tc.event_id
        JOIN sessions s ON s.id = tc.session_id
        WHERE s.first_ts >= ? AND tc.tool_name IS NOT NULL
        ORDER BY tc.session_id, e.id
        """,
        (cutoff,),
    ).fetchall()

    session_sequences: dict[int, list[str]] = defaultdict(list)
    for row in rows:
        session_sequences[int(row["session_id"])].append(row["tool_name"])

    gram_sessions: dict[tuple[str, ...], set[int]] = defaultdict(set)
    for session_id, sequence in session_sequences.items():
        seen_this_session: set[tuple[str, ...]] = set()
        for i in range(len(sequence) - n + 1):
            gram = tuple(sequence[i:i + n])
            if gram in seen_this_session:
                continue
            seen_this_session.add(gram)
            gram_sessions[gram].add(session_id)

    ranked = sorted(gram_sessions.items(), key=lambda kv: len(kv[1]), reverse=True)

    findings: list[CoachFinding] = []
    for gram, session_ids in ranked:
        if len(session_ids) < min_sessions:
            break  # sorted descending, so nothing after this qualifies either
        label = " → ".join(gram)
        placeholders = ",".join("?" * len(session_ids))
        last_seen = conn.execute(
            f"SELECT MAX(COALESCE(last_ts, first_ts)) FROM sessions WHERE id IN ({placeholders})",
            tuple(session_ids),
        ).fetchone()[0]
        findings.append(CoachFinding(
            title=f"Repeated workflow: {label}",
            body=f"This exact sequence of tool calls appears in {len(session_ids)} different sessions.",
            recommendation="Consider a skill or agent that automates this sequence end-to-end.",
            metric=f"{len(session_ids)} sessions",
            severity="info",
            session_count=len(session_ids),
            occurrence_count=len(session_ids),
            source="workflow",
            last_seen_at=last_seen,
        ))
        if len(findings) >= limit:
            break
    return findings


def pattern_analysis(conn: sqlite3.Connection, *, weeks: int = 12) -> CoachPatternsResponse:
    cutoff = (datetime.now(timezone.utc) - timedelta(weeks=weeks)).isoformat()

    total_session_count = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE first_ts >= ?", (cutoff,)
    ).fetchone()[0]

    findings = [
        *_heuristic_findings(conn, cutoff),
        *_risk_pattern_findings(conn, cutoff),
        *_workflow_findings(conn, cutoff),
    ]

    return CoachPatternsResponse(
        findings=findings,
        total_session_count=total_session_count,
        weeks=weeks,
    )
