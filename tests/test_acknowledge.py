"""Batch A1: Acknowledge opened the acknowledgment draft in Outlook, from the
template (CX1: no draft by Claude any more). FX4 (2026-11-12): the button,
its route and its template went (she has her own template tool). FX5
(2026-11-13, her answer): Acknowledge is back, blank - on a New case
registered from a mail and not acknowledged, a Reply All of the registered
mail with the case number in the subject and nothing in the body but what
Outlook puts there (her signature); displayed, never sent, nothing written
on the case. Also checked: 'Acknowledged <date>', the subject's case
number and the questions the case leaves open (ack.questions, wait.prefill
reads them).

A mocked Outlook: GetItemFromID gives a FakeMail whose ReplyAll is a
FakeReply; the signature appears in HTMLBody only once the inspector is
opened, as Outlook does; Send fails the test. Every company, person,
place, number and quoted line here is invented; cases from OPP-4001.
"""

from __future__ import annotations

import contextlib

import pytest

from conftest import add_party, seed_reference
from opplist import ack, ack_questions, db, outlook_bridge
from opplist.web import create_app

SIGNATURE = '<div class=sig><p class=MsoNormal>Mara Osten<br>JoJo Freight JJC</p></div>'
QUOTED = '<div class=quoted><p>From: Ines Calloway<br>Please quote air MIA to SAI.</p></div>'


class FakeReply:
    def __init__(self, subject):
        self.Subject = "RE: " + subject
        self._inspector = False
        self._html = "<html><head></head><body lang=EN-US>" + QUOTED + "</body></html>"
        self.displayed = False
        self.written = False                          # FX5: the app never writes the body

    @property
    def GetInspector(self):
        # Outlook puts the signature in when the inspector opens
        if not self._inspector:
            self._inspector = True
            self._html = self._html.replace("<body lang=EN-US>", "<body lang=EN-US>" + SIGNATURE)
        return object()

    @property
    def HTMLBody(self):
        return self._html

    @HTMLBody.setter
    def HTMLBody(self, value):
        self.written = True
        self._html = value

    def Display(self):
        self.GetInspector                             # the window opens: the signature goes in
        self.displayed = True

    def Send(self):                                   # never
        pytest.fail("the draft was sent")

    def Save(self):                                   # never
        pytest.fail("the draft was saved")


class FakeMail:
    Class = outlook_bridge.OL_MAIL_ITEM

    def __init__(self, subject, sender="Ines Calloway"):
        self.Subject = subject
        self.SenderName = sender
        self.replies = []

    def ReplyAll(self):
        reply = FakeReply(self.Subject)
        self.replies.append(reply)
        return reply

    def Send(self):
        pytest.fail("the mail was sent")


class FakeApplication:
    def __init__(self, mails):
        app = self
        self.mails = mails

        class Session:
            @staticmethod
            def GetItemFromID(entry_id):
                if entry_id not in app.mails:
                    raise RuntimeError("item not found")
                return app.mails[entry_id]
        self.Session = Session


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "a1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    ids = {"path": path}
    ids["agent"] = add_party(repo, "Quill Agents", country="Chile")
    repo.insert("party_domain", {"domain": "quillagents.example", "party_id": ids["agent"]})
    db.set_setting(conn, "next_opp_number", 4001)
    repo.commit()
    mails = {}
    app = FakeApplication(mails)
    monkeypatch.setattr(outlook_bridge, "_com", contextlib.nullcontext)
    monkeypatch.setattr(outlook_bridge, "_application", lambda: app)
    yield conn, repo, ids, mails
    conn.close()


def new_case(repo, ids, entry="ENTRY-4001", mode="Air", **values):
    row = {"requester_party_id": ids["agent"], "received_on": "2026-11-16", "service_mode": mode,
           "origin_loc": "MIA", "origin_country": "USA", "dest_loc": "SAI", "dest_country": "Chile",
           "entry_id": entry, "source_subject": "RATE REQUEST MIA - SAI", "created_by": "outlook"}
    row.update(values)
    case = repo.create_enquiry(row, status_at="2026-11-16")
    repo.commit()
    return case


