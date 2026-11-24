"""Batch AL3: registering an inquiry a colleague forwarded.

A JJC colleague's forward is read from the forwarded chain: the oldest
mail written by somebody outside JJC gives the requester, Received on and
Handled by (its To line, batch 10's rule), each with a source note; JJC's
own mails are skipped. A chain whose newest mail is more than 30 days
older than the forward is an old thread passed on: Received on stays the
forward's, under an amber line. Saving keeps no note of the forward (CX1:
the form's 'Forwarded by <name>, <date>' is only the forward flag). A chain that cannot be read keeps today's prefill under
an amber line; another office's forward keeps the office as requester
with one muted line.

Every company, person, mail, place pairing, date and number here is
invented; cases from OPP-3201."""

from __future__ import annotations

import pytest

from conftest import add_party, seed_reference
from opplist import db, intake, outlook_bridge, parsing
from opplist.web import create_app

AGENT = "rosa@quillagents.example"
WHEN = "2026-11-01 10:14"
NOTE = "from the forwarded chain, first outside mail: Nov 1, 10:14 AM, " + AGENT

# the same forwarded mail, as each client writes its header block
HEADERS = {
    "outlook-en": ("From: Rosa Teller <{a}>\nSent: Sunday, November 1, 2026 10:14 AM\n"
                   "To: JJK-NOOR HADI <noor@jojofreight.example>\nCc: Desk <desk@jojofreight.example>\n"
                   "Subject: RATE REQUEST // MIA - SAI\n"),
    "outlook-es": ("De: Rosa Teller [mailto:{a}]\nEnviado el: domingo, 1 de noviembre de 2026 10:14\n"
                   "Para: JJK-NOOR HADI\nCC: Desk\nAsunto: RATE REQUEST // MIA - SAI\n"),
    "outlook-pt": ("De: Rosa Teller <{a}>\nEnviada em: domingo, 1 de novembro de 2026 10:14\n"
                   "Para: JJK-NOOR HADI <noor@jojofreight.example>\nCc: Desk\nAssunto: RATE REQUEST // MIA - SAI\n"),
    "gmail": ("---------- Forwarded message ---------\nFrom: Rosa Teller <{a}>\n"
              "Date: Sun, Nov 1, 2026 at 10:14 AM\nSubject: RATE REQUEST // MIA - SAI\n"
              "To: JJK-NOOR HADI <noor@jojofreight.example>, Desk <desk@jojofreight.example>\n"),
}
ASK = "\nHello team,\nPlease quote air MIA to SAI, 3 cartons, 120 kg, general cargo.\nRosa\n"


def forward(header, subject="FW: RATE REQUEST // MIA - SAI", top="Lena, can you register this one?\n\n",
            sender="lena@jojofreight.example", name="Lena Park", key="3201"):
    """A colleague forwards the agent's mail to the desk on Nov 4."""
    return outlook_bridge.MailInfo(
        subject=subject, received_on="2026-11-04", received_at="2026-11-04 09:40",
        sender_address=sender, sender_name=name, sender_internal=True,
        to_addresses=["desk@jojofreight.example"], to_names=["Desk"],
        body_excerpt=top + header.format(a=AGENT) + ASK,
        entry_id="ENTRY-" + key, conversation_id="CONV-" + key)


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "fwd.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    quill = add_party(repo, "Quill Agents", country="Chile")
    repo.insert("party_domain", {"domain": "quillagents.example", "party_id": quill})
    brightwater = add_party(repo, "Brightwater Logistics", country="Brazil")
    repo.insert("contact", {"party_id": brightwater, "name": "Odile Marsh", "email": "someone@gmail.com"})
    kul = add_party(repo, "JoJo JJK", ptype="Office", country="Unknown")
    db.set_setting(conn, "next_opp_number", 3201)
    repo.commit()
    yield conn, path, {"quill": quill, "brightwater": brightwater, "kul": kul}
    conn.close()


# ------------------------------------------------------- 1. the header block --
@pytest.mark.parametrize("style", sorted(HEADERS))
def test_a_forward_in_each_header_style_gives_the_agent_and_the_original_date(world, style):
    conn, path, ids = world
    draft = intake.build(conn, forward(HEADERS[style]))
    assert draft.values["requester_party_id"] == ids["quill"]
    assert draft.values["received_on"] == "2026-11-01"
    assert draft.sources["requester_party_id"] == NOTE and draft.sources["received_on"] == NOTE
    # batch 10's rule on the forwarded To line: JJK in To, no JJC person
    assert draft.values["handled_by"] == "JJK" and draft.sources["handled_by"] == NOTE
    assert draft.forward_note == "Forwarded by Lena Park, Nov 4, 9:40 AM"
    assert not draft.forward_line
    # the registered subject is the forward's own, cleaned as ever
    assert draft.values["source_subject"] == "FW: RATE REQUEST // MIA - SAI"
    assert draft.values["thread_key"] == parsing.parse_subject("RATE REQUEST // MIA - SAI").thread_key


