"""Batch RQ2 (2026-11-17, schema v54): Read quote from the team site - a
read action with the vendor's mail (.msg or .eml), files or a pasted
message, taken in, read on her computer into section 11's drafts and
answered (INTERFACE.md section 12); a mail file dropped into
Read quote on 8765.

A fake Firestore client, a fake Outlook bridge and a fake API; no network.
Every company, person, place, amount, date and reference here is invented:
cases OPP-9990 to OPP-9994, domains .test, invented vendor names.
"""

from __future__ import annotations

import base64
import datetime as dt
import email.message
import io
import json

import pytest

from conftest import add_enquiry, add_party
from opplist import db, firebase_pull, firebase_sync as fs, outlook_bridge, paths, read_quote as rq, schema_migrations
from opplist import site_read
from opplist import web as web_module
from opplist.web import create_app
from test_firebase_sync import FakeFirestore
from test_q2d import FakeAPI, NoCall, answer, line, option, pdf_bytes, response

TODAY = "2027-05-12"
KEY = "demo-key-INVENTED-4410"
AT = dt.datetime(2027, 5, 12, 14, 0, tzinfo=dt.timezone.utc)


# ------------------------------------------------------------ the fakes --
class SiteFake(FakeFirestore):
    """FakeFirestore with deletes and the two queries RQ2 makes (actions
    after a time, uploads before one)."""

    def __init__(self):
        super().__init__()
        self.deleted, self.queries = [], []

    def commit(self, writes):
        gone = [w for w in writes if isinstance(w, fs.Delete)]
        for w in gone:
            self.docs.pop(w.path, None)
            self.deleted.append(w.path)
        rest = [w for w in writes if not isinstance(w, fs.Delete)]
        if rest:
            return super().commit(rest)
        self.commits.append([w.path for w in gone])
        return {}

    def run_query(self, query):
        self.queries.append(query)
        where = query["where"]["fieldFilter"]
        bound = where["value"]["timestampValue"]
        name = query["from"][0]["collectionId"]
        docs = []
        for path, doc in sorted(self.docs.items(), key=lambda kv: firebase_pull.iso(kv[1].get("at"))):
            if not path.startswith(name + "/") or not isinstance(doc.get("at"), dt.datetime):
                continue
            at = firebase_pull.iso(doc["at"])
            if (where["op"] == "GREATER_THAN" and at > bound) or (where["op"] == "LESS_THAN" and at < bound):
                docs.append((path, dict(doc)))
        return {"docs": docs, "readTime": "2027-05-12T15:00:00.000000Z"}


class Bridge:
    """outlook_bridge's open_msg, for a .msg sent from the site."""

    def __init__(self, info=None, files=None, fail=None):
        self.info, self.files, self.fail, self.opened = info, files or [], fail, []

    def open_msg(self, path):
        self.opened.append(path)
        if self.fail:
            raise outlook_bridge.OutlookError(self.fail)
        return self.info, list(self.files)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def put_action(client, action_id, opp, kind, files=(), text="", sent="2027-05-11", vendor="Brisk Cargo Lines",
               route=None, name="Eric", at=AT, part=None, skip=()):
    """The site's write: each file's parts into uploads/, then the action."""
    listed = []
    for i, (fname, data) in enumerate(files):
        coded = b64(data)
        size = part or max(1, len(coded))
        chunks = [coded[k:k + size] for k in range(0, len(coded), size)] or [""]
        for j, chunk in enumerate(chunks):
            if (i, j) in skip:
                continue
            client.docs["uploads/{}-{}-{}".format(action_id, i, j)] = {"data": chunk, "by": "eric@site.test", "at": at}
        listed.append({"name": fname, "type": "application/octet-stream", "size": len(data), "parts": len(chunks)})
    client.docs["actions/" + action_id] = {
        "type": "read", "opp": opp, "by": "{}@site.test".format(name.lower()), "name": name, "owner": name, "app": "1117a",
        "at": at, "vendor": vendor, "route": route, "kind": kind, "text": text, "sent": sent, "files": listed}


def take(w, client):
    state = w.setdefault("state", {})
    return firebase_pull.pull(w["conn"], client, state, TODAY)


def result(client, action_id):
    return client.docs["actionResults/" + action_id]


