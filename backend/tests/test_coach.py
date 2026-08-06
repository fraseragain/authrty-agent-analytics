from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ccfr.api import coach as coach_module
from ccfr.api.coach import avoidable_spend
from ccfr.storage import init_db


def _add_message(conn: sqlite3.Connection, session_id: int, ts: str, model: str, base: int, out: int) -> int:
    ev = conn.execute(
        "INSERT INTO events (session_id, source_path, line_no, type, timestamp, raw_json)"
        " VALUES (?, 'f.jsonl', 1, 'assistant', ?, '{}')",
        (session_id, ts),
    ).lastrowid
    conn.execute(
        "INSERT INTO messages (event_id, role, model, input_tokens, output_tokens,"
        " base_input_tokens, cache_5m_tokens, cache_1h_tokens, cache_read_tokens)"
        " VALUES (?, 'assistant', ?, ?, ?, ?, 0, 0, 0)",
        (ev, model, base + out, out, base),
    )
    return int(ev)


def _add_session(
    conn: sqlite3.Connection,
    project_id: int,
    session_id: str,
    ts: str,
    *,
    loop_count: int = 0,
    max_repeat: int = 0,
    tool_call_count: int = 1,
) -> int:
    s = conn.execute(
        "INSERT INTO sessions (project_id, session_id, title, first_ts, last_ts)"
        " VALUES (?, ?, 'Session', ?, ?)",
        (project_id, session_id, ts, ts),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO session_stats(
            session_id, event_count, turn_count, tool_call_count, subagent_count, error_count,
            system_count, persisted_output_count, input_tokens, output_tokens, loop_count, max_repeat
        ) VALUES (?, 1, 1, ?, 0, 0, 0, 0, 0, 0, ?, ?)
        """,
        (s, tool_call_count, loop_count, max_repeat),
    )
    return int(s)


@pytest.fixture()
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    csv = tmp_path / "pricing.csv"
    csv.write_text(
        "model,base-input-tokens,5m-cache-writes,1h-cache-writes,cache-hits-&-refreshes,output-tokens\n"
        "claude-sonnet-4-6,3,3.75,6,0.30,15\n"
        "claude-haiku-4-5-20251001,0.25,0.3125,0.5,0.025,1.25\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(coach_module, "pricing_path", lambda: csv)
    monkeypatch.setattr(coach_module, "pricing_dir", lambda: tmp_path / "no-sheets")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db(c)
    import_id = c.execute(
        "INSERT INTO imports (source_path, imported_at, file_count, status, error_count)"
        " VALUES ('fx', '2026-01-01T00:00:00Z', 0, 'complete', 0)"
    ).lastrowid
    c.execute(
        "INSERT INTO projects (import_id, export_name, inferred_cwd) VALUES (?, 'alpha', NULL)",
        (import_id,),
    )
    c.commit()
    return c


def _project_id(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def test_empty_corpus_returns_zeroes(conn: sqlite3.Connection) -> None:
    resp = avoidable_spend(conn, weeks=12)
    assert resp.summary.total_usd == 0.0
    assert resp.summary.avoidable_usd == 0.0
    assert resp.summary.avoidable_pct == 0.0
    assert len(resp.weeks) == 1  # no sessions -> window collapses to today only


def test_efficient_session_has_no_avoidable_spend(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    ts = _now()
    s = _add_session(conn, pid, "s1", ts, tool_call_count=4)
    _add_message(conn, s, ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    conn.commit()

    resp = avoidable_spend(conn, weeks=12)

    assert resp.summary.total_usd > 0
    assert resp.summary.avoidable_usd == 0.0
    assert resp.summary.loop_waste_usd == 0.0
    assert resp.summary.error_retry_usd == 0.0
    assert resp.summary.alerted_usd == 0.0


def test_loop_waste_requires_max_repeat_over_threshold(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    ts = _now()
    # max_repeat below the threshold (5): loop_count present but not counted.
    s = _add_session(conn, pid, "s1", ts, loop_count=2, max_repeat=4, tool_call_count=4)
    _add_message(conn, s, ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    conn.commit()
    resp = avoidable_spend(conn, weeks=12)
    assert resp.summary.loop_waste_usd == 0.0

    # max_repeat at/above the threshold: loop waste is now counted.
    conn.execute("UPDATE session_stats SET max_repeat = 5 WHERE session_id = ?", (s,))
    conn.commit()
    resp2 = avoidable_spend(conn, weeks=12)
    assert resp2.summary.loop_waste_usd > 0
    assert resp2.summary.avoidable_usd == pytest.approx(resp2.summary.loop_waste_usd)


def test_error_retries_counted_from_tool_results(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    ts = _now()
    s = _add_session(conn, pid, "s1", ts, tool_call_count=4)
    ev = _add_message(conn, s, ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    conn.execute(
        "INSERT INTO tool_results(event_id, session_id, tool_use_id, is_error, raw_json)"
        " VALUES (?, ?, 'tool-1', 1, '{}')",
        (ev, s),
    )
    conn.commit()

    resp = avoidable_spend(conn, weeks=12)

    assert resp.summary.error_retry_usd > 0
    assert resp.summary.avoidable_usd == pytest.approx(resp.summary.error_retry_usd)


def test_risk_finding_marks_session_alerted(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    ts = _now()
    s = _add_session(conn, pid, "s1", ts, tool_call_count=4)
    _add_message(conn, s, ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    conn.execute(
        "INSERT INTO risk_findings(session_id, severity, category, title, explanation, score)"
        " VALUES (?, 'medium', 'loop', 'Repeated expensive work', 'Fixture finding.', 2.5)",
        (s,),
    )
    conn.commit()

    resp = avoidable_spend(conn, weeks=12)

    # Alerted sessions contribute a flat 25% of session cost as avoidable.
    assert resp.summary.alerted_usd == pytest.approx(resp.summary.total_usd * 0.25, rel=1e-6)
    assert resp.summary.avoidable_usd == pytest.approx(resp.summary.alerted_usd)


def test_model_overkill_compares_subagent_cost_to_haiku(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    ts = _now()
    s = _add_session(conn, pid, "s1", ts, tool_call_count=1)
    ev = conn.execute(
        "INSERT INTO events (session_id, source_path, line_no, type, timestamp, raw_json, agent_id)"
        " VALUES (?, 'f.jsonl', 1, 'assistant', ?, '{}', 'agent-1')",
        (s, ts),
    ).lastrowid
    conn.execute(
        "INSERT INTO messages (event_id, role, model, input_tokens, output_tokens,"
        " base_input_tokens, cache_5m_tokens, cache_1h_tokens, cache_read_tokens)"
        " VALUES (?, 'assistant', 'claude-sonnet-4-6', 1000000, 1000000, 1000000, 0, 0, 0)",
        (ev,),
    )
    conn.execute(
        "INSERT INTO subagents (parent_session_id, agent_id, agent_type) VALUES (?, 'agent-1', 'general-purpose')",
        (s,),
    )
    conn.commit()

    resp = avoidable_spend(conn, weeks=12)

    # Sonnet input (3.0/M) vs Haiku input (0.25/M) on 1M tokens, plus output differential.
    assert resp.summary.model_overkill_usd > 0
    assert resp.summary.avoidable_usd == pytest.approx(resp.summary.model_overkill_usd)


def test_window_excludes_sessions_before_the_requested_weeks(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    old_ts = "2000-01-01T00:00:00Z"
    s = _add_session(conn, pid, "old", old_ts, tool_call_count=4)
    _add_message(conn, s, old_ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    conn.commit()

    resp = avoidable_spend(conn, weeks=1)

    assert resp.summary.total_usd == 0.0
    # ~1 week + 1 day of daily buckets, not the multi-decade span back to old_ts.
    assert len(resp.weeks) <= 9


def test_total_tokens_sums_across_sessions_and_days(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    ts = _now()
    s1 = _add_session(conn, pid, "s1", ts, tool_call_count=4)
    _add_message(conn, s1, ts, "claude-sonnet-4-6", 100, 50)
    s2 = _add_session(conn, pid, "s2", ts, tool_call_count=4)
    _add_message(conn, s2, ts, "claude-haiku-4-5-20251001", 200, 25)
    conn.commit()

    resp = avoidable_spend(conn, weeks=12)

    assert resp.summary.total_tokens == 100 + 50 + 200 + 25
    assert sum(w.total_tokens for w in resp.weeks) == resp.summary.total_tokens


def test_vs_prev_period_pct_tracks_waste_rate_not_absolute_spend(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    # Older half of the window: small spend, heavy loop waste -> high avoidable %.
    for n in range(4, 8):
        ts = _days_ago(n)
        s = _add_session(conn, pid, f"old-{n}", ts, loop_count=3, max_repeat=5, tool_call_count=4)
        _add_message(conn, s, ts, "claude-sonnet-4-6", 10_000, 10_000)
    # Recent half: much larger spend, but no loop waste -> ~0% avoidable.
    for n in range(0, 4):
        ts = _days_ago(n)
        s = _add_session(conn, pid, f"new-{n}", ts, tool_call_count=4)
        _add_message(conn, s, ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    conn.commit()

    resp = avoidable_spend(conn, weeks=12)

    assert len(resp.weeks) == 8
    first_total = sum(w.total_usd for w in resp.weeks[:4])
    second_total = sum(w.total_usd for w in resp.weeks[4:])
    # Absolute spend grew sharply in the second half...
    assert second_total > first_total * 5
    # ...but vs_prev_period_pct should show *improvement* (negative), because
    # it tracks the waste rate (avoidable %) rather than the $ amount -- the
    # old (pre-change) formula would have reported a large positive number here.
    assert resp.summary.vs_prev_period_pct is not None
    assert resp.summary.vs_prev_period_pct < 0


# ---------------------------------------------------------------------------
# intervention impact: before/after comparison attached to applied-action markers
# ---------------------------------------------------------------------------


def test_action_impact_shows_improvement_after_applying_fix(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    for n in range(8, 15):  # 7 days strictly before the marker: loop-wasteful sessions
        ts = _days_ago(n)
        s = _add_session(conn, pid, f"before-{n}", ts, loop_count=3, max_repeat=5, tool_call_count=4)
        _add_message(conn, s, ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    for n in range(0, 7):  # 7 days strictly after the marker: clean sessions
        ts = _days_ago(n)
        s = _add_session(conn, pid, f"after-{n}", ts, tool_call_count=4)
        _add_message(conn, s, ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    conn.execute(
        "INSERT INTO coach_applied_actions (kind, key, label, applied_at) VALUES (?, ?, ?, ?)",
        ("claude_md", "fix", "Fixed it", _days_ago(7)),
    )
    conn.commit()

    resp = avoidable_spend(conn, weeks=12)

    assert len(resp.markers) == 1
    impact = resp.markers[0].impact
    assert impact.sufficient_data is True
    assert impact.confounded is False
    assert impact.before_avoidable_pct > 0
    assert impact.after_avoidable_pct == 0.0
    assert impact.after_avoidable_pct < impact.before_avoidable_pct


def test_action_impact_insufficient_when_recently_applied(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    # An older session pushes the query's start_date back far enough that the
    # "before" side of the window is fully populated -- isolating the
    # insufficiency to the "after" side, which is what "recently applied"
    # should actually trip.
    old_ts = _days_ago(10)
    s_old = _add_session(conn, pid, "old", old_ts, tool_call_count=4)
    _add_message(conn, s_old, old_ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    ts = _now()
    s = _add_session(conn, pid, "today", ts, tool_call_count=4)
    _add_message(conn, s, ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    conn.execute(
        "INSERT INTO coach_applied_actions (kind, key, label, applied_at) VALUES (?, ?, ?, ?)",
        ("claude_md", "fix", "Fixed it", _days_ago(1)),
    )
    conn.commit()

    resp = avoidable_spend(conn, weeks=12)

    assert len(resp.markers) == 1
    impact = resp.markers[0].impact
    assert impact.before_days == 7
    assert impact.after_days == 1
    assert impact.sufficient_data is False


def test_action_impact_confounded_when_actions_land_close_together(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    # A session predating both markers so the query's start_date covers them
    # -- otherwise they'd fall outside [start_date, end_date] and never load.
    old_ts = _days_ago(6)
    s_old = _add_session(conn, pid, "old", old_ts, tool_call_count=4)
    _add_message(conn, s_old, old_ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    ts = _now()
    s = _add_session(conn, pid, "s1", ts, tool_call_count=4)
    _add_message(conn, s, ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    conn.execute(
        "INSERT INTO coach_applied_actions (kind, key, label, applied_at) VALUES (?, ?, ?, ?), (?, ?, ?, ?)",
        ("claude_md", "fix-a", "Fix A", _days_ago(5), "claude_md", "fix-b", "Fix B", _days_ago(3)),
    )
    conn.commit()

    resp = avoidable_spend(conn, weeks=12)

    assert len(resp.markers) == 2
    assert all(m.impact.confounded for m in resp.markers)


def test_action_impact_not_confounded_when_actions_are_far_apart(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    far_ts = _days_ago(60)
    s_far = _add_session(conn, pid, "far", far_ts, tool_call_count=4)
    _add_message(conn, s_far, far_ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    ts = _now()
    s = _add_session(conn, pid, "s1", ts, tool_call_count=4)
    _add_message(conn, s, ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    conn.execute(
        "INSERT INTO coach_applied_actions (kind, key, label, applied_at) VALUES (?, ?, ?, ?), (?, ?, ?, ?), (?, ?, ?, ?)",
        (
            "claude_md", "fix-a", "Fix A", _days_ago(5),
            "claude_md", "fix-b", "Fix B", _days_ago(3),
            "claude_md", "fix-c", "Fix C", far_ts,
        ),
    )
    conn.commit()

    resp = avoidable_spend(conn, weeks=12)

    by_key = {m.key: m for m in resp.markers}
    assert by_key["fix-a"].impact.confounded is True
    assert by_key["fix-b"].impact.confounded is True
    assert by_key["fix-c"].impact.confounded is False


def test_action_impact_clamped_to_query_window(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    ts = _now()
    s = _add_session(conn, pid, "s1", ts, tool_call_count=4)
    _add_message(conn, s, ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    conn.execute(
        "INSERT INTO coach_applied_actions (kind, key, label, applied_at) VALUES (?, ?, ?, ?)",
        ("claude_md", "fix", "Fixed it", _now()),
    )
    conn.commit()

    resp = avoidable_spend(conn, weeks=1)

    assert len(resp.markers) == 1
    impact = resp.markers[0].impact
    # The only session is today, so the query window collapses to a single
    # day -- neither side of the before/after comparison can reach past it.
    assert impact.before_days == 0
    assert impact.after_days == 0
    assert impact.sufficient_data is False
