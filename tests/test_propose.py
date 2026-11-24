"""propose-status and apply-status.

Outlook is stood in for here the way it is everywhere else: the COM calls are
not the interesting part, the reading of a thread is.
"""

from __future__ import annotations

import csv
from collections import Counter

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, propose


class FakeMail:
    """What outlook_bridge hands back, with only the fields propose reads."""

    def __init__(self, at, sender, address, subject="RATE REQUEST // MIA - LIM",
                 body="", internal=False, folder="\\Personal Folder\\TOLVEX",
                 recipients=("VictorLang@jojofreight.example",), conversation="CONV-1"):
        self.received_on = at
        self.sender_name = sender
        self.sender_address = address
        self.sender_internal = internal
        self.subject = subject
        self.body_excerpt = body
        self.conversation_id = conversation
        self.folder_path = folder
        self.recipients = list(recipients)
        self.entry_id = "ID-{}-{}-{}".format(at, address, subject)[:80]


class FakeSearch:
    """Answers the three ways of finding a thread, and counts the calls."""

    def __init__(self, by_conversation=None, by_subject=None, folders=None,
                 by_domains=None):
        self._conversation = by_conversation or {}
        self._subject = by_subject or {}
        self._folders = folders or {}
        self._domains = by_domains or {}
        self.subject_terms = []
        self.domain_calls = []
        self.domain_windows = []
        self.hints = []
        self.expanded = []
        self.hydrated = []

    def by_domains(self, domains, start=None, end=None, limit=600, **kwargs):
        self.domain_calls.append(tuple(domains))
        self.domain_windows.append((start, end))
        self.hints.append((tuple(kwargs.get("hints", ())), kwargs.get("opp_no", "")))
        out = []
        for domain in domains:
            out.extend(self._domains.get(domain, []))
        return out

    def by_conversation(self, conversation_id, limit=200):
        return self._conversation.get(conversation_id, [])

    def by_subject(self, term, limit=200, **kwargs):
        self.subject_terms.append(term)
        self.hints.append((tuple(kwargs.get("hints", ())), kwargs.get("opp_no", "")))
        return self._subject.get(term.lower(), [])

    def conversation_of(self, mail):
        self.expanded.append(mail.conversation_id)
        return list(self._conversation.get(mail.conversation_id, [])) or [mail]

    def hydrate(self, mails):
        self.hydrated.append(len(mails))
        return list(mails)

    def case_folders(self, opp_no):
        return list(self._folders.get(opp_no, []))

    def in_folder(self, info, limit=200):
        return list(info.mails)


class FakeFolder:
    def __init__(self, path, mails):
        self.path = path
        self.mails = mails


