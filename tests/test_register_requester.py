"""Batch RG1: Register from Outlook reads the requester right - another
office's own mail is its inquiry (B), our mail quoting an inquiry written
to us takes its requester from it (A) and the office that asked first wins
(E), a booking subject's C/ name is the requester and a common word offers
nobody (C), a reply to our offer of services asking for a rate is an
inquiry (D) - and reader-check, which needs no Outlook.

Every company, person, office code, mail, date and number here is
invented; .test domains, cases from OPP-8601.
"""

from __future__ import annotations

import json

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, intake, matching, outlook_bridge, reader_check

US = "ourco.test"


def header(name, address, sent, to, subject, cc=""):
    return ("From: {} <{}>\nSent: {}\nTo: {}\n{}Subject: {}\n".format(
        name, address, sent, to, "Cc: {}\n".format(cc) if cc else "", subject))


def mail(subject, body, sender="mara@" + US, name="JJC-MARA OSTEN", to=(), cc=(), to_names=None, cc_names=None,
         internal=True, on="2026-12-02", key="8601"):
    return outlook_bridge.MailInfo(
        subject=subject, received_on=on, received_at=on + " 10:00", sender_address=sender, sender_name=name,
        sender_internal=internal, to_addresses=list(to), cc_addresses=list(cc),
        to_names=list(to_names or [""] * len(to)), cc_names=list(cc_names or [""] * len(cc)),
        display_to="; ".join(to_names or to), body_excerpt=body, entry_id="ENTRY-" + key,
        conversation_id="CONV-" + key)


@pytest.fixture
def world(tmp_path):
    conn = db.open_db(tmp_path / "rg1.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", [US])
    ids = {}
    ids["tbc"] = add_party(repo, "(TBC)", ptype="Unknown", country="Unknown", placeholder=1)
    for key, ptype, domain in (("JoJo QRX", "Office", None), ("JoJo NVX", "Office", None),
                               ("Quill Agents", "Agent", "quillagents.test"),
                               ("Fernmoor Lines", "Agent", "fernmoor.test"),
                               ("Tallow Carriers", "Agent", "tallowcarriers.test"),
                               ("Orchard & Pike Supply", "Direct Customer", "orchardpike.test"),
                               ("Alder Supply", "Agent", None), ("Birch Supply", "Agent", None),
                               ("Cedar Supply", "Agent", None), ("Supply Hollow", "Agent", None),
                               ("Supply Outfit", "Agent", None)):
        ids[key] = add_party(repo, key, ptype=ptype, country="USA")
        if domain:
            repo.insert("party_domain", {"domain": domain, "party_id": ids[key]})
    repo.commit()
    yield conn, repo, ids
    conn.close()


def requester(conn, draft):
    party = draft.values.get("requester_party_id")
    row = conn.execute("SELECT key FROM party WHERE id = ?", (party,)).fetchone() if party else None
    return row["key"] if row else None


# ------------------------------------------------- B. another office wrote --
def test_another_office_s_own_mail_is_its_inquiry_with_its_colleagues_copied(world):
    conn, repo, ids = world
    inquiry = mail("Air inquiry ZQA - SAI 3 PLTS", "Hi Mara,\nPlease quote air ZQA to SAI, 3 plts 410 kg.\nLin\n",
                   sender="lin@" + US, name="QRX-LIN HOLLOWAY",
                   to=["mara@" + US, "bo@" + US], to_names=["JJC-MARA OSTEN", "QRX-BO TERN"],
                   cc=["kit@" + US], cc_names=["QRX-KIT ASHE"])
    draft = intake.build(conn, inquiry)
    assert requester(conn, draft) == "JoJo QRX"
    assert not draft.outbound and not draft.vendors


# ------------------------------------- A. our mail quoting an inquiry to us --
AGENT_TO_US = header("Ines Calloway", "ines@quillagents.test", "Tuesday, December 1, 2026 8:15 AM",
                     "JJC-MARA OSTEN <mara@{}>".format(US), "Rate request LAX - SAI 2 crates") + (
    "\nHi Mara,\nPlease quote LAX to SAI, 2 crates 380 kg.\nInes\n")


