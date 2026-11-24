"""Plans are not events (5e, 2026-09-25): shipments.detect() tells a done
milestone from a planned one, Arrived means the case's destination, a
missed cut-off is an issue; Start shipment fires on either and logs a
milestone only when it is done."""

from __future__ import annotations

import json

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, mail_archive, paths, shipments, suggestions
from opplist.outlook_bridge import MailInfo, read_fixture

CASES = json.loads((paths.FIXTURES_DIR / "milestone_plan_lines.json").read_text(encoding="utf-8"))["lines"]
OCEAN = ("ocean_booking_confirmed.json", "ocean_prealert_sent.json", "ocean_debit_note_request.json",
         "ocean_eta_moved.json")


def mail_of(line, sent_at="2026-09-15 10:00", subject="RE: RATE REQUEST", sender="Someone"):
    return MailInfo(subject=subject, sender_name=sender, sender_address="x@sample.example", sender_internal=False,
                    received_on=sent_at[:10], received_at=sent_at, to_addresses=[], cc_addresses=[],
                    body_excerpt="Hi,\r\n\r\n{}\r\n\r\nRegards".format(line), message_id="<{}>".format(abs(hash(line))))


@pytest.mark.parametrize("row", CASES, ids=[c["case"] for c in CASES])
def test_the_evidence_lines_read_as_expected(row):
    mail = mail_of(row["line"], row["sent_at"], row["subject"], row["sender"])
    got = shipments.detect(mail, mode=row["mode"], dest=row["dest"], plans=True)
    want = row["expect"]
    assert (got.kind, got.name, got.planned) == (want["kind"], want["name"], want["planned"]), row["line"]
    # without plans (a shipment's own suggestions): only the done one is a milestone
    plain = shipments.detect(mail, mode=row["mode"], dest=row["dest"])
    if want["planned"]:
        # the planned milestone is never proposed as one ('pay your invoice now' may still read as Invoiced)
        assert plain is None or plain.kind == "note" or plain.name != want["name"], row["line"]
    else:
        assert (plain.kind, plain.name) == (want["kind"], want["name"])


@pytest.mark.parametrize("line", [
    "The skids have been picked up.", "The boxes were picked up an hour ago.", "Freight has departed on the 9/9 flight.",
    "We have booked two pallet positions.", "Booking confirmed for the Tuesday sailing.",
    "Delivered to the receiver this morning.", "Freight departed 9/9, ETA expected 9/19."])
def test_done_wording_is_an_event(line):
    got = shipments.detect(mail_of(line), mode="air", plans=True)
    assert got is not None and got.kind == "milestone" and got.planned is False, line


@pytest.mark.parametrize("line", [
    "The pallets will be picked up on Thursday.", "We are going to book the space on Friday.",
    "Collection is scheduled for Wednesday, the crates will be picked up at 8.",
    "Collection waits on the funds; picked up once the wire lands.",
    "Kindly arrange for the drums to be picked up.", "Goods expected to be delivered in a few days.",
    "Crates to be picked up by 12 Oct."])
def test_intent_and_the_future_are_plans(line):
    got = shipments.detect(mail_of(line), mode="air", plans=True)
    assert got is None or got.planned or got.kind != "milestone", line


def test_arrived_means_the_destination():
    dest = ["GRU", "Brazil", "SAO PAULO"]
    assert shipments.detect(mail_of("The consignment has arrived at ORD."), mode="air", dest=dest) is None
    got = shipments.detect(mail_of("The consignment has arrived at GRU."), mode="air", dest=dest)
    assert (got.kind, got.name) == ("milestone", "Arrived")
    got = shipments.detect(mail_of("The freight arrived in Sao Paulo this morning."), mode="air", dest=dest)
    assert got.name == "Arrived"
    # no place named, or no destination known: as before
    assert shipments.detect(mail_of("Everything has arrived."), mode="air", dest=dest).name == "Arrived"
    assert shipments.detect(mail_of("The consignment has arrived at ORD."), mode="air").name == "Arrived"
    # the cut-off is an issue on a started shipment
    cut = shipments.detect(mail_of(CASES[5]["line"]), mode="air", dest=dest)
    assert (cut.kind, cut.name) == ("issue", "Missed cut-off")
    assert shipments.detect(mail_of("Apologies, the truck came after the cutoff for tonight's flight."), mode="air").name == "Missed cut-off"


def test_the_sample_mails_keep_their_done_readings():
    """The sample ocean mails (the shipment of the 5b tests) and the last
    three evidence lines: the done readings stand."""
    ocean = {"mode": "ocean", "eta": None, "planned_arrival": "2026-10-06", "planned_delivery": None}

    class Ship(dict):
        def __getitem__(self, key):
            return self.get(key)
    ship = Ship(ocean)
    booked = shipments.detect(read_fixture(paths.FIXTURES_DIR / OCEAN[0]), ship, dest=["Santos"])
    assert (booked.kind, booked.name, booked.planned) == ("milestone", "Booked", False)
    departed = shipments.detect(read_fixture(paths.FIXTURES_DIR / OCEAN[1]), ship, dest=["Santos"])
    assert (departed.kind, departed.name, departed.planned) == ("milestone", "Departed", False)


# ----------------------------------------------------- Start shipment --
@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    plan = read_fixture(paths.FIXTURES_DIR / OCEAN[0])
    plan.body_excerpt = "Hi,\r\n\r\nThe booking confirmation will follow when the line releases its schedule.\r\n"
    plan.message_id = "<plan-0824@hlag-desk.example>"
    plan.entry_id = "ENTRY-PLAN"
    plan.received_at = "2026-08-24 07:30"
    aconn = mail_archive.open_archive(archive)
    mail_archive.store(aconn, [mail_archive.record_from_info(plan)])
    mail_archive.set_meta(aconn, "last_sync_at", "2026-09-24 08:15:00")
    aconn.commit()
    aconn.close()
    path = tmp_path / "plans.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    agent = add_party(repo, "Costa Verde Cargo", country="USA")
    case = add_enquiry(repo, "OPP-1380", agent, received="2026-08-17", service_mode="FCL",
                       origin_country="USA", dest_country="Brazil", conversation_id="CONV-OCEAN-SAMPLE",
                       source_subject="BKG CONFIRMED - HLCUSAV3Q418 - Greenville SC to Santos 1x40HC")
    repo.set_status(case, "Won", "2026-08-21", rate_used="own")
    repo.commit()
    return conn, repo, case


def test_a_planned_start_is_labelled_and_accept_logs_no_milestone(world):
    conn, repo, case = world
    with mail_archive.ArchiveSearch() as search:
        report = shipments.propose(conn, search, dry_run=False, today="2026-09-24")
    [(row, won, mail, proposal)] = report["starts"]["proposals"]
    assert (proposal.name, proposal.planned) == ("Booked", True)
    [s] = conn.execute("SELECT * FROM suggestion WHERE enquiry_id = ?", (case,)).fetchall()
    assert s["proposed_status"] == "Booked, planned"
    assert suggestions.describe(s) == "Start shipment · Booked, planned · Aug 24"
    done = suggestions.accept(repo, s["id"])
    assert done == "OPP-1380: shipment started (Booked, planned - no milestone logged)"
    ship = shipments.for_enquiry(conn, case)
    assert ship is not None and shipments.events(conn, ship["id"]) == []
    assert conn.execute("SELECT state, shipment_id FROM suggestion WHERE id = ?", (s["id"],)).fetchone()[:] == \
        ("accepted", ship["id"])
