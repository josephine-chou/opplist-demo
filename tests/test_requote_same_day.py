"""A re-quote on the day of the quote (schema v19; v30).

The same-day order is New < Sourcing < Quoted = requote < Won = Lost =
Dropped, then the newest row. Since v30 (Re-quote means re-quoted) a
requote event is kind 'requote' with status Quoted - a second quote - and
it says what the case is like any status event. Old requote events carry
Sourcing (v19 to v28).
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, schema_migrations, tracking
from opplist.web import create_app


def view(conn, enquiry_id):
    return conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()


@pytest.fixture
def quoted_today(repo):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1701", party, received="2026-07-01")
    repo.set_status(case, "Quoted", "2026-07-10")
    repo.commit()
    return repo, case


def test_a_same_day_requote_is_the_second_round(quoted_today):
    repo, case = quoted_today
    assert repo.requote(case, "2026-07-10", note="3 x 40HQ") is True
    repo.commit()
    row = view(repo.conn, case)
    assert (row["status"], row["in_requote"], row["quote_rounds"]) == ("Quoted", 0, 2)
    event = repo.conn.execute(
        "SELECT kind, status, note FROM status_event WHERE enquiry_id = ? "
        "ORDER BY id DESC LIMIT 1", (case,)).fetchone()
    assert tuple(event) == ("requote", "Quoted", "Re-quote: 3 x 40HQ")
    assert repo.latest_event(case)["kind"] == "requote"


def test_quoting_again_the_same_day_after_a_requote_is_round_three(quoted_today):
    repo, case = quoted_today
    repo.requote(case, "2026-07-10")
    repo.set_status(case, "Quoted", "2026-07-10")
    repo.commit()
    row = view(repo.conn, case)
    assert (row["status"], row["quote_rounds"], row["in_requote"]) == ("Quoted", 3, 0)
    assert row["fu_count"] == 0


def test_won_the_same_day_as_a_requote_is_won(quoted_today):
    repo, case = quoted_today
    repo.requote(case, "2026-07-10")
    repo.set_status(case, "Won", "2026-07-10")
    repo.commit()
    row = view(repo.conn, case)
    assert (row["status"], row["in_requote"]) == ("Won", 0)


def test_a_plain_new_still_loses_to_a_same_day_quote(quoted_today):
    """The Set status menu: a hand-picked New dated the quote day is
    history filled in, not a re-quote."""
    repo, case = quoted_today
    assert repo.set_status(case, "New", "2026-07-10") is False
    repo.commit()
    row = view(repo.conn, case)
    assert (row["status"], row["in_requote"]) == ("Quoted", 0)


def test_registering_new_and_quoted_on_one_day_is_still_quoted(repo):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = repo.create_enquiry({"opp_no": "OPP-1702", "requester_party_id": party,
                                "received_on": "2026-07-01", "status": "Quoted"},
                               "2026-07-01")
    repo.commit()
    kinds = [tuple(r) for r in repo.conn.execute(
        "SELECT status, kind FROM status_event WHERE enquiry_id = ? ORDER BY id", (case,))]
    assert kinds == [("New", "status"), ("Quoted", "status")]
    assert view(repo.conn, case)["status"] == "Quoted"
    assert repo.latest_event(case)["status"] == "Quoted"


def test_a_requote_the_day_after_still_works(quoted_today):
    repo, case = quoted_today
    repo.requote(case, "2026-07-11")
    repo.commit()
    row = view(repo.conn, case)
    assert (row["status"], row["in_requote"], row["quote_rounds"]) == ("Quoted", 0, 2)


def test_voiding_the_requote_puts_the_quote_back(quoted_today):
    repo, case = quoted_today
    repo.requote(case, "2026-07-10")
    event = repo.conn.execute("SELECT id FROM status_event WHERE enquiry_id = ? "
                              "AND kind = 'requote'", (case,)).fetchone()
    repo.void_status_event(event["id"])
    repo.commit()
    row = view(repo.conn, case)
    assert (row["status"], row["in_requote"], row["quote_rounds"]) == ("Quoted", 0, 1)


# ----------------------------------------------------------- tracking ---
def test_r3_restarts_its_clock_from_the_requote(quoted_today):
    repo, case = quoted_today
    assert [i.label for i in tracking.rule_quoted_no_answer(repo.conn, "2026-07-20")] == ["OPP-1701"]
    repo.requote(case, "2026-07-17")                        # a Friday
    repo.commit()
    [item] = tracking.rule_quoted_no_answer(repo.conn, "2026-07-20")
    assert (item.extra["step"], item.since, item.days) == ("FU1", "2026-07-17", 1)


# ----------------------------------------------------------- backfill ---
def test_v18_requote_events_take_the_new_kind(repo):
    """What requote() wrote under v18 - kind status, Sourcing, note
    'Re-quote...' - becomes kind requote, through the repository."""
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1703", party, received="2026-07-01")
    repo.set_status(case, "Quoted", "2026-07-10")
    old = repo.insert("status_event", {"enquiry_id": case, "at": "2026-07-10", "status": "Sourcing",
                                       "note": "Re-quote: 3 x 40HQ", "kind": "status"})
    plain = repo.insert("status_event", {"enquiry_id": case, "at": "2026-07-09", "status": "Sourcing",
                                         "note": "asked another vendor", "kind": "status"})
    repo.commit()
    assert view(repo.conn, case)["status"] == "Quoted"        # v18 quirk: the quote won

    notes = schema_migrations._to_v19(repo.conn)
    repo.conn.commit()
    assert any("kind 'requote': 1" in n for n in notes)
    kinds = {r["id"]: r["kind"] for r in repo.conn.execute(
        "SELECT id, kind FROM status_event WHERE enquiry_id = ?", (case,))}
    assert kinds[old] == "requote" and kinds[plain] == "status"
    logged = repo.conn.execute(
        "SELECT source, old_value, new_value FROM change_log WHERE table_name = 'status_event' "
        "AND row_id = ? AND field = 'kind'", (old,)).fetchone()
    assert tuple(logged) == ("migration", "status", "requote")
    # the view now reads it the v19 way, though enquiry.status is only
    # restated by the next edit on the case
    repo._restate(old)
    repo.commit()
    assert (view(repo.conn, case)["status"], view(repo.conn, case)["in_requote"]) == ("Sourcing", 1)


def test_running_the_backfill_twice_changes_nothing(repo):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1704", party, received="2026-07-01")
    repo.set_status(case, "Quoted", "2026-07-10")
    repo.requote(case, "2026-07-12")
    repo.commit()
    before = repo.conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    notes = schema_migrations._to_v19(repo.conn)
    assert any("kind 'requote': 0" in n for n in notes)
    assert repo.conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == before


# --------------------------------------------------------------- pages ---
# test_the_menu_points_a_quoted_case_at_re_quote: replaced in 15a by tests/test_case_v3.py (case page v3).