def priced(route=None, n=1, rate=2.4):
    return answer((route, [option([line("OCEAN FREIGHT", unit="40'HQ", rate=rate + k, quote="OF USD {}".format(rate + k))],
                                  label="Option {}".format(k + 1)) for k in range(n)]))


# ------------------------------------------------------------ the world --
@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "rq2.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    for name, region, iso2 in (("USA", "North America", "US"), ("Peru", "Latin America", "PE"),
                               ("Unknown", "Undefined", None)):
        repo.insert("country", {"name": name, "region": region, "iso2": iso2})
    agent = add_party(repo, "Quillon Forwarding", country="Peru")
    brisk = add_party(repo, "Brisk Cargo Lines", ptype="Carrier", country="USA")
    repo.insert("party_domain", {"domain": "brisk.test", "party_id": brisk})
    ids = {"agent": agent, "brisk": brisk}
    for opp, extra in (("OPP-9990", {}), ("OPP-9991", {}), ("OPP-9992", {"status": "Lost"}),
                       ("OPP-9993", {}), ("OPP-9994", {})):
        ids[opp] = add_enquiry(repo, opp, agent, received="2027-05-01", scope="In", service_mode="FCL",
                               origin_country="USA", dest_country="Peru", **extra)
    # OPP-9991 has two routes: A FCL, B LCL
    repo.add_route(ids["OPP-9991"], {"origin_country": "USA", "dest_country": "Peru", "service_mode": "LCL"})
    for opp in ("OPP-9990", "OPP-9991", "OPP-9992", "OPP-9993"):
        ids["vr" + opp[-2:]] = repo.insert("vendor_request", {
            "enquiry_id": ids[opp], "vendor_party_id": brisk, "asked_on": "2027-05-02",
            "thread_key": "RATE REQUEST // " + opp, "parts": "Freight"})
    repo.commit()
    (paths.DATA_DIR / "secrets").mkdir(parents=True, exist_ok=True)
    (paths.DATA_DIR / "secrets" / "anthropic-key.txt").write_text(KEY + "\n", encoding="utf-8")
    db.set_setting(conn, rq.ON_SETTING, "1")
    db.set_setting(conn, rq.CAP_SETTING, "50")
    conn.commit()
    monkeypatch.setattr(rq, "SLEEP", lambda s: None)
    monkeypatch.setattr(rq, "_now", lambda: dt.datetime(2027, 5, 12, 9, 30, 0))
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    monkeypatch.setattr(fs, "_archive_search", lambda: None)
    yield {"conn": conn, "ids": ids, "path": path, "repo": repo}
    conn.close()


def read_next(w, api):
    waiting = site_read.queued(w["conn"])
    return site_read.read_one(w["conn"], waiting[0], today=TODAY, transport=api)


def site_words_ok(text):
    low = text.lower()
    return "oppList".lower() not in low and "computer" not in low and "8765" not in low and "settings" not in low


# ================================================================ point 2
def test_a_paste_taken_in_is_queued(world):
    w = world
    client = SiteFake()
    put_action(client, "aPasteRequest0000001", "OPP-9990", "paste", text="Ocean freight USD 2,400 per 40HQ")
    got = take(w, client)
    assert got.applied == 1 and got.refused == 0
    r = result(client, "aPasteRequest0000001")
    assert r == {"ok": True, "state": "queued", "msg": "Waiting to be read.", "by": "eric@site.test", "type": "read",
                 "opp": "OPP-9990", "at": "REQUEST_TIME"}
    assert [q["id"] for q in site_read.queued(w["conn"])] == ["aPasteRequest0000001"]


def test_parts_are_put_together_in_order_and_kept(world):
    w = world
    client = SiteFake()
    pdf = pdf_bytes(2)
    sheet = b"Port,Rate\nCallao,2400\n" * 100
    put_action(client, "aFilesRequest0000002", "OPP-9990", "files", files=[("rates.pdf", pdf), ("rates.csv", sheet)],
               text="use the direct service only", part=400)
    assert sum(1 for p in client.docs if p.startswith("uploads/aFilesRequest0000002-0-")) > 1
    take(w, client)
    assert (result(client, "aFilesRequest0000002")["state"], result(client, "aFilesRequest0000002")["msg"]) == \
        ("queued", "Waiting to be read.")
    where = paths.DATA_DIR / "site-reads" / "aFilesRequest0000002"
    assert (where / "rates.pdf").read_bytes() == pdf and (where / "rates.csv").read_bytes() == sheet
    # taken in once: a second pull skips it
    again = take(w, client)
    assert again.applied == 0


