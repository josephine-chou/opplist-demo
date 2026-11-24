"""The Outlook bridge and the draft it feeds.

The bridge itself is not driven against a real Outlook here - that
keeps that for `python -m opplist outlook-selftest`. What is tested is
everything around the COM calls: that a failure becomes a sentence rather than
a stack trace, and that a mail turns into the right pre-filled form.
"""

from __future__ import annotations

import pytest

from conftest import add_party, seed_reference
from opplist import intake, matching, outlook_bridge


class FakeAddressEntry:
    def __init__(self, address, kind="SMTP", smtp=None):
        self.Address = address
        self.Type = kind
        self._smtp = smtp

    def GetExchangeUser(self):
        if self._smtp is None:
            raise RuntimeError("no exchange user")
        return type("U", (), {"PrimarySmtpAddress": self._smtp})()


class FakeRecipient:
    def __init__(self, entry, kind=1):
        self.AddressEntry = entry
        self.Type = kind
        self.Address = entry.Address


class FakeRecipients:
    def __init__(self, items):
        self._items = items
        self.Count = len(items)

    def Item(self, index):
        return self._items[index - 1]


class FakeMail:
    Class = outlook_bridge.OL_MAIL_ITEM

    def __init__(self, subject, sender_type="SMTP", sender_address="a@b.example",
                 sender_smtp=None, recipients=(), received=(2026, 9, 4, 9, 30)):
        self.Subject = subject
        self.SenderName = "Someone"
        self.SenderEmailType = sender_type
        self.SenderEmailAddress = sender_address
        self.Sender = FakeAddressEntry(sender_address, sender_type, sender_smtp)
        self.Recipients = FakeRecipients(list(recipients))
        self.EntryID = "ENTRY-1"
        self.ConversationID = "CONV-1"
        self.ConversationTopic = subject
        self.Body = "body text " * 7000        # 70000 characters, past the limit
        self.ReceivedTime = type("T", (), {
            "year": received[0], "month": received[1], "day": received[2],
            "hour": received[3], "minute": received[4]})()


def read(mail, count=1):
    return outlook_bridge._read_item(mail, selection_count=count)


# ----------------------------------------------------------- reading -----
def test_an_external_sender_is_read_straight_off():
    info = read(FakeMail("RATE REQUEST // MIA - LIM",
                         sender_address="ana@tolvex.example"))
    assert info.sender_address == "ana@tolvex.example"
    assert info.sender_internal is False
    assert info.received_on == "2026-09-04"
    assert info.received_at == "2026-09-04 09:30"
    assert info.entry_id == "ENTRY-1" and info.conversation_id == "CONV-1"


def test_an_exchange_sender_is_resolved_to_its_smtp_address():
    info = read(FakeMail("x", sender_type="EX", sender_address="/O=EX/CN=VICTOR",
                         sender_smtp="VictorLang@jojofreight.example"))
    assert info.sender_address == "VictorLang@jojofreight.example"
    assert info.sender_internal is True


def test_an_exchange_sender_with_no_smtp_is_still_marked_internal():
    info = read(FakeMail("x", sender_type="EX", sender_address="/O=EX/CN=X"))
    assert info.sender_address == ""
    assert info.sender_internal is True
    assert any("To and CC" in note for note in info.notes)


def test_recipients_are_split_into_to_and_cc():
    info = read(FakeMail("x", recipients=[
        FakeRecipient(FakeAddressEntry("ops@partner-agent.example"), kind=1),
        FakeRecipient(FakeAddressEntry("/O=EX/CN=AVA", "EX", "AvaT@jojofreight.example"),
                      kind=2),
        FakeRecipient(FakeAddressEntry("not-an-address"), kind=1),
    ]))
    assert info.to_addresses == ["ops@partner-agent.example"]
    assert info.cc_addresses == ["AvaT@jojofreight.example"]
    assert info.recipients == ["ops@partner-agent.example", "AvaT@jojofreight.example"]


def test_the_body_is_cut_at_sixty_thousand_characters():
    assert outlook_bridge.BODY_LIMIT == 60000
    assert len(read(FakeMail("x")).body_excerpt) == outlook_bridge.BODY_LIMIT


