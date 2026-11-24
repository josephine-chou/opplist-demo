"""Batch W3b (2026-11-11): the quote card's reply, made safe - our own
mail is never answered (a new mail to the requester on it), a vendor on
the mail takes the mid and every vendor address away, one of our offices
signing with its prefix gives the word after it, and `via` says which.

Every company, person, mail, place and date here is invented; cases from
OPP-9971, domains .test.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, firebase_reply, firebase_sync as fs, outlook_bridge
from test_firebase_sync import FakeFirestore
from test_w3_quote_card import FakeArchive

TODAY = "2026-12-14"
HERS = "desk@jojofreight.test"
COLLEAGUE = "wendell@jojofreight.test"
AGENT = "ilse@ferromar.test"
AGENT_TOO = "bruno@ferromar.test"
VENDOR_A = "rates@kestrelline.test"
VENDOR_B = "pricing@duneport.test"
OFFICE = "jacky.li@jojo-jjz.test"


def mail(entry, **kw):
    kw.setdefault("subject", "Rates please // OMAHA - QUITO")
    kw.setdefault("received_on", "2026-12-04")
    kw.setdefault("received_at", "2026-12-04 10:00")
    return outlook_bridge.MailInfo(entry_id=entry, conversation_id="C-" + entry,
                                   message_id="<{}@mail.test>".format(entry.lower()), **kw)


def mails():
    return [
        # 9971: our colleague's mail, the requester on CC with its display name (the archive keeps
        # one line of names for To and CC together)
        mail("E-9971", sender_name="JJC-WENDELL ROOK", sender_address=COLLEAGUE, sender_internal=True,
             to_addresses=[HERS], to_names=["JJC-DESK", "Ilse Brandt", "Bruno Sohl"],
             cc_addresses=["ILSE@ferromar.test", AGENT_TOO]),
        # 9972: our rate request to two vendors
        mail("E-9972", subject="LCL RATE REQUEST // OMAHA - QUITO", sender_name="JJC-WENDELL ROOK",
             sender_address=COLLEAGUE, sender_internal=True, to_addresses=[VENDOR_A, VENDOR_B],
             to_names=["Kestrel Line", "Duneport"], cc_addresses=[HERS]),
        # 9973: an internal-only mail
        mail("E-9973", sender_name="JJC-WENDELL ROOK", sender_address=COLLEAGUE, sender_internal=True,
             to_addresses=[HERS, "ops@jojofreight.test"], to_names=["JJC-DESK", "JJC-OPS"]),
        # 9974: the requester's own mail with a vendor on CC
        mail("E-9974", sender_name="Ilse Brandt", sender_address=AGENT,
             to_addresses=[HERS], to_names=["JJC-DESK"], cc_addresses=[VENDOR_A, AGENT_TOO]),
        # 9975: an office requester's own mail, signed with its prefix
        mail("E-9975", sender_name="JJZ-JACKY LI", sender_address=OFFICE,
             to_addresses=[HERS], to_names=["JJC-DESK"], cc_addresses=["JJZ-OPS@jojo-jjz.test"]),
        # 9976: a colleague's forward of the agent's mail
        mail("E-9976", subject="Fwd: Rates please // OMAHA - QUITO", sender_name="Wendell Rook",
             sender_address=COLLEAGUE, sender_internal=True, to_addresses=[HERS],
             body_excerpt="Can you take this one?\n\nFrom: Ilse Brandt <{}>\nSent: Thursday, December 3, 2026 9:05 AM\n"
                          "To: JJC-DESK <{}>\nSubject: Rates please // OMAHA - QUITO\n\nHello,\nPlease quote.\nIlse\n"
                          .format(AGENT, HERS)),
        # 9977: our colleague's mail to the office JJZ, its people known by their prefix only
        mail("E-9977", sender_name="JJC-WENDELL ROOK", sender_address=COLLEAGUE, sender_internal=True,
             to_addresses=[OFFICE, "mei.fan@jojo-jjz.test"], cc_addresses=[HERS],
             to_names=["JJZ-JACKY LI", "JJZ-MEI FAN"], cc_names=["JJC-DESK"]),
        # 9978: the names line does not count the same as the addresses: no name is used
        mail("E-9978", sender_name="JJC-WENDELL ROOK", sender_address=COLLEAGUE, sender_internal=True,
             to_addresses=[OFFICE], cc_addresses=[HERS], to_names=["#JJC-GROUP", "JJZ-JACKY LI", "JJC-DESK"]),
    ]


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "w3b.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.test", "jojo-jjz.test"])
    agent = add_party(repo, "Ferromar", country="Chile")
    repo.insert("contact", {"party_id": agent, "name": "Ilse Brandt", "email": AGENT, "is_primary": 1})
    repo.insert("contact", {"party_id": agent, "name": "Bruno Sohl", "email": AGENT_TOO})
    office = add_party(repo, "JoJo JJZ", ptype="Office", country="Unknown")
    kestrel = add_party(repo, "Kestrel Line", ptype="Carrier")
    repo.insert("contact", {"party_id": kestrel, "name": "Rate Desk", "email": VENDOR_A})
    duneport = add_party(repo, "Duneport", ptype="Carrier")
    repo.insert("contact", {"party_id": duneport, "name": "Pricing", "email": VENDOR_B})
    ids = {}
    for opp, party, vendors in [("OPP-9971", agent, ()), ("OPP-9972", agent, (kestrel, duneport)),
                                ("OPP-9973", agent, ()), ("OPP-9974", agent, (kestrel,)),
                                ("OPP-9975", office, ()), ("OPP-9976", agent, ()),
                                ("OPP-9977", office, ()), ("OPP-9978", office, ())]:
        ids[opp] = add_enquiry(repo, opp, party, received="2026-12-04", origin_loc="MIA", dest_loc="SAI",
                               service_mode="LCL", scope="In", entry_id="E-" + opp[4:],
                               source_subject="Rates please // OMAHA - QUITO")
        for v in vendors:
            repo.insert("vendor_request", {"enquiry_id": ids[opp], "vendor_party_id": v, "asked_on": "2026-12-04"})
    repo.commit()
    archive = FakeArchive(mails())
    monkeypatch.setattr(archive, "own_addresses", lambda: {HERS})
    monkeypatch.setattr(fs, "_archive_search", lambda: archive)
    yield {"conn": conn, "repo": repo, "path": path, "ids": ids, "archive": archive, "state": tmp_path / "fs.json",
           "record": tmp_path / "exports.json", "reports": tmp_path / "reports", "kestrel": kestrel}
    conn.close()


def run(w, fake, **kw):
    kw.setdefault("today", TODAY)
    return fs.sync(w["path"], client=fake, state_file=w["state"], reports_dir=w["reports"], record=w["record"], **kw)


@pytest.fixture
def replies(world):
    fake = FakeFirestore()
    assert run(world, fake).ok
    return {opp: fake.docs["cases/" + opp]["reply"] for opp in world["ids"]}


def test_our_colleagues_mail_with_the_requester_on_cc_is_a_new_mail_to_the_requester(replies):
    assert replies["OPP-9971"] == {
        "on": "2026-12-04", "subject": "RE: [OPP-9971] Rates please // OMAHA - QUITO", "mid": "",
        "name": "Ilse Brandt", "first": "Ilse", "to": "ILSE@ferromar.test", "cc": [AGENT_TOO, COLLEAGUE],
        "via": "colleague"}


def test_our_rate_request_to_two_vendors_goes_to_the_primary_contact_and_no_vendor(replies):
    r = replies["OPP-9972"]
    assert (r["to"], r["name"], r["first"], r["mid"], r["via"]) == (AGENT, "Ilse Brandt", "Ilse", "", "colleague")
    assert r["cc"] == [COLLEAGUE]
    assert r["subject"] == "RE: [OPP-9972] LCL RATE REQUEST // OMAHA - QUITO"


def test_an_internal_only_mail_goes_to_the_primary_contact_cc_the_colleague(replies):
    r = replies["OPP-9973"]
    assert (r["to"], r["name"], r["mid"], r["cc"], r["via"]) == (AGENT, "Ilse Brandt", "", [COLLEAGUE], "colleague")


def test_the_requesters_own_mail_with_a_vendor_on_cc_loses_the_mid_and_the_vendor(replies):
    r = replies["OPP-9974"]
    assert (r["to"], r["name"], r["mid"], r["cc"], r["via"]) == (AGENT, "Ilse Brandt", "", [AGENT_TOO], "")


def test_an_office_requesters_own_mail_keeps_the_mid_and_the_word_after_the_prefix(replies):
    r = replies["OPP-9975"]
    assert (r["to"], r["name"], r["first"], r["mid"], r["via"]) == (OFFICE, "JJZ-JACKY LI", "Jacky",
                                                                     "<e-9975@mail.test>", "")
    assert r["cc"] == ["JJZ-OPS@jojo-jjz.test"]


def test_a_colleagues_forward_keeps_w3s_answer_with_via_forward(replies):
    assert replies["OPP-9976"] == {
        "on": "2026-12-04", "subject": "RE: [OPP-9976] Rates please // OMAHA - QUITO", "mid": "",
        "name": "Ilse Brandt", "first": "Ilse", "to": AGENT, "cc": [COLLEAGUE], "via": "forward"}


def test_the_vendors_are_read_on_every_push_not_only_when_searched(world):
    w = world
    fake = FakeFirestore()
    assert run(w, fake).ok
    assert fake.docs["cases/OPP-9974"]["reply"]["mid"] == ""
    # the vendor request struck out: Kestrel is no vendor of the case, the mid and the address come back
    vr = w["conn"].execute("SELECT id FROM vendor_request WHERE enquiry_id = ?", (w["ids"]["OPP-9974"],)).fetchone()[0]
    w["repo"].update("vendor_request", vr, {"voided": 1})
    w["repo"].commit()
    again = run(w, fake)
    assert again.ok and again.replies.searched == 0
    assert fake.docs["cases/OPP-9974"]["reply"]["mid"] == "<e-9974@mail.test>"
    assert fake.docs["cases/OPP-9974"]["reply"]["cc"] == [VENDOR_A, AGENT_TOO]
    # asked again: gone again, still no search
    w["repo"].update("vendor_request", vr, {"voided": 0})
    w["repo"].commit()
    later = run(w, fake)
    assert later.replies.searched == 0 and fake.docs["cases/OPP-9974"]["reply"]["cc"] == [AGENT_TOO]


def test_the_rules_version_looks_every_open_case_up_once(world, monkeypatch):
    w = world
    fake = FakeFirestore()
    monkeypatch.setattr(firebase_reply, "RULES", "W3")
    assert run(w, fake).ok
    monkeypatch.setattr(firebase_reply, "RULES", "W3b")
    first = run(w, fake)
    assert first.ok and first.replies.searched == len(w["ids"])
    assert run(w, fake).replies.searched == 0


def test_the_kept_answer_is_the_mails_facts_not_the_reply(world):
    assert run(world, FakeFirestore()).ok
    kept = fs.load_state(world["state"])["replies"]["OPP-9974"]
    assert kept["found"] and "reply" not in kept
    assert kept["mail"]["cc"] == [VENDOR_A, AGENT_TOO] and kept["mail"]["own"] == [HERS]


def test_our_colleagues_mail_to_an_office_goes_to_the_first_of_its_people(replies):
    r = replies["OPP-9977"]
    assert (r["to"], r["name"], r["first"], r["mid"], r["via"]) == (OFFICE, "JJZ-JACKY LI", "Jacky", "", "colleague")
    assert r["cc"] == ["mei.fan@jojo-jjz.test", COLLEAGUE]


def test_names_that_do_not_count_the_same_are_not_used(replies):
    # the office has no contact: nothing names an address of it, the case knows no one
    r = replies["OPP-9978"]
    assert (r["to"], r["name"], r["cc"], r["mid"], r["via"]) == ("", "", [COLLEAGUE], "", "colleague")
