"""The mail archive: storing, searching it the way Outlook is searched, the
keyword search that replaces Search-Mail.ps1, and the sync loop over a
stand-in Outlook. No real Outlook, no real archive."""

from __future__ import annotations

import csv
import datetime as dt

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import mail_archive, outlook_bridge, paths, propose
from opplist.outlook_bridge import FolderInfo, MailInfo
from test_propose import FakeFolder, FakeMail, FakeSearch

NOW = dt.datetime(2026, 9, 14, 10, 0, 0)


def info(subject, at="2026-08-14 09:30", sender="renata@tolvex.example", name="Renata",
         body="", folder="\\Personal Folder\\TOLVEX", conversation="CONV-1",
         topic="", to=("VictorLang@jojofreight.example",), cc=(), entry="", message_id="",
         internal=False, display_to=""):
    return MailInfo(
        subject=subject, received_on=at[:10], received_at=at, entry_id=entry or
        "EID-{}-{}".format(at, subject)[:60], conversation_id=conversation,
        conversation_topic=topic or subject, sender_name=name, sender_address=sender,
        sender_internal=internal, to_addresses=list(to), cc_addresses=list(cc),
        body_excerpt=body, folder_path=folder, display_to=display_to,
        message_id=message_id or "<{}-{}@x>".format(at.replace(" ", "T"), subject[:20]))


@pytest.fixture
def archive(tmp_path):
    conn = mail_archive.open_archive(tmp_path / "mail.db")
    yield conn
    conn.close()


def load(conn, mails, now=NOW):
    records = [mail_archive.record_from_info(m) for m in mails]
    out = mail_archive.store(conn, records, now=now)
    conn.commit()
    return out


# ----------------------------------------------------------------- storing --
def test_a_new_mail_is_inserted_and_a_moved_one_only_changes_its_place(archive):
    mail = info("Kelmora lamps quote", body="please proceed\n\nFrom: x\nold", entry="E1")
    assert load(archive, [mail]) == (1, 0)
    moved = info("Kelmora lamps quote", body="CHANGED", entry="E2",
                 folder="\\Personal Folder\\TOLVEX\\OPP-1307")
    assert load(archive, [moved], now=NOW + dt.timedelta(days=1)) == (0, 1)
    rows = archive.execute("SELECT * FROM message").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["entry_id"] == "E2"
    assert row["folder_path"] == "\\Personal Folder\\TOLVEX\\OPP-1307"
    assert row["body"].startswith("please proceed")           # the body is kept
    assert row["body_top"] == "please proceed"                 # quote cut off
    assert row["first_seen_at"] == "2026-09-14 10:00:00"
    assert row["last_seen_at"] == "2026-09-15 10:00:00"
    assert row["to_addresses"] == '["VictorLang@jojofreight.example"]'


def test_a_mail_without_a_message_id_is_keyed_on_its_entry_id(archive):
    a = info("No header", entry="E9", message_id=" ")
    a.message_id = ""
    assert load(archive, [a]) == (1, 0)
    assert load(archive, [a]) == (0, 1)
    b = info("No header", entry="E10", message_id=" ")
    b.message_id = ""
    assert load(archive, [b]) == (1, 0)                        # a different mail
    none = info("Nothing to key on", entry="", message_id=" ")
    none.message_id = ""
    none.entry_id = ""
    assert load(archive, [none]) == (0, 0)


def test_the_full_text_index_follows_the_table(archive):
    load(archive, [info("Pre alert MAWB 057-3309 2174", body="LFD 9/1")])
    found = archive.execute(
        "SELECT rowid FROM message_fts WHERE message_fts MATCH ?",
        (mail_archive.fts_query("057-33092174"),)).fetchall()
    assert len(found) == 1


def test_top_part_stops_at_the_quoted_mail():
    body = "\n\nThanks, please proceed.\n\nRegards\n\nFrom: Renata\nSent: x\nold text"
    assert mail_archive.top_part(body) == "Thanks, please proceed.\n\nRegards"
    body = "ok\nOn Mon, Aug 31, 2026 at 9:00 AM Someone <s@x.example> wrote:\n> quoted"
    assert mail_archive.top_part(body) == "ok"
    assert mail_archive.top_part("") == ""