@pytest.mark.parametrize("change, why", [
    ({"skip": ((0, 1),)}, "a part of rates.pdf is missing"),
    ({"opp": "OPP-9992"}, "the case is closed"),
    ({"opp": "OPP-9998"}, "the case is not in the lists"),
    ({"vendor": "Tamsin Air"}, "Tamsin Air is not on this case's rate requests"),
    ({"route": 7}, "route 7 is not a route of this case"),
    ({"kind": "mail"}, "no mail file (.msg or .eml) among the files"),
    ({"sent": "2027-05-40"}, "'2027-05-40' is not the day they sent it"),
])
def test_refused_with_why_in_the_sites_words(world, change, why):
    w = world
    client = SiteFake()
    opts = dict({"opp": "OPP-9990", "kind": "files"}, **change)
    opp, kind = opts.pop("opp"), opts.pop("kind")
    put_action(client, "aRefusedRequest00003", opp, kind, files=[("rates.pdf", pdf_bytes(1))], part=500, **opts)
    take(w, client)
    r = result(client, "aRefusedRequest00003")
    assert (r["ok"], r["state"], r["msg"]) == (False, "done", why)
    assert site_words_ok(r["msg"])
    assert site_read.queued(w["conn"]) == []


def test_sizes_that_do_not_add_up_are_refused(world):
    w = world
    client = SiteFake()
    put_action(client, "aShortRequest0000004", "OPP-9990", "files", files=[("rates.pdf", pdf_bytes(1))])
    client.docs["actions/aShortRequest0000004"]["files"][0]["size"] += 5
    take(w, client)
    assert result(client, "aShortRequest0000004")["msg"].startswith("rates.pdf did not arrive whole (")


# ============================================================ points 3 to 5
def test_a_paste_read_drafts_and_the_answer_rewritten(world):
    w = world
    client = SiteFake()
    put_action(client, "aPasteRequest0000005", "OPP-9990", "paste", text="OF USD 2400 per 40HQ, valid to May 31",
               name="Victor")
    take(w, client)
    api = FakeAPI(response(priced(n=2)))
    reading_id = read_next(w, api)
    q = w["conn"].execute("SELECT * FROM quote_read WHERE id = ?", (reading_id,)).fetchone()
    assert (q["source"], q["by_name"], q["site_action"], q["status"]) == ("paste", "Victor", "aPasteRequest0000005", "read")
    fs.sync_once(w["conn"], client, w["state"], today=TODAY, reports_dir=w["path"].parent / "none")
    doc = client.docs["rateDrafts/rq{}-1".format(reading_id)]
    assert doc["readBy"] == "Victor" and doc["source"] == {"kind": "paste", "subject": "", "sent": "2027-05-11",
                                                            "files": []}
    r = result(client, "aPasteRequest0000005")
    assert (r["ok"], r["state"], r["msg"]) == (True, "done", "Read: 2 options for route A.")
    assert site_read.queued(w["conn"]) == [] and len(api.calls) == 1


def test_no_prices_is_its_why_in_lower_case(world):
    w = world
    client = SiteFake()
    put_action(client, "aNoPriceRequest00006", "OPP-9990", "paste", text="We will revert with rates tomorrow")
    take(w, client)
    read_next(w, FakeAPI(response(answer(priced=False, why="The vendor says rates will follow"))))
    fs.sync_once(w["conn"], client, w["state"], today=TODAY, reports_dir=w["path"].parent / "none")
    r = result(client, "aNoPriceRequest00006")
    assert (r["ok"], r["state"], r["msg"]) == (False, "done", "the vendor says rates will follow")


