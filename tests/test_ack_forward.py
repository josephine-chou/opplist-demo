"""Batch A2b: a colleague's forward is acknowledged with a new mail to the
original sender, the colleague on Cc, the original quoted under the
signature; an office's forward keeps A1's Reply All. FX4 (2026-11-12): the
drafts went. FX5 (2026-11-13): Acknowledge is back, blank - a colleague's
forward opens a new mail to the original sender (the colleague on Cc, the
numbered subject, nothing but her signature), an office's forward the Reply
All; whom a forward came from (ack.forward_target, is_colleague_forward:
the team site's reply too) and the scan's Acknowledged event.

CX1: the registered mail itself says it is a forward - the archive's copy,
else Outlook's - not a quick note. A mocked Outlook (test_acknowledge's
fakes plus a new-mail item; Send and Save fail the test). Every company,
person, place and number here is invented; cases from OPP-5701.
"""

from __future__ import annotations

import contextlib

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import ack, db, detect_followups, mail_archive, outlook_bridge, parsing
from opplist.web import create_app
from test_acknowledge import SIGNATURE, FakeApplication, FakeMail
from test_register_v3 import fields_of

AGENT = "odalys@brackenfreight.example"
COLLEAGUE = "rafe@jojofreight.example"
ORIGINAL_SUBJECT = "RATE REQUEST // CHARLESTON - VALPARAISO // 1 X 40HQ"
HEADER = ("From: Odalys Varga <{a}>\nSent: Tuesday, November 3, 2026 9:05 AM\n"
          "To: JJC-DESK <desk@jojofreight.example>\nSubject: {s}\n")
ASK = "\nHello,\nPlease quote 1 x 40HQ Charleston to Valparaiso, machine parts, 18,000 kg.\nOdalys\n"
NOTE = "Can you take this one over? I am out tomorrow."


def forward_mail(header=None, subject="Fwd: " + ORIGINAL_SUBJECT, key="5701", sender=COLLEAGUE,
                 name="Rafe Moreno"):
    return outlook_bridge.MailInfo(
        subject=subject, received_on="2026-11-04", received_at="2026-11-04 08:30",
        sender_address=sender, sender_name=name, sender_internal=True,
        to_addresses=["desk@jojofreight.example"], to_names=["Desk"],
        body_excerpt=NOTE + "\n\n" + (header or HEADER.format(a=AGENT, s=ORIGINAL_SUBJECT)) + ASK,
        entry_id="ENTRY-" + key, conversation_id="CONV-" + key)


class FakeNew:
    """A new mail: the signature for new mail appears once the inspector opens."""

    def __init__(self):
        self.To = self.CC = self.Subject = ""
        self._html = "<html><head></head><body lang=EN-US></body></html>"
        self.displayed = False

    @property
    def GetInspector(self):
        self._html = self._html.replace("<body lang=EN-US>", "<body lang=EN-US>" + SIGNATURE, 1) \
            if SIGNATURE not in self._html else self._html
        return object()

    @property
    def HTMLBody(self):
        return self._html

    @HTMLBody.setter
    def HTMLBody(self, value):
        self._html = value

    def Display(self):
        self.GetInspector                             # the window opens: the signature goes in
        self.displayed = True

    def Send(self):
        pytest.fail("the draft was sent")

    def Save(self):
        pytest.fail("the draft was saved")


class Outlook(FakeApplication):
    def __init__(self, mails):
        super().__init__(mails)
        self.created = []

    def CreateItem(self, kind):
        assert kind == 0                              # olMailItem
        item = FakeNew()
        self.created.append(item)
        return item


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "a2b.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    bracken = add_party(repo, "Bracken Freight", country="Chile")
    repo.insert("party_domain", {"domain": "brackenfreight.example", "party_id": bracken})
    db.set_setting(conn, "next_opp_number", 5701)
    repo.commit()
    mails = {}
    outlook = Outlook(mails)
    monkeypatch.setattr(outlook_bridge, "_com", contextlib.nullcontext)
    monkeypatch.setattr(outlook_bridge, "_application", lambda: outlook)
    reads = {}
    monkeypatch.setattr(outlook_bridge, "read_item", lambda entry_id, **kw: reads[entry_id])
    holder = {"mail": None}
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: holder["mail"])
    app = create_app(path)
    app.config["TESTING"] = True
    yield {"conn": conn, "repo": repo, "bracken": bracken, "outlook": outlook, "mails": mails, "reads": reads,
           "holder": holder, "client": app.test_client(), "path": path}
    conn.close()


def forwarded_case(w, mail, opp="OPP-5701"):
    case = add_enquiry(w["repo"], opp, w["bracken"], received="2026-11-03", entry_id=mail.entry_id,
                       source_subject=mail.subject, created_by="outlook", service_mode="FCL",
                       origin_country="USA", dest_country="Chile", container_type="40HQ", container_qty=1)
    w["repo"].commit()
    w["reads"][mail.entry_id] = mail
    return case