# ---------------------------------------------------------------- reading --
class FakeProperty:
    def __init__(self, values):
        self._values = values

    def GetProperty(self, tag):
        if tag not in self._values:
            raise RuntimeError("no such property")
        return self._values[tag]


class FakeAttachment:
    def __init__(self, name):
        self.FileName = name


class FakeCollection:
    def __init__(self, items):
        self._items = list(items)
        self.Count = len(self._items)

    def Item(self, index):
        return self._items[index - 1]


class FakeRecipient:
    def __init__(self, smtp, kind=1, dn=None):
        self.Type = kind
        self.Address = dn or smtp
        self.AddressEntry = None
        self.PropertyAccessor = FakeProperty({mail_archive.PR_SMTP_ADDRESS: smtp})


class Stamp:
    def __init__(self, *parts):
        self.year, self.month, self.day, self.hour, self.minute, self.second = parts


class FakeItem:
    Class = outlook_bridge.OL_MAIL_ITEM

    def __init__(self, subject, message_id="<m1@x>", sender="ana@tolvex.example",
                 received=(2026, 9, 4, 9, 30, 15), body="hello\n\nFrom: old",
                 recipients=(), attachments=(), entry="ENTRY-1", conversation="CONV-9"):
        self.Subject = subject
        self.SenderName = "Ana"
        self.SenderEmailType = "SMTP"
        self.SenderEmailAddress = sender
        self.Sender = None
        self.To = "Victor Lang"
        self.CC = "Iris"
        self.EntryID = entry
        self.ConversationID = conversation
        self.ConversationTopic = subject
        self.Body = body
        self.ReceivedTime = Stamp(*received)
        self.SentOn = Stamp(2026, 9, 4, 9, 29, 0)
        self.Recipients = FakeCollection(recipients)
        self.Attachments = FakeCollection([FakeAttachment(a) for a in attachments])
        self.PropertyAccessor = FakeProperty(
            {outlook_bridge.PR_INTERNET_MESSAGE_ID: message_id} if message_id else {})


def test_an_outlook_item_is_read_whole():
    item = FakeItem("RATE REQUEST", recipients=[FakeRecipient("VictorLang@jojofreight.example"),
                                                FakeRecipient("jo@jojofreight.example", kind=2)],
                    attachments=["quote.pdf"])
    record = mail_archive.record_from_item(item, "\\Mailbox\\Inbox")
    assert record.message_id == "<m1@x>"
    assert record.received_at == "2026-09-04 09:30:15"
    assert record.sent_at == "2026-09-04 09:29:00"
    assert record.to_addresses == ["VictorLang@jojofreight.example"]
    assert record.cc_addresses == ["jo@jojofreight.example"]
    assert record.display_to == "Victor Lang; Iris"
    assert record.has_attachments and record.attachment_names == ["quote.pdf"]
    assert record.store == "Mailbox"
    assert record.key == ("message_id", "<m1@x>")


def test_an_item_without_a_message_id_falls_back_to_its_entry_id():
    record = mail_archive.record_from_item(FakeItem("x", message_id=""), "\\M\\Inbox")
    assert record.key == ("entry_id", "ENTRY-1")


# ------------------------------------------------------------ the search --
MAILS = [
    info("RATE REQUEST // Kelmora lamps MIA - SAI", at="2026-08-13 09:00",
         body="please quote\n\nFrom: x", entry="E1", message_id="<1@x>"),
    info("RE: RATE REQUEST // Kelmora lamps MIA - SAI", at="2026-08-14 11:00",
         sender="VictorLang@jojofreight.example", name="JJC-VICTOR LANG", internal=True,
         to=("renata@tolvex.example",), display_to="Renata Vidal",
         body="Please find our rates attached.\n\nFrom: Renata", entry="E2",
         message_id="<2@x>", folder="\\Mailbox\\Sent Items"),
    info("RE: RATE REQUEST // Kelmora lamps MIA - SAI", at="2026-08-20 15:00",
         body="Thanks, please proceed with the booking.\n\nFrom: Victor",
         entry="E3", message_id="<3@x>"),
    info("Other business // Chicago", at="2026-08-15 10:00", sender="ana@seavio.example",
         name="Ana", conversation="CONV-2", body="unrelated", entry="E4",
         message_id="<4@x>", folder="\\Personal Folder\\SEAVIO"),
    info("Old thread // Kelmora lamps", at="2026-03-01 10:00", conversation="",
         topic="Old thread // Kelmora lamps", body="ancient", entry="E5",
         message_id="<5@x>", folder="\\Personal Folder\\TOLVEX\\OPP-1290 (Old)"),
    info("Old thread // Kelmora lamps", at="2026-04-15 10:00", conversation="",
         topic="Old thread // Kelmora lamps", body="still old", entry="E6",
         message_id="<6@x>", folder="\\Personal Folder\\TOLVEX\\OPP-1290 (Old)"),
]


