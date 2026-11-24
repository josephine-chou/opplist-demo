"""Export target and the auto export debounce."""

from __future__ import annotations

import time

import pytest

from conftest import add_party, seed_reference
from opplist import autoexport, db, paths


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "auto.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    enquiry_id = repo.insert("enquiry", {
        "opp_no": "OPP-0001", "requester_party_id": party,
        "received_on": "2026-09-01", "status": "New"})
    repo.insert("status_event", {"enquiry_id": enquiry_id, "at": "2026-09-01",
                                 "status": "New"})
    repo.commit()
    conn.close()
    return path


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    preview = tmp_path / "preview"
    live_books = tmp_path / "live-books"
    live_data = tmp_path / "live-data"
    monkeypatch.setattr(paths, "EXPORT_PREVIEW_DIR", preview)
    monkeypatch.setattr(paths, "WORKBOOKS_DIR", live_books)
    monkeypatch.setattr(paths, "DATA_DIR", live_data)
    return preview, live_books, live_data


def set_target(path, value):
    conn = db.open_db(path)
    db.set_setting(conn, "export_target", value)
    conn.commit()
    conn.close()


# ------------------------------------------------------------ the target --
def test_the_default_target_is_preview(db_path):
    conn = db.open_db(db_path)
    assert autoexport.target(conn) == "preview"
    assert db.get_setting(conn, "export_target") == "preview"
    conn.close()


def test_directories_follow_the_target(dirs):
    preview, live_books, live_data = dirs
    assert autoexport.directories("preview") == (preview, preview)
    assert autoexport.directories("publish") == (live_books, live_data)


def test_export_now_writes_where_the_setting_says(db_path, dirs):
    preview, live_books, live_data = dirs
    written = autoexport.export_now(db_path)
    assert written["mode"] == "preview"
    assert written["opp_index"].parent == preview
    assert not live_books.exists() and not live_data.exists()

    set_target(db_path, "publish")
    written = autoexport.export_now(db_path)
    assert written["mode"] == "publish"
    assert written["workbook"].parent == live_books
    assert written["opp_index"].parent == live_data


def test_one_run_can_override_the_setting(db_path, dirs):
    preview, live_books, _ = dirs
    written = autoexport.export_now(db_path, mode="publish")
    assert written["workbook"].parent == live_books
    conn = db.open_db(db_path)
    assert autoexport.target(conn) == "preview", "the setting is not changed"
    conn.close()


def test_a_bad_share_directory_does_not_fail_the_export(db_path, dirs, tmp_path):
    conn = db.open_db(db_path)
    db.set_setting(conn, "export_target", "publish")
    # A path that cannot be a directory: the copy must fail, the export must not.
    db.set_setting(conn, "export_share_dir",
                   str(tmp_path / "not-a-dir.txt" / "inside"))
    (tmp_path / "not-a-dir.txt").write_text("x", encoding="utf-8")
    conn.commit()
    conn.close()
    written = autoexport.export_now(db_path)
    assert written["workbook"].exists()
    assert written["share"] is None and "share_error" in written


# ------------------------------------------------------------ the timer ---
def test_auto_export_stays_asleep_while_the_target_is_preview(db_path, dirs):
    exporter = autoexport.AutoExporter(db_path, delay=0.01)
    assert exporter.enabled() is False
    assert exporter.schedule() is False
    time.sleep(0.05)
    assert exporter.last_result is None
    preview, _, _ = dirs
    assert not preview.exists(), "nothing was written"


def test_auto_export_runs_once_on_publish(db_path, dirs):
    set_target(db_path, "publish")
    _, live_books, live_data = dirs
    exporter = autoexport.AutoExporter(db_path, delay=0.05)
    assert exporter.enabled() is True
    assert exporter.schedule() is True
    for _ in range(100):
        if exporter.last_result:
            break
        time.sleep(0.02)
    assert exporter.last_result is not None
    assert exporter.last_error is None
    # OP1 (2026-11-10): the two CSV files only; the workbook is the Export page's
    assert (live_data / paths.OPP_INDEX_NAME).exists() and (live_data / paths.AGENTS_CSV_NAME).exists()
    assert not (live_books / paths.EXPORT_WORKBOOK_NAME).exists()


def test_a_burst_of_writes_produces_one_export(db_path, dirs):
    set_target(db_path, "publish")
    exporter = autoexport.AutoExporter(db_path, delay=0.15)
    runs = []
    original = autoexport.export_now
    autoexport.export_now = lambda *a, **k: (runs.append(1)
                                             or original(*a, **k))
    try:
        for _ in range(5):
            exporter.schedule()
            time.sleep(0.02)
        for _ in range(100):
            if runs:
                break
            time.sleep(0.02)
        time.sleep(0.2)
    finally:
        autoexport.export_now = original
        exporter.cancel()
    assert len(runs) == 1, "the debounce collapses a burst into one export"


def test_auto_export_can_be_turned_off(db_path, dirs):
    conn = db.open_db(db_path)
    db.set_setting(conn, "export_target", "publish")
    db.set_setting(conn, "auto_export", "0")
    conn.commit()
    conn.close()
    exporter = autoexport.AutoExporter(db_path, delay=0.01)
    assert exporter.enabled() is False
    assert exporter.schedule() is False


def test_a_failing_export_is_recorded_not_raised(db_path, dirs):
    set_target(db_path, "publish")
    exporter = autoexport.AutoExporter(db_path, delay=0.01)
    original = autoexport.export_now
    autoexport.export_now = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("disk full"))
    try:
        exporter.schedule()
        for _ in range(100):
            if exporter.last_error:
                break
            time.sleep(0.02)
    finally:
        autoexport.export_now = original
    assert exporter.last_error == "disk full"
    for _ in range(100):                      # the timer clears itself last
        if not exporter.pending:
            break
        time.sleep(0.02)
    assert exporter.pending is False
