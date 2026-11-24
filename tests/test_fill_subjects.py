"""Batch M1: cases with a thread key but no registered subject get their
subject from the archive (fill-subjects); and the month report's "none"
lines no longer climb into their headings.

Every company, person, subject, place and date here is invented; cases
from OPP-4301.
"""

from __future__ import annotations

import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, fill_subjects, mail_archive, monthly, monthly_export, parsing
from opplist.__main__ import main
from opplist.outlook_bridge import MailInfo

WINE = "Red grape juice ex works Talca to Busan (trial order)"
TILES = "RATE REQUEST // MIA - SAI // ceramic tiles"


def mail(subject, at, mid, name="Lin Okafor", sender="lin@brightwateragents.example"):
    return MailInfo(subject=subject, received_on=at[:10], received_at=at, entry_id="EID-" + mid,
                    conversation_id="CONV-" + mid, sender_name=name, sender_address=sender,
                    to_addresses=["desk@jojofreight.example"], body_excerpt="Hi,\n\nInvented text.\n",
                    message_id="<{}@x>".format(mid))


MAILS = [
    mail("RE: " + WINE, "2026-08-12 10:00", "w2"),
    mail("FW: " + WINE, "2026-08-04 09:00", "w1", name="Dov Hartwell", sender="dov@saltmarsh.example"),
    mail("RE: [OPP-4302] " + TILES, "2026-09-02 08:30", "t1"),
    mail("RE: [OPP-4302] " + TILES, "2026-09-05 08:30", "t2"),
]


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    a = mail_archive.open_archive(archive)
    mail_archive.store(a, [mail_archive.record_from_info(m) for m in MAILS])
    a.commit()
    a.close()
    path = tmp_path / "m1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Brightwater Agents", country="Chile")
    ids = {"path": path, "archive": archive}
    ids["wine"] = add_enquiry(repo, "OPP-4301", party, received="2026-08-04", status="Quoted",
                              thread_key=parsing.subject_thread_key(WINE))
    ids["tiles"] = add_enquiry(repo, "OPP-4302", party, received="2026-09-02",
                               thread_key=parsing.subject_thread_key(TILES))
    ids["lost"] = add_enquiry(repo, "OPP-4303", party, received="2026-07-01", thread_key="NOTHINGLIKEITINTHEARCHIVE")
    ids["has"] = add_enquiry(repo, "OPP-4304", party, received="2026-07-02", source_subject="Already here",
                             thread_key=parsing.subject_thread_key(WINE))
    repo.commit()
    yield conn, repo, ids
    conn.close()


def subject_of(conn, case):
    return conn.execute("SELECT source_subject, thread_key FROM enquiry WHERE id = ?", (case,)).fetchone()


def test_the_earliest_matching_mail_gives_the_subject_and_numbers_go(world):
    conn, repo, ids = world
    found = {f.opp_no: f for f in fill_subjects.propose(conn)}
    assert set(found) == {"OPP-4301", "OPP-4302", "OPP-4303"}               # OPP-4304 has a subject
    wine = found["OPP-4301"]
    assert (wine.subject, wine.mail_at, wine.sender, wine.why) == ("FW: " + WINE, "2026-08-04 09:00", "Dov Hartwell", "")
    assert found["OPP-4302"].subject == "RE: " + TILES                       # written without its number
    assert found["OPP-4303"].why == "no archived mail has this thread key" and not found["OPP-4303"].subject
    written = fill_subjects.apply(db.Repository(conn, source=fill_subjects.SOURCE), list(found.values()))
    conn.commit()
    assert written == 2
    assert tuple(subject_of(conn, ids["wine"])) == ("FW: " + WINE, parsing.subject_thread_key(WINE))
    assert subject_of(conn, ids["lost"])["source_subject"] is None
    logged = conn.execute("SELECT field, new_value, source FROM change_log WHERE table_name = 'enquiry' "
                          "AND row_id = ? AND field = 'source_subject'", (ids["wine"],)).fetchall()
    assert [tuple(r) for r in logged] == [("source_subject", "FW: " + WINE, "fill-subjects")]


def test_the_derived_key_guard_leaves_a_case_alone(world, monkeypatch):
    conn, repo, ids = world
    real = parsing.subject_thread_key

    def odd(subject):
        # as if the written subject (numbers taken out) derived another key
        return "SOMETHINGELSE" if subject == "RE: " + TILES else real(subject)
    monkeypatch.setattr(parsing, "subject_thread_key", odd)
    found = {f.opp_no: f for f in fill_subjects.propose(conn)}
    assert found["OPP-4302"].why == "the subject derives another key (SOMETHINGELSE)"
    fill_subjects.apply(db.Repository(conn, source=fill_subjects.SOURCE), list(found.values()))
    conn.commit()
    assert subject_of(conn, ids["tiles"])["source_subject"] is None


def test_the_dry_run_writes_nothing_and_apply_takes_a_restore_point(world, capsys, tmp_path):
    conn, repo, ids = world
    conn.close()
    assert main(["fill-subjects", "--db", str(ids["path"]), "--archive", str(ids["archive"])]) == 0
    out = capsys.readouterr().out
    assert "cases with a thread key and no subject: 3; a subject found for 2, left alone 1" in out
    assert "OPP-4301  Quoted    FW: " + WINE in out and "dry run: nothing written" in out
    check = db.open_db(ids["path"])
    assert check.execute("SELECT COUNT(*) FROM enquiry WHERE COALESCE(source_subject, '') <> ''").fetchone()[0] == 1
    check.close()
    restore = tmp_path / "rp"
    assert main(["fill-subjects", "--db", str(ids["path"]), "--archive", str(ids["archive"]), "--apply",
                 "--restore-dir", str(restore)]) == 0
    out = capsys.readouterr().out
    assert "subjects written: 2" in out and list(restore.glob("opplist_before_fill-subjects_*.db"))
    check = db.open_db(ids["path"])
    assert check.execute("SELECT COUNT(*) FROM enquiry WHERE COALESCE(source_subject, '') <> ''").fetchone()[0] == 3
    check.close()


# ------------------------------------------- the month report's 'none' lines --
def test_a_month_without_lost_cases_keeps_its_none_line_under_its_heading(world):
    conn, repo, ids = world
    html = monthly_export.render_html(monthly.build(conn, "2026-03"))
    assert "No lost cases this month." in html
    # 17a: the report's 'none' lines have one rule, no negative margin
    css = monthly_export.css()
    assert re.search(r"(^|\s)\.none\{[^}]*margin:0 0 12px", css)
    assert not re.search(r"\.none\{[^}]*margin:-", css)
