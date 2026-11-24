"""Follow-ups and replies read out of the archive (second batch):
detection, the Replied step, old quotes straight to Decide, the batch Lost
button, and the two small text fixes."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, detect_followups, followup, mail_archive, tracking
from opplist.outlook_bridge import MailInfo

TODAY = "2026-09-16"          # Wednesday


def mail(subject, sender, name, sent, body, conversation="CONV-407", internal=False,
         entry="", mid=""):
    return MailInfo(subject=subject, received_on=sent[:10], received_at=sent, sent_at=sent,
                    entry_id=entry or "E-" + sent, conversation_id=conversation,
                    conversation_topic=subject.replace("RE: ", ""), sender_name=name,
                    sender_address=sender, sender_internal=internal,
                    to_addresses=["VictorLang@jojofreight.example"], cc_addresses=[],
                    body_excerpt=body, folder_path="\\X",
                    message_id=mid or "<{}@x>".format(sent.replace(" ", "T")))


QUOTE = ("Hi Renata,\n\nPlease find our rates below.\nO/F USD 1,250 per 40HQ\nTHC USD 300\n\n"
         "From: Renata Solis\nSent: Monday\n\nPlease quote")
FU = ("Hi Renata,\n\nChecking back about the offer of last week. Is it workable?\n\n"
      "From: JJC-VICTOR LANG\nSent: Tuesday\n\nO/F USD 1,250 per 40HQ")
REPLY = ("Hi Victor,\n\nStill checking with the client, will revert by Friday.\n\n"
         "From: JJC-VICTOR LANG\nSent: Wednesday\n\nJust checking back")


def build_archive(tmp_path, mails):
    path = tmp_path / "mail.db"
    archive = mail_archive.open_archive(path)
    mail_archive.store(archive, [mail_archive.record_from_info(m, sent_at=m.sent_at + ":00")
                                 for m in mails])
    archive.commit()
    archive.close()
    return path


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "det.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    case = add_enquiry(repo, "OPP-1407", tolvex, received="2026-08-28", conversation_id="CONV-407",
                       source_subject="RE: RATE REQUEST // LAX - SAI", origin_loc="LAX",
                       origin_country="USA", dest_loc="SAI", dest_country="Chile")
    repo.set_status(case, "Quoted", "2026-09-01")
    repo.commit()
    conn.close()
    return path, case


def events(path, case):
    conn = db.open_db(path)
    rows = conn.execute("SELECT at, kind, seq, channel, source, note FROM status_event "
                        "WHERE enquiry_id = ? AND voided = 0 ORDER BY at, id", (case,)).fetchall()
    conn.close()
    return [tuple(r) for r in rows]


# T4 (2026-10-29): the follow-up and reply tests that sent our mails to an
# internal To line only were dropped; invented replacements are in
# tests/test_to_requester_line.py (a follow-up and the reply line are read
# only from our mails to the requester).


def test_is_followup_text():
    assert detect_followups.is_followup_text("RE: x", "Checking back about the offer.") is True
    assert detect_followups.is_followup_text("RE: x", "Any feedback from your client?") is True
    assert detect_followups.is_followup_text("RE: x", "We look forward to your approval.") is True
    assert detect_followups.is_followup_text("RE: x", "Following up: O/F USD 1,250 per 40HQ") is False
    assert detect_followups.is_followup_text("RE: x", "Please find our rates attached, any feedback welcome") is False
    assert detect_followups.is_followup_text("RE: x", "Booking confirmed, see below.") is False


# ------------------------------------------------ old quotes go to Decide --
def test_an_old_quote_nobody_chased_is_a_decision(repo, conn):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    old = add_enquiry(repo, "OPP-0001", party, received="2026-07-01")
    repo.set_status(old, "Quoted", "2026-08-01")                   # 11 working days is the ladder (1+5+5)
    fresh = add_enquiry(repo, "OPP-0002", party, received="2026-09-01")
    repo.set_status(fresh, "Quoted", "2026-09-10")
    repo.commit()
    found = {i.label: i.extra["step"] for i in
             {s.rule: s for s in tracking.run(conn, today=TODAY)}["R3"].items}
    assert found == {"OPP-0001": "Decide", "OPP-0002": "FU1"}
    assert tracking.fu_step(0, 10, tracking.FU_DEFAULTS) == "FU1"
    assert tracking.fu_step(0, 11, tracking.FU_DEFAULTS) == "Decide"


# ------------------------------------------------------- the batch button --
@pytest.fixture
def web(tmp_path):
    from opplist.web import create_app

    path = tmp_path / "decide.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    ids = {}
    for n, quoted in ((1, "2026-07-01"), (2, "2026-07-02"), (3, "2026-09-14")):
        case = add_enquiry(repo, "OPP-000{}".format(n), party, received="2026-06-01")
        repo.set_status(case, "Quoted", quoted)
        ids[n] = case
    db.set_setting(conn, "closed_reasons_lost",
               ["Price too high", "No Response from Customer", "Other"])
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, ids


# test_decide_rows_have_a_checkbox_and_the_batch_form: replaced in L2 by tests/test_fu1_wait_rewrites.py with invented data (no FU2 without an answer to FU1)


# test_the_batch_marks_the_ticked_cases_lost_with_the_existing_reason_spelling: replaced in L2 by tests/test_fu1_wait_rewrites.py with invented data (no FU2 without an answer to FU1)


# test_the_reason_is_added_when_the_settings_lack_it: replaced in L2 by tests/test_fu1_wait_rewrites.py with invented data (no FU2 without an answer to FU1)


# ------------------------------------------------------ the two text fixes --
def test_the_name_falls_back_to_the_primary_contact(repo, conn):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    repo.insert("contact", {"party_id": party, "name": "Carlos Perez", "email": "c@tolvex.example"})
    repo.insert("contact", {"party_id": party, "name": "Renata Solis", "email": "p@tolvex.example",
                            "is_primary": 1})
    case = add_enquiry(repo, "OPP-1407", party, received="2026-08-28")
    repo.set_status(case, "Quoted", "2026-09-01")
    repo.commit()
    text = followup.build(conn, case, 1, sources=followup.Sources("", None))
    assert text.startswith("Hi Renata,")
    assert followup.contact_first_name(conn, party) == "Renata"
    assert followup.contact_first_name(conn, None) == ""


def test_the_rates_phrase_names_the_mode_and_nothing_names_the_lane(repo, conn):
    """{rates} by mode; a second quoted case for the requester changes
    nothing any more ({which} is gone)."""
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1407", party, received="2026-08-28", origin_loc="LAX",
                       origin_country="USA", dest_loc="SAI", dest_country="Chile", service_mode="Air")
    repo.set_status(case, "Quoted", "2026-09-01")
    other = add_enquiry(repo, "OPP-1408", party, received="2026-08-29", origin_country="USA",
                        dest_country="Chile")
    repo.set_status(other, "Quoted", "2026-09-02")
    repo.commit()
    text = followup.build(conn, case, 1, variant=1, sources=followup.Sources("Ana", None))
    assert "Are the air rates below workable" in text and "Los Angeles" not in text
    text = followup.build(conn, other, 1, variant=1, sources=followup.Sources("Ana", None))
    assert "Are the rates below workable" in text and "USA" not in text


def test_a_counter_offer_needs_an_answer():
    """Their own selling rate, or 'refer to the below', is a target we have
    to answer (2026-09-21): fixtures/reply_counter_offer_selling_rate.json."""
    import json

    from opplist import paths

    with open(paths.FIXTURES_DIR / "reply_counter_offer_selling_rate.json",
              encoding="utf-8") as handle:
        data = json.load(handle)
    assert detect_followups.reply_kind(data["body"]) == data["expected"]["reply_kind"] == "asks"
    for text in ("Please see our selling rate as below.",
                 "Kindly refer to below and advise.",
                 "Pls refer to the below selling.",
                 "Please check the below and revert."):
        assert detect_followups.reply_kind(text) == "asks", text
    for text in ("Noted, we are selling this to the client now.",
                 "Thanks, will refer to it later."):
        assert detect_followups.reply_kind(text) == "noted", text


# Replied rows offer Re-quote, and a reply that asks is on the list: FX4 rewrote them
# with invented data on the new behaviour in tests/test_fx4_rewritten.py
# ------------------------------- a reply that asks, and one that only says --
def test_the_six_shapes_a_reply_comes_in():
    asks = ("Hi Victor, our competitor is cheaper by 300 usd, can you match?",
            "Please advise TT & FT for this shipment.",
            "What is the transit time via Busan?",
            "Your rate is too high for this trade lane.",
            "Could you reduce the THC?",
            "We have a target rate of USD 1,800 for this lane.")
    noted = ("Thanks Victor, I will send it over to my client.",
             "I will do my best and revert to you.",
             "Noted with thanks.",
             "Still checking with the client, will revert by Friday.",
             "Received, thank you.",
             "Understood. I will keep you posted.")
    for text in asks:
        assert detect_followups.reply_kind(text) == "asks", text
    for text in noted:
        assert detect_followups.reply_kind(text) == "noted", text


def test_the_note_carries_the_kind_and_the_kind_is_read_back():
    note = detect_followups.reply_note("Renata Solis", "Can you update the rate to 3 x 40HQ?", "<r@x>")
    assert note == "Reply from Renata Solis (needs an answer): Can you update the rate to 3 x 40HQ? [mid:<r@x>]"
    assert detect_followups.note_kind(note) == "asks"
    quiet = detect_followups.reply_note("Ana", "Noted with thanks.", "<q@x>")
    assert quiet == "Reply from Ana (noted): Noted with thanks. [mid:<q@x>]"
    assert detect_followups.note_kind(quiet) == "noted"
    # a note written before the marks existed is read by its words
    assert detect_followups.note_kind("Reply from Ana: can you redo it? [mid:<x>]") == "asks"
    assert detect_followups.note_kind("Reply from Ana: noted, thanks [mid:<x>]") == "noted"
    assert detect_followups.note_kind("FU detected: RE: something") == ""


def test_a_reply_that_only_says_thanks_moves_the_clock_and_nothing_else(repo, conn):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1602", party, received="2026-08-01")
    repo.set_status(case, "Quoted", "2026-09-01")
    repo.add_note(case, "2026-09-15", detect_followups.reply_note(
        "Renata", "Thanks, I will send it over to my client.", "<r2@x>"), source="mail")
    repo.commit()
    # the clock restarted at the reply, so the ladder starts again from there
    day_after = {s.rule: s for s in tracking.run(conn, today="2026-09-16")}["R3"]
    item = day_after.items[0]
    assert item.extra["step"] == "FU1" and item.since == "2026-09-15"
    assert "reply_note" not in item.extra
    assert "Replied" not in [i.extra["step"] for i in day_after.items]


# --------------------------------------------- reading the old notes again --
def test_reclassify_marks_the_notes_that_were_written_before_the_marks(repo, conn):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1603", party, received="2026-08-01")
    repo.set_status(case, "Quoted", "2026-09-01")
    repo.add_note(case, "2026-09-10", "Reply from Ana: can you redo it for 2 x 40HQ? [mid:<a@x>]", source="mail")
    repo.add_note(case, "2026-09-11", "Reply from Ben: noted with thanks [mid:<b@x>]", source="mail")
    repo.add_note(case, "2026-09-12", "Reply from Cara (needs an answer): what is the ETD? [mid:<c@x>]", source="mail")
    repo.add_note(case, "2026-09-13", "chased by phone", source="manual")
    repo.commit()

    dry = detect_followups.reclassify(conn)
    assert (dry.asks, dry.noted, dry.rewritten) == (2, 1, 2)      # the plain note is not a reply
    assert "dry run: nothing was written" in "\n".join(dry.summary())
    assert conn.execute("SELECT note FROM status_event WHERE at = '2026-09-10'").fetchone()[0] == \
        "Reply from Ana: can you redo it for 2 x 40HQ? [mid:<a@x>]"

    report = detect_followups.reclassify(conn, dry_run=False)
    assert (report.asks, report.noted, report.rewritten) == (2, 1, 2)
    notes = [r[0] for r in conn.execute(
        "SELECT note FROM status_event WHERE kind = 'note' ORDER BY at")]
    assert notes[0] == "Reply from Ana (needs an answer): can you redo it for 2 x 40HQ? [mid:<a@x>]"
    assert notes[1] == "Reply from Ben (noted): noted with thanks [mid:<b@x>]"
    assert notes[2] == "Reply from Cara (needs an answer): what is the ETD? [mid:<c@x>]"   # already right
    assert notes[3] == "chased by phone"                          # left alone
    # the words are never changed, only the mark, and it is logged
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'status_event' "
                        "AND field = 'note'").fetchone()[0] == 2
    # running it again changes nothing
    again = detect_followups.reclassify(conn, dry_run=False)
    assert again.rewritten == 0 and (again.asks, again.noted) == (2, 1)


def test_the_command_reclassifies_without_reading_the_archive(tmp_path, capsys):
    from opplist import db as db_mod
    from opplist.__main__ import main

    path = tmp_path / "re.db"
    conn = db_mod.open_db(path)
    repo = db_mod.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1604", party, received="2026-08-01")
    repo.set_status(case, "Quoted", "2026-09-01")
    repo.add_note(case, "2026-09-10", "Reply from Ana: any chance of a better rate? [mid:<a@x>]", source="mail")
    repo.commit()
    conn.close()

    assert main(["detect-followups", "--db", str(path), "--reclassify", "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "needs an answer       : 1" in printed and "dry run" in printed
    assert main(["detect-followups", "--db", str(path), "--reclassify"]) == 0
    assert "marks written into the note : 1" in capsys.readouterr().out
    conn = db_mod.open_db(path)
    assert conn.execute("SELECT note FROM status_event WHERE at = '2026-09-10'").fetchone()[0] == \
        "Reply from Ana (needs an answer): any chance of a better rate? [mid:<a@x>]"
    conn.close()


# --------------------------------------- one of our offices as the requester --
# 2026-09-30: every value here is invented (office QVT, OPP-2101, the people,
# the lane, the dates). The requester office's people write from jojofreight.example
# like we do; their 'following up' is the requester writing, not our follow-up.
OFFICE_QUOTE = "Hi Mei,\n\nPlease find our rates below.\nO/F USD 1,480 per 40HQ\n\nFrom: QVT-MEI LAN\nSent: Monday"
OFFICE_REPLY = ("Following up on this one: I will discuss the case with the factory next week and revert. "
                "Please also advise the charges for carrier ABC.\n\nFrom: JJC-DANA PERL\nSent: Monday")


def test_mail_from_the_requesting_office_is_their_reply_not_our_follow_up(tmp_path):
    path = tmp_path / "office.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    office = add_party(repo, "JoJo QVT", ptype="Office", country="China")
    case = add_enquiry(repo, "OPP-2101", office, received="2026-10-01", conversation_id="CONV-2101",
                       source_subject="DAP from Portvale to Lakeport", origin_loc="LAX", origin_country="USA",
                       dest_loc="SAI", dest_country="Chile")
    repo.set_status(case, "Quoted", "2026-10-05")
    repo.commit()
    subject = "DAP from Portvale to Lakeport"
    archive = build_archive(tmp_path, [
        mail("RE: " + subject, "DanaPerl@jojofreight.example", "JJC-DANA PERL", "2026-10-05 10:00", OFFICE_QUOTE,
             conversation="CONV-2101", internal=True, mid="<q2101@x>"),
        mail("\u56de\u590d: RE: " + subject, "MeiLan@jojofreight.example", "QVT-MEI LAN", "2026-10-08 21:30",
             OFFICE_REPLY, conversation="CONV-2101", internal=True, mid="<r2101@x>"),
    ])
    with mail_archive.ArchiveSearch(archive) as search:
        report = detect_followups.run(conn, search, opp_nos=["OPP-2101"])
    assert (report.followups, report.replies) == (0, 1)
    assert [e for e in events(path, case) if e[1] == "followup"] == []
    assert events(path, case)[-1] == (
        "2026-10-08", "note", None, None, "mail",
        # FX4: the scan writes every reply as noted ('Please also advise' asks, but only a
        # question logged by hand puts the case on Pending on us)
        "Reply from QVT-MEI LAN (noted): Following up on this one: I will discuss the case with the "
        "factory next week and revert. Please also advise the charges  [mid:<r2101@x>]")
    assert detect_followups.reply_kind(OFFICE_REPLY.split("\n\n")[0]) == "asks"
    conn.close()

