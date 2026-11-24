"""Batch FX4 (2026-11-12): older tests that FX4 changed, rewritten
with invented data on the new behaviour. Their old versions were
deleted from test_detect_followups.py and test_followup.py.

Every company, person, mail, place and date here is invented; cases from
OPP-9995.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, detect_followups, tracking
from opplist.web import create_app

QUESTION = "Could you redo the rate for 3 x 40HQ to Puerto Lidra?"


@pytest.fixture
def waiting(tmp_path):
    """A quoted case whose latest event is a question logged by hand
    (Waiting on us, the case page's note, source manual)."""
    path = tmp_path / "waiting.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tamsel Freight", country="Chile")
    case = add_enquiry(repo, "OPP-9995", party, received="2026-07-01", origin_loc="LAX",
                       origin_country="USA", dest_loc="SAI", dest_country="Chile")
    repo.set_status(case, "Quoted", "2026-07-10")
    repo.add_note(case, "2026-07-20", detect_followups.reply_note("Orla Venn", QUESTION, kind="asks"),
                  source="manual")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, case


def card_of(body):
    """The Waiting on us card's part of the Tracking page."""
    start = body.index('id="replied"')
    return body[start:body.index("</section>", start)]


# replaces test_detect_followups.test_a_replied_row_offers_re_quote
def test_a_waiting_on_us_row_offers_re_quote(waiting):
    client, path, case = waiting
    body = client.get("/tracking").data.decode("utf-8")
    assert 'class="pill p-rose">Waiting on us</span>' in body
    assert "Re-quote" in body and 'value="quick:requote"' in body
    replied = card_of(body)
    assert "OPP-9995" in replied
    assert "Draft FU" not in body and "Log FU" not in replied            # they asked us; nothing to chase
    assert "Orla Venn: Could you redo the rate" in body                  # the gist


# replaces test_detect_followups.test_re_quoted_records_the_quote_and_takes_the_row_off_the_list
def test_re_quoted_records_the_quote_and_takes_the_row_off_the_list(waiting):
    client, path, case = waiting
    body = client.get("/tracking").data.decode("utf-8")
    assert "OPP-9995" in card_of(body)                                          # on the card first
    page = client.post("/tracking/act", data={
        "do": "quick:requote", "rule": "R3", "kind": "enquiry", "ref_id": case,
        "enquiry_id": case, "at": "2026-07-25"}, follow_redirects=True)
    assert page.status_code == 200 and b"OPP-9995 re-quoted on 2026-07-25" in page.data
    conn = db.open_db(path)
    row = conn.execute("SELECT status, status_confirmed FROM enquiry WHERE id = ?", (case,)).fetchone()
    assert (row["status"], row["status_confirmed"]) == ("Quoted", 1)
    event = conn.execute("SELECT at, status, kind FROM status_event WHERE enquiry_id = ? "
                         "ORDER BY id DESC LIMIT 1", (case,)).fetchone()
    assert tuple(event) == ("2026-07-25", "Quoted", "requote")
    conn.close()
    # off the card: the clock restarted at the re-quote
    body = client.get("/tracking").data.decode("utf-8")
    assert "OPP-9995" not in card_of(body)


# replaces test_detect_followups.test_a_reply_that_asks_is_on_the_list_the_next_day
def test_a_scan_reply_that_asks_never_lists_and_a_hand_logged_one_lists_the_same_day(repo, conn):
    seed_reference(repo)
    party = add_party(repo, "Tamsel Freight", country="Chile")
    scanned = add_enquiry(repo, "OPP-9996", party, received="2026-08-01")
    repo.set_status(scanned, "Quoted", "2026-09-01")
    repo.add_note(scanned, "2026-09-15", detect_followups.reply_note(
        "Orla", "Our competitor is cheaper, can you match?", "<r1@x>", kind="asks"), source="mail")
    by_hand = add_enquiry(repo, "OPP-9997", party, received="2026-08-01")
    repo.set_status(by_hand, "Quoted", "2026-09-01")
    repo.add_note(by_hand, "2026-09-15", detect_followups.reply_note(
        "Orla", "Our competitor is cheaper, can you match?", kind="asks"), source="manual")
    repo.commit()

    def replied(today):
        r3 = {s.rule: s for s in tracking.run(conn, today=today)}["R3"]
        return {i.label: i for i in r3.items if i.extra.get("step") == "Replied"}

    same_day, next_day = replied("2026-09-15"), replied("2026-09-16")
    assert "OPP-9996" not in same_day and "OPP-9996" not in next_day    # the scan's never asks
    assert "OPP-9997" in same_day                                        # by hand: ours at once
    item = next_day["OPP-9997"]
    assert (item.since, item.days) == ("2026-09-15", 1)
    assert item.extra["reply_note"] == "Orla: Our competitor is cheaper, can you match?"


