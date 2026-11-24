"""Batch CX1 (2026-11-04): no Claude anywhere in OppList, no quick notes, no
Assign List sheet.

Nothing in the database is deleted: the case_note rows and the stored
settings stay, unread. A colleague's forward keeps A2b's acknowledgment (a
new mail to the original sender); what says it is a forward is the
register form's flag, afterwards the archive's copy of the registered
mail, else Outlook's. A mocked Outlook (test_acknowledge's and
test_ack_forward's fakes; Send and Save fail the test). Every company,
person, place and number here is invented; cases from OPP-8801. FX4
(2026-11-12): the acknowledgment's draft and After Register are gone; the
form's forward flag and the archive's copy still say what a forward is
(ack.is_colleague_forward, the team site's reply).
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import __main__ as cli
from opplist import ack, db, firebase_sync, mail_archive, outlook_bridge, scheduled
from opplist.web import create_app
from test_ack_forward import Outlook
from test_acknowledge import FakeMail
from test_register_v3 import fields_of

AGENT = "oriel@thistlebankcargo.test"
COLLEAGUE = "juno@jojofreight.example"
SUBJECT = "RATE REQUEST // SAVANNAH - SAN ANTONIO // 2 X 20GP"
HEADER = ("From: Oriel Sandoval <{}>\nSent: Monday, November 2, 2026 8:15 AM\n"
          "To: JJC-DESK <desk@jojofreight.example>\nSubject: {}\n".format(AGENT, SUBJECT))
ASK = "\nHello,\nPlease quote 2 x 20GP Savannah to San Antonio, ceramic tiles, 21,000 kg.\nOriel\n"
GONE = ("claude_reader", "claude_evaluate", "fu_claude", "fu_drafts", "scan_claude", "quote_reader", "assign_sheet")


def forward_mail(key="8801"):
    return outlook_bridge.MailInfo(
        subject="FW: " + SUBJECT, received_on="2026-11-03", received_at="2026-11-03 09:20",
        sender_address=COLLEAGUE, sender_name="Juno Brandt", sender_internal=True,
        to_addresses=["desk@jojofreight.example"], to_names=["Desk"],
        body_excerpt="Could you quote this one? Thanks.\n\n" + HEADER + ASK,
        entry_id="ENTRY-" + key, conversation_id="CONV-" + key, message_id="<fw{}@x>".format(key),
        conversation_topic=SUBJECT, folder_path="\\\\Mailbox\\\\Inbox")


def agent_mail(key="8802"):
    return outlook_bridge.MailInfo(
        subject=SUBJECT, received_on="2026-11-03", received_at="2026-11-03 10:00",
        sender_address=AGENT, sender_name="Oriel Sandoval", to_addresses=["desk@jojofreight.example"], to_names=["Desk"],
        body_excerpt=ASK, entry_id="ENTRY-" + key, conversation_id="CONV-" + key, message_id="<a{}@x>".format(key),
        conversation_topic=SUBJECT, folder_path="\\\\Mailbox\\\\Inbox")


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "cx1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    db.set_setting(conn, "owners", ["Ines", "Tobin"])
    thistle = add_party(repo, "Thistlebank Cargo", country="Chile")
    repo.insert("party_domain", {"domain": "thistlebankcargo.test", "party_id": thistle})
    # what earlier batches left behind: stored, never read again
    for key, value in (("claude_account", "login"), ("claude_model", "claude-sonnet-5-5"),
                       ("claude_month_cap_usd", "15"), ("scan_replies_with_claude", "1"),
                       ("morning_fu_drafts", "1"), ("morning_fu_drafts_max", "10"),
                       ("claude_last_selftest", json.dumps({"ok": True})),
                       ("assign_sheet_url", "https://sheets.example/d/invented-sheet-8801"),
                       ("ack_after_register", "draft")):
        db.set_setting(conn, key, value)
    db.set_setting(conn, "next_opp_number", 8801)
    repo.commit()
    mails = {}
    outlook = Outlook(mails)
    monkeypatch.setattr(outlook_bridge, "_com", contextlib.nullcontext)
    monkeypatch.setattr(outlook_bridge, "_application", lambda: outlook)
    reads = {}

    def read_item(entry_id="", **kw):
        if entry_id not in reads:
            raise outlook_bridge.OutlookError("not in Outlook", "test: no such item")
        return reads[entry_id]
    monkeypatch.setattr(outlook_bridge, "read_item", read_item)
    holder = {"mail": None}
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: holder["mail"])
    app = create_app(path)
    app.config["TESTING"] = True
    yield {"conn": conn, "repo": repo, "thistle": thistle, "outlook": outlook, "mails": mails, "reads": reads,
           "holder": holder, "app": app, "client": app.test_client(), "path": path, "tmp": tmp_path}
    conn.close()


def case_of(w, mail, opp):
    case = add_enquiry(w["repo"], opp, w["thistle"], received="2026-11-02", entry_id=mail.entry_id,
                       source_subject=mail.subject, created_by="outlook", service_mode="FCL",
                       origin_country="USA", dest_country="Chile", container_type="20GP", container_qty=2)
    w["repo"].commit()
    return case


def store(w, *mails):
    path = w["tmp"] / "archive" / "mail.db"
    path.parent.mkdir(exist_ok=True)
    archive = mail_archive.open_archive(path)
    mail_archive.store(archive, [mail_archive.record_from_info(m) for m in mails])
    mail_archive.set_meta(archive, "last_sync_at", "2026-11-03 12:00:00")
    archive.commit()
    archive.close()
    return path


# ------------------------------------------------------------ 1. Claude --
def test_no_page_offers_it_and_no_route_for_it_answers(world, monkeypatch):
    # (the test's name stays off the database path the Settings page shows)
    w = world
    client = w["client"]
    case = case_of(w, agent_mail(), "OPP-8801")
    w["repo"].set_status(case, "Quoted", "2026-11-03")
    w["repo"].commit()
    w["holder"]["mail"] = agent_mail("8803")
    pages = ["/", "/enquiries", "/enquiries/{}".format(case), "/enquiries/{}?step=2&draft=1".format(case),
             "/tracking", "/settings", "/dashboard", "/report?month=2026-11", "/enquiries/new"]
    for url in pages:
        body = client.get(url, follow_redirects=True).data.decode("utf-8")
        if url == "/settings":
            # Q2d (16 Nov): Read quote's block is the one place that names Claude
            start = body.index('<section class="panel" id="read-quote">')
            body = body[:start] + body[body.index("</section>", start):]
        assert "claude" not in body.lower(), url
    body = client.post("/enquiries/from-outlook").data.decode("utf-8")
    assert "Read with Claude" not in body and "claude" not in body.lower()
    rules = [r.rule for r in w["app"].url_map.iter_rules()]
    # Q2d: Read quote's two routes are the only ones back (her press on 8765, the weekly report)
    assert not [r for r in rules if "claude" in r or "assign-sheet" in r or "/notes" in r or (
        "read-quote" in r and r not in ("/enquiries/<int:enquiry_id>/read-quote/<int:request_id>",
                                        "/settings/read-quote-report"))]
    for url in ("/enquiries/from-outlook/claude", "/enquiries/{}/followup/claude".format(case),
                "/enquiries/{}/acknowledge/claude".format(case), "/enquiries/{}/vendors/1/read-quote".format(case),
                "/settings/claude/test", "/settings/assign-sheet/sync", "/enquiries/{}/notes".format(case),
                "/notes/1", "/notes/1/delete"):
        assert client.post(url).status_code == 404, url
    for name in GONE:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("opplist." + name)
    # the stored settings stay, unread
    assert db.get_setting(w["conn"], "scan_replies_with_claude") == "1"
    assert db.get_setting(w["conn"], "claude_model") == "claude-sonnet-5-5"


def test_saving_settings_keeps_the_old_values(world):
    w = world
    page = w["client"].post("/settings", data={"fu1_days": "2"}, follow_redirects=True).data.decode("utf-8")
    assert "Assign list" not in page and 'name="assign_sheet_url"' not in page and "assign_list_customers" not in page
    for key in ("morning_fu_drafts", "scan_replies_with_claude", "claude_account", "assign_sheet_url"):
        assert db.get_setting(w["conn"], key) not in (None, "", "0"), key


# -------------------------------------------------- 2. a colleague's forward --
def test_a_forward_registered_from_outlook_leaves_no_note_and_opens_no_draft(world):
    w = world
    mail = forward_mail()
    w["holder"]["mail"] = mail
    w["reads"][mail.entry_id] = mail
    body = w["client"].post("/enquiries/from-outlook").data.decode("utf-8")
    fields = fields_of(body)
    assert fields["forward_note"][0].startswith("Forwarded by Juno Brandt")       # the form's flag
    fields.update(ack_after=["draft"], origin_country=["USA"], dest_country=["Chile"], service_mode=["FCL"],
                  container_type=["20GP"], container_qty=["2"])
    remembered = db.get_setting(w["conn"], "ack_after_register")
    page = w["client"].post("/enquiries/new", data=fields, follow_redirects=True).data.decode("utf-8")
    assert "Saved OPP-8801" in page, page[:3000]
    assert w["conn"].execute("SELECT COUNT(*) FROM case_note").fetchone()[0] == 0
    # FX4: After Register is gone - no draft, the old setting stays unused
    assert w["outlook"].created == [] and w["mails"] == {}
    assert "Acknowledgment draft opened in Outlook." not in page
    assert db.get_setting(w["conn"], "ack_after_register") == remembered
    assert ack.is_colleague_forward(mail)


def test_acknowledge_reads_the_forward_from_outlook(world):
    """FX5: Acknowledge is back, blank; registered today, only Outlook has
    the mail, and it says it is a colleague's forward."""
    w = world
    mail = forward_mail("8804")
    case = case_of(w, mail, "OPP-8802")
    w["reads"][mail.entry_id] = mail                       # registered today: only Outlook has it
    w["client"].post("/enquiries/{}/acknowledge".format(case))
    assert len(w["outlook"].created) == 1 and w["mails"] == {}


