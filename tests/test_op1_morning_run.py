"""Batch OP1 (2026-11-10): the morning run from the last commit, every
step's outcome, the banner, the auto export without the workbook, the
off-PC copy, Remove award and the Sea-Air re-sync. Each git test builds its
own small repository under tmp_path; every value here is invented (OPP-9401
up, .test domains)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import __main__ as cli
from opplist import autoexport, checks, db, paths, scheduled, schema_migrations, seaair_resync
from opplist.web import create_app


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.test",
                           *args], check=True, capture_output=True, text=True).stdout


PROBE_MAIN = '''import json, os, sys
from pathlib import Path
from opplist import app
Path(os.environ["OP1_PROBE"]).write_text(json.dumps({
    "cwd": os.getcwd(), "argv": sys.argv[1:], "value": app.VALUE,
    "commit": os.environ.get("OPPLIST_RUN_COMMIT"), "left": os.environ.get("OPPLIST_RUN_UNCOMMITTED"),
    "inside": os.environ.get("OPPLIST_RUN_WORKTREE")}))
sys.exit(3)
'''


@pytest.fixture
def code(tmp_path, monkeypatch):
    """A repository shaped like opplist: a package whose __main__ only says
    where it ran and what it saw, schema.sql and tests, all committed, in
    a JoJo-like folder (so the worktree lands beside it)."""
    root = tmp_path / "JoJo" / "opplist"
    for name, text in (("opplist/__init__.py", ""), ("opplist/__main__.py", PROBE_MAIN),
                       ("opplist/app.py", "VALUE = 1\n"), ("schema.sql", "-- schema\n"),
                       ("tests/test_app.py", "def test(): pass\n")):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    probe = tmp_path / "probe.json"
    monkeypatch.setenv("OP1_PROBE", str(probe))
    monkeypatch.delenv(cli.IN_WORKTREE_ENV, raising=False)
    monkeypatch.setattr(cli, "HANDOVER_SKIP", None)
    return root, probe


def test_uncommitted_code_runs_the_command_from_a_worktree_at_head(code):
    root, probe = code
    (root / "opplist" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")     # unfinished
    head = git(root, "rev-parse", "HEAD").strip()
    assert cli.hand_over(["propose-status", "--open", "--scheduled"], repo_root=root) == 3   # its exit code
    seen = json.loads(probe.read_text(encoding="utf-8"))
    worktree = root.parent / "opplist-run"
    assert seen["cwd"] == str(worktree) and seen["value"] == 1                   # the committed code
    assert seen["argv"] == ["propose-status", "--open", "--scheduled"]
    assert (seen["commit"], seen["left"], seen["inside"]) == (head, "1", "1")
    assert git(worktree, "rev-parse", "HEAD").strip() == head
    assert not list(worktree.rglob("__pycache__"))                              # nothing written in there


def test_the_next_run_brings_the_worktree_to_the_new_head_and_discards_its_changes(code):
    root, probe = code
    (root / "opplist" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert cli.hand_over(["mail-sync", "--scheduled"], repo_root=root) == 3
    worktree = root.parent / "opplist-run"
    (root / "opplist" / "app.py").write_text("VALUE = 5\n", encoding="utf-8")
    git(root, "commit", "-q", "-am", "five")
    (root / "opplist" / "app.py").write_text("VALUE = 6\n", encoding="utf-8")     # unfinished again
    (worktree / "opplist" / "app.py").write_text("VALUE = 99\n", encoding="utf-8")   # changed in the worktree
    assert cli.hand_over(["mail-sync", "--scheduled"], repo_root=root) == 3
    seen = json.loads(probe.read_text(encoding="utf-8"))
    assert seen["value"] == 5 and seen["commit"] == git(root, "rev-parse", "HEAD").strip()


def test_clean_code_makes_no_worktree(code):
    root, probe = code
    (root / "tests" / "test_app.py").write_text("def test(): assert 1\n", encoding="utf-8")   # not code
    assert cli.hand_over(["mail-sync", "--scheduled"], repo_root=root) is None
    assert not (root.parent / "opplist-run").exists() and not probe.exists()


def test_a_manual_run_is_never_handed_over(code):
    root, probe = code
    (root / "opplist" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert cli.hand_over(["mail-sync"], repo_root=root) is None
    assert not (root.parent / "opplist-run").exists()


def test_a_run_inside_the_worktree_never_starts_another(code, monkeypatch):
    root, probe = code
    (root / "opplist" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    monkeypatch.setenv(cli.IN_WORKTREE_ENV, "1")
    assert cli.hand_over(["mail-sync", "--scheduled"], repo_root=root) is None
    monkeypatch.delenv(cli.IN_WORKTREE_ENV)
    assert cli.hand_over(["mail-sync", "--scheduled"], repo_root=root.parent / "opplist-run") is None
    assert not probe.exists()


def test_the_worktree_run_records_its_commit_and_the_uncommitted_count(live, monkeypatch, caplog):
    monkeypatch.setattr(scheduled, "REPO_ROOT", live.parent / "clean-code")
    monkeypatch.setattr(scheduled, "uncommitted", lambda repo_root=None: [])
    monkeypatch.setenv(scheduled.COMMIT_ENV, "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678")
    monkeypatch.setenv(scheduled.UNCOMMITTED_ENV, "3")
    with caplog.at_level(logging.INFO):
        assert cli.main(["check", "--db", str(live), "--scheduled"]) == 0
    run = scheduled.last_run()
    assert (run["state"], run["commit"], run["uncommitted"]) == ("done", "a1b2c3d", 3)
    assert any("scheduled run done [check --db {}] at a1b2c3d from opplist-run (3 files uncommitted)".format(live)
               == r.getMessage() for r in caplog.records)


def test_a_clean_run_records_the_commit_it_ran_at(repo, live):
    assert cli.main(["check", "--db", str(live), "--scheduled"]) == 0
    assert scheduled.last_run()["commit"] == git(repo, "rev-parse", "--short=7", "HEAD").strip()
    assert scheduled.last_run()["uncommitted"] == 0


def test_a_git_failure_is_a_skip_with_its_reason(code, live, monkeypatch):
    root, probe = code
    (root / "opplist" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_git_exe", lambda: None)
    assert cli.hand_over(["check", "--scheduled"], repo_root=root) is None
    assert cli.HANDOVER_SKIP == "git cannot be run: git not found"
    assert cli.main(["check", "--db", str(live), "--scheduled"]) == scheduled.EXIT_NOT_COMMITTED
    run = scheduled.last_run()
    assert (run["state"], run["reason"]) == ("skipped", "git cannot be run: git not found")


def test_inside_the_worktree_a_database_behind_still_waits_for_the_restart(repo, live, monkeypatch):
    monkeypatch.setenv(cli.IN_WORKTREE_ENV, "1")
    monkeypatch.setenv(scheduled.COMMIT_ENV, "f00dfeed00000000000000000000000000000000")
    conn = sqlite3.connect(str(live))
    conn.execute("UPDATE setting SET value = ? WHERE key = 'schema_version'",
                 (str(schema_migrations.CURRENT_VERSION - 1),))
    conn.commit()
    conn.close()
    before = live.read_bytes()
    assert cli.main(["propose-shipments", "--db", str(live), "--scheduled"]) == scheduled.EXIT_NEEDS_UPGRADE
    assert live.read_bytes() == before
    assert scheduled.last_run()["commit"] == "f00dfee"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A clean repository for scheduled.guard (as in test_scheduled_runs)."""
    root = tmp_path / "code"
    for name, text in (("opplist/app.py", "VALUE = 1\n"), ("schema.sql", "-- schema\n")):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    monkeypatch.setattr(scheduled, "REPO_ROOT", root)
    monkeypatch.delenv(scheduled.COMMIT_ENV, raising=False)
    monkeypatch.delenv(scheduled.UNCOMMITTED_ENV, raising=False)
    return root


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "HANDOVER_SKIP", None)
    path = tmp_path / "live.db"
    conn = db.open_db(path)
    seed_reference(db.Repository(conn, source="test"))
    conn.commit()
    conn.close()
    return path


