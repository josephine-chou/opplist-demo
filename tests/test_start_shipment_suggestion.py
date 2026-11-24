"""Start shipment suggestions (5d, 2026-09-25): the daily scan notices a won
JJC case with no shipment whose threads show the cargo moving, and
proposes Start shipment with that mail's milestone. Accept starts the
shipment as the button does and logs the milestone; Dismiss keeps the case
from being asked again."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, mail_archive, paths, shipments, suggestions
from opplist import web as web_module
from opplist.outlook_bridge import read_fixture
from opplist.web import create_app

TODAY = "2026-09-24"
OCEAN_MAILS = ("ocean_booking_confirmed.json", "ocean_prealert_sent.json", "ocean_debit_note_request.json",
               "ocean_eta_moved.json")


def fixture(name):
    return read_fixture(paths.FIXTURES_DIR / name)


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    aconn = mail_archive.open_archive(archive)
    mail_archive.store(aconn, [mail_archive.record_from_info(fixture(n)) for n in OCEAN_MAILS])
    mail_archive.set_meta(aconn, "last_sync_at", "2026-09-24 08:15:00")
    aconn.commit()
    aconn.close()
    path = tmp_path / "start.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    agent = add_party(repo, "Costa Verde Cargo", country="USA")
    line = add_party(repo, "Hapag-Lloyd", ptype="Unknown", country="USA")
    case = add_enquiry(repo, "OPP-1380", agent, received="2026-08-17", service_mode="FCL",
                       origin_country="USA", dest_country="Brazil", conversation_id="CONV-OCEAN-SAMPLE",
                       source_subject="BKG CONFIRMED - HLCUSAV3Q418 - Greenville SC to Santos 1x40HC")
    repo.set_status(case, "Won", "2026-08-21", rate_used="carrier")
    repo.update("enquiry", case, {"awarded_vendor_id": line})
    repo.commit()
    return conn, repo, path, case


def scan(conn, dry_run=False):
    with mail_archive.ArchiveSearch() as search:
        return shipments.propose(conn, search, dry_run=dry_run, today=TODAY)


def starts(conn, case):
    return conn.execute("SELECT * FROM suggestion WHERE enquiry_id = ? AND kind = 'shipment_milestone' "
                        "AND shipment_id IS NULL", (case,)).fetchall()


def test_a_won_jjc_case_with_a_booking_mail_gets_one_start_suggestion(world):
    conn, repo, path, case = world
    dry = scan(conn, dry_run=True)
    assert dry["starts"]["cases"] == 1 and dry["written"] == 0 and starts(conn, case) == []
    [(row, won, mail, proposal)] = dry["starts"]["proposals"]
    assert (row["opp_no"], won, proposal.name, proposal.at) == ("OPP-1380", "2026-08-21", "Booked", "2026-08-24 08:40")
    assert proposal.message_id == "<bkg-conf-0824@hlag-desk.example>"
    run = scan(conn)
    assert run["starts"]["written"] == 1
    [s] = starts(conn, case)
    assert (s["state"], s["proposed_status"], s["proposed_date"], s["message_id"]) == \
        ("pending", "Booked", "2026-08-24 08:40", "<bkg-conf-0824@hlag-desk.example>")
    assert suggestions.describe(s) == "Start shipment · Booked · Aug 24"
    # the scan tomorrow asks nothing more
    again = scan(conn)
    assert again["starts"]["asked"] == 1 and again["starts"]["written"] == 0 and len(starts(conn, case)) == 1


def test_accept_starts_the_shipment_as_the_button_does_and_logs_the_milestone(world):
    conn, repo, path, case = world
    scan(conn)
    [s] = starts(conn, case)
    expected = shipments.prefill(conn, case, shipments.button_title(
        conn, conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case,)).fetchone()))
    done = suggestions.accept(repo, s["id"], at="2026-09-24", today=TODAY)     # the date box is ignored
    assert done == "OPP-1380: shipment started, Booked logged on 2026-08-24 08:40"
    ship = shipments.for_enquiry(conn, case)
    for key, value in expected.items():
        assert ship[key] == value, key
    assert ship["carrier"] == "Hapag-Lloyd" and ship["mode"] == "ocean"
    [event] = shipments.events(conn, ship["id"])
    assert (event["kind"], event["milestone"], event["at"], event["source"], event["message_id"]) == \
        ("milestone", "Booked", "2026-08-24 08:40", "mail", "<bkg-conf-0824@hlag-desk.example>")
    row = conn.execute("SELECT state, shipment_id FROM suggestion WHERE id = ?", (s["id"],)).fetchone()
    assert (row["state"], row["shipment_id"]) == ("accepted", ship["id"])
    # the later mails now go through the shipment's own suggestions, the booking mail not again
    later = scan(conn, dry_run=True)
    names = [p.name for _, _, p in later["proposals"]]
    assert "Departed" in names and "Booked" not in names
    assert later["starts"]["cases"] == 0


def test_a_case_another_office_handles_gets_none(world):
    conn, repo, path, case = world
    repo.update("enquiry", case, {"handled_by": "JJV"})
    repo.commit()
    report = scan(conn)
    assert report["starts"]["cases"] == 0 and starts(conn, case) == []


def test_a_case_with_a_shipment_gets_none(world):
    conn, repo, path, case = world
    shipments.start(repo, case)
    repo.commit()
    report = scan(conn)
    assert report["starts"]["cases"] == 0 and starts(conn, case) == []


def test_a_mail_before_the_won_date_does_not_count(world):
    conn, repo, path, case = world
    # won the day after the booking mail: the pre-alert (sailed) is the first that counts
    conn.execute("UPDATE status_event SET at = '2026-08-25' WHERE enquiry_id = ? AND status = 'Won'", (case,))
    [(row, won, mail, proposal)] = scan(conn, dry_run=True)["starts"]["proposals"]
    assert won == "2026-08-25" and proposal.name == "Departed"
    # won after every mail: nothing, and the case is counted as having no milestone mail
    conn.execute("UPDATE status_event SET at = '2026-09-30' WHERE enquiry_id = ? AND status = 'Won'", (case,))
    report = scan(conn, dry_run=True)["starts"]
    assert report["proposals"] == [] and report["no_mail"] == 1


def test_dismiss_keeps_the_case_away(world):
    conn, repo, path, case = world
    scan(conn)
    [s] = starts(conn, case)
    assert suggestions.dismiss(repo, s["id"]) == \
        "OPP-1380: Start shipment dismissed - this case will not be suggested again"
    report = scan(conn)
    assert report["starts"]["asked"] == 1 and report["starts"]["written"] == 0
    assert [r["state"] for r in starts(conn, case)] == ["dismissed"]
    assert shipments.for_enquiry(conn, case) is None


# test_tracking_and_the_case_page_offer_it_and_accept_goes_to_the_shipment: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).