# replaces test_followup.test_the_case_page_counts_the_round_and_names_the_total
def test_the_case_page_counts_the_round_and_names_the_total(tmp_path):
    path = tmp_path / "fu.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tamsel Freight", country="Chile")
    case = add_enquiry(repo, "OPP-9998", party, received="2026-09-01", origin_loc="LAX", origin_country="USA",
                       dest_loc="SAI", dest_country="Chile", service_mode="FCL", container_type="40HQ",
                       container_qty=2)
    repo.set_status(case, "Quoted", "2026-09-10")
    repo.add_followup(case, "2026-09-11")
    repo.set_status(case, "Quoted", "2026-09-14")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    body = app.test_client().get("/enquiries/{}".format(case)).data.decode("utf-8")
    log = body[body.index('id="fu-log"'):]
    assert log.index('<span class="k">FU1</span>') < log.index("</form>")      # back to FU1
    assert "1 follow-ups in total on this case" in body


# ------------------------------------- the FU templates (test_fu_templates_and_request_card) --
@pytest.fixture
def templates(tmp_path, monkeypatch):
    from opplist import web as web_module
    path = tmp_path / "tpl.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tamsel Freight", country="Chile")
    case = add_enquiry(repo, "OPP-9999", party, received="2026-09-21", service_mode="Air", origin_loc="MIA",
                       origin_country="USA", dest_loc="SAI", dest_country="Chile")
    repo.set_status(case, "Quoted", "2026-09-23")
    repo.commit()
    conn.close()
    monkeypatch.setattr(web_module, "_today", lambda: "2026-09-24")
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path


def _stored(path, key):
    conn = db.open_db(path)
    try:
        return db.get_setting(conn, key, "")
    finally:
        conn.close()


def _counts(path):
    from opplist import followup
    conn = db.open_db(path)
    try:
        return {k: len(followup.versions(conn, int(k[-1]))) for k in POOL}
    finally:
        conn.close()


def _pool():
    from opplist import followup, schema_migrations
    return {k: len([p for p in followup.VERSION_SEPARATOR.split(v) if p.strip()])
            for k, v in schema_migrations.FU_SETTINGS if k.startswith("fu_template_")}


POOL = _pool()


# replaces test_fu_templates_and_request_card.test_saving_settings_untouched_keeps_every_version
def test_settings_has_no_template_boxes_and_a_save_keeps_every_version(templates):
    client, path = templates
    page = client.get("/settings").data.decode("utf-8")
    assert not any('name="{}"'.format(k) in page for k in POOL)
    before = [_stored(path, k) for k in POOL]
    client.post("/settings", data={"owners": "Ana\r\nBen"}, follow_redirects=True)
    assert _counts(path) == POOL
    assert [_stored(path, k) for k in POOL] == before                # untouched


# replaces test_fu_templates_and_request_card.test_an_old_default_with_crlf_is_recognised
def test_an_old_default_with_crlf_is_recognised(templates):
    from opplist import followup
    client, path = templates
    conn = db.open_db(path)
    for key, olds in followup.OLD_FU_TEMPLATES.items():
        db.set_setting(conn, key, olds[0].replace("\n", "\r\n") + "  \r\n")
    conn.commit()
    assert {k: len(followup.versions(conn, int(k[-1]))) for k in POOL} == POOL
    assert followup.is_old_default("fu_template_1", followup.OLD_FU_TEMPLATES["fu_template_1"][0].replace("\n", "\r"))
    conn.close()
    # FX4: no Settings box shows it any more
    assert 'name="fu_template_1"' not in client.get("/settings").data.decode("utf-8")


# replaces test_fu_templates_and_request_card.test_her_own_template_is_kept_with_lf
def test_a_posted_template_is_not_saved_any_more(templates):
    client, path = templates
    before = _stored(path, "fu_template_1")
    mine = "Hi {first_name},\r\n\r\nAny news on {rates}?  \r\nThank you."
    client.post("/settings", data={"fu_template_1": mine}, follow_redirects=True)
    assert _stored(path, "fu_template_1") == before and _counts(path) == POOL


