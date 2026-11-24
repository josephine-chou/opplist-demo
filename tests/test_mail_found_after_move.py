"""Batch M2: a mail is found after it is moved, also after the morning
sync; Ask for feedback looks at who wrote last; the draft text is Calibri 11.
FX4 (2026-11-12): Acknowledge, Ask for feedback and every draft are gone;
Open in Outlook still finds the moved mail.

A fake Outlook with two stores, a folder tree and Items.Find, and a test
mail archive. Every company, person, place, id and number here is
invented; cases from OPP-6001.
"""

from __future__ import annotations

import contextlib
import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import ack, case_mail, case_numbers, db, mail_archive, outlook_bridge, parsing
from opplist.web import create_app
from test_acknowledge import FakeReply
from test_register_v3 import fields_of

AGENT = ("pell@corvidcargo.example", "Pell Arden")
US = ("desk@jojofreight.example", "JJC-DESK")
SUBJECT = "RATE REQUEST // TACOMA - CALLAO // AIR"
INBOX = "\\Mailbox - Desk\\Inbox"
AGENTS = "\\Mailbox - Desk\\Inbox\\AGENTS"
SENT = "\\Mailbox - Desk\\Sent Items"


def info(mid, at, sender=AGENT, internal=False, subject=SUBJECT, folder=INBOX, entry=None, conv="CONV-6001",
         to=US, body="Hi,\nPlease quote air Tacoma - Callao, 4 crates.\nPell"):
    return outlook_bridge.MailInfo(
        subject=subject, received_on=at[:10], received_at=at, entry_id=entry or "EID-" + mid, conversation_id=conv,
        conversation_topic=parsing.clean_subject(subject), sender_name=sender[1], sender_address=sender[0],
        sender_internal=internal, to_addresses=[to[0]], to_names=[to[1]], body_excerpt=body, folder_path=folder,
        display_to=to[0], message_id="<{}@x>".format(mid))


# ------------------------------------------------------------ fake Outlook --
class Item:
    Class = outlook_bridge.OL_MAIL_ITEM

    def __init__(self, mid, subject=SUBJECT, sender="Pell Arden"):
        self.mid, self.Subject, self.SenderName = mid, subject, sender
        self.displayed, self.replies = False, []

    def Display(self):
        self.displayed = True

    def ReplyAll(self):
        reply = FakeReply(self.Subject)
        self.replies.append(reply)
        return reply

    def Send(self):
        pytest.fail("the mail was sent")


class Collection:
    def __init__(self, items):
        self.items = list(items)

    @property
    def Count(self):
        return len(self.items)

    def Item(self, key):
        if isinstance(key, int):
            return self.items[key - 1]
        for item in self.items:
            if item.Name == key:
                return item
        raise RuntimeError("no folder " + key)


class Folder:
    def __init__(self, name, children=(), mails=()):
        self.Name = name
        self.Folders = Collection(children)
        folder = self

        class Items:
            @staticmethod
            def Find(criteria):
                for mail in folder.mails:
                    if "'{}'".format(mail.mid) in criteria:
                        return mail
                return None
        self.mails = list(mails)
        self.Items = Items


class Store:
    def __init__(self, store_id, name):
        self.StoreID, self.DisplayName = store_id, name


