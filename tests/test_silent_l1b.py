"""Batch L1b: R11 counts from the quote date; each case's reading is kept
until the case or the archive changes. (L1b's section 12 test went with
the section in 17a-2; its closed September cases stay as cases R11 skips.)

A test archive; every company, person, mail and date here is invented;
cases from OPP-5131 (OPP-5121 to 5123 are the CLI test's in
test_silent_cases.py).
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, mail_archive, parsing, silent
from opplist.outlook_bridge import MailInfo

TODAY = "2026-11-20"
US = ("desk@jojofreight.example", "JJC-DESK")
AGENT = ("ops@saltfern.example", "Oona Salt")


def mail(opp, at, sender, mid, internal=False):
    subject = "RATE REQUEST // {} // LCL".format(opp)
    return MailInfo(subject=("RE: " if internal else "") + subject, received_on=at[:10], received_at=at,
                    entry_id="EID-" + mid, conversation_id="CONV-" + opp, conversation_topic=parsing.clean_subject(subject),
                    sender_name=sender[1], sender_address=sender[0], sender_internal=internal,
                    to_addresses=[AGENT[0] if internal else US[0]], body_excerpt="Hi,\nAbout the rate.",
                    folder_path="\\Mailbox\\Inbox", message_id="<{}@x>".format(mid))


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    monkeypatch.setattr(silent, "_READINGS", {})
    conn = db.open_db(tmp_path / "l1b.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    party = add_party(repo, "Saltfern Cargo", country="Chile")
    repo.insert("party_domain", {"domain": "saltfern.example", "party_id": party})
    ids, mails = {}, []

    def case(opp, quoted, status=None, reason=None):
        ids[opp] = add_enquiry(repo, opp, party, received=quoted, source_subject="RATE REQUEST // {} // LCL".format(opp),
                               conversation_id="CONV-" + opp, service_mode="LCL", origin_country="USA",
                               dest_country="Chile")
        repo.set_status(ids[opp], "Quoted", quoted)
        mails.append(mail(opp, quoted + " 15:00", US, "q" + opp, internal=True))
        if status:
            repo.set_status(ids[opp], status, "2026-10-20", closed_reason=reason)

    case("OPP-5131", "2026-10-20")                                         # 31 days, our follow-up 3 days ago
    repo.add_followup(ids["OPP-5131"], "2026-11-17")
    mails.append(mail("OPP-5131", "2026-11-17 09:00", US, "fOPP-5131", internal=True))
    case("OPP-5132", "2026-10-22")                                         # 29 days
    # September's quotes, no requester mail on any thread
    case("OPP-5133", "2026-09-10", "Won")
    case("OPP-5134", "2026-09-11", "Lost", "Price too high")
    case("OPP-5135", "2026-09-12", "Lost", silent.NO_RESPONSE)
    case("OPP-5136", "2026-09-14")
    repo.commit()
    aconn = mail_archive.open_archive(archive)
    mail_archive.store(aconn, [mail_archive.record_from_info(m) for m in mails])
    mail_archive.set_meta(aconn, "last_sync_at", TODAY + " 07:00:00")
    aconn.commit()
    aconn.close()
    yield {"conn": conn, "repo": repo, "ids": ids, "archive": archive}
    conn.close()


def test_the_clock_runs_from_the_quote_date(world):
    w = world
    listed = [c["row"]["opp_no"] for c in silent.cached(w["conn"], TODAY)]
    assert "OPP-5131" in listed and "OPP-5132" not in listed
    one = next(c for c in silent.cached(w["conn"], TODAY) if c["row"]["opp_no"] == "OPP-5131")
    assert (one["days"], one["quoted"]) == (31, "2026-10-20")


def test_each_case_s_reading_is_kept_until_the_case_or_the_archive_changes(world, monkeypatch):
    w = world
    reads = []
    real = silent.reading

    def counting(conn, search, row, internal):
        reads.append(row["opp_no"])
        return real(conn, search, row, internal)

    monkeypatch.setattr(silent, "reading", counting)
    silent.cached(w["conn"], TODAY)
    assert sorted(reads) == ["OPP-5131", "OPP-5132", "OPP-5136"]           # the open quoted cases
    reads.clear()
    opened = []
    real_enter = mail_archive.ArchiveSearch.__enter__
    monkeypatch.setattr(mail_archive.ArchiveSearch, "__enter__", lambda self: opened.append(1) or real_enter(self))
    silent.cached(w["conn"], TODAY)
    assert reads == [] and opened == []                                  # no thread read, no archive opened
    w["conn"].execute("UPDATE enquiry SET updated_at = '2031-01-01 00:00:00' WHERE opp_no = 'OPP-5132'")
    w["conn"].commit()
    silent.cached(w["conn"], TODAY)
    assert reads == ["OPP-5132"]
    reads.clear()
    w["repo"].add_followup(w["ids"]["OPP-5131"], "2026-11-19")            # a follow-up does not change the reading
    w["repo"].commit()
    silent.cached(w["conn"], TODAY)
    assert reads == []
    aconn = mail_archive.open_archive(w["archive"])
    mail_archive.set_meta(aconn, "last_sync_at", TODAY + " 12:00:00")
    aconn.commit()
    aconn.close()
    silent.cached(w["conn"], TODAY)
    assert sorted(reads) == ["OPP-5131", "OPP-5132", "OPP-5136"]           # an archive sync reads them all again
