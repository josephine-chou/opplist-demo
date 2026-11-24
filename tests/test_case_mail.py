"""The case page's Mail card (2026-09-25): which archived
mails belong to the case, the tags on them, the reading pane, and the four
small fixes to the page from the batch-2 review."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference, form_nesting
from opplist import case_mail, db, followup, mail_archive, schema_migrations, suggestions
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app

SUBJECT = "PRICING NEEDED - 20' OPEN TOP / EXW DAYTON OH / PUMP HOUSINGS"
VENDOR_SUBJECT = "OCEAN QUOTE // MEMPHIS TN - SAN ANTONIO CL"


def info(subject, at, sender, name, body, conversation, to=("VictorLang@jojofreight.example",),
         internal=False, entry="", message_id="", attachments=(), cc=()):
    mail = MailInfo(
        subject=subject, received_on=at[:10], received_at=at,
        entry_id=entry or "EID-" + message_id.strip("<>"), conversation_id=conversation,
        conversation_topic=subject, sender_name=name, sender_address=sender,
        sender_internal=internal, to_addresses=list(to), cc_addresses=list(cc),
        body_excerpt=body, folder_path="\\Personal Folder\\AGENTS\\KESSLER",
        display_to="; ".join(to), message_id=message_id)
    mail.attachments = list(attachments)                  # record_from_info takes them apart
    return mail


AGENT = "m.fuentes@kesslermorrow.example"
FIVE = [
    info(SUBJECT, "2026-09-07 08:31", AGENT, "Martin Fuentes",
         "Dear Victor\n\nWishing you a good week\n\nA client of ours has a new shipment, details below.\n\n"
         "Best regards,\n\nMartin Fuentes\nKessler Morrow", "CONV-407", message_id="<m1@x>",
         entry="EID-REGISTERED", attachments=["image001.png"]),
    info("RE: " + SUBJECT, "2026-09-14 18:04", "VictorLang@jojofreight.example", "JJC-VICTOR LANG",
         "Hi Martin,\n\nOur offer for the open top is below, valid to month end:\n\n"
         "Ocean Freight : $2380.00/cntr\n\nThanks\n\nAtte.-\nVictor", "CONV-407",
         to=(AGENT,), internal=True, message_id="<m2@x>",
         attachments=["Rates.pdf", "image002.jpg"]),
    info("RE: " + SUBJECT, "2026-09-15 09:10", AGENT, "Martin Fuentes",
         "Hi Victor,\n\nCould you also price it as 2 x 40HQ?\n\nRegards,\nMartin", "CONV-407",
         message_id="<m3@x>"),
    info("RE: " + SUBJECT, "2026-09-16 12:46", "VictorLang@jojofreight.example", "JJC-VICTOR LANG",
         "Hi Martin,\n\nJust following up on the offer we sent. Any news from your client?\n\nKind regards,\nVictor",
         "CONV-407", to=(AGENT,), internal=True, message_id="<m4@x>"),
    info(VENDOR_SUBJECT, "2026-09-14 15:43", "VictorLang@jojofreight.example", "JJC-VICTOR LANG",
         "Hi Carla,\n\nKindly send us your pickup and ocean costs for the cargo below:\n\nBest Regards,\nVictor",
         "CONV-VENDOR", to=("carla@vantamulti.example",), internal=True, message_id="<v1@x>"),
    # the vendor's answer: their file number after a single slash, and their
    # own conversation id - the loose key match is what finds it
    info("RE: " + VENDOR_SUBJECT + " / QT8842M", "2026-09-14 17:46", "elena@vantamulti.example",
         "Elena Montero", "Dear Victor,\n\nOur costing is shown below:\n\nSaludos,\nElena",
         "CONV-CRAFT", message_id="<v2@x>"),
]


@pytest.fixture
def archive(tmp_path, monkeypatch):
    path = tmp_path / "archive" / "mail.db"
    path.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", path)
    conn = mail_archive.open_archive(path)
    mail_archive.store(conn, [mail_archive.record_from_info(
        m, has_attachments=bool(m.attachments), attachment_names=m.attachments) for m in FIVE])
    mail_archive.set_meta(conn, "last_sync_at", "2026-09-24 08:15:00")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def world(tmp_path, archive):
    """OPP-1407 as the live one is: no entry id, no conversation id, only the
    subject and its thread key - plus one vendor logged from Outlook."""
    path = tmp_path / "mail-web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    kessler = add_party(repo, "Kessler Morrow Seaway", country="Chile")
    repo.insert("party_domain", {"domain": "kesslermorrow.example", "party_id": kessler})
    craft = add_party(repo, "Vanta Multimodal", country="Brazil")
    from opplist import parsing
    case = add_enquiry(repo, "OPP-1407", kessler, received="2026-09-07", service_mode="FCL",
                       container_type="20OT", container_qty=1, origin_country="USA", origin_loc="MEM",
                       dest_country="Chile", dest_loc="SAI", source_subject=SUBJECT,
                       thread_key=parsing.subject_thread_key(SUBJECT))
    repo.set_status(case, "Quoted", "2026-09-14", note="Victor Lang <victorlang@jojofreight.example> "
                    "Our offer for the open top is below, valid to month end:", source="mail")
    repo.add_note(case, "2026-09-15", "Reply from Martin Fuentes (needs an answer): Could you also "
                  "price it as 2 x 40HQ? [mid:<m3@x>]", source="mail")
    repo.add_followup(case, "2026-09-16", note="FU detected: RE: PRICING NEEDED [mid:<m4@x>]", source="mail")
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": craft, "asked_on": "2026-09-14",
                                   "notes": "Logged from Outlook: " + VENDOR_SUBJECT,
                                   "thread_key": parsing.subject_thread_key(VENDOR_SUBJECT)})
    won = add_enquiry(repo, "OPP-1408", kessler, received="2026-09-01", service_mode="Air")
    repo.set_status(won, "Quoted", "2026-09-03")
    repo.set_status(won, "Won", "2026-09-10")
    repo.add_followup(won, "2026-09-05")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, case, won


# ---------------------------------------------------------- the lookup ---
def test_the_agent_thread_is_found_by_thread_key_and_the_vendor_by_its_own(world):
    client, path, case, won = world
    conn = db.open_db(path)
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    search, _ = case_mail.open_search()
    try:
        tabs = case_mail.tabs(conn, search, row)
    finally:
        search.__exit__(None, None, None)
    conn.close()
    assert [t["label"] for t in tabs] == ["Agent thread", "Vanta Multimodal"]
    assert [m.message_id for m in tabs[0]["mails"]] == ["<m1@x>", "<m2@x>", "<m3@x>", "<m4@x>"]
    assert [m.message_id for m in tabs[1]["mails"]] == ["<v1@x>", "<v2@x>"]
    assert case_mail.key_matches("OCEANQUOTEMEMPHISTNSANANTONIOCLQT8842M", {"OCEANQUOTEMEMPHISTNSANANTONIOCL"}, loose=True)
    assert not case_mail.key_matches("OCEANQUOTEMEMPHISTNSANANTONIOCLQT8842M", {"OCEANQUOTEMEMPHISTNSANANTONIOCL"})


def test_the_union_reads_entry_id_conversation_attached_threads_and_keys(world):
    client, path, case, won = world
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    other = add_enquiry(repo, "OPP-1500", 1, received="2026-09-20", source_subject="Something else",
                        entry_id="EID-REGISTERED")
    repo.attach_thread(other, "conversation", "CONV-VENDOR", "2026-09-25", note=VENDOR_SUBJECT)
    repo.commit()
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (other,)).fetchone()
    search, _ = case_mail.open_search()
    try:
        mails = case_mail.case_mails(conn, search, row)
        # the fallback: nothing identifies the case but a subject the archive knows
        plain = add_enquiry(repo, "OPP-1501", 1, received="2026-09-20", source_subject="Fwd: " + SUBJECT)
        repo.commit()
        fallback = case_mail.case_mails(conn, search, conn.execute(
            "SELECT * FROM v_enquiry WHERE id = ?", (plain,)).fetchone())
    finally:
        search.__exit__(None, None, None)
    conn.close()
    assert {m.message_id for m in mails} == {"<m1@x>", "<v1@x>"}       # registered + attached conversation
    assert {m.message_id for m in fallback} == {"<m1@x>", "<m2@x>", "<m3@x>", "<m4@x>"}


# ----------------------------------------------------------- the card -----
def body_of(client, case):
    response = client.get("/enquiries/{}".format(case))
    assert response.status_code == 200
    return response.data.decode("utf-8")


def test_the_card_renders_the_tabs_the_tags_and_the_reading_pane(world):
    client, path, case, won = world
    body = body_of(client, case)
    card = body[body.index('id="mail"'):body.index('class="bottombar"')]
    assert "<small>synced 2026-09-24 08:15</small>" in card and "not live Outlook" not in card
    assert 'data-tab="case">Agent thread <b>4</b>' in card
    assert "Vanta Multimodal <b>2</b>" in card
    assert "Mail" in body[body.index('<ol class="sections">'):body.index("</ol>")]
    # rows: newest first, our mails marked, names shortened, tags from the case
    rows = card[card.index('id="tab-case"'):card.index('id="tab-vendor-')]
    assert rows.index("Just following up on the offer we sent") < rows.index("Wishing you a good week")
    assert '<span class="m-from">Victor Lang</span>' in rows and "Martin Fuentes" in rows
    assert 'class="pill tag-fu">FU1</span>' in rows
    assert 'class="pill tag-note">reply</span>' in rows
    assert 'class="pill s-quoted">Quoted</span>' in rows
    assert "registered from" not in rows                     # the case has no entry id
    # the reading pane: signature cut, no inline image, the three buttons
    def pane_of(index):
        start = card.index('id="read-case-{}"'.format(index))
        return card[start:card.index('class="m-actions"', start)]
    pane = pane_of(3)                                          # the oldest: the registration mail
    assert "Wishing you a good week" in pane and "Best regards" not in pane and "Kessler Morrow" not in pane
    quote = pane_of(2)                                         # our quote
    assert "Ocean Freight : $2380.00/cntr" in quote and "Atte.-" not in quote and "Thanks" not in quote
    assert "Rates.pdf" in quote and "image002.jpg" not in quote
    buttons = card[card.index('id="read-case-2"'):card.index('id="read-case-3"')]   # its three forms
    assert 'name="entry_id" value="EID-m2@x"' in buttons
    assert 'name="conversation_id" value="CONV-407"' in buttons and 'name="message_id" value="&lt;m2@x&gt;"' in buttons
    deepest, ids = form_nesting(body)
    assert deepest <= 1, ids


def test_a_case_with_no_archive_says_so_in_one_line(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", tmp_path / "nowhere" / "mail.db")
    path = tmp_path / "plain.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-0001", party, received="2026-09-07")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    body = body_of(app.test_client(), case)
    card = body[body.index('id="mail"'):body.index('class="bottombar"')]
    assert "No mail to show: no mail archive yet" in card and 'class="tabs"' not in card


def test_the_buttons_post_to_the_existing_routes(world, monkeypatch):
    client, path, case, won = world
    from opplist import outlook_bridge
    opened = []
    monkeypatch.setattr(outlook_bridge, "open_item", lambda entry_id, **kw: opened.append(entry_id))
    client.post("/enquiries/{}/open-in-outlook".format(case), data={"entry_id": "EID-m2@x"})
    assert opened == ["EID-m2@x"]
    # 'Use as this case's thread' attaches the mail's conversation without Outlook
    page = client.post("/enquiries/{}/attach-thread".format(case), data={
        "conversation_id": "CONV-407", "subject": "RE: " + SUBJECT}, follow_redirects=True)
    assert b"Thread attached to OPP-1407" in page.data
    conn = db.open_db(path)
    assert conn.execute("SELECT value FROM enquiry_thread WHERE kind = 'conversation'").fetchone()[0] == "CONV-407"
    conn.close()
    # 'Log vendor request' from an archive row, with the thread columns set
    page = client.post("/enquiries/{}/vendors/from-outlook".format(case), data={"message_id": "<v1@x>"},
                       follow_redirects=True)
    assert page.status_code == 200
    conn = db.open_db(path)
    rows = conn.execute("SELECT p.key, v.conversation_id, v.thread_key FROM vendor_request v "
                        "JOIN party p ON p.id = v.vendor_party_id WHERE v.enquiry_id = ? ORDER BY v.id",
                        (case,)).fetchall()
    conn.close()
    assert rows[-1]["conversation_id"] == "CONV-VENDOR" and rows[-1]["thread_key"] == "OCEANQUOTEMEMPHISTNSANANTONIOCL"
    assert len(rows) == 2 and rows[-1]["key"].lower().startswith("vantamulti")


# ----------------------------------------------------- the small fixes ---
# test_the_requote_form_lives_under_set_status_and_the_header_keeps_one_button: replaced in 15a by tests/test_case_v3.py (case page v3).


def test_since_is_the_latest_status_event_not_the_last_note(world):
    client, path, case, won = world
    body = body_of(client, case)
    line = body[body.index('<div class="current-status">'):]
    assert '<span class="muted since">since 2026-09-14' in line      # the Quoted event, not the FU of the 16th


def test_a_closed_case_has_nothing_to_chase(world):
    client, path, case, won = world
    body = body_of(client, won)
    card = body[body.index('id="followups"'):body.index('id="mail"')]
    assert ">Closed Sep 10</p>" in card and "nothing to chase" not in card       # 2026-09-25
    assert 'class="ladder"' not in card and "not quoted yet" not in card
    assert 'id="fu-text"' not in card and "Log as sent" not in card
    assert "FU1" in card                                               # the list of sent ones stays


def test_the_follow_up_draft_greets_martin_by_name(world):
    """The archive knows the thread only by its subject, which carries an
    apostrophe; by_subject used to refuse it and the draft said 'Hi,'."""
    client, path, case, won = world
    conn = db.open_db(path)
    text = followup.build(conn, case, 2, variant=1)
    conn.close()
    assert text.startswith("Hi Martin,")
    search = mail_archive.ArchiveSearch()
    with search:
        assert len(search.by_subject(SUBJECT)) == 4


# ----------------------------------------------------------- migration ---
def test_v23_adds_the_columns_and_keys_the_logged_vendors_from_the_archive(world):
    client, path, case, won = world
    conn = db.open_db(path)
    conn.execute("UPDATE vendor_request SET thread_key = NULL, conversation_id = NULL")
    conn.commit()
    notes = schema_migrations._to_v23(conn)
    conn.commit()
    assert any("thread key from the archive: 1 of 1" in n for n in notes)
    row = conn.execute("SELECT thread_key, conversation_id FROM vendor_request").fetchone()
    assert tuple(row) == ("OCEANQUOTEMEMPHISTNSANANTONIOCL", "CONV-VENDOR")
    assert conn.execute("SELECT source FROM change_log WHERE table_name = 'vendor_request' "
                        "AND field = 'thread_key'").fetchone()[0] == "migration"
    assert any("1 of 0" not in n and "0 of 0" in n for n in schema_migrations._to_v23(conn))  # nothing twice
    conn.close()


def test_helpers():
    assert case_mail.short_name("JJC-VICTOR LANG") == "Victor Lang"
    assert case_mail.short_name("Martin Fuentes") == "Martin Fuentes"
    assert case_mail.snippet("Dear Victor\n\nWishing you a good week\n") == "Wishing you a good week"
    assert case_mail.signature_cut("Line one\n\nThanks\n\nAtte.-\nVictor") == "Line one"
    assert case_mail.signature_cut("Line one\n\nThanks for your help\nmore") == "Line one\n\nThanks for your help\nmore"
    assert case_mail.attachments(["a.pdf", "image001.png", "b.xlsx"]) == ["a.pdf", "b.xlsx"]