class Outlook:
    """ids: {(entry_id, store_id or None): Item}; the folder tree holds the
    mails Items.Find can see."""

    def __init__(self):
        self.ids = {}
        self.agents = Folder("AGENTS")
        self.inbox = Folder("Inbox", [self.agents])
        self.root = Folder("Mailbox - Desk", [self.inbox, Folder("Sent Items")])
        app = self

        class Session:
            Stores = Collection([Store("STORE-MBX", "Mailbox - Desk"), Store("STORE-PST", "Archive 2026")])
            Folders = Collection([app.root])

            @staticmethod
            def GetItemFromID(entry_id, store_id=None):
                if (entry_id, store_id) in app.ids:
                    return app.ids[(entry_id, store_id)]
                raise RuntimeError("The message you specified cannot be found")
        self.Session = Session


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "m2.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    corvid = add_party(repo, "Corvid Cargo", country="Chile")
    repo.insert("party_domain", {"domain": "corvidcargo.example", "party_id": corvid})
    db.set_setting(conn, "next_opp_number", 6001)
    repo.commit()
    outlook = Outlook()
    monkeypatch.setattr(outlook_bridge, "_com", contextlib.nullcontext)
    monkeypatch.setattr(outlook_bridge, "_application", lambda: outlook)
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    app = create_app(path)
    app.config["TESTING"] = True
    yield {"conn": conn, "repo": repo, "corvid": corvid, "outlook": outlook, "archive": archive,
           "client": app.test_client()}
    conn.close()


def store(w, *mails):
    conn = mail_archive.open_archive(w["archive"])
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in mails])
    mail_archive.set_meta(conn, "last_sync_at", "2026-11-10 08:19:00")
    conn.commit()
    conn.close()


def case(w, opp="OPP-6001", entry="EID-REG", received="2026-11-02", **extra):
    values = dict(received=received, entry_id=entry, conversation_id="CONV-6001", source_subject=SUBJECT,
                  created_by="outlook", service_mode="Air", origin_country="USA", dest_country="Chile")
    values.update(extra)
    out = add_enquiry(w["repo"], opp, w["corvid"], **values)
    w["repo"].commit()
    return out


def row_of(w, case_id):
    return w["conn"].execute("SELECT * FROM v_enquiry WHERE id = ?", (case_id,)).fetchone()


def resolve(w, case_id):
    with mail_archive.ArchiveSearch(w["archive"]) as search:
        mail, step = case_mail.resolve_registered(w["conn"], row_of(w, case_id), search)
        return (mail.message_id if mail else None), step


# ------------------------------------------------------------ 1. the resolver --
def test_a_the_entry_id_or_a_move_the_archive_recorded(world):
    w = world
    store(w, info("m1", "2026-11-02 09:00", entry="EID-REG"))
    assert resolve(w, case(w)) == ("<m1@x>", "a")
    # filed into AGENTS: the archive saw it there under a new id, and keeps the old one
    store(w, info("m1", "2026-11-02 09:00", folder=AGENTS, entry="EID-FILED"))
    other = case(w, "OPP-6002", entry="EID-REG")
    assert resolve(w, other) == ("<m1@x>", "a")


def test_b_the_message_id_the_case_keeps(world):
    w = world
    store(w, info("m1", "2026-11-02 09:00", folder=AGENTS, entry="EID-FILED"))
    c = case(w, entry="EID-NEVER-SEEN", conversation_id="CONV-OTHER")
    assert resolve(w, c) == (None, "")
    case_mail.remember_registered(w["repo"], c, "<m1@x>", "2026-11-02")
    w["repo"].commit()
    assert resolve(w, c) == ("<m1@x>", "b")


def test_c_the_registered_subject_in_the_conversation(world):
    w = world
    store(w, info("m1", "2026-11-02 09:00", folder=AGENTS, entry="EID-FILED"),
          info("m0", "2026-11-01 16:00", subject="RE: " + SUBJECT),
          info("m2", "2026-11-03 10:00", sender=US, internal=True, subject="RE: " + SUBJECT, to=AGENT))
    assert resolve(w, case(w, entry="EID-NEVER-SEEN")) == ("<m1@x>", "c")
    # two mails of that subject on the day: the earliest of them
    store(w, info("m3", "2026-11-02 08:10", subject="  rate request // tacoma -  callao // air"))
    assert resolve(w, case(w, "OPP-6002", entry="EID-NEVER-SEEN-2")) == ("<m3@x>", "c")
    # none on the day: the earliest after it
    assert resolve(w, case(w, "OPP-6003", entry="EID-NEVER-SEEN-3", received="2026-11-01")) == ("<m3@x>", "c")
    # no mail with the registered subject: nothing
    assert resolve(w, case(w, "OPP-6004", entry="EID-NEVER-SEEN-4", source_subject="QUOTE TACOMA")) == (None, "")