def test_our_mail_asking_vendors_takes_the_requester_from_the_quoted_inquiry(world):
    conn, repo, ids = world
    ask = mail("FW: Rate request LAX - SAI 2 crates",
               "Hi all,\nPlease quote your best rate LAX - SAI, 2 crates 380 kg.\nMara\n\n" + AGENT_TO_US,
               to=["desk@fernmoor.test", "ops@tallowcarriers.test"])
    draft = intake.build(conn, ask)
    assert requester(conn, draft) == "Quill Agents"
    assert draft.outbound
    assert sorted(v["domain"] for v in draft.vendors) == ["fernmoor.test", "tallowcarriers.test"]


def test_the_requester_copied_on_our_vendor_request_is_not_a_vendor(world):
    conn, repo, ids = world
    ask = mail("RE: Rate request LAX - SAI 2 crates",
               "Hi Fernmoor,\nPlease quote your best rate LAX - SAI, 2 crates.\nMara\n\n" + AGENT_TO_US,
               to=["desk@fernmoor.test"], cc=["ines@quillagents.test"])
    draft = intake.build(conn, ask)
    assert requester(conn, draft) == "Quill Agents"
    assert [v["domain"] for v in draft.vendors] == ["fernmoor.test"]


def test_our_reply_to_the_office_that_asked_is_its_case_not_an_office_we_asked(world):
    conn, repo, ids = world
    office_to_us = header("QRX-LIN HOLLOWAY", "lin@" + US, "Tuesday, December 1, 2026 9:00 PM",
                          "JJC-MARA OSTEN <mara@{}>".format(US), "DAP ZQA to MZN") + (
        "\nHi Mara,\nPlease quote DAP ZQA to MZN, 1 x 40HQ.\nLin\n")
    reply = mail("RE: DAP ZQA to MZN", "Hi Lin,\nCould you advise the commodity and the weight?\nMara\n\n"
                 + office_to_us, to=["lin@" + US], to_names=["QRX-LIN HOLLOWAY"])
    draft = intake.build(conn, reply)
    assert requester(conn, draft) == "JoJo QRX"
    assert draft.reply_to_requester and not draft.vendors


def test_a_mail_of_ours_that_quotes_no_inquiry_stays_tbc(world):
    conn, repo, ids = world
    ask = mail("RATE REQUEST LAX - SAI", "Hi,\nPlease quote your best rate LAX - SAI, 2 crates.\nMara\n",
               to=["desk@fernmoor.test"])
    draft = intake.build(conn, ask)
    assert requester(conn, draft) == "(TBC)"
    assert [v["domain"] for v in draft.vendors] == ["fernmoor.test"]


# ------------------------------------------- E. two offices in the chain --
QRX_TO_NVX = header("QRX-LIN HOLLOWAY", "lin@" + US, "Tuesday, December 1, 2026 6:00 AM",
                    "NVX-ROWAN DELL <rowan@{}>".format(US), "Air ZQA to SAI") + (
    "\nHi Rowan,\nPlease check a rate ZQA to SAI, 300 kg.\nLin\n")


def test_the_office_that_asked_first_is_the_requester(world):
    conn, repo, ids = world
    nvx_to_us = header("NVX-ROWAN DELL", "rowan@" + US, "Tuesday, December 1, 2026 8:00 AM",
                       "JJC-MARA OSTEN <mara@{}>".format(US), "FW: Air ZQA to SAI") + (
        "\nMara, can you help QRX with this one?\nRowan\n\n")
    reply = mail("RE: Air ZQA to SAI", "Hi both,\nPlease see below rates: USD 3.40/kg all-in.\nMara\n\n"
                 + nvx_to_us + QRX_TO_NVX, to=["rowan@" + US, "lin@" + US],
                 to_names=["NVX-ROWAN DELL", "QRX-LIN HOLLOWAY"])
    assert requester(conn, intake.build(conn, reply)) == "JoJo QRX"