def test_a_forward_found_by_its_forwarded_message_line_alone(world):
    conn, path, ids = world
    draft = intake.build(conn, forward(HEADERS["gmail"], subject="RATE REQUEST // MIA - SAI"))
    assert draft.values["requester_party_id"] == ids["quill"] and draft.values["received_on"] == "2026-11-01"


def test_a_chi_person_on_the_forwarded_to_line_leaves_handled_by_empty(world):
    conn, path, ids = world
    header = HEADERS["outlook-en"].replace("JJK-NOOR HADI <noor@jojofreight.example>",
                                           "JJC-LENA PARK <lena@jojofreight.example>; JJK-NOOR HADI <noor@jojofreight.example>")
    draft = intake.build(conn, forward(header))
    assert "handled_by" not in draft.values
    assert draft.values["requester_party_id"] == ids["quill"]


# a later reply on top: Rosa chasing on Nov 3, then JJC answering, then her ask of Nov 1
CHASER = ("From: Rosa Teller <{a}>\nSent: Tuesday, November 3, 2026 4:05 PM\n"
          "To: JJC-LENA PARK <lena@jojofreight.example>\nSubject: RE: RATE REQUEST // MIA - SAI\n\nAny news?\n\n")
OUR_REPLY = ("From: JJC-LENA PARK <lena@jojofreight.example>\nSent: Sunday, November 1, 2026 3:30 PM\n"
             "To: Rosa Teller <{a}>\nSubject: RE: RATE REQUEST // MIA - SAI\n\nWorking on it.\n\n")


def test_in_a_chain_the_oldest_outside_mail_is_the_inquiry_not_the_later_reply_on_top(world):
    conn, path, ids = world
    draft = intake.build(conn, forward(CHASER + OUR_REPLY + HEADERS["outlook-en"]))
    assert draft.values["requester_party_id"] == ids["quill"]
    assert draft.values["received_on"] == "2026-11-01"
    assert draft.sources["received_on"] == NOTE
    # Handled by from that same mail's To line (JJK), not the chaser's (a JJC person)
    assert draft.values["handled_by"] == "JJK"
    assert not draft.forward_line


def test_a_chain_that_starts_with_our_own_mail_skips_it(world):
    conn, path, ids = world
    ours_first = ("From: JJC-LENA PARK <lena@jojofreight.example>\nSent: Friday, October 30, 2026 8:00 AM\n"
                  "To: Rosa Teller <{a}>\nSubject: Rates for next week?\n\nDo you have cargo for us?\n")
    draft = intake.build(conn, forward(HEADERS["outlook-en"] + ASK + ours_first))
    assert draft.values["requester_party_id"] == ids["quill"]
    assert draft.values["received_on"] == "2026-11-01" and draft.sources["received_on"] == NOTE
    # a chain of JJC's own mails only cannot be read
    only_ours = intake.build(conn, forward(OUR_REPLY.replace("{a}", "rosa")))
    assert only_ours.forward_level == "check" and "requester_party_id" not in only_ours.values


def test_an_old_thread_passed_on_keeps_the_forward_s_date_under_the_amber_line(world):
    conn, path, ids = world
    old = (HEADERS["outlook-en"].replace("Sunday, November 1, 2026", "Thursday, September 17, 2026")
           + ASK + "\nFrom: Rosa Teller <{a}>\nSent: Tuesday, September 1, 2026 9:00 AM\n"
           "To: JJK-NOOR HADI <noor@jojofreight.example>\nSubject: first ask\n\nhello\n")
    draft = intake.build(conn, forward(old))
    assert draft.values["received_on"] == "2026-11-04"                 # the forward's own date
    assert draft.values["requester_party_id"] == ids["quill"]           # the requester still from the chain
    assert draft.sources["requester_party_id"] == (
        "from the forwarded chain, first outside mail: Sep 1, 9:00 AM, " + AGENT)
    assert "handled_by" not in draft.values
    assert draft.forward_level == "check"
    assert draft.forward_line == ("Forwarded mail - an old thread passed on (Sep 1, 2026 to Sep 17, 2026): "
                                  "check Received on and Requester")
    # 30 days is not yet old: the newest mail on Oct 5, the forward on Nov 4
    recent = intake.build(conn, forward(HEADERS["outlook-en"].replace("Sunday, November 1, 2026",
                                                                      "Monday, October 5, 2026")))
    assert recent.values["received_on"] == "2026-10-05" and not recent.forward_line


