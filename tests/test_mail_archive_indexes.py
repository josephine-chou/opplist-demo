"""Batch F2c: the archive's lookups use indexes - by_subject reads ids from
ix_message_subject, every lookup by message_id uses ux_message_message_id,
by_entry_id's fallback uses ix_location_entry; the answers are the old ones.

Every mail here is invented.
"""

from __future__ import annotations

import sqlite3

import pytest

from opplist import mail_archive
from opplist.outlook_bridge import MailInfo

SUBJECTS = [
    "RATE REQUEST // 40' EXW // TACOMA",
    "QUOTE 100% LIQUID CARGO",
    "REF ABC_123 // LCL",
    "PATH C:\\SHIPPING\\DOCS",
    "COTIZACIÓN TRANSPORTE MARÍTIMO // ARICA",
    "cotización transporte marítimo // iquique",
    "Re: plain words",
]


def info(n, subject, mid=True, folder="\\Mailbox\\Inbox"):
    return MailInfo(subject=subject, received_on="2026-11-01",
                    received_at="2026-11-{:02d} {:02d}:{:02d}".format(1 + n // 600, (n // 60) % 10, n % 60),
                    entry_id="EID-{}".format(n), conversation_id="CONV-{}".format(n % 7),
                    sender_name="Wren Ashby", sender_address="wren@heathcotelines.example", body_excerpt="Body {}".format(n),
                    folder_path=folder, message_id="<m{}@x>".format(n) if mid else "")


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "mail.db"
    conn = mail_archive.open_archive(path)
    mails = [info(i, s) for i, s in enumerate(SUBJECTS)]
    mails += [info(100 + i, "BULK NOTICE {} // TALCAHUANO".format(i)) for i in range(620)]
    mails.append(info(900, "NO HEADER ID", mid=False))
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in mails])
    conn.commit()
    conn.close()
    return path


def old_query(path, term):
    """by_subject as it was before F2c: the table scanned, bodies and all."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM message WHERE subject LIKE ? ESCAPE '\\' ORDER BY received_at",
                        (mail_archive._like(term),)).fetchall()
    conn.close()
    wanted = term.strip().casefold()
    return [r["message_id"] or r["entry_id"] for r in rows if wanted in (r["subject"] or "").casefold()]


@pytest.mark.parametrize("term", ["40' EXW", "100%", "ABC_123", "C:\\SHIPPING", "MARÍTIMO", "marítimo",
                                  "TALCAHUANO", "nothing like it"])
def test_by_subject_gives_the_old_query_s_mails_in_its_order(archive, term):
    with mail_archive.ArchiveSearch(archive) as search:
        got = [m.message_id or m.entry_id for m in search.by_subject(term, limit=None)]
    assert got == old_query(archive, term)
    if term == "TALCAHUANO":
        assert len(got) == 620                                     # more than one chunk of ids


def plan(conn, sql, params):
    return " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_the_queries_use_the_indexes(archive):
    conn = mail_archive.open_archive(archive, create=False)
    try:
        assert "COVERING INDEX ix_message_subject" in plan(
            conn, "SELECT id FROM message WHERE subject LIKE ? ESCAPE '\\'", ("%TACOMA%",))
        for sql in ("SELECT id FROM message WHERE message_id = ? AND message_id <> ''",
                    "SELECT entry_id FROM message WHERE message_id = ? AND message_id <> ''",
                    "SELECT * FROM message WHERE message_id = ? AND message_id <> '' ORDER BY received_at"):
            assert "ux_message_message_id" in plan(conn, sql, ("<m1@x>",)), sql
        join = plan(conn, "SELECT m.* FROM message_location l JOIN message m ON m.message_id = l.message_id "
                          "AND m.message_id <> '' WHERE l.entry_id = ? LIMIT 1", ("EID-1",))
        assert "ix_location_entry" in join and "ux_message_message_id" in join
    finally:
        conn.close()


def test_the_lookups_answer_as_before(archive):
    with mail_archive.ArchiveSearch(archive) as search:
        assert search.by_message_id("<m3@x>").subject == SUBJECTS[3]
        assert search.by_message_id("") is None
        assert search.entry_ids_of(search.by_message_id("<m2@x>")) == {"EID-2"}
        assert search.by_entry_id("EID-900").subject == "NO HEADER ID"            # no Message-ID: by its id
    # a mail filed elsewhere: its old entry id finds it through message_location
    conn = mail_archive.open_archive(archive)
    mail_archive.store(conn, [mail_archive.record_from_info(info(4, SUBJECTS[4], folder="\\Mailbox\\Inbox\\AGENTS"))])
    conn.execute("UPDATE message_location SET entry_id = 'EID-FILED' WHERE message_id = '<m4@x>' "
                 "AND folder_path LIKE '%AGENTS'")
    conn.commit()
    conn.close()
    with mail_archive.ArchiveSearch(archive) as search:
        assert search.by_entry_id("EID-FILED").message_id == "<m4@x>"


def test_an_archive_without_the_indexes_gets_them_on_open(tmp_path):
    path = tmp_path / "old.db"
    conn = mail_archive.open_archive(path)
    conn.execute("DROP INDEX ix_message_subject")
    conn.execute("DROP INDEX ix_location_entry")
    conn.commit()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert not {"ix_message_subject", "ix_location_entry"} & names
    version = mail_archive.get_meta(conn, "schema_version")
    conn.close()
    conn = mail_archive.open_archive(path, create=False)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"ix_message_subject", "ix_location_entry"} <= names
    assert mail_archive.get_meta(conn, "schema_version") == version              # no version change
    conn.close()
