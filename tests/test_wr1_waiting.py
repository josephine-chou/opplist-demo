"""Batch WR1 (2026-11-06, no schema change): Waiting for requester - a New
case that waits for the requester's details has its own follow-ups
.

1. the button writes one note, the status stays New, the case leaves R1
   and R12 lists it; pressing it again replaces the wait;
2. the ladder: FU1 one working day after the note, Log as sent moves it to
   FU2, Decide after that;
3. a reply, a vendor request, a status, Info received and striking the note
   out each end the wait; a vendor asked before the note does not;
4. the morning scan reads a waiting New case from the note and leaves a New
   case that is not waiting alone;
5. a quote's ladder does not count the chasers sent before the quote;
6. the text: each item of what-we-need on its own line;
7. the team site: case_doc carries 'wait' only while it is live.

Every company, person, mail, place and date here is invented; cases from
OPP-9001.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, detect_followups, firebase_sync, followup, mail_archive, parsing, suggestions, tracking, wait
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app

RECEIVED = "2026-11-09"            # a Monday
WAITED = "2026-11-10"              # the wait note, a Tuesday
NEED = "the dimensions of each piece; the pickup address"
SUBJECT = "RATE REQUEST // DRELLMOOR - VASTRO // 4 CRATES"
AGENT = ("tova@quillmerefreight.test", "Tova Rensk")
US = ("ines@jojofreight.example", "JJC-INES MARLO")


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    path = tmp_path / "wr1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    agent = add_party(repo, "Quillmere Freight", country="Chile")
    repo.insert("party_domain", {"domain": "quillmerefreight.test", "party_id": agent})
    vendor = add_party(repo, "Harrowgate Lines", ptype="Agent", country="USA")
    ids = {"path": path, "archive": archive, "vendor": vendor, "agent": agent}
    for opp in ("OPP-9001", "OPP-9002", "OPP-9003"):
        subject = "{} {}".format(SUBJECT, opp)
        ids[opp] = add_enquiry(repo, opp, agent, received=RECEIVED, source_subject=subject,
                               thread_key=parsing.subject_thread_key(subject), conversation_id="CONV-" + opp,
                               service_mode="Air", origin_country="USA", dest_country="Chile")
    repo.commit()
    app = create_app(path)
    app.config["TESTING"] = True
    yield conn, repo, ids, app.test_client()
    conn.close()


def press(client, case, what=NEED, at=WAITED):
    return client.post("/enquiries/{}/status".format(case), data={"status": "waiting", "what": what, "at": at},
                       follow_redirects=True)


def rows(conn, rule, today):
    return {item.enquiry_id: item for s in tracking.run(conn, today) if s.rule == rule for item in s.items}


def wait_notes(conn, case):
    return [tuple(r) for r in conn.execute(
        "SELECT note, voided, source, status FROM status_event WHERE enquiry_id = ? AND note LIKE ? ORDER BY id",
        (case, wait.PREFIX + "%"))]


# ------------------------------------------------------------ 1. the button --
def test_the_button_writes_one_note_and_the_case_moves_from_r1_to_r12(world):
    conn, repo, ids, client = world
    case = ids["OPP-9001"]
    assert case in rows(conn, "R1", "2026-11-12")
    page = client.get("/enquiries/{}".format(case)).data.decode("utf-8")
    assert 'data-act="waiting"' in page
    press(client, case)
    assert wait_notes(conn, case) == [("Waiting for requester: " + NEED, 0, "manual", "New")]
    assert conn.execute("SELECT status FROM enquiry WHERE id = ?", (case,)).fetchone()[0] == "New"
    assert case not in rows(conn, "R1", "2026-11-12")
    item = rows(conn, "R12", "2026-11-12")[case]
    assert (item.since, item.days, item.extra["what"]) == (WAITED, 2, NEED)
    assert tracking.new_cases_off_every_card(conn, "2026-11-12") == []
    # the Tracking page lists it, with what we need
    page = client.get("/tracking").data.decode("utf-8")
    assert 'id="wait"' in page and "the pickup address" in page and "Info received" in page


def test_pressing_it_again_replaces_the_wait_and_it_is_not_offered_on_a_quoted_case(world):
    conn, repo, ids, client = world
    case = ids["OPP-9001"]
    press(client, case)
    press(client, case, what="the commodity", at="2026-11-11")
    assert wait_notes(conn, case) == [("Waiting for requester: " + NEED, 1, "manual", "New"),
                                      ("Waiting for requester: the commodity", 0, "manual", "New")]
    assert wait.live(conn, case).what == "the commodity"
    quoted = ids["OPP-9002"]
    repo.set_status(quoted, "Quoted", "2026-11-10")
    repo.commit()
    assert 'data-act="waiting"' not in client.get("/enquiries/{}".format(quoted)).data.decode("utf-8")
    body = press(client, quoted).data.decode("utf-8")
    assert "Waiting for requester is for a New case" in body and wait_notes(conn, quoted) == []
    # what we need is required and has a length
    assert "Say what we need" in press(client, ids["OPP-9003"], what="  ").data.decode("utf-8")
    assert "keep it to 300" in press(client, ids["OPP-9003"], what="x" * 301).data.decode("utf-8")
    assert wait_notes(conn, ids["OPP-9003"]) == []


def test_tracking_r1_row_offers_the_button_prefilled_and_it_writes_the_note(world):
    conn, repo, ids, client = world
    case = ids["OPP-9001"]
    model = tracking.page_model(conn, "2026-11-12")
    r1 = next(c for c in model["today_cards"] if c["key"] == "r1")
    row = next(r for r in r1["rows"] if r["enquiry_id"] == case)
    assert "the dimensions of each piece" in row["wait_prefill"]           # what the acknowledgment asks
    client.post("/tracking/act", data={"do": "wait", "enquiry_id": case, "rule": "R1", "kind": "enquiry",
                                       "ref_id": case, "what": "the commodity; the cargo ready date", "at": WAITED})
    assert wait.live(conn, case).what == "the commodity; the cargo ready date"


# ------------------------------------------------------------- 2. the ladder --
def test_fu1_is_due_a_working_day_after_log_as_sent_moves_to_fu2_and_decide_comes_after(world):
    conn, repo, ids, client = world
    case = ids["OPP-9001"]
    press(client, case)
    assert rows(conn, "R12", "2026-11-10")[case].extra["step"] == ""
    assert rows(conn, "R12", "2026-11-11")[case].extra["step"] == "FU1"
    assert tracking.focus_count(conn, "2026-11-11") >= 1
    client.post("/tracking/act", data={"do": "followup", "enquiry_id": case, "rule": "R12", "kind": "enquiry",
                                       "ref_id": case, "at": "2026-11-11"})
    assert conn.execute("SELECT seq FROM status_event WHERE enquiry_id = ? AND kind = 'followup'",
                        (case,)).fetchone()[0] == 1
    item = rows(conn, "R12", "2026-11-17")[case]
    assert (item.extra["step"], item.extra["next"], item.extra["next_due"]) == ("", "FU2", "2026-11-18")
    assert rows(conn, "R12", "2026-11-18")[case].extra["step"] == "FU2"
    client.post("/enquiries/{}/followups".format(case), data={"step": "2", "at": "2026-11-18", "channel": "mail"})
    assert rows(conn, "R12", "2026-11-24")[case].extra["step"] == ""
    assert rows(conn, "R12", "2026-11-25")[case].extra["step"] == "Decide"
    rung = wait.ladder(conn, wait.live(conn, case), "2026-11-25")
    assert [s[0] for s in rung["steps"]] == ["Waiting", "FU1", "FU2", "Decide"]
    # the case page shows this ladder; FX4: no details chaser text any more, the log stays
    page = client.get("/enquiries/{}".format(case)).data.decode("utf-8")
    assert "waiting for requester since" in page and "To prepare our quotation" not in page
    assert 'id="fu-log"' in page and 'id="fu-text"' not in page


def test_the_decide_row_offers_lost_with_the_no_response_reason_cancelled_and_wait(world):
    conn, repo, ids, client = world
    case = ids["OPP-9001"]
    press(client, case)
    repo.add_followup(case, "2026-11-11", seq=1)
    repo.add_followup(case, "2026-11-18", seq=2)
    repo.commit()
    card = next(c for c in tracking.page_model(conn, "2026-11-25")["today_cards"] if c["key"] == "wait")
    row = card["rows"][0]
    assert (row["pill"], row["no_response"]) == ("Decide", "No response from agent")
    client.post("/tracking/act", data={"do": "quick:Lost", "enquiry_id": case, "rule": "R12", "kind": "enquiry",
                                       "ref_id": case, "at": "2026-11-25",
                                       "closed_reason": "No response from agent"})
    assert conn.execute("SELECT status, closed_reason FROM enquiry WHERE id = ?", (case,)).fetchone()[:] == \
        ("Lost", "No response from agent")
    assert wait.live(conn, case) is None


# ------------------------------------------------------------ 3. what ends it --
def test_a_reply_after_the_note_ends_the_wait_and_the_case_is_back_in_r1(world):
    conn, repo, ids, client = world
    case = ids["OPP-9001"]
    press(client, case)
    repo.add_note(case, "2026-11-09", "Reply from Tova (noted): thanks, will check", source="mail")   # before
    repo.commit()
    assert wait.live(conn, case) is not None
    repo.add_note(case, "2026-11-12", "Reply from Tova (noted): 4 crates 120x80x90 cm", source="mail")
    repo.commit()
    assert wait.live(conn, case) is None
    assert case in rows(conn, "R1", "2026-11-12") and case not in rows(conn, "R12", "2026-11-12")


def test_a_vendor_request_after_the_note_ends_it_one_before_does_not(world):
    conn, repo, ids, client = world
    case = ids["OPP-9001"]
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": ids["vendor"], "asked_on": "2026-11-09"})
    repo.commit()
    press(client, case)
    assert wait.live(conn, case) is not None
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": ids["vendor"], "asked_on": "2026-11-12"})
    repo.commit()
    assert wait.live(conn, case) is None


def test_a_status_ends_it(world):
    conn, repo, ids, client = world
    case = ids["OPP-9001"]
    press(client, case)
    repo.set_status(case, "Cancelled", "2026-11-12", closed_reason="Shipment cancelled")
    repo.commit()
    assert wait.live(conn, case) is None


def test_info_received_ends_it_from_the_case_page_and_from_tracking(world):
    conn, repo, ids, client = world
    case, other = ids["OPP-9001"], ids["OPP-9002"]
    press(client, case)
    press(client, other)
    client.post("/enquiries/{}/status".format(case), data={"status": "info_received"})
    assert wait.live(conn, case) is None
    assert conn.execute("SELECT note FROM status_event WHERE enquiry_id = ? ORDER BY id DESC LIMIT 1",
                        (case,)).fetchone()[0] == "Info received"
    client.post("/tracking/act", data={"do": "info_received", "enquiry_id": other, "rule": "R12",
                                       "kind": "enquiry", "ref_id": other})
    assert wait.live(conn, other) is None


def test_striking_the_note_out_ends_it(world):
    conn, repo, ids, client = world
    case = ids["OPP-9001"]
    press(client, case)
    note_id = wait.live(conn, case).event_id
    client.post("/enquiries/{}/events/{}".format(case, note_id), data={"action": "void"})
    assert wait.live(conn, case) is None
    assert case in rows(conn, "R1", "2026-11-12")


# -------------------------------------------------------------- 4. the scan --
def mail(at, sender, body, mid, opp, internal=False, to=(AGENT,)):
    return MailInfo(subject="RE: {} {}".format(SUBJECT, opp), received_on=at[:10], received_at=at, sent_at=at,
                    entry_id="EID-" + mid, conversation_id="CONV-" + opp, conversation_topic=SUBJECT,
                    sender_name=sender[1], sender_address=sender[0], sender_internal=internal,
                    to_addresses=[a for a, _ in to], body_excerpt=body, folder_path="\\Mailbox\\Inbox",
                    display_to="; ".join(n for _, n in to), message_id="<{}@wr1.test>".format(mid))


def store(ids, *mails):
    conn = mail_archive.open_archive(ids["archive"])
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in mails])
    conn.commit()
    conn.close()


def test_the_scan_reads_a_waiting_new_case_and_leaves_one_that_is_not_waiting(world):
    conn, repo, ids, client = world
    case, idle = ids["OPP-9001"], ids["OPP-9002"]
    press(client, case)
    chaser = "Hi Tova,\nThank you for your inquiry. To prepare our quotation, we still need the following:\n" \
             "- the dimensions of each piece\nThank you!\nInes"
    store(ids,
          mail("2026-11-09 15:00", US, "Hi Tova,\nWell received, we are on it.\nInes", "k1", "OPP-9001",
               internal=True),
          mail("2026-11-12 09:00", US, chaser, "c1", "OPP-9001", internal=True),
          mail("2026-11-13 10:00", AGENT, "Hi Ines,\nDims are 120x80x90 cm each.\nTova", "r1", "OPP-9001",
               to=(US,)),
          mail("2026-11-12 09:00", US, chaser, "c2", "OPP-9002", internal=True),
          mail("2026-11-13 10:00", AGENT, "Hi Ines,\nDims attached.\nTova", "r2", "OPP-9002", to=(US,)))
    with mail_archive.ArchiveSearch(ids["archive"]) as search:
        report = detect_followups.run(conn, search)
    assert (report.waits, report.followups, report.replies) == (1, 1, 1)
    # FX7: the chaser waits in Suggested updates, on our question; Accept writes it
    found = conn.execute("SELECT id, proposed_status FROM suggestion WHERE enquiry_id = ? AND kind = 'followup' "
                         "AND state = 'pending'", (case,)).fetchone()
    assert found["proposed_status"] == "question"
    assert conn.execute("SELECT COUNT(*) FROM status_event WHERE enquiry_id = ? AND kind = 'followup'",
                        (case,)).fetchone()[0] == 0
    suggestions.accept(db.Repository(conn, source="test"), found["id"])
    fu = conn.execute("SELECT at, seq, source, note FROM status_event WHERE enquiry_id = ? AND kind = 'followup'",
                      (case,)).fetchone()
    assert (fu["at"], fu["seq"], fu["source"]) == ("2026-11-12", 1, "mail") and "[mid:<c1@wr1.test>]" in fu["note"]
    reply = conn.execute("SELECT at, note FROM status_event WHERE enquiry_id = ? AND note LIKE 'Reply from%'",
                         (case,)).fetchone()
    assert reply["at"] == "2026-11-13" and "120x80x90" in reply["note"]
    assert wait.live(conn, case) is None                              # the reply ended it
    assert conn.execute("SELECT COUNT(*) FROM status_event WHERE enquiry_id = ? AND kind IN ('note','followup')",
                        (idle,)).fetchone()[0] == 0
    # a second run writes nothing again
    with mail_archive.ArchiveSearch(ids["archive"]) as search:
        again = detect_followups.run(conn, search)
    assert (again.waits, again.followups, again.replies) == (0, 0, 0)


# ------------------------------------------------- 5. the quote's ladder --
def test_a_quoted_cases_ladder_does_not_count_the_chasers_before_its_quote(world):
    conn, repo, ids, client = world
    case = ids["OPP-9001"]
    press(client, case)
    repo.add_followup(case, "2026-11-11", seq=1)
    repo.add_followup(case, "2026-11-12", seq=2)
    repo.set_status(case, "Quoted", "2026-11-13")
    repo.commit()
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    assert (row["fu_count"], row["last_fu_on"], row["fu_count_total"]) == (0, None, 2)
    rung = tracking.ladder(conn, row, "2026-11-16")
    assert (rung["next"], rung["next_seq"]) == ("FU1", 1)
    item = next(i for s in tracking.run(conn, "2026-11-16") if s.rule == "R3" for i in s.items)
    assert (item.enquiry_id, item.extra["step"], item.extra["fu_count"]) == (case, "FU1", 0)
    assert repo.add_followup(case, "2026-11-16") and conn.execute(
        "SELECT seq FROM status_event WHERE enquiry_id = ? AND at = '2026-11-16'", (case,)).fetchone()[0] == 1


# --------------------------------------------------------------- 6. the text --
# test_the_template_puts_each_item_on_its_own_line: FX4 (2026-11-12) took out the details
# chaser's template, its text and its Settings boxes (she has her own template tool).


# ----------------------------------------------------------- 7. the team site --
def test_case_doc_carries_wait_only_while_it_is_live(world):
    conn, repo, ids, client = world
    case = ids["OPP-9001"]
    built = firebase_sync.build(conn, "2026-11-11")
    assert "wait" not in built.cases["OPP-9001"]
    assert {r["opp"]: r["wait"] for r in built.inquiries}["OPP-9001"] == ""
    press(client, case)
    built = firebase_sync.build(conn, "2026-11-11")
    assert built.cases["OPP-9001"]["wait"] == {"since": WAITED, "what": NEED, "next": "FU1 due"}
    assert {r["opp"]: r["wait"] for r in built.inquiries}["OPP-9001"] == WAITED
    assert firebase_sync.build(conn, "2026-11-10").cases["OPP-9001"]["wait"]["next"] == ""
    assert "wait" not in built.cases["OPP-9002"]
    client.post("/enquiries/{}/status".format(case), data={"status": "info_received"})
    built = firebase_sync.build(conn, "2026-11-11")
    assert "wait" not in built.cases["OPP-9001"]
    assert {r["opp"]: r["wait"] for r in built.inquiries}["OPP-9001"] == ""