def test_a_sender_named_without_an_address_matches_a_contact(world):
    conn, path, ids = world
    header = HEADERS["outlook-en"].replace("Rosa Teller <{a}>", "Odile Marsh")
    draft = intake.build(conn, forward(header))
    assert draft.values["requester_party_id"] == ids["brightwater"]
    assert draft.sources["requester_party_id"] == "from the forwarded chain, first outside mail: Nov 1, 10:14 AM, Odile Marsh"


def test_a_name_nobody_has_leaves_the_requester_empty_with_new_party(world):
    conn, path, ids = world
    header = HEADERS["outlook-en"].replace("Rosa Teller <{a}>", "Pim Okafor")
    draft = intake.build(conn, forward(header))
    assert "requester_party_id" not in draft.values and draft.new_party is not None
    assert draft.values["received_on"] == "2026-11-01"


def test_a_public_mailbox_sender_is_found_by_its_contact_address(world):
    conn, path, ids = world
    header = HEADERS["outlook-en"].replace("Rosa Teller <{a}>", "Odile Marsh <someone@gmail.com>")
    draft = intake.build(conn, forward(header))
    assert draft.values["requester_party_id"] == ids["brightwater"]


def test_an_unreadable_header_keeps_today_s_prefill_under_an_amber_line(world):
    conn, path, ids = world
    mail = forward("", top="Lena here - the agent called, can you register? MIA to SAI by air.\n")
    draft = intake.build(conn, mail)
    assert draft.forward_line == intake.FORWARD_CHECK and draft.forward_level == "check"
    assert draft.values["received_on"] == "2026-11-04"
    assert "requester_party_id" not in draft.values
    assert draft.forward_note.startswith("Forwarded by Lena Park")
    # a block with no date cannot be read either
    no_date = HEADERS["outlook-en"].replace("Sent: Sunday, November 1, 2026 10:14 AM\n", "")
    assert intake.build(conn, forward(no_date)).forward_level == "check"


def test_a_reply_from_a_colleague_is_not_a_forward(world):
    conn, path, ids = world
    draft = intake.build(conn, forward(HEADERS["outlook-en"], subject="RE: RATE REQUEST // MIA - SAI"))
    assert not draft.forward_note and not draft.forward_line


def test_a_forward_from_another_office_keeps_the_office_with_a_muted_line(world):
    conn, path, ids = world
    mail = forward(HEADERS["outlook-en"], sender="noor@jojofreight.example", name="JJK-NOOR HADI")
    draft = intake.build(conn, mail)
    assert draft.values["requester_party_id"] == ids["kul"]
    assert draft.forward_level == "muted"
    assert draft.forward_line == "Forwarded mail from Rosa Teller <{}>, Nov 1, 10:14 AM".format(AGENT)
    assert not draft.forward_note


# ------------------------------------- a sender named without an address --
# the inquiry itself, from Rosa by name only (Outlook dropped the address)
NAME_ONLY = HEADERS["outlook-en"].replace("Rosa Teller <{a}>", "Rosa Teller") + ASK
BORROWED = ("from the forwarded chain, first outside mail: Nov 1, 10:14 AM, Rosa Teller, "
            "address from elsewhere in the chain")


def test_a_name_without_an_address_borrows_it_from_another_mail_s_from_line(world):
    conn, path, ids = world
    draft = intake.build(conn, forward(CHASER + NAME_ONLY))
    assert draft.values["requester_party_id"] == ids["quill"]
    assert draft.values["received_on"] == "2026-11-01"
    assert draft.sources["requester_party_id"] == BORROWED


def test_a_name_without_an_address_borrows_it_from_a_to_or_cc_line(world):
    conn, path, ids = world
    # our reply above it is addressed To Rosa
    draft = intake.build(conn, forward(OUR_REPLY + NAME_ONLY))
    assert draft.values["requester_party_id"] == ids["quill"] and draft.sources["requester_party_id"] == BORROWED
    # or copies her
    copied = OUR_REPLY.replace("To: Rosa Teller <{a}>", "To: Pim Okafor <pim@okaforco.example>\nCc: Rosa Teller <{a}>")
    draft = intake.build(conn, forward(copied + NAME_ONLY))
    assert draft.values["requester_party_id"] == ids["quill"] and draft.sources["requester_party_id"] == BORROWED