# ----------------------------------------------------- one entry each --
def test_one_entry_per_command_and_last_run_the_newest(repo, live):
    scheduled.record("mail-sync", "done")
    scheduled.record("propose-status --open --write-suggestions", "skipped", "the app has to upgrade the database first (v48 to v49)")
    data = scheduled.load()
    assert set(data["runs"]) == {"mail-sync", "propose-status"}
    assert data["runs"]["propose-status"]["command"] == "propose-status --open --write-suggestions"
    assert scheduled.last_run()["command"] == "propose-status --open --write-suggestions"
    scheduled.record("mail-sync", "failed", "exit 1")
    assert len(scheduled.load()["runs"]) == 2 and scheduled.last_run()["state"] == "failed"


def test_the_file_from_before_reads_as_its_commands_entry():
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (paths.DATA_DIR / scheduled.STATE_NAME).write_text(json.dumps(
        {"at": "2026-11-10 08:15:10", "command": "firebase-sync", "state": "skipped", "reason": "x"}), encoding="utf-8")
    assert scheduled.load()["runs"]["firebase-sync"]["reason"] == "x"
    assert scheduled.last_run()["at"] == "2026-11-10 08:15:10"


def test_a_step_that_raises_is_recorded_failed_with_the_first_line(repo, live, monkeypatch):
    def boom(args):
        raise RuntimeError("Outlook is not open\nsecond line")
    monkeypatch.setattr(cli, "cmd_check", boom)
    with pytest.raises(RuntimeError):
        cli.main(["check", "--db", str(live), "--scheduled"])
    run = scheduled.load()["runs"]["check"]
    assert (run["state"], run["reason"]) == ("failed", "RuntimeError: Outlook is not open")


