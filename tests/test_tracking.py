"""The tracking rules, and the snooze that silences them."""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import tracking

TODAY = "2026-09-09"          # a Wednesday


def by_rule(sections):
    return {section.rule: section for section in sections}


def counts(conn, today=TODAY):
    return {rule: section.count
            for rule, section in by_rule(tracking.run(conn, today=today)).items()}


@pytest.fixture
def party(repo):
    seed_reference(repo)
    party_id = add_party(repo, "Tolvex", country="Chile")
    repo.commit()
    return party_id


# ------------------------------------------------------------ counting ----
def test_business_days_skip_the_weekend():
    # Friday to Monday is one working day, not three.
    assert tracking.business_days_between("2026-09-04", "2026-09-07") == 1
    assert tracking.business_days_between("2026-09-07", "2026-09-11") == 4
    assert tracking.business_days_between("2026-09-09", "2026-09-09") == 0
    assert tracking.business_days_between("2026-09-10", "2026-09-09") is None
    assert tracking.business_days_between(None, "2026-09-09") is None


def test_calendar_days_are_plain_days():
    assert tracking.calendar_days_between("2026-09-04", "2026-09-07") == 3


# ------------------------------------------------------------ the rules ---
def test_r1_new_with_nothing_done(repo, conn, party):
    add_enquiry(repo, "OPP-0001", party, received="2026-09-04")   # Friday
    add_enquiry(repo, "OPP-0002", party, received="2026-09-08")   # yesterday
    repo.commit()
    section = by_rule(tracking.run(conn, today=TODAY))["R1"]
    # rule_new_days is 1: yesterday's case is not late yet.
    assert [item.label for item in section.items] == ["OPP-0001"]
    assert section.items[0].days == 3


def test_r1_leaves_a_case_alone_once_a_vendor_has_been_asked(repo, conn, party):
    case = add_enquiry(repo, "OPP-0001", party, received="2026-09-01")
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": party,
                                   "asked_on": "2026-09-02"})
    repo.commit()
    assert counts(conn)["R1"] == 0


def test_r2_a_vendor_that_has_not_come_back(repo, conn, party):
    case = add_enquiry(repo, "OPP-0001", party, status="New",
                       received="2026-09-01")
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": party,
                                   "asked_on": "2026-09-03"})
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": party,
                                   "asked_on": "2026-09-03",
                                   "replied_on": "2026-09-04"})
    repo.commit()
    section = by_rule(tracking.run(conn, today=TODAY))["R2"]
    assert section.count == 1                 # the one that answered is gone
    assert section.items[0].kind == "vendor_request"
    assert "Tolvex has not replied" in section.items[0].detail


def test_r3_quoted_and_then_silence(repo, conn, party):
    """Quoted on 08-20 and nothing since: the days are working days from
    the quote, and past the whole ladder (11) it is a decision, not a
    first follow-up."""
    case = add_enquiry(repo, "OPP-0001", party, received="2026-08-01")
    repo.set_status(case, "Quoted", "2026-08-20")
    repo.commit()
    section = by_rule(tracking.run(conn, today=TODAY))["R3"]
    assert [(item.days, item.since, item.extra["step"]) for item in section.items] == \
        [(14, "2026-08-20", "Decide")]


def test_r4_r5_and_r6(repo, conn, party):
    add_enquiry(repo, "OPP-0001", party, received="2026-09-01",
                status_confirmed=0)
    tbc = add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    add_enquiry(repo, "OPP-0002", tbc, status="New", received="2026-09-02")
    won = add_enquiry(repo, "OPP-0003", party, status="Won",
                      received="2026-08-01")
    repo.insert("vendor_request", {"enquiry_id": won, "vendor_party_id": party,
                                   "asked_on": "2026-08-02",
                                   "replied_on": "2026-08-03"})
    repo.commit()
    found = counts(conn)
    assert (found["R4"], found["R5"], found["R6"]) == (1, 1, 1)


def test_r5_ignores_the_placeholder_that_was_given_up_on(repo, conn, party):
    unknown = add_party(repo, "Unknown", ptype="Unknown", country=None,
                        placeholder=1)
    add_enquiry(repo, "OPP-0001", unknown, status="New",
                received="2026-09-01")
    repo.commit()
    assert counts(conn)["R5"] == 0


def test_r7_a_party_that_has_gone_quiet(repo, conn, party):
    quiet = add_party(repo, "Quietwater", country="USA")
    repo.insert("party_event", {"party_id": quiet, "at": "2026-06-01",
                                "type": "outreach"})
    repo.insert("party_event", {"party_id": party, "at": "2026-09-01",
                                "type": "reply"})
    repo.commit()
    section = by_rule(tracking.run(conn, today=TODAY))["R7"]
    assert [item.label for item in section.items] == ["Quietwater"]
    assert section.items[0].days == 100