# ------------------------------------------- 2. the buttons use the resolved id --
def test_open_in_outlook_and_acknowledge_use_the_resolved_mail(world):
    w = world
    store(w, info("m1", "2026-11-02 09:00", folder=AGENTS, entry="EID-FILED"))
    c = case(w, entry="EID-NEVER-SEEN")
    moved = Item("<m1@x>")
    w["outlook"].ids[("EID-FILED", None)] = moved
    page = w["client"].post("/enquiries/{}/open-in-outlook".format(c), follow_redirects=True).data.decode()
    assert "Opened in Outlook." in page and moved.displayed
    # FX5: Acknowledge (blank) replies to the same mail, after its move
    w["client"].post("/enquiries/{}/acknowledge".format(c))
    assert len(moved.replies) == 1 and moved.replies[0].displayed
    # the Mail card tags it as the registered mail
    assert "registered from" in w["client"].get("/enquiries/{}".format(c)).data.decode()


# --------------------------------------------- 3. Outlook finds a moved mail --
def test_another_store_then_the_message_id_in_a_candidate_folder_then_the_message(world):
    w = world
    store(w, info("m1", "2026-11-02 09:00"), info("m9", "2026-10-20 09:00", folder=AGENTS, conv="CONV-OLD",
                                                  subject="RATE REQUEST // OLYMPIA - LIMA"))
    c = case(w, entry="EID-m1")
    in_pst = Item("<m1@x>")
    w["outlook"].ids[("EID-m1", "STORE-PST")] = in_pst
    w["client"].post("/enquiries/{}/open-in-outlook".format(c))
    assert in_pst.displayed
    # moved after the sync: only its Message-ID in AGENTS (where the requester's mails are) finds it
    w["outlook"].ids.clear()
    after_sync = Item("<m1@x>")
    w["outlook"].agents.mails.append(after_sync)
    with mail_archive.ArchiveSearch(w["archive"]) as search:
        folders = case_mail.candidate_folders(w["conn"], row_of(w, c), search, today=None)
    assert folders[-1] == INBOX and AGENTS in folders       # the archive knows no Sent Items here
    w["client"].post("/enquiries/{}/open-in-outlook".format(c))
    assert after_sync.displayed
    # nowhere at all
    w["outlook"].agents.mails.clear()
    page = w["client"].post("/enquiries/{}/open-in-outlook".format(c), follow_redirects=True).data.decode()
    assert "That mail is not where this morning&#39;s mail sync saw it" in page


# -------------------------------------------- 4. the Message-ID row is kept --
def test_registration_writes_the_message_id_row(world, monkeypatch):
    w = world
    mail = info("m1", "2026-11-02 09:00")
    mail.body_excerpt = "Hi,\nPlease quote air Tacoma - Callao, 4 crates, 120 kg.\nPell"
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: mail)
    body = w["client"].post("/enquiries/from-outlook").data.decode()
    fields = fields_of(body)
    assert fields["message_id"] == ["<m1@x>"]
    fields.update(origin_country=["USA"], dest_country=["Chile"], service_mode=["Air"], gross_kg=["120"],
                  gross_unit=["kg"], ack_after=["nothing"])
    saved = w["client"].post("/enquiries/new", data=fields)
    assert saved.status_code == 302, re.sub(r"<[^>]+>", " ", saved.data.decode())[:3000]
    rows = w["conn"].execute("SELECT kind, value, source, note FROM enquiry_thread").fetchall()
    assert [tuple(r) for r in rows] == [("message", "<m1@x>", "register", "registered mail")]
    page = w["client"].get("/enquiries/{}".format(
        w["conn"].execute("SELECT id FROM enquiry WHERE opp_no = 'OPP-6001'").fetchone()[0])).data.decode()
    assert "registered mail" not in page                               # not a Subjects line, not in More


