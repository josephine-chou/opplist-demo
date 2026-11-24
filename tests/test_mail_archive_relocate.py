"""mail-sync --relocate: keys and places only, and the primary-folder rule
'most recently seen case folder first'."""

from __future__ import annotations

import datetime as dt

import pytest

from opplist import mail_archive
from opplist.outlook_bridge import FolderInfo
from test_mail_archive import NOW, FakeItem, FakeOutlook, FakeOutlookFolder, info, load
from test_mail_archive_locations import CASE, INBOX, SENT, locations, primary

CASE2 = "\\Personal Folder\\TOLVEX\\OPP-1399 (Re-filed)"


@pytest.fixture
def outlook(monkeypatch):
    FakeOutlook.folders_to_serve = []
    FakeOutlook.fail_connects = 0
    FakeOutlook.connects = 0
    monkeypatch.setattr(mail_archive.outlook_bridge, "Search", FakeOutlook)
    monkeypatch.setattr(mail_archive, "SYNC_LOG", None)
    return FakeOutlook


def test_the_most_recently_seen_case_folder_is_primary(tmp_path):
    conn = mail_archive.open_archive(tmp_path / "mail.db")
    key = "<dup@x>"
    load(conn, [info("quote", entry="E-SENT", folder=SENT, message_id=key)])
    load(conn, [info("quote", entry="E-CASE", folder=CASE, message_id=key)],
         now=NOW + dt.timedelta(hours=1))
    assert primary(conn)[:2] == (CASE, "E-CASE")
    # re-filed into another case folder a day later: that one is primary now
    load(conn, [info("quote", entry="E-CASE2", folder=CASE2, message_id=key)],
         now=NOW + dt.timedelta(days=1))
    assert primary(conn)[:2] == (CASE2, "E-CASE2")
    # the first case folder seen again later still wins on recency
    load(conn, [info("quote", entry="E-CASE-B", folder=CASE, message_id=key)],
         now=NOW + dt.timedelta(days=2))
    assert primary(conn)[:2] == (CASE, "E-CASE-B")
    assert len(locations(conn, key)) == 3


def test_relocate_only_moves_places_and_reads_no_bodies(tmp_path, outlook):
    path = tmp_path / "mail.db"
    conn = mail_archive.open_archive(path)
    load(conn, [info("quote", entry="E-SENT", folder=SENT, message_id="<m@x>",
                     body="please proceed")])
    conn.execute("INSERT INTO folder_sync(folder_path, position, last_synced_at) "
                 "VALUES (?, 0, ?)", (SENT, "2026-09-13 08:00:00"))
    conn.commit()
    conn.close()

    def _no_body(self):                       # --relocate must never read this
        raise AssertionError("body read during relocate")

    class LightItem(FakeItem):
        Body = property(_no_body, lambda self, value: None)

    outlook.folders_to_serve = [
        FolderInfo(CASE, FakeOutlookFolder([
            LightItem("quote", message_id="<m@x>", received=(2026, 9, 10, 9, 0, 0), entry="E-CASE"),
            LightItem("not archived", message_id="<zz@x>", received=(2026, 9, 10, 9, 0, 0), entry="E-ZZ"),
        ])),
        FolderInfo(SENT, FakeOutlookFolder([
            LightItem("quote", message_id="<m@x>", received=(2026, 9, 10, 9, 0, 0), entry="E-SENT"),
        ])),
    ]
    seen = []
    report = mail_archive.sync(db_path=path, now=NOW, retry_wait=0, relocate=True,
                               progress=lambda r, why: seen.append((r.path, why)))
    assert report.ok and report.relocate and report.days_back == 30
    assert (report.seen, report.new, report.updated, report.moved, report.skipped) == (3, 1, 1, 1, 1)
    assert seen[0] == (CASE, "30 days")
    assert "mail-sync --relocate ok: 2 folder(s), 3 mails looked at, 1 place(s) added, " \
           "1 refreshed, 1 primary moved, 1 not in the archive" in report.summary()

    conn = mail_archive.open_archive(path)
    assert primary(conn)[:2] == (CASE, "E-CASE")
    assert locations(conn, "<m@x>") == [(SENT, "E-SENT"), (CASE, "E-CASE")]
    assert conn.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 1     # <zz@x> not added
    assert conn.execute("SELECT body FROM message").fetchone()[0] == "please proceed"
    # folder_sync is not the relocate's to touch; the daily sync keeps its own window
    row = conn.execute("SELECT last_synced_at FROM folder_sync WHERE folder_path = ?", (SENT,)).fetchone()
    assert row["last_synced_at"] == "2026-09-13 08:00:00"
    assert mail_archive.get_meta(conn, "last_sync_at") is None
    assert mail_archive.get_meta(conn, "last_relocate_at") is not None
    conn.close()


def test_relocate_from_the_command_line_with_folders(tmp_path, outlook, monkeypatch, capsys):
    from opplist.__main__ import main
    path = tmp_path / "mail.db"
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", path)
    # the command line reads the clock; the fake mails are dated 4 Sep, so
    # the 30-day window is pinned to NOW (14 Sep), not to the day the test runs
    monkeypatch.setattr(mail_archive, "_now", lambda now=None: now or NOW)
    conn = mail_archive.open_archive(path)
    load(conn, [info("quote", entry="E-SENT", folder=SENT, message_id="<m@x>")])
    conn.close()
    outlook.folders_to_serve = [
        FolderInfo(CASE, FakeOutlookFolder([FakeItem("quote", message_id="<m@x>", entry="E-CASE")])),
        FolderInfo(INBOX, FakeOutlookFolder([FakeItem("other", message_id="<o@x>", entry="E-O")])),
    ]
    assert main(["mail-sync", "--relocate", "--folders", "*TOLVEX*,*Nowhere*"]) == 0
    printed = capsys.readouterr().out
    assert "mode         : relocate" in printed and "30 days" in printed
    assert "folders      : *TOLVEX*, *Nowhere*" in printed
    assert "1 folder(s), 1 mails looked at, 1 place(s) added" in printed
    conn = mail_archive.open_archive(path)
    assert primary(conn)[:2] == (CASE, "E-CASE")
    assert conn.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 1
    conn.close()


def test_a_partial_sync_does_not_count_as_up_to_date(tmp_path, outlook):
    path = tmp_path / "mail.db"
    outlook.folders_to_serve = [
        FolderInfo(INBOX, FakeOutlookFolder([FakeItem("a", message_id="<a@x>", entry="E-A")])),
        FolderInfo(SENT, FakeOutlookFolder([FakeItem("b", message_id="<b@x>", entry="E-B")])),
    ]
    report = mail_archive.sync(db_path=path, now=NOW, retry_wait=0, folders=["*Inbox*"])
    assert report.ok and report.new == 1 and [f.path for f in report.folders] == [INBOX]
    conn = mail_archive.open_archive(path)
    assert mail_archive.get_meta(conn, "last_sync_at") is None
    conn.close()
    report = mail_archive.sync(db_path=path, now=NOW, retry_wait=0)
    conn = mail_archive.open_archive(path)
    assert mail_archive.get_meta(conn, "last_sync_at") is not None
    conn.close()
