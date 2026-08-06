"""Coach analytics: avoidable spend breakdown, pattern discovery, and fixes.

Every finding is display-only unless the user clicks its action button --
apply_* functions below only ever run when POST /api/coach/actions is hit.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ccfr.analysis.pricing import TokenBreakdown, cost_usd, load_price_timeline, match_price
from ccfr.api.schemas import (
    CoachAction,
    CoachActionImpact,
    CoachActionMarker,
    CoachActionResult,
    CoachAvoidableResponse,
    CoachFinding,
    CoachPatternsResponse,
    CoachSummary,
    CoachWeek,
)
from ccfr.config import (
    agents_dir,
    claude_home_display,
    claude_md_path,
    claude_settings_path,
    pricing_dir,
    pricing_path,
)


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

# Intervention-impact comparison window: how many days on either side of an
# applied action to sum for the before/after comparison, and the minimum
# number of real (in-range) days required on each side to trust the result.
_IMPACT_WINDOW_DAYS = 7
_MIN_IMPACT_DAYS = 3


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
    # Token counts don't depend on pricing being available, so they're tallied
    # unconditionally (unlike day_cost below, which needs a price match).
    day_tokens: dict[str, int] = defaultdict(int)

    for row in rows:
        spk = int(row["session_pk"])
        day = row["event_day"] or ""
        day_tokens[day] += sum(int(row[cat]) for cat in _CATEGORIES)
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
    for day, tokens in day_tokens.items():
        if day in week_map:
            week_map[day].total_tokens += tokens

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
    total_tokens = sum(w.total_tokens for w in week_list)
    avoidable_usd = sum(w.avoidable_usd for w in week_list)
    loop_waste_usd = sum(w.loop_waste_usd for w in week_list)
    error_retry_usd = sum(w.error_retry_usd for w in week_list)
    alerted_total = sum(w.alerted_usd for w in week_list)
    model_overkill_usd = sum(w.model_overkill_usd for w in week_list)

    avoidable_pct = (avoidable_usd / total_usd * 100) if total_usd > 0 else 0.0

    # vs_prev tracks the *waste rate* (avoidable %), not absolute spend --
    # total spend naturally grows with usage, so a $ comparison mostly just
    # reflects "did I use it more", not "did I get more efficient". Expressed
    # as a percentage-point delta in avoidable_pct between the first and
    # second half of the window.
    vs_prev = None
    if len(week_list) >= 4:
        mid = len(week_list) // 2
        first_total = sum(w.total_usd for w in week_list[:mid])
        second_total = sum(w.total_usd for w in week_list[mid:])
        first_avoidable = sum(w.avoidable_usd for w in week_list[:mid])
        second_avoidable = sum(w.avoidable_usd for w in week_list[mid:])
        if first_total > 0:
            first_pct = first_avoidable / first_total * 100
            second_pct = (second_avoidable / second_total * 100) if second_total > 0 else 0.0
            vs_prev = second_pct - first_pct

    summary = CoachSummary(
        total_usd=total_usd,
        total_tokens=total_tokens,
        avoidable_usd=avoidable_usd,
        avoidable_pct=avoidable_pct,
        loop_waste_usd=loop_waste_usd,
        error_retry_usd=error_retry_usd,
        alerted_usd=alerted_total,
        model_overkill_usd=model_overkill_usd,
        vs_prev_period_pct=vs_prev,
    )

    markers = _load_action_markers(conn, start_date=start_date, end_date=today_str, day_map=week_map)

    return CoachAvoidableResponse(weeks=week_list, summary=summary, markers=markers)


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


# ---------------------------------------------------------------------------
# Apply-fix actions: agent files, CLAUDE.md snippets, permission allow-list
# entries. Every write here only happens when the user clicks a button --
# _build_actions() below only ever *offers* an action, never applies one.
# ---------------------------------------------------------------------------

_AGENT_TEMPLATES: dict[str, str] = {
    "bash-verifier": """\
---
name: bash-verifier
description: Verify bash commands to break repetition loops. Use when the same Bash command keeps failing or repeating.
tools: [Bash]
model: claude-haiku-4-5-20251001
---

You are a bash verification specialist. Given a shell command that has been failing or repeating:

1. Analyse the command syntax and common failure modes (wrong path, missing quotes, incorrect flags)
2. Run the command and check the output
3. Return a clear PASS/FAIL verdict with one sentence of explanation

Be concise. PASS or FAIL, then the fix if needed.
""",
    "research-agent": """\
---
name: research-agent
description: Cost-efficient web research using Haiku. Delegate any task where >75% of the work is WebSearch.
tools: [WebSearch, WebFetch]
model: claude-haiku-4-5-20251001
---