def test_a_missing_received_time_is_reported_not_guessed():
    mail = FakeMail("x")
    mail.ReceivedTime = None
    info = read(mail)
    assert info.received_on == ""
    assert any("received date" in note for note in info.notes)


def test_a_recipient_that_throws_is_skipped_not_fatal():
    class Angry:
        @property
        def AddressEntry(self):
            raise RuntimeError("COM said no")

    info = read(FakeMail("x", recipients=[
        Angry(), FakeRecipient(FakeAddressEntry("ok@partner-agent.example"))]))
    assert info.to_addresses == ["ok@partner-agent.example"]


# ------------------------------------------------------------ failures ---
def test_every_failure_is_a_sentence(monkeypatch):
    """The server must not fall over because Outlook is not there."""
    monkeypatch.setattr(outlook_bridge, "available", lambda: True)

    class NoCom:
        def CoInitialize(self):
            pass

        def CoUninitialize(self):
            pass

    import sys
    monkeypatch.setitem(sys.modules, "pythoncom", NoCom())
    monkeypatch.setattr(outlook_bridge, "_application",
                        lambda: (_ for _ in ()).throw(
                            outlook_bridge.OutlookError(outlook_bridge.NOT_RUNNING)))
    with pytest.raises(outlook_bridge.OutlookError) as caught:
        outlook_bridge.read_selection()
    assert caught.value.message == "Open Outlook and select a mail first."


def test_open_item_without_an_entry_id_says_so():
    with pytest.raises(outlook_bridge.OutlookError) as caught:
        outlook_bridge.open_item("")
    assert "moved or deleted" in caught.value.message


def test_missing_pywin32_is_a_message_not_an_import_error(monkeypatch):
    monkeypatch.setattr(outlook_bridge, "available", lambda: False)
    with pytest.raises(outlook_bridge.OutlookError) as caught:
        outlook_bridge.read_selection()
    assert "pywin32" in caught.value.message


