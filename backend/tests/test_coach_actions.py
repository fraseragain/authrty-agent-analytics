from __future__ import annotations

import json
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
def bare_conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db(c)
    return c


# ---------------------------------------------------------------------------
# create_agent
# ---------------------------------------------------------------------------


def test_create_agent_writes_known_template(tmp_path: Path) -> None:
    dest = coach_module.create_agent("bash-verifier", tmp_path)

    assert dest == tmp_path / "bash-verifier.md"
    content = dest.read_text(encoding="utf-8")
    assert "name: bash-verifier" in content
    assert "model: claude-haiku-4-5-20251001" in content


def test_create_agent_rejects_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        coach_module.create_agent("not-a-real-template", tmp_path)


def test_create_agent_overwrite_is_idempotent(tmp_path: Path) -> None:
    coach_module.create_agent("gh-context", tmp_path)
    dest = coach_module.create_agent("gh-context", tmp_path)

    assert dest.read_text(encoding="utf-8") == coach_module._AGENT_TEMPLATES["gh-context"]


# ---------------------------------------------------------------------------
# apply_claude_md_snippet
# ---------------------------------------------------------------------------


def test_apply_claude_md_snippet_creates_fresh_file(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"

    coach_module.apply_claude_md_snippet("deep-loop-sessions", "Add a bash-verifier subagent.", path)

    content = path.read_text(encoding="utf-8")
    assert "## Coach suggestions" in content
    assert "Add a bash-verifier subagent." in content
    assert "<!-- coach:deep-loop-sessions -->" in content


def test_apply_claude_md_snippet_appends_to_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text("# My global instructions\n\nBe terse.\n", encoding="utf-8")

    coach_module.apply_claude_md_snippet("finding-a", "Do the thing.", path)

    content = path.read_text(encoding="utf-8")
    assert "Be terse." in content
    assert "## Coach suggestions" in content
    assert "Do the thing." in content


def test_apply_claude_md_snippet_reapply_is_a_noop(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"

    coach_module.apply_claude_md_snippet("finding-a", "Do the thing.", path)
    first = path.read_text(encoding="utf-8")
    coach_module.apply_claude_md_snippet("finding-a", "Do the thing.", path)
    second = path.read_text(encoding="utf-8")

    assert first == second
    assert second.count("<!-- coach:finding-a -->") == 1


def test_apply_claude_md_snippet_preserves_other_findings(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"

    coach_module.apply_claude_md_snippet("finding-a", "Do thing A.", path)
    coach_module.apply_claude_md_snippet("finding-b", "Do thing B.", path)

    content = path.read_text(encoding="utf-8")
    assert "Do thing A." in content
    assert "Do thing B." in content
    assert content.count("## Coach suggestions") == 1


# ---------------------------------------------------------------------------
# apply_permission_allow
# ---------------------------------------------------------------------------


def test_apply_permission_allow_creates_fresh_settings(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"

    coach_module.apply_permission_allow("WebSearch", path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == ["WebSearch"]


def test_apply_permission_allow_preserves_unrelated_keys(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"theme": "dark", "permissions": {"allow": ["Bash"], "deny": ["Write"]}}),
        encoding="utf-8",
    )

    coach_module.apply_permission_allow("WebSearch", path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["permissions"]["deny"] == ["Write"]
    assert set(data["permissions"]["allow"]) == {"Bash", "WebSearch"}


def test_apply_permission_allow_reapply_is_a_noop(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"

    coach_module.apply_permission_allow("WebSearch", path)
    coach_module.apply_permission_allow("WebSearch", path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == ["WebSearch"]


# ---------------------------------------------------------------------------
# _build_actions: proving generality, not just today's three findings
# ---------------------------------------------------------------------------


def test_build_actions_always_offers_claude_md(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(coach_module, "claude_md_path", lambda: tmp_path / "CLAUDE.md")
    monkeypatch.setattr(coach_module, "agents_dir", lambda: tmp_path / "agents")

    actions = coach_module._build_actions(
        "some-finding", skill_key=None, recommendation="Do the evergreen thing.",
        title="Some finding", body="Some body text with no known tool names.",
    )

    kinds = [a.kind for a in actions]
    assert kinds == ["claude_md"]
    assert actions[0].applied is False


def test_build_actions_offers_create_agent_only_for_known_templates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(coach_module, "claude_md_path", lambda: tmp_path / "CLAUDE.md")
    monkeypatch.setattr(coach_module, "agents_dir", lambda: tmp_path / "agents")

    known = coach_module._build_actions(
        "loop-finding", skill_key="bash-verifier", recommendation="Add a bash-verifier subagent.",
        title="Deep loop sessions", body="body",
    )
    unknown = coach_module._build_actions(
        "future-finding", skill_key="some-future-skill-not-yet-templated",
        recommendation="Some future fix.", title="Future finding", body="body",
    )

    assert any(a.kind == "create_agent" and a.key == "bash-verifier" for a in known)
    assert not any(a.kind == "create_agent" for a in unknown)


def test_build_actions_offers_permission_allow_from_finding_text_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No per-finding rule wires this up -- it fires purely because the
    finding's own text happens to mention a known permission-gated tool."""
    monkeypatch.setattr(coach_module, "claude_md_path", lambda: tmp_path / "CLAUDE.md")
    monkeypatch.setattr(coach_module, "agents_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr(coach_module, "claude_settings_path", lambda: tmp_path / "settings.json")

    actions = coach_module._build_actions(
        "websearch-retries", skill_key=None, recommendation="Pre-authorise WebSearch to avoid repeated prompts.",
        title="WebSearch permission retries", body="Claude kept retrying WebSearch after it was denied.",
    )

    perm_actions = [a for a in actions if a.kind == "permission_allow"]
    assert len(perm_actions) == 1
    assert perm_actions[0].key == "WebSearch"


def test_build_actions_reflects_applied_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    md_path = tmp_path / "CLAUDE.md"
    agents_path = tmp_path / "agents"
    monkeypatch.setattr(coach_module, "claude_md_path", lambda: md_path)
    monkeypatch.setattr(coach_module, "agents_dir", lambda: agents_path)

    coach_module.apply_claude_md_snippet("loop-finding", "Add a bash-verifier subagent.", md_path)
    coach_module.create_agent("bash-verifier", agents_path)

    actions = coach_module._build_actions(
        "loop-finding", skill_key="bash-verifier", recommendation="Add a bash-verifier subagent.",
        title="Deep loop sessions", body="body",
    )

    claude_md_action = next(a for a in actions if a.kind == "claude_md")
    agent_action = next(a for a in actions if a.kind == "create_agent")
    assert claude_md_action.applied is True
    assert agent_action.applied is True


# ---------------------------------------------------------------------------
# apply_action dispatcher
# ---------------------------------------------------------------------------


def test_apply_action_claude_md_requires_recommendation() -> None:
    with pytest.raises(ValueError):
        coach_module.apply_action("claude_md", "some-key")


def test_apply_action_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        coach_module.apply_action("hooks", "some-key")


def test_apply_action_rejects_unknown_create_agent_key() -> None:
    with pytest.raises(ValueError):
        coach_module.apply_action("create_agent", "not-a-template")


def test_apply_action_rejects_unknown_permission_tool() -> None:
    with pytest.raises(ValueError):
        coach_module.apply_action("permission_allow", "NotARealTool")


# ---------------------------------------------------------------------------
# applied-action markers (avoidable-spend chart impact tracking)
# ---------------------------------------------------------------------------


def test_log_applied_action_inserts_a_row(bare_conn: sqlite3.Connection) -> None:
    applied_at = coach_module._log_applied_action(
        bare_conn, kind="create_agent", key="bash-verifier", label="Create bash-verifier agent"
    )

    row = bare_conn.execute("SELECT kind, key, label, applied_at FROM coach_applied_actions").fetchone()
    assert row["kind"] == "create_agent"
    assert row["key"] == "bash-verifier"
    assert row["label"] == "Create bash-verifier agent"
    assert row["applied_at"] == applied_at


def test_load_action_markers_buckets_by_day(bare_conn: sqlite3.Connection) -> None:
    coach_module._log_applied_action(bare_conn, kind="claude_md", key="finding-a", label="Add to CLAUDE.md")
    bare_conn.execute(
        "UPDATE coach_applied_actions SET applied_at = '2026-06-15T14:32:00+00:00' WHERE key = 'finding-a'"
    )
    bare_conn.commit()

    markers = coach_module._load_action_markers(bare_conn, start_date="2026-06-01", end_date="2026-06-30")

    assert len(markers) == 1
    assert markers[0].week == "2026-06-15"
    assert markers[0].applied_at == "2026-06-15T14:32:00+00:00"
    assert markers[0].label == "Add to CLAUDE.md"


def test_load_action_markers_excludes_outside_window(bare_conn: sqlite3.Connection) -> None:
    coach_module._log_applied_action(bare_conn, kind="claude_md", key="old", label="Add to CLAUDE.md")
    bare_conn.execute("UPDATE coach_applied_actions SET applied_at = '2026-01-01T00:00:00+00:00' WHERE key = 'old'")
    bare_conn.commit()

    markers = coach_module._load_action_markers(bare_conn, start_date="2026-06-01", end_date="2026-06-30")

    assert markers == []


def test_apply_action_with_conn_logs_a_marker(bare_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(coach_module, "claude_md_path", lambda: tmp_path / "CLAUDE.md")

    coach_module.apply_action("claude_md", "deep-loop-sessions", recommendation="Do the thing.", conn=bare_conn)

    row = bare_conn.execute("SELECT kind, key, label FROM coach_applied_actions").fetchone()
    assert row["kind"] == "claude_md"
    assert row["key"] == "deep-loop-sessions"
    # No finding_title supplied -- falls back to the generic action label.
    assert row["label"] == "Add to CLAUDE.md"


def test_apply_action_with_finding_title_logs_a_descriptive_marker_label(
    bare_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(coach_module, "claude_md_path", lambda: tmp_path / "CLAUDE.md")

    coach_module.apply_action(
        "claude_md", "deep-loop-sessions", recommendation="Do the thing.",
        finding_title="Deep loop sessions", conn=bare_conn,
    )

    row = bare_conn.execute("SELECT label FROM coach_applied_actions").fetchone()
    assert row["label"] == "Deep loop sessions"


def test_marker_label_ignores_finding_title_for_non_claude_md_kinds(
    bare_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(coach_module, "agents_dir", lambda: tmp_path / "agents")

    coach_module.apply_action("create_agent", "bash-verifier", finding_title="Deep loop sessions", conn=bare_conn)

    row = bare_conn.execute("SELECT label FROM coach_applied_actions").fetchone()
    assert row["label"] == "Create bash-verifier agent"


# ---------------------------------------------------------------------------
# POST /api/coach/actions/{kind}/{key}
# ---------------------------------------------------------------------------


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
    monkeypatch.setattr(coach_module, "claude_md_path", lambda: tmp_path / "claude-home" / "CLAUDE.md")
    monkeypatch.setattr(coach_module, "agents_dir", lambda: tmp_path / "claude-home" / "agents")
    monkeypatch.setattr(coach_module, "claude_settings_path", lambda: tmp_path / "claude-home" / "settings.json")
    # Deliberately different from the write path above, to prove host_path
    # comes from claude_home_display() and not from the actual I/O path.
    monkeypatch.setattr(coach_module, "claude_home_display", lambda: Path("/host/.claude"))
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
        c.claude_home = tmp_path / "claude-home"  # type: ignore[attr-defined]
        yield c
    conn.close()


def test_post_coach_action_create_agent(client: TestClient) -> None:
    resp = client.post("/api/coach/actions/create_agent/bash-verifier")

    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["host_path"] == "/host/.claude/agents/bash-verifier.md"
    assert (client.claude_home / "agents" / "bash-verifier.md").exists()  # type: ignore[attr-defined]


def test_post_coach_action_claude_md(client: TestClient) -> None:
    resp = client.post(
        "/api/coach/actions/claude_md/deep-loop-sessions",
        json={"recommendation": "Add a bash-verifier subagent."},
    )

    assert resp.status_code == 200
    assert resp.json()["host_path"] == "/host/.claude/CLAUDE.md"
    content = (client.claude_home / "CLAUDE.md").read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert "Add a bash-verifier subagent." in content


def test_post_coach_action_claude_md_without_recommendation_is_400(client: TestClient) -> None:
    resp = client.post("/api/coach/actions/claude_md/deep-loop-sessions")
    assert resp.status_code == 400


def test_post_coach_action_permission_allow(client: TestClient) -> None:
    resp = client.post("/api/coach/actions/permission_allow/WebSearch")

    assert resp.status_code == 200
    assert resp.json()["host_path"] == "/host/.claude/settings.json"
    data = json.loads((client.claude_home / "settings.json").read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert "WebSearch" in data["permissions"]["allow"]


def test_post_coach_action_unknown_kind_is_400(client: TestClient) -> None:
    resp = client.post("/api/coach/actions/hooks/some-key")
    assert resp.status_code == 400


def test_post_coach_action_then_avoidable_shows_a_marker(client: TestClient) -> None:
    resp = client.post("/api/coach/actions/create_agent/bash-verifier")
    assert resp.status_code == 200

    avoidable = client.get("/api/coach/avoidable")
    assert avoidable.status_code == 200
    markers = avoidable.json()["markers"]
    assert len(markers) == 1
    assert markers[0]["kind"] == "create_agent"
    assert markers[0]["key"] == "bash-verifier"
    assert markers[0]["label"] == "Create bash-verifier agent"
    assert markers[0]["week"] == markers[0]["applied_at"][:10]


def test_post_coach_action_claude_md_with_finding_title_shows_descriptive_marker(client: TestClient) -> None:
    resp = client.post(
        "/api/coach/actions/claude_md/deep-loop-sessions",
        json={"recommendation": "Add a bash-verifier subagent.", "finding_title": "Deep loop sessions"},
    )
    assert resp.status_code == 200

    avoidable = client.get("/api/coach/avoidable")
    markers = avoidable.json()["markers"]
    assert markers[0]["label"] == "Deep loop sessions"


def test_coach_patterns_findings_carry_actions(client: TestClient) -> None:
    conn = client.seed_conn  # type: ignore[attr-defined]
    project_id = int(conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0])
    ts = _now()
    _add_session(conn, project_id, "s1", ts, max_repeat=8)
    _add_session(conn, project_id, "s2", ts, max_repeat=9)
    conn.commit()

    resp = client.get("/api/coach/patterns")

    assert resp.status_code == 200
    finding = next(f for f in resp.json()["findings"] if f["title"] == "Deep loop sessions")
    kinds = {a["kind"] for a in finding["actions"]}
    assert "claude_md" in kinds
    assert "create_agent" in kinds
