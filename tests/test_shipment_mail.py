"""Shipments 5b: what a mail proposes for a shipment - rules
only, on the sender's own words - the suggestions it becomes, Accept,
the daily scan, the Mail card's Log buttons, Update from Outlook, and
closing with a reason."""

from __future__ import annotations

import json

import pytest

from conftest import add_enquiry, add_party, seed_reference, form_nesting
from opplist import db, mail_archive, outlook_bridge, paths, shipments, suggestions, tracking
from opplist import web as web_module
from opplist.outlook_bridge import read_fixture
from opplist.web import create_app

TODAY = "2026-09-24"
OCEAN_MAILS = ("ocean_booking_confirmed.json", "ocean_prealert_sent.json", "ocean_debit_note_request.json",
               "ocean_eta_moved.json")
TRUCK_MAILS = ("truck_liftgate_dispute.json", "truck_driver_window_missed.json", "truck_carrier_replaced.json")


def fixture(name):
    return read_fixture(paths.FIXTURES_DIR / name)


class Ship(dict):
    """A shipment row stand-in for detect() outside the database."""
    def __getitem__(self, key):
        return self.get(key)


OCEAN = Ship(mode="ocean", eta=None, planned_arrival="2026-10-06", planned_delivery=None)
TRUCK = Ship(mode="truck", eta=None, planned_arrival=None, planned_delivery="2026-09-24")


# ---------------------------------------------------------- detection ----
def test_the_ocean_mails_propose_what_the_sample_shows():
    booked = shipments.detect(fixture(OCEAN_MAILS[0]), OCEAN)
    assert (booked.kind, booked.name, booked.at, booked.confidence) == ("milestone", "Booked", "2026-08-24 08:40", "medium")
    assert booked.message_id == "<bkg-conf-0824@hlag-desk.example>" and booked.sender == "Hapag-Lloyd Booking Desk"
    assert booked.quote.startswith("We have booked 1 x 40' HC")
    # our own pre-alert: the vessel sailed, with the ETD date in our words
    departed = shipments.detect(fixture(OCEAN_MAILS[1]), OCEAN)
    assert (departed.kind, departed.name, departed.at, departed.confidence) == ("milestone", "Departed", "2026-09-09 16:20", "high")
    # the DN asked for: a note, never an invoice milestone
    dn = shipments.detect(fixture(OCEAN_MAILS[2]), OCEAN)
    assert (dn.kind, dn.name, dn.at) == ("note", "Debit note requested", "2026-09-12 10:05")
    # the revised ETA is later than the planned arrival: an issue carrying the new ETA
    moved = shipments.detect(fixture(OCEAN_MAILS[3]), OCEAN)
    assert (moved.kind, moved.name, moved.eta, moved.at, moved.confidence) == \
        ("issue", "ETA moved to Oct 19", "2026-10-19", "2026-09-18 14:35", "high")
    assert "new ETA Santos is 19 Oct" in moved.quote


def test_the_truck_mails_raise_issues():
    liftgate = shipments.detect(fixture(TRUCK_MAILS[0]), TRUCK)
    assert (liftgate.kind, liftgate.name, liftgate.at, liftgate.confidence) == ("issue", "Liftgate", "2026-09-22 17:48", "medium")
    driver = shipments.detect(fixture(TRUCK_MAILS[1]), TRUCK)
    assert (driver.kind, driver.name) == ("issue", "Pickup missed")           # 'missed' outranks 'driver' 
    replaced = shipments.detect(fixture(TRUCK_MAILS[2]), TRUCK)
    assert (replaced.kind, replaced.name, replaced.at) == ("issue", "Problem reported", "2026-09-23 10:02")


def test_quoted_text_never_counts():
    # the question quoted under the trucker's mail says 'delivered': quoted, ignored
    replaced = shipments.detect(fixture(TRUCK_MAILS[2]), TRUCK)
    assert "delivered" not in replaced.quote.lower()
    # the DN request quotes our pre-alert ('sailed', 'ETD 9 Sep'): the note wins, not Departed
    assert shipments.detect(fixture(OCEAN_MAILS[2]), OCEAN).kind == "note"
    # a mail that is nothing but quoted text proposes nothing
    mail = fixture(TRUCK_MAILS[2])
    mail.body_excerpt = "Noted.\r\n\r\nOn Sep 23, 2026 at 9:40 AM Eric Lo wrote:\r\n> Goods delivered this morning.\r\n"
    assert shipments.detect(mail, TRUCK) is None


def test_dates_and_the_eta_rules():
    assert shipments.find_date("ETA Santos 6 Oct", 2026) == "2026-10-06"
    assert shipments.find_date("reached the CFS Oct 19, 2026", 2026) == "2026-10-19"
    assert shipments.find_date("unloaded 2026-09-25 14:10", 2026) == "2026-09-25"
    assert shipments.find_date("collected 9/21", 2026) == "2026-09-21"
    assert shipments.find_date("nothing dated", 2026) is None
    mail = fixture(OCEAN_MAILS[3])
    # an ETA not later than the mail date reads as an arrival
    mail.body_excerpt = "Hi, vessel update: ETA Santos 16 Sep, unloading next.\r\n"
    got = shipments.detect(mail, OCEAN)
    assert (got.kind, got.name, got.at) == ("milestone", "Arrived", "2026-09-16 14:35")
    # an ETA later than the mail but not later than the shipment's: nothing to raise
    mail.body_excerpt = "Hi, ETA Santos unchanged at 6 Oct.\r\n"
    assert shipments.detect(mail, OCEAN) is None
    # an issue word on the same line as a milestone word: the issue wins
    mail.body_excerpt = "Hi, the unit was rolled; it finally sailed a week later on the relief vessel.\r\n"
    assert shipments.detect(mail, OCEAN).name == "Delay"


# test_the_scan_proposes_once_per_mail_and_accept_writes_the_event: deleted in batch FX3 (shipments are off by default now). Replaced by tests/test_fx3_one_window.py's test_with_shipments_on_the_scan_proposes_and_accept_writes_the_event, on invented values.


# test_the_pages_offer_the_mail_and_the_proposals: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).


# test_close_with_a_reason: deleted in batch FX3 for the same reason; replaced by test_with_shipments_on_a_shipment_closes_with_a_reason_on_its_page.