@pytest.fixture
def searched(tmp_path):
    path = tmp_path / "mail.db"
    conn = mail_archive.open_archive(path)
    load(conn, MAILS)
    conn.execute("INSERT INTO folder_sync(folder_path, position) VALUES (?, ?)",
                 ("\\Mailbox\\Inbox", 0))
    conn.execute("INSERT INTO folder_sync(folder_path, position) VALUES (?, ?)",
                 ("\\Personal Folder\\TOLVEX", 3))
    conn.commit()
    conn.close()
    with mail_archive.ArchiveSearch(path) as search:
        yield search


def test_folders_come_from_the_sync_table_and_the_messages(searched):
    paths = [f.path for f in searched.folders()]
    assert paths[0] == "\\Mailbox\\Inbox"
    assert "\\Personal Folder\\SEAVIO" in paths
    assert "\\Personal Folder\\TOLVEX\\OPP-1290 (Old)" in paths
    assert [f.path for f in searched.case_folders("OPP-1290")] == \
        ["\\Personal Folder\\TOLVEX\\OPP-1290 (Old)"]
    assert searched.folders_named("seavio")[0].path == "\\Personal Folder\\SEAVIO"
    ranked = searched.folders_for(hints=["tolvex"], opp_no="OPP-1290")
    assert ranked[0].path == "\\Personal Folder\\TOLVEX\\OPP-1290 (Old)"


def test_in_folder_and_hydrate(searched):
    found = searched.in_folder(FolderInfo("\\Personal Folder\\TOLVEX\\OPP-1290 (Old)"))
    assert [m.entry_id for m in found] == ["E5", "E6"]
    assert found[0].body_excerpt == "ancient" and found[0].light is False
    assert searched.hydrate(found) == found


def test_by_subject_is_a_case_insensitive_substring(searched):
    found = searched.by_subject("kelmora")
    assert [m.entry_id for m in found] == ["E5", "E6", "E1", "E2", "E3"]
    assert searched.by_subject("KELMORA") == found                 # cached, same list
    assert searched.by_subject("") == [] and searched.by_subject("o'neil") == []
    assert searched.by_subject("100%") == []                        # LIKE escaped
    assert searched.cut_short is False


def test_by_domains_reads_from_to_cc_and_display_names(searched):
    found = searched.by_domains(["tolvex.example"], "2026-08-01", "2026-08-31")
    assert [m.entry_id for m in found] == ["E1", "E2", "E3"]      # E2: we wrote to them
    assert all(m.header_match for m in found)
    assert [m.entry_id for m in searched.by_domains(["tolvex.example"], "2026-08-14", "2026-08-14")] == ["E2"]
    assert searched.by_domains([]) == []
    # a mail we sent to them, seen only through the display name
    assert [m.entry_id for m in searched.by_domains(["vidal"], "2026-08-01", "2026-08-31")] == ["E2"]


def test_conversation_of_by_id_and_by_topic(searched):
    first = searched.by_conversation("CONV-1")[0]
    thread = searched.conversation_of(first)
    assert [m.entry_id for m in thread] == ["E1", "E2", "E3"]
    assert thread[0] is first                                     # itself, once
    # the live mail was moved after the sync: a new entry id, same message
    live = MailInfo(subject=first.subject, received_at=first.received_at,
                    received_on=first.received_on, entry_id="E1-MOVED",
                    conversation_id="CONV-1", sender_address=first.sender_address)
    thread = searched.conversation_of(live)
    assert [m.entry_id for m in thread] == ["E1-MOVED", "E2", "E3"]
    assert [m.entry_id for m in searched.later_in_conversation(live)] == ["E2", "E3"]
    # no conversation id: the topic within 90 days
    old = searched.in_folder(FolderInfo("\\Personal Folder\\TOLVEX\\OPP-1290 (Old)"))[0]
    assert [m.entry_id for m in searched.conversation_of(old)] == ["E5", "E6"]
    far = MailInfo(subject="Old thread // Kelmora lamps",
                   conversation_topic="Old thread // Kelmora lamps",
                   received_at="2026-07-01 10:00", received_on="2026-07-01", entry_id="E7")
    assert [m.entry_id for m in searched.conversation_of(far)] == ["E6", "E7"]