# replaces test_fu_templates_and_request_card.test_an_unknown_blank_is_refused
def test_an_unknown_blank_no_longer_blocks_the_save(templates):
    client, path = templates
    before = _stored(path, "fu_template_2")
    body = client.post("/settings", data={"fu_template_2": "Hi {first_name},\nThe quote of {quote_date}.",
                                          "export_share_dir": "X:\share"}, follow_redirects=True).data.decode("utf-8")
    assert "not saved: it uses" not in body
    assert _stored(path, "fu_template_2") == before and _stored(path, "export_share_dir") == "X:\share"


# ---------------------------------- a Re-quoted suggestion (test_requoted_suggestions) --
DESK = "kel.marsh@jojofreight.example"
CLIENT = "marta@hollowayfreight.test"


def _rq_mail(subject, sender, name, sent, body, to, internal=False, mid=""):
    from opplist.outlook_bridge import MailInfo
    return MailInfo(subject=subject, received_on=sent[:10], received_at=sent, sent_at=sent,
                    entry_id="E-" + sent, conversation_id="CONV-9990", conversation_topic=subject.replace("RE: ", ""),
                    sender_name=name, sender_address=sender, sender_internal=internal, to_addresses=list(to),
                    cc_addresses=[], to_names=[], display_to="; ".join(to), body_excerpt=body, folder_path="\X",
                    message_id=mid or "<{}@x>".format(sent.replace(" ", "T")))