def test_a_name_with_two_different_addresses_in_the_chain_borrows_neither(world):
    conn, path, ids = world
    other = OUR_REPLY.replace("To: Rosa Teller <{a}>", "To: Rosa Teller <rosa@tellerhome.example>")
    draft = intake.build(conn, forward(CHASER + other + NAME_ONLY))
    # today's path: the contact name (none on file), then New party
    assert "requester_party_id" not in draft.values and draft.new_party is not None
    assert draft.values["received_on"] == "2026-11-01"
    assert draft.sources["received_on"] == "from the forwarded chain, first outside mail: Nov 1, 10:14 AM, Rosa Teller"


def test_names_match_without_accents_case_spaces_or_quotes(world):
    conn, path, ids = world
    accented = HEADERS["outlook-en"].replace("Rosa Teller <{a}>", "Inés  Vidal") + ASK
    reply = OUR_REPLY.replace("To: Rosa Teller <{a}>", 'To: "INES VIDAL" <ines@quillagents.example>')
    draft = intake.build(conn, forward(reply + accented))
    assert draft.values["requester_party_id"] == ids["quill"]
    assert draft.sources["requester_party_id"].endswith("Inés Vidal, address from elsewhere in the chain")
    # the contact-name lookup compares the same way
    repo = db.Repository(conn, source="test")
    repo.insert("contact", {"party_id": ids["brightwater"], "name": "Joël Brunet"})
    repo.commit()
    alone = HEADERS["outlook-en"].replace("Rosa Teller <{a}>", "'JOEL  BRUNET'")
    assert intake.build(conn, forward(alone)).values["requester_party_id"] == ids["brightwater"]


# ------------------------------------------------------------ 2. the page --
@pytest.fixture
def web(world, monkeypatch):
    conn, path, ids = world
    app = create_app(path)
    app.config["TESTING"] = True
    holder = {"mail": None}
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: holder["mail"])
    return app.test_client(), path, holder, ids


def _fields(html):
    from test_register_v3 import fields_of
    return fields_of(html)


def _register(client, holder, mail, **changes):
    holder["mail"] = mail
    page = client.post("/enquiries/from-outlook")
    assert page.status_code == 200
    body = page.data.decode("utf-8")
    fields = _fields(body)
    for name, value in changes.items():
        fields[name] = [value]
    return body, fields


def test_saving_a_colleague_s_forward_keeps_the_chain_s_values_and_no_note(web):
    client, path, holder, ids = web
    body, fields = _register(client, holder, forward(HEADERS["outlook-en"]), dest_country="Chile", origin_country="USA")
    assert NOTE in body
    assert fields["forward_note"] == ["Forwarded by Lena Park, Nov 4, 9:40 AM"]
    saved = client.post("/enquiries/new", data=fields)
    assert saved.status_code == 302, saved.data.decode("utf-8")[:3000]
    conn = db.open_db(path)
    try:
        case = conn.execute("SELECT id, requester_party_id, received_on, handled_by FROM enquiry").fetchone()
        assert (case["requester_party_id"], case["received_on"], case["handled_by"]) == (ids["quill"], "2026-11-01", "JJK")
        assert conn.execute("SELECT COUNT(*) FROM case_note").fetchone()[0] == 0     # CX1
    finally:
        conn.close()


def test_the_page_shows_the_amber_line_and_the_muted_line(web):
    client, path, holder, ids = web
    body, _ = _register(client, holder, forward("", top="Lena here - please register.\n"))
    assert 'class="fwd-line vendor-note"' in body and intake.FORWARD_CHECK in body
    body, fields = _register(client, holder, forward(HEADERS["outlook-en"], sender="noor@jojofreight.example",
                                                     name="JJK-NOOR HADI"))
    assert 'class="fwd-line muted small"' in body and "Forwarded mail from Rosa Teller" in body
    assert "forward_note" not in fields


def test_a_forward_of_a_registered_mail_shows_the_case_and_the_duplicate_panel(web):
    client, path, holder, ids = web
    direct = outlook_bridge.MailInfo(
        subject="RATE REQUEST // MIA - SAI", received_on="2026-11-01", received_at=WHEN,
        sender_address=AGENT, sender_name="Rosa Teller", to_addresses=["desk@jojofreight.example"],
        body_excerpt=ASK, entry_id="ENTRY-3200", conversation_id="CONV-3200")
    _, fields = _register(client, holder, direct, dest_country="Chile", origin_country="USA")
    assert client.post("/enquiries/new", data=fields).status_code == 302
    body, fields = _register(client, holder, forward(HEADERS["outlook-en"]), dest_country="Chile",
                             origin_country="USA")
    assert 'id="already"' in body and "OPP-3201" in body               # DD1: Already in OppList?
    page = client.post("/enquiries/new", data=fields)
    assert page.status_code == 200 and "OPP-3201" in page.data.decode("utf-8")
    conn = db.open_db(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM enquiry").fetchone()[0] == 1
    finally:
        conn.close()
