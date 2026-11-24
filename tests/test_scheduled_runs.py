"""Batch 13f (2026-09-30): a --scheduled run never uses unfinished code and
never upgrades the database. Each test builds its own small git repository
under tmp_path; every value here is invented."""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess

import pytest

from conftest import seed_reference
from opplist import __main__ as cli
from opplist import db, paths, scheduled, schema_migrations
from opplist.web import create_app


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.test", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repository shaped like this one: the package, schema.sql, tests,
    docs, export-preview - all committed."""
    root = tmp_path / "code"
    for name, text in (("opplist/app.py", "VALUE = 1\n"), ("opplist/web/page.html", "<p>hi</p>\n"),
                       ("schema.sql", "-- schema\n"), ("tests/test_app.py", "def test(): pass\n"),
                       ("docs/notes.md", "notes\n"), ("export-preview/sample.html", "<p>sample</p>\n")):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    monkeypatch.setattr(scheduled, "REPO_ROOT", root)
    return root


@pytest.fixture
def live(tmp_path):
    path = tmp_path / "live.db"
    conn = db.open_db(path)
    seed_reference(db.Repository(conn, source="test"))
    conn.commit()
    conn.close()
    return path


def test_a_clean_repository_lets_the_run_go_ahead(repo, live):
    assert scheduled.uncommitted(repo) == []
    assert scheduled.guard(live, "propose-status") is None


@pytest.mark.parametrize("change", ["modify", "add_py", "schema"])
def test_a_code_change_skips_and_logs(repo, live, change, caplog):
    if change == "modify":
        (repo / "opplist" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    elif change == "add_py":
        (repo / "opplist" / "half_done.py").write_text("x = 1\n", encoding="utf-8")
    else:
        (repo / "schema.sql").write_text("-- schema v99\n", encoding="utf-8")
    with caplog.at_level(logging.INFO):
        code = scheduled.guard(live, "propose-status --open --write-suggestions")
    assert code == scheduled.EXIT_NOT_COMMITTED == 75
    assert any("scheduled run skipped: code not committed, 1 file" in r.getMessage() for r in caplog.records)
    run = scheduled.last_run()
    assert (run["state"], run["reason"], run["command"]) == (
        "skipped", "code not committed, 1 file", "propose-status --open --write-suggestions")


def test_tests_docs_samples_and_untracked_non_code_do_not_count(repo, live):
    (repo / "tests" / "test_app.py").write_text("def test(): assert True\n", encoding="utf-8")
    (repo / "tests" / "test_new.py").write_text("def test(): pass\n", encoding="utf-8")
    (repo / "docs" / "notes.md").write_text("more notes\n", encoding="utf-8")
    (repo / "export-preview" / "other.html").write_text("<p></p>\n", encoding="utf-8")
    (repo / "opplist" / "scratch.txt").write_text("not code\n", encoding="utf-8")
    assert scheduled.uncommitted(repo) == []
    assert scheduled.guard(live, "mail-sync") is None


def test_no_git_skips_the_same_way(repo, live, monkeypatch):
    monkeypatch.setattr(scheduled, "_git", lambda: None)
    assert scheduled.guard(live, "mail-sync") == 75
    assert scheduled.last_run()["reason"] == "git cannot be run: git not found"


def test_a_database_one_version_behind_stops_the_run_untouched(repo, live):
    conn = sqlite3.connect(str(live))
    conn.execute("UPDATE setting SET value = ? WHERE key = 'schema_version'",
                 (str(schema_migrations.CURRENT_VERSION - 1),))
    conn.commit()
    conn.close()
    before = live.read_bytes()
    code = cli.main(["propose-shipments", "--db", str(live), "--scheduled"])
    assert code == scheduled.EXIT_NEEDS_UPGRADE == 76
    assert live.read_bytes() == before                           # byte for byte: not opened for writing
    assert not (live.parent / "restore-points").exists()        # nothing tried to upgrade it
    want = schema_migrations.CURRENT_VERSION
    assert scheduled.last_run()["reason"] == \
        "the app has to upgrade the database first (v{} to v{})".format(want - 1, want)


def test_a_scheduled_run_that_goes_ahead_is_recorded_done(repo, live, capsys):
    assert cli.main(["check", "--db", str(live), "--scheduled"]) == 0
    run = scheduled.last_run()
    assert (run["state"], run["command"]) == ("done", "check --db {}".format(live))


def test_without_the_flag_nothing_changes(repo, live, monkeypatch, capsys):
    (repo / "opplist" / "app.py").write_text("VALUE = 3\n", encoding="utf-8")   # would skip a scheduled run
    ran = []
    monkeypatch.setattr(cli, "cmd_check", lambda args: ran.append(args.db) or 0)
    assert cli.main(["check", "--db", str(live)]) == 0
    assert ran == [str(live)] and scheduled.last_run() is None


@pytest.mark.parametrize("state,reason,shown", [
    ("done", "", "last scheduled run 10/07 08:15 done</span>"),
    ("skipped", "code not committed, 2 files", "last scheduled run 10/07 08:15 skipped: code not committed, 2 files"),
    ("skipped", "the app has to upgrade the database first (v34 to v35)",
     "last scheduled run 10/07 08:15 skipped: the app has to upgrade the database first (v34 to v35)"),
])
def test_the_tracking_line_shows_the_last_scheduled_run(live, state, reason, shown):
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (paths.DATA_DIR / scheduled.STATE_NAME).write_text(json.dumps(
        {"at": "2026-10-07 08:15:02", "command": "mail-sync", "state": state, "reason": reason}), encoding="utf-8")
    app = create_app(live)
    body = app.test_client().get("/tracking").data.decode("utf-8")
    assert 'id="scheduled-status"' in body and shown in body
    assert ('sched-status bad' in body) == (state != "done")


def test_no_scheduled_run_yet_shows_nothing(live):
    body = create_app(live).test_client().get("/tracking").data.decode("utf-8")
    assert 'id="scheduled-status"' not in body