def test_the_scan_adds_the_row_from_the_entry_id_and_never_from_the_subject(world):
    w = world
    store(w, info("m1", "2026-11-02 09:00", entry="EID-REG"),
          info("m2", "2026-11-04 09:00", folder=AGENTS, entry="EID-ELSE", conv="CONV-6002"))
    by_id = case(w, entry="EID-REG")
    by_subject = case(w, "OPP-6002", entry="EID-NEVER-SEEN", conversation_id="CONV-6002", received="2026-11-04")
    assert resolve(w, by_subject)[1] == "c"
    with mail_archive.ArchiveSearch(w["archive"]) as search:
        report = case_numbers.scan(w["conn"], w["repo"], search)
    w["repo"].commit()
    assert report.remembered == 1
    assert case_mail.registered_message_id(w["conn"], by_id) == "<m1@x>"
    assert case_mail.registered_message_id(w["conn"], by_subject) == ""


# --------------------------------------- 5. the title card's Open in Outlook --
def test_open_falls_back_to_the_thread_s_newest_mail_and_is_absent_with_neither(world):
    w = world
    store(w, info("m5", "2026-11-05 11:00", subject="RE: [OPP-6001] " + SUBJECT, conv="CONV-6001"))
    c = case(w, entry="", source_subject="")            # typed in, its thread attached by conversation
    title = 'action="/enquiries/{}/open-in-outlook"'
    page = w["client"].get("/enquiries/{}".format(c)).data.decode()
    assert title.format(c) in page
    newest = Item("<m5@x>")
    w["outlook"].ids[("EID-m5", None)] = newest
    w["client"].post("/enquiries/{}/open-in-outlook".format(c))
    assert newest.displayed
    # MF1: a registered mail the archive does not have is still opened, by
    # the case's own entry id through Outlook; with neither, no button
    lone = case(w, "OPP-6002", entry="EID-GONE", conversation_id="CONV-NONE", source_subject="QUOTE OLYMPIA")
    assert title.format(lone) in w["client"].get("/enquiries/{}".format(lone)).data.decode()
    none = case(w, "OPP-6003", entry="", conversation_id="CONV-NONE", source_subject="QUOTE OLYMPIA")
    assert title.format(none) not in w["client"].get("/enquiries/{}".format(none)).data.decode()


# ------------------------------------------------- 6. Ask for feedback --
def test_ask_for_feedback_is_gone_whoever_wrote_last(world):
    w = world
    store(w, info("m1", "2026-11-02 09:00"),
          info("f1", "2026-11-09 09:00", sender=US, internal=True, subject="RE: [OPP-6001] " + SUBJECT, to=AGENT,
               body="Hi Pell,\nChecking back about our offer.\nRegards"),
          info("n1", "2026-11-09 15:00", subject="RE: [OPP-6001] " + SUBJECT,
               body="Hi,\nThe client chose another forwarder.\nPell"))
    c = case(w, entry="EID-m1")
    w["repo"].set_status(c, "Lost", "2026-11-09", reason="Went with another forwarder")
    w["repo"].commit()
    assert ">Ask for feedback</button>" not in w["client"].get("/enquiries/{}".format(c)).data.decode()
    assert w["client"].post("/enquiries/{}/feedback".format(c)).status_code == 404
    store(w, info("t1", "2026-11-09 16:30", sender=US, internal=True, subject="RE: [OPP-6001] " + SUBJECT, to=AGENT,
                  body="Hi Pell,\nThanks for letting us know.\nRegards"))
    assert ">Ask for feedback</button>" not in w["client"].get("/enquiries/{}".format(c)).data.decode()


# 7. Calibri 11: the draft text went with the drafts (FX4)