# ---------------------------------------------------------------- banner --
UPGRADE = "the app has to upgrade the database first (v48 to v49)"


def _write_runs(runs, manual=None):
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (paths.DATA_DIR / scheduled.STATE_NAME).write_text(json.dumps(
        {"runs": runs, "manual": manual or {}}), encoding="utf-8")


def _morning(state, reason, day=None):
    day = day or dt.date.today().isoformat()
    return {scheduled.step_of(c): {"at": "{} 08:15:{:02d}".format(day, i), "command": c, "state": state,
                                   "reason": reason} for i, c in enumerate(scheduled.MORNING_STEPS)}


def test_the_banner_for_a_skipped_morning_says_what_and_the_fix(live):
    _write_runs(_morning("skipped", UPGRADE))
    body = create_app(live).test_client().get("/enquiries").data.decode("utf-8")
    assert ('id="run-banner"' in body and "This morning&#39;s update was skipped: " + UPGRADE + ". "
            "Restart OppList, then run: python -m opplist mail-sync, propose-status --open --write-suggestions, "
            "propose-shipments.") in body
    assert 'id="run-banner"' in create_app(live).test_client().get("/settings").data.decode("utf-8")


@pytest.mark.parametrize("state,reason,text,fix", [
    ("skipped", "git cannot be run: git not found", "This morning's update was skipped: git cannot be run: git not found.",
     "Check that git runs on this PC, then run: python -m opplist mail-sync, propose-status --open "
     "--write-suggestions, propose-shipments."),
    ("failed", "exit 1", "This morning's update failed: exit 1.",
     "Look in data\\opplist.log for the error, then run: python -m opplist mail-sync, propose-status --open "
     "--write-suggestions, propose-shipments."),
])
def test_the_banner_words_for_each_reason(state, reason, text, fix):
    _write_runs(_morning(state, reason))
    got = scheduled.banner()
    assert (got["text"], got["fix"]) == (text, fix)


def test_only_the_steps_that_did_not_run_are_in_the_fix():
    runs = _morning("done", "")
    runs["propose-shipments"].update(state="failed", reason="RuntimeError: no carrier page")
    _write_runs(runs)
    got = scheduled.banner()
    assert got["text"] == "This morning's update failed: RuntimeError: no carrier page."
    assert got["fix"].endswith("then run: python -m opplist propose-shipments.")


def test_the_banner_goes_away_after_a_done_run_of_those_steps(live):
    _write_runs(_morning("skipped", UPGRADE))
    for command in scheduled.MORNING_STEPS[:3]:
        scheduled.record_manual(command, "done")                 # what she runs from the fix
    assert scheduled.banner() is not None                        # firebase-sync: the app's push clears it
    scheduled.record_manual("firebase-sync", "done")
    assert scheduled.banner() is None
    body = create_app(live).test_client().get("/enquiries").data.decode("utf-8")
    assert 'id="run-banner"' not in body


def test_a_later_scheduled_run_done_clears_the_banner_too(repo, live):
    _write_runs(_morning("skipped", UPGRADE))
    for command in scheduled.MORNING_STEPS:
        scheduled.record(command, "done")
    assert scheduled.banner() is None