# replaces test_requoted_suggestions.test_accepting_it_writes_the_buttons_event
def test_accepting_a_requoted_suggestion_writes_the_buttons_event_and_clears_waiting_on_us(tmp_path):
    from opplist import mail_archive, suggestions
    conn = db.open_db(tmp_path / "rq.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    holloway = add_party(repo, "Holloway Freight", country="Chile")
    repo.insert("party_domain", {"domain": "hollowayfreight.test", "party_id": holloway})
    case = add_enquiry(repo, "OPP-9990", holloway, received="2026-08-28", conversation_id="CONV-9990",
                       source_subject="RE: RATE REQUEST // MIA - SAI", origin_loc="MIA",
                       origin_country="USA", dest_loc="SAI", dest_country="Chile")
    repo.set_status(case, "Quoted", "2026-09-01")
    # FX4: she logged their question by hand (Waiting on us)
    repo.add_note(case, "2026-09-02", detect_followups.reply_note(
        "Marta Lund", "Can you price 2 x 20GP instead of the 40HQ?", kind="asks"), source="manual")
    repo.commit()
    assert {i.label: i for i in tracking.rule_quoted_no_answer(conn, "2026-09-03")}["OPP-9990"].extra["step"] \
        == "Replied"
    subject = "RE: RATE REQUEST // MIA - SAI"
    mails = [
        _rq_mail("RATE REQUEST // MIA - SAI", CLIENT, "Marta Lund", "2026-08-28 09:00", "please quote 1 x 40HQ",
                 [DESK]),
        _rq_mail(subject, DESK, "JJC-KEL MARSH", "2026-09-01 15:00",
                 "Hi Marta,\n\nOur offer for the 40HQ:\nO/F USD 1,640 per 40HQ\nTHC USD 280\n\nKel", [CLIENT],
                 internal=True, mid="<first@x>"),
        _rq_mail(subject, DESK, "JJC-KEL MARSH", "2026-09-03 11:00",
                 "Hi Marta,\n\nPlease see below the revised option, 2 x 20GP:\nO/F USD 1,020 per 20GP\nTHC USD 280\n\n"
                 "Kel", [CLIENT], internal=True, mid="<second@x>"),
    ]
    path = tmp_path / "mail.db"
    archive = mail_archive.open_archive(path)
    mail_archive.store(archive, [mail_archive.record_from_info(m, sent_at=m.sent_at + ":00") for m in mails])
    archive.commit()
    archive.close()
    with mail_archive.ArchiveSearch(path) as search:
        found = detect_followups.requoted_suggestions(conn, search)
    assert [(r.opp_no, r.at, r.message_id) for r in found] == [("OPP-9990", "2026-09-03", "<second@x>")]
    detect_followups.record_requoted(repo, found)
    sid = conn.execute("SELECT id FROM suggestion WHERE enquiry_id = ?", (case,)).fetchone()["id"]
    suggestions.accept(repo, sid)
    event = conn.execute("SELECT at, status, kind, reason, source FROM status_event WHERE enquiry_id = ? "
                         "ORDER BY id DESC LIMIT 1", (case,)).fetchone()
    assert tuple(event) == ("2026-09-03", "Quoted", "requote", None, "mail")
    row = conn.execute("SELECT status, quote_rounds, in_requote FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    assert tuple(row) == ("Quoted", 2, 0)
    assert tracking.rule_quoted_no_answer(conn, "2026-09-03") == []        # off Waiting on us
    [item] = tracking.rule_quoted_no_answer(conn, "2026-09-04")             # Thursday: FU1 due
    assert (item.extra["step"], item.since, item.days) == ("FU1", "2026-09-03", 1)
    conn.close()


# replaces test_retire_sourcing.test_tracking_cards_have_a_plain_border_and_title_case_titles
def test_tracking_cards_have_a_plain_border_and_title_case_titles():
    import re
    from pathlib import Path
    css = (Path(tracking.__file__).parent / "web" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".tracking-page .card, .tracking-page .tracking-section.focus, " \
           ".tracking-page .tracking-section.other { border: 1px solid var(--line); }" in css
    assert re.search(r"\.tracking-page \.card h2 \{[^}]*text-transform: none", css)
    titles = [c[3] for c in tracking.TODAY_CARDS]
    assert "Waiting on us" in titles and "Replied, they asked us something" not in titles


# replaces test_tracking.test_late_is_three_working_days_for_r1_and_fourteen_days_for_r3
def test_late_is_three_working_days_for_r1_and_fourteen_days_for_r3(repo, conn):
    seed_reference(repo)
    party = add_party(repo, "Tamsel Freight", country="Chile")
    add_enquiry(repo, "OPP-9981", party, received="2026-09-04")   # Friday: 3 working days
    add_enquiry(repo, "OPP-9982", party, received="2026-09-07")   # Monday: 2
    q1 = add_enquiry(repo, "OPP-9983", party, received="2026-08-01")
    repo.set_status(q1, "Quoted", "2026-08-20")                    # 14 working days: Decide
    q2 = add_enquiry(repo, "OPP-9984", party, received="2026-08-01")
    repo.set_status(q2, "Quoted", "2026-08-31")                    # 7: FU1
    repo.commit()
    found = {s.rule: s for s in tracking.run(conn, today="2026-09-09")}
    assert {i.label: i.late for i in found["R1"].items} == {"OPP-9981": True, "OPP-9982": False}
    assert {i.label: i.late for i in found["R3"].items} == {"OPP-9983": True, "OPP-9984": False}
    # FX4: the Decide card is hidden by default and the badge leaves its row out
    assert tracking.focus_count(conn, today="2026-09-09") == 3
    db.set_setting(conn, tracking.HIDDEN_SETTING, "r9,r2,r6")
    conn.commit()
    assert tracking.focus_count(conn, today="2026-09-09") == 4


# replaces test_web.test_logging_a_vendor_reply_from_the_list
def test_logging_a_vendor_reply_from_the_list(tmp_path, monkeypatch):
    from opplist import web as web_module
    path = tmp_path / "r2.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tamsel Freight", country="Chile")
    vendor = add_party(repo, "Corrib Lines", ptype="Carrier", country="USA")
    case = add_enquiry(repo, "OPP-9985", party, received="2026-08-04")
    vendor_request_id = repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": vendor,
                                                       "asked_on": "2026-08-05"})
    repo.commit()
    conn.close()
    monkeypatch.setattr(web_module, "_today", lambda: "2026-09-09")
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    body = client.get("/tracking").data.decode("utf-8")
    # FX4: the R2 card is hidden by default, named in the line under Today's work
    assert 'id="r2"' not in body and "Vendor has not replied 1" in body
    conn = db.open_db(path)
    db.set_setting(conn, tracking.HIDDEN_SETTING, "r9,decide,r6")
    conn.commit()
    conn.close()
    body = client.get("/tracking").data.decode("utf-8")
    assert 'id="r2"' in body and "has not replied" in body
    client.post("/tracking/act", data={
        "do": "vendor_replied", "rule": "R2", "kind": "vendor_request",
        "ref_id": vendor_request_id, "enquiry_id": case,
        "at": "2026-09-09", "quoted": "1", "note": "USD 2,300"},
        follow_redirects=True)
    conn = db.open_db(path)
    row = conn.execute("SELECT replied_on, quoted, notes FROM vendor_request WHERE id = ?",
                       (vendor_request_id,)).fetchone()
    assert (row["replied_on"], row["quoted"]) == ("2026-09-09", 1)
    assert row["notes"] == "USD 2,300"
    conn.close()
