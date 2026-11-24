"""The command line: where exports land, and bringing an old database forward."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import add_party, seed_reference
from opplist import db, paths, schema_migrations
from opplist.__main__ import main


@pytest.fixture
def populated(tmp_path):
    path = tmp_path / "cli.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    enquiry_id = repo.insert("enquiry", {
        "opp_no": "OPP-0001", "requester_party_id": tolvex,
        "received_on": "2026-08-03", "service_mode": "FCL",
        "origin_country": "USA", "dest_country": "Chile", "status": "New"})
    repo.insert("status_event", {"enquiry_id": enquiry_id, "at": "2026-08-03",
                                 "status": "New"})
    repo.commit()
    conn.close()
    return path


# ----------------------------------------------------- preview vs publish --
def test_export_defaults_to_the_preview_folder(populated, tmp_path, monkeypatch,
                                               capsys):
    """In preview mode nothing may land where downstream tools read."""
    preview = tmp_path / "preview"
    live_data = tmp_path / "live-data"
    live_books = tmp_path / "live-books"
    live_data.mkdir()
    live_books.mkdir()
    monkeypatch.setattr(paths, "EXPORT_PREVIEW_DIR", preview)
    monkeypatch.setattr(paths, "DATA_DIR", live_data)
    monkeypatch.setattr(paths, "WORKBOOKS_DIR", live_books)

    assert main(["export", "--db", str(populated)]) == 0

    assert (preview / paths.OPP_INDEX_NAME).exists()
    assert (preview / paths.AGENTS_CSV_NAME).exists()
    assert (preview / paths.EXPORT_WORKBOOK_NAME).exists()
    assert list(live_data.iterdir()) == [], "data\\ must be untouched"
    assert list(live_books.iterdir()) == [], "workbooks\\ must be untouched"
    assert "preview only" in capsys.readouterr().out


def test_publish_writes_the_live_paths(populated, tmp_path, monkeypatch, capsys):
    preview = tmp_path / "preview"
    live_data = tmp_path / "live-data"
    live_books = tmp_path / "live-books"
    monkeypatch.setattr(paths, "EXPORT_PREVIEW_DIR", preview)
    monkeypatch.setattr(paths, "DATA_DIR", live_data)
    monkeypatch.setattr(paths, "WORKBOOKS_DIR", live_books)

    assert main(["export", "--db", str(populated), "--publish"]) == 0

    assert (live_data / paths.OPP_INDEX_NAME).exists()
    assert (live_books / paths.EXPORT_WORKBOOK_NAME).exists()
    assert not preview.exists()
    assert "PUBLISH" in capsys.readouterr().out


def test_the_setting_alone_can_switch_to_publish(populated, tmp_path,
                                                 monkeypatch, capsys):
    """Switching exports to publish is one setting, not a flag on every command."""
    live_data = tmp_path / "live-data"
    live_books = tmp_path / "live-books"
    monkeypatch.setattr(paths, "EXPORT_PREVIEW_DIR", tmp_path / "preview")
    monkeypatch.setattr(paths, "DATA_DIR", live_data)
    monkeypatch.setattr(paths, "WORKBOOKS_DIR", live_books)
    conn = db.open_db(populated)
    db.set_setting(conn, "export_target", "publish")
    conn.commit()
    conn.close()

    assert main(["export", "--db", str(populated)]) == 0
    assert (live_data / paths.OPP_INDEX_NAME).exists()
    assert "PUBLISH" in capsys.readouterr().out


def test_preview_overrides_a_publish_setting_for_one_run(populated, tmp_path,
                                                         monkeypatch):
    preview = tmp_path / "preview"
    live_data = tmp_path / "live-data"
    monkeypatch.setattr(paths, "EXPORT_PREVIEW_DIR", preview)
    monkeypatch.setattr(paths, "DATA_DIR", live_data)
    monkeypatch.setattr(paths, "WORKBOOKS_DIR", tmp_path / "live-books")
    conn = db.open_db(populated)
    db.set_setting(conn, "export_target", "publish")
    conn.commit()
    conn.close()

    assert main(["export", "--db", str(populated), "--preview"]) == 0
    assert (preview / paths.OPP_INDEX_NAME).exists()
    assert not live_data.exists()


def test_check_lists_findings_without_touching_anything(populated, capsys):
    assert main(["check", "--db", str(populated)]) == 0
    out = capsys.readouterr().out
    assert "check(s) worth looking at" in out
    conn = db.open_db(populated)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM change_log").fetchone()["n"] > 0
    conn.close()


def test_explicit_directories_still_win(populated, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "EXPORT_PREVIEW_DIR", tmp_path / "preview")
    out = tmp_path / "chosen"
    assert main(["export", "--db", str(populated), "--out-dir", str(out),
                 "--data-dir", str(out)]) == 0
    assert (out / paths.OPP_INDEX_NAME).exists()


# ------------------------------------------------------- schema upgrades --
V1_VIEW_TAIL = "AS quoted_on"


def test_a_version_one_database_is_brought_forward(tmp_path):
    """The v1 view had no _any columns and counted approximate dates."""
    path = tmp_path / "old.db"
    conn = db.open_db(path)
    conn.execute("UPDATE setting SET value = '1' WHERE key = 'schema_version'")
    conn.executescript(
        "DROP VIEW v_enquiry;"
        "CREATE VIEW v_enquiry AS SELECT e.*, "
        " (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id "
        "  AND s.status = 'Quoted') AS quoted_on "
        "FROM enquiry e;")
    conn.commit()
    conn.close()

    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION
    columns = [r[1] for r in conn.execute("PRAGMA table_info(v_enquiry)")]
    for name in ("quoted_on", "closed_on", "quoted_on_any", "closed_on_any",
                 "last_event_on"):
        assert name in columns
    assert any("version 2" in n for n in notes)
    conn.close()


def test_the_upgrade_clears_the_other_placeholder(tmp_path):
    path = tmp_path / "old.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="migration")
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    repo.insert("enquiry", {
        "opp_no": "OPP-0001", "requester_party_id": party, "status": "Lost",
        "closed_reason": "Other", "created_by": "migration",
        "system_notes": "kept\nno Loss Reason in the workbook"})
    repo.insert("enquiry", {
        "opp_no": "OPP-0002", "requester_party_id": party, "status": "Lost",
        "closed_reason": "Other", "created_by": "manual"})
    conn.execute("UPDATE setting SET value = '1' WHERE key = 'schema_version'")
    repo.commit()
    conn.close()

    conn = db.open_db(path)
    rows = {r["opp_no"]: r for r in conn.execute("SELECT * FROM enquiry")}
    assert rows["OPP-0001"]["closed_reason"] is None
    assert "no Loss Reason" not in (rows["OPP-0001"]["system_notes"] or "")
    assert rows["OPP-0002"]["closed_reason"] == "Other", \
        "a reason someone chose in the app is left alone"
    conn.close()


def test_upgrading_twice_is_harmless(tmp_path):
    path = tmp_path / "x.db"
    conn = db.open_db(path)
    assert schema_migrations.upgrade(conn) == []
    conn.close()


def test_init_db_reports_the_current_version(tmp_path, capsys):
    assert main(["init-db", "--db", str(tmp_path / "new.db")]) == 0
    assert "version {}".format(schema_migrations.CURRENT_VERSION) \
        in capsys.readouterr().out


def test_a_schema_upgrade_takes_a_restore_point_first(tmp_path):
    """An upgrade rewrites the live database, so it is a migration (rule 3)."""
    from opplist import db

    path = tmp_path / "old.db"
    conn = db.open_db(path)
    conn.execute("UPDATE setting SET value = '2' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    conn.close()
    # Beside the database that was upgraded, never in the live data folder.
    copies = list((tmp_path / "restore-points").glob("opplist_before_schema*.db"))
    assert len(copies) == 1
    assert any("restore point" in note for note in notes)


def test_a_new_database_is_not_backed_up_before_its_first_use(tmp_path, monkeypatch):
    from opplist import backup, db

    called = []
    monkeypatch.setattr(backup, "before_task",
                        lambda *a, **k: called.append(a) or tmp_path / "x.db")
    conn = db.open_db(tmp_path / "fresh.db")
    conn.close()
    assert called == []


def test_no_test_ever_writes_into_the_live_restore_points(tmp_path):
    """The upgrade copy follows the database, so a --db elsewhere stays there."""
    from opplist import db, paths

    path = tmp_path / "elsewhere.db"
    conn = db.open_db(path)
    conn.execute("UPDATE setting SET value = '3' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    before = sorted(p.name for p in paths.RESTORE_POINTS_DIR.glob("*")) \
        if paths.RESTORE_POINTS_DIR.exists() else []
    db.open_db(path).close()
    after = sorted(p.name for p in paths.RESTORE_POINTS_DIR.glob("*")) \
        if paths.RESTORE_POINTS_DIR.exists() else []
    assert before == after
    assert (tmp_path / "restore-points").exists()


def test_the_tracking_command_prints_the_list(tmp_path, capsys):
    """`python -m opplist tracking` shows the same list as the page."""
    import datetime as dt

    from conftest import add_enquiry, add_party, seed_reference
    from opplist import db
    from opplist.__main__ import main

    path = tmp_path / "track.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    add_enquiry(repo, "OPP-0001", party, received="2026-08-01")
    repo.commit()
    conn.close()

    assert main(["tracking", "--db", str(path), "--today", "2026-09-09"]) == 0
    printed = capsys.readouterr().out
    assert "R1" in printed and "OPP-0001" in printed
    assert "focus: R1 and R3" in printed


def test_the_tracking_command_on_a_clear_day(tmp_path, capsys):
    from opplist import db
    from opplist.__main__ import main

    path = tmp_path / "empty.db"
    db.open_db(path).close()
    assert main(["tracking", "--db", str(path)]) == 0
    assert "Nothing to chase today." in capsys.readouterr().out


def test_an_older_database_gains_the_note_column(tmp_path):
    """schema v5 on a database written before notes existed."""
    from opplist import db

    path = tmp_path / "v4.db"
    conn = db.open_db(path)
    conn.execute("UPDATE setting SET value = '4' WHERE key = 'schema_version'")
    # The view reads the column, so it has to go first - which is exactly what
    # a version 4 database looks like.
    conn.execute("DROP VIEW v_enquiry")
    # v58 (FX7): the wait views read it too, and a version 4 database has neither
    conn.execute("DROP VIEW v_wait_followup")
    conn.execute("DROP VIEW v_wait")
    conn.execute("ALTER TABLE status_event DROP COLUMN kind")
    conn.commit()
    conn.close()

    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(status_event)")]
    assert "kind" in columns
    assert conn.execute("SELECT value FROM setting WHERE key = 'schema_version'"
                        ).fetchone()[0] == str(
        __import__("opplist.schema_migrations", fromlist=["x"]).CURRENT_VERSION)
    assert any("kind" in note for note in notes)
    conn.close()


# ------------------------------------------------ export_target = publish -----
def test_the_workbook_is_found_in_restore_points_after_the_move(tmp_path,
                                                                monkeypatch):
    """The master is not in workbooks\\ any anymore; verify still finds it."""
    from opplist import paths

    workbooks = tmp_path / "workbooks"
    restore = tmp_path / "data" / "restore-points"
    workbooks.mkdir(parents=True)
    restore.mkdir(parents=True)
    monkeypatch.setattr(paths, "SOURCE_WORKBOOK",
                        workbooks / "LATAM Opportunity List.xlsx")
    monkeypatch.setattr(paths, "ARCHIVED_WORKBOOK",
                        restore / "LATAM Opportunity List.xlsx")
    monkeypatch.setattr(paths, "RESTORE_POINTS_DIR", restore)

    assert paths.source_workbook() is None          # nothing anywhere yet

    (restore / "LATAM OppList pre-cutover-0909.xlsx").write_bytes(b"x")
    assert paths.source_workbook().name == "LATAM OppList pre-cutover-0909.xlsx"

    (restore / "LATAM Opportunity List.xlsx").write_bytes(b"x")
    assert paths.source_workbook() == paths.ARCHIVED_WORKBOOK

    (workbooks / "LATAM Opportunity List.xlsx").write_bytes(b"x")
    assert paths.source_workbook() == paths.SOURCE_WORKBOOK   # master wins


def test_verify_without_a_workbook_says_so_instead_of_crashing(tmp_path,
                                                               monkeypatch,
                                                               capsys):
    from opplist import db, paths
    from opplist.__main__ import main

    monkeypatch.setattr(paths, "SOURCE_WORKBOOK", tmp_path / "gone.xlsx")
    monkeypatch.setattr(paths, "ARCHIVED_WORKBOOK", tmp_path / "gone2.xlsx")
    monkeypatch.setattr(paths, "RESTORE_POINTS_DIR", tmp_path / "empty")
    path = tmp_path / "verify.db"
    db.open_db(path).close()

    assert main(["verify", "--db", str(path)]) == 1
    assert "No workbook to verify against" in capsys.readouterr().out


# ------------------------------------------------ propose / apply status ----
def _proposal_csv(path, **overrides):
    import csv

    from opplist import propose

    row = {
        "opp_no": "OPP-0001", "requester": "Tolvex", "current_status": "New",
        "proposed_status": "Won", "proposed_date": "2026-08-17",
        "closed_reason": "", "evidence_sender": "Renata <renata@tolvex.example>",
        "evidence_date": "2026-08-17",
        "evidence_quote": "Please proceed with the service.",
        "confidence": "high", "awarded_vendor_guess": "", "found_by": "rare word",
        "mails_seen": "4", "note": "",
    }
    row.update(overrides)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=propose.CSV_COLUMNS,
                                quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writeheader()
        writer.writerow(row)
    return path


def _one_case(tmp_path):
    import datetime as dt

    from conftest import add_enquiry, add_party, seed_reference
    from opplist import db

    path = tmp_path / "apply.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    add_enquiry(repo, "OPP-0001", party, received="2026-08-13",
                status_confirmed=0)
    repo.commit()
    conn.close()
    return path


def test_apply_status_writes_and_takes_a_restore_point(tmp_path, capsys):
    import sqlite3

    from opplist.__main__ import main

    path = _one_case(tmp_path)
    csv_path = _proposal_csv(tmp_path / "proposed.csv")
    restore = tmp_path / "restore"
    restore.mkdir()

    assert main(["apply-status", str(csv_path), "--db", str(path),
                 "--restore-dir", str(restore)]) == 0
    printed = capsys.readouterr().out
    assert "applied 1 of 1 row(s)" in printed
    assert "restore point" in printed
    assert list(restore.glob("opplist_before_apply-status_*.db"))

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, status, status_confirmed FROM enquiry "
                       "WHERE opp_no = 'OPP-0001'").fetchone()
    assert (row["status"], row["status_confirmed"]) == ("Won", 1)
    event = conn.execute("SELECT at, source, note FROM status_event "
                         "WHERE enquiry_id = ? ORDER BY id DESC LIMIT 1",
                         (row["id"],)).fetchone()
    assert (event["at"], event["source"]) == ("2026-08-17", "scan")
    assert "Please proceed" in event["note"]
    conn.close()


def test_apply_status_leaves_alone_what_is_not_in_the_file(tmp_path, capsys):
    import sqlite3

    from opplist.__main__ import main

    path = _one_case(tmp_path)
    csv_path = _proposal_csv(tmp_path / "proposed.csv",
                             opp_no="OPP-9999")      # a row for another case
    assert main(["apply-status", str(csv_path), "--db", str(path)]) == 0
    assert "no such case" in capsys.readouterr().out
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT status_confirmed FROM enquiry "
                        "WHERE opp_no = 'OPP-0001'").fetchone()[0] == 0
    conn.close()


def test_propose_status_needs_a_month_or_a_case(tmp_path, capsys):
    from opplist import db
    from opplist.__main__ import main

    path = tmp_path / "propose.db"
    db.open_db(path).close()
    assert main(["propose-status", "--db", str(path)]) == 2
    assert "Say which month" in capsys.readouterr().out


def test_propose_status_with_outlook_closed_says_so(tmp_path, monkeypatch,
                                                    capsys):
    """A closed Outlook is a sentence, never a stack trace."""
    from conftest import add_enquiry, add_party, seed_reference
    from opplist import db, outlook_bridge
    from opplist.__main__ import main

    path = tmp_path / "closed.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    add_enquiry(repo, "OPP-0001", party, received="2026-08-13",
                status_confirmed=0)
    repo.commit()
    conn.close()

    class Closed:
        def __enter__(self):
            raise outlook_bridge.OutlookError(outlook_bridge.NOT_RUNNING)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(outlook_bridge, "Search", Closed)
    assert main(["propose-status", "--month", "2026-08", "--db", str(path),
                 "--source", "outlook"]) == 1
    assert "Open Outlook" in capsys.readouterr().out