def test_the_archive_s_copy_says_what_a_forward_is(world, monkeypatch):
    w = world
    plain = agent_mail("8805")
    case = case_of(w, plain, "OPP-8803")
    fwd = forward_mail("8806")
    case_of(w, fwd, "OPP-8804")
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", store(w, plain, fwd))
    with mail_archive.ArchiveSearch(mail_archive.ARCHIVE_DB) as search:
        assert not ack.is_colleague_forward(search.by_entry_id(plain.entry_id))   # the agent's own mail
        assert ack.is_colleague_forward(search.by_entry_id(fwd.entry_id))         # a forward is a forward
    # FX5: the agent's own mail gets the Reply All, not a new mail
    w["mails"][plain.entry_id] = FakeMail(SUBJECT, sender="Oriel Sandoval")
    w["client"].post("/enquiries/{}/acknowledge".format(case))
    assert len(w["mails"][plain.entry_id].replies) == 1 and w["outlook"].created == []


# ------------------------------------------------------ 3. quick notes --
def test_the_case_page_has_no_quick_notes_card_and_old_notes_stay_unshown(world):
    w = world
    case = case_of(w, agent_mail(), "OPP-8801")
    w["repo"].insert("case_note", {"enquiry_id": case, "body": "Ask Tobin about the tiles"})   # from before CX1
    w["repo"].commit()
    body = w["client"].get("/enquiries/{}".format(case)).data.decode("utf-8")
    assert 'id="qnotes"' not in body and 'class="qadd"' not in body and "qnotes-data" not in body
    assert "Ask Tobin about the tiles" not in body
    assert w["conn"].execute("SELECT COUNT(*) FROM case_note").fetchone()[0] == 1     # nothing deleted