def test_a_msg_through_the_bridge_with_its_attachments(world, monkeypatch):
    w = world
    client = SiteFake()
    pdf = pdf_bytes(1)
    info = outlook_bridge.MailInfo(subject="RE: RATE REQUEST // OPP-9990 // HOU - CALLAO", sender_name="Nell Brisk",
                                   sender_address="nell@brisk.test", sent_at="2027-05-10 16:05",
                                   body_excerpt="Dear team,\r\nPlease see our rates attached.\r\n\r\n> earlier mail")
    files = [outlook_bridge.AttachedFile(name="image001.png", size=900, inline=True),
             outlook_bridge.AttachedFile(name="brisk-rates.pdf", size=len(pdf), data=pdf)]
    bridge = Bridge(info, files)
    monkeypatch.setattr(rq, "OUTLOOK", bridge)
    put_action(client, "aMailRequest00000007", "OPP-9990", "mail", files=[("RE rates.msg", b"\xd0\xcf\x11\xe0 invented msg"),
                                                                         ("extra.txt", b"THC USD 95 per container")],
               text="use the direct service")
    take(w, client)
    api = FakeAPI(response(priced()))
    reading_id = read_next(w, api)
    assert bridge.opened == [str(paths.DATA_DIR / "site-reads" / "aMailRequest00000007" / "RE rates.msg")]
    q = w["conn"].execute("SELECT * FROM quote_read WHERE id = ?", (reading_id,)).fetchone()
    assert (q["source"], q["subject"], q["sent_at"]) == ("mail", "RE: RATE REQUEST // OPP-9990 // HOU - CALLAO",
                                                         "2027-05-10 16:05")
    kept = {f["name"]: f for f in json.loads(q["files"])}
    assert kept["image001.png"]["sent"] is False and kept["brisk-rates.pdf"]["sent"] is True
    assert kept["extra.txt"]["sent"] is True and kept["extra.txt"]["path"].endswith("extra.txt")
    text = api.calls[0]["body"]["messages"][0]["content"][-1]["text"]
    assert "Subject: RE: RATE REQUEST // OPP-9990 // HOU - CALLAO" in text and "> earlier mail" in text
    assert "Their note for the reading: use the direct service" in text and "THC USD 95 per container" in text
    fs.sync_once(w["conn"], client, w["state"], today=TODAY, reports_dir=w["path"].parent / "none")
    doc = client.docs["rateDrafts/rq{}-1".format(reading_id)]
    assert doc["on"] == "2027-05-10" and doc["source"]["kind"] == "mail"
    assert doc["source"]["subject"] == "RE: RATE REQUEST // OPP-9990 // HOU - CALLAO"
    assert doc["source"]["files"] == ["brisk-rates.pdf", "extra.txt"] and doc["readBy"] == "Eric"


def eml_bytes():
    m = email.message.EmailMessage()
    m["From"] = "Nell Brisk <nell@brisk.test>"
    m["To"] = "desk@forwarder.test"
    m["Subject"] = "RE: RATE REQUEST // OPP-9993"
    m["Date"] = "Mon, 10 May 2027 16:05:00 -0500"
    m["Message-ID"] = "<invented-4410@brisk.test>"
    m.set_content("Ocean freight USD 2,400 per 40HQ\nValid until 31 May\n\n> your request below")
    m.add_related(b"\x89PNG invented logo", maintype="image", subtype="png", cid="<logo1>", filename="logo.png")
    m.add_attachment(pdf_bytes(1), maintype="application", subtype="pdf", filename="tariff.pdf")
    m.add_attachment(b"\x89PNG invented signature", maintype="image", subtype="png", filename="sig.png",
                     cid="<sig1>", disposition="inline")
    return m.as_bytes()