def test_r7_leaves_alone_a_party_we_have_never_dealt_with(repo, conn):
    """An imported agent list is prospecting work, not a quiet relationship."""
    seed_reference(repo)
    add_party(repo, "Never Contacted", country="USA")
    repo.commit()
    assert counts(conn)["R7"] == 0


def test_r8_a_lane_that_needs_fixing(repo, conn, party):
    repo.add_location("SAN VICENTE", country="Chile")     # status To Confirm
    add_enquiry(repo, "OPP-0001", party, received="2026-09-01",
                origin_country="USA")                     # country, no location
    add_enquiry(repo, "OPP-0002", party, received="2026-09-01",
                origin_loc="SAN VICENTE", origin_country="Chile")
    add_enquiry(repo, "OPP-0003", party, received="2026-09-01",
                origin_loc="LAX", origin_country="USA",
                dest_loc="SAI", dest_country="Chile")
    repo.commit()
    section = by_rule(tracking.run(conn, today=TODAY))["R8"]
    assert sorted(item.label for item in section.items) == ["OPP-0001", "OPP-0002"]


def test_r9_a_vendor_answered_after_the_last_thing_on_the_case(repo, conn, party):
    case = add_enquiry(repo, "OPP-0001", party, status="New",
                       received="2026-09-01")
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": party,
                                   "asked_on": "2026-09-02",
                                   "replied_on": "2026-09-08"})
    repo.commit()
    assert counts(conn)["R9"] == 1


def test_legacy_never_appears(repo, conn, party):
    add_enquiry(repo, "OPP-0001", party, received="2026-01-01", legacy=1,
                status_confirmed=0)
    repo.commit()
    found = counts(conn)
    assert found["R1"] == 0 and found["R4"] == 0 and found["R8"] == 0


# ------------------------------------------------------------- snoozing ---
def test_a_snoozed_item_stays_away_until_its_date(repo, conn, party):
    case = add_enquiry(repo, "OPP-0001", party, received="2026-09-01")
    repo.commit()
    assert counts(conn)["R1"] == 1

    tracking.snooze(repo, "R1", "enquiry", case, "2026-09-11")
    repo.commit()
    assert counts(conn)["R1"] == 0                     # today is the 9th
    assert counts(conn, today="2026-09-12")["R1"] == 1  # and back on the 12th


def test_a_snooze_only_silences_its_own_rule(repo, conn, party):
    case = add_enquiry(repo, "OPP-0001", party, received="2026-09-01",
                       status_confirmed=0, origin_country="USA")
    repo.commit()
    tracking.snooze(repo, "R4", "enquiry", case, "2026-09-30")
    repo.commit()
    found = counts(conn)
    assert found["R4"] == 0 and found["R8"] == 1


def test_snoozing_twice_moves_the_date_rather_than_adding_a_row(repo, conn, party):
    case = add_enquiry(repo, "OPP-0001", party, received="2026-09-01")
    repo.commit()
    tracking.snooze(repo, "R1", "enquiry", case, "2026-09-11")
    tracking.snooze(repo, "R1", "enquiry", case, "2026-09-20")
    repo.commit()
    rows = conn.execute("SELECT until FROM tracking_snooze").fetchall()
    assert [r["until"] for r in rows] == ["2026-09-20"]


def test_snooze_until_counts_plain_days():
    assert tracking.snooze_until(3, today="2026-09-09") == "2026-09-12"


# ---------------------------------------------------------------- output --
def test_the_bands_keep_the_backlog_out_of_the_way(repo, conn, party):
    add_enquiry(repo, "OPP-0001", party, received="2026-09-01")       # R1
    add_enquiry(repo, "OPP-0002", party, received="2026-09-01",
                status_confirmed=0)                                   # R4
    repo.commit()
    sections = tracking.run(conn, today=TODAY)
    bands = {section.rule: section.band for section in sections if section.count}
    assert bands == {"R1": "today", "R4": "backlog"}
    assert tracking.total(sections) == 2


def test_an_empty_list_says_so(repo, conn, party):
    assert tracking.render(tracking.run(conn, today=TODAY)) == [
        "Nothing to chase today."]


