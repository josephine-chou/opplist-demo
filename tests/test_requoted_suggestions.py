"""Re-quote suggestions follow the button (2026-09-25): Re-quoted
records a revised quote that went out, so the suggestion is read from our
own mail - a rate or charge with an amount, To the requester, after the
last quote - and the requester's 'please requote' suggests nothing (that
mail is R3's Replied step)."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, detect_followups, mail_archive, propose, suggestions, tracking, withdraw_requote
from opplist.outlook_bridge import MailInfo

VICTOR = "VictorLang@jojofreight.example"


def mail(subject, sender, name, sent, body, to, internal=False, mid="", to_names=None, conversation="CONV-407"):
    return MailInfo(subject=subject, received_on=sent[:10], received_at=sent, sent_at=sent,
                    entry_id="E-" + sent, conversation_id=conversation,
                    conversation_topic=subject.replace("RE: ", ""), sender_name=name,
                    sender_address=sender, sender_internal=internal,
                    to_addresses=list(to), cc_addresses=[], to_names=list(to_names or []),
                    display_to="; ".join(to_names or to), body_excerpt=body, folder_path="\\X",
                    message_id=mid or "<{}@x>".format(sent.replace(" ", "T")))


RENATA = "renata@tolvex.example"
SUBJ = "OCEAN PRICING // LAX - SAI // MINING SPARES"
QUOTE = ("Hi Renata,\n\nOur offer is as follows.\nOcean freight USD 2,040 per 40HQ\nTHC USD 265\n\n"
         "From: Renata Solis\nSent: Monday\n\nPlease quote")
ASK_AGAIN = ("Hi Victor,\n\nOur shipper now wants to split it. Could you requote on 2 x 40HQ?\n\n"
             "From: JJC-VICTOR LANG\nSent: Monday\n\nOur offer is as follows.")
# a revised quote as we word one: the reader is pointed at the new amounts
REVISED = ("Hi Renata,\n\nKindly see the revised pricing for 2 x 40HQ:\nOcean freight USD 1,880 per 40HQ\n"
           "THC USD 265\n\nFrom: Renata Solis\nSent: Wednesday\n\nCould you requote")
CHASER = ("Hi Renata,\n\nFollowing up on the offer below - any feedback from the consignee?\n"
          "Ocean freight USD 2,040 per 40HQ\n\nFrom: JJC-VICTOR LANG\nSent: Monday\n\nOur offer is as follows.")
ASK_VENDOR = ("Hi Kyle,\n\nPlease quote us LAX - SAI, 2 x 40HQ, general cargo.\n\n"
              "From: Renata Solis\nSent: Wednesday\n\nCould you requote")


def thread(*extra):
    return [
        mail(SUBJ, RENATA, "Renata Solis", "2026-08-28 09:00",
             "please quote", [VICTOR]),
        mail("RE: " + SUBJ, VICTOR, "JJC-VICTOR LANG", "2026-09-01 15:00", QUOTE,
             [RENATA], internal=True, mid="<quote@x>"),
        mail("RE: " + SUBJ, RENATA, "Renata Solis", "2026-09-02 10:00",
             ASK_AGAIN, [VICTOR], mid="<ask@x>"),
    ] + list(extra)


def revised(sent="2026-09-03 11:00", body=REVISED, to=(RENATA,), mid="<revised@x>", **kw):
    return mail("RE: " + SUBJ, VICTOR, "JJC-VICTOR LANG", sent, body, to,
                internal=True, mid=mid, **kw)


def build_archive(tmp_path, mails):
    path = tmp_path / "mail.db"
    archive = mail_archive.open_archive(path)
    mail_archive.store(archive, [mail_archive.record_from_info(m, sent_at=m.sent_at + ":00") for m in mails])
    archive.commit()
    archive.close()
    return path


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "rq.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    case = add_enquiry(repo, "OPP-1407", tolvex, received="2026-08-28", conversation_id="CONV-407",
                       source_subject="RE: " + SUBJ, origin_loc="LAX",
                       origin_country="USA", dest_loc="SAI", dest_country="Chile")
    repo.set_status(case, "Quoted", "2026-09-01")
    # the daily run recorded their reply on the 2nd: R3's Replied step
    repo.add_note(case, "2026-09-02", detect_followups.reply_note(
        "Renata Solis", "Our shipper now wants to split it. Could you requote on 2 x 40HQ?",
        "<ask@x>", "asks"), source="mail")
    repo.commit()
    yield conn, repo, case
    conn.close()


def found(conn, archive):
    with mail_archive.ArchiveSearch(archive) as search:
        return detect_followups.requoted_suggestions(conn, search)


# --------------------------------------------------------- the old rule --
def test_the_requesters_please_requote_suggests_nothing(world, tmp_path):
    conn, repo, case = world
    assert found(conn, build_archive(tmp_path, thread())) == []
    # propose-status no longer reads it as a re-quote either
    their = propose.ThreadMail(at="2026-09-02", sender_name="Renata Solis", address=RENATA,
                               internal=False, subject="RE: OCEAN PRICING", body="Could you requote on 2 x 40HQ instead.",
                               from_requester=True)
    result = propose.classify([their], "Tolvex", requester_known=True)
    assert not propose.is_requote_note(result.note)
    assert not hasattr(propose, "REQUOTE_PHRASES")


# --------------------------------------------------------- the new rule --
def test_our_revised_rates_to_the_requester_give_one_requoted_suggestion(world, tmp_path):
    conn, repo, case = world
    archive = build_archive(tmp_path, thread(revised()))
    [r] = found(conn, archive)
    assert (r.opp_no, r.at, r.last_quoted, r.message_id) == ("OPP-1407", "2026-09-03", "2026-09-01", "<revised@x>")
    assert r.sender == "JJC-VICTOR LANG <{}>".format(VICTOR) and "USD 1,880" in r.quote
    made = detect_followups.record_requoted(repo, [r])
    assert made == {"written": 1, "same": 0}
    row = conn.execute("SELECT * FROM suggestion WHERE enquiry_id = ?", (case,)).fetchone()
    assert (row["state"], row["kind"], row["proposed_status"], row["proposed_date"], row["evidence_date"]) == \
        ("pending", "status", "Quoted", "2026-09-03", "2026-09-03")
    assert (row["message_id"], row["note"], row["source"]) == ("<revised@x>", detect_followups.REQUOTED_NOTE, "scan")


# test_accepting_it_writes_the_buttons_event: FX4 rewrote it with invented data in tests/test_fx4_rewritten.py
# (a question is Waiting on us only when logged by hand)


def test_a_chaser_repeating_the_old_rate_gives_none(world, tmp_path):
    conn, repo, case = world
    assert found(conn, build_archive(tmp_path, thread(revised(body=CHASER)))) == []


def test_a_rate_request_to_a_vendor_gives_none(world, tmp_path):
    conn, repo, case = world
    to_vendor = revised(body=ASK_VENDOR, to=("kyle@lanecarrier.example",))
    revised_to_vendor = revised(sent="2026-09-04 11:00", to=("kyle@lanecarrier.example",), mid="<v2@x>")
    assert found(conn, build_archive(tmp_path, thread(to_vendor, revised_to_vendor))) == []


def test_a_mail_on_the_day_of_the_quote_gives_none(world, tmp_path):
    conn, repo, case = world
    same_day = revised(sent="2026-09-01 17:30", mid="<corr@x>")
    assert found(conn, build_archive(tmp_path, thread(same_day))) == []


def test_two_mails_on_one_day_are_one_requote(world, tmp_path):
    conn, repo, case = world
    extra = revised(sent="2026-09-03 15:00", mid="<dest@x>",
                    body="Hi Renata,\n\nDestination handling charges: USD 385 per shipment.\n")
    [r] = found(conn, build_archive(tmp_path, thread(revised(), extra)))
    assert r.message_id == "<revised@x>"


def test_a_dismissed_mail_does_not_come_back(world, tmp_path):
    conn, repo, case = world
    archive = build_archive(tmp_path, thread(revised()))
    detect_followups.record_requoted(repo, found(conn, archive))
    sid = conn.execute("SELECT id FROM suggestion WHERE enquiry_id = ?", (case,)).fetchone()["id"]
    suggestions.dismiss(repo, sid)
    assert found(conn, archive) == []
    assert detect_followups.record_requoted(repo, [detect_followups.Requoted(
        case, "OPP-1407", "2026-09-03", "x", "y", "s", message_id="<revised@x>")]) == {"written": 0, "same": 1}
    assert conn.execute("SELECT COUNT(*) FROM suggestion WHERE enquiry_id = ?", (case,)).fetchone()[0] == 1


def test_a_own_office_requester_is_matched_by_its_code(tmp_path):
    path = tmp_path / "office.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    jjt = add_party(repo, "JoJo JJT", ptype="Office", country="China")
    case = add_enquiry(repo, "OPP-1408", jjt, received="2026-08-28", conversation_id="CONV-408",
                       source_subject="RE: AIR PRICING // ORD - GRU")
    repo.set_status(case, "Quoted", "2026-09-01")
    repo.commit()
    to_office = revised(to=("frank.zhou@jojofreight.example",), to_names=["JJT-FRANK ZHOU"], conversation="CONV-408")
    to_office = mail("RE: AIR PRICING // ORD - GRU", VICTOR, "JJC-VICTOR LANG", "2026-09-03 11:00", REVISED,
                     ["frank.zhou@jojofreight.example"], internal=True, mid="<jjt@x>", to_names=["JJT-FRANK ZHOU"],
                     conversation="CONV-408")
    other_office = mail("RE: AIR PRICING // ORD - GRU", VICTOR, "JJC-VICTOR LANG", "2026-09-04 11:00", REVISED,
                        ["ava@jojofreight.example"], internal=True, mid="<jjh@x>", to_names=["JJH-AVA WONG"],
                        conversation="CONV-408")
    [r] = found(conn, build_archive(tmp_path, [to_office, other_office]))
    assert (r.opp_no, r.at) == ("OPP-1408", "2026-09-03")
    conn.close()


# ------------------------------------------------------ the old suggestions --
def test_pending_old_suggestions_are_withdrawn_not_deleted(world):
    conn, repo, case = world
    old = repo.insert("suggestion", {"enquiry_id": case, "opp_no": "OPP-1407", "kind": "status",
                                     "proposed_status": "Quoted", "proposed_date": "2026-09-02",
                                     "note": propose.REQUOTE_NOTE, "state": "pending"})
    repo.commit()
    with pytest.raises(ValueError):
        suggestions.accept(repo, old)
    dry = withdraw_requote.run(conn, dry_run=True)
    assert [f[0] for f in dry.found] == [old] and dry.withdrawn == []
    done = withdraw_requote.run(conn, dry_run=False)
    assert done.withdrawn == [old]
    row = conn.execute("SELECT state, note FROM suggestion WHERE id = ?", (old,)).fetchone()
    assert row["state"] == "dismissed"
    assert row["note"] == propose.REQUOTE_NOTE + " | " + withdraw_requote.WITHDRAWN_NOTE
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'suggestion' AND row_id = ?",
                        (old,)).fetchone()[0] >= 2
    assert withdraw_requote.run(conn, dry_run=False).found == []


# -------------------------------------- an amount on its own line (2026-09-25) --
BARE_AMOUNT = ("Hi Renata,\n\nFind below the air figures from our partner:\nOrigin: IAH\nDestination: LIM\nNet:\n"
               "Usd2140\nUSD95\n0.42 X KG MIN USD310\n\nFrom: Renata Solis\nSent: Wednesday\n\nCould you requote")
TARGET_ONLY = ("Hi Renata,\n\nFor reference, the budget price we can work with is USD 1,720 per 40HQ.\n\n"
               "From: Renata Solis\nSent: Wednesday\n\nCould you requote")


def test_a_currency_amount_on_its_own_line_counts(world, tmp_path):
    """An agent's figures passed on as they came: the amounts sit alone on their lines."""
    conn, repo, case = world
    [r] = found(conn, build_archive(tmp_path, thread(revised(body=BARE_AMOUNT))))
    assert (r.at, r.quote) == ("2026-09-03", "Usd2140")


def test_a_target_or_budget_amount_still_does_not(world, tmp_path):
    conn, repo, case = world
    assert found(conn, build_archive(tmp_path, thread(revised(body=TARGET_ONLY)))) == []