def test_an_eml_with_the_email_module(world):
    w = world
    client = SiteFake()
    put_action(client, "aEmlRequest000000008", "OPP-9993", "mail", files=[("reply.eml", eml_bytes())], name="Victor")
    take(w, client)
    api = FakeAPI(response(priced()))
    reading_id = read_next(w, api)
    q = w["conn"].execute("SELECT * FROM quote_read WHERE id = ?", (reading_id,)).fetchone()
    assert q["source"] == "mail" and q["subject"] == "RE: RATE REQUEST // OPP-9993"
    assert q["sender"] == "Nell Brisk <nell@brisk.test>" and q["message_id"] == "<invented-4410@brisk.test>"
    local = dt.datetime(2027, 5, 10, 21, 5, tzinfo=dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    assert q["sent_at"] == local
    kept = {f["name"]: f for f in json.loads(q["files"])}
    assert kept["tariff.pdf"]["sent"] is True and kept["sig.png"]["sent"] is False and "logo.png" not in kept
    assert kept["sig.png"]["why"] == "a picture in the mail's body or signature"
    blocks = api.calls[0]["body"]["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["document", "text"]
    assert "Ocean freight USD 2,400 per 40HQ" in blocks[-1]["text"] and "> your request below" in blocks[-1]["text"]


def test_files_the_note_as_its_text_and_the_day_sent(world):
    w = world
    client = SiteFake()
    put_action(client, "aFilesRequest0000009", "OPP-9990", "files", files=[("rates.pdf", pdf_bytes(1))],
               text="the CMA option only", sent="2027-05-09")
    take(w, client)
    api = FakeAPI(response(priced()))
    reading_id = read_next(w, api)
    q = w["conn"].execute("SELECT * FROM quote_read WHERE id = ?", (reading_id,)).fetchone()
    assert q["source"] == "files" and q["sent_at"] == "2027-05-09"
    text = api.calls[0]["body"]["messages"][0]["content"][-1]["text"]
    assert "Files the vendor sent, on 2027-05-09" in text and "the CMA option only" in text
    doc = rq.draft_docs(w["conn"])["rateDrafts/rq{}-1".format(reading_id)]
    assert doc["on"] == "2027-05-09" and doc["source"] == {"kind": "files", "subject": "", "sent": "2027-05-09",
                                                           "files": ["rates.pdf"]}


def test_the_route_the_action_names(world):
    w = world
    client = SiteFake()
    put_action(client, "aRouteRequest0000010", "OPP-9991", "paste", text="LCL USD 80 per cbm", route=2)
    take(w, client)
    api = FakeAPI(response(priced(route=1)))
    reading_id = read_next(w, api)
    assert "says it is for route 2 (B)" in api.calls[0]["body"]["messages"][0]["content"][-1]["text"]
    doc = rq.draft_docs(w["conn"])["rateDrafts/rq{}-2".format(reading_id)]
    assert doc["route"] == 2
    assert "The vendor named route 1; read as route 2, as the request said" in doc["warnings"]
    fs.sync_once(w["conn"], client, w["state"], today=TODAY, reports_dir=w["path"].parent / "none")
    assert result(client, "aRouteRequest0000010")["msg"] == "Read: 1 option for route B."


def test_the_cap_reached_and_read_quote_off_read_nothing(world):
    w = world
    client = SiteFake()
    put_action(client, "aCapRequest000000011", "OPP-9990", "paste", text="OF USD 2400")
    put_action(client, "aOffRequest000000012", "OPP-9993", "paste", text="OF USD 2500",
               at=AT + dt.timedelta(minutes=1))
    take(w, client)
    w["repo"].insert("quote_read", {"enquiry_id": w["ids"]["OPP-9994"], "vendor_party_id": w["ids"]["brisk"],
                                    "source": "paste", "model": "claude-sonnet-5-5", "cost_usd": 50.0, "status": "read",
                                    "at": "2027-05-03 10:00:00"})
    w["repo"].commit()
    read_next(w, NoCall())
    db.set_setting(w["conn"], rq.ON_SETTING, "0")
    w["conn"].commit()
    read_next(w, NoCall())
    fs.sync_once(w["conn"], client, w["state"], today=TODAY, reports_dir=w["path"].parent / "none")
    assert result(client, "aCapRequest000000011")["msg"] == "this month's limit is reached"
    assert result(client, "aOffRequest000000012")["msg"] == "Read quote is off"
    assert all(result(client, a)["ok"] is False and result(client, a)["state"] == "done"
               for a in ("aCapRequest000000011", "aOffRequest000000012"))
    # the cap's reading is on file, failed, no call made (it costs nothing)
    rows = w["conn"].execute("SELECT status, model, cost_usd FROM quote_read WHERE site_action IS NOT NULL").fetchall()
    assert [tuple(r) for r in rows] == [("failed", None, None), ("failed", None, None)]
    assert "readQuote" not in client.docs["meta/sync"]          # off: the site offers nothing


def test_a_file_that_cannot_be_opened(world, monkeypatch):
    w = world
    client = SiteFake()
    monkeypatch.setattr(rq, "OUTLOOK", Bridge(fail=outlook_bridge.NOT_RUNNING))
    put_action(client, "aBadMsgRequest000013", "OPP-9990", "mail", files=[("reply.msg", b"invented")])
    put_action(client, "aBadEmlRequest000014", "OPP-9993", "mail", files=[("reply.eml", b"\x00\x01 not a mail")],
               at=AT + dt.timedelta(minutes=1))
    take(w, client)
    read_next(w, NoCall())
    read_next(w, NoCall())
    fs.sync_once(w["conn"], client, w["state"], today=TODAY, reports_dir=w["path"].parent / "none")
    assert result(client, "aBadMsgRequest000013")["msg"] == "reply.msg could not be opened (Outlook was not open to read it)"
    assert result(client, "aBadEmlRequest000014")["msg"] == \
        "reply.eml could not be opened (it is not a mail file Read quote can read)"
    assert all(site_words_ok(result(client, a)["msg"]) for a in ("aBadMsgRequest000013", "aBadEmlRequest000014"))


def test_api_failures_are_phrases_that_never_name_her_computer():
    for sentence in (rq.BUSY, rq.TIMED_OUT, rq.NO_CONNECTION, rq.NO_CREDITS, rq.LIMIT_REACHED, rq.CUT_OFF, rq.NOT_JSON,
                     rq.KEY_REFUSED.format(path="C:\\x"), rq.MODEL_GONE.format(model="m"), rq.NOT_LIVE,
                     rq.REFUSAL.format(""), rq.LIST_LINE, "The answer did not pass OppList's check (x): read again."):
        said = site_read.phrase(sentence)
        assert said and (said[0].islower() or said.startswith("Read quote ")) and len(said) <= 200 \
            and site_words_ok(said), (sentence, said)


# ======================================================= the scheduled run
def test_the_scheduled_run_only_takes_them_in(world, monkeypatch):
    w = world
    monkeypatch.setattr(rq, "TRANSPORT", NoCall())
    client = SiteFake()
    put_action(client, "aSchedRequest0000015", "OPP-9990", "paste", text="OF USD 2400")
    client.docs["meta/sync"] = {"readQuote": {"on": True, "capReached": False, "appAt": AT}}
    got = fs.sync_once(w["conn"], client, {}, today=TODAY, reports_dir=w["path"].parent / "none")
    assert got.ok, got.reason
    assert result(client, "aSchedRequest0000015")["state"] == "queued"
    assert w["conn"].execute("SELECT COUNT(*) FROM quote_read").fetchone()[0] == 0
    # the value it found is kept; the app's own cycle stamps the server time
    assert client.docs["meta/sync"]["readQuote"] == {"on": True, "capReached": False, "appAt": str(AT)}
    fs.sync_once(w["conn"], client, {}, today=TODAY, reports_dir=w["path"].parent / "none", by_app=True)
    assert client.docs["meta/sync"]["readQuote"] == {"on": True, "capReached": False}
    assert client.docs["meta/sync"]["readQuote.appAt"] == "REQUEST_TIME"


def test_cap_reached_on_meta_sync(world):
    w = world
    w["repo"].insert("quote_read", {"enquiry_id": w["ids"]["OPP-9994"], "vendor_party_id": w["ids"]["brisk"],
                                    "source": "paste", "model": "claude-sonnet-5-5", "cost_usd": 50.5, "status": "read",
                                    "at": "2027-05-03 10:00:00"})
    w["repo"].commit()
    client = SiteFake()
    fs.sync_once(w["conn"], client, {}, today=TODAY, reports_dir=w["path"].parent / "none", by_app=True)
    assert client.docs["meta/sync"]["readQuote"]["capReached"] is True


# ================================================================ point 6
def test_uploads_deleted_once_read_and_orphans_after_a_day(world, monkeypatch):
    w = world
    monkeypatch.setattr(site_read, "_utcnow", lambda: AT)
    client = SiteFake()
    put_action(client, "aDoneRequest00000016", "OPP-9990", "files", files=[("rates.pdf", pdf_bytes(1))], part=400)
    put_action(client, "aWaitRequest00000017", "OPP-9993", "files", files=[("tariff.pdf", pdf_bytes(1))],
               at=AT + dt.timedelta(minutes=1))
    put_action(client, "aRefusedRequest00018", "OPP-9992", "files", files=[("old.pdf", pdf_bytes(1))],
               at=AT + dt.timedelta(minutes=2))
    take(w, client)
    # the refused one at once, the others kept
    assert [p for p in client.deleted if p.startswith("uploads/aRefusedRequest00018-")]
    assert not any("aDoneRequest00000016" in p or "aWaitRequest00000017" in p for p in client.deleted)
    uploads_queries = [q for q in client.queries if q["from"][0]["collectionId"] == "uploads"]
    assert len(uploads_queries) == 1 and uploads_queries[0]["select"] == {"fields": [{"fieldPath": "__name__"}]}
    # a day later: a part with no action goes; one whose action is on the site, not taken in yet, stays;
    # the parts of a request waiting to be read stay
    old = AT - dt.timedelta(days=2)
    client.docs["uploads/aOrphanRequest000019-0-0"] = {"data": "QUJD", "by": "ava@site.test", "at": old}
    client.docs["uploads/aLaterRequest0000020-0-0"] = {"data": "QUJD", "by": "ava@site.test", "at": old}
    client.docs["uploads/aWaitRequest00000017-0-0"]["at"] = old
    later = SiteFake()
    later.docs = client.docs
    later.docs["actions/aLaterRequest0000020"] = {"type": "read", "opp": "OPP-9990", "at": AT + dt.timedelta(days=9)}
    site_read.clean_uploads(w["conn"], later, w["state"], "2027-05-13")
    w["state"]["read_uploads_checked"] = TODAY
    assert later.deleted == ["uploads/aOrphanRequest000019-0-0"]
    del later.docs["actions/aLaterRequest0000020"]
    # read: its parts go in the run the reading starts; the waiting one stays
    read_next(w, FakeAPI(response(priced())))
    fs.sync_once(w["conn"], client, w["state"], today=TODAY, reports_dir=w["path"].parent / "none")
    assert not any(p.startswith("uploads/aDoneRequest00000016-") for p in client.docs)
    assert any(p.startswith("uploads/aWaitRequest00000017-") for p in client.docs)
    # once a day
    assert len([q for q in client.queries if q["from"][0]["collectionId"] == "uploads"]) == 1
    # the files stay on her computer
    assert (paths.DATA_DIR / "site-reads" / "aDoneRequest00000016" / "rates.pdf").exists()


# ================================================================ the minute check
def test_the_minute_check_looks_once_and_writes_nothing(world):
    w = world
    client = SiteFake()
    wanted, newest = site_read.look(w["conn"], client, "2027-05-12T00:00:00.000000Z")
    assert (wanted, newest) == (False, "2027-05-12T00:00:00.000000Z") and client.commits == []
    client.docs["actions/aAssignAction0000021"] = {"type": "assign", "opp": "OPP-9990", "at": AT, "to": "Ava"}
    wanted, newest = site_read.look(w["conn"], client, "2027-05-12T00:00:00.000000Z")
    assert wanted is False and newest == firebase_pull.iso(AT)
    put_action(client, "aMinuteRequest000022", "OPP-9990", "paste", text="OF USD 2400", at=AT + dt.timedelta(seconds=30))
    wanted, _ = site_read.look(w["conn"], client, newest)
    assert wanted is True and len(client.queries) == 3 and client.commits == []
    assert client.queries[-1]["limit"] == 50
    take(w, client)                       # taken in: not wanted again
    assert site_read.look(w["conn"], client, newest)[0] is False


def test_the_syncer_reads_only_on_the_live_app(world):
    syncer = fs.Syncer(world["path"])
    assert syncer.check_reads() is False            # a test is never live
    syncer.read_waiting()
    assert world["conn"].execute("SELECT COUNT(*) FROM quote_read").fetchone()[0] == 0


# ================================================================ point 7
def test_the_weekly_report_says_who_asked(world):
    w = world
    client = SiteFake()
    for n, (opp, name) in enumerate((("OPP-9990", "Eric"), ("OPP-9993", "Victor"), ("OPP-9990", "Eric"))):
        put_action(client, "aReportRequest00002{}".format(n), opp, "paste", text="OF USD 2400", name=name,
                   at=AT + dt.timedelta(minutes=n))
    take(w, client)
    for _ in range(3):
        read_next(w, FakeAPI(response(priced())))
    rq.read(w["conn"], w["ids"]["vr90"], rq.from_paste("OF USD 2500", "2027-05-11", TODAY), today=TODAY,
            transport=FakeAPI(response(priced())))
    text = rq.report(w["conn"], 7, TODAY)
    assert "From the site: Eric 2, Victor 1." in text
    assert "OPP-9993 Brisk Cargo Lines: read, 1 draft, asked by Victor (team site)" in text
    assert text.count("asked by Iris\n") == 1


# ================================================================ point 8
def test_a_mail_file_dropped_on_8765(world, monkeypatch):
    w = world
    api = FakeAPI(response(priced()))
    monkeypatch.setattr(rq, "TRANSPORT", api)
    client = create_app(w["path"]).test_client()
    case_id = w["ids"]["OPP-9993"]
    page = client.get("/enquiries/{}?read={}".format(case_id, w["ids"]["vr93"])).get_data(as_text=True)
    assert 'enctype="multipart/form-data"' in page and 'name="mail_file" accept=".msg,.eml"' in page
    resp = client.post("/enquiries/{}/read-quote/{}".format(case_id, w["ids"]["vr93"]),
                       data={"mail_file": (io.BytesIO(eml_bytes()), "reply.eml"), "paste": ""},
                       content_type="multipart/form-data")
    assert resp.status_code == 302 and "qr=" in resp.headers["Location"]
    q = w["conn"].execute("SELECT * FROM quote_read ORDER BY id DESC").fetchone()
    assert (q["source"], q["subject"], q["site_action"], q["by_name"]) == ("mail", "RE: RATE REQUEST // OPP-9993",
                                                                         None, "Iris")
    assert list((paths.DATA_DIR / "site-reads").glob("app-*/reply.eml"))
    # another kind of file: refused before any call
    client.post("/enquiries/{}/read-quote/{}".format(case_id, w["ids"]["vr93"]),
                data={"mail_file": (io.BytesIO(b"x"), "rates.txt")}, content_type="multipart/form-data")
    assert len(api.calls) == 1


# ================================================================ the schema
def test_v54_from_v53_keeps_every_reading(tmp_path):
    path = tmp_path / "old.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    repo.insert("country", {"name": "USA", "region": "North America", "iso2": "US"})
    p = add_party(repo, "Pellow Air", ptype="Carrier")
    e = add_enquiry(repo, "OPP-9990", p, received="2027-01-02", scope="In")
    repo.commit()
    import re
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'quote_read'").fetchone()[0]
    old_sql = sql.replace("'mail','paste','files'", "'mail','paste'")
    old_sql = re.sub(r"\n\s*site_action[^\n]*", "", old_sql)
    assert "site_action" not in old_sql and "'files'" not in old_sql
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.execute("DROP INDEX IF EXISTS ix_quote_read_site")
    conn.execute("ALTER TABLE quote_read RENAME TO quote_read_v54")
    conn.execute(old_sql)
    conn.execute("DROP TABLE quote_read_v54")
    conn.execute("PRAGMA legacy_alter_table = OFF")
    conn.execute("PRAGMA foreign_keys = ON")
    for n in range(3):
        conn.execute("INSERT INTO quote_read (enquiry_id, vendor_party_id, source, status, by_name) "
                     "VALUES (?, ?, 'paste', 'read', 'Iris')", (e, p))
    q = conn.execute("SELECT MAX(id) FROM quote_read").fetchone()[0]
    conn.execute("INSERT INTO quote_read_draft (quote_read_id, site_id, draft) VALUES (?, 'rq3-1', '{}')", (q,))
    conn.execute("UPDATE setting SET value = '53' WHERE key = 'schema_version'")
    conn.commit()
    notes = schema_migrations.upgrade(conn)
    assert "rows before and after: quote_read 3, quote_read_draft 1" in notes
    assert "foreign_key_check: nothing" in notes
    assert any(n.startswith("quote_read rebuilt (source mail / paste / files, site_action)") for n in notes)
    assert [r[0] for r in conn.execute("SELECT by_name FROM quote_read")] == ["Iris"] * 3
    conn.execute("INSERT INTO quote_read (enquiry_id, vendor_party_id, source, status, site_action) "
                 "VALUES (?, ?, 'files', 'failed', 'aSchemaAction0000001')", (e, p))
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION
    assert schema_migrations.upgrade(conn) == []                    # twice: nothing
    conn.close()
