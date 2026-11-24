"""Which way a mail of ours goes, measured on the archive (2026-09-25).

intake.mail_direction used to read a sentence such as 'please see below the
revised rates for your cargo' as asking: 'rates for' / 'cost for' / 'rate
from' in the very sentence that points at our amounts is also how a request
is worded. When we state our own amount above the quoted chain, a sentence
pointing the reader at it is giving; an ask still needs a request aimed at
the reader's rate. The fixture holds invented mails of each misread shape,
with the asks that must stay asks; the rate-request fixture and the
target-rate readings do not move."""

from __future__ import annotations

import json

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, detect_followups, intake, mail_archive, parsing, paths
from opplist.outlook_bridge import MailInfo

FIXTURE = paths.FIXTURES_DIR / "mail_direction_samples.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))


def intake_top(body):
    return "\n".join(parsing.body_lines(body or ""))


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_mails_read_as_measured(case):
    assert intake.mail_direction(case["subject"], intake_top(case["body"])) == case["expected"], case["why"]


def test_the_fixtures_were_misread_before():
    """The file keeps what the old reading said, so the fix is on record."""
    misread = [c["name"] for c in CASES if c["expected"] == "quoting"]
    assert len(misread) == 10
    assert all(c["read_before"] in ("asking", "") for c in CASES if c["expected"] == "quoting")
    assert all(c["read_before"] == "asking" for c in CASES if c["expected"] == "asking")


@pytest.mark.parametrize("top", [
    "Please see below the updated rates for your file:\nAir freight: $2.10/kg all in",
    "Please find below cost for both air and sea.\nAir freight: $3.05/kg all in",
    "Below rates for this lane:\nAir freight (+45 kg): $3.40/kg all in",
    "Here’s a second 20’GP rate for the same sailing.\nOcean freight usd3150/20’gp",
    "Please find our revised rates for 2 x 20GP:\nO/F USD 1,450 per 20GP",
    "Please see our revised rates for 2 x 20GP:\nO/F USD 1,450 per 20GP",
    "Rates remain unchanged since last week.\n+1 cbm : $47.00/CBM",
    "Pleased to share the Santos rate for this week:\n$4,300.00 all in per 40'HQ\n"
    "Should a booking come up, please send us the goods description and when they are ready.",
    "Right now the airline will give a spot rate on this lane of $1.15/kg all in, the lowest we have seen.",
])
def test_our_amount_with_a_pointer_is_giving(top):
    assert intake.mail_direction("RE: RATE REQUEST // DTW - SGN", top) == "quoting"


@pytest.mark.parametrize("top", [
    "Please kindly provide your import charges and final delivery for the cargo below:\nValue: $520",
    "We need a rate of $610.00 to keep this account. Can you get there?",
    "Can you confirm your rate is a $85 minimum, then $1.45/kg all in?",
    "Please see the cargo below and quote your best rate:\nInvoice value USD 41,500",
    "Please reach out to the shipper listed below and provide cargo details, rates and the next sailing.\n"
    "Value: USD 8,800",
])
def test_a_request_for_their_rate_stays_asking(top):
    assert intake.mail_direction("RE: RATE REQUEST", top) == "asking"


def test_the_cy_to_door_request_and_target_rate_readings_do_not_move():
    cy_door = json.loads((paths.FIXTURES_DIR / "rate_request_cy_to_door.json").read_text(encoding="utf-8"))
    assert intake.mail_direction(cy_door["subject"], intake_top(cy_door["body"])) == "asking"
    # their target, with a pointer sentence: still theirs
    assert intake.mail_direction("RE: RATE REQUEST", "Please see below, our target rate is USD 1,850 per 40HQ.\n"
                                                     "Please quote.") == "asking"
    assert intake.mail_direction("RE: x", "Target rate USD 1,850, please see below details.") != "quoting"


# ------------------------------------------ what it changes for Re-quoted --
def test_a_revised_rates_for_mail_now_gives_a_requoted_suggestion(tmp_path):
    """8f had to word its fixture around 'revised rates for'; that wording
    is suggested now."""
    path = tmp_path / "rq.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "andes-demo.cl", "party_id": tolvex})
    case = add_enquiry(repo, "OPP-1407", tolvex, received="2026-08-28", conversation_id="CONV-407",
                       source_subject="RE: RATE REQUEST // SEA - VAP")
    repo.set_status(case, "Quoted", "2026-09-01")
    repo.commit()
    body = ("Hi Renata,\n\nPlease see our revised rates for 2 x 20GP:\nO/F USD 1,450 per 20GP\nTHC USD 240\n\n"
            "From: Renata Solis\nSent: Wednesday\n\nPlease requote")
    mail = MailInfo(subject="RE: RATE REQUEST // SEA - VAP", received_on="2026-09-03", received_at="2026-09-03 11:00",
                    sent_at="2026-09-03 11:00", entry_id="E-1", conversation_id="CONV-407",
                    conversation_topic="RATE REQUEST // SEA - VAP", sender_name="JJC-VICTOR LANG",
                    sender_address="VictorLang@jojofreight.example", sender_internal=True,
                    to_addresses=["renata@andes-demo.cl"], cc_addresses=[], to_names=["Renata Solis"],
                    display_to="Renata Solis", body_excerpt=body, folder_path="\\X", message_id="<rev@x>")
    archive = tmp_path / "mail.db"
    handle = mail_archive.open_archive(archive)
    mail_archive.store(handle, [mail_archive.record_from_info(mail, sent_at="2026-09-03 11:00:00")])
    handle.commit()
    handle.close()
    with mail_archive.ArchiveSearch(archive) as search:
        [r] = detect_followups.requoted_suggestions(conn, search)
    assert (r.opp_no, r.at) == ("OPP-1407", "2026-09-03")
    conn.close()