def test_yesterdays_skip_is_not_this_mornings_and_a_long_silence_is(live):
    yesterday = dt.date.today() - dt.timedelta(days=1)
    _write_runs(_morning("skipped", UPGRADE, day=yesterday.isoformat()))
    assert scheduled.banner(now=dt.datetime.combine(yesterday + dt.timedelta(days=1), dt.time(7, 0))) is None
    old = dt.date(2026, 11, 2)                                   # a Monday
    _write_runs(_morning("done", "", day=old.isoformat()))
    assert scheduled.banner(now=dt.datetime(2026, 11, 3, 9, 0)) is None          # one working day
    got = scheduled.banner(now=dt.datetime(2026, 11, 5, 9, 0))
    assert got["text"] == "The morning update has not run since Mon 11/2 08:15."
    assert got["fix"].startswith("Check that the JoJo Mail Refresh task is still scheduled, then run: ")


def test_a_manual_run_of_another_command_is_not_remembered(live, capsys):
    assert cli.main(["check", "--db", str(live)]) == 0
    assert scheduled.load()["manual"] == {}


def test_the_app_push_of_the_team_site_clears_its_step(live, monkeypatch):
    from opplist import firebase_sync
    monkeypatch.setattr(firebase_sync, "sync", lambda db_path, **kw: SimpleNamespace(ok=True))
    firebase_sync.Syncer(live).run()
    assert scheduled.load()["manual"]["firebase-sync"]["state"] == "done"