# -------------------------------------------------------------- draft ----
@pytest.fixture
def conn_with_parties(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    klx = add_party(repo, "KLX", country="USA", full_name="KLX Logistics LLC")
    add_party(repo, "JoJo JJC", ptype="Office", country="USA")
    tbc = add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    add_party(repo, "Unknown", ptype="Unknown", country=None, placeholder=1)
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    repo.insert("party_domain", {"domain": "shipklx.example", "party_id": klx})
    repo.commit()
    return repo.conn, {"tolvex": tolvex, "klx": klx, "tbc": tbc}


def test_a_plain_agent_mail_fills_the_form(conn_with_parties):
    conn, ids = conn_with_parties
    mail = read(FakeMail("RE: RATE REQUEST // MIA - LIM // FCL // DAP",
                         sender_address="ana@tolvex.example"))
    draft = intake.build(conn, mail)
    assert draft.values["requester_party_id"] == ids["tolvex"]
    assert draft.sources["requester_party_id"] == intake.FROM_SENDER
    assert draft.values["received_on"] == "2026-09-04"
    assert draft.values["service_mode"] == "FCL"
    assert draft.values["incoterm"] == "DAP"
    assert draft.values["origin_loc"] == "MIA" and draft.values["dest_loc"] == "LIM"
    assert draft.values["thread_key"] == "RATEREQUESTMIALIMFCLDAP"
    assert draft.values["entry_id"] == "ENTRY-1"
    assert draft.lane_count == 1


def test_a_mail_we_sent_out_is_registered_against_the_placeholder(conn_with_parties):
    """Internal sender, outside recipient - we asked, nobody asked us."""
    conn, ids = conn_with_parties
    mail = read(FakeMail(
        "FW: RATE REQUEST // SEA - BUD", sender_type="EX",
        sender_address="/O=EX/CN=VICTOR", sender_smtp="VictorLang@jojofreight.example",
        recipients=[FakeRecipient(FakeAddressEntry("gnovak@shipklx.example"))]))
    draft = intake.build(conn, mail)
    assert draft.outbound is True
    assert draft.values["requester_party_id"] == ids["tbc"]
    assert draft.sources["requester_party_id"] == intake.FROM_OUTBOUND
    assert draft.values["status"] == "New"
    # KLX is who we asked, offered for logging as a vendor request.
    assert [(v["key"], v["checked"], v["known"]) for v in draft.vendors] == [
        ("KLX", True, True)]


def test_the_c_slash_name_is_used_when_no_domain_matches(conn_with_parties):
    conn, ids = conn_with_parties
    mail = read(FakeMail(
        "RATE REQUEST // C/KLX // SEA - BUD // DDP", sender_type="EX",
        sender_address="/O=EX/CN=VICTOR", sender_smtp="VictorLang@jojofreight.example",
        recipients=[]))
    draft = intake.build(conn, mail)
    assert draft.values["end_customer"] == "KLX"
    assert draft.values["requester_party_id"] == ids["klx"]
    assert draft.sources["requester_party_id"] == intake.FROM_SUBJECT


def test_nothing_matching_leaves_the_party_blank_and_says_so(conn_with_parties):
    conn, _ = conn_with_parties
    mail = read(FakeMail("RATE REQUEST // MIA - LIM",
                         sender_address="ops@brand-new.example"))
    draft = intake.build(conn, mail)
    assert "requester_party_id" not in draft.values
    assert any("'New party' is filled in from it" in note for note in draft.notes)
    assert draft.new_party["key"] == "Brand New" and draft.new_party["domain"] == "brand-new.example"
    assert draft.learn_domain == "brand-new.example"


def test_two_origins_become_two_lanes(conn_with_parties):
    conn, _ = conn_with_parties
    mail = read(FakeMail("RATE REQUEST // HALDEN TOOLS // FOB // NGB / XMN - LAX",
                         sender_address="ana@tolvex.example"))
    draft = intake.build(conn, mail)
    assert draft.lane_count == 2
    assert [(l["origin"], l["dest"]) for l in draft.lanes] == [
        ("NGB", "LAX"), ("XMN", "LAX")]
    assert draft.lanes[0]["origin_known"] is True    # NGB and LAX are both known
    assert draft.lanes[1]["origin_known"] is False   # XMN is not a location yet
    assert draft.unknown_locations == ["XMN"]
    assert any("2 lanes detected" in note for note in draft.notes)


def test_a_subject_in_no_particular_format_still_gives_the_facts(conn_with_parties):
    conn, ids = conn_with_parties
    mail = read(FakeMail("Air Freight Quote Request",
                         sender_address="ana@tolvex.example"))
    draft = intake.build(conn, mail)
    assert draft.values["received_on"] == "2026-09-04"
    assert draft.values["requester_party_id"] == ids["tolvex"]
    assert draft.values["service_mode"] == "Air"
    assert draft.lane_count == 0
    assert "origin_loc" not in draft.values


def test_describe_prints_without_touching_the_database(conn_with_parties):
    conn, _ = conn_with_parties
    mail = read(FakeMail("RATE REQUEST // MIA - LIM", sender_address="ana@tolvex.example"))
    before = conn.execute("SELECT COUNT(*) AS n FROM change_log").fetchone()["n"]
    text = "\n".join(intake.describe(intake.build(conn, mail)))
    assert "Nothing was written" in text
    assert "thread key" in text and "MIA - LIM" in text
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM change_log").fetchone()["n"] == before


# ------------------------------------------------- the whole selection ---
class FakeSelection:
    def __init__(self, items):
        self._items = items
        self.Count = len(items)

    def Item(self, index):
        return self._items[index - 1]


def fake_outlook(monkeypatch, items):
    """Point the bridge at a selection without a real Outlook."""
    monkeypatch.setattr(outlook_bridge, "available", lambda: True)

    class NoCom:
        def CoInitialize(self):
            pass

        def CoUninitialize(self):
            pass

    import sys
    monkeypatch.setitem(sys.modules, "pythoncom", NoCom())
    selection = FakeSelection(items)
    explorer = type("E", (), {"Selection": selection})()
    app = type("A", (), {"ActiveExplorer": lambda self: explorer})()
    monkeypatch.setattr(outlook_bridge, "_application", lambda: app)


def test_read_selection_all_returns_every_mail(monkeypatch):
    fake_outlook(monkeypatch, [FakeMail("one"), FakeMail("two"), FakeMail("three")])
    mails = outlook_bridge.read_selection_all()
    assert [m.subject for m in mails] == ["one", "two", "three"]
    assert {m.selection_count for m in mails} == {3}


def test_read_selection_all_skips_items_that_are_not_mails(monkeypatch):
    appointment = type("Appt", (), {"Class": 26})()
    fake_outlook(monkeypatch, [appointment, FakeMail("the enquiry")])
    mails = outlook_bridge.read_selection_all()
    assert [m.subject for m in mails] == ["the enquiry"]
    assert mails[0].selection_count == 2      # so the caller can say '1 of 2'


def test_read_selection_all_with_no_mail_at_all_is_a_sentence(monkeypatch):
    fake_outlook(monkeypatch, [type("Appt", (), {"Class": 26})()])
    with pytest.raises(outlook_bridge.OutlookError) as caught:
        outlook_bridge.read_selection_all()
    assert caught.value.message == outlook_bridge.NOT_A_MAIL


def test_read_selection_still_takes_the_first_and_says_so(monkeypatch):
    fake_outlook(monkeypatch, [FakeMail("one"), FakeMail("two")])
    info = outlook_bridge.read_selection()
    assert info.subject == "one"
    assert any("first one was used" in note for note in info.notes)


def test_summarise_tallies_what_was_pre_filled(conn_with_parties):
    conn, _ = conn_with_parties
    mails = [read(FakeMail("RATE REQUEST // MIA - LIM // FCL",
                           sender_address="ana@tolvex.example"), count=3),
             read(FakeMail("RE: quote please", sender_address="who@nowhere.example"),
                  count=3),
             read(FakeMail("RATE REQUEST // NGB / XMN - LAX // Air",
                           sender_address="ana@tolvex.example"), count=3)]
    text = "\n".join(intake.summarise([intake.build(conn, m) for m in mails],
                                      selection_count=3))
    assert "received date  3/3" in text
    assert "requester      2/3" in text        # the unknown domain matched nothing
    assert "lane           2/3" in text
    assert "Tolvex (sender)" in text
    assert "NGB - LAX +1" in text              # the second lane is counted, not lost
    assert "new locations  LIM, XMN" in text   # both offered as "Add location..."
    assert "not what was correct" in text


def test_summarise_reports_the_items_that_were_not_mails(conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, read(FakeMail("RATE REQUEST // MIA - LIM"), count=4))
    text = "\n".join(intake.summarise([draft], selection_count=4))
    assert "3 of 4 selected items were not mails" in text


# ------------------------------------------- the lane stated in the body --
def body_mail(subject, body, **kwargs):
    mail = FakeMail(subject, **kwargs)
    mail.Body = body
    return read(mail)


def test_the_body_fills_the_lane_when_the_subject_has_none(conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, body_mail(
        "Air quote needed - lab centrifuges",
        "Dear Victor,\n\nWe would like a price for an air move "
        "from Denver, CO, USA, to Curitiba, Brazil.\n",
        sender_address="ana@tolvex.example"))
    assert draft.values["origin_loc"] == "DENVER"
    assert draft.values["dest_loc"] == "CURITIBA"
    assert draft.sources["origin_loc"] == intake.FROM_BODY
    # The quoted fragment is what the parser actually used, no more; the
    # country after the comma belongs to the place (2026-09-10).
    assert draft.evidence["origin_loc"] == "from Denver, CO, USA, to Curitiba, Brazil"


def test_the_subject_still_wins_over_the_body(conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, body_mail(
        "RATE REQUEST // MIA - LIM",
        "POL: Houston\nPOD: Recife\n", sender_address="ana@tolvex.example"))
    assert draft.values["origin_loc"] == "MIA"
    assert draft.values["dest_loc"] == "LIM"
    assert draft.sources["origin_loc"] == intake.FROM_SUBJECT
    assert "origin_loc" not in draft.evidence


def test_a_body_that_disagrees_with_itself_fills_nothing(conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, body_mail(
        "QUOTE REQUEST // OCEAN IMPORT FROM USA // MERIDA",
        "Hi,\n\nWe need two alternatives priced on EXW terms:\n\n"
        "Option A\nPOD: Itajai/Navegantes\n\nOption B:\nPOD: Santos\n",
        sender_address="ana@tolvex.example"))
    assert "dest_loc" not in draft.values
    assert [p for p, _ in draft.body_conflicts["dest"]] == [
        "Itajai", "Navegantes", "Santos"]
    assert any("none pre-filled" in note for note in draft.notes)


def test_only_a_destination_still_fills_the_destination(conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, body_mail(
        "Quotation request", "POD: Mombasa, Kenya\n",
        sender_address="ana@tolvex.example"))
    assert draft.values["dest_loc"] == "MOMBASA"
    assert "origin_loc" not in draft.values
    assert draft.evidence["dest_loc"] == "POD: Mombasa, Kenya"


def test_a_quoted_older_mail_does_not_lend_its_lane(conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, body_mail(
        "RE: booking confirmation",
        "Hi Victor,\n\nSounds good, the handling instructions are attached.\n\n"
        "De: JJC-VICTOR LANG <VictorLang@jojofreight.example>\n"
        "Assunto: RE: RATE REQUEST // MIA - LIM // DAP\n",
        sender_address="ana@tolvex.example"))
    assert "origin_loc" not in draft.values and "dest_loc" not in draft.values


def test_a_body_lane_that_is_new_offers_add_location(conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, body_mail(
        "Rates please", "Please advise your rates from Pipavav to Norfolk\n",
        sender_address="ana@tolvex.example"))
    assert draft.values["origin_loc"] == "PIPAVAV"
    assert "PIPAVAV" in draft.unknown_locations
    assert any("Add location" in note for note in draft.notes)


def test_describe_quotes_the_body_line_it_used(conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, body_mail(
        "Quotation request", "POL: Atlanta, U.S\nPOD: Mombasa, Kenya\n",
        sender_address="ana@tolvex.example"))
    text = "\n".join(intake.describe(draft))
    assert "from body" in text and "POL: Atlanta, U.S" in text


def test_the_summary_says_which_lanes_came_from_the_body(conn_with_parties):
    conn, _ = conn_with_parties
    mails = [body_mail("RATE REQUEST // MIA - LIM", "Hi\n",
                       sender_address="ana@tolvex.example"),
             body_mail("Quotation request",
                       "POL: Atlanta\nPOD: Mombasa, Kenya\n",
                       sender_address="ana@tolvex.example"),
             body_mail("QUOTE REQUEST // MERIDA",
                       "POD: Itajai\n\nPOD: Santos\n",
                       sender_address="ana@tolvex.example")]
    text = "\n".join(intake.summarise([intake.build(conn, m) for m in mails]))
    assert "MIA - LIM" in text and "(body)" not in text.split("\n")[2]
    assert "ATLANTA - MOMBASA (body)" in text
    assert "? 2 dests in body" in text
    assert "lane           2/3   (1 read from the body)" in text
    assert "1 mail(s) name several places in the body" in text


# ---------------------------------------------- mails we sent out (4.3) ---
def out_mail(subject, recipients, body="Hi,\n\nPlease advise your rates.\n"):
    """Victor writing out to somebody, as Outlook hands it over."""
    mail = FakeMail(subject, sender_type="EX", sender_address="/O=EX/CN=VICTOR",
                    sender_smtp="VictorLang@jojofreight.example",
                    recipients=[FakeRecipient(FakeAddressEntry(a))
                                for a in recipients])
    mail.Body = body
    return read(mail)


def test_several_addresses_at_one_vendor_are_one_vendor(conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, out_mail(
        "RATE REQUEST // DOOR - AIRPORT // IAH - BLR // DG CLASS 9",
        ["rmarino@seavio.example", "lperalta@seavio.example", "pwojcik@seavio.example"]))
    assert len(draft.vendors) == 1
    vendor = draft.vendors[0]
    assert vendor["domain"] == "seavio.example" and len(vendor["addresses"]) == 3
    assert vendor["known"] is False and vendor["key"] == "Seavio"
    assert vendor["checked"] is True


def test_our_own_people_on_cc_are_not_vendors(conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, out_mail(
        "RATE REQUEST // DALLAS - MOMBASA",
        ["khale@bluefin-usa.example", "AvaT@jojofreight.example", "IrisChen@jojofreight.example"]))
    assert [v["domain"] for v in draft.vendors] == ["bluefin-usa.example"]
    assert draft.vendors[0]["key"] == "Bluefin Usa"


def test_a_mail_only_to_our_own_people_is_not_a_vendor_ask(conn_with_parties):
    conn, ids = conn_with_parties
    draft = intake.build(conn, out_mail(
        "RE: air cross-trade move", ["KenjiSato@jojofreight.example"]))
    assert draft.outbound is False
    assert draft.vendors == []
    assert "status" not in draft.values
    assert draft.values.get("requester_party_id") != ids["tbc"]


def test_the_lane_and_the_end_customer_are_read_as_usual(conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, out_mail(
        "RATE REQUEST // C/KLX // MIA - LIM // FCL", ["ops@seavio.example"]))
    assert draft.values["origin_loc"] == "MIA" and draft.values["dest_loc"] == "LIM"
    assert draft.values["end_customer"] == "KLX"
    assert draft.values["service_mode"] == "FCL"


def test_victor_replying_to_tolvex_fills_tolvex(conn_with_parties):
    """RE: plus their own mail quoted below - they are asking us."""
    conn, ids = conn_with_parties
    draft = intake.build(conn, out_mail(
        "RE: QUOTE REQUEST // MERIDA", ["tomas.ibanez@tolvex.example"],
        body="Hi Tomas,\n\nAttached rates.\n\n"
             "De: Tomas Ibanez <tomas.ibanez@tolvex.example>\n"
             "Para: VictorLang@jojofreight.example\n"))
    assert draft.reply_to_requester is True and draft.outbound is False
    assert draft.values["requester_party_id"] == ids["tolvex"]
    assert draft.sources["requester_party_id"] == intake.FROM_REPLY
    assert draft.values["status"] == "Quoted"
    assert draft.vendors == []                     # nobody here is a vendor


def test_victor_writing_to_a_carrier_fills_the_placeholder(conn_with_parties):
    """The same shape without the reply: we are the ones asking."""
    conn, ids = conn_with_parties
    draft = intake.build(conn, out_mail(
        "RATE REQUEST // DALLAS - MOMBASA", ["khale@bluefin-usa.example"],
        body="Hi Kyle,\n\nPlease advise your rates.\n\n"
             "POL: Dallas\nPOD: Mombasa, Kenya\n"))
    assert draft.outbound is True and draft.reply_to_requester is False
    assert draft.values["requester_party_id"] == ids["tbc"]
    assert draft.values["status"] == "New"
    assert [v["domain"] for v in draft.vendors] == ["bluefin-usa.example"]
    assert draft.vendors[0]["checked"] is True


def test_a_reply_to_somebody_who_did_not_write_the_quote_is_a_vendor_ask(
        conn_with_parties):
    """RE: on its own proves nothing: the mail quoted below is our own."""
    conn, ids = conn_with_parties
    draft = intake.build(conn, out_mail(
        "RE: RATE REQUEST // MIA - LIM", ["ops@seavio.example"],
        body="Hi,\n\nAny update?\n\n"
             "From: JJC-VICTOR LANG <VictorLang@jojofreight.example>\n"))
    assert draft.outbound is True and draft.reply_to_requester is False
    assert draft.values["requester_party_id"] == ids["tbc"]
    assert [v["checked"] for v in draft.vendors] == [True]


def test_without_a_reply_prefix_the_quoted_author_is_only_unticked(
        conn_with_parties):
    """No RE:, so it reads as a fresh ask - but they wrote the mail below,
    and that mail asked us for a rate."""
    conn, ids = conn_with_parties
    draft = intake.build(conn, out_mail(
        "QUOTE REQUEST // MERIDA", ["tomas.ibanez@tolvex.example"],
        body="Hi Tomas,\n\nPlease advise your rates for the below.\n\n"
             "De: Tomas Ibanez <tomas.ibanez@tolvex.example>\n"
             "Asunto: MERIDA\n\nNecesitamos precio para 3 bultos.\n"))
    assert draft.outbound is True and draft.direction == "asking"
    assert draft.values["requester_party_id"] == ids["tbc"]
    assert draft.vendors[0]["checked"] is False
    assert "asking" in draft.vendors[0]["note"]
    quoted = [m for m in draft.party_candidates
              if m.how == matching.MATCH_QUOTED]
    assert [m.key for m in quoted] == ["Tolvex"]


def test_the_quoted_author_stays_ticked_when_their_mail_asked_nothing(
        conn_with_parties):
    """They wrote the mail below, but it was a hello, not an enquiry: we are
    the ones asking, so they are a vendor like anybody else we wrote to."""
    conn, ids = conn_with_parties
    draft = intake.build(conn, out_mail(
        "QUOTE REQUEST // MERIDA", ["tomas.ibanez@tolvex.example"],
        body="Hi Tomas,\n\nPlease advise your rates for the below.\n\n"
             "De: Tomas Ibanez <tomas.ibanez@tolvex.example>\n"
             "Asunto: Hola\n\nFue un placer verte en la feria.\n"))
    assert draft.outbound is True and draft.direction == "asking"
    assert draft.values["requester_party_id"] == ids["tbc"]
    assert draft.vendors[0]["checked"] is True
    assert draft.vendors[0]["note"] == ""


def test_rates_attached_to_the_quoted_author_is_our_quote(conn_with_parties):
    """Same mail, but our words give the rate: no panel, they are the
    requester (they wrote the mail below), Quoted on the day it went out."""
    conn, ids = conn_with_parties
    draft = intake.build(conn, out_mail(
        "QUOTE REQUEST // MERIDA", ["tomas.ibanez@tolvex.example"],
        body="Hi Tomas,\n\nRates attached.\n\n"
             "De: Tomas Ibanez <tomas.ibanez@tolvex.example>\n"))
    assert draft.direction == "quoting" and draft.vendors == []
    assert draft.values["requester_party_id"] == ids["tolvex"]
    assert draft.values["status"] == "Quoted"
    assert draft.values["status_at"] == "2026-09-04"


def test_a_reply_to_a_party_we_do_not_know_yet_still_has_no_vendors(
        conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, out_mail(
        "RE: RATE REQUEST // MIA - LIM", ["ops@brand-new.example"],
        body="Hi,\n\nRates attached.\n\nFrom: Ops <ops@brand-new.example>\n"))
    assert draft.reply_to_requester is True
    assert "requester_party_id" not in draft.values
    assert draft.values["status"] == "Quoted"
    assert draft.vendors == []
    assert draft.new_party["key"] == "Brand New"
    assert any("'New party' is filled in from it" in note for note in draft.notes)


def test_a_mail_that_came_to_us_is_untouched_by_any_of_this(conn_with_parties):
    conn, ids = conn_with_parties
    draft = intake.build(conn, body_mail(
        "RATE REQUEST // MIA - LIM", "Hi Victor,\n\nPlease quote.\n",
        sender_address="ana@tolvex.example"))
    assert draft.outbound is False and draft.vendors == []
    assert draft.values["requester_party_id"] == ids["tolvex"]
    assert draft.sources["requester_party_id"] == intake.FROM_SENDER
    assert "status" not in draft.values


def test_the_summary_names_the_placeholder_rather_than_its_row_id(
        conn_with_parties):
    """'#3' tells a reader nothing; '(TBC) (we asked)' tells them everything."""
    conn, _ = conn_with_parties
    drafts = [intake.build(conn, out_mail(
                  "RATE REQUEST // DALLAS - MOMBASA", ["khale@bluefin-usa.example"],
                  body="Hi Kyle,\n\nPlease advise your rates.\n")),
              intake.build(conn, out_mail(
                  "RE: QUOTE REQUEST // MERIDA", ["tomas@tolvex.example"],
                  body="Hi Tomas,\n\nRates attached.\n\n"
                       "De: Tomas Ibanez <tomas@tolvex.example>\n"))]
    text = "\n".join(intake.summarise(drafts))
    assert "(TBC) (we asked)" in text
    assert "Tolvex (our reply)" in text
    assert "#" not in text.split("--- pre-filled")[0].replace("  #  ", "")


# ------------------------------------ the quantity stated in the body -----
def test_the_body_fills_the_quantity_and_suggests_the_mode(conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, body_mail(
        "QUOTE REQUEST // MERIDA",
        "Hello Victor,\n\nPlease quote EXW.\nQTY: 2x40HC\nGeneral Cargo\n",
        sender_address="ana@tolvex.example"))
    assert draft.values["qty_raw"] == "FCL:2 x 40HQ"
    assert draft.values["service_mode"] == "FCL"
    assert draft.sources["service_mode"] == intake.FROM_BODY
    assert draft.evidence["qty_raw"] == "QTY: 2x40HC"


def test_the_subject_mode_is_never_argued_with(conn_with_parties):
    """Only suggest a mode when the subject named none."""
    conn, _ = conn_with_parties
    draft = intake.build(conn, body_mail(
        "RATE REQUEST // MIA - LIM // LCL",
        "Hi,\n\nGross Weight: 188.00 kg\n", sender_address="ana@tolvex.example"))
    assert draft.values["service_mode"] == "LCL"
    assert draft.sources["service_mode"] == intake.FROM_SUBJECT
    assert draft.values["qty_raw"] == "Air:188"      # still worth recording


def test_containers_in_the_subject_leave_the_body_alone(conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, body_mail(
        "RATE REQUEST // MIA - LIM // 1 X 20'",
        "Hi,\n\nPlus 4.2 cbm of spare parts\n",
        sender_address="ana@tolvex.example"))
    assert draft.values["container_type"] == "20GP"
    assert "qty_raw" not in draft.values


def test_crate_by_crate_weights_are_reported_not_added(conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, body_mail(
        "Air quote, two crates",
        "Dear Victor,\n\n1. Crate one\nGross Weight: 188.00 kg\n"
        "2. Crate two\nGross Weight: 305 kgs\n",
        sender_address="ana@tolvex.example"))
    assert "qty_raw" not in draft.values
    assert any("different quantities" in note for note in draft.notes)


def test_folders_for_puts_the_likely_folders_first():
    search = outlook_bridge.Search.__new__(outlook_bridge.Search)
    search._folders = [outlook_bridge.FolderInfo(p) for p in (
        "\\Personal Folder\\- ARCHIVE\\2019",
        "\\Personal Folder\\TOLVEX\\2025 JOBS",
        "\\IrisChen@jojofreight.example\\Inbox",
        "\\Personal Folder\\JOJO\\- JQI\\-1323",
        "\\Personal Folder\\QUOTES\\RECEIVED",
        "\\IrisChen@jojofreight.example\\Sent Items",
    )]
    ordered = [i.path for i in search.folders_for(["tolvex"], "OPP-1323")]
    assert ordered == [
        "\\Personal Folder\\JOJO\\- JQI\\-1323",
        "\\Personal Folder\\TOLVEX\\2025 JOBS",
        "\\IrisChen@jojofreight.example\\Inbox",
        "\\Personal Folder\\QUOTES\\RECEIVED",
        "\\IrisChen@jojofreight.example\\Sent Items",
        "\\Personal Folder\\- ARCHIVE\\2019",
    ]


class _Item:
    """A COM MailItem as far as _read_item looks."""
    Subject = "RATE REQUEST // MIA - LIM"
    ReceivedTime = None
    EntryID = "E1"
    ConversationID = "C1"
    ConversationTopic = "RATE REQUEST"
    SenderName = "Renata"
    SenderEmailType = "SMTP"
    SenderEmailAddress = "renata@tolvex.example"

    def __init__(self):
        self.body_reads = 0
        self.recipient_reads = 0

    @property
    def Body(self):
        self.body_reads += 1
        return "Please proceed."

    @property
    def Recipients(self):
        self.recipient_reads += 1
        raise RuntimeError("not in a light read")


def test_a_light_read_touches_neither_body_nor_recipients():
    item = _Item()
    info = outlook_bridge._read_item(item, light=True)
    assert (info.light, info.body_excerpt, info.to_addresses) == (True, "", [])
    assert item.body_reads == 0 and item.recipient_reads == 0
    assert info.sender_address == "renata@tolvex.example"
    full = outlook_bridge._read_item(_Item())
    assert full.light is False and full.body_excerpt == "Please proceed."
