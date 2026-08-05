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
