"""Batch D1: the requester of our reply is the chain's mail written to us;
a thread we started is a vendor thread; the New party's Domain can be
changed; a domain is remembered only for the party it was read for, and a
wrong one can be moved.

Every company, person, mail, place, date and number here is invented;
cases from OPP-3901.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_party, seed_reference
from opplist import db, intake, outlook_bridge
from opplist.web import create_app

FORWARDER = "tomas@larchgate.example"          # a forwarder that wrote to the customer
CUSTOMER = "pell@brindlefarm.example"           # the direct customer that asked us
AGENT = "ines@quillagents.example"              # an agent of ours
VENDOR = "desk@fernmoor.example"                # a warehouse we asked


def header(name, address, sent, to, subject, cc=""):
    return ("From: {} <{}>\nSent: {}\nTo: {}\n{}Subject: {}\n".format(
        name, address, sent, to, "Cc: {}\n".format(cc) if cc else "", subject))


FORWARDER_TO_CUSTOMER = header("Tobias Reyes", FORWARDER, "Monday, November 9, 2026 9:05 AM",
                               "Pell Ardent <{}>".format(CUSTOMER), "Quote DDP Brindle Field") + (
    "\nHi Pell,\nOur rate for the two crates to Brindle Field is attached.\nTomas\n")
CUSTOMER_TO_US = header("Pell Ardent", CUSTOMER, "Monday, November 9, 2026 2:40 PM",
                        "JJC-MARA OSTEN <mara@jojofreight.example>", "FW: Quote DDP Brindle Field") + (
    "\nMara, can you help? Please quote DDP Brindle Field, 2 crates 380 kg.\nPell\n\n")


def mail(subject, body, sender="mara@jojofreight.example", name="JJC-MARA OSTEN", to=(), cc=(), internal=True,
         on="2026-11-10", key="3901"):
    return outlook_bridge.MailInfo(
        subject=subject, received_on=on, received_at=on + " 10:00", sender_address=sender, sender_name=name,
        sender_internal=internal, to_addresses=list(to), cc_addresses=list(cc),
        to_names=[""] * len(to), body_excerpt=body, entry_id="ENTRY-" + key, conversation_id="CONV-" + key)


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "d1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    ids = {"path": path}
    ids["customer"] = add_party(repo, "Brindle Farm", ptype="Direct Customer", country="USA")
    repo.insert("party_domain", {"domain": "brindlefarm.example", "party_id": ids["customer"]})
    ids["agent"] = add_party(repo, "Quill Agents", country="Chile")
    repo.insert("party_domain", {"domain": "quillagents.example", "party_id": ids["agent"]})
    ids["tbc"] = add_party(repo, "(TBC)", ptype="Unknown", country="Unknown", placeholder=1)
    db.set_setting(conn, "next_opp_number", 3901)
    repo.commit()
    yield conn, repo, ids
    conn.close()


@pytest.fixture
def client(world, monkeypatch):
    conn, repo, ids = world
    app = create_app(ids["path"])
    app.config["TESTING"] = True
    holder = {"mail": None}
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: holder["mail"])
    return app.test_client(), holder


# -------------------------------------------------- 1. our reply's requester --
def test_our_reply_to_a_forwarder_cc_the_customer_who_asked_us(world):
    conn, repo, ids = world
    reply = mail("RE: Quote DDP Brindle Field",
                 "Hi Pell,\nPlease find our rate: USD 640 all-in to the door.\nMara\n\n"
                 + CUSTOMER_TO_US + FORWARDER_TO_CUSTOMER,
                 to=[FORWARDER], cc=[CUSTOMER])
    draft = intake.build(conn, reply)
    assert draft.values["requester_party_id"] == ids["customer"]
    assert draft.chain_line == "larchgate.example is in the chain but wrote to the requester, not to us"
    assert not draft.vendors and "end_customer" not in draft.values


def test_our_reply_to_the_agent_with_its_customer_below_is_unchanged(world):
    conn, repo, ids = world
    agent_to_us = header("Ines Calloway", AGENT, "Tuesday, November 10, 2026 8:15 AM",
                         "JJC-MARA OSTEN <mara@jojofreight.example>", "RATE REQUEST MIA - SAI") + (
        "\nPlease quote air MIA to SAI for our customer below.\nInes\n\n")
    customer_to_agent = header("Dov Hartwell", "dov@saltmarshtools.example", "Monday, November 9, 2026 4:00 PM",
                               "Ines Calloway <{}>".format(AGENT), "need a rate to Chile") + (
        "\nInes, we need a rate for 4 cartons to Santiago.\nDov\n")
    reply = mail("RE: RATE REQUEST MIA - SAI", "Ines, our rate is USD 3.10/kg all-in.\nMara\n\n"
                 + agent_to_us + customer_to_agent, to=[AGENT])
    draft = intake.build(conn, reply)
    assert draft.values["requester_party_id"] == ids["agent"]
    assert draft.values["end_customer"] == "Saltmarshtools" or "saltmarsh" in draft.values["end_customer"].lower()
    assert not draft.chain_line


def test_a_chain_with_no_mail_written_to_us_keeps_to_before_cc(world):
    conn, repo, ids = world
    agent_mail = header("Ines Calloway", AGENT, "Monday, November 9, 2026 9:00 AM",
                        "Pell Ardent <{}>".format(CUSTOMER), "rates please") + "\nPlease quote MIA to SAI.\n\n"
    customer_mail = header("Pell Ardent", CUSTOMER, "Monday, November 9, 2026 8:00 AM",
                           "Ines Calloway <{}>".format(AGENT), "rates please") + "\nCan you quote MIA to SAI?\n"
    reply = mail("RE: rates please", "Our rate is USD 900 all-in.\n\n" + agent_mail + customer_mail,
                 to=[AGENT], cc=[CUSTOMER])
    draft = intake.build(conn, reply)
    assert draft.values["requester_party_id"] == ids["agent"]
    assert not draft.chain_line


def test_a_colleague_s_forward_of_the_customer_s_forward_gives_the_customer(world):
    conn, repo, ids = world
    fwd = mail("FW: Quote DDP Brindle Field", "Can you register this one?\n\n" + CUSTOMER_TO_US + FORWARDER_TO_CUSTOMER,
               sender="lena@jojofreight.example", name="Lena Park", to=["mara@jojofreight.example"])
    draft = intake.build(conn, fwd)
    assert draft.values["requester_party_id"] == ids["customer"]
    assert draft.values["received_on"] == "2026-11-09"
    assert "end_customer" not in draft.values
    assert draft.chain_line == "larchgate.example is in the chain but wrote to the requester, not to us"


# ------------------------------------------------- 1b. a thread we started --
OUR_ASK = header("JJC-DARA VOSS", "dara@jojofreight.example", "Monday, November 2, 2026 11:00 AM",
                 "Fern Moor Storage <{}>".format(VENDOR), "Warehousing Port Alder") + (
    "\nHello, please quote storage for 6 pallets in Port Alder, 30 days.\nDara\n")
THEIR_REPLY = header("Fern Moor Storage", VENDOR, "Tuesday, November 3, 2026 9:30 AM",
                     "JJC-DARA VOSS <dara@jojofreight.example>", "RE: Warehousing Port Alder") + (
    "\nHi Dara, which dates? And are the pallets stackable?\n\n")


def follow_up(key="3902"):
    return mail("RE: Warehousing Port Alder", "Hi, any update on the rate?\nDara\n\n" + THEIR_REPLY + OUR_ASK,
                sender="dara@jojofreight.example", name="JJC-DARA VOSS", to=[VENDOR], on="2026-11-06", key=key)


def vendor_reply(key="3903"):
    return mail("RE: Warehousing Port Alder", "Dara, USD 14 per pallet per week.\n\n" + OUR_ASK,
                sender=VENDOR, name="Fern Moor Storage", internal=False, to=["dara@jojofreight.example"],
                on="2026-11-07", key=key)


def test_a_thread_we_started_is_a_vendor_thread(world):
    conn, repo, ids = world
    draft = intake.build(conn, follow_up())
    assert draft.vendor_thread == {"domain": "fernmoor.example", "asked_on": "2026-11-02", "key": "Fernmoor"}
    assert [(v["domain"], v["checked"]) for v in draft.vendors] == [("fernmoor.example", True)]
    assert draft.values["requester_party_id"] == ids["tbc"]
    assert "a vendor thread, not an inquiry" in draft.forward_line
    # the vendor's own reply: no vendor offered until Log anyway, never a requester
    theirs = intake.build(conn, vendor_reply())
    assert theirs.values["requester_party_id"] == ids["tbc"] and not theirs.vendors
    assert intake.build(conn, vendor_reply(), force_asking=True).vendors[0]["domain"] == "fernmoor.example"


def test_logging_from_a_vendor_thread_uses_the_first_mail_s_date_once(world, client):
    conn, repo, ids = world
    web, holder = client
    case = repo.create_enquiry({"requester_party_id": ids["agent"], "received_on": "2026-11-02",
                                "origin_loc": "MIA", "origin_country": "USA", "dest_loc": "SAI",
                                "dest_country": "Chile", "service_mode": "Air"}, status_at="2026-11-02")
    repo.commit()
    holder["mail"] = follow_up()
    page = web.post("/enquiries/{}/vendors/from-outlook".format(case), follow_redirects=True).data.decode()
    assert "Logged 1 vendor request: Fernmoor" in page
    holder["mail"] = vendor_reply()
    page = web.post("/enquiries/{}/vendors/from-outlook".format(case), follow_redirects=True).data.decode()
    assert "Fernmoor&#39;s reply in a thread we started on Nov 2 - nothing was logged." in page
    page = web.post("/enquiries/{}/vendors/from-outlook".format(case), data={"anyway": "1", "entry_id": "ENTRY-3903"},
                    follow_redirects=True).data.decode()
    assert "already on the case: Fernmoor" in page
    rows = conn.execute("SELECT asked_on FROM vendor_request WHERE enquiry_id = ?", (case,)).fetchall()
    assert [r[0] for r in rows] == ["2026-11-02"]


def test_register_from_outlook_on_a_vendor_thread_names_it_our_own_request(world, client):
    web, holder = client
    holder["mail"] = vendor_reply()
    page = web.post("/enquiries/from-outlook").data.decode()
    assert "Our own request to Fernmoor (fernmoor.example), first sent Nov 2: a vendor thread, not an inquiry" in page


# ---------------------------------------------- 2. the New party's Domain --
def new_party_form(ids, **extra):
    values = {"received_on": "2026-11-10", "requester_party_id": "", "service_mode": "FCL",
              "container_type": "40HQ", "container_qty": "1",
              "origin_country": "USA", "origin_loc": "MIA", "dest_country": "Chile", "dest_loc": "SAI",
              "commodity": "Crates", "decision": "new", "created_by": "outlook",
              "sender_address": "mara@jojofreight.example", "sender_internal": "1",
              "new_party_key": "Thornbury Freight", "new_party_type": "Agent", "new_party_country": "USA",
              "new_party_domain": "thornbury.example", "new_party_domain_shown": "1",
              "learn_domain": "thornbury.example", "learn_for": "new"}
    values.update(extra)
    return values


def domain_owner(conn, domain):
    row = conn.execute("SELECT p.key FROM party_domain d JOIN party p ON p.id = d.party_id WHERE d.domain = ?",
                       (domain,)).fetchone()
    return row[0] if row else None


def test_the_new_party_s_domain_can_be_edited(world, client):
    conn, repo, ids = world
    web, _ = client
    page = web.post("/enquiries/new", data=new_party_form(ids, new_party_domain="thornburyfreight.example"),
                    follow_redirects=True).data.decode()
    assert "Remembered thornburyfreight.example for Thornbury Freight." in page
    assert domain_owner(conn, "thornburyfreight.example") == "Thornbury Freight"
    assert domain_owner(conn, "thornbury.example") is None


def test_the_new_party_s_domain_can_be_emptied(world, client):
    conn, repo, ids = world
    web, _ = client
    page = web.post("/enquiries/new", data=new_party_form(ids, new_party_key="Ashcombe Lines",
                                                          new_party_domain=""), follow_redirects=True).data.decode()
    assert "Remembered" not in page
    party = conn.execute("SELECT id FROM party WHERE key = 'Ashcombe Lines'").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM party_domain WHERE party_id = ?", (party,)).fetchone()[0] == 0


@pytest.mark.parametrize("domain,said", [
    ("gmail.com", "gmail.com is a public mailbox, so the company behind it is not guessed - clear the domain."),
    ("quillagents.example", "quillagents.example already belongs to Quill Agents (/parties/"),
    ("thornbury example", "is not a mail domain"),
    ("someone@thornbury.example", "is not a mail domain"),
])
def test_the_new_party_s_domain_is_checked(world, client, domain, said):
    conn, repo, ids = world
    web, _ = client
    page = web.post("/enquiries/new", data=new_party_form(ids, new_party_domain=domain),
                    follow_redirects=True).data.decode()
    assert said.replace("'", "&#39;") in page
    assert conn.execute("SELECT COUNT(*) FROM enquiry").fetchone()[0] == 0       # nothing saved
    assert domain_owner(conn, domain) in (None, "Quill Agents")
    assert 'name="new_party_domain"' in page              # the box stays, as she left it


# ------------------------------- 3. remembered only for the party it was read for --
def test_another_existing_party_chosen_remembers_nothing(world, client):
    conn, repo, ids = world
    web, _ = client
    stem = add_party(repo, "Larchgate", country="USA")
    repo.commit()
    form = new_party_form(ids, new_party_key="", new_party_domain="", requester_party_id=str(ids["agent"]),
                          learn_domain="larchgate.example", learn_for=str(stem))
    form.pop("new_party_domain_shown")
    form.pop("new_party_domain")
    page = web.post("/enquiries/new", data=form, follow_redirects=True).data.decode()
    assert "Remembered" not in page and domain_owner(conn, "larchgate.example") is None
    # the stem-match party chosen: remembered
    form["requester_party_id"] = str(stem)
    form["received_on"] = "2026-11-11"
    page = web.post("/enquiries/new", data=form, follow_redirects=True).data.decode()
    assert "Remembered larchgate.example for Larchgate." in page
    assert domain_owner(conn, "larchgate.example") == "Larchgate"


def test_the_draft_says_whom_the_domain_was_read_for(world):
    conn, repo, ids = world
    stem = add_party(repo, "Larchgate", country="USA")
    repo.commit()
    reply = mail("RE: Quote DDP Brindle Field", "Our rate is USD 640 all-in.\n\n"
                 + header("Tobias Reyes", "tomas@larchgate.example", "Monday, November 9, 2026 9:05 AM",
                          "JJC-MARA OSTEN <mara@jojofreight.example>", "Quote DDP Brindle Field")
                 + "\nPlease quote DDP Brindle Field.\n", to=[FORWARDER])
    draft = intake.build(conn, reply)
    assert (draft.learn_domain, draft.learn_for) == ("larchgate.example", str(stem))
    other = intake.build(conn, mail("RE: rates", "Our rate is USD 1 all-in.\n\n" + header(
        "Wren Hale", "wren@oxbowcargo.example", "Monday, November 9, 2026 9:05 AM",
        "JJC-MARA OSTEN <mara@jojofreight.example>", "rates") + "\nPlease quote MIA - SAI.\n", to=["wren@oxbowcargo.example"]))
    assert (other.learn_domain, other.learn_for) == ("oxbowcargo.example", "new")


# ----------------------------------------------- 4. moving a domain --
def test_a_domain_moves_between_parties_with_change_log(world, client):
    conn, repo, ids = world
    web, _ = client
    page = web.get("/parties/{}".format(ids["agent"])).data.decode()
    assert "move to..." in page
    page = web.post("/parties/{}/domains/move".format(ids["agent"]),
                    data={"domain": "quillagents.example", "other_key": "brindle farm"},
                    follow_redirects=True).data.decode()
    assert "Moved quillagents.example from Quill Agents to Brindle Farm." in page
    assert domain_owner(conn, "quillagents.example") == "Brindle Farm"
    logged = conn.execute("SELECT old_value, new_value, source FROM change_log WHERE table_name = 'party_domain' "
                          "AND field = 'party_id' ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(logged) == (str(ids["agent"]), str(ids["customer"]), "ui")
    assert conn.execute("SELECT COUNT(*) FROM party_domain").fetchone()[0] == 2      # nothing deleted


def test_the_service_list_is_saved_from_settings(world, client):
    conn, repo, ids = world
    web, _ = client
    page = web.get("/settings").data.decode()
    assert 'name="service_scopes"' in page and "Door to door" in page
    web.post("/settings", data={"service_scopes": "Door to door\nDoor to port\nPort to port\nCFS to door"})
    assert db.get_setting_list(conn, "service_scopes") == ["Door to door", "Door to port", "Port to port",
                                                           "CFS to door"]
