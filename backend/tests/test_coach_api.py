from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ccfr.api import coach as coach_module
from ccfr.api.deps import get_db
from ccfr.main import create_app
from ccfr.storage import init_db
from tests.test_coach import _add_message, _add_session, _now


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    csv = tmp_path / "pricing.csv"
    csv.write_text(
        "model,base-input-tokens,5m-cache-writes,1h-cache-writes,cache-hits-&-refreshes,output-tokens\n"
        "claude-sonnet-4-6,3,3.75,6,0.30,15\n"
        "claude-haiku-4-5-20251001,0.25,0.3125,0.5,0.025,1.25\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(coach_module, "pricing_path", lambda: csv)
    monkeypatch.setattr(coach_module, "pricing_dir", lambda: tmp_path / "no-sheets")
    monkeypatch.setattr("ccfr.main.database_path", lambda: tmp_path / "startup.sqlite3")

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    import_id = conn.execute(
        "INSERT INTO imports (source_path, imported_at, file_count, status, error_count)"
        " VALUES ('fx', '2026-01-01T00:00:00Z', 0, 'complete', 0)"
    ).lastrowid
    conn.execute(
        "INSERT INTO projects (import_id, export_name, inferred_cwd) VALUES (?, 'alpha', NULL)",
        (import_id,),
    )
    conn.commit()

    app = create_app()
    app.dependency_overrides[get_db] = lambda: conn
    with TestClient(app) as c:
        c.seed_conn = conn  # type: ignore[attr-defined]
        yield c
    conn.close()


def test_coach_avoidable_endpoint_returns_summary_and_weeks(client: TestClient) -> None:
    conn = client.seed_conn  # type: ignore[attr-defined]
    project_id = int(conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0])
    ts = _now()
    session = _add_session(conn, project_id, "s1", ts, tool_call_count=4)
    _add_message(conn, session, ts, "claude-sonnet-4-6", 1_000_000, 1_000_000)
    conn.commit()

    resp = client.get("/api/coach/avoidable")

    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["total_usd"] > 0
    assert len(body["weeks"]) >= 1


def test_coach_avoidable_endpoint_empty_corpus(client: TestClient) -> None:
    resp = client.get("/api/coach/avoidable")

    assert resp.status_code == 200
    assert resp.json()["summary"]["total_usd"] == 0.0


def test_coach_avoidable_honors_weeks_param(client: TestClient) -> None:
    resp = client.get("/api/coach/avoidable", params={"weeks": 1})
    assert resp.status_code == 200

    resp_bad = client.get("/api/coach/avoidable", params={"weeks": 0})
    assert resp_bad.status_code == 422


def test_coach_patterns_endpoint_returns_findings(client: TestClient) -> None:
    conn = client.seed_conn  # type: ignore[attr-defined]
    project_id = int(conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0])
    ts = _now()
    _add_session(conn, project_id, "s1", ts, max_repeat=8)
    _add_session(conn, project_id, "s2", ts, max_repeat=9)
    conn.commit()

    resp = client.get("/api/coach/patterns")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_session_count"] == 2
    assert any(f["title"] == "Deep loop sessions" for f in body["findings"])


def test_coach_patterns_endpoint_empty_corpus(client: TestClient) -> None:
    resp = client.get("/api/coach/patterns")

    assert resp.status_code == 200
    assert resp.json()["findings"] == []