def test_an_office_that_only_copied_us_on_its_own_mail_stays_the_requester(world):
    conn, repo, ids = world
    nvx_to_customer = header("NVX-ROWAN DELL", "rowan@" + US, "Tuesday, December 1, 2026 8:00 AM",
                             "ops@quillagents.test", "Air ZQA to SAI", cc="JJC-MARA OSTEN <mara@{}>".format(US)) + (
        "\nHi, please send the pickup address.\nRowan\n\n")
    reply = mail("RE: Air ZQA to SAI", "Hi both,\nPlease see below rates: USD 3.40/kg all-in.\nMara\n\n"
                 + nvx_to_customer + QRX_TO_NVX, to=["rowan@" + US, "lin@" + US],
                 to_names=["NVX-ROWAN DELL", "QRX-LIN HOLLOWAY"])
    assert requester(conn, intake.build(conn, reply)) == "JoJo NVX"


# ------------------------------------------------------------ C. booking --
def test_the_booking_subject_s_c_name_is_the_requester(world):
    conn, repo, ids = world
    booking = mail("NEW BOOKING // C/ORCHARD AND PIKE SUPPLY // S/WREN MILLS // LCL // TWN - ATL // PO# 88-1",
                   "Hi team,\nPlease see below booking details, rate USD 95/cbm as quoted.\nMara\n",
                   to=["buyer@orchardpike.test", "ops@tallowcarriers.test"])
    draft = intake.build(conn, booking)
    assert requester(conn, draft) == "Orchard & Pike Supply"
    assert draft.sources["requester_party_id"] == intake.FROM_BOOKING


def test_the_c_name_follows_a_merge_and_two_parties_name_nobody(world):
    conn, repo, ids = world
    old = add_party(repo, "Wren Mills Co", ptype="Direct Customer")
    new = add_party(repo, "Wren Mills Company", ptype="Direct Customer")
    repo.update("party", old, {"merged_into": new})
    repo.commit()
    assert intake.booking_customer(conn, "NEW BOOKING // C/WREN MILLS CO. // S/X // FCL") == (new, "Wren Mills Company")
    add_party(repo, "ORCHARD AND PIKE SUPPLY", ptype="Direct Customer")
    repo.commit()
    assert intake.booking_customer(conn, "NEW BOOKING // C/Orchard & Pike Supply // S/X") == (None, "")
    assert intake.booking_customer(conn, "RATE REQUEST // C/Wren Mills Co") == (None, "")


def test_a_word_many_party_names_share_offers_nobody_and_the_whole_name_wins(world):
    conn, repo, ids = world
    keys = [m.key for m in matching.by_name(conn, "ORCHARD AND PIKE SUPPLY")]
    assert keys == ["Orchard & Pike Supply"]
    # 'supply' is in six party names: no partial match on it ('Supply Outfit' is offered for nothing)
    assert matching.by_name(conn, "HARROW SUPPLY") == []
    # a rare word still matches part of a name
    assert [m.key for m in matching.by_name(conn, "QUILL")] == ["Quill Agents"]


# ---------------------------------------- D. a reply to our offer of services --
OUR_PROMOTION = header("JJC-MARA OSTEN", "mara@" + US, "Monday, November 30, 2026 9:00 AM",
                       "desk@fernmoor.test", "OURCO | ZQA to MIA Air Promotion") + (
    "\nHi Fernmoor team,\nWe have a very competitive promotional air rate ZQA to MIA this month.\n"
    "Tell us if that would suit you.\nMara\n")
OUR_RATE_REQUEST = header("JJC-MARA OSTEN", "mara@" + US, "Monday, November 30, 2026 9:00 AM",
                          "desk@fernmoor.test", "RATE REQUEST ZQA - MIA") + (
    "\nHi Fernmoor team,\nPlease quote your best air rate ZQA to MIA, 2 plts.\nMara\n")