def test_a_colleagues_forward_opens_a_blank_new_mail_to_the_original_sender(world):
    """FX5: Acknowledge, blank - To the original sender, the colleague on Cc,
    the numbered subject, nothing but her signature; displayed, not sent."""
    w = world
    mail = forward_mail()
    case = forwarded_case(w, mail)
    target = ack.forward_target(w["conn"], mail)
    assert ack.is_colleague_forward(mail)
    assert (target["address"], target["cc"], target["name"]) == (AGENT, COLLEAGUE, "Odalys Varga")
    assert ">Acknowledge</button>" in w["client"].get("/enquiries/{}".format(case)).data.decode()
    before = [tuple(r) for r in w["conn"].execute("SELECT * FROM change_log ORDER BY id")]
    page = w["client"].post("/enquiries/{}/acknowledge".format(case), follow_redirects=True).data.decode()
    assert "Acknowledgment opened in Outlook." in page
    [new] = w["outlook"].created
    assert (new.To, new.CC, new.Subject) == (AGENT, COLLEAGUE, "RE: [OPP-5701] " + ORIGINAL_SUBJECT)
    assert new.displayed and new.HTMLBody == "<html><head></head><body lang=EN-US>" + SIGNATURE + "</body></html>"
    assert w["mails"] == {}                                    # no Reply All of the forward
    assert [tuple(r) for r in w["conn"].execute("SELECT * FROM change_log ORDER BY id")] == before


def test_a_subject_with_re_already_gets_no_second_one(world):
    w = world
    mail = forward_mail(header=HEADER.format(a=AGENT, s="Re: " + ORIGINAL_SUBJECT), subject="FW: Re: " + ORIGINAL_SUBJECT)
    target = ack.forward_target(w["conn"], mail)
    assert ack.numbered_subject(target["subject"], "OPP-5701") == "RE: [OPP-5701] " + ORIGINAL_SUBJECT


def test_no_address_leaves_to_empty(world):
    w = world
    header = ("From: Odalys Varga\nSent: Tuesday, November 3, 2026 9:05 AM\nTo: JJC-DESK\n"
              "Subject: {}\n".format(ORIGINAL_SUBJECT))
    target = ack.forward_target(w["conn"], forward_mail(header=header))
    assert (target["address"], target["cc"], target["name"]) == ("", COLLEAGUE, "Odalys Varga")


def test_the_register_page_has_no_after_register_and_opens_nothing(world):
    w = world
    db.set_setting(w["conn"], "ack_after_register", "draft")
    w["conn"].commit()
    mail = forward_mail()
    w["holder"]["mail"] = mail
    w["reads"][mail.entry_id] = mail
    body = w["client"].post("/enquiries/from-outlook").data.decode("utf-8")
    assert "After Register" not in body and 'name="ack_after"' not in body and 'name="ack_offer"' not in body
    fields = fields_of(body)
    fields.update(ack_after=["draft"], ack_offer=["1"], origin_country=["USA"], dest_country=["Chile"],
                  service_mode=["FCL"], container_type=["40HQ"], container_qty=["1"])
    page = w["client"].post("/enquiries/new", data=fields, follow_redirects=True).data.decode("utf-8")
    assert "Saved OPP-5701" in page, page[:3000]
    assert "Acknowledgment draft opened in Outlook." not in page and w["outlook"].created == []
    assert db.get_setting(w["conn"], "ack_after_register") == "draft"          # stays, unused


def test_an_office_forward_is_not_a_colleagues_and_gets_the_reply_all(world):
    w = world
    mail = forward_mail(sender="noor@jojofreight.example", name="JJK-NOOR HADI", key="5702")
    assert mail.subject.startswith("Fwd:")
    assert not ack.is_colleague_forward(mail)
    case = add_enquiry(w["repo"], "OPP-5702", w["bracken"], received="2026-11-03", entry_id=mail.entry_id,
                       source_subject=mail.subject, created_by="outlook", service_mode="FCL")
    w["repo"].commit()
    w["mails"][mail.entry_id] = FakeMail(mail.subject, sender="JJK-NOOR HADI")
    w["reads"][mail.entry_id] = mail
    w["client"].post("/enquiries/{}/acknowledge".format(case))
    [reply] = w["mails"][mail.entry_id].replies
    assert reply.displayed and reply.Subject == "RE: [OPP-5702] " + ORIGINAL_SUBJECT and not reply.written
    assert w["outlook"].created == []


def test_the_scan_records_the_acknowledgment_sent_as_a_new_mail(world, tmp_path, monkeypatch):
    w = world
    fwd = forward_mail()
    case = forwarded_case(w, fwd)
    ours = outlook_bridge.MailInfo(
        subject="[OPP-5701] RE: " + ORIGINAL_SUBJECT, received_on="2026-11-04", received_at="2026-11-04 09:10",
        sender_address="desk@jojofreight.example", sender_name="JJC-DESK", sender_internal=True,
        to_addresses=[AGENT], to_names=["Odalys Varga"], cc_addresses=[COLLEAGUE], cc_names=["Rafe Moreno"],
        body_excerpt="Hi Odalys,\n\nThank you for your inquiry. We are working on the rates.\n\nRegards",
        entry_id="EID-ACK", conversation_id="CONV-OURS", conversation_topic=parsing.clean_subject(ORIGINAL_SUBJECT),
        folder_path="\\\\Personal Folder\\\\Sent Items", display_to=AGENT, message_id="<ack5701@x>")
    fwd.conversation_topic = parsing.clean_subject(fwd.subject)
    fwd.message_id = "<fwd5701@x>"
    path = tmp_path / "archive" / "mail.db"
    path.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", path)
    archive = mail_archive.open_archive(path)
    mail_archive.store(archive, [mail_archive.record_from_info(m) for m in (fwd, ours)])
    mail_archive.set_meta(archive, "last_sync_at", "2026-11-04 12:00:00")
    archive.commit()
    archive.close()
    with mail_archive.ArchiveSearch(path) as search:
        report = detect_followups.run(w["conn"], search)
    assert report.case_numbers.acknowledged and report.case_numbers.acknowledged[0].startswith("OPP-5701 2026-11-04")
    rows = w["conn"].execute("SELECT enquiry_id, at FROM status_event WHERE kind = 'ack'").fetchall()
    assert [tuple(r) for r in rows] == [(case, "2026-11-04")]