You are a focused web research agent. Given a research question:

1. Search for relevant information with WebSearch
2. Fetch key pages if detail is needed
3. Return a concise, factual summary with sources

Return only what was asked. Be brief.
""",
    "gh-context": """\
---
name: gh-context
description: Pre-fetch GitHub issue/PR context to avoid Pro plan and rate-limit errors. Run at session start for any GitHub task.
tools: [Bash]
model: claude-haiku-4-5-20251001
---

You are a GitHub context fetcher. Given a GitHub issue URL, PR number, or repo reference:

1. Use `gh` CLI to fetch issue/PR details (title, body, labels, status, recent comments)
2. Return structured context the main session can use directly
3. Flag any access errors clearly (rate limit, auth, Pro plan requirement)

Return clean structured context, not raw JSON.
""",
    "session-bootstrap": """\
---
name: session-bootstrap
description: Gather recurring session-start context once via a cheap Haiku agent. Use at the start of sessions that always fetch the same project context.
tools: [Bash, Read]
model: claude-haiku-4-5-20251001
---

You are a session initialisation agent. Quickly gather project context:

1. Current git branch and last 3 commits
2. Contents of CLAUDE.md if present
3. Any .env.example or README headlines

Return a compact bullet-point context summary. Be brief.
""",
    "verify-agent": """\
---
name: verify-agent
description: Independent post-implementation verification. Use after code changes to verify correctness without contaminating the main context.
tools: [Bash, Read]
model: claude-haiku-4-5-20251001
---

You are an independent verification specialist. Given a description of code changes:

1. Read the changed files
2. Run tests or linting if available
3. Check for obvious errors, regressions, or missing edge cases
4. Return PASS or FAIL with specifics

Be thorough. Only flag real issues, not style preferences.
""",
}

# Tool names Coach knows how to pre-authorise. A finding offers the
# permission_allow action whenever one of these appears in its own title,
# body, or recommendation text -- not a per-finding rule.
_KNOWN_PERMISSION_TOOLS = ["WebSearch", "WebFetch"]

_CLAUDE_MD_HEADING = "## Coach suggestions"


def _slug(title: str) -> str:
    """Stable key for CLAUDE.md markers -- also used as the frontend list key."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "finding"


