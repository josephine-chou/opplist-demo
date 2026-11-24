"""fill-subjects (batch M1): cases with a thread key but no registered
subject get their subject from the mail archive.

Migrated cases took their thread key from thread-opp-map but no
source_subject, so their Mail card never had a subject to search the
archive with (case_mail.mails_for searches by the cleaned subject). For
each such case the archive's subjects are read once; the earliest mail
whose subject's thread key equals the case's gives the subject, as written
and without case numbers. The key that subject derives must be the case's
key, else the case is left alone and listed. Nothing else on the case
changes; thread_key stays.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import mail_archive, parsing

SOURCE = "fill-subjects"


@dataclass
class Found:
    enquiry_id: int
    opp_no: str
    status: str
    thread_key: str
    subject: str = ""             # what would be written
    mail_at: str = ""
    sender: str = ""
    why: str = ""                 # '' when it can be written, else why not


def _archive_rows(archive_path=None) -> list:
    path = Path(archive_path or mail_archive.ARCHIVE_DB)
    if not path.exists():
        raise mail_archive.ArchiveError("No mail archive at {}".format(path))
    conn = sqlite3.connect("file:{}?mode=ro".format(path.as_posix()), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT subject, sender_name, sender_address, "
            "COALESCE(NULLIF(received_at, ''), NULLIF(sent_at, ''), '') AS at FROM message "
            "WHERE subject <> ''").fetchall()
    finally:
        conn.close()


def propose(conn, archive_path=None) -> list:
    """[Found] for every case with a thread key and no source subject."""
    cases = conn.execute(
        "SELECT id, opp_no, status, thread_key FROM enquiry WHERE COALESCE(thread_key, '') <> '' "
        "AND COALESCE(TRIM(source_subject), '') = '' ORDER BY CAST(substr(opp_no, 5) AS INTEGER)").fetchall()
    if not cases:
        return []
    wanted = {c["thread_key"] for c in cases}
    earliest = {}
    for row in _archive_rows(archive_path):
        key = parsing.subject_thread_key(row["subject"])
        if key not in wanted:
            continue
        have = earliest.get(key)
        if have is None or (row["at"] or "9999") < (have["at"] or "9999"):
            earliest[key] = row
    out = []
    for case in cases:
        found = Found(case["id"], case["opp_no"], case["status"], case["thread_key"])
        mail = earliest.get(case["thread_key"])
        if mail is None:
            found.why = "no archived mail has this thread key"
        else:
            subject = " ".join(parsing.strip_case_numbers(mail["subject"]).split())
            found.subject, found.mail_at = subject, (mail["at"] or "")[:16]
            found.sender = mail["sender_name"] or mail["sender_address"] or ""
            if parsing.subject_thread_key(subject) != case["thread_key"]:
                found.why = "the subject derives another key ({})".format(parsing.subject_thread_key(subject))
        out.append(found)
    return out


def apply(repo, found: list) -> int:
    """Write source_subject for the ones that can be; the number written."""
    done = 0
    for f in found:
        if f.why or not f.subject:
            continue
        row = repo.conn.execute("SELECT source_subject FROM enquiry WHERE id = ?", (f.enquiry_id,)).fetchone()
        if row is None or (row["source_subject"] or "").strip():
            continue
        repo.update("enquiry", f.enquiry_id, {"source_subject": f.subject})
        repo.touch("enquiry", f.enquiry_id)
        done += 1
    return done


def lines(found: list) -> list:
    out = []
    for f in found:
        if f.why:
            out.append("  {:<9} {:<9} left alone: {}".format(f.opp_no, f.status, f.why))
        else:
            out.append("  {:<9} {:<9} {}  ({}, {})".format(f.opp_no, f.status, f.subject, f.mail_at, f.sender))
    return out
