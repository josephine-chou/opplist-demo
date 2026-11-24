"""Batch FX6 (2026-11-16, no schema change): our quote is a mail of ours
that went to the requester - a colleague's internal forward of a vendor's
cost is never it; asking a vendor to help with a quote is asking; Update
from Outlook's "Nothing new" names what stopped it; Read quote's trial picks
the reply that quoted.

Every company, person, place, amount and date here is invented; cases from
OPP-9960, domains .test.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party
from opplist import case_mail, db, outlook_bridge, propose, read_quote as rq, suggestions

INTERNAL = {"jojofreight.example"}
AGENT = {"quillmarsh.test"}


def mail(n, at, sender, body, to=(), cc=(), name="", internal=None, subject="RE: RATE REQUEST // ORD - LIM"):
    address = sender
    return outlook_bridge.MailInfo(
        subject=subject, received_on=at[:10], received_at=at, entry_id="E{}".format(n),
        message_id="<m{}@fx6.test>".format(n), sender_name=name or address.split("@")[0],
        sender_address=address, sender_internal=address.endswith("@jojofreight.example") if internal is None else internal,
        to_addresses=list(to), cc_addresses=list(cc), body_excerpt=body)


def classify(mails, requester_domains=AGENT, office="", key="Quillmarsh Cargo"):
    threads = [propose.to_thread_mail(m, INTERNAL, requester_domains, office) for m in mails]
    return propose.classify(threads, key, requester_known=bool(requester_domains or office))


ASK = mail(1, "2027-06-01 09:00", "rosa@quillmarsh.test", "Please quote 2 pallets ORD to LIM.",
           to=["desk@jojofreight.example"])
QUOTE = mail(2, "2027-06-02 11:45", "dorian@jojofreight.example", "Hello Rosa,\nPlease find below rate quote.\nAF USD 3.40/kg",
             to=["rosa@quillmarsh.test"], cc=["desk@jojofreight.example"], name="JJC-DORIAN VALE")
FORWARD = mail(3, "2027-06-02 11:47", "dorian@jojofreight.example", "Team,\nBrightwell cost attached.\nThanks",
               to=["mira@jojofreight.example", "otto@jojofreight.example"], name="JJC-DORIAN VALE", subject="FW: ORD - LIM")


# ------------------------------------------------------------- point 1 --
def test_an_internal_forward_after_our_quote_is_not_the_quote():
    p = classify([ASK, QUOTE, FORWARD])
    assert p.proposed_status == "Quoted" and p.proposed_date == "2027-06-02"
    assert p.evidence_quote == "Please find below rate quote." and p.message_id == "<m2@fx6.test>"


def test_only_an_internal_forward_is_no_quote():
    p = classify([ASK, FORWARD])
    assert p.proposed_status != "Quoted"


def test_a_requester_with_no_domain_on_file():
    # internal only: never our quote
    assert classify([FORWARD], requester_domains=set()).proposed_status != "Quoted"
    # to an outside address: our quote
    out = mail(4, "2027-06-02 12:00", "dorian@jojofreight.example", "Please find below rate quote.",
               to=["someone@harbourlane.test"], name="JJC-DORIAN VALE")
    p = classify([FORWARD, out], requester_domains=set())
    assert p.proposed_status == "Quoted" and p.message_id == "<m4@fx6.test>"


def test_an_office_requester_still_counts():
    office_quote = mail(5, "2027-06-03 10:00", "dorian@jojofreight.example", "Please see below rates for your reference.",
                        to=["jjt-desk@jojofreight.example"], name="JJC-DORIAN VALE")
    office_quote.display_to = "JJT-ARLO PIKE; JJC-DESK"
    p = classify([office_quote], requester_domains=set(), office="JJT-", key="JoJo JJT")
    assert p.proposed_status == "Quoted" and p.evidence_quote == "Please see below rates for your reference."


def test_a_follow_up_sent_to_a_colleague_is_not_read():
    chase = mail(6, "2027-06-05 09:00", "dorian@jojofreight.example", "Any feedback on our rates provided last week?",
                 to=["mira@jojofreight.example"], name="JJC-DORIAN VALE")
    assert classify([ASK, chase]).proposed_status != "Quoted"
    chase_out = mail(7, "2027-06-05 09:05", "dorian@jojofreight.example", "Any feedback on our rates provided last week?",
                     to=["rosa@quillmarsh.test"], name="JJC-DORIAN VALE")
    p = classify([ASK, chase_out])
    assert p.proposed_status == "Quoted" and "follow-up" in p.note


# FX6 replaces test_propose's two Tolvex tests with these
def test_our_quote_to_the_requester_with_no_answer_is_quoted():
    quote = mail(11, "2027-06-03 10:00", "dorian@jojofreight.example", "Please see below rates for your reference.",
                 to=["rosa@quillmarsh.test"], name="JJC-DORIAN VALE")
    p = classify([ASK, quote])
    assert (p.proposed_status, p.proposed_date) == ("Quoted", "2027-06-03")
    assert "rates for your reference" in p.evidence_quote and p.note == "we quoted and nothing came back"


def test_our_own_mail_to_a_vendor_never_asks_us_to_requote():
    quote = mail(12, "2027-06-03 10:00", "dorian@jojofreight.example", "Please see below rates for your reference.",
                 to=["rosa@quillmarsh.test"], name="JJC-DORIAN VALE")
    requote = mail(13, "2027-06-06 10:00", "dorian@jojofreight.example", "Brightwell, please requote with 3 x 40HQ.",
                   to=["kai@brightwell.test"], name="JJC-DORIAN VALE")
    p = classify([ASK, quote, requote])
    assert p.proposed_status == "Quoted" and p.note == "we quoted and nothing came back"


# ------------------------------------------------------------- point 2 --
@pytest.mark.parametrize("words", ["Would you please assist for this quote below?",
                                   "Can you assist us with the quote below?",
                                   "Could you help me with this quote below?"])
def test_asking_a_vendor_to_help_with_a_quote_is_asking(words):
    assert propose.quote_words(words) == ""
    ask_vendor = mail(8, "2027-06-02 10:00", "dorian@jojofreight.example", words, to=["ops@brightwell.test"],
                      name="JJC-DORIAN VALE")
    assert classify([ASK, ask_vendor], requester_domains=set()).proposed_status != "Quoted"


# ------------------------------------------------------------- point 3 --
@pytest.fixture
def world(tmp_path):
    conn = db.open_db(tmp_path / "fx6.db")
    repo = db.Repository(conn, source="test")
    repo.insert("country", {"name": "USA", "region": "North America", "iso2": "US"})
    agent = add_party(repo, "Quillmarsh Cargo")
    repo.insert("party_domain", {"domain": "quillmarsh.test", "party_id": agent})
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    case_id = add_enquiry(repo, "OPP-9960", agent, received="2027-06-01", scope="In")
    repo.commit()
    yield {"conn": conn, "repo": repo, "case": conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case_id,)).fetchone()}
    conn.close()


class Thread:
    def __init__(self, mails):
        self.mails = mails

    def conversation_of(self, mail):
        return self.mails


def test_nothing_new_names_the_dismissed_suggestion(world):
    w = world
    w["repo"].insert("suggestion", {
        "enquiry_id": w["case"]["id"], "opp_no": "OPP-9960", "kind": "status", "proposed_status": "Quoted",
        "proposed_date": "2027-06-02", "evidence_date": "2027-06-02", "state": "dismissed",
        "decided_at": "2027-06-04 08:30:00", "entry_id": "E2",
        "evidence_quote": "Please find below rate quote, valid until the end of the month, all-in charges as agreed",
        "evidence_sender": "JJC-DORIAN VALE <dorian@jojofreight.example>", "source": "scan"})
    w["repo"].commit()
    blocked = []
    made = suggestions.from_mail(w["repo"], w["case"], QUOTE, Thread([QUOTE]), blocked=blocked)
    assert made == []
    assert suggestions.nothing_new(blocked) == (
        "Nothing new: Quoted on 2 Jun from that thread was dismissed on 4 Jun ('Please find below rate quote, valid "
        "until the end of the...'). If that reading was wrong, record it on the case page.")
    assert suggestions.nothing_new([]) == "Nothing new to suggest from that mail (already suggested, or dismissed before)."


def test_a_pending_same_suggestion_is_named_too(world):
    w = world
    first = suggestions.from_mail(w["repo"], w["case"], QUOTE, Thread([QUOTE]))
    assert len(first) == 1
    blocked = []
    assert suggestions.from_mail(w["repo"], w["case"], QUOTE, Thread([QUOTE]), blocked=blocked) == []
    assert suggestions.nothing_new(blocked) == ("Nothing new: Quoted on 2 Jun from that thread is already waiting "
                                                "below ('Please find below rate quote.'). If that reading was wrong, "
                                                "record it on the case page.")


# ------------------------------------------------------------- point 7 --
def _mail(n, at, subject, body="", files=(), sender="kai@brightwell.test"):
    return outlook_bridge.MailInfo(subject=subject, received_at=at, received_on=at[:10], entry_id="T{}".format(n),
                                   message_id="<t{}@fx6.test>".format(n), sender_address=sender,
                                   sender_name="Kai Brennt", body_excerpt=body, attachment_names=list(files))


@pytest.fixture
def trial_world(tmp_path, monkeypatch):
    conn = db.open_db(tmp_path / "trial.db")
    repo = db.Repository(conn, source="test")
    repo.insert("country", {"name": "USA", "region": "North America", "iso2": "US"})
    agent = add_party(repo, "Quillmarsh Cargo")
    w = {"conn": conn, "repo": repo, "agent": agent, "vendors": {}, "cases": {}, "threads": {}}
    monkeypatch.setattr(case_mail, "vendor_mails", lambda search, vr, conn=None: w["threads"].get(vr["thread_key"], []))
    yield w
    conn.close()


def vendor(w, key):
    if key not in w["vendors"]:
        w["vendors"][key] = add_party(w["repo"], key, ptype="Carrier")
        w["repo"].insert("party_domain", {"domain": key.split()[0].lower() + ".test", "party_id": w["vendors"][key]})
    return w["vendors"][key]


def request(w, n, vendor_key, mails, asked="2027-06-02", replied=None, mode="Air"):
    if n not in w["cases"]:
        w["cases"][n] = add_enquiry(w["repo"], "OPP-{}".format(n), w["agent"], received="2027-06-01", scope="In",
                                    service_mode=mode)
    key = "RQ {} {}".format(n, vendor_key)
    w["repo"].insert("vendor_request", {"enquiry_id": w["cases"][n], "vendor_party_id": vendor(w, vendor_key),
                                        "asked_on": asked, "replied_on": replied, "machine_status": "Quoted",
                                        "thread_key": key})
    w["repo"].commit()
    w["threads"][key] = mails


def picks_of(w):
    return [(p["case"]["opp_no"], p["id"]) for p in rq.trial_picks(w["conn"], object(), limit=10, since="2027-06-01")]


def test_the_earliest_reply_within_asked_to_replied_plus_three(trial_world):
    w = trial_world
    b = "kai@brightwell.test"
    request(w, 9961, "Brightwell Lines", [_mail(1, "2027-06-01 09:00", "RE: RQ", "USD 3.30/kg", sender=b),   # before asked
                                          _mail(2, "2027-06-04 09:00", "RE: RQ", "AF USD 3.10/kg", sender=b),
                                          _mail(3, "2027-06-05 10:00", "RE: RQ", "Updated: USD 3.00/kg", sender=b),
                                          _mail(4, "2027-06-20 10:00", "RE: RQ", "Booking USD 3.00/kg", sender=b)],
            replied="2027-06-04")
    assert picks_of(w) == [("OPP-9961", "<t2@fx6.test>")]


def test_no_replied_day_looks_14_days_after_asked(trial_world):
    w = trial_world
    b = "kai@brightwell.test"
    request(w, 9962, "Brightwell Lines", [_mail(5, "2027-06-17 09:00", "RE: RQ", "USD 85 W/M", sender=b),
                                          _mail(6, "2027-06-10 09:00", "RE: RQ", "USD 90 W/M", sender=b)],
            mode="LCL")
    assert picks_of(w) == [("OPP-9962", "<t6@fx6.test>")]
    w["threads"]["RQ 9962 Brightwell Lines"] = [_mail(5, "2027-06-17 09:00", "RE: RQ", "USD 85 W/M", sender=b)]
    assert picks_of(w) == []


def test_shipping_documents_are_left_out(trial_world):
    w = trial_world
    b = "kai@brightwell.test"
    request(w, 9963, "Brightwell Lines", [_mail(7, "2027-06-03 08:00", "DRAFT : 7120/27 LCL", "USD 1,900",
                                                files=["HBL_X1.pdf"], sender=b),
                                          _mail(8, "2027-06-03 09:00", "RE: RQ FCL", "see attached",
                                                files=["Invoice 77.pdf"], sender=b),
                                          _mail(9, "2027-06-03 12:00", "RE: RQ FCL", "40HQ USD 1,900", sender=b)],
            replied="2027-06-03", mode="FCL")
    assert picks_of(w) == [("OPP-9963", "<t9@fx6.test>")]


def test_one_a_request_two_a_vendor_three_a_case(trial_world):
    w = trial_world
    n = 10
    for case in (9964, 9965, 9966):
        n += 1
        request(w, case, "Brightwell Lines", [_mail(n, "2027-06-0{} 09:00".format(case - 9961), "RE: RQ", "USD 3/kg",
                                                    sender="kai@brightwell.test"),
                                              _mail(n + 100, "2027-06-0{} 10:00".format(case - 9961), "RE: RQ",
                                                    "USD 2.9/kg", sender="kai@brightwell.test")],
                replied="2027-06-05")
    got = picks_of(w)
    # two a vendor; one a request, its earliest (never the 10:00 mails, t111 to t113)
    assert len(got) == 2 and not {i for _o, i in got} & {"<t111@fx6.test>", "<t112@fx6.test>", "<t113@fx6.test>"}
    for k, key in enumerate(("Pell Air", "Corran Air", "Tisdale Air", "Wrenfield Air")):
        request(w, 9967, key, [_mail(200 + k, "2027-06-04 0{}:00".format(k + 1), "RE: RQ", "USD 3/kg",
                                     sender="ops@{}.test".format(key.split()[0].lower()))], replied="2027-06-04")
    assert sum(1 for o, _i in picks_of(w) if o == "OPP-9967") == 3


def test_shipping_document_words_are_whole_words():
    assert rq.shipping_doc("DRAFT : 76/26 LCL", []) == "DRAFT"
    assert rq.shipping_doc("RE: rates", ["HBL_M26.pdf"]) == "HBL"
    assert rq.shipping_doc("Booking confirmation ORD-LIM", []) == "Booking confirmation"
    assert rq.shipping_doc("Pre-alert", []) == "Pre-alert"
    assert rq.shipping_doc("Arrival notice 4410", ["packing list.xlsx"]) == "Arrival notice"
    assert rq.shipping_doc("Drafting our offer", ["awbfee-rates.pdf", "invoiced.xlsx"]) == ""
