"""One mail, several folders (message_location): the case folder wins as the
primary place, and searches by folder see every copy."""

from __future__ import annotations

import datetime as dt

from opplist import mail_archive
from opplist.outlook_bridge import FolderInfo
from test_mail_archive import NOW, info, load

SENT = "\\Personal Folder\\Sent Items"
CASE = "\\Personal Folder\\TOLVEX\\OPP-1307 (Vetrora glass)"
INBOX = "\\Mailbox\\Inbox"


def locations(conn, key):
    return [(r["folder_path"], r["entry_id"]) for r in conn.execute(
        "SELECT folder_path, entry_id FROM message_location WHERE message_id = ? "
        "ORDER BY first_seen_at, rowid", (key,))]


def primary(conn):
    row = conn.execute("SELECT folder_path, entry_id, store FROM message").fetchone()
    return row["folder_path"], row["entry_id"], row["store"]


def test_the_case_folder_becomes_the_primary_place(tmp_path):
    conn = mail_archive.open_archive(tmp_path / "mail.db")
    key = "<dup@x>"
    load(conn, [info("quote", entry="E-SENT", folder=SENT, message_id=key)])
    assert primary(conn) == (SENT, "E-SENT", "Personal Folder")
    assert locations(conn, key) == [(SENT, "E-SENT")]

    later = NOW + dt.timedelta(hours=1)
    assert load(conn, [info("quote", entry="E-CASE", folder=CASE, message_id=key)],
                now=later) == (0, 1)
    assert primary(conn) == (CASE, "E-CASE", "Personal Folder")
    assert locations(conn, key) == [(SENT, "E-SENT"), (CASE, "E-CASE")]

    # seen in Sent Items again (a later sync): the case folder stays primary
    assert load(conn, [info("quote", entry="E-SENT-2", folder=SENT, message_id=key)],
                now=later + dt.timedelta(days=1)) == (0, 1)
    assert primary(conn) == (CASE, "E-CASE", "Personal Folder")
    assert locations(conn, key) == [(SENT, "E-SENT-2"), (CASE, "E-CASE")]
    row = conn.execute("SELECT last_seen_at FROM message_location WHERE folder_path = ?",
                       (SENT,)).fetchone()
    assert row["last_seen_at"] == "2026-09-15 11:00:00"
    assert mail_archive.location_stats(conn) == {"messages": 1, "locations": 2,
                                                 "multi": 1, "max": 2}


def test_without_a_case_folder_the_first_place_stays_primary(tmp_path):
    conn = mail_archive.open_archive(tmp_path / "mail.db")
    key = "<two@x>"
    load(conn, [info("x", entry="E1", folder=INBOX, message_id=key)])
    load(conn, [info("x", entry="E2", folder=SENT, message_id=key)])
    assert primary(conn)[:2] == (INBOX, "E1")
    # a mail without a Message-ID is keyed on its entry id in both tables
    no_id = info("no header", entry="E-ONLY", folder=INBOX, message_id=" ")
    no_id.message_id = ""
    load(conn, [no_id])
    assert locations(conn, "E-ONLY") == [(INBOX, "E-ONLY")]
    assert mail_archive.is_case_path("\\Personal Folder\\KEEL\\-0311")
    assert mail_archive.is_case_path(CASE) and not mail_archive.is_case_path(SENT)


def test_a_version_one_archive_gets_its_locations_filled_in(tmp_path):
    path = tmp_path / "mail.db"
    conn = mail_archive.open_archive(path)
    load(conn, [info("a", entry="E1", folder=CASE, message_id="<a@x>"),
                info("b", entry="E2", folder=INBOX, message_id="<b@x>")])
    conn.execute("DELETE FROM message_location")            # what a v1 file has
    mail_archive.set_meta(conn, "schema_version", "1")
    conn.commit()
    conn.close()
    conn = mail_archive.open_archive(path)
    assert mail_archive.get_meta(conn, "schema_version") == "2"
    assert locations(conn, "<a@x>") == [(CASE, "E1")]
    assert locations(conn, "<b@x>") == [(INBOX, "E2")]
    conn.close()


def test_folder_searches_see_every_copy(tmp_path):
    path = tmp_path / "mail.db"
    conn = mail_archive.open_archive(path)
    key = "<dup@x>"
    load(conn, [info("RE: RATE REQUEST // Vetrora", entry="E-CASE", folder=CASE,
                     message_id=key, body="please proceed 001-4817 5362")])
    load(conn, [info("RE: RATE REQUEST // Vetrora", entry="E-SENT", folder=SENT,
                     message_id=key, body="please proceed 001-4817 5362")])
    load(conn, [info("other", entry="E-O", folder=INBOX, message_id="<o@x>",
                     body="001-4817 5362 elsewhere")])
    conn.close()
    with mail_archive.ArchiveSearch(path) as search:
        assert primary_of(search, key) == CASE
        assert {f.path for f in search.folders()} == {CASE, SENT, INBOX}
        assert [f.path for f in search.case_folders("OPP-1307")] == [CASE]
        # the copy in Sent Items is found under Sent Items, with its own entry id
        found = search.in_folder(FolderInfo(SENT))
        assert [(m.entry_id, m.folder_path) for m in found] == [("E-SENT", SENT)]
        assert found[0].body_excerpt.startswith("please proceed")
        found = search.in_folder(FolderInfo(CASE))
        assert [(m.entry_id, m.folder_path) for m in found] == [("E-CASE", CASE)]
        assert search.in_folder(FolderInfo("\\nowhere")) == []

        # a keyword search limited to Sent Items still finds the shared mail
        result = mail_archive.search_terms(search._conn, ["001-48175362"], days_back=0,
                                           folders=["*Sent Items*"])
        assert [h.subject for h in result.hits] == ["RE: RATE REQUEST // Vetrora"]
        assert result.folders == 3
        result = mail_archive.search_terms(search._conn, ["001-48175362"], days_back=0)
        assert len(result.hits) == 2


def primary_of(search, key):
    return search._conn.execute("SELECT folder_path FROM message WHERE message_id = ?",
                                (key,)).fetchone()[0]