def test_a_reply_to_our_promotion_asking_for_a_rate_is_their_inquiry(world):
    conn, repo, ids = world
    theirs = mail("RE: OURCO | ZQA to MIA Air Promotion",
                  "Dear Mara,\nNothing from ZQA now, but can you please quote PEK to SCL, 140 cartons 2,100 kg?\n"
                  "Arlo\n\n" + OUR_PROMOTION,
                  sender="arlo@fernmoor.test", name="Arlo Fenn", internal=False, to=["mara@" + US])
    draft = intake.build(conn, theirs)
    assert requester(conn, draft) == "Fernmoor Lines"
    assert not draft.vendor_thread


def test_a_reply_to_our_rate_request_is_still_a_vendor_thread(world):
    conn, repo, ids = world
    theirs = mail("RE: RATE REQUEST ZQA - MIA", "Dear Mara,\nCan you confirm the dimensions?\nArlo\n\n"
                  + OUR_RATE_REQUEST, sender="arlo@fernmoor.test", name="Arlo Fenn", internal=False,
                  to=["mara@" + US])
    draft = intake.build(conn, theirs)
    assert draft.vendor_thread and draft.vendor_thread["domain"] == "fernmoor.test"
    assert requester(conn, draft) == "(TBC)"


def test_our_first_mail_kinds():
    from opplist import parsing
    promo, request = (parsing.quoted_mails(OUR_PROMOTION)[-1], parsing.quoted_mails(OUR_RATE_REQUEST)[-1])
    assert not intake.our_rate_request(promo) and intake.our_rate_request(request)


# ------------------------------------------------------------ reader-check --
class Archive:
    def __init__(self, mails):
        self.mails = mails

    def by_entry_id(self, entry_id):
        return self.mails.get(entry_id)

    def by_message_id(self, mid):
        return None

    def by_conversation(self, conversation, limit=500):
        return []


def test_reader_check_counts_and_compares(world, tmp_path, monkeypatch):
    conn, repo, ids = world
    monkeypatch.setattr(reader_check, "REPORT_DIR", tmp_path / "reports")
    add_enquiry(repo, "OPP-8601", ids["JoJo QRX"], received="2026-12-02", entry_id="ENTRY-8601",
                origin_country="USA", dest_country="Chile")
    add_enquiry(repo, "OPP-8602", ids["Quill Agents"], received="2026-12-03", entry_id="ENTRY-8602")
    repo.commit()
    inquiry = mail("Air inquiry ZQA - SAI", "Hi Mara,\nPlease quote air ZQA to SAI.\nLin\n",
                   sender="lin@" + US, name="QRX-LIN HOLLOWAY", to=["mara@" + US], to_names=["JJC-MARA OSTEN"])
    result = reader_check.check(conn, Archive({"ENTRY-8601": inquiry}), ["2026-12"])
    cases = result["2026-12"]
    assert cases["OPP-8602"] == {"missing": True}
    assert cases["OPP-8601"]["requester"] == ["JoJo QRX", "JoJo QRX", "right"]
    counts = reader_check.counts(result)["2026-12"]["requester"]
    assert counts == {"value": 1, "right": 1, "wrong": 0, "empty": 0}
    before = json.loads(json.dumps(result))
    before["2026-12"]["OPP-8601"]["requester"] = ["JoJo QRX", "(TBC)", "wrong"]
    worse, better = reader_check.changes(before, result)
    assert worse == [] and [(m, o, f) for m, o, f, *_ in better] == [("2026-12", "OPP-8601", "requester")]
    path = reader_check.write_report(result, before, today="2026-12-04", compared_with="before.md")
    text = path.read_text(encoding="utf-8")
    assert "| Requester | 1 (1) | 1 (0) (100%) | 0 (1) | 0 (0) |" in text
    assert "From not right to right (1)" in text and "Please quote" not in text
    assert json.loads(path.with_suffix(".json").read_text(encoding="utf-8")) == result
    # a second report the same day does not overwrite the first
    assert reader_check.write_report(result, today="2026-12-04").name == "reader-check-2026-12-04-2.md"