def opp(conn, case):
    return conn.execute("SELECT opp_no FROM enquiry WHERE id = ?", (case,)).fetchone()[0]


def client_of(ids):
    app = create_app(ids["path"])
    app.config["TESTING"] = True
    return app.test_client()


def snapshot(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM change_log ORDER BY id")]


def registered_as(monkeypatch, entry_id, subject="RATE REQUEST MIA - SAI"):
    """Outlook's copy of the registered mail (no archive in tests): an agent's own mail."""
    info = outlook_bridge.MailInfo(subject=subject, received_on="2026-11-16", received_at="2026-11-16 09:00",
                                   sender_address="ines@quillagents.example", sender_name="Ines Calloway",
                                   to_addresses=["desk@jojofreight.example"], body_excerpt="Please quote air MIA to SAI.",
                                   entry_id=entry_id, conversation_id="CONV-" + entry_id)
    monkeypatch.setattr(outlook_bridge, "read_item", lambda entry_id, **kw: info)


# ------------------------------------------------------------ 1. the button --
def test_the_button_on_a_new_case_registered_from_a_mail(world):
    conn, repo, ids, mails = world
    case = new_case(repo, ids)
    page = client_of(ids).get("/enquiries/{}".format(case)).data.decode()
    assert ">Acknowledge</button>" in page and "Draft with Claude" not in page
    assert 'action="/enquiries/{}/acknowledge"'.format(case) in page


def test_no_button_when_quoted_acknowledged_or_typed_and_the_acknowledged_line_stays(world):
    conn, repo, ids, mails = world
    web = client_of(ids)
    quoted = new_case(repo, ids, entry="ENTRY-4002")
    repo.set_status(quoted, "Quoted", "2026-11-17")
    repo.commit()
    assert ">Acknowledge</button>" not in web.get("/enquiries/{}".format(quoted)).data.decode()
    acked = new_case(repo, ids, entry="ENTRY-4003")
    repo.add_ack(acked, "2026-11-17")
    repo.commit()
    page = web.get("/enquiries/{}".format(acked)).data.decode()
    assert ">Acknowledge</button>" not in page and "Acknowledged Nov 17" in page
    typed = new_case(repo, ids, entry=None, created_by="manual")
    page = web.get("/enquiries/{}".format(typed)).data.decode()
    assert ">Acknowledge</button>" not in page and 'class="ack-on' not in page


# ------------------------------------------------- 2. what it opens --
def test_a_blank_reply_all_with_the_numbered_subject_and_nothing_written(world, monkeypatch):
    conn, repo, ids, mails = world
    case = new_case(repo, ids, gross_kg=380, commodity="valve parts")
    mails["ENTRY-4001"] = FakeMail("RATE REQUEST MIA - SAI")
    registered_as(monkeypatch, "ENTRY-4001")
    before = snapshot(conn)
    page = client_of(ids).post("/enquiries/{}/acknowledge".format(case), follow_redirects=True).data.decode()
    assert "Acknowledgment opened in Outlook." in page
    [reply] = mails["ENTRY-4001"].replies
    assert reply.displayed and reply.Subject == "RE: [{}] RATE REQUEST MIA - SAI".format(opp(conn, case))
    assert not reply.written                                 # no text: her signature and the mail replied to
    assert reply.HTMLBody == "<html><head></head><body lang=EN-US>" + SIGNATURE + QUOTED + "</body></html>"
    assert not hasattr(outlook_bridge, "draft_reply_all")
    assert snapshot(conn) == before                          # nothing written to the case


def test_not_offered_opens_nothing(world, monkeypatch):
    conn, repo, ids, mails = world
    case = new_case(repo, ids)
    repo.set_status(case, "Quoted", "2026-11-17")
    repo.commit()
    mails["ENTRY-4001"] = FakeMail("RATE REQUEST MIA - SAI")
    registered_as(monkeypatch, "ENTRY-4001")
    page = client_of(ids).post("/enquiries/{}/acknowledge".format(case), follow_redirects=True).data.decode()
    assert "Acknowledge is for a New case" in page and mails["ENTRY-4001"].replies == []


def test_an_earlier_number_is_replaced_and_the_number_goes_in_once():
    assert ack.numbered_subject("RE: [OPP-2999] RATE REQUEST MIA - SAI", "OPP-4005") == \
        "RE: [OPP-4005] RATE REQUEST MIA - SAI"
    assert ack.numbered_subject("[OPP-4005] RE: RATE REQUEST", "OPP-4005") == "RE: [OPP-4005] RATE REQUEST"


# --------------------------------------------------------- 3. the template --
def test_an_air_case_with_gaps(world):
    conn, repo, ids, mails = world
    case = new_case(repo, ids, incoterm="EXW", commodity="valve parts")
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    # the questions the acknowledgment's paragraph asked, still read by wait.prefill
    assert ack.questions(conn, row) == [
        "the gross weight", "the pickup address", "the number of pieces", "the dimensions of each piece",
        "whether the cargo is dangerous goods (DG) or not", "the cargo ready date"]


def test_nothing_to_ask(world):
    conn, repo, ids, mails = world
    case = new_case(repo, ids, mode="FCL", container_type="40HQ", container_qty=2, gross_kg=18000,
                    commodity="ceramic tiles", incoterm="FOB")
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    assert ack.questions(conn, row) == []


def test_the_standard_questions_skip_what_the_case_has(world):
    conn, repo, ids, mails = world
    case = new_case(repo, ids, mode="FCL", container_type="20GP", container_qty=1, incoterm="DAP")
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    assert ack.questions(conn, row) == ["the delivery address", "the commodity", "the cargo weight per container"]


def _service(conn, repo, case, service):
    route = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? ORDER BY seq LIMIT 1",
                         (case,)).fetchone()[0]
    repo.edit_route(route, {"service_scope": service})
    repo.commit()