def _write_text_atomic(path: Path, content: str) -> None:
    """Write via temp-file-then-rename so a mid-write crash can't corrupt the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def create_agent(key: str, agents_dir_path: Path) -> Path:
    """Write the agent template file for the given skill key. Returns the created path."""
    template = _AGENT_TEMPLATES.get(key)
    if template is None:
        raise KeyError(f"Unknown skill key: {key}")
    dest = agents_dir_path / f"{key}.md"
    _write_text_atomic(dest, template)
    return dest


def _claude_md_marker(key: str) -> str:
    return f"<!-- coach:{key} -->"


def _claude_md_marker_present(key: str, path: Path | None = None) -> bool:
    md_path = path if path is not None else claude_md_path()
    if not md_path.exists():
        return False
    return _claude_md_marker(key) in md_path.read_text(encoding="utf-8")


def apply_claude_md_snippet(key: str, recommendation: str, path: Path | None = None) -> None:
    """Append `recommendation` under a '## Coach suggestions' heading, once.

    Idempotent: a `<!-- coach:<key> -->` marker records that this finding's
    suggestion has already been applied, so re-clicking is a no-op.
    """
    md_path = path if path is not None else claude_md_path()
    marker = _claude_md_marker(key)
    existing = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    if marker in existing:
        return

    snippet = f"- {recommendation} {marker}\n"
    if _CLAUDE_MD_HEADING in existing:
        content = existing.rstrip("\n") + "\n" + snippet
    else:
        separator = "\n\n" if existing and not existing.endswith("\n\n") else ""
        if existing and not existing.endswith("\n"):
            separator = "\n" + separator
        content = existing + separator + f"{_CLAUDE_MD_HEADING}\n\n{snippet}"
    _write_text_atomic(md_path, content)


def _tool_in_allow_list(tool: str, path: Path | None = None) -> bool:
    settings_path = path if path is not None else claude_settings_path()
    if not settings_path.exists():
        return False
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    allow = data.get("permissions", {}).get("allow", [])
    return tool in allow


def apply_permission_allow(tool: str, path: Path | None = None) -> None:
    """Add `tool` to permissions.allow, preserving every other key. Idempotent."""
    settings_path = path if path is not None else claude_settings_path()
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    if not isinstance(data, dict):
        data = {}

    permissions = data.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        permissions = {}
        data["permissions"] = permissions
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        allow = []
        permissions["allow"] = allow

    if tool not in allow:
        allow.append(tool)
        _write_text_atomic(settings_path, json.dumps(data, indent=2) + "\n")


def _action_label(kind: str, key: str) -> str:
    """Single source for an action's button label, keyed only off kind+key.

    Shared by _build_actions() (what the button says) and, as a fallback,
    apply_action()'s marker log. create_agent/permission_allow labels are
    already specific (the agent/tool name is in the key); claude_md's isn't
    -- every claude_md button says the same mechanical thing regardless of
    finding, which is fine on a card (the finding's own title is right there)
    but useless on a detached chart marker. See _marker_label() for the fix.
    """
    if kind == "claude_md":
        return "Add to CLAUDE.md"
    if kind == "create_agent":
        return f"Create {key} agent"
    if kind == "permission_allow":
        return f"Allow {key} without prompting"
    return f"{kind}: {key}"


def _marker_label(kind: str, key: str, *, finding_title: str | None) -> str:
    """Label for the applied-action marker log -- descriptive on its own,
    since the chart pill has no surrounding card to supply context. The
    finding's own title carries the meaning; a "CLAUDE.md:" prefix would
    just repeat the mechanism, not the actual fix."""
    if kind == "claude_md" and finding_title:
        return finding_title
    return _action_label(kind, key)


def _build_actions(
    finding_key: str,
    *,
    skill_key: str | None,
    recommendation: str,
    title: str,
    body: str,
) -> list[CoachAction]:
    """Derive available fix actions from data every finding already carries.

    No per-finding-title table: every finding gets the CLAUDE.md action
    (its recommendation is already written as evergreen advice), create_agent
    appears only when skill_key names a template we actually have, and
    permission_allow appears only when the finding's own text names a known
    permission-gated tool.
    """
    actions = [
        CoachAction(
            kind="claude_md",
            key=finding_key,
            label=_action_label("claude_md", finding_key),
            applied=_claude_md_marker_present(finding_key),
        ),
    ]

    if skill_key and skill_key in _AGENT_TEMPLATES:
        actions.append(CoachAction(
            kind="create_agent",
            key=skill_key,
            label=_action_label("create_agent", skill_key),
            applied=(agents_dir() / f"{skill_key}.md").exists(),
        ))

    haystack = f"{title} {body} {recommendation}".lower()
    for tool in _KNOWN_PERMISSION_TOOLS:
        if tool.lower() in haystack:
            actions.append(CoachAction(
                kind="permission_allow",
                key=tool,
                label=_action_label("permission_allow", tool),
                applied=_tool_in_allow_list(tool),
            ))

    return actions


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
        title = pat["title"]
        body = pat["body"].format(sessions=session_count, occurrences=occurrence_count)
        findings.append(CoachFinding(
            title=title,
            body=body,
            recommendation=pat["recommendation"],
            metric=f"{occurrence_count}× across {session_count} sessions",
            severity=pat["severity"],
            session_count=session_count,
            occurrence_count=occurrence_count,
            skill_key=pat.get("skill_key"),
            source="heuristic",
            last_seen_at=last_seen,
            actions=_build_actions(
                _slug(title),
                skill_key=pat.get("skill_key"),
                recommendation=pat["recommendation"],
                title=title,
                body=body,
            ),
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
            title = "Error concentration"
            body = (
                f"Just {top_n} session{'s' if top_n > 1 else ''} account for {pct}% "
                f"of all {total_errors} tool errors. "
                f"These sessions had {running} errors between them — "
                f"the rest of your sessions are relatively clean."
            )
            recommendation = "Investigate these specific sessions to find the root cause. A single fix could eliminate most of your error retry cost."
            findings.append(CoachFinding(
                title=title,
                body=body,
                recommendation=recommendation,
                metric=f"{top_n} sessions · {pct}% of errors",
                severity="warning",
                session_count=top_n,
                occurrence_count=running,
                source="heuristic",
                last_seen_at=last_seen,
                actions=_build_actions(
                    _slug(title), skill_key=None, recommendation=recommendation, title=title, body=body,
                ),
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
        title = "Deep loop sessions"
        body = (
            f"{n} sessions repeated the same tool call 8 or more times consecutively. "
            f"These are the primary driver of loop waste — each run past the first is a wasted token spend."
        )
        recommendation = "A bash-verifier subagent that checks the output after each Bash call would break these loops early."
        findings.append(CoachFinding(
            title=title,
            body=body,
            recommendation=recommendation,
            metric=f"{n} sessions",
            severity="warning",
            session_count=n,
            skill_key="bash-verifier",
            source="heuristic",
            last_seen_at=deep["last_seen"],
            actions=_build_actions(
                _slug(title), skill_key="bash-verifier", recommendation=recommendation, title=title, body=body,
            ),
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
            title = "Project loop hotspot"
            body = (
                f'"{_proj_name(worst["cwd"])}" has a loop rate of {worst_rate*100:.1f}% '
                f'across {int(worst["session_count"])} sessions — '
                f'{worst_rate / max(best_rate, 0.001):.1f}× higher than '
                f'"{_proj_name(best["cwd"])}" ({best_rate*100:.1f}%). '
                f"This project is driving a disproportionate share of loop waste."
            )
            recommendation = "Look for a repeating pattern specific to this project — a failing test, a tool that always needs retrying, or a prompt that reliably causes loops."
            findings.append(CoachFinding(
                title=title,
                body=body,
                recommendation=recommendation,
                metric=f'{_proj_name(worst["cwd"])} · {worst_rate*100:.1f}% loop rate',
                severity="info",
                session_count=int(worst["session_count"]),
                skill_key="bash-verifier",
                source="heuristic",
                last_seen_at=worst["last_seen"],
                actions=_build_actions(
                    _slug(title), skill_key="bash-verifier", recommendation=recommendation, title=title, body=body,
                ),
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
        raw_title = row["title"] or "Recurring finding"
        title = f"Recurring finding: {category.title()} · {raw_title}"
        body = (
            f"This risk finding appears in {session_count} sessions "
            f"({occurrence_count} times) over the selected window."
        )
        recommendation = "Review these sessions for a shared root cause — a flaky test, a tool that regularly needs retrying, or a prompt pattern that reliably triggers this."
        findings.append(CoachFinding(
            title=title,
            body=body,
            recommendation=recommendation,
            metric=f"{session_count} sessions",
            severity="warning" if int(row["severity_rank"]) >= 2 else "info",
            session_count=session_count,
            occurrence_count=occurrence_count,
            source="risk_patterns",
            last_seen_at=row["last_seen"],
            actions=_build_actions(
                _slug(title), skill_key=None, recommendation=recommendation, title=title, body=body,
            ),
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
        title = f"Repeated workflow: {label}"
        body = f"This exact sequence of tool calls appears in {len(session_ids)} different sessions."
        recommendation = "Consider a skill or agent that automates this sequence end-to-end."
        findings.append(CoachFinding(
            title=title,
            body=body,
            recommendation=recommendation,
            metric=f"{len(session_ids)} sessions",
            severity="info",
            session_count=len(session_ids),
            occurrence_count=len(session_ids),
            source="workflow",
            last_seen_at=last_seen,
            actions=_build_actions(
                _slug(title), skill_key=None, recommendation=recommendation, title=title, body=body,
            ),
        ))
        if len(findings) >= limit:
            break
    return findings


def _log_applied_action(conn: sqlite3.Connection, *, kind: str, key: str, label: str) -> str:
    """Record that an action was applied, for the avoidable-spend chart's markers.

    Returns the applied_at timestamp. Never touches ~/.claude -- this is
    purely the app's own history of when *it* made a change, so the chart
    can show impact over time.
    """
    applied_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO coach_applied_actions (kind, key, label, applied_at) VALUES (?, ?, ?, ?)",
        (kind, key, label, applied_at),
    )
    conn.commit()
    return applied_at


def _action_impact(
    marker_date: str,
    day_map: dict[str, CoachWeek],
    *,
    start_date: str,
    end_date: str,
    window_days: int = _IMPACT_WINDOW_DAYS,
) -> CoachActionImpact:
    """Compare the `window_days` days before `marker_date` to the `window_days`
    days after it. The applied-on day itself is excluded from both sides --
    it's a mixed pre/post signal, not cleanly one or the other. Both windows
    are clamped to [start_date, end_date] so this never reads data outside the
    chart's own range or past "now". `sufficient_data` is False when either
    side has fewer than _MIN_IMPACT_DAYS of real days available -- e.g. the
    action was applied too recently, or too close to the window start.
    """
    try:
        marker = date.fromisoformat(marker_date)
        bound_start = date.fromisoformat(start_date)
        bound_end = date.fromisoformat(end_date)
    except ValueError:
        return CoachActionImpact(window_days=window_days, before_days=0, after_days=0, sufficient_data=False)

    before_days = [
        d for d in (marker - timedelta(days=n) for n in range(1, window_days + 1))
        if bound_start <= d <= bound_end
    ]
    after_days = [
        d for d in (marker + timedelta(days=n) for n in range(1, window_days + 1))
        if bound_start <= d <= bound_end
    ]

    def _sum(days: list[date], attr: str) -> float:
        return sum(getattr(day_map[d.isoformat()], attr) for d in days if d.isoformat() in day_map)

    before_total = _sum(before_days, "total_usd")
    after_total = _sum(after_days, "total_usd")
    before_avoidable = _sum(before_days, "avoidable_usd")
    after_avoidable = _sum(after_days, "avoidable_usd")

    return CoachActionImpact(
        window_days=window_days,
        before_days=len(before_days),
        after_days=len(after_days),
        sufficient_data=len(before_days) >= _MIN_IMPACT_DAYS and len(after_days) >= _MIN_IMPACT_DAYS,
        before_total_usd=before_total,
        after_total_usd=after_total,
        before_avoidable_usd=before_avoidable,
        after_avoidable_usd=after_avoidable,
        before_avoidable_pct=(before_avoidable / before_total * 100) if before_total > 0 else 0.0,
        after_avoidable_pct=(after_avoidable / after_total * 100) if after_total > 0 else 0.0,
    )


def _load_action_markers(
    conn: sqlite3.Connection, *, start_date: str, end_date: str, day_map: dict[str, CoachWeek],
) -> list[CoachActionMarker]:
    """Applied-action history within [start_date, end_date], bucketed to the
    day for alignment with CoachWeek.week (avoidable_spend is daily-bucketed
    despite the name -- see its docstring). Each marker also carries a
    before/after impact comparison (_action_impact) so the UI can show
    whether the fix actually helped, using `day_map` (the same per-day totals
    avoidable_spend() already built -- no extra query needed)."""
    rows = conn.execute(
        """
        SELECT kind, key, label, applied_at
        FROM coach_applied_actions
        WHERE date(applied_at) >= ? AND date(applied_at) <= ?
        ORDER BY applied_at
        """,
        (start_date, end_date),
    ).fetchall()

    marker_dates = [date.fromisoformat(row["applied_at"][:10]) for row in rows]

    markers: list[CoachActionMarker] = []
    for i, row in enumerate(rows):
        marker_day = row["applied_at"][:10]
        impact = _action_impact(marker_day, day_map, start_date=start_date, end_date=end_date)
        # Confounded when another applied action within this same loaded
        # window lands within `window_days` of this one -- a deliberate
        # simplification (only markers inside [start_date, end_date] are
        # considered), not a rigorous causal analysis.
        impact.confounded = any(
            j != i and abs((marker_dates[i] - marker_dates[j]).days) < impact.window_days
            for j in range(len(marker_dates))
        )
        markers.append(CoachActionMarker(
            kind=row["kind"],
            key=row["key"],
            label=row["label"],
            applied_at=row["applied_at"],
            week=marker_day,
            impact=impact,
        ))
    return markers


def apply_action(
    kind: str,
    key: str,
    *,
    recommendation: str | None = None,
    finding_title: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> CoachActionResult:
    """Dispatch a POST /api/coach/actions/{kind}/{key} request to the right apply_*.

    `recommendation` is only required for kind="claude_md" -- the route has no
    other way to recover the finding's recommendation text, since actions are
    derived on the fly rather than looked up from a stored table. `finding_title`
    only affects the marker log's label (see _marker_label()). `conn`, when
    given, logs the application for the avoidable-spend chart's markers.
    """
    if kind == "claude_md":
        if not recommendation:
            raise ValueError("recommendation is required for claude_md actions")
        apply_claude_md_snippet(key, recommendation)
        host_path = str(claude_home_display() / "CLAUDE.md")
        detail = f"Added to {claude_md_path()}"
    elif kind == "create_agent":
        if key not in _AGENT_TEMPLATES:
            raise ValueError(f"Unknown agent template: {key}")
        dest = create_agent(key, agents_dir())
        host_path = str(claude_home_display() / "agents" / f"{key}.md")
        detail = f"Created {dest}"
    elif kind == "permission_allow":
        if key not in _KNOWN_PERMISSION_TOOLS:
            raise ValueError(f"Unknown permission tool: {key}")
        apply_permission_allow(key)
        host_path = str(claude_home_display() / "settings.json")
        detail = f"Added {key} to permissions.allow"
    else:
        raise ValueError(f"Unknown action kind: {kind}")

    if conn is not None:
        _log_applied_action(conn, kind=kind, key=key, label=_marker_label(kind, key, finding_title=finding_title))
    return CoachActionResult(applied=True, detail=detail, host_path=host_path)


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