# ----------------------------------------------------------- auto export --
@pytest.fixture
def published(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORKBOOKS_DIR", tmp_path / "workbooks")
    path = tmp_path / "op1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    add_enquiry(repo, "OPP-9401", add_party(repo, "Velmora Cargo"), received="2027-02-01")
    db.set_setting(conn, "export_target", "publish")
    conn.commit()
    conn.close()
    return path


def test_the_auto_export_writes_the_two_csv_files_and_not_the_workbook(published):
    exporter = autoexport.AutoExporter(published)
    assert exporter.enabled()
    exporter._run()
    assert exporter.last_error is None
    assert (paths.DATA_DIR / paths.OPP_INDEX_NAME).exists() and (paths.DATA_DIR / paths.AGENTS_CSV_NAME).exists()
    assert not (paths.WORKBOOKS_DIR / paths.EXPORT_WORKBOOK_NAME).exists()
    assert "OPP-9401" in (paths.DATA_DIR / paths.OPP_INDEX_NAME).read_text(encoding="utf-8-sig")


def test_export_now_and_the_export_page_still_write_the_workbook(published):
    client = create_app(published).test_client()
    page = client.get("/export").data.decode("utf-8")
    assert 'id="export-auto-csv"' in page and "the workbook is written only by Export now" in page
    client.post("/export/run")
    assert (paths.WORKBOOKS_DIR / paths.EXPORT_WORKBOOK_NAME).exists()


# ------------------------------------------------------------ off-PC copy --
@pytest.fixture
def offsite(tmp_path, live):
    folder = tmp_path / "share" / "OppList copies"
    folder.mkdir(parents=True)
    conn = db.open_db(live)
    db.set_setting(conn, scheduled.OFFSITE_SETTING, str(folder))
    conn.commit()
    conn.close()
    points = live.parent / "restore-points"
    points.mkdir()
    for name in ("opplist_20271101.db", "opplist_20271109.db", "opplist_before_mfn-link_20271109_101010.db"):
        (points / name).write_bytes(name.encode())
    (live.parent / "secrets").mkdir()
    (live.parent / "secrets" / "key.json").write_text("{}", encoding="utf-8")
    return folder, points


def test_the_off_pc_copy_is_made_when_due_and_not_again_within_seven_days(offsite, live):
    folder, points = offsite
    (folder / "OppList_20271020.db").write_bytes(b"older copy")
    got = scheduled.offsite_copy(live, today=dt.date(2027, 11, 10))
    assert got["state"] == "copied"
    assert (folder / "OppList_20271109.db").read_bytes() == b"opplist_20271109.db"
    assert scheduled.offsite_copy(live, today=dt.date(2027, 11, 10)) is None                 # once a day
    (points / "opplist_20271111.db").write_bytes(b"newer")
    assert scheduled.offsite_copy(live, today=dt.date(2027, 11, 11))["state"] == "not due"   # 2 days old
    (points / "opplist_20271116.db").write_bytes(b"week later")
    assert scheduled.offsite_copy(live, today=dt.date(2027, 11, 16))["state"] == "copied"    # 7 days
    assert sorted(p.name for p in folder.iterdir()) == ["OppList_20271020.db", "OppList_20271109.db",
                                                        "OppList_20271116.db"]   # nothing deleted, only dailies
    assert (folder / "OppList_20271020.db").read_bytes() == b"older copy"
    status = scheduled.offsite_status(live, today=dt.date(2027, 11, 20))
    assert (status["last_copy"], status["bad"]) == ("2027-11-16", False)
    assert scheduled.offsite_status(live, today=dt.date(2027, 11, 25))["bad"]          # 9 days old


def test_a_name_already_there_is_never_overwritten(offsite, live):
    folder, points = offsite
    (folder / "OppList_20271101.db").write_bytes(b"hers")
    (points / "opplist_20271109.db").unlink()
    got = scheduled.offsite_copy(live, today=dt.date(2027, 11, 10))
    assert got["state"] == "not due" and got["reason"] == "no restore point newer than the last copy"
    assert (folder / "OppList_20271101.db").read_bytes() == b"hers"


def test_an_unreachable_folder_is_recorded_and_never_fails_the_run(offsite, live, repo, monkeypatch):
    conn = db.open_db(live)
    db.set_setting(conn, scheduled.OFFSITE_SETTING, str(live.parent / "offline-share" / "OppList"))
    conn.commit()
    conn.close()
    assert cli.main(["check", "--db", str(live), "--scheduled"]) == 0             # the run goes on
    entry = scheduled.load()["offsite"]
    assert entry["state"] == "failed" and entry["reason"].startswith("the folder cannot be read")
    assert scheduled.offsite_status(live)["bad"]
    body = create_app(live).test_client().get("/tracking").data.decode("utf-8")
    assert 'id="offsite-status"' in body and "offsite-status bad" in body and "last attempt failed" in body


def test_the_skipped_run_still_copies(offsite, live, repo):
    conn = sqlite3.connect(str(live))
    conn.execute("UPDATE setting SET value = ? WHERE key = 'schema_version'",
                 (str(schema_migrations.CURRENT_VERSION - 1),))
    conn.commit()
    conn.close()
    assert cli.main(["propose-shipments", "--db", str(live), "--scheduled"]) == scheduled.EXIT_NEEDS_UPGRADE
    folder, _ = offsite
    assert [p.name for p in folder.iterdir()] == ["OppList_20271109.db"]


def test_no_folder_set_does_nothing(live):
    assert scheduled.offsite_copy(live) is None
    assert scheduled.offsite_status(live) is None


def test_the_setting_saves_and_settings_shows_the_last_copy(offsite, live):
    folder, _ = offsite
    scheduled.offsite_copy(live, today=dt.date(2027, 11, 10))
    client = create_app(live).test_client()
    page = client.get("/settings").data.decode("utf-8")
    assert 'name="offsite_backup_dir"' in page and "A folder only you can open" in page
    assert "Last copy 2027-11-09" in page
    client.post("/settings", data={"offsite_backup_dir": "  \\\\chi-share\\Iris\\OppList  "})
    conn = db.open_db(live)
    assert db.get_setting(conn, "offsite_backup_dir") == "\\\\chi-share\\Iris\\OppList"
    conn.close()


# ----------------------------------------------------------- remove award --
@pytest.fixture
def awarded(tmp_path):
    path = tmp_path / "award.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    asker = add_party(repo, "Orvanta Logistics")
    first, second = add_party(repo, "Pellucid Air Partners"), add_party(repo, "Tamsin Ocean Lines")
    case = add_enquiry(repo, "OPP-9402", asker, status="Won", received="2027-02-03")
    vr1 = repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": first, "asked_on": "2027-02-03",
                                         "source": "manual"})
    vr2 = repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": second, "asked_on": "2027-02-03",
                                         "source": "manual"})
    repo.set_rate_used(case, "agent", vendor_request_id=vr1, note="the cheaper one")
    conn.commit()
    conn.close()
    return path, case, vr1, vr2