def test_the_team_site_s_case_documents_carry_no_quick_notes(world):
    w = world
    case = case_of(w, agent_mail(), "OPP-8801")
    w["repo"].insert("case_note", {"enquiry_id": case, "body": "Ask Tobin about the tiles"})
    w["repo"].commit()
    built = firebase_sync.build(w["conn"], today="2026-11-04")
    doc = built.cases["OPP-8801"]
    assert "qnotes" not in doc and "Ask Tobin" not in json.dumps(built.cases)


# ---------------------------------------------------------- 4. the sheet --
def test_the_app_starts_no_assign_list_syncer(world):
    app = world["app"]
    assert not hasattr(app, "assign_syncer") and hasattr(app, "firebase_syncer")
    response = world["client"].post("/settings", data={"fu1_days": "2"})
    assert response.status_code == 302
    assert not hasattr(app, "assign_syncer")


# ------------------------------------------------- 5. draft-followups --
def test_draft_followups_scheduled_exits_0_with_its_one_line(world, monkeypatch, capsys, caplog):
    w = world
    monkeypatch.setattr(scheduled, "guard", lambda db_path, command: None)
    recorded = []
    monkeypatch.setattr(scheduled, "record", lambda *a: recorded.append(a))
    with caplog.at_level(logging.INFO, logger="opplist"):
        assert cli.main(["draft-followups", "--db", str(w["path"]), "--scheduled"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["draft-followups: retired 2026-11-04 (batch CX1, no Claude)"]
    assert [r.getMessage() for r in caplog.records if r.name.startswith("opplist")] == out
    assert [(r[0].split()[0], r[1]) for r in recorded] == [("draft-followups", "done")]
