"""Re-quoted (v30, 2026-09-25): the button records that a revised
quote went out. One event - status Quoted, kind requote, the date entered,
the reason (optional) and the note - written the same way from the case
page, the Tracking Replied row and an accepted suggestion. The case stays
Quoted; the re-quote is the second quote round."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference, form_nesting
from opplist import dashboard, db, detect_followups, monthly, propose, suggestions, tracking
from opplist import status as status_mod
from opplist.web import create_app


def view(conn, enquiry_id):
    return conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()


def last_event(conn, case):
    return conn.execute(
        "SELECT at, status, kind, note, reason, source FROM status_event WHERE enquiry_id = ? "
        "ORDER BY id DESC LIMIT 1", (case,)).fetchone()


@pytest.fixture
def quoted(repo):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1601", party, received="2026-07-01")
    repo.set_status(case, "Quoted", "2026-07-10")
    repo.add_followup(case, "2026-07-14")                 # FU1 of round one
    repo.commit()
    return repo, case


# ------------------------------------------------------- the repository ---
def test_requoted_writes_one_quoted_event_and_the_case_stays_quoted(quoted):
    repo, case = quoted
    before = repo.conn.execute("SELECT COUNT(*) FROM status_event WHERE enquiry_id = ?", (case,)).fetchone()[0]
    repo.requote(case, "2026-07-20", note="3 x 40HQ instead of 2", reason="Request changed")
    repo.commit()
    assert repo.conn.execute("SELECT COUNT(*) FROM status_event WHERE enquiry_id = ?",
                             (case,)).fetchone()[0] == before + 1
    assert tuple(last_event(repo.conn, case)) == (
        "2026-07-20", "Quoted", "requote", "Re-quote: 3 x 40HQ instead of 2", "Request changed", "manual")
    row = view(repo.conn, case)
    assert (row["status"], row["status_confirmed"], row["quote_rounds"], row["in_requote"]) == ("Quoted", 1, 2, 0)
    assert row["quoted_on"] == "2026-07-10"                             # the first quote stays the Date Quoted
    assert repo.conn.execute("SELECT COUNT(*) FROM status_event WHERE enquiry_id = ? AND status = 'New' "
                             "AND kind = 'requote'", (case,)).fetchone()[0] == 0


def test_an_empty_note_is_just_the_word(quoted):
    repo, case = quoted
    repo.requote(case, "2026-07-20", note="   ")
    assert last_event(repo.conn, case)["note"] == "Re-quote"


def test_the_round_is_read_off_the_quoted_events(quoted):
    repo, case = quoted
    repo.requote(case, "2026-07-20", reason="Lower rate")
    repo.commit()
    rounds = status_mod.quote_rounds(repo.conn, case)
    assert [(r["at"], r["requote"], r["reason"]) for r in rounds] == [
        ("2026-07-10", False, None), ("2026-07-20", True, "Lower rate")]


def test_the_follow_ups_restart_at_fu1_one_working_day_after(quoted):
    repo, case = quoted
    repo.requote(case, "2026-07-20")                       # a Monday
    repo.commit()
    row = view(repo.conn, case)
    assert (row["fu_count"], row["fu_count_total"]) == (0, 1)          # a new round
    assert tracking.rule_quoted_no_answer(repo.conn, "2026-07-20") == []  # the same day: waiting
    [item] = tracking.rule_quoted_no_answer(repo.conn, "2026-07-21")
    assert (item.extra["step"], item.since) == ("FU1", "2026-07-20")
    repo.add_followup(case, "2026-07-21")
    repo.commit()
    row = view(repo.conn, case)
    assert (row["fu_count"], row["last_fu_seq"]) == (1, 1)            # FU1 again
    assert [r["seq"] for r in repo.conn.execute(
        "SELECT seq FROM status_event WHERE enquiry_id = ? AND kind = 'followup' ORDER BY id",
        (case,))] == [1, 1]


def test_a_case_never_quoted_has_no_round(repo):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1602", party, received="2026-07-01")
    repo.set_status(case, "New", "2026-07-02")
    repo.commit()
    row = view(repo.conn, case)
    assert (row["quote_rounds"], row["in_requote"]) == (0, 0)


def test_a_case_shaped_like_opp_0314_reads_as_round_two(repo):
    """OPP-1314 on the live data: migrated New and Quoted in July, FU1, a
    requote event the old button wrote on Sourcing and she set to Quoted
    (17 Aug), then FU2. Not edited: it reads as round 2, not in a round."""
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1314", party, received="2026-07-16")
    repo.set_status(case, "Quoted", "2026-07-17")
    repo.add_followup(case, "2026-07-24")
    requote = repo.insert("status_event", {"enquiry_id": case, "at": "2026-08-17", "status": "Sourcing",
                                           "kind": "requote", "reason": "Request changed",
                                           "note": "Re-quote: new figures for a single crate"})
    repo.update("status_event", requote, {"status": "Quoted"})
    repo.insert("status_event", {"enquiry_id": case, "at": "2026-09-23", "status": "Quoted",
                                 "kind": "followup", "seq": 2, "channel": "mail", "note": "FU2 sent"})
    conn = repo.conn
    conn.execute("UPDATE status_event SET source = 'migration' WHERE enquiry_id = ? AND kind = 'status'", (case,))
    repo.commit()
    row = view(conn, case)
    assert (row["status"], row["quote_rounds"], row["in_requote"]) == ("Quoted", 2, 0)
    assert (row["quoted_on"], row["fu_count"], row["last_fu_on"]) == ("2026-07-17", 1, "2026-09-23")
    assert [r["reason"] for r in status_mod.quote_rounds(conn, case)] == [None, "Request changed"]


def test_an_old_requote_on_new_is_still_an_open_round(repo):
    """What batch 8 and v19 to v28 wrote (a requote on New or Sourcing):
    in_requote until the next Quoted."""
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1603", party, received="2026-07-01")
    repo.set_status(case, "Quoted", "2026-07-10")
    repo.set_status(case, "New", "2026-07-20", kind="requote", note="Re-quote")
    repo.commit()
    assert (view(repo.conn, case)["status"], view(repo.conn, case)["in_requote"]) == ("New", 1)
    repo.set_status(case, "Quoted", "2026-07-22")
    repo.commit()
    row = view(repo.conn, case)
    assert (row["status"], row["in_requote"], row["quote_rounds"]) == ("Quoted", 0, 2)


def test_the_dashboard_and_the_monthly_report_count_it(quoted):
    repo, case = quoted
    repo.requote(case, "2026-07-20", reason="Lower rate")
    repo.commit()
    dash = dashboard.build(repo.conn, today="2026-07-21")
    assert dash.pipeline["on_them"] == 1 and dash.pipeline["on_us"] == 0     # waiting on them
    # batch 17a: the month report no longer counts re-quotes by reason (the
    # reason stays on the case and its timeline)


# ----------------------------------------------------- mail suggestions ---
def test_an_accepted_requote_suggestion_writes_the_same_event(quoted):
    repo, case = quoted
    suggestions.record(repo, case, "OPP-1601", {
        "kind": "status", "proposed_status": "Quoted", "closed_reason": None,
        "proposed_date": "2026-07-21", "evidence_sender": "JJC-VICTOR LANG <VictorLang@jojofreight.example>",
        "evidence_date": "2026-07-21", "evidence_quote": "O/F USD 1,100 per 40HQ",
        "confidence": "medium", "note": detect_followups.REQUOTED_NOTE, "source": "scan"})
    repo.commit()
    pending = repo.conn.execute("SELECT id FROM suggestion WHERE enquiry_id = ?", (case,)).fetchone()
    suggestions.accept(repo, pending["id"])
    repo.commit()
    event = last_event(repo.conn, case)
    assert (event["status"], event["kind"], event["source"], event["reason"]) == ("Quoted", "requote", "mail", None)
    assert event["note"] == "Re-quote: JJC-VICTOR LANG <VictorLang@jojofreight.example> O/F USD 1,100 per 40HQ"
    row = view(repo.conn, case)
    assert (row["status"], row["quote_rounds"], row["in_requote"]) == ("Quoted", 2, 0)


def test_the_proposal_note_carries_the_mark():
    assert propose.REQUOTE_NOTE.startswith("Re-quote: ")
    assert propose.is_requote_note("Re-quote: they asked for the rate again")
    assert propose.is_requote_note("re-quote")
    assert not propose.is_requote_note("we quoted and nothing came back")


# ------------------------------------------------------------ the pages ---
@pytest.fixture
def web(tmp_path):
    path = tmp_path / "requote-web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1601", party, received="2026-07-01", origin_loc="LAX",
                       origin_country="USA", dest_loc="SAI", dest_country="Chile")
    repo.set_status(case, "Quoted", "2026-07-10")
    repo.add_note(case, "2026-07-20", "Reply from Renata Solis (needs an answer): can you "
                                      "update the rate to 3 x 40HQ? [mid:<r@x>]", source="mail")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, case


def test_the_case_page_records_a_re_quote(web):
    client, path, case = web
    body = client.get("/enquiries/{}".format(case)).data.decode("utf-8")
    assert 'id="requote-form"' in body and "/requote" in body and ">Re-quoted</" in body
    assert "re-quote · round" not in body                     # one quote so far

    page = client.post("/enquiries/{}/requote".format(case),
                       data={"at": "2026-07-25", "note": "3 x 40HQ", "requote_reason": "Request changed"},
                       follow_redirects=True)
    assert page.status_code == 200 and b"OPP-1601 re-quoted on 2026-07-25." in page.data
    body = page.data.decode("utf-8")
    assert 'id="requote-form"' in body                        # still Quoted: can re-quote again
    assert "re-quote · round 2" in body                       # the pill
    assert "Re-quote: 3 x 40HQ" in body                       # on the timeline
    assert '<span class="pill step">re-quote &middot; Request changed</span>' in body

    conn = db.open_db(path)
    row = view(conn, case)
    assert (row["status"], row["in_requote"], row["quote_rounds"]) == ("Quoted", 0, 2)
    assert tuple(last_event(conn, case))[:5] == ("2026-07-25", "Quoted", "requote", "Re-quote: 3 x 40HQ",
                                                 "Request changed")
    conn.close()
    # the list shows the round after the status (2026-09-25)
    listing = client.get("/enquiries?view=open").data.decode("utf-8")
    assert '<span class="lbl fill tone-accent">round 2</span>' in listing


def test_re_quote_on_a_case_that_is_not_quoted_is_refused(web):
    client, path, case = web
    client.post("/enquiries/{}/status".format(case), data={"status": "New", "at": "2026-07-24"})
    page = client.post("/enquiries/{}/requote".format(case), data={"at": "2026-07-26"},
                       follow_redirects=True)
    assert b"Re-quote is for a quoted case" in page.data
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM status_event WHERE enquiry_id = ? "
                        "AND kind = 'requote'", (case,)).fetchone()[0] == 0
    conn.close()


# test_the_tracking_row_writes_the_same_event_and_leaves_replied: replaced in batch C1 by tests/test_cancelled_counts.py (it pinned the old Lost / cancelled words).


def test_no_page_nests_the_new_form(web):
    client, path, case = web
    for path_ in ("/enquiries/{}".format(case), "/enquiries?view=open", "/tracking"):
        deepest, ids = form_nesting(client.get(path_).data.decode("utf-8"))
        assert deepest <= 1, (path_, ids)