def test_remove_award_clears_the_flag_and_rate_used_in_one_action(awarded):
    path, case, vr1, vr2 = awarded
    client = create_app(path).test_client()
    page = client.get("/enquiries/{}".format(case)).data.decode("utf-8")
    assert 'value="unaward"' in page and "Remove award" in page
    response = client.post("/enquiries/{}/vendors".format(case), data={"vendor_request_id": vr1, "action": "unaward"},
                           follow_redirects=True)
    assert "Award removed: Pellucid Air Partners. Rate used is not answered." in response.data.decode("utf-8")
    conn = db.open_db(path)
    row = conn.execute("SELECT rate_used, awarded_vendor_id, awarded_note FROM enquiry WHERE id = ?", (case,)).fetchone()
    assert tuple(row) == (None, None, None)
    assert conn.execute("SELECT awarded FROM vendor_request WHERE id = ?", (vr1,)).fetchone()[0] == 0
    logged = conn.execute("SELECT table_name, field, action_id, source FROM change_log "
                          "WHERE action_id = (SELECT MAX(action_id) FROM change_log)").fetchall()
    assert {(r["table_name"], r["field"]) for r in logged} >= {
        ("vendor_request", "awarded"), ("enquiry", "rate_used"), ("enquiry", "awarded_vendor_id"),
        ("enquiry", "awarded_note")}
    assert {r["source"] for r in logged} == {"ui"}
    finding = checks.won_without_award(conn)
    assert [r["opp_no"] for r in finding.rows] == ["OPP-9402"]
    assert "not answered yet" in client.get("/enquiries/{}".format(case)).data.decode("utf-8")
    conn.close()


def test_remove_on_a_request_that_is_not_awarded_changes_nothing(awarded):
    path, case, vr1, vr2 = awarded
    client = create_app(path).test_client()
    response = client.post("/enquiries/{}/vendors".format(case), data={"vendor_request_id": vr2, "action": "unaward"},
                           follow_redirects=True)
    assert "not the awarded one" in response.data.decode("utf-8")
    conn = db.open_db(path)
    assert conn.execute("SELECT rate_used FROM enquiry WHERE id = ?", (case,)).fetchone()[0] == "agent"
    conn.close()


def test_award_on_another_row_still_moves_the_flag(awarded):
    path, case, vr1, vr2 = awarded
    create_app(path).test_client().post("/enquiries/{}/vendors".format(case),
                                        data={"vendor_request_id": vr2, "action": "award"})
    conn = db.open_db(path)
    flags = dict(conn.execute("SELECT id, awarded FROM vendor_request WHERE enquiry_id = ?", (case,)).fetchall())
    assert flags == {vr1: 0, vr2: 1}
    conn.close()


# ------------------------------------------------------- Sea-Air re-sync --
def test_the_sea_air_totals_are_re_synced_once_and_not_twice(tmp_path):
    path = tmp_path / "seaair.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Brisa Norte Cargo", country="Chile")
    sea_air = add_enquiry(repo, "OPP-9403", party, received="2027-02-05", service_mode="LCL",
                          origin_country="China", dest_country="Chile")
    repo.add_cargo_line(sea_air, {"pieces": 4, "weight_value": 1800, "weight_unit": "kg", "cbm_value": 2,
                                  "source": "manual"})
    other = add_enquiry(repo, "OPP-9404", party, received="2027-02-06", service_mode="LCL",
                        origin_country="China", dest_country="Chile")
    repo.add_cargo_line(other, {"pieces": 2, "weight_value": 300, "weight_unit": "kg", "cbm_value": 1,
                                "source": "manual"})
    repo.commit()
    # what the v48 upgrade did: the mode set, the totals left as they were
    conn.execute("UPDATE enquiry SET service_mode = 'Sea-Air' WHERE id = ?", (sea_air,))
    conn.execute("UPDATE enquiry_route SET service_mode = 'Sea-Air' WHERE enquiry_id = ?", (sea_air,))
    conn.commit()
    assert conn.execute("SELECT air_weight_kg FROM enquiry WHERE id = ?", (sea_air,)).fetchone()[0] is None
    lines = seaair_resync.run_once(conn)
    weight = conn.execute("SELECT air_weight_kg FROM enquiry WHERE id = ?", (sea_air,)).fetchone()[0]
    assert weight and lines == ["Sea-Air totals re-synced: OPP-9403 (chargeable {:,.1f} kg).".format(weight)]
    logged = conn.execute("SELECT DISTINCT source, action_id FROM change_log WHERE table_name = 'enquiry' "
                          "AND row_id = ? AND field = 'air_weight_kg'", (sea_air,)).fetchall()
    assert [r["source"] for r in logged] == ["migration"]
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE row_id = ? AND table_name = 'enquiry' "
                        "AND source = 'migration'", (other,)).fetchone()[0] == 0
    before = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    assert seaair_resync.run_once(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == before
    assert db.get_setting(conn, seaair_resync.SETTING)
    conn.close()


@pytest.fixture(autouse=True)
def _worktrees_asked_for(monkeypatch):
    """This edition hands over to a worktree only when asked to
    (OPPLIST_WORKTREES=1); these tests are about the hand-over."""
    monkeypatch.setenv(cli.WORKTREES_ENV, "1")
