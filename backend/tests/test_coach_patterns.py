from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ccfr.api import coach as coach_module
from ccfr.api.coach import pattern_analysis
from ccfr.storage import init_db
from tests.test_coach import _add_session, _now, _project_id


def _add_tool_result(conn: sqlite3.Connection, session_id: int, ts: str, output_preview: str) -> None:
    ev = conn.execute(
        "INSERT INTO events (session_id, source_path, line_no, type, timestamp, raw_json)"
        " VALUES (?, 'f.jsonl', 1, 'assistant', ?, '{}')",
        (session_id, ts),
    ).lastrowid
    conn.execute(
        "INSERT INTO tool_results(event_id, session_id, tool_use_id, is_error, output_preview, raw_json)"
        " VALUES (?, ?, 'tool-1', 1, ?, '{}')",
        (ev, session_id, output_preview),
    )


def _add_risk_finding(conn: sqlite3.Connection, session_id: int, category: str, title: str, severity: str = "medium") -> None:
    conn.execute(
        "INSERT INTO risk_findings(session_id, severity, category, title, explanation, score)"
        " VALUES (?, ?, ?, ?, 'Fixture finding.', 2.5)",
        (session_id, severity, category, title),
    )


def _add_tool_call_sequence(conn: sqlite3.Connection, session_id: int, ts: str, tool_names: list[str]) -> None:
    for tool_name in tool_names:
        ev = conn.execute(
            "INSERT INTO events (session_id, source_path, line_no, type, timestamp, raw_json)"
            " VALUES (?, 'f.jsonl', 1, 'assistant', ?, '{}')",
            (session_id, ts),
        ).lastrowid
        conn.execute(
            "INSERT INTO tool_calls (event_id, session_id, tool_use_id, tool_name, raw_json)"
            " VALUES (?, ?, 'tool-1', ?, '{}')",
            (ev, session_id, tool_name),
        )


@pytest.fixture()
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setattr(coach_module, "pricing_path", lambda: tmp_path / "pricing.csv")
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


def test_empty_corpus_has_no_findings(conn: sqlite3.Connection) -> None:
    resp = pattern_analysis(conn, weeks=12)
    assert resp.findings == []
    assert resp.total_session_count == 0
    assert resp.weeks == 12


