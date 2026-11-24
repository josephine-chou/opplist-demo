"""Batch 24c: the Follow-ups card - Another version is gone, the rotation
stays (Draft with Claude, its note and its language went in CX1). FX4
(2026-11-12): the card's draft and the Settings templates are gone (she
has her own template tool); the rotation stays for the followup command.

The archive is a test one. Every company, person, mail, place, price and date here is
invented; cases from OPP-4801.
"""

from __future__ import annotations

import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, followup, mail_archive, parsing
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app

SUBJECT = "COTIZACION // HOUSTON - ARICA // 1 X 40HQ"
AGENT = ("marisol@quebradacarga.example", "Marisol Ibarra")
US = ("desk@jojofreight.example", "JJC-DESK")


def mail(subject, at, sender, body, mid, internal=False, entry=None, to=(US[0],)):
    return MailInfo(subject=subject, received_on=at[:10], received_at=at, entry_id=entry or "EID-" + mid,
                    conversation_id="CONV-4801", conversation_topic=parsing.clean_subject(subject),
                    sender_name=sender[1], sender_address=sender[0], sender_internal=internal,
                    to_addresses=list(to), body_excerpt=body, folder_path="\\Mailbox\\Inbox\\AGENTS",
                    display_to="; ".join(to), message_id="<{}@x>".format(mid))


THREAD = [
    mail(SUBJECT, "2026-11-02 09:00", AGENT,
         "Hola,\n\nFavor cotizar 1 x 40HQ Houston - Arica, repuestos de bombas, carga lista el 20.\n\nMarisol",
         "i1", entry="EID-4801"),
    mail("RE: [OPP-4801] " + SUBJECT, "2026-11-03 10:00", US,
         "Hi Marisol,\n\nOur rate is USD 3,150 per 40HQ, valid until Nov 30. Please confirm the cargo weight.\n\n"
         "Regards,\nDesk", "q1", internal=True, to=(AGENT[0],)),
    mail("RE: [OPP-4801] " + SUBJECT, "2026-11-06 09:30", US,
         "Hi Marisol,\n\nJust checking back on the rate for the 40HQ to Arica.\n\nThank you.", "f1",
         internal=True, to=(AGENT[0],)),
    mail("RE: [OPP-4801] " + SUBJECT, "2026-11-07 15:00", AGENT,
         "Hola,\n\nEl cliente todavia esta revisando con su gerencia.\n\nSaludos,\nMarisol", "r1"),
]


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    aconn = mail_archive.open_archive(archive)
    mail_archive.store(aconn, [mail_archive.record_from_info(m) for m in THREAD])
    mail_archive.set_meta(aconn, "last_sync_at", "2026-11-09 08:00:00")
    aconn.commit()
    aconn.close()
    path = tmp_path / "fu.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    quebrada = add_party(repo, "Quebrada Carga", country="Chile")
    repo.insert("party_domain", {"domain": "quebradacarga.example", "party_id": quebrada})
    case = add_enquiry(repo, "OPP-4801", quebrada, received="2026-11-02", source_subject=SUBJECT,
                       thread_key=parsing.subject_thread_key(SUBJECT), entry_id="EID-4801",
                       conversation_id="CONV-4801", service_mode="FCL", origin_country="USA", dest_country="Chile",
                       container_type="40HQ", container_qty=1, created_by="outlook")
    repo.set_status(case, "Quoted", "2026-11-03")
    repo.add_followup(case, "2026-11-06")
    repo.commit()
    app = create_app(path)
    app.config["TESTING"] = True
    yield {"conn": conn, "repo": repo, "case": case, "party": quebrada, "web": app.test_client()}
    conn.close()


def test_no_another_version_and_the_rotation_still_alternates(world):
    w = world
    conn, repo = w["conn"], w["repo"]
    page = w["web"].get("/enquiries/{}?draft=1".format(w["case"])).data.decode("utf-8")
    assert "Claude" not in page and "Another version" not in page and "version_of" not in page
    db.set_setting(conn, "fu_template_1", "Hi {first_name}, one.\n---\nHi {first_name}, two.")
    conn.commit()
    other = add_enquiry(repo, "OPP-4802", w["party"], received="2026-11-04", service_mode="Air",
                        origin_country="USA", dest_country="Chile")
    third = add_enquiry(repo, "OPP-4803", w["party"], received="2026-11-05", service_mode="Air",
                        origin_country="USA", dest_country="Chile")
    for c in (other, third):
        repo.set_status(c, "Quoted", "2026-11-06")
    repo.commit()
    src = followup.Sources("Marisol", None)
    first = followup.compose(conn, other, 1, sources=src)
    repo.add_followup(other, "2026-11-08")
    repo.commit()
    second = followup.compose(conn, third, 1, sources=src)
    # OPP-4801 already had an FU1: version two, then version one
    assert (first.count, first.index, second.index) == (2, 1, 0)


# ------------------------------------- the card as it is now (24c), invented --
@pytest.fixture
def card(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", tmp_path / "no-archive" / "mail.db")
    path = tmp_path / "card.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Wrenmoor Freight", country="Chile")
    case = add_enquiry(repo, "OPP-4811", party, received="2026-11-02", service_mode="FCL", origin_loc="LAX",
                       origin_country="USA", dest_loc="SAI", dest_country="Chile", container_type="40HQ",
                       container_qty=2)
    repo.set_status(case, "Quoted", "2026-11-03")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, case


def test_the_case_page_has_the_followup_section_without_a_draft(card):
    client, path, case = card
    body = client.get("/enquiries/{}".format(case)).data.decode("utf-8")
    fu = body[body.index('id="followups"'):]
    fu = fu[:fu.index("</section>")]
    assert "No follow-up sent on this quote yet." in fu and '<span class="k">FU1</span>' in fu
    assert 'name="channel" value="mail"' in fu
    assert 'data-copy="fu-text"' not in fu and "<textarea" not in fu and 'name="step"' not in fu
    assert "workable for this shipment" not in fu                          # the first template's words
    assert "Another version" not in fu and " of 5" not in fu
    assert not re.search(r"\{\w+\}", fu)                                   # no raw blank


def test_the_settings_page_has_no_templates_and_keeps_the_ladder_numbers(card):
    client, path, case = card
    settings = client.get("/settings").data.decode("utf-8")
    assert 'name="fu_template_1"' not in settings and 'name="fu1_days" value="1"' in settings
    conn = db.open_db(path)
    stored = db.get_setting(conn, "fu_template_1", "")
    conn.close()
    page = client.post("/settings", data={"fu_template_1": "Hi {first_name}, how do {rates} look?",
                                          "fu1_days": "2", "fu2_days": "5", "fu_decide_days": "5",
                                          "fu_max": "2"}, follow_redirects=True)
    assert page.status_code == 200
    conn = db.open_db(path)
    # the template text stays as it was in the setting table, unused; the ladder number is saved
    assert db.get_setting(conn, "fu_template_1", "") == stored and str(db.get_setting(conn, "fu1_days")) == "2"
    conn.close()
    body = client.get("/enquiries/{}".format(case)).data.decode("utf-8")
    assert "how do the FCL rates below look?" not in body
