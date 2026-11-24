"""A same-day re-quote is never swallowed (batch 13d). Set status Quoted on
a case that is already Quoted is a re-quote: a requote event with its date,
reason and note. Only a repeat with nothing new (same date, no reason, no
note) just confirms, and the page says so. Re-quoted always writes a round;
only the exact same click again within a minute is a double click.

Every name, number, date and note here is invented."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, tracking
from opplist import status as status_mod
from opplist.web import create_app

QUOTED_ON = "2026-11-03"          # a Tuesday


def events(conn, case):
    return [tuple(r) for r in conn.execute(
        "SELECT at, status, kind, note, reason FROM status_event WHERE enquiry_id = ? "
        "AND kind IN ('status','requote') AND voided = 0 AND status <> 'New' ORDER BY id", (case,))]


def view(conn, case):
    return conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case,)).fetchone()


@pytest.fixture
def quoted(repo):
    seed_reference(repo)
    party = add_party(repo, "Harbor Finch Freight", country="Chile")
    case = add_enquiry(repo, "OPP-2131", party, received="2026-10-29")
    repo.set_status(case, "Quoted", QUOTED_ON)
    repo.commit()
    return repo, case


@pytest.fixture
def web(tmp_path):
    path = tmp_path / "requote-13d.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Harbor Finch Freight", country="Chile")
    case = add_enquiry(repo, "OPP-2131", party, received="2026-10-29")
    fresh = add_enquiry(repo, "OPP-2132", party, received="2026-10-29")
    repo.set_status(case, "Quoted", QUOTED_ON)
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, case, fresh


def read(path, fn, *args):
    conn = db.open_db(path)
    try:
        return fn(conn, *args)
    finally:
        conn.close()


# ------------------------------------------------------- the repository ---
def test_quote_again_the_same_day_with_a_reason_writes_a_requote(quoted):
    repo, case = quoted
    assert repo.quote_again(case, QUOTED_ON, note="new pallet rate from the second carrier",
                            reason="Lower rate") == "written"
    repo.commit()
    assert events(repo.conn, case)[-1] == (
        QUOTED_ON, "Quoted", "requote", "Re-quote: new pallet rate from the second carrier", "Lower rate")
    assert view(repo.conn, case)["quote_rounds"] == 2


def test_quote_again_with_nothing_new_only_confirms(quoted):
    repo, case = quoted
    repo.update("enquiry", case, {"status_confirmed": 0})
    before = events(repo.conn, case)
    assert repo.quote_again(case, QUOTED_ON) == "confirmed"
    repo.commit()
    assert events(repo.conn, case) == before
    row = view(repo.conn, case)
    assert (row["quote_rounds"], row["status_confirmed"]) == (1, 1)


def test_quote_again_on_another_day_is_a_requote_even_without_words(quoted):
    repo, case = quoted
    assert repo.quote_again(case, "2026-11-06") == "written"
    assert events(repo.conn, case)[-1][:3] == ("2026-11-06", "Quoted", "requote")


def test_set_status_writes_a_same_day_repeat_that_brings_a_note(quoted):
    repo, case = quoted
    repo.set_status(case, "Quoted", QUOTED_ON, note="rate held until month end")
    assert repo.last_status_outcome == "written"
    assert len(events(repo.conn, case)) == 2                   # the quote and the repeat with a note
    repo.set_status(case, "Quoted", QUOTED_ON)                 # nothing new: confirm only
    assert repo.last_status_outcome == "confirmed"
    assert len(events(repo.conn, case)) == 2


def test_two_requotes_the_same_day_with_different_notes_are_two_rounds(quoted):
    repo, case = quoted
    repo.requote(case, "2026-11-05", note="option A via the north gateway")
    repo.requote(case, "2026-11-05", note="option B via the south gateway")
    repo.commit()
    kinds = [e[2] for e in events(repo.conn, case)]
    assert kinds == ["status", "requote", "requote"]
    assert view(repo.conn, case)["quote_rounds"] == 3
    assert [r["requote"] for r in status_mod.quote_rounds(repo.conn, case)] == [False, True, True]


def test_a_double_click_within_a_minute_writes_one_round(quoted):
    repo, case = quoted
    repo.requote(case, "2026-11-05", note="kite spools, 4 pallets", reason="Request changed")
    assert repo.last_status_outcome == "written"
    repo.requote(case, "2026-11-05", note="kite spools, 4 pallets", reason="Request changed")
    assert repo.last_status_outcome == "repeat"
    repo.commit()
    assert [e[2] for e in events(repo.conn, case)] == ["status", "requote"]
    # the same click more than a minute later is a new round
    repo.conn.execute("UPDATE status_event SET created_at = datetime('now','localtime','-5 minutes') "
                      "WHERE enquiry_id = ?", (case,))
    repo.requote(case, "2026-11-05", note="kite spools, 4 pallets", reason="Request changed")
    assert repo.last_status_outcome == "written"
    assert [e[2] for e in events(repo.conn, case)] == ["status", "requote", "requote"]


def test_the_fu_clock_restarts_from_the_same_day_requote(quoted):
    repo, case = quoted
    repo.add_followup(case, "2026-11-04")                     # FU1 of round one
    repo.quote_again(case, "2026-11-05", note="second offer", reason="Lower rate")
    repo.commit()
    row = view(repo.conn, case)
    assert (row["quote_rounds"], row["fu_count"]) == (2, 0)
    [item] = tracking.rule_quoted_no_answer(repo.conn, "2026-11-06")
    assert (item.extra["step"], item.since) == ("FU1", "2026-11-05")


# ------------------------------------------------------------ the pages ---
def test_set_status_quoted_again_the_same_day_with_a_reason_is_a_requote(web):
    client, path, case, fresh = web
    page = client.post("/enquiries/{}/status".format(case), data={
        "status": "Quoted", "at": QUOTED_ON, "requote_reason": "Lower rate",
        "note": "better offer from the second carrier"}, follow_redirects=True)
    body = page.data.decode("utf-8")
    assert page.status_code == 200
    assert "OPP-2131 was already Quoted: recorded as a re-quote on Nov 3 (round 2)." in body
    assert "Status set to Quoted" not in body
    assert read(path, events, case)[-1] == (
        QUOTED_ON, "Quoted", "requote", "Re-quote: better offer from the second carrier", "Lower rate")


def test_set_status_quoted_again_with_nothing_new_says_so(web):
    client, path, case, fresh = web
    page = client.post("/enquiries/{}/status".format(case), data={"status": "Quoted", "at": QUOTED_ON},
                       follow_redirects=True)
    body = page.data.decode("utf-8")
    assert ("Already Quoted on Nov 3; nothing new was recorded. "
            "Use Re-quoted for a revised quote.") in body
    assert "Status set to Quoted" not in body
    assert len(read(path, events, case)) == 1


def test_a_wrong_reason_is_refused_and_says_why(web):
    client, path, case, fresh = web
    page = client.post("/enquiries/{}/status".format(case), data={
        "status": "Quoted", "at": QUOTED_ON, "requote_reason": "Weather"}, follow_redirects=True)
    assert b"is not a re-quote reason" in page.data and b"Weather" in page.data
    assert len(read(path, events, case)) == 1
    # a re-quote reason sent with another status is ignored, not refused
    client.post("/enquiries/{}/status".format(fresh), data={
        "status": "Quoted", "at": QUOTED_ON, "requote_reason": "Weather"}, follow_redirects=True)
    assert read(path, events, fresh) == []                     # refused: not on the list
    page = client.post("/enquiries/{}/status".format(fresh), data={
        "status": "New", "at": QUOTED_ON, "requote_reason": "Lower rate"}, follow_redirects=True)
    assert b"Status set to New" in page.data and read(path, events, fresh) == []


def test_the_requoted_button_double_click_says_nothing_new(web):
    client, path, case, fresh = web
    form = {"at": "2026-11-05", "note": "two crates, revised size", "requote_reason": "Request changed"}
    first = client.post("/enquiries/{}/requote".format(case), data=form, follow_redirects=True)
    assert b"OPP-2131 re-quoted on 2026-11-05." in first.data
    again = client.post("/enquiries/{}/requote".format(case), data=form, follow_redirects=True)
    assert (b"OPP-2131 was already re-quoted on Nov 5 with the same reason and note a moment ago; "
            b"nothing new was recorded.") in again.data
    other = client.post("/enquiries/{}/requote".format(case), data=dict(form, note="three crates"),
                        follow_redirects=True)
    assert b"OPP-2131 re-quoted on 2026-11-05." in other.data
    assert [e[2] for e in read(path, events, case)] == ["status", "requote", "requote"]
    assert read(path, view, case)["quote_rounds"] == 3