def test_a_warehousing_case(world):
    conn, repo, ids, mails = world
    case = new_case(repo, ids, mode="FCL", container_type="40HQ", container_qty=3, commodity="furniture")
    _service(conn, repo, case, "Warehousing")
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    assert ack.questions(conn, row) == [
        "whether the cargo is dangerous goods (DG) or not", "the weight per container or pallet",
        "whether the containers stay loaded or are stripped into the warehouse", "the storage duration",
        "the inbound and outbound schedule", "the customs status of the cargo", "the final delivery locations"]


def test_a_customs_only_case(world):
    conn, repo, ids, mails = world
    case = new_case(repo, ids, mode="LCL", lcl_cbm=4.2, commodity="lamps")
    _service(conn, repo, case, "Customs only")
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    assert ack.questions(conn, row)[-4:] == [
        "the commodity and its HS codes", "the cargo value",
        "the documents: commercial invoice, packing list, AWB or B/L", "the ISF details and a POA"]


def test_the_template_box_is_gone_and_her_text_stays_stored(world):
    conn, repo, ids, mails = world
    db.set_setting(conn, "ack_template", "Hello {first_name},\n\nGot it: {request}.")
    conn.commit()
    web = client_of(ids)
    page = web.get("/settings").data.decode()
    assert 'name="ack_template"' not in page and "Acknowledgment template" not in page
    web.post("/settings", data={"ack_template": "Hi {first_name}, rate {rate}"})
    assert db.get_setting(conn, "ack_template") == "Hello {first_name},\n\nGot it: {request}."


def test_the_questions_live_in_one_place():
    assert [q.key for q in ack_questions.standard("Air", None, "FCA")] == \
        ["pieces", "dimensions", "weight", "commodity", "dg", "ready", "pickup"]
    assert [q.key for q in ack_questions.standard("Air", "Customs only", None)][-1] == "poa"