def test_the_search_needs_an_archive(tmp_path):
    with pytest.raises(mail_archive.ArchiveError):
        with mail_archive.ArchiveSearch(tmp_path / "missing.db"):
            pass


# ------------------------------------------------- propose, either source --
@pytest.fixture
def case(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_enquiry(repo, "OPP-1307", tolvex, received="2026-08-13",
                status="New", status_confirmed=0, commodity="Kelmora lamps",
                origin_loc="MIA", dest_loc="SAI", service_mode="Air")
    repo.commit()
    return repo.conn


def _fake_mails():
    return [
        FakeMail("2026-08-13", "Renata", "renata@tolvex.example",
                 subject="RATE REQUEST // Kelmora lamps MIA - SAI", body="please quote"),
        FakeMail("2026-08-14", "JJC-VICTOR LANG", "VictorLang@jojofreight.example", internal=True,
                 subject="RE: RATE REQUEST // Kelmora lamps MIA - SAI",
                 body="Please find our rates attached.", recipients=("renata@tolvex.example",)),
        FakeMail("2026-08-20", "Renata", "renata@tolvex.example",
                 subject="RE: RATE REQUEST // Kelmora lamps MIA - SAI",
                 body="Thanks, please proceed with the booking."),
    ]


def _as_info(fake, n):
    return info(fake.subject, at=fake.received_on + " 09:0{}".format(n),
                sender=fake.sender_address, name=fake.sender_name, body=fake.body_excerpt,
                internal=fake.sender_internal, to=tuple(fake.recipients),
                entry="E{}".format(n), message_id="<p{}@x>".format(n),
                conversation=fake.conversation_id, folder=fake.folder_path)


def test_propose_reads_the_same_answer_from_outlook_and_from_the_archive(case, tmp_path):
    fakes = _fake_mails()
    fake = FakeSearch(by_domains={"tolvex.example": fakes},
                      by_conversation={"CONV-1": fakes},
                      by_subject={"kelmora": fakes})
    expected = propose.propose(case, fake, opp_nos=["OPP-1307"])

    path = tmp_path / "mail.db"
    conn = mail_archive.open_archive(path)
    load(conn, [_as_info(f, n) for n, f in enumerate(fakes, 1)])
    conn.close()
    with mail_archive.ArchiveSearch(path) as search:
        got = propose.propose(case, search, opp_nos=["OPP-1307"])

    assert len(expected) == len(got) == 1
    for field in ("opp_no", "proposed_status", "proposed_date", "closed_reason",
                  "evidence_quote", "evidence_date", "confidence"):
        assert getattr(got[0], field) == getattr(expected[0], field), field
    assert got[0].proposed_status == "Won"
    assert got[0].found_by.startswith("requester domain tolvex.example")


# -------------------------------------------------------- keyword search --
AWB_MAILS = [
    info("FW: PRE ALERT // MAWB 057-3309 2174 // HAWB NWLQZ70316",
         at="2026-08-31 16:43", sender="AvaT@jojofreight.example", name="JJC-AVA TRAN",
         body="Cargo is available at the CFS, please collect by the LFD\n\nLFD: 9/1, gate closes 16:00\n\n"
              "From: JJC-AVA TRAN\nSent: Monday\nold quoted part",
         entry="A1", message_id="<a1@x>", folder="\\Personal Folder\\CLIENTS\\HOLLOWAY",
         display_to="Mira Castell"),
    info("DELIVERY ORDER // T.M 176-51820943", at="2026-09-01 15:25", sender="AvaT@jojofreight.example",
         body="Release to the trucker named on the DO; same consignment as 05733092174.\nTel: 555-0100",
         entry="A2", message_id="<a2@x>", folder="\\Personal Folder\\CLIENTS\\HOLLOWAY"),
    info("Nothing here", at="2026-09-02 10:00", body="057-33092175 belongs to another consignment",
         entry="A3", message_id="<a3@x>"),
    info("Too old 057-33092174", at="2026-01-02 10:00", body="x",
         entry="A4", message_id="<a4@x>"),
    info("PO 4471", at="2026-09-03 10:00", body="see PO 4471 and PO 44712",
         entry="A5", message_id="<a5@x>"),
]


def test_awb_numbers_match_loosely_and_short_terms_whole(tmp_path):
    conn = mail_archive.open_archive(tmp_path / "mail.db")
    load(conn, AWB_MAILS)
    result = mail_archive.search_terms(
        conn, ["057-33092174", "176-51820943", "4471"], days_back=60, now=NOW)
    conn.close()
    by_term = {t: [h.subject for h in result.by_term(t)] for t in result.terms}
    assert by_term["057-33092174"] == [
        "FW: PRE ALERT // MAWB 057-3309 2174 // HAWB NWLQZ70316",
        "DELIVERY ORDER // T.M 176-51820943"]                       # not A3, not A4
    assert by_term["176-51820943"] == ["DELIVERY ORDER // T.M 176-51820943"]
    assert by_term["4471"] == ["PO 4471"]
    hit = result.by_term("057-33092174")[0]
    assert hit.matched_in == "subject"
    assert hit.folder == "Personal Folder\\CLIENTS\\HOLLOWAY"        # no leading backslash
    assert hit.key_lines == "Cargo is available at the CFS, please collect by the LFD | LFD: 9/1, gate closes 16:00"
    assert hit.top.endswith("LFD: 9/1, gate closes 16:00")               # quoted part cut
    assert result.by_term("057-33092174")[1].matched_in == "body"
    assert result.candidates == 3 and len(result.hits) == 4


def test_the_search_files_look_like_the_powershell_ones(tmp_path):
    conn = mail_archive.open_archive(tmp_path / "mail.db")
    load(conn, AWB_MAILS)
    result = mail_archive.search_terms(conn, ["057-33092174", "zzz-none"], days_back=60,
                                       now=NOW, full_body=True)
    conn.close()
    written = mail_archive.write_search(result, tag="holloway awb", out_dir=tmp_path / "out",
                                        full_body=True, now=NOW)
    assert written["hits"].name == "holloway-awb-hits.csv"
    raw = written["hits"].read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                         # UTF-8 BOM
    assert b"\r\n" in raw and b"\n\n" not in raw
    lines = raw.decode("utf-8-sig").split("\r\n")
    assert lines[0] == '"Term","Date","Folder","SenderName","SenderEmail","To","Subject","MatchedIn","KeyLines"'
    assert lines[1].startswith('"057-33092174","2026-08-31 16:43","Personal Folder\\CLIENTS\\HOLLOWAY","JJC-AVA TRAN","AvaT@jojofreight.example","Mira Castell","FW: PRE ALERT')
    rows = list(csv.reader(lines[1:-1]))
    assert len(rows) == 2 and all(len(r) == 9 for r in rows)

    bodies = written["bodies"].read_bytes().decode("utf-8-sig")
    assert bodies.startswith("Search-Mail  2026-09-14 10:00  terms=2  days=60  hits=2\r\n\r\n" + "#" * 80)
    assert "TERM: 057-33092174   (2 mail(s))" in bodies
    assert "TERM: zzz-none   (0 mail(s))\r\n" + "#" * 80 + "\r\n  (no mail found)" in bodies
    assert "DATE    : 2026-08-31 16:43\r\nFROM    : JJC-AVA TRAN <AvaT@jojofreight.example>\r\n" \
           "TO      : Mira Castell\r\nSUBJECT : FW: PRE ALERT" in bodies
    assert "old quoted part" not in bodies                          # bodies: new part only

    full = written["full"].read_bytes().decode("utf-8-sig")
    assert full.startswith("Search-Mail full bodies  2026-09-14 10:00  mails=2\r\n" + "=" * 80)
    assert "TERMS   : 057-33092174\r\n" in full and "old quoted part" in full
    assert written["log"].exists()
    assert "--- SUMMARY ---" in written["log"].read_text(encoding="utf-8-sig")


def test_fts_queries_and_regexes():
    assert mail_archive.is_awb("057-33092174") and mail_archive.is_awb("057 3309 2174")
    assert not mail_archive.is_awb("NWLQZ70316") and not mail_archive.is_awb("05733092")
    assert mail_archive.fts_query("057-33092174") == \
        '"057 3309 2174" OR "057 33092174" OR "0573309 2174" OR "05733092174"'
    assert mail_archive.fts_query("Zenith Cargo") == '"Zenith Cargo"*'
    assert mail_archive.fts_query("4471") == '"4471"'
    assert mail_archive.fts_query("--") == ""
    assert mail_archive.term_regex("057-33092174").search("no 0573309-2174 yes")
    assert not mail_archive.term_regex("057-33092174").search("105733092174")


# ---------------------------------------------------------------- status --
def test_status_says_when_the_archive_cannot_be_trusted(tmp_path):
    path = tmp_path / "mail.db"
    assert mail_archive.status(path, now=NOW)["ok"] is False
    assert "no mail archive" in mail_archive.usable(path, now=NOW)[1]
    conn = mail_archive.open_archive(path)
    load(conn, MAILS[:2])
    assert mail_archive.status(path, now=NOW)["reason"] == "the mail archive has never completed a sync"
    mail_archive.set_meta(conn, "last_sync_at", "2026-09-12 08:00:00")
    conn.commit()
    conn.close()
    stale = mail_archive.status(path, now=NOW)
    assert stale["stale"] and not stale["ok"] and stale["count"] == 2
    assert "50 h ago" in stale["reason"]
    fresh = mail_archive.status(path, now=dt.datetime(2026, 9, 12, 20, 0))
    assert fresh["ok"] and fresh["hours_since"] == 12.0 and fresh["reason"] == ""


# ------------------------------------------------------------------ sync --
class FakeItems:
    """An Outlook Items collection: Restrict on the received date, then
    GetFirst / GetNext."""

    def __init__(self, items):
        self._items = list(items)
        self._pos = 0

    def Restrict(self, restriction):
        stamp = restriction.split(">= '")[1].rstrip("'")
        since = dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M")
        keep = [i for i in self._items
                if dt.datetime(i.ReceivedTime.year, i.ReceivedTime.month, i.ReceivedTime.day,
                               i.ReceivedTime.hour, i.ReceivedTime.minute) >= since]
        return FakeItems(keep)

    @property
    def Count(self):
        return len(self._items)

    def GetFirst(self):
        self._pos = 0
        return self.GetNext()

    def GetNext(self):
        if self._pos >= len(self._items):
            return None
        item = self._items[self._pos]
        self._pos += 1
        return item


class FakeOutlookFolder:
    def __init__(self, items, broken=False):
        self._items = items
        self._broken = broken

    @property
    def Items(self):
        if self._broken:
            raise RuntimeError("(-2147023174, 'The RPC server is unavailable.', None, None)")
        return FakeItems(self._items)


class FakeOutlook:
    """Stands in for outlook_bridge.Search inside sync(): the folder walk."""

    folders_to_serve = []
    fail_connects = 0
    connects = 0

    def __init__(self, budget=None):
        pass

    def __enter__(self):
        type(self).connects += 1
        if type(self).fail_connects > 0:
            type(self).fail_connects -= 1
            raise outlook_bridge.OutlookError(outlook_bridge.NOT_RUNNING, "com_error")
        return self

    def __exit__(self, *exc):
        return False

    def folders(self, mail_only=True):
        return list(type(self).folders_to_serve)


@pytest.fixture
def outlook(monkeypatch):
    FakeOutlook.folders_to_serve = []
    FakeOutlook.fail_connects = 0
    FakeOutlook.connects = 0
    monkeypatch.setattr(mail_archive.outlook_bridge, "Search", FakeOutlook)
    monkeypatch.setattr(mail_archive, "SYNC_LOG", None)
    return FakeOutlook


def test_sync_reads_the_window_then_only_what_is_new(tmp_path, outlook, monkeypatch):
    log_path = tmp_path / "sync-log.txt"
    monkeypatch.setattr(mail_archive, "SYNC_LOG", log_path)
    inbox = [FakeItem("first", message_id="<1@x>", received=(2026, 6, 1, 9, 0, 0), entry="I1"),
             FakeItem("second", message_id="<2@x>", received=(2026, 9, 10, 9, 0, 0), entry="I2")]
    sent = [FakeItem("reply", message_id="<3@x>", received=(2026, 9, 11, 9, 0, 0), entry="S1")]
    outlook.folders_to_serve = [
        FolderInfo("\\Mailbox\\Inbox", FakeOutlookFolder(inbox)),
        FolderInfo("\\Mailbox\\Sent Items", FakeOutlookFolder(sent)),
        FolderInfo("\\Mailbox\\Broken", FakeOutlookFolder([], broken=True)),
    ]
    path = tmp_path / "mail.db"
    seen = []
    report = mail_archive.sync(days_back=None, db_path=path, now=NOW, retry_wait=0,
                               progress=lambda r, why: seen.append((r.path, why)))
    assert report.ok and report.new == 2 and report.updated == 0 and report.failed == 1
    assert seen[0] == ("\\Mailbox\\Inbox", "first sync, 60 days")   # 'first' is too old
    assert report.folders[2].error.startswith("(-2147023174")
    assert "1 failed" in report.summary()

    conn = mail_archive.open_archive(path)
    rows = {r["folder_path"]: r for r in conn.execute("SELECT * FROM folder_sync")}
    assert rows["\\Mailbox\\Inbox"]["last_received_at"] == "2026-09-10 09:00:00"
    assert rows["\\Mailbox\\Inbox"]["position"] == 0
    assert rows["\\Mailbox\\Broken"]["last_error"].startswith("(-2147023174")
    assert mail_archive.get_meta(conn, "last_sync_at") is not None
    conn.close()
    assert "mail-sync ok: 3 folder(s), 2 mails read, 2 new, 0 updated, 1 failed" in log_path.read_text(encoding="utf-8")

    # second pass: 'second' was moved to a case folder, one more arrived
    moved = FakeItem("second", message_id="<2@x>", received=(2026, 9, 10, 9, 0, 0), entry="I2-MOVED")
    newer = FakeItem("third", message_id="<4@x>", received=(2026, 9, 13, 12, 0, 0), entry="I3")
    outlook.folders_to_serve = [
        FolderInfo("\\Mailbox\\Inbox", FakeOutlookFolder([inbox[0], newer])),
        FolderInfo("\\Mailbox\\Sent Items", FakeOutlookFolder(sent)),
        FolderInfo("\\Personal Folder\\OPP-1400", FakeOutlookFolder([moved])),
    ]
    seen = []
    report = mail_archive.sync(db_path=path, now=NOW, retry_wait=0,
                               progress=lambda r, why: seen.append((r.path, why)))
    assert report.ok and report.new == 1 and report.updated == 1       # 'third'; moved copy
    assert seen[0] == ("\\Mailbox\\Inbox", "since 2026-09-13 10:00")   # 24 h before last sync
    assert seen[2][1] == "first sync, 60 days"                           # a folder never seen
    conn = mail_archive.open_archive(path)
    row = conn.execute("SELECT entry_id, folder_path FROM message WHERE message_id = '<2@x>'").fetchone()
    assert (row["entry_id"], row["folder_path"]) == ("I2-MOVED", "\\Personal Folder\\OPP-1400")
    assert conn.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 3
    conn.close()

    # --full reads everything, 'first' included
    report = mail_archive.sync(days_back=0, db_path=path, now=NOW, retry_wait=0)
    assert report.new == 1 and report.ok


def test_sync_retries_when_outlook_is_not_there(tmp_path, outlook):
    outlook.fail_connects = 5
    report = mail_archive.sync(db_path=tmp_path / "mail.db", now=NOW, retry_wait=0, retries=3)
    assert not report.ok and report.attempts == 3 and outlook.connects == 3
    assert len(report.errors) == 3 and "attempt 3 of 3" in report.errors[-1]
    assert mail_archive.status(tmp_path / "mail.db", now=NOW)["ok"] is False

    outlook.fail_connects = 1
    outlook.folders_to_serve = [FolderInfo("\\Mailbox\\Inbox", FakeOutlookFolder(
        [FakeItem("hello", received=(2026, 9, 10, 9, 0, 0))]))]
    report = mail_archive.sync(db_path=tmp_path / "mail.db", now=NOW, retry_wait=0)
    assert report.ok and report.attempts == 2 and report.new == 1


def test_sync_gives_up_on_a_run_of_dead_folders(tmp_path, outlook):
    dead = [FolderInfo("\\M\\F{}".format(n), FakeOutlookFolder([], broken=True))
            for n in range(8)]
    outlook.folders_to_serve = dead
    report = mail_archive.sync(db_path=tmp_path / "mail.db", now=NOW, retry_wait=0, retries=2)
    assert not report.ok and outlook.connects == 2
    assert "RPC server" in report.errors[0]


def test_since_for_windows():
    row = {"last_received_at": "2026-09-10 09:00:00", "last_synced_at": "2026-09-13 08:00:00"}
    since, why = mail_archive._since_for(row, None, NOW)
    assert since == dt.datetime(2026, 9, 12, 8, 0) and why == "since 2026-09-12 08:00"
    since, why = mail_archive._since_for(row, 7, NOW)               # --days wins everywhere
    assert since == NOW - dt.timedelta(days=7)
    assert mail_archive._since_for(row, 0, NOW) == (None, "all mail")
    since, why = mail_archive._since_for({"last_received_at": "2026-09-10 09:00:00",
                                          "last_synced_at": None}, None, NOW)
    assert since == NOW - dt.timedelta(days=60)                     # never synced
    since, why = mail_archive._since_for(None, None, NOW)
    assert since == NOW - dt.timedelta(days=60) and why.startswith("first sync")
    assert mail_archive._restriction(since) == \
        '@SQL="urn:schemas:httpmail:datereceived" >= \'2026-07-16 10:00\''


# ------------------------------------------------------------------- CLI --
def test_mail_search_from_the_command_line(tmp_path, monkeypatch, capsys):
    from opplist.__main__ import main
    path = tmp_path / "cli" / "mail.db"
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", path)
    assert main(["mail-search", "057-33092174"]) == 1                # no archive yet
    assert "No mail archive yet" in capsys.readouterr().out
    conn = mail_archive.open_archive(path)
    load(conn, AWB_MAILS)
    mail_archive.set_meta(conn, "last_sync_at", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    conn.commit()
    conn.close()
    terms = tmp_path / "terms.txt"
    terms.write_text("# batch\n176-51820943\n", encoding="utf-8")
    out = tmp_path / "out"
    assert main(["mail-search", "057-33092174", "--term-file", str(terms), "--tag", "t",
                 "--full-body", "--out-dir", str(out), "--days", "0"]) == 0
    printed = capsys.readouterr().out
    assert "057-33092174        3 mail(s)" in printed                  # A4 too, days=0
    assert "176-51820943        1 mail(s)" in printed
    assert (out / "t-hits.csv").exists() and (out / "t-full.txt").exists()
    assert main(["mail-search"]) == 1                                 # no terms


def test_propose_status_stops_without_an_archive_unless_outlook_is_asked_for(tmp_path, monkeypatch, capsys):
    from opplist import db
    from opplist.__main__ import main
    path = tmp_path / "opp.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    add_enquiry(repo, "OPP-0001", party, received="2026-08-13", status_confirmed=0)
    repo.commit()
    conn.close()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", tmp_path / "none" / "mail.db")
    monkeypatch.setattr(outlook_bridge, "Search", FakeOutlook)
    FakeOutlook.fail_connects = 1
    FakeOutlook.connects = 0
    assert main(["propose-status", "--month", "2026-08", "--db", str(path)]) == 2
    printed = capsys.readouterr().out
    assert "WARNING: no mail archive yet" in printed
    assert "Run: python -m opplist mail-sync" in printed and "Outlook (live)" not in printed
    assert FakeOutlook.connects == 0                                  # Outlook untouched
    assert main(["propose-status", "--month", "2026-08", "--db", str(path),
                 "--source", "outlook"]) == 1
    printed = capsys.readouterr().out
    assert "WARNING" not in printed and "source   : Outlook (live)" in printed
