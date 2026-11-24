"""Batch FX2 (2026-10-27): a struck-out vendor request no longer blocks
logging the same vendor again on that case and day. A live one still does
('already on the case').

Every company, person, mail, place and date here is invented; cases from
OPP-7001."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, outlook_bridge
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app

ASKED = "2026-12-03"


def rate_request() -> MailInfo:
    return MailInfo(
        subject="[OPP-7001] JOJO | RATE REQUEST // PORTVALE - LAKEMOUTH // FCL | TERNWAY",
        received_on=ASKED, received_at=ASKED + " 16:41", sent_at=ASKED + " 16:41",
        sender_address="desk@jojofreight.example", sender_name="JJC-DESK", sender_internal=True,
        to_addresses=["rates@ternwayfreight.example"], display_to="Rates Ternway",
        conversation_id="CONV-7001-T", entry_id="ENTRY-7001-T", message_id="<rr-7001-t@jojofreight.example>",
        body_excerpt="Hi Ternway team,\n\nPlease quote 1 x 40HQ Portvale to Lakemouth.\n\nThank you,\nDesk")


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "relog.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    requester = add_party(repo, "Fernhill Forwarding", country="Chile")
    vendor = add_party(repo, "Ternway Freight", country="USA")
    repo.insert("party_domain", {"domain": "ternwayfreight.example", "party_id": vendor})
    case = add_enquiry(repo, "OPP-7001", requester, received=ASKED, service_mode="FCL")
    repo.commit()
    monkeypatch.setattr(outlook_bridge, "read_selection", rate_request)
    app = create_app(path)
    app.config["TESTING"] = True
    yield {"conn": conn, "repo": repo, "case": case, "vendor": vendor, "web": app.test_client()}
    conn.close()


def _earlier_request(w, voided):
    vr = w["repo"].insert("vendor_request", {"enquiry_id": w["case"], "vendor_party_id": w["vendor"],
                                             "asked_on": ASKED, "source": "manual",
                                             "notes": "Logged from Outlook: an earlier request"})
    w["repo"].commit()
    if voided:
        w["repo"].void_vendor_request(vr)
        w["repo"].commit()
    return vr


def _rows(w):
    return [tuple(r) for r in w["conn"].execute(
        "SELECT id, vendor_party_id, asked_on, voided FROM vendor_request WHERE enquiry_id = ? ORDER BY id",
        (w["case"],))]


def _log(w):
    return w["web"].post("/enquiries/{}/vendors/from-outlook".format(w["case"]),
                         follow_redirects=True).data.decode("utf-8")


def test_a_struck_out_request_does_not_block_the_vendor_again(world):
    w = world
    struck = _earlier_request(w, voided=True)
    body = _log(w)
    assert "Logged 1 vendor request: Ternway Freight" in body
    assert "already on the case" not in body
    rows = _rows(w)
    assert rows[0] == (struck, w["vendor"], ASKED, 1)            # still struck out
    assert len(rows) == 2 and rows[1][1:] == (w["vendor"], ASKED, 0)


def test_a_live_request_the_same_day_is_already_on_the_case(world):
    w = world
    live = _earlier_request(w, voided=False)
    body = _log(w)
    assert "already on the case: Ternway Freight" in body
    assert "Logged 1 vendor request" not in body
    assert _rows(w) == [(live, w["vendor"], ASKED, 0)]