@pytest.fixture
def case(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_party(repo, "Seavio", ptype="Unknown", country="USA")
    add_enquiry(repo, "OPP-1307", tolvex, received="2026-08-13",
                status="New", status_confirmed=0, commodity="Kelmora lamps",
                origin_loc="MIA", dest_loc="SAI", service_mode="Air")
    repo.commit()
    return repo.conn


def run(conn, search, **kwargs):
    return propose.propose(conn, search, **kwargs)


# ------------------------------------------------------------- the rules --
def test_the_agent_saying_proceed_is_a_win(case):
    search = FakeSearch(by_conversation={"CONV-1": []}, by_subject={"kelmora": [
        FakeMail("2026-08-13", "Renata", "renata@tolvex.example",
                 body="Hi,\n\nPlease quote air freight.\n"),
        FakeMail("2026-08-15", "JJC-VICTOR LANG", "VictorLang@jojofreight.example",
                 internal=True, body="Our rate is USD 4.20 per kg.\n"),
        FakeMail("2026-08-17", "Renata", "renata@tolvex.example",
                 body="Dear Victor\nPlease proceed with the service.\n"
                      "When can you pick it up?\n"),
        FakeMail("2026-08-19", "JJC-AVA TRAN", "AvaT@jojofreight.example",
                 internal=True, body="Flight schedule below.\n"),
    ]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert proposal.proposed_status == "Won"
    assert proposal.proposed_date == "2026-08-17"
    assert proposal.evidence_quote == "Please proceed with the service."
    assert proposal.evidence_sender == "Renata <renata@tolvex.example>"
    assert proposal.confidence == "high"
    assert proposal.found_by == "rare word 'kelmora'"


def test_the_salutation_is_not_the_evidence(case):
    search = FakeSearch(by_subject={"kelmora": [
        FakeMail("2026-08-17", "Renata", "renata@tolvex.example",
                 body="Dear @JJC-VICTOR LANG <mailto:VictorLang@jojofreight.example>\n"
                      "Please proceed with the service.\n")]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert proposal.evidence_quote == "Please proceed with the service."


def test_somebody_else_on_the_thread_is_only_worth_a_look(case):
    """A shipper's reminder says 'please arrange' too, and means nothing.

    With the requester known by domain it decides nothing at all; only a
    requester with no domain on file leaves the stranger a low-confidence
    say."""
    reminder = FakeMail("2026-08-17", "OrderDesk", "orders@coloria.example",
                        body="Automated notice: please arrange collection of web order 5521.\n",
                        recipients=["VictorLang@jojofreight.example", "renata@tolvex.example"])
    [proposal] = run(case, FakeSearch(by_subject={"kelmora": [reminder]}),
                     opp_nos=["OPP-1307"])
    assert proposal.proposed_status == "Unknown"
    assert proposal.verdict == "undecided"

    case.execute("DELETE FROM party_domain WHERE domain = 'tolvex.example'")
    case.commit()
    [proposal] = run(case, FakeSearch(by_subject={"kelmora": [reminder]}),
                     opp_nos=["OPP-1307"])
    assert proposal.proposed_status == "Won"
    assert proposal.confidence == "low"
    assert "coloria.example" in proposal.evidence_sender


def test_a_later_refusal_lowers_a_win_to_medium(case):
    search = FakeSearch(by_subject={"kelmora": [
        FakeMail("2026-08-17", "Renata", "renata@tolvex.example",
                 body="Please proceed with the service.\n"),
        FakeMail("2026-08-18", "Renata", "renata@tolvex.example",
                 body="Sorry, the customer says the price is too high.\n"),
    ]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    # The newest decision wins, and it is a loss on price.
    assert (proposal.proposed_status, proposal.closed_reason) == ("Lost", "Price too high")
    assert proposal.proposed_date == "2026-08-18"


def test_too_high_is_lost_on_price(case):
    search = FakeSearch(by_subject={"kelmora": [
        FakeMail("2026-08-16", "Renata", "renata@tolvex.example",
                 body="Thanks, but the rate is too high for this shipper.\n")]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert (proposal.proposed_status, proposal.closed_reason) == ("Lost", "Price too high")
    assert proposal.confidence == "high"


def test_awarded_to_another_forwarder_is_lost_to_that_forwarder(case):
    search = FakeSearch(by_subject={"kelmora": [
        FakeMail("2026-08-16", "Renata", "renata@tolvex.example",
                 body="It was awarded to another forwarder this time.\n")]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert (proposal.proposed_status, proposal.closed_reason) == (
        "Lost", "Went with another forwarder")


# test_cancelled_is_lost: replaced in batch C1 by tests/test_cancelled.py and tests/test_cancelled_counts.py (it pinned the old Lost / cancelled words).


def test_only_us_asking_stays_new(case):
    search = FakeSearch(by_subject={"kelmora": [
        tolvex_mail("2026-08-13", "Please advise the gross weight.\n",
                    sender="VictorLang@jojofreight.example", internal=True),
        tolvex_mail("2026-08-15", "Any update on the weight?\n",
                    sender="VictorLang@jojofreight.example", internal=True),
    ]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert proposal.proposed_status == "New"
    assert proposal.note == "waiting on customer"
    assert proposal.proposed_date == ""       # nothing to date it from


def test_no_thread_at_all_is_unknown(case):
    [proposal] = run(case, FakeSearch(), opp_nos=["OPP-1307"])
    assert proposal.proposed_status == "Unknown"
    assert proposal.note == "no thread found in Outlook"
    assert proposal.mails_seen == 0


def test_a_thread_that_decides_nothing_says_so(case):
    search = FakeSearch(by_subject={"kelmora": [
        FakeMail("2026-08-16", "Renata", "renata@tolvex.example",
                 body="Thanks for the update.\n")]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert proposal.proposed_status == "Unknown"
    assert "nothing in it decides" in proposal.note


# ------------------------------------------------------- finding a thread --
def test_the_case_folder_is_tried_before_rare_words(case):
    mails = [FakeMail("2026-08-17", "Renata", "renata@tolvex.example",
                      body="Please proceed with the service.\n")]
    search = FakeSearch(folders={"OPP-1307": [
        FakeFolder("\\Personal Folder\\TOLVEX\\-1307", mails)]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert proposal.found_by.startswith("case folder")
    assert search.subject_terms == []        # never had to guess


def test_the_thread_key_is_confirmed_not_searched_for(repo, conn):
    """The key has the spaces taken out, so no subject ever contains it."""
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    add_enquiry(repo, "OPP-1401", tolvex, received="2026-08-13",
                status_confirmed=0,
                source_subject="RATE REQUEST // KELMORA // MIA - SAI",
                thread_key="RATEREQUESTKELMORAMIASAI")
    repo.commit()
    right = FakeMail("2026-08-17", "Renata", "renata@tolvex.example",
                     subject="RE: RATE REQUEST // KELMORA // MIA - SAI",
                     body="Please proceed with the service.\n")
    wrong = FakeMail("2026-08-18", "Renata", "renata@tolvex.example",
                     subject="RATE REQUEST // KELMORA // ORD - BOG",
                     body="This one was cancelled.\n")
    search = FakeSearch(by_subject={"kelmora": [right, wrong]})
    [proposal] = run(conn, search, opp_nos=["OPP-1401"])
    assert search.subject_terms == ["KELMORA"]     # a word, not the key
    assert proposal.found_by == "thread key"
    assert proposal.proposed_status == "Won"        # the other subject is out


def test_the_conversation_id_wins_when_the_case_has_one(repo, conn):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    add_enquiry(repo, "OPP-1400", tolvex, received="2026-08-13",
                status_confirmed=0, conversation_id="CONV-1")
    repo.commit()
    search = FakeSearch(by_conversation={"CONV-1": [
        FakeMail("2026-08-17", "Renata", "renata@tolvex.example",
                 body="Please proceed with the service.\n")]})
    [proposal] = run(conn, search, opp_nos=["OPP-1400"])
    assert proposal.found_by == "conversation id"


def test_rare_words_come_from_the_whole_book_not_the_shortlist(repo, conn):
    """A word is only rare if it is rare everywhere."""
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    for number in range(1, 12):
        add_enquiry(repo, "OPP-{:04d}".format(number), tolvex,
                    received="2026-08-01", commodity="Machine parts")
    add_enquiry(repo, "OPP-0099", tolvex, received="2026-08-13",
                status_confirmed=0, commodity="Machine parts kelmora")
    repo.commit()
    search = FakeSearch()
    run(conn, search, opp_nos=["OPP-0099"])
    # 'machine' and 'part' are all over the book; only 'kelmora' is rare.
    assert search.subject_terms == ["kelmora"]


# -------------------------------------------------------------- the CSV --
def test_the_csv_carries_the_evidence(case, tmp_path):
    search = FakeSearch(by_subject={"kelmora": [
        FakeMail("2026-08-17", "Renata", "renata@tolvex.example",
                 body="Please proceed with the service.\n")]})
    proposals = run(case, search, opp_nos=["OPP-1307"])
    path = propose.write_csv(proposals, tmp_path / "proposed.csv")
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    assert list(rows[0]) == list(propose.CSV_COLUMNS)
    assert rows[0]["opp_no"] == "OPP-1307"
    assert rows[0]["proposed_status"] == "Won"
    assert rows[0]["evidence_quote"] == "Please proceed with the service."
    assert rows[0]["confidence"] == "high"


def test_nothing_is_written_to_the_database(case, tmp_path):
    before = case.execute("SELECT status, status_confirmed FROM enquiry "
                          "WHERE opp_no = 'OPP-1307'").fetchone()
    events = case.execute("SELECT COUNT(*) AS n FROM status_event").fetchone()["n"]
    search = FakeSearch(by_subject={"kelmora": [
        FakeMail("2026-08-17", "Renata", "renata@tolvex.example",
                 body="Please proceed with the service.\n")]})
    propose.write_csv(run(case, search, opp_nos=["OPP-1307"]),
                      tmp_path / "p.csv")
    after = case.execute("SELECT status, status_confirmed FROM enquiry "
                         "WHERE opp_no = 'OPP-1307'").fetchone()
    assert (after["status"], after["status_confirmed"]) == (
        before["status"], before["status_confirmed"])
    assert case.execute(
        "SELECT COUNT(*) AS n FROM status_event").fetchone()["n"] == events


def test_a_month_only_looks_at_unconfirmed_cases(repo, conn):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    add_enquiry(repo, "OPP-0001", tolvex, received="2026-08-05",
                status_confirmed=0)
    add_enquiry(repo, "OPP-0002", tolvex, received="2026-08-06",
                status_confirmed=1)
    add_enquiry(repo, "OPP-0003", tolvex, received="2026-07-05",
                status_confirmed=0)
    add_enquiry(repo, "OPP-0004", tolvex, received="2026-08-07",
                status_confirmed=0, legacy=1)
    repo.commit()
    proposals = run(conn, FakeSearch(), month="2026-08")
    assert [p.opp_no for p in proposals] == ["OPP-0001"]


# ------------------------------------------------------------- applying --
def _proposal_rows(**overrides):
    row = {
        "opp_no": "OPP-1307", "requester": "Tolvex", "current_status": "New",
        "proposed_status": "Won", "proposed_date": "2026-08-17",
        "closed_reason": "", "evidence_sender": "Renata <renata@tolvex.example>",
        "evidence_date": "2026-08-17",
        "evidence_quote": "Please proceed with the service.",
        "confidence": "high", "awarded_vendor_guess": "", "found_by": "rare word",
        "mails_seen": "4", "note": "",
    }
    row.update(overrides)
    return [row]


def test_applying_writes_the_event_and_confirms_the_status(case):
    repo = db.Repository(case, source="scan")
    applied, skipped = propose.apply(repo, _proposal_rows())
    repo.commit()
    assert applied == [("OPP-1307", "Won", "2026-08-17")] and skipped == []
    row = case.execute("SELECT id, status, status_confirmed FROM enquiry "
                       "WHERE opp_no = 'OPP-1307'").fetchone()
    assert (row["status"], row["status_confirmed"]) == ("Won", 1)
    event = case.execute(
        "SELECT at, status, source, note, kind FROM status_event "
        "WHERE enquiry_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
    assert (event["at"], event["status"], event["source"]) == (
        "2026-08-17", "Won", "scan")
    assert event["kind"] == "status"
    assert "Please proceed" in event["note"]


def test_a_row_that_was_deleted_is_simply_not_applied(case):
    repo = db.Repository(case, source="scan")
    applied, _ = propose.apply(repo, [])
    repo.commit()
    assert applied == []
    assert case.execute("SELECT status_confirmed FROM enquiry "
                        "WHERE opp_no = 'OPP-1307'").fetchone()[0] == 0


def test_unknown_and_missing_rows_are_skipped_with_a_reason(case):
    repo = db.Repository(case, source="scan")
    applied, skipped = propose.apply(repo, (
        _proposal_rows(proposed_status="Unknown")
        + _proposal_rows(opp_no="OPP-9999")
        + _proposal_rows(proposed_date="")))
    repo.commit()
    assert applied == []
    assert [why for _, why in skipped] == [
        "proposed status is 'Unknown'", "no such case", "no date on the row"]


def test_a_loss_without_a_reason_gets_one_rather_than_failing(case):
    repo = db.Repository(case, source="scan")
    propose.apply(repo, _proposal_rows(proposed_status="Lost",
                                       closed_reason=""))
    repo.commit()
    row = case.execute("SELECT status, closed_reason FROM enquiry "
                       "WHERE opp_no = 'OPP-1307'").fetchone()
    assert (row["status"], row["closed_reason"]) == ("Lost", "Other")


def test_a_case_with_no_commodity_still_has_the_agent_to_go_on(repo, conn):
    """'RATE REQUEST // MIA - LIM' with nothing else: search for the agent."""
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    morvel = add_party(repo, "Morvels", country="China")
    for number in range(1, 9):        # Tolvex is on eight cases: no needle
        add_enquiry(repo, "OPP-{:04d}".format(number), tolvex,
                    received="2026-08-01")
    add_enquiry(repo, "OPP-0050", morvel, received="2026-08-13",
                status_confirmed=0, origin_loc="MIA", dest_loc="SAI")
    repo.commit()
    search = FakeSearch(by_subject={"morvel": [
        FakeMail("2026-08-17", "Wei", "wei@morvel.example",
                 body="Please proceed with the booking.\n")]})
    [proposal] = run(conn, search, opp_nos=["OPP-0050"])
    # Get-NameTokens drops the trailing s; '%morvel%' still finds 'Morvels'.
    assert search.subject_terms == ["morvel"]
    assert proposal.found_by == "rare word 'morvel'"
    assert proposal.proposed_status == "Won"


def test_an_agent_on_every_case_is_no_needle(repo, conn):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    for number in range(1, 9):
        add_enquiry(repo, "OPP-{:04d}".format(number), tolvex,
                    received="2026-08-01")
    add_enquiry(repo, "OPP-0050", tolvex, received="2026-08-13",
                status_confirmed=0, origin_loc="MIA", dest_loc="SAI")
    repo.commit()
    search = FakeSearch()
    run(conn, search, opp_nos=["OPP-0050"])
    assert search.subject_terms == []          # nothing worth searching for


def test_the_cargo_word_is_tried_before_the_agent(repo, conn):
    seed_reference(repo)
    morvel = add_party(repo, "Morvels", country="China")
    add_enquiry(repo, "OPP-0050", morvel, received="2026-08-13",
                status_confirmed=0, commodity="Polyurethane adhesive")
    repo.commit()
    search = FakeSearch()
    run(conn, search, opp_nos=["OPP-0050"])
    assert search.subject_terms[0] == "polyurethane"
    assert "morvel" in search.subject_terms


def test_a_locked_csv_becomes_a_second_file_not_an_error(case, tmp_path,
                                                         monkeypatch):
    """Excel holds the file open; minutes of reading must not be lost."""
    search = FakeSearch(by_subject={"kelmora": [
        FakeMail("2026-08-17", "Renata", "renata@tolvex.example",
                 body="Please proceed with the service.")]})
    proposals = run(case, search, opp_nos=["OPP-1307"])
    wanted = tmp_path / "proposed-status-2026-08.csv"

    real_open = type(wanted).open

    def locked(self, *args, **kwargs):
        if self == wanted:
            raise PermissionError(13, "Permission denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(type(wanted), "open", locked)
    written = propose.write_csv(proposals, wanted)
    assert written != wanted
    assert written.name.startswith("proposed-status-2026-08-")
    assert written.exists() and written.read_text(encoding="utf-8-sig")


def test_the_evidence_comes_from_the_message_not_the_signature(case):
    """A status quoting 'Email: ava@jojofreight.example' would be worth nothing."""
    search = FakeSearch(by_subject={"kelmora": [
        FakeMail("2026-08-13", "JJC-VICTOR LANG", "VictorLang@jojofreight.example",
                 internal=True, recipients=["renata@tolvex.example"],
                 body="Hello team,\n"
                      "We still need the gross weight before we can quote.\n"
                      "Thanks / Regards\nVictor Lang\n"
                      "Email: VictorLang@jojofreight.example | Website: www.jojofreight.example\n"
                      "If you would like to no longer receive information\n")]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert proposal.proposed_status == "New"
    assert proposal.evidence_quote == \
        "We still need the gross weight before we can quote."


def test_a_mail_that_is_only_a_signature_quotes_nothing(case):
    search = FakeSearch(by_subject={"kelmora": [
        FakeMail("2026-08-13", "JJC-VICTOR LANG", "VictorLang@jojofreight.example",
                 internal=True,
                 body="Thanks / Regards\nVictor Lang\n"
                      "Tel: 312-555-0140 | Fax: 312-555-0141\n")]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert proposal.evidence_quote == ""      # better than quoting a phone number


def test_a_decision_under_a_sign_off_is_still_read(case):
    """The phrase is looked for in the whole body, signature block or not."""
    search = FakeSearch(by_subject={"kelmora": [
        FakeMail("2026-08-17", "Renata", "renata@tolvex.example",
                 body="Thanks\nRenata\nPS: please proceed with the service.\n")]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert proposal.proposed_status == "Won"
    assert "please proceed with the service" in proposal.evidence_quote.lower()


def test_a_status_set_by_hand_since_the_file_was_made_is_left_alone(case):
    """She is working in the app while the CSV sits on the desktop."""
    repo = db.Repository(case, source="ui")
    row = case.execute("SELECT id FROM enquiry WHERE opp_no = 'OPP-1307'"
                       ).fetchone()
    repo.set_status(row["id"], "Quoted", "2026-08-20")     # her own answer
    repo.commit()

    applied, skipped = propose.apply(repo, _proposal_rows())   # says Won
    repo.commit()
    assert applied == []
    assert skipped == [("OPP-1307", "set to Quoted by hand since this file "
                                    "was made")]
    assert case.execute("SELECT status FROM enquiry WHERE opp_no = 'OPP-1307'"
                        ).fetchone()[0] == "Quoted"


def test_an_untouched_case_still_applies(case):
    repo = db.Repository(case, source="scan")
    applied, skipped = propose.apply(repo, _proposal_rows())
    repo.commit()
    assert [opp for opp, _, _ in applied] == ["OPP-1307"] and skipped == []


# ---------------------------------------- the requester's domain first ----
def tolvex_mail(at, body, sender="renata@tolvex.example", internal=False,
                subject="RATE REQUEST // MIA - LIM", **kwargs):
    name = "JJC-VICTOR LANG" if internal else "Renata"
    recipients = kwargs.pop("recipients", None)
    if recipients is None:
        recipients = ["renata@tolvex.example"] if internal else ["VictorLang@jojofreight.example"]
    return FakeMail(at, name, sender, subject=subject, body=body,
                    internal=internal, recipients=recipients, **kwargs)


def test_the_requesters_domain_is_searched_before_anything_else(case):
    """Mail the agent sent or received in the window is the thread."""
    search = FakeSearch(
        by_domains={"tolvex.example": [
            tolvex_mail("2026-08-17", "Please proceed with the service.\n",
                        subject="RE: Kelmora lamps // MIA - LIM")]},
        by_subject={"kelmora": [
            tolvex_mail("2026-08-16", "The rate is too high.\n")]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert search.domain_calls == [("tolvex.example",)]
    assert search.subject_terms == []                # never needed the words
    assert proposal.proposed_status == "Won"
    assert proposal.found_by.startswith("requester domain tolvex.example")


def test_only_mail_inside_the_window_counts(case):
    """Received 2026-08-13: three days before to forty-five after."""
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-09", "Please proceed with the service.\n"),   # too early
        tolvex_mail("2026-09-28", "The rate is too high.\n"),              # too late
        tolvex_mail("2026-08-10", "Please quote MIA - LIM.\n"),            # day -3
        tolvex_mail("2026-09-27", "Please book the economy service.\n"),   # day +45
    ]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert proposal.mails_seen == 2
    assert proposal.proposed_status == "Won" and proposal.proposed_date == "2026-09-27"


def test_a_mail_that_only_mentions_the_domain_in_the_body_is_not_theirs(case):
    """The domain has to be on the From, To or CC line."""
    search = FakeSearch(by_domains={"tolvex.example": [
        FakeMail("2026-08-17", "Somebody", "x@other.example",
                 body="Ask tolvex.example, they said please proceed.\n",
                 recipients=["VictorLang@jojofreight.example"])]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert proposal.proposed_status == "Unknown"


def test_the_window_is_narrowed_to_this_case_when_something_identifies_it(case):
    """Tolvex is on many threads; the rare word picks this one out."""
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-15", "The rate is too high.\n",
                    subject="RATE REQUEST // OTHER CARGO // ORD - BOG"),
        tolvex_mail("2026-08-17", "Please proceed with the service.\n",
                    subject="URGENT // Kelmora lamps // MIA - SAI"),
    ]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert proposal.proposed_status == "Won"
    assert "rare word 'kelmora'" in proposal.found_by
    assert "1 of 2 mails" in proposal.found_by


def test_with_nothing_to_narrow_by_the_whole_window_stands(repo, conn):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_enquiry(repo, "OPP-1400", tolvex, received="2026-08-13",
                status_confirmed=0)                      # no commodity, no lane
    repo.commit()
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-15", "Anything new?\n"),
        tolvex_mail("2026-08-17", "Please proceed with the service.\n")]})
    [proposal] = run(conn, search, opp_nos=["OPP-1400"])
    assert "whole window, 2 mails" in proposal.found_by
    assert proposal.proposed_status == "Won"


def test_a_contact_address_domain_counts_as_the_requesters(repo, conn):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("contact", {"party_id": tolvex, "name": "Ana",
                            "email": "ana@tolvex-mail.example"})
    add_enquiry(repo, "OPP-1400", tolvex, received="2026-08-13",
                status_confirmed=0)
    repo.commit()
    search = FakeSearch(by_domains={"tolvex-mail.example": [
        tolvex_mail("2026-08-17", "Please proceed with the service.\n",
                    sender="ana@tolvex-mail.example")]})
    [proposal] = run(conn, search, opp_nos=["OPP-1400"])
    assert search.domain_calls == [("tolvex-mail.example",)]
    assert proposal.proposed_status == "Won"


def test_a_requester_with_no_domain_falls_back_to_the_old_ways(repo, conn):
    seed_reference(repo)
    unknown = add_party(repo, "Some Agent", country="USA")
    add_enquiry(repo, "OPP-1400", unknown, received="2026-08-13",
                status_confirmed=0, commodity="Kelmora lamps")
    repo.commit()
    search = FakeSearch(by_subject={"kelmora": [
        FakeMail("2026-08-17", "Bob", "bob@some-agent.example",
                 body="Please proceed with the service.\n")]})
    [proposal] = run(conn, search, opp_nos=["OPP-1400"])
    assert search.domain_calls == []
    assert proposal.found_by == "rare word 'kelmora'"


# ------------------------------------------- the quote is a sentence -----
def test_our_quote_is_recognised_by_its_wording_not_its_numbers(case):
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-14", "Please quote MIA - LIM.\n"),
        tolvex_mail("2026-08-15", "Please see below rates for your reference:\n"
                    "MIA - LIM all in.\n", internal=True,
                    sender="VictorLang@jojofreight.example")]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert proposal.proposed_status == "Quoted"
    assert proposal.evidence_quote.startswith("Please see below rates")


def test_numbers_alone_are_not_a_quote(case):
    """Quoting the customer's own figures back is not us quoting."""
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-15", "Please advise the gross weight; your target "
                    "of USD 4.20 per kg noted.\n", internal=True,
                    sender="VictorLang@jojofreight.example")]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert proposal.proposed_status == "New"
    assert proposal.note == "waiting on customer"


def test_a_quote_beats_waiting_on_customer_even_with_later_chasing(case):
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-15", "Please find attached quotation.\n",
                    internal=True, sender="VictorLang@jojofreight.example"),
        tolvex_mail("2026-08-20", "Any update on this one?\n",
                    internal=True, sender="VictorLang@jojofreight.example")]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert proposal.proposed_status == "Quoted"
    assert proposal.proposed_date == "2026-08-15"


def test_a_quote_stays_a_quote_after_the_customer_writes_back(case):
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-15", "Please see below dest charges.\n",
                    internal=True, sender="VictorLang@jojofreight.example"),
        tolvex_mail("2026-08-16", "Thanks, checking with the shipper.\n")]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert proposal.proposed_status == "Quoted"


# ------------------------------------------------- the open-cases scope ---
def test_open_scope_takes_every_open_case_confirmed_or_not(repo, conn):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    add_enquiry(repo, "OPP-0001", tolvex, received="2026-08-05",
                status="Quoted", status_confirmed=1)
    add_enquiry(repo, "OPP-0002", tolvex, received="2026-08-06",
                status="New", status_confirmed=0)
    add_enquiry(repo, "OPP-0003", tolvex, received="2026-08-07",
                status="Won", status_confirmed=1)
    add_enquiry(repo, "OPP-0004", tolvex, received="2026-08-08",
                status="Sourcing", legacy=1)
    repo.commit()
    rows = propose.cases(conn, scope="open")
    assert [r["opp_no"] for r in rows] == ["OPP-0001", "OPP-0002"]


def test_current_since_is_the_latest_counting_event(repo, conn):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    case_id = add_enquiry(repo, "OPP-0001", tolvex, received="2026-08-05",
                          status="Quoted")
    repo.insert("status_event", {"enquiry_id": case_id, "at": "2026-08-20",
                                 "status": "Quoted", "source": "manual"})
    repo.insert("status_event", {"enquiry_id": case_id, "at": "2026-08-25",
                                 "status": "Quoted", "kind": "note"})
    repo.insert("status_event", {"enquiry_id": case_id, "at": "2026-08-28",
                                 "status": "Won", "voided": 1})
    repo.commit()
    [row] = propose.cases(conn, scope="open")
    assert row["current_since"] == "2026-08-20"     # notes and voided do not count


def test_after_current_reads_only_the_mail_since_the_status(repo, conn):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    case_id = add_enquiry(repo, "OPP-0001", tolvex, received="2026-08-05",
                          status="Quoted", commodity="Kelmora lamps")
    repo.insert("status_event", {"enquiry_id": case_id, "at": "2026-08-20",
                                 "status": "Quoted", "source": "manual"})
    repo.commit()
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-15", "The rate is too high.\n"),               # before
        tolvex_mail("2026-08-22", "Please proceed with the service.\n")]})  # after
    [proposal] = propose.propose(conn, search, scope="open", after_current=True)
    assert proposal.mails_seen == 1
    assert proposal.proposed_status == "Won"
    assert proposal.current_since == "2026-08-20"
    assert proposal.verdict == "change"


def test_nothing_since_the_status_means_keep_it(repo, conn):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    case_id = add_enquiry(repo, "OPP-0001", tolvex, received="2026-08-05",
                          status="Quoted")
    repo.insert("status_event", {"enquiry_id": case_id, "at": "2026-08-20",
                                 "status": "Quoted", "source": "manual"})
    repo.commit()
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-15", "The rate is too high.\n")]})
    [proposal] = propose.propose(conn, search, scope="open", after_current=True)
    assert proposal.proposed_status == "Unknown"
    assert "nothing since 2026-08-20" in proposal.note
    assert proposal.verdict == "no thread"


def test_the_csv_carries_current_since_and_the_summary_counts_verdicts(case, tmp_path):
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-17", "Please proceed with the service.\n")]})
    proposals = run(case, search, opp_nos=["OPP-1307"])
    rows = list(csv.DictReader(propose.write_csv(
        proposals, tmp_path / "p.csv").open(encoding="utf-8-sig")))
    assert list(rows[0])[-1] == "current_since"
    assert rows[0]["current_since"] == "2026-08-13"
    text = "\n".join(propose.render(proposals))
    assert "suggests a change 1" in text


def test_domain_search_is_asked_for_the_case_window_only(case):
    # The dates go into the Outlook query: an agent with years of mail
    # would otherwise fill the search limit before the window was reached.
    search = FakeSearch(by_domains={"tolvex.example": []})
    run(case, search, opp_nos=["OPP-1307"])
    assert search.domain_windows == [("2026-08-10", "2026-09-27")]


def test_public_suffix_is_not_a_requester_domain():
    assert propose.is_public_suffix("com.ar")
    assert propose.is_public_suffix("co.uk")
    assert propose.is_public_suffix("ar")
    assert not propose.is_public_suffix("pampa-demo.com.ar")
    assert not propose.is_public_suffix("tolvex.example")
    assert not propose.is_public_suffix("jojofreight.example")


def test_seed_row_with_public_suffix_only_searches_the_real_domain(case):
    # A seed row 'pampa-demo.com.ar/com.ar' left one party with the
    # "domain" com.ar; that matches every Argentine address.
    party_id = case.execute(
        "SELECT id FROM party WHERE key = 'Tolvex'").fetchone()[0]
    case.execute("INSERT INTO party_domain (domain, party_id) VALUES (?, ?)",
                 ("com.ar", party_id))
    assert propose.requester_domains_of(case, party_id) == {"tolvex.example"}
    search = FakeSearch(by_domains={"tolvex.example": []})
    run(case, search, opp_nos=["OPP-1307"])
    assert search.domain_calls == [("tolvex.example",)]


def test_requester_name_does_not_narrow_its_own_window(case):
    # Every mail in Tolvex's window is Tolvex's; only the cargo word tells
    # this case (Kelmora lamps) from their other ones.
    ours = tolvex_mail("2026-08-15", "Please see below rates for your reference.\n",
                       sender="VictorLang@jojofreight.example", internal=True,
                       subject="Tolvex // Kelmora lamps", conversation="C-A")
    other = tolvex_mail("2026-08-16", "Sorry, we go with another forwarder.\n",
                        subject="Tolvex // Wine pallets", conversation="C-B")
    search = FakeSearch(by_domains={"tolvex.example": [ours, other]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert "rare word 'kelmora'" in p.found_by
    assert p.proposed_status == "Quoted"


def test_search_word_is_the_subject_word_fewest_cases_use():
    df = Counter({"request": 300, "rates": 250, "dravik": 1, "gearbox": 4})
    assert propose._search_word("RE: Rates request // Gearbox Opel - Dravik", df) == "Dravik"
    # every word common: no term, rather than a 45 s walk for 'Request'
    assert propose._search_word("Rates request", df) == ""
    # a word no case uses yet is the best needle of all
    assert propose._search_word("Rates request Kelmora", df) == "Kelmora"
    # stock words are out even when the counts are small
    assert propose._search_word("Urgent quotation shipment", Counter()) == ""
    assert propose._search_word("Urgent quotation Dravik", Counter()) == "Dravik"


def test_subject_frequency_counts_cases_not_mentions(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    add_enquiry(repo, "OPP-0001", tolvex, source_subject="Dravik gearbox Dravik")
    add_enquiry(repo, "OPP-0002", tolvex, source_subject="Dravik hose")
    repo.commit()
    sdf = propose.subject_frequency(repo.conn)
    assert sdf["dravik"] == 2 and sdf["gearbox"] == 1 and "hose" not in sdf


def test_thread_key_search_never_uses_a_common_word(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    for n in range(20):
        add_enquiry(repo, "OPP-%04d" % (1100 + n), tolvex, received="2026-08-01",
                    status="Won", source_subject="Rate request %d" % n,
                    thread_key="RATEREQUEST%d" % n)
    add_enquiry(repo, "OPP-1307", tolvex, received="2026-08-13", status="New",
                status_confirmed=0, source_subject="Rate request Kelmora lamps",
                thread_key="RATEREQUESTKELMORALAMPS")
    repo.commit()
    search = FakeSearch()
    run(repo.conn, search, opp_nos=["OPP-1307"])
    assert "request" not in {t.lower() for t in search.subject_terms}
    assert "kelmora" in {t.lower() for t in search.subject_terms}


def test_one_domain_search_spans_all_of_a_requesters_cases(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_enquiry(repo, "OPP-1307", tolvex, received="2026-08-13", status="New",
                status_confirmed=0, commodity="Kelmora lamps")
    add_enquiry(repo, "OPP-1308", tolvex, received="2026-08-20", status="New",
                status_confirmed=0, commodity="Opel gearbox")
    repo.commit()
    early = tolvex_mail("2026-08-11", "Please proceed with the service.\n",
                        subject="Kelmora lamps")
    late = tolvex_mail("2026-10-01", "Sorry, we go with another forwarder.\n",
                       subject="Opel gearbox")
    search = FakeSearch(by_domains={"tolvex.example": [early, late]})
    a, b = run(repo.conn, search, opp_nos=["OPP-1307", "OPP-1308"])
    # asked once for the union of the two windows...
    assert search.domain_windows == [("2026-08-10", "2026-10-04")] * 2
    # ...but each case keeps its own: 0307 never sees October, 0308 never August
    assert (a.proposed_status, a.mails_seen) == ("Won", 1)
    assert (b.proposed_status, b.mails_seen) == ("Lost", 1)


def test_folders_are_walked_most_likely_first(case):
    search = FakeSearch(by_domains={"tolvex.example": []})
    run(case, search, opp_nos=["OPP-1307"])
    assert search.hints[0] == (("tolvex",), "OPP-1307")


# ------------------------------------------ what the thread dump taught --
def test_their_mail_is_completed_with_our_side_of_the_conversation(case):
    """The domain search returns only what Tolvex sent (Outlook's To/CC are
    display names); our quote sits in the same conversation."""
    theirs = tolvex_mail("2026-08-18", "Any update..\n", conversation="C-1",
                         subject="RE: DDU RATES")
    ours = tolvex_mail("2026-08-15", "Please see below our best rates:\n",
                       sender="VictorLang@jojofreight.example", internal=True,
                       conversation="C-1", subject="RE: DDU RATES")
    search = FakeSearch(by_domains={"tolvex.example": [theirs]},
                        by_conversation={"C-1": [theirs, ours]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert search.expanded == ["C-1"]
    assert p.mails_seen == 2
    assert (p.proposed_status, p.proposed_date) == ("Quoted", "2026-08-15")


def test_a_window_shared_with_their_other_cases_is_not_read_as_this_one(repo):
    """Tolvex's 'please proceed' for the Kelmora lamps must not close the
    Opel gearbox case just because both fell in the same weeks."""
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_enquiry(repo, "OPP-1307", tolvex, received="2026-08-13", status="New",
                status_confirmed=0, commodity="Kelmora lamps")
    add_enquiry(repo, "OPP-1308", tolvex, received="2026-08-13", status="New",
                status_confirmed=0, commodity="Opel gearbox",
                source_subject="Air rates quotation FCA Gearbox Opel M32 - Dravik",
                thread_key="AIRRATESQUOTATIONFCAGEARBOXOPELM32DRAVIK")
    repo.commit()
    kelmora = tolvex_mail("2026-08-17", "Please proceed with the service.\n",
                           subject="RE: Pedido lamparas Kelmora Chile")
    search = FakeSearch(by_domains={"tolvex.example": [kelmora]})
    a, b = run(repo.conn, search, opp_nos=["OPP-1307", "OPP-1308"])
    assert a.proposed_status == "Won"                      # narrowed by 'kelmora'
    assert b.proposed_status == "Unknown"
    assert b.verdict == "no thread"
    assert "none identifiable as this case" in b.note
    assert "OPP-1307" in b.note


def test_their_only_case_in_the_window_takes_the_whole_window(case):
    """Nothing names the case in the requester's mail, but it is the only
    case of theirs in these weeks - so their mail is its thread."""
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-17", "Please proceed with the service.\n",
                    subject="RE: something else entirely")]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert p.proposed_status == "Won"
    assert p.found_by == "requester domain tolvex.example (whole window, 1 mails)"


def test_identity_searches_come_before_a_whole_window(case):
    """When their mail cannot be narrowed, the thread-key search runs
    before the window is taken as a whole."""
    case.execute("UPDATE enquiry SET source_subject = 'Air quotation Dravik', "
                 "thread_key = 'AIRQUOTATIONDRAVIK' WHERE opp_no = 'OPP-1307'")
    case.commit()
    search = FakeSearch(
        by_domains={"tolvex.example": [
            tolvex_mail("2026-08-17", "Please proceed with the service.\n",
                        subject="RE: something else entirely")]},
        by_subject={"dravik": [
            tolvex_mail("2026-08-16", "Sorry, the rate is too high.\n",
                        subject="RE: Air quotation Dravik")]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert p.found_by == "thread key"
    assert p.proposed_status == "Lost"


@pytest.mark.parametrize("sentence", [
    "Please see below our two routing options with rates:",
    "Please see below updated rates now that the cargo is non-DG:",
    "Please find attached our revised quotation.",
    "Kindly find below the all-in rates:",
    "Our quotation as below.",
])
def test_a_quote_is_known_by_its_shape(case, sentence):
    search = FakeSearch(by_subject={"kelmora": [
        tolvex_mail("2026-08-15", sentence + "\nUSD 1,234 per container\n",
                    sender="VictorLang@jojofreight.example", internal=True)]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert p.proposed_status == "Quoted", sentence


@pytest.mark.parametrize("sentence", [
    "Thank You for your rates",
    "Thank you very much for your quotation.",
    "Thnks fr yr offer, passing it to the buyer today",
    "Gracias por la cotización, la revisamos con el cliente.",
])
def test_the_agent_thanking_us_for_the_rates_means_we_quoted(case, sentence):
    """Our quote may never have reached this mailbox; theirs did."""
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-18", sentence + "\n")]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert (p.proposed_status, p.proposed_date, p.confidence) == \
        ("Quoted", "2026-08-18", "high"), sentence
    assert p.note == "the agent acknowledged our quote"


def test_our_chase_about_securing_the_business_means_we_quoted(case):
    search = FakeSearch(by_subject={"kelmora": [
        tolvex_mail("2026-08-19", "Please see below:\nBOG/MIA routing, Avianca Cargo\n",
                    sender="VictorLang@jojofreight.example", internal=True),
        tolvex_mail("2026-08-24", "Did the buyer give any feedback on our quotation "
                    "of the 19th?\n",
                    sender="VictorLang@jojofreight.example", internal=True)]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert (p.proposed_status, p.proposed_date) == ("Quoted", "2026-08-24")
    assert "follow-up" in p.note


def test_out_of_budget_is_lost_on_price_unless_we_quoted_again(case):
    out_of_budget = tolvex_mail(
        "2026-08-27", "Their buyer says this is over budget for them.\n"
        "Could you look at it again as non-hazardous cargo?\n")
    search = FakeSearch(by_subject={"kelmora": [out_of_budget]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert (p.proposed_status, p.closed_reason) == ("Lost", "Price too high")

    requote = tolvex_mail(
        "2026-08-27", "Please see below updated rates now that the cargo is non-DG:\n",
        sender="VictorLang@jojofreight.example", internal=True)
    search = FakeSearch(by_subject={"kelmora": [out_of_budget, requote]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert (p.proposed_status, p.proposed_date) == ("Quoted", "2026-08-27")
    assert p.note.startswith("re-quoted after 'over budget'")


def test_vendor_mail_on_a_tbc_case_decides_nothing(repo):
    """A case parked on (TBC) has no requester; a vendor asking for the
    collection address, then sending its quotes, is not the customer."""
    seed_reference(repo)
    tbc = add_party(repo, "(TBC)", ptype="Unknown", country="USA")
    add_enquiry(repo, "OPP-1337", tbc, received="2026-08-19", status="Sourcing",
                status_confirmed=0, commodity="Lab reagents Corviant")
    repo.commit()
    search = FakeSearch(folders={"OPP-1337": [FakeFolder("\\-1337", [
        FakeMail("2026-08-19", "Brightwing-Export", "air@brightwing.example",
                 body="We need the full collection address before we can proceed "
                      "with quotation, thanks\n", recipients=["AvaT@jojofreight.example"]),
        FakeMail("2026-08-20", "Brightwing-Export", "air@brightwing.example",
                 body="Our two options are on the sheet we attached\n",
                 recipients=["AvaT@jojofreight.example"]),
    ])]})
    [p] = run(repo.conn, search, opp_nos=["OPP-1337"])
    assert p.proposed_status == "Unknown"
    assert p.note == "requester not known yet - vendor mail decides nothing"


def test_proceed_with_quotation_is_not_a_go_ahead(case):
    search = FakeSearch(by_subject={"kelmora": [
        tolvex_mail("2026-08-17", "Once we have the dock address we can proceed "
                    "with the quotation.\n")]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert p.proposed_status != "Won"
    search = FakeSearch(by_subject={"kelmora": [
        tolvex_mail("2026-08-17", "Please proceed with the booking.\n")]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert p.proposed_status == "Won"


def test_a_case_with_no_received_date_still_gets_its_folder_and_words(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_enquiry(repo, "OPP-1337", tolvex, status="New", status_confirmed=0,
                commodity="Kelmora lamps")
    repo.conn.execute("UPDATE enquiry SET received_on = NULL WHERE opp_no = 'OPP-1337'")
    repo.commit()
    search = FakeSearch(by_subject={"kelmora": [
        tolvex_mail("2026-08-17", "Please proceed with the service.\n")]})
    [p] = run(repo.conn, search, opp_nos=["OPP-1337"])
    assert search.domain_calls == []
    assert p.proposed_status == "Won"


def test_a_dateless_case_takes_the_thread_that_opened_on_its_received_date(repo):
    """OPP-1307 (workbook: 'Tessaro toner cartridges', no subject) is the
    Tolvex thread that opened on 2026-08-13 - 'Pedido 5521 - Lumaq Norte'
    shares no word with the commodity."""
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_enquiry(repo, "OPP-1307", tolvex, received="2026-08-13", status="New",
                status_confirmed=0, commodity="Tessaro toner cartridges")
    add_enquiry(repo, "OPP-1308", tolvex, received="2026-08-13", status="New",
                status_confirmed=0, commodity="Opel gearbox",
                source_subject="Air rates quotation Dravik",
                thread_key="AIRRATESQUOTATIONDRAVIK")
    repo.commit()
    lumaq = "URGENT AIR SHIPMENT // Pedido 5521 - Lumaq Norte"
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-13", "Please quote air MIA - SCL.\n", subject=lumaq),
        tolvex_mail("2026-08-17", "Please proceed with the service.\n",
                    subject="RE: " + lumaq),
        tolvex_mail("2026-08-19", "New order, please quote.\n",
                    subject="NEW ORDER // BELTRA - OSORNO"),
    ]})
    a, b = run(repo.conn, search, opp_nos=["OPP-1307", "OPP-1308"])
    assert a.found_by.startswith("requester domain tolvex.example (thread started on the received date, 2 of 3 mails)")
    assert (a.proposed_status, a.proposed_date) == ("Won", "2026-08-17")
    # 0308 has a thread key of its own that nothing in the pool matches:
    # the same thread is not handed to it as well
    assert b.proposed_status == "Unknown" and b.verdict == "no thread"


def test_two_threads_opening_the_same_day_tell_nothing(case):
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-13", "Please quote.\n", subject="Order A"),
        tolvex_mail("2026-08-13", "Please quote.\n", subject="Order B"),
        tolvex_mail("2026-08-17", "Please proceed with the service.\n",
                    subject="RE: Order A"),
    ]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    # their only case in the window: the whole window is still read
    assert "whole window" in p.found_by


def test_two_dateless_cases_received_the_same_day_tell_nothing(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_enquiry(repo, "OPP-1307", tolvex, received="2026-08-13", status="New",
                status_confirmed=0, commodity="Inks")
    add_enquiry(repo, "OPP-1309", tolvex, received="2026-08-13", status="New",
                status_confirmed=0, commodity="Pumps")
    repo.commit()
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-13", "Please quote.\n", subject="Order A"),
        tolvex_mail("2026-08-17", "Please proceed with the service.\n",
                    subject="RE: Order A"),
    ]})
    a, b = run(repo.conn, search, opp_nos=["OPP-1307", "OPP-1309"])
    assert a.verdict == b.verdict == "no thread"


def test_a_subject_kept_in_the_commodity_column_serves_the_search(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    subject = "RE: Air and sea options // PO 8830-17 // HALVORSEN DRYERS"
    add_enquiry(repo, "OPP-1344", tolvex, received="2026-08-20", status="New",
                status_confirmed=0, commodity=subject,
                thread_key="AIRANDSEAOPTIONSPO883017HALVORSENDRYERS")
    repo.commit()
    search = FakeSearch(by_subject={"halvorsen": [
        tolvex_mail("2026-08-22", "Thank you for your rates.\n", subject=subject)]})
    [p] = run(repo.conn, search, opp_nos=["OPP-1344"])
    assert p.found_by == "thread key"
    assert p.proposed_status == "Quoted"


# --------------------------------------------- the second scoring run --
def test_a_stranger_on_the_thread_decides_nothing_when_the_requester_is_known(case):
    """A vendor on the thread saying a colleague has left is not the customer."""
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-14", "Please quote 1 x 40 ft Xiamen - Guayaquil.\n",
                    subject="Rate request ex Xiamen - Guayaquil"),
        tolvex_mail("2026-08-16", "Marek is no longer with us; send new requests to me.\n",
                    sender="desk@tradewise.example",
                    subject="RE: Rate request ex Xiamen - Guayaquil"),
        tolvex_mail("2026-08-18", "Thank You for your rates\n",
                    subject="RE: Rate request ex Xiamen - Guayaquil"),
    ]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert (p.proposed_status, p.proposed_date) == ("Quoted", "2026-08-18")


# test_no_longer_alone_is_not_a_loss: replaced in batch C1 by tests/test_cancelled.py and tests/test_cancelled_counts.py (it pinned the old Lost / cancelled words).


def test_a_rare_word_hit_that_never_touches_the_requester_is_not_theirs(case):
    """A rare word can find another company's 'please book' for this case."""
    case.execute("UPDATE enquiry SET commodity = 'Sonar recorders' WHERE opp_no = 'OPP-1307'")
    case.commit()
    search = FakeSearch(by_subject={"sonar": [
        FakeMail("2026-09-02", "Lena Hart", "lena@cargoways.example",
                 subject="Sonar units // NOU", body="Please book the economy service.\n",
                 recipients=["VictorLang@jojofreight.example"])]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert p.verdict == "no thread"


@pytest.mark.parametrize("sentence", [
    "Please see below our lowest LCL consolidation rates for this pickup:",
    "Please see below our air freight airport \u2013 airport rates for the three carriers:",
    "Kindly see the FCL port-to-door rates below.",
])
def test_a_quote_sentence_may_carry_a_few_words_of_its_own(case, sentence):
    search = FakeSearch(by_subject={"kelmora": [
        tolvex_mail("2026-08-21", sentence + "\n", sender="VictorLang@jojofreight.example",
                    internal=True)]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert p.proposed_status == "Quoted", sentence


def test_a_own_office_as_requester_is_told_by_the_name_prefix(repo):
    seed_reference(repo)
    jjt = add_party(repo, "JoJo JJT", ptype="Office", country="China")
    add_enquiry(repo, "OPP-1351", jjt, received="2026-08-19", status="New",
                status_confirmed=0, commodity="Automotive molds",
                source_subject="inquiry for local charge 157-20468835",
                thread_key="INQUIRYFORLOCALCHARGE15720468835")
    repo.commit()
    subject = "RE: inquiry for local charge 157-20468835"
    search = FakeSearch(by_subject={"157-20468835": [], "local": [], "charge": [],
                                    "inquiry": []},
                        folders={"OPP-1351": [FakeFolder("\\-1351", [
        FakeMail("2026-08-19", "JJC-VICTOR LANG", "VictorLang@jojofreight.example",
                 internal=True, subject=subject,
                 body="Local charges for this one are in the table below.\n"),
        FakeMail("2026-08-21", "JJT-FRANK ZHOU", "frankzhou@jojofreight.example",
                 internal=True, subject=subject,
                 body="Go ahead and book it, please.\n"),
    ])]})
    assert propose.office_prefix("JoJo JJT") == "JJT-"
    assert propose.office_prefix("Tolvex") == ""
    [p] = run(repo.conn, search, opp_nos=["OPP-1351"])
    assert (p.proposed_status, p.proposed_date, p.confidence) == ("Won", "2026-08-21", "high")
    assert p.evidence_sender.startswith("JJT-FRANK ZHOU")


def test_the_agent_passing_our_proposal_on_means_we_quoted(case):
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-19", "Got it, I sent the proposal on to our "
                    "client this morning.\n",
                    subject="RE: Kelmora lamps quotation")]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert (p.proposed_status, p.proposed_date) == ("Quoted", "2026-08-19")


def test_our_own_sourcing_mail_is_not_waiting_on_the_customer(case):
    """Our JJB office sending Victor its local costs; nobody asked the customer."""
    search = FakeSearch(by_subject={"kelmora": [
        tolvex_mail("2026-08-16", "CMA CGM: USD 3,150 per 40HQ, cut-off Thursday\n",
                    sender="hana.ito@jojofreight.example", internal=True),
        tolvex_mail("2026-08-17", "Got the costs, many thanks.\n",
                    sender="VictorLang@jojofreight.example", internal=True)]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert p.proposed_status == "Unknown"
    assert p.verdict == "undecided"
    assert "none of it asks the customer" in p.note


def test_the_thread_is_hydrated_once_it_is_found(case):
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-17", "Please proceed with the service.\n",
                    subject="RE: Kelmora lamps")]})
    run(case, search, opp_nos=["OPP-1307"])
    assert search.hydrated == [1]


def test_a_header_matched_mail_counts_as_theirs_without_recipients(case):
    """A light read has no recipients; Outlook already matched the domain
    on a header, and that is what the flag records."""
    ours = tolvex_mail("2026-08-15", "Please see below rates for your reference.\n",
                       sender="VictorLang@jojofreight.example", internal=True,
                       subject="RE: Kelmora lamps", recipients=[])
    ours.header_match = True
    search = FakeSearch(by_domains={"tolvex.example": [ours]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert p.proposed_status == "Quoted"


@pytest.mark.parametrize("sentence", [
    "Quote is approved.",
    "Here you go: shipping documents attached, collection can be Monday.",
    "La cotización quedó aprobada.",
])
def test_the_customer_approving_or_sending_the_papers_is_a_win(case, sentence):
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-27", sentence + "\n", subject="RE: Kelmora lamps")]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert (p.proposed_status, p.confidence) == ("Won", "high"), sentence


def test_our_own_booking_mail_is_a_win_when_the_customers_yes_is_missing(case):
    search = FakeSearch(by_subject={"kelmora": [
        tolvex_mail("2026-08-27", "Hello Petra,\nA new booking came in from the "
                    "shipper Lantrec, LCL, details underneath:\n",
                    sender="VictorLang@jojofreight.example", internal=True,
                    subject="NEW BOOKING // C/ORVALE // LCL // SGN - SAV // Kelmora"),
        tolvex_mail("2026-08-27", "Noted with thanks, we will inform origin "
                    "to proceed with the pickup.\n",
                    sender="VictorLang@jojofreight.example", internal=True,
                    subject="RE: NEW BOOKING // C/ORVALE // LCL // SGN - SAV // Kelmora")]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert (p.proposed_status, p.proposed_date, p.confidence) == ("Won", "2026-08-27", "medium")
    assert "inform origin to proceed" in p.evidence_quote.lower()


def test_please_keep_this_on_record_is_a_win(case):
    search = FakeSearch(by_subject={"kelmora": [
        tolvex_mail("2026-08-19", "Booked on our side, keep this on record for the list.\n",
                    sender="VictorLang@jojofreight.example", internal=True,
                    subject="FW: local costs Kelmora")]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert p.proposed_status == "Won"
    assert p.note.startswith("our own booking mail")


# test_the_customers_word_comes_before_our_booking_mail: replaced in batch C1 by tests/test_cancelled.py and tests/test_cancelled_counts.py (it pinned the old Lost / cancelled words).


@pytest.mark.parametrize("sentence", [
    "Only 40HQ equipment is free this week, rates as per below:",
    "Below rates are valid to the month end:",
    "Below are the updated rates for your lane:",
])
def test_more_shapes_of_a_quote(case, sentence):
    search = FakeSearch(by_subject={"kelmora": [
        tolvex_mail("2026-08-24", sentence + "\nOcean freight USD 7,350 per box\n",
                    sender="VictorLang@jojofreight.example", internal=True)]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert p.proposed_status == "Quoted", sentence


def test_among_threads_opening_the_same_day_the_one_naming_the_commodity_wins(repo):
    """Tolvex opens several threads a day; only 'Pedido 5521 - Lumaq Norte'
    mentions the Tessaro cartridges in its body."""
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_enquiry(repo, "OPP-1307", tolvex, received="2026-08-13", status="New",
                status_confirmed=0, commodity="Tessaro toner cartridges, cleaning kits")
    repo.commit()
    inks = tolvex_mail("2026-08-13", "Please quote air MIA - SCL for 4 pallets of "
                       "Tessaro toner cartridges.\n",
                       subject="URGENT AIR SHIPMENT // Pedido 5521 - Lumaq Norte")
    inks_won = tolvex_mail("2026-08-17", "Please proceed with the service.\n",
                           subject="RE: URGENT AIR SHIPMENT // Pedido 5521 - Lumaq Norte")
    pumps = tolvex_mail("2026-08-13", "Please quote ocean for the pumps.\n",
                        subject="Order 318 // FERNWELL PUMPS")
    pumps_lost = tolvex_mail("2026-08-18", "The rate is too high.\n",
                             subject="RE: Order 318 // FERNWELL PUMPS")
    search = FakeSearch(by_domains={"tolvex.example": [inks, inks_won, pumps, pumps_lost]})
    [p] = run(repo.conn, search, opp_nos=["OPP-1307"])
    assert "thread started on the received date, commodity in the mail" in p.found_by
    assert (p.proposed_status, p.proposed_date) == ("Won", "2026-08-17")
    # only the two opening mails were read in full before the thread itself
    assert search.hydrated[0] == 2


def test_asking_our_own_office_is_not_waiting_on_the_customer(case):
    """Victor asking our JQG office for the destination charges is sourcing."""
    ask_office = tolvex_mail("2026-08-20", "Please send your destination charges "
                             "for a DAP delivery, details underneath:\n",
                             sender="VictorLang@jojofreight.example", internal=True,
                             recipients=["terencekoh@jojofreight.example"])
    ask_office.display_to = "JQG-TERENCE KOH"
    [p] = run(case, FakeSearch(folders={"OPP-1307": [FakeFolder("-1307", [ask_office])]}),
              opp_nos=["OPP-1307"])
    assert p.proposed_status == "Unknown"
    assert "none of it asks the customer" in p.note

    ask_customer = tolvex_mail("2026-08-20", "Please provide the gross weight.\n",
                               sender="VictorLang@jojofreight.example", internal=True,
                               recipients=["renata@tolvex.example"])
    ask_customer.display_to = "'renata@tolvex.example'"
    [p] = run(case, FakeSearch(by_subject={"kelmora": [ask_customer]}),
              opp_nos=["OPP-1307"])
    assert (p.proposed_status, p.note) == ("New", "waiting on customer")


def test_with_no_domain_on_file_our_own_mail_alone_decides_nothing(repo):
    seed_reference(repo)
    brenton = add_party(repo, "Corvane Tooling", ptype="Direct Customer",
                    country="USA")
    add_enquiry(repo, "OPP-1345", brenton, received="2026-08-20", status="New",
                status_confirmed=0, commodity="Tile spacers")
    repo.commit()
    search = FakeSearch(by_subject={"spacer": [
        FakeMail("2026-08-20", "JJC-VICTOR LANG", "VictorLang@jojofreight.example",
                 internal=True, subject="DAP RATE REQUEST // BOS - KUL // spacers",
                 body="Please send your destination charges for the spacers.\n")]})
    [p] = run(repo.conn, search, opp_nos=["OPP-1345"])
    assert p.proposed_status == "Unknown"
    assert "no domain on file" in p.note


def test_threads_that_belong_to_their_other_cases_are_set_aside(repo):
    """Several Tolvex threads opened on 13 and 14 August; the others carry
    the thread keys of OPP-1308 and OPP-1314. The one left is OPP-1307."""
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_enquiry(repo, "OPP-1307", tolvex, received="2026-08-13", status="New",
                status_confirmed=0, commodity="Tessaro toner cartridges, cleaning kits")
    add_enquiry(repo, "OPP-1308", tolvex, received="2026-08-13", status="New",
                status_confirmed=0, commodity="Opel gearbox",
                source_subject="AIR RATES QUOTATION (FCA) - Gearbox OPEL M32 - DRAVIK",
                thread_key="AIRRATESQUOTATIONFCAGEARBOXOPELM32DRAVIK")
    add_enquiry(repo, "OPP-1314", tolvex, received="2026-08-14", status="New",
                status_confirmed=0, commodity="Express parcels",
                source_subject="FCA AIR RATE // CANADA - ECUADOR // AIR & EXPRESS",
                thread_key="FCAAIRRATECANADAECUADORAIREXPRESS")
    repo.commit()
    search = FakeSearch(by_domains={"tolvex.example": [
        tolvex_mail("2026-08-13", "Please quote urgent air MIA - SCL.\n",
                    subject="URGENT AIR SHIPMENT // Pedido 5521 - Lumaq Norte"),
        tolvex_mail("2026-08-17", "Please proceed with the service.\n",
                    subject="RE: URGENT AIR SHIPMENT // Pedido 5521 - Lumaq Norte"),
        tolvex_mail("2026-08-13", "Rates for the gearbox please.\n",
                    subject="RE: AIR RATES QUOTATION (FCA) - Gearbox OPEL M32 - DRAVIK"),
        tolvex_mail("2026-08-14", "Express rates please.\n",
                    subject="FCA AIR RATE // CANADA - ECUADOR // AIR & EXPRESS"),
    ]})
    [p] = run(repo.conn, search, opp_nos=["OPP-1307"])
    assert "the others are their other cases" in p.found_by
    assert (p.proposed_status, p.proposed_date) == ("Won", "2026-08-17")


@pytest.mark.parametrize("sentence", [
    "Please kindly quote below:",
    "Kindly check and send us air rates as per below:",
    "Pls give us rates as per below, both origins:",
])
def test_us_asking_a_vendor_for_a_quote_is_not_a_quote(case, sentence):
    ask = tolvex_mail("2026-08-12", sentence + "\nOrigin: MIA\nDest: SCL\n",
                      sender="VictorLang@jojofreight.example", internal=True,
                      recipients=["rates@vendor.example"])
    [p] = run(case, FakeSearch(folders={"OPP-1307": [FakeFolder("-1307", [ask])]}),
              opp_nos=["OPP-1307"])
    assert p.proposed_status != "Quoted", sentence


def test_a_quote_for_their_reference_is_still_a_quote(case):
    search = FakeSearch(by_subject={"kelmora": [
        tolvex_mail("2026-08-15", "Please see below rates for your reference:\n",
                    sender="VictorLang@jojofreight.example", internal=True)]})
    [p] = run(case, search, opp_nos=["OPP-1307"])
    assert p.proposed_status == "Quoted"


# ---------------------------------- they want the rate again ---
def _requote_thread(body):
    return FakeSearch(by_subject={"kelmora": [
        FakeMail("2026-08-13", "Renata", "renata@tolvex.example", body="Please quote.\n"),
        FakeMail("2026-08-15", "JJC-VICTOR LANG", "VictorLang@jojofreight.example", internal=True,
                 body="Please see below rates for your reference.\n"),
        FakeMail("2026-08-18", "Renata", "renata@tolvex.example", body=body),
    ]})


def test_asking_for_the_rate_again_is_no_longer_a_proposal(case):
    """Retired 2026-09-25: their 'please requote' is R3's Replied step; a
    re-quote is read from our own revised mail (detect_followups). The
    thread still reads as quoted, from our 15 Aug mail."""
    for body in ("We are still quoting on our side, will revert shortly.\n",
                 "Please requote with 3 x 40HQ instead.\n",
                 "Can you update the rate to include the door delivery?\n"):
        [proposal] = run(case, _requote_thread(body), opp_nos=["OPP-1307"])
        assert not propose.is_requote_note(proposal.note), body
        assert proposal.proposed_date != "2026-08-18", body


def test_a_decision_outranks_a_request_to_quote_again(case):
    """Won and Lost are decisions; 'please requote' is more work. The
    decision wins wherever it sits in the thread."""
    search = FakeSearch(by_subject={"kelmora": [
        FakeMail("2026-08-18", "Renata", "renata@tolvex.example", body="Please requote with 3 x 40HQ.\n"),
        FakeMail("2026-08-20", "Renata", "renata@tolvex.example",
                 body="All good, please proceed with the booking.\n"),
    ]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert (proposal.proposed_status, proposal.proposed_date) == ("Won", "2026-08-20")

    search = FakeSearch(by_subject={"kelmora": [
        FakeMail("2026-08-18", "Renata", "renata@tolvex.example",
                 body="Your rate is too high, we went with another forwarder.\n"),
        FakeMail("2026-08-20", "Renata", "renata@tolvex.example",
                 body="Please requote if you can do something for the next one.\n"),
    ]})
    [proposal] = run(case, search, opp_nos=["OPP-1307"])
    assert (proposal.proposed_status, proposal.closed_reason) == ("Lost", "Price too high")