# ------------------------------------ a note is activity, not a date ------
def test_a_note_does_not_hand_the_export_a_date_it_never_had(repo, conn, party):
    """A note on a Won case must not become its closing date (schema v5)."""
    case = add_enquiry(repo, "OPP-0001", party, status="Won",
                       received="2026-08-01")
    # The migration's closing date is approximate, so nothing is exported.
    conn.execute("UPDATE status_event SET approx = 1 WHERE enquiry_id = ?",
                 (case,))
    repo.insert("status_event", {"enquiry_id": case, "at": "2026-08-20",
                                 "status": "Won", "approx": 1})
    repo.commit()
    before = conn.execute(
        "SELECT closed_on, closed_on_any, last_event_on FROM v_enquiry "
        "WHERE id = ?", (case,)).fetchone()
    assert before["closed_on"] is None

    repo.add_note(case, "2026-09-09", "customer asked for the BL")
    repo.commit()
    after = conn.execute(
        "SELECT closed_on, closed_on_any, last_event_on, status FROM v_enquiry "
        "WHERE id = ?", (case,)).fetchone()
    assert after["closed_on"] is None            # still no real closing date
    assert after["closed_on_any"] == before["closed_on_any"]
    assert after["status"] == "Won"
    # But something did happen, so the tracking clock restarted.
    assert after["last_event_on"] == "2026-09-09"


def test_a_note_does_not_become_a_quote_date(repo, conn, party):
    case = add_enquiry(repo, "OPP-0001", party, status="Quoted",
                       received="2026-08-01")
    # Both events approximate, the way a migrated case with no dates looks.
    conn.execute("UPDATE status_event SET approx = 1 WHERE enquiry_id = ?",
                 (case,))
    repo.insert("status_event", {"enquiry_id": case, "at": "2026-08-20",
                                 "status": "Quoted", "approx": 1})
    repo.commit()
    repo.add_note(case, "2026-09-09", "chased")
    repo.commit()
    row = conn.execute("SELECT quoted_on, quoted_on_any FROM v_enquiry "
                       "WHERE id = ?", (case,)).fetchone()
    assert row["quoted_on"] is None
    # The earliest real Quoted event, never the note.
    assert row["quoted_on_any"] == "2026-08-01"


def test_a_note_stops_r3_asking_again(repo, conn, party):
    case = add_enquiry(repo, "OPP-0001", party, status="Quoted",
                       received="2026-07-01")
    repo.insert("status_event", {"enquiry_id": case, "at": "2026-07-10",
                                 "status": "Quoted"})
    repo.commit()
    assert counts(conn)["R3"] == 1
    repo.add_note(case, TODAY, "chased by phone")
    repo.commit()
    assert counts(conn)["R3"] == 0


def test_the_kind_column_only_takes_the_two_words(repo, conn, party):
    import sqlite3

    case = add_enquiry(repo, "OPP-0001", party)
    repo.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO status_event (enquiry_id, at, status, kind) "
                     "VALUES (?, '2026-09-09', 'New', 'memo')", (case,))


# ------------------------------------------ the 2026-09-15 page layout ---
@pytest.fixture
def web(tmp_path):
    """A web client over a small db with a party, the placeholder and the
    Won / Lost reasons the row forms list."""
    from opplist import db as db_mod
    from opplist.web import create_app

    path = tmp_path / "tracking-web.db"
    conn = db_mod.open_db(path)
    repo = db_mod.Repository(conn, source="test")
    seed_reference(repo)
    party_id = add_party(repo, "Tolvex", country="Chile")
    add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    db_mod.set_setting(conn, "closed_reasons_lost",
                   ["Price too high", "No response from customer", "Other"])
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, party_id


def _open(path):
    from opplist import db as db_mod
    return db_mod.open_db(path)


def _stale(path, party_id, opp_no="OPP-1501", status="New", received="2026-08-01", **extra):
    from opplist import db as db_mod
    conn = _open(path)
    repo = db_mod.Repository(conn, source="test")
    case = add_enquiry(repo, opp_no, party_id, status=status, received=received, **extra)
    repo.commit()
    conn.close()
    return case


# test_the_focus_panels_say_nothing_here_when_empty: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).


def test_a_focus_row_has_one_main_action_and_more(web):
    client, path, party_id = web
    case = _stale(path, party_id)                               # R1, 5+ weeks old
    body = client.get("/tracking").data.decode("utf-8")
    assert 'value="quick:Quoted"' in body                        # Quoted
    assert '/enquiries/{}#vendors'.format(case) in body          # Ask vendor
    assert 'value="quick:Dropped"' in body                       # Drop... -> Confirm
    assert 'value="quick:Sourcing"' not in body                  # no Sourcing shortcut
    assert '<details class="more">' in body
    assert 'href="#r1"><div class="t-l">New, untouched</div>' in body and 'data-tile="r1">1</div>' in body
    assert "<b>Tolvex</b>" in body and "? - ?" in body            # requester and the dotted lane line
    assert 'class="days late"' in body                           # 3+ working days: red
    # the nav badge counts R1 + R3
    assert '<span class="badge" title="R1 + R3">1</span>' in body
    assert '<span class="badge" title="R1 + R3">1</span>' in client.get("/enquiries").data.decode("utf-8")


# test_quick_status_writes_the_status_and_confirms_it: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).