def test_risk_pattern_finding_for_recurring_category_title(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    ts = _now()
    s1 = _add_session(conn, pid, "s1", ts)
    s2 = _add_session(conn, pid, "s2", ts)
    _add_risk_finding(conn, s1, "loop", "Repeated expensive work")
    _add_risk_finding(conn, s2, "loop", "Repeated expensive work")
    conn.commit()

    resp = pattern_analysis(conn, weeks=12)

    risk_findings = [f for f in resp.findings if f.source == "risk_patterns"]
    assert len(risk_findings) == 1
    assert risk_findings[0].session_count == 2
    assert "Repeated expensive work" in risk_findings[0].title
    assert risk_findings[0].last_seen_at == ts


def test_risk_pattern_finding_requires_at_least_two_sessions(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    ts = _now()
    s1 = _add_session(conn, pid, "s1", ts)
    _add_risk_finding(conn, s1, "loop", "Repeated expensive work")
    conn.commit()

    resp = pattern_analysis(conn, weeks=12)

    assert [f for f in resp.findings if f.source == "risk_patterns"] == []


def test_heuristic_error_pattern_finding(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    ts = _now()
    s1 = _add_session(conn, pid, "s1", ts)
    s2 = _add_session(conn, pid, "s2", ts)
    _add_tool_result(conn, s1, ts, "Error: File has not been read yet. Read it first.")
    _add_tool_result(conn, s2, ts, "Error: File has not been read yet. Read it first.")
    conn.commit()

    resp = pattern_analysis(conn, weeks=12)

    heuristic = [f for f in resp.findings if f.source == "heuristic" and f.title == "Edit-before-read errors"]
    assert len(heuristic) == 1
    assert heuristic[0].session_count == 2
    assert heuristic[0].last_seen_at == ts


def test_heuristic_error_concentration_finding(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    ts = _now()
    s1 = _add_session(conn, pid, "s1", ts)
    s2 = _add_session(conn, pid, "s2", ts)
    s3 = _add_session(conn, pid, "s3", ts)
    for _ in range(20):
        _add_tool_result(conn, s1, ts, "some unrelated error text")
    _add_tool_result(conn, s2, ts, "some unrelated error text")
    _add_tool_result(conn, s3, ts, "some unrelated error text")
    conn.commit()

    resp = pattern_analysis(conn, weeks=12)

    concentration = [f for f in resp.findings if f.title == "Error concentration"]
    assert len(concentration) == 1
    assert concentration[0].session_count == 1


def test_heuristic_deep_loop_finding(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    ts = _now()
    _add_session(conn, pid, "s1", ts, max_repeat=8)
    _add_session(conn, pid, "s2", ts, max_repeat=9)
    conn.commit()

    resp = pattern_analysis(conn, weeks=12)

    deep_loop = [f for f in resp.findings if f.title == "Deep loop sessions"]
    assert len(deep_loop) == 1
    assert deep_loop[0].session_count == 2
    assert deep_loop[0].last_seen_at == ts


def test_heuristic_project_hotspot_finding(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    ts = _now()
    # Worst project: high loop rate (loop_count high relative to tool_call_count).
    for sid in ("bad1", "bad2"):
        conn.execute(
            "INSERT INTO sessions (project_id, session_id, title, first_ts, last_ts, cwd)"
            " VALUES (?, ?, 'Session', ?, ?, '/home/dev/bad-project')",
            (pid, sid, ts, ts),
        )
        session_pk = conn.execute("SELECT id FROM sessions WHERE session_id = ?", (sid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO session_stats(session_id, event_count, turn_count, tool_call_count, subagent_count,"
            " error_count, system_count, persisted_output_count, input_tokens, output_tokens, loop_count, max_repeat)"
            " VALUES (?, 1, 1, 10, 0, 0, 0, 0, 0, 0, 5, 3)",
            (session_pk,),
        )
    # Best project: no loops at all.
    for sid in ("good1", "good2"):
        conn.execute(
            "INSERT INTO sessions (project_id, session_id, title, first_ts, last_ts, cwd)"
            " VALUES (?, ?, 'Session', ?, ?, '/home/dev/good-project')",
            (pid, sid, ts, ts),
        )
        session_pk = conn.execute("SELECT id FROM sessions WHERE session_id = ?", (sid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO session_stats(session_id, event_count, turn_count, tool_call_count, subagent_count,"
            " error_count, system_count, persisted_output_count, input_tokens, output_tokens, loop_count, max_repeat)"
            " VALUES (?, 1, 1, 10, 0, 0, 0, 0, 0, 0, 0, 0)",
            (session_pk,),
        )
    conn.commit()

    resp = pattern_analysis(conn, weeks=12)

    hotspot = [f for f in resp.findings if f.title == "Project loop hotspot"]
    assert len(hotspot) == 1
    assert "bad-project" in hotspot[0].body


def test_workflow_finding_for_repeated_tool_sequence(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    ts = _now()
    for name in ("s1", "s2", "s3"):
        sid = _add_session(conn, pid, name, ts)
        _add_tool_call_sequence(conn, sid, ts, ["Read", "Bash", "Edit"])
    conn.commit()

    resp = pattern_analysis(conn, weeks=12)

    workflow = [f for f in resp.findings if f.source == "workflow"]
    assert len(workflow) == 1
    assert workflow[0].title == "Repeated workflow: Read → Bash → Edit"
    assert workflow[0].session_count == 3
    assert workflow[0].last_seen_at == ts


def test_workflow_finding_requires_the_minimum_session_count(conn: sqlite3.Connection) -> None:
    pid = _project_id(conn)
    ts = _now()
    for name in ("s1", "s2"):
        sid = _add_session(conn, pid, name, ts)
        _add_tool_call_sequence(conn, sid, ts, ["Read", "Bash", "Edit"])
    conn.commit()

    resp = pattern_analysis(conn, weeks=12)

    assert [f for f in resp.findings if f.source == "workflow"] == []
