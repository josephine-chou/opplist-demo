"""status_confirmed follows anybody looking at the case; verify's check 3
under the same-day rule; Suggested name for any joined key."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, intake, suggestions, verify
from opplist.web import create_app


@pytest.fixture
def case(repo):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    enquiry_id = add_enquiry(repo, "OPP-1500", party, received="2026-09-01", status="New",
                             status_confirmed=0)
    repo.commit()
    return repo, enquiry_id


def confirmed(conn, enquiry_id):
    return conn.execute("SELECT status_confirmed FROM enquiry WHERE id = ?",
                        (enquiry_id,)).fetchone()[0]


def event_count(conn, enquiry_id):
    return conn.execute("SELECT COUNT(*) FROM status_event WHERE enquiry_id = ? "
                        "AND kind = 'status'", (enquiry_id,)).fetchone()[0]


def test_a_hold_confirms_the_status_a_note_does_not(case):
    repo, enquiry_id = case
    repo.add_note(enquiry_id, "2026-09-02", "called them")
    assert confirmed(repo.conn, enquiry_id) == 0
    repo.set_hold(enquiry_id, "waiting for the shipper", "2026-09-02")
    assert confirmed(repo.conn, enquiry_id) == 1


def test_the_same_status_again_confirms_without_a_second_event(case):
    repo, enquiry_id = case
    assert event_count(repo.conn, enquiry_id) == 1
    assert repo.set_status(enquiry_id, "New", "2026-09-01") is True
    assert event_count(repo.conn, enquiry_id) == 1                      # no duplicate
    assert confirmed(repo.conn, enquiry_id) == 1
    assert [tuple(r) for r in repo.conn.execute(
        "SELECT old_value, new_value FROM change_log WHERE table_name = 'enquiry' "
        "AND row_id = ? AND field = 'status_confirmed'", (enquiry_id,))] == [("0", "1")]
    # a backdated event also counts as having looked
    other = add_enquiry(repo, "OPP-1501", 1, received="2026-09-01", status="Quoted",
                        status_confirmed=0)
    repo.insert("status_event", {"enquiry_id": other, "at": "2026-09-05", "status": "Quoted"})
    assert repo.set_status(other, "Sourcing", "2026-09-03") is False
    assert confirmed(repo.conn, other) == 1


def test_accepting_a_suggestion_confirms(case):
    repo, enquiry_id = case
    suggestion_id = suggestions.record(repo, enquiry_id, "OPP-1500", {
        "kind": "note", "note": "they wrote back", "proposed_date": "2026-09-03",
        "evidence_sender": "Ana", "evidence_date": "2026-09-03",
        "evidence_quote": "they wrote back", "confidence": "low", "source": "mail"})
    suggestions.accept(repo, suggestion_id)
    assert confirmed(repo.conn, enquiry_id) == 1


def test_the_confirm_link_on_the_case_page(tmp_path):
    path = tmp_path / "web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    add_enquiry(repo, "OPP-1500", party, received="2026-09-01", status="New", status_confirmed=0)
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    body = client.get("/enquiries/1").data.decode("utf-8")
    assert "unconfirmed" in body and 'action="/enquiries/1/confirm"' in body
    page = client.post("/enquiries/1/confirm", follow_redirects=True)
    body = page.data.decode("utf-8")
    assert "Status confirmed." in body and 'action="/enquiries/1/confirm"' not in body
    conn = db.open_db(path)
    assert confirmed(conn, 1) == 1
    conn.close()


def test_verify_passes_when_the_status_matches_the_rule(case):
    repo, enquiry_id = case
    repo.set_status(enquiry_id, "Quoted", "2026-09-01")          # New and Quoted on one day
    check = verify.check_events(repo.conn)
    assert check.ok
    assert "last event != enquiry.status 0" in check.lines
    assert "latest day has several events 1 (info, not a failure)" in check.lines
    assert "   OPP-1500 (2)" in check.lines
    # a status that disagrees with the rule still fails
    repo.conn.execute("UPDATE enquiry SET status = 'New' WHERE id = ?", (enquiry_id,))
    check = verify.check_events(repo.conn)
    assert not check.ok
    assert "   OPP-1500 status New but last event Quoted" in check.lines


def test_suggested_name_for_any_joined_key():
    assert intake.looks_like_domain_stem("Bluefin-Usa")
    assert intake.looks_like_domain_stem("Kestrellogistics-Ph")
    assert not intake.looks_like_domain_stem("Tolvex")             # already a name
    assert not intake.looks_like_domain_stem("Zenith Cargo")
    assert intake.name_from_stem("Bluefin-Usa") == "Bluefin Usa"
