"""Batch 8 follow-up (2026-09-24): a New case is our turn and never falls
off Tracking. R1 = no live vendor request (whatever else happened on the
case), R2 = a silent vendor, R9 = for a New case any answered request,
for a Quoted case an answer newer than the last activity. Events a CLI
backfill wrote (source import: retire-sourcing ...) are no activity; the
cutover's events (source migration) are history and count (8b fix). In a
re-quote round R1 / R2 / R9 read only that round's vendor requests."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import checks, db, retire_sourcing, tracking
from opplist import status as status_mod
from opplist.web import create_app

TODAY = "2026-09-24"


def ids_of(items):
    return {i.enquiry_id for i in items}


def r1(conn):
    return ids_of(tracking.rule_new_no_action(conn, TODAY, 1))


def r2(conn):
    return ids_of(tracking.rule_vendor_silent(conn, TODAY, 2))


def r9(conn):
    return ids_of(tracking.rule_mail_activity(conn, TODAY))


@pytest.fixture
def world(tmp_path):
    """The five live shapes of batch 8 and a few more."""
    path = tmp_path / "coverage.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    agent = add_party(repo, "Tolvex", country="Chile")
    bqr = add_party(repo, "BQR", country="USA")
    ids = {"agent": agent, "bqr": bqr}

    def sourcing(opp, received, at):
        case = add_enquiry(repo, opp, agent, received=received)
        repo.insert("status_event", {"enquiry_id": case, "at": at, "status": "Sourcing", "source": "manual"})
        repo.update("enquiry", case, {"status": "Sourcing"})
        return case

    ids["0342"] = sourcing("OPP-1342", "2026-08-19", "2026-08-19")          # no vendor request
    ids["0370"] = sourcing("OPP-1370", "2026-08-26", "2026-08-26")          # only a voided one
    repo.insert("vendor_request", {"enquiry_id": ids["0370"], "vendor_party_id": bqr,
                                   "asked_on": "2026-08-27", "voided": 1})
    ids["0422"] = sourcing("OPP-1422", "2026-09-11", "2026-09-14")          # BQR answered Sep 16
    repo.insert("vendor_request", {"enquiry_id": ids["0422"], "vendor_party_id": bqr,
                                   "asked_on": "2026-09-14", "replied_on": "2026-09-16"})
    # OPP-1421: quoted, then set back to New by hand
    ids["0421"] = add_enquiry(repo, "OPP-1421", agent, received="2026-09-11")
    repo.set_status(ids["0421"], "Quoted", "2026-09-15")
    repo.set_status(ids["0421"], "New", "2026-09-23")
    # OPP-1437: one note
    ids["0437"] = add_enquiry(repo, "OPP-1437", agent, received="2026-09-16")
    repo.add_note(ids["0437"], "2026-09-21", "asked the client for dims")
    # a New case whose vendor answered long ago, with a newer note since
    ids["old_reply"] = add_enquiry(repo, "OPP-1480", agent, received="2026-09-01")
    repo.insert("vendor_request", {"enquiry_id": ids["old_reply"], "vendor_party_id": bqr,
                                   "asked_on": "2026-09-02", "replied_on": "2026-09-03"})
    repo.add_note(ids["old_reply"], "2026-09-20", "client still deciding")
    # a New case that came in today: R1's tomorrow
    ids["today"] = add_enquiry(repo, "OPP-1481", agent, received=TODAY)
    # a New case with no received date
    ids["undated"] = add_enquiry(repo, "OPP-1482", agent, received="2026-09-01")
    repo.update("enquiry", ids["undated"], {"received_on": None})
    # a New case asked yesterday: R2's in two days
    ids["asked_now"] = add_enquiry(repo, "OPP-1483", agent, received="2026-09-22")
    repo.insert("vendor_request", {"enquiry_id": ids["asked_now"], "vendor_party_id": bqr,
                                   "asked_on": "2026-09-23"})
    repo.commit()
    retire_sourcing.run(conn, dry_run=False, today=TODAY)
    yield conn, path, ids
    conn.close()


def test_the_live_shapes_land_on_a_card(world):
    conn, _, ids = world
    assert {ids["0342"], ids["0370"], ids["0421"], ids["0437"]} <= r1(conn)
    assert ids["0422"] in r9(conn) and ids["0422"] not in r1(conn)
    assert ids["undated"] in r1(conn)


def test_nothing_but_a_live_request_takes_a_case_out_of_r1(world):
    conn, _, ids = world
    repo = db.Repository(conn, source="test")
    case = ids["0437"]
    repo.add_note(case, "2026-09-22", "another note")
    repo.set_status(case, "New", "2026-09-23")
    repo.commit()
    assert case in r1(conn)
    vr = repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": ids["bqr"],
                                        "asked_on": "2026-09-18"})
    repo.commit()
    assert case not in r1(conn) and case in r2(conn)
    repo.update("vendor_request", vr, {"voided": 1})
    repo.commit()
    assert case in r1(conn)                                          # a voided request asks nobody


def test_r9_holds_a_new_case_with_any_answer(world):
    conn, _, ids = world
    assert ids["old_reply"] in r9(conn)                    # reply Sep 3, note Sep 20: still R9
    assert ids["old_reply"] not in r1(conn) and ids["old_reply"] not in r2(conn)


def test_r9_for_a_quoted_case_still_needs_a_newer_answer(world):
    conn, _, ids = world
    repo = db.Repository(conn, source="test")
    case = ids["old_reply"]
    repo.set_status(case, "Quoted", "2026-09-10")
    repo.commit()
    assert case not in r9(conn)                            # answered Sep 3, quoted Sep 10
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": ids["bqr"],
                                   "asked_on": "2026-09-21", "replied_on": "2026-09-22"})
    repo.commit()
    assert case in r9(conn)                                # a newer answer
    # a backfill's event is no activity: the answer stays newer
    repo.insert("status_event", {"enquiry_id": case, "at": "2026-09-23", "kind": "note",
                                 "status": "Quoted", "note": "fixed a lane", "source": "import"})
    repo.commit()
    assert case in r9(conn)
    repo.add_note(case, "2026-09-23", "told the client")    # a person's note is
    repo.commit()
    assert case not in r9(conn)


def test_system_events_are_not_last_activity(world, tmp_path):
    conn, path, ids = world
    case = ids["0342"]
    # the newest live event is retire-sourcing's New; Last activity skips it
    newest = conn.execute("SELECT note, source FROM status_event WHERE enquiry_id = ? "
                          "ORDER BY at DESC, id DESC LIMIT 1", (case,)).fetchone()
    assert tuple(newest) == (status_mod.SOURCING_RETIRED_NOTE, "import")
    assert tracking.last_activity(conn, case)["text"].startswith("Sourcing ")
    act = conn.execute("SELECT {} FROM enquiry e WHERE id = ?".format(tracking.activity_on_sql("e")),
                       (case,)).fetchone()[0]
    assert act == "2026-08-19"
    # the cutover's events are history: they are last activity like any other
    repo = db.Repository(conn, source="test")
    mig = add_enquiry(repo, "OPP-1490", ids["agent"], received="2026-09-01")
    repo.set_status(mig, "Quoted", "2026-09-05")
    conn.execute("UPDATE status_event SET source = 'migration' WHERE enquiry_id = ?", (mig,))
    conn.commit()
    assert tracking.last_activity(conn, mig)["text"] == "Quoted Sep 5"
    act = conn.execute("SELECT {} FROM enquiry e WHERE id = ?".format(tracking.activity_on_sql("e")),
                       (mig,)).fetchone()[0]
    assert act == "2026-09-05"
    # the Enquiries page shows the same last activity and sorts by it
    conn.commit()
    app = create_app(path)
    app.config["TESTING"] = True
    body = app.test_client().get("/enquiries?view=open&sort=last_event_on&dir=desc").get_data(as_text=True)
    row = body[body.index(">OPP-1342<"):]
    row = row[:row.index("</tr>")]
    assert "Aug 19" in row or "2026-08-19" in row
    assert "Sep 24" not in row and "2026-09-24" not in row


def test_the_invariant_holds_and_the_check_says_so(world):
    conn, _, ids = world
    assert tracking.new_cases_off_every_card(conn, TODAY) == []
    finding = checks.new_cases_off_tracking(conn, today=TODAY)
    assert finding.clean and not finding.informational
    assert finding in [f for f in checks.run(conn, today=TODAY) if f.key == "new_cases_off_tracking"]
    # every In, confirmed, open New case: R1, R2 or R9 - with the rules' own days
    # set aside, a case that came in today and one asked yesterday are covered too
    assert ids["today"] not in r1(conn) and ids["asked_now"] not in r2(conn)


def test_the_check_lists_a_case_that_falls_off(world, monkeypatch):
    conn, _, ids = world
    # break R9: a New case with an answered request then shows on no card
    monkeypatch.setattr(tracking, "rule_mail_activity", lambda conn, today: [])
    lost = tracking.new_cases_off_every_card(conn, TODAY)
    assert [r["opp_no"] for r in lost] == ["OPP-1480", "OPP-1422"] or \
        sorted(r["opp_no"] for r in lost) == ["OPP-1422", "OPP-1480"]
    finding = checks.new_cases_off_tracking(conn, today=TODAY)
    assert finding.count == 2 and not finding.clean
    text = "\n".join(checks.render([finding]))
    assert "[LOOK] 2" in text and "opp_no=OPP-1422" in text


def test_snoozed_and_held_cases_are_not_findings(world):
    conn, _, ids = world
    repo = db.Repository(conn, source="test")
    tracking.snooze(repo, "R1", "enquiry", ids["0437"], "2026-10-01")
    repo.update("enquiry", ids["0342"], {"hold_reason": "waiting", "hold_until": "2026-10-10"})
    repo.commit()
    assert tracking.new_cases_off_every_card(conn, TODAY) == []
    sections = {s.rule: s for s in tracking.run(conn, TODAY)}
    shown = {i.enquiry_id for i in sections["R1"].items}
    assert ids["0437"] not in shown and ids["0342"] not in shown     # asleep / held, not lost


def test_the_check_cli_prints_the_finding(world, capsys):
    conn, path, _ = world
    conn.close()
    from opplist.__main__ import main
    main(["check", "--db", str(path)])
    out = capsys.readouterr().out
    assert "New cases on none of R1, R2, R9" in out


def test_a_migrated_quote_is_not_older_than_a_same_day_answer(world):
    """OPP-1236 on the live DB: quoted Aug 11 by the cutover, BQR answered
    Aug 11. The quote counts, so the answer is not newer: not R9."""
    conn, _, ids = world
    repo = db.Repository(conn, source="test")
    case = add_enquiry(repo, "OPP-1236", ids["agent"], received="2026-07-08")
    repo.set_status(case, "Quoted", "2026-08-11")
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": ids["bqr"],
                                   "asked_on": "2026-08-11", "replied_on": "2026-08-11"})
    conn.execute("UPDATE status_event SET source = 'migration' WHERE enquiry_id = ?", (case,))
    conn.commit()
    assert case not in r9(conn)
    # and its R3 clock starts at the migrated quote
    r3 = {i.enquiry_id: i for i in tracking.rule_quoted_no_answer(conn, TODAY)}
    assert r3[case].since == "2026-08-11"


# ------------------------------------------------------------ re-quote rounds --
@pytest.fixture
def requoted(world):
    """Quoted after BQR answered, then an old-style re-quote (a requote
    event on New, as batch 8 wrote it - v30 writes Quoted, so only old data
    opens a round like this)."""
    conn, path, ids = world
    repo = db.Repository(conn, source="test")
    case = add_enquiry(repo, "OPP-1495", ids["agent"], received="2026-09-01")
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": ids["bqr"],
                                   "asked_on": "2026-09-02", "replied_on": "2026-09-03"})
    repo.set_status(case, "Quoted", "2026-09-04")
    repo.set_status(case, "New", "2026-09-10", note="Re-quote: 3 x 40HQ now", kind="requote")
    repo.commit()
    return conn, repo, case, ids


def test_a_new_round_with_no_request_is_r1(requoted):
    conn, repo, case, _ = requoted
    row = conn.execute("SELECT status, in_requote FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    assert tuple(row) == ("New", 1)
    assert case in r1(conn)
    assert case not in r9(conn)                          # the Sep 3 answer was the last round's
    assert case not in r2(conn)
    assert tracking.case_nudge(conn, conn.execute("SELECT * FROM v_enquiry WHERE id = ?",
                                                  (case,)).fetchone(), TODAY)["rule"] == "R1"
    assert tracking.new_cases_off_every_card(conn, TODAY) == []


def test_the_round_moves_through_r2_r9_to_quoted(requoted):
    conn, repo, case, ids = requoted
    vr = repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": ids["bqr"],
                                        "asked_on": "2026-09-11"})
    repo.commit()
    assert case in r2(conn) and case not in r1(conn) and case not in r9(conn)
    repo.update("vendor_request", vr, {"replied_on": "2026-09-15"})
    repo.commit()
    assert case in r9(conn) and case not in r1(conn) and case not in r2(conn)
    replied = {i.enquiry_id: i for i in tracking.rule_mail_activity(conn, TODAY)}[case]
    assert replied.since == "2026-09-15"                 # this round's answer
    repo.set_status(case, "Quoted", "2026-09-16")
    repo.commit()
    row = conn.execute("SELECT status, in_requote, quote_rounds FROM v_enquiry WHERE id = ?",
                       (case,)).fetchone()
    assert tuple(row) == ("Quoted", 0, 2)
    assert case not in r9(conn)                          # the answer is older than the quote


def test_a_silent_vendor_of_the_last_round_is_not_chased(requoted):
    conn, repo, case, ids = requoted
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": ids["bqr"],
                                   "asked_on": "2026-09-02"})          # never answered, last round
    repo.commit()
    assert case not in r2(conn) and case in r1(conn)
