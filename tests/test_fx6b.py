"""Batch FX6b (2026-11-16, no schema change): Update from Outlook reads whom
our own mails in an Outlook conversation went to. Search.conversation_of and
Search.hydrate resolve To and Cc on a mail of ours (and only ours); a mail of
ours whose recipients could not be read still counts as to the requester
when an address on its display line has the requester's domain.

A fake Outlook (items with Recipients, a conversation table); every company,
person, place and date here is invented; cases from OPP-9970, domains .test.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_enquiry, add_party
from opplist import db, outlook_bridge, propose, suggestions


# --------------------------------------------------------- a fake Outlook --
class Entry:
    def __init__(self, address, exchange):
        self.Type = "EX" if exchange else "SMTP"
        self.Address = address
        self._address = address

    def GetExchangeUser(self):
        return type("User", (), {"PrimarySmtpAddress": self._address})()


class Recipient:
    def __init__(self, address, name, kind):
        self.AddressEntry = Entry(address, address.endswith("@jojofreight.example"))
        self.Address = address
        self.Name = name
        self.Type = kind


class Recipients:
    def __init__(self, item, people):
        self._item, self._people = item, people

    @property
    def Count(self):
        self._item.recipient_reads += 1
        return len(self._people)

    def Item(self, index):
        return self._people[index - 1]


class Item:
    Class = outlook_bridge.OL_MAIL_ITEM

    def __init__(self, entry_id, at, sender, name, body, to=(), cc=(), display_to="", conversation=None):
        self.EntryID = entry_id
        self.Subject = "RE: RATE REQUEST // ORD - LIM"
        self.ReceivedTime = at
        self.ConversationID = "CONV-9970"
        self.ConversationTopic = "RATE REQUEST // ORD - LIM"
        ours = sender.endswith("@jojofreight.example")
        self.SenderName = name
        self.SenderEmailType = "EX" if ours else "SMTP"
        self.Sender = Entry(sender, ours)
        self.SenderEmailAddress = sender
        self.To = display_to
        self.CC = ""
        self.Body = body
        self.recipient_reads = 0
        self.Recipients = Recipients(self, [Recipient(a, a.split("@")[0], 1) for a in to]
                                     + [Recipient(a, a.split("@")[0], 2) for a in cc])
        self.Parent = type("Folder", (), {"FolderPath": "\\\\Mailbox\\Inbox"})()
        self._conversation = conversation

    def GetConversation(self):
        return self._conversation


class Table:
    def __init__(self, ids):
        self._ids = list(ids)

    @property
    def EndOfTable(self):
        return not self._ids

    def GetNextRow(self):
        entry_id = self._ids.pop(0)
        return type("Row", (), {"Item": lambda self, name: entry_id})()


class Conversation:
    def __init__(self, ids):
        self._ids = ids

    def GetTable(self):
        return Table(self._ids)


class Session:
    def __init__(self, items):
        self.items = {i.EntryID: i for i in items}

    def GetItemFromID(self, entry_id):
        return self.items[entry_id]


def search_over(items):
    s = outlook_bridge.Search()
    s._app = type("App", (), {"Session": Session(items)})()
    return s


def thread(*, quote_to=("rosa@quillmarsh.test",), extra=()):
    ids = ["A1", "Q2"] + [i.EntryID for i in extra]
    conv = Conversation(ids)
    ask = Item("A1", dt.datetime(2027, 7, 1, 9, 0), "rosa@quillmarsh.test", "Rosa Quill",
               "Please quote 2 pallets ORD to LIM.", to=["desk@jojofreight.example"], display_to="JJC-DESK", conversation=conv)
    quote = Item("Q2", dt.datetime(2027, 7, 2, 11, 45), "dorian@jojofreight.example", "JJC-DORIAN VALE",
                 "Hello Rosa,\nPlease find below rate quote.\nAF USD 3.40/kg", to=list(quote_to),
                 cc=["desk@jojofreight.example"], display_to="Rosa Quill; JJC-DESK", conversation=conv)
    return [ask, quote] + list(extra)


def selected(item):
    """The selected mail, read in full as read_selection reads it."""
    return outlook_bridge._read_item(item)


# ------------------------------------------------------------- the world --
@pytest.fixture
def world(tmp_path):
    conn = db.open_db(tmp_path / "fx6b.db")
    repo = db.Repository(conn, source="test")
    repo.insert("country", {"name": "USA", "region": "North America", "iso2": "US"})
    agent = add_party(repo, "Quillmarsh Cargo")
    repo.insert("party_domain", {"domain": "quillmarsh.test", "party_id": agent})
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    case_id = add_enquiry(repo, "OPP-9970", agent, received="2027-07-01", scope="In")
    repo.commit()
    yield {"conn": conn, "repo": repo, "case": conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case_id,)).fetchone()}
    conn.close()


# ---------------------------------------------------------------- point 1 --
def test_our_quote_read_from_the_conversation_is_quoted(world):
    w = world
    items = thread()
    search = search_over(items)
    made = suggestions.from_mail(w["repo"], w["case"], selected(items[0]), search)
    row = w["conn"].execute("SELECT * FROM suggestion WHERE id = ?", (made[0],)).fetchone()
    assert (row["kind"], row["proposed_status"], row["evidence_quote"]) == ("status", "Quoted",
                                                                         "Please find below rate quote.")
    quote = [m for m in search.conversation_of(selected(items[0])) if m.entry_id == "Q2"][0]
    assert quote.to_addresses == ["rosa@quillmarsh.test"] and quote.cc_addresses == ["desk@jojofreight.example"]


def test_a_mail_of_ours_to_colleagues_only_is_not_our_quote(world):
    w = world
    items = thread(quote_to=("mira@jojofreight.example",))
    made = suggestions.from_mail(w["repo"], w["case"], selected(items[0]), search_over(items))
    kinds = [r["kind"] for r in w["conn"].execute("SELECT kind FROM suggestion WHERE id IN ({})".format(
        ",".join(str(i) for i in made) or "0"))]
    assert "status" not in kinds


def test_the_others_recipients_are_not_read(world):
    items = thread()
    search = search_over(items)
    search.conversation_of(outlook_bridge._read_item(items[0], light=True))
    assert items[0].recipient_reads == 0 and items[1].recipient_reads == 1


def test_hydrate_resolves_a_light_mail_of_ours_and_leaves_the_others():
    items = thread()
    search = search_over(items)
    light = [outlook_bridge._read_item(i, light=True) for i in items]
    assert all(m.light and not m.to_addresses for m in light)
    out = search.hydrate(light)
    assert out[1].to_addresses == ["rosa@quillmarsh.test"] and out[1].body_excerpt.startswith("Hello Rosa")
    assert out[0].to_addresses == [] and out[0].body_excerpt.startswith("Please quote")
    assert items[0].recipient_reads == 0


# ---------------------------------------------------------------- point 2 --
def _ours(display_to):
    return outlook_bridge.MailInfo(subject="RE: RATE REQUEST", received_on="2027-07-02", entry_id="Q9",
                                   sender_name="JJC-DORIAN VALE", sender_address="dorian@jojofreight.example",
                                   sender_internal=True, display_to=display_to,
                                   body_excerpt="Please find below rate quote.")


def test_no_recipients_readable_an_address_on_the_display_line_counts():
    m = propose.to_thread_mail(_ours("Rosa Quill <rosa@quillmarsh.test>; JJC-DESK"), {"jojofreight.example"},
                               {"quillmarsh.test"})
    assert m.to_requester is True
    p = propose.classify([m], "Quillmarsh Cargo", requester_known=True)
    assert p.proposed_status == "Quoted"


def test_names_alone_on_the_display_line_never_count():
    m = propose.to_thread_mail(_ours("Rosa Quill; JJC-DESK"), {"jojofreight.example"}, {"quillmarsh.test"})
    assert m.to_requester is False
    assert propose.classify([m], "Quillmarsh Cargo", requester_known=True).proposed_status != "Quoted"
