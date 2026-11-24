"""Batch 24b: the follow-up text opened in Outlook (Draft with Claude went in
CX1, 2026-11-04). FX4 (2026-11-12): the draft and its Open in Outlook are
gone (she has her own template tool): no button, the route 404, Outlook
untouched; the route-save test below is unchanged.

Outlook is the fake of the Acknowledge tests (Send fails the test). Every company, person, mail,
place, price and date here is invented; cases from OPP-4501.
"""

from __future__ import annotations

import contextlib

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import case_mail, db, mail_archive, outlook_bridge, parsing
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app
from test_acknowledge import FakeApplication, FakeMail

SUBJECT = "RATE REQUEST // MIA - SAI // valve parts"
AGENT = ("lin@brightwateragents.example", "Lin Okafor")
US = ("desk@jojofreight.example", "JJC-DESK")
QUOTE = "Hi Lin,\n\nOur rate is USD 1,900 all-in, air MIA to SAI. Rates valid until Nov 30.\n\nRegards,\nDesk"


def mail(subject, at, sender, name, body, mid, internal=False, entry=None, to=(US[0],)):
    return MailInfo(subject=subject, received_on=at[:10], received_at=at, entry_id=entry or "EID-" + mid,
                    conversation_id="CONV-4501", conversation_topic=parsing.clean_subject(subject),
                    sender_name=name, sender_address=sender, sender_internal=internal,
                    to_addresses=list(to), body_excerpt=body, folder_path="\\\\Personal Folder\\\\AGENTS",
                    display_to="; ".join(to), message_id="<{}@x>".format(mid))


MAILS = [
    mail(SUBJECT, "2026-11-09 09:00", AGENT[0], AGENT[1], "Hi,\n\nPlease quote air MIA to SAI, 3 cartons.\n\nLin",
         "q0", entry="EID-4501"),
    mail("RE: [OPP-4501] " + SUBJECT, "2026-11-10 10:00", US[0], US[1], QUOTE, "q1", internal=True, to=(AGENT[0],)),
    mail("RE: [OPP-4501] " + SUBJECT, "2026-11-12 09:30", US[0], US[1],
         "Hi Lin,\n\nJust checking back: do the air rates we sent work for you?\n\nThank you.", "f1",
         internal=True, to=(AGENT[0],)),
]
REPLY = mail("RE: [OPP-4501] " + SUBJECT, "2026-11-13 08:00", AGENT[0], AGENT[1],
             "Hi,\n\nStill waiting for the client.\n\nLin", "r1")


def store(path, mails):
    conn = mail_archive.open_archive(path)
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in mails])
    mail_archive.set_meta(conn, "last_sync_at", "2026-11-13 12:00:00")
    conn.commit()
    conn.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    store(archive, MAILS)
    path = tmp_path / "fu.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    agent = add_party(repo, "Brightwater Agents", country="Chile")
    repo.insert("party_domain", {"domain": "brightwateragents.example", "party_id": agent})
    case = add_enquiry(repo, "OPP-4501", agent, received="2026-11-09", source_subject=SUBJECT,
                       thread_key=parsing.subject_thread_key(SUBJECT), entry_id="EID-4501",
                       conversation_id="CONV-4501", service_mode="Air", origin_loc="MIA", origin_country="USA",
                       dest_loc="SAI", dest_country="Chile", created_by="outlook")
    repo.set_status(case, "Quoted", "2026-11-10")
    repo.add_followup(case, "2026-11-12")
    db.set_setting(conn, "next_opp_number", 4502)
    repo.commit()
    outlook = {}
    monkeypatch.setattr(outlook_bridge, "_com", contextlib.nullcontext)
    monkeypatch.setattr(outlook_bridge, "_application", lambda: FakeApplication(outlook))
    app = create_app(path)
    app.config["TESTING"] = True
    yield {"conn": conn, "repo": repo, "case": case, "agent": agent, "archive": archive, "outlook": outlook,
           "web": app.test_client()}
    conn.close()


def page_of(w, **args):
    return w["web"].get("/enquiries/{}".format(w["case"]), query_string=args).data.decode("utf-8")


# ------------------------------------------------------------- the button --


# -------------------------------------------------------- Open in Outlook --
def test_open_in_outlook_and_its_route_are_gone(world):
    w = world
    w["outlook"]["EID-f1"] = FakeMail("RE: [OPP-4501] " + SUBJECT, sender="JJC-DESK")
    before = [tuple(r) for r in w["conn"].execute("SELECT * FROM change_log")]
    page = page_of(w)
    assert 'form="fu-outlook"' not in page and 'id="fu-text"' not in page
    response = w["web"].post("/enquiries/{}/followup/outlook".format(w["case"]),
                             data={"text": "Hi Lin,\n\nAny news on the air rates?"})
    assert response.status_code == 404
    assert w["outlook"]["EID-f1"].replies == []
    assert [tuple(r) for r in w["conn"].execute("SELECT * FROM change_log")] == before


def test_a_typed_in_case_has_the_log_and_no_draft_either(world, tmp_path, monkeypatch):
    w = world
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", tmp_path / "none" / "mail.db")
    assert not hasattr(case_mail, "reply_target")                 # the drafts' target went with them
    typed = add_enquiry(w["repo"], "OPP-4502", w["agent"], received="2026-11-11", service_mode="Air")
    w["repo"].set_status(typed, "Quoted", "2026-11-11")
    w["repo"].commit()
    body = w["web"].get("/enquiries/{}".format(typed)).data.decode("utf-8")
    assert 'form="fu-outlook"' not in body and 'id="fu-text"' not in body and 'id="fu-log"' in body


# ------------------------------------------- 24b-fix: post, redirect, get --


def test_a_refused_route_save_redirects_and_comes_back_with_the_editor(world, monkeypatch):
    w = world
    # T2: the route form has no result date, so the save is dated today;
    # today is fixed before the case's Quoted (Nov 10) so it stays refused
    import opplist.web as web_module
    monkeypatch.setattr(web_module, "_today", lambda: "2026-11-01")
    route = w["conn"].execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (w["case"],)).fetchone()[0]
    page = w["web"].post("/enquiries/{}/routes".format(w["case"]), data={
        "form": "route", "route_id": str(route), "origin_loc": "MIA", "origin_country": "USA", "dest_loc": "SAI",
        "dest_country": "Chile", "service_mode": "Air", "result": "Won", "result_on": "2026-11-01"})
    assert page.status_code == 302 and page.headers["Location"].endswith("refused=1#routes")
    body = w["web"].get(page.headers["Location"]).data.decode("utf-8")
    assert "is before this case&#39;s latest event" in body