def test_quick_lost_without_a_reason_is_refused(web):
    client, path, party_id = web
    case = _stale(path, party_id, status="Quoted", received="2026-08-01")
    response = client.post("/tracking/act", data={
        "do": "quick:Lost", "rule": "R3", "kind": "enquiry", "ref_id": case,
        "enquiry_id": case, "at": "2026-09-15", "closed_reason": ""}, follow_redirects=True)
    assert b"Lost needs a reason" in response.data
    conn = _open(path)
    assert conn.execute("SELECT status FROM enquiry WHERE id = ?", (case,)).fetchone()["status"] == "Quoted"
    conn.close()
    # with a reason - from either of the row's two reason lists - it goes through
    response = client.post("/tracking/act", data={
        "do": "quick:Lost", "rule": "R3", "kind": "enquiry", "ref_id": case,
        "enquiry_id": case, "at": "2026-09-15", "closed_reason": ["", "Price too high"]},
        follow_redirects=True)
    assert b"set to Lost" in response.data
    conn = _open(path)
    row = conn.execute("SELECT status, closed_reason FROM enquiry WHERE id = ?", (case,)).fetchone()
    assert (row["status"], row["closed_reason"]) == ("Lost", "Price too high")
    conn.close()


def test_a_quick_status_nobody_knows_is_refused(web):
    client, path, party_id = web
    case = _stale(path, party_id)
    response = client.post("/tracking/act", data={
        "do": "quick:Deleted", "rule": "R1", "kind": "enquiry", "ref_id": case,
        "enquiry_id": case}, follow_redirects=True)
    assert b"Pick a status first" in response.data


# test_a_focus_row_shows_its_pending_suggestion_with_accept: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).


# test_the_cli_text_puts_the_focus_rules_first moved to test_silent_cases.py with invented data (L1: R11 joins the list)


# test_late_is_three_working_days_for_r1_and_fourteen_days_for_r3: FX4 leaves a hidden card's rows
# out of the badge; rewritten with invented data in tests/test_fx4_rewritten.py.


def _many_quoted(path, party_id, count):
    """`count` R3 rows, quoted one day apart so the sort is unambiguous."""
    from opplist import db as db_mod
    conn = _open(path)
    repo = db_mod.Repository(conn, source="test")
    for n in range(count):
        case = add_enquiry(repo, "OPP-16{:02d}".format(n), party_id, status="Quoted",
                           received="2026-06-01")
        repo.insert("status_event", {"enquiry_id": case, "status": "Quoted",
                                     "at": (dt.date(2026, 7, 1) + dt.timedelta(days=n)).isoformat()})
    repo.commit()
    conn.close()


# test_a_focus_panel_shows_ten_rows_and_a_show_all_link: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).


# test_show_all_opens_one_focus_panel_only: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).


# ------------------------------------------------- R3 reads in four groups --
def _days_ago(n):
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


def _r3_world(path, party_id, replied=1, fu1=1, fu2=1, decide=1):
    """Cases sitting on each step of the ladder, dated from today so the
    test does not care which day it runs on."""
    from opplist import db as db_mod
    from opplist import detect_followups

    conn = _open(path)
    repo = db_mod.Repository(conn, source="test")
    made = {}
    for n in range(replied):
        case = add_enquiry(repo, "OPP-81{:02d}".format(n), party_id, received=_days_ago(60))
        repo.set_status(case, "Quoted", _days_ago(30))
        repo.add_note(case, _days_ago(4), detect_followups.reply_note(
            "Ana", "Can you match the competitor on this one?", "<r{}@x>".format(n)), source="mail")
        made.setdefault("Replied", []).append("OPP-81{:02d}".format(n))
    for n in range(fu1):
        case = add_enquiry(repo, "OPP-82{:02d}".format(n), party_id, received=_days_ago(20))
        repo.set_status(case, "Quoted", _days_ago(5))
        made.setdefault("FU1", []).append("OPP-82{:02d}".format(n))
    for n in range(fu2):
        case = add_enquiry(repo, "OPP-83{:02d}".format(n), party_id, received=_days_ago(40))
        repo.set_status(case, "Quoted", _days_ago(25))
        repo.add_followup(case, _days_ago(12))
        made.setdefault("FU2", []).append("OPP-83{:02d}".format(n))
    for n in range(decide):
        case = add_enquiry(repo, "OPP-84{:02d}".format(n), party_id, received=_days_ago(90))
        repo.set_status(case, "Quoted", _days_ago(60))
        made.setdefault("Decide", []).append("OPP-84{:02d}".format(n))
    repo.commit()
    conn.close()
    return made


# test_r3_groups_the_rows_by_step_in_reading_order: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).


# test_each_group_shows_ten_and_has_its_own_show_all: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).


# test_the_batch_lost_button_sits_under_the_decide_group: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).


# test_nothing_here_only_when_every_group_is_empty: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).
