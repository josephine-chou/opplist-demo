"""Batch W1b (2026-10-28): OppList takes in what people do on the team
site - the actions of INTERFACE.md section 2, applied once each in the
order of `at` and answered in actionResults, pulledAt moved on only once
every action up to it is answered. CX1: the site's notes stay on the site.

Every company, person, mail, place and date here is invented; cases from
OPP-6901.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from types import SimpleNamespace

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import assign_list, db, detect_followups, firebase_pull as fp, firebase_sync as fs
from opplist import schema_migrations, tracking
from test_firebase_sync import FakeFirestore

TODAY = "2026-12-14"
READ_TIME = "2026-12-14T18:00:00.123456789Z"


def at(minute, second=0, hour=15):
    return dt.datetime(2026, 12, 14, hour, minute, second, tzinfo=dt.timezone.utc)


def later(fake, hour):
    """What people do after a run: later than its readTime."""
    fake.read_time = "2026-12-14T{:02d}:30:00Z".format(hour)
    return at(0, hour=hour)


class FakeSite(FakeFirestore):
    """W1a's fake, with the site's actions and notes to query."""

    def __init__(self):
        super().__init__()
        self.actions, self.notes = {}, {}
        self.read_time = READ_TIME
        self.queries = []

    def act(self, action_id, when, kind, opp, person="Dee Rowan", owner="Ana", **fields):
        self.actions[action_id] = dict(fields, type=kind, opp=opp, by="dee@site.test", name=person, owner=owner,
                                       app="test", at=when)

    def run_query(self, query):
        self.queries.append(query)
        name = query["from"][0]["collectionId"]
        test = query["where"]["fieldFilter"]
        key, after = test["field"]["fieldPath"], fs.from_value(test["value"])
        source = self.actions if name == "actions" else self.notes
        docs = sorted(((name + "/" + i, dict(d)) for i, d in source.items() if d[key] > after),
                      key=lambda item: item[1][key])
        return {"docs": docs, "readTime": self.read_time}


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "team.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "owners", ["Ana", "Ben", "Cara"])
    db.set_setting(conn, "internal_domains", ["jojo.example"])
    heron = add_party(repo, "Heron Cargo", country="Chile")
    ids = {}

    def case(opp, status="New", **extra):
        extra.setdefault("scope", "In")
        ids[opp] = add_enquiry(repo, opp, heron, received="2026-12-01", origin_loc="SAI", dest_loc="MIA",
                               service_mode="Air", air_weight_kg=410.0,
                               source_subject="RATE REQUEST // SAI - MIA // {}".format(opp), **extra)
        if status != "New":
            repo.set_status(ids[opp], status, "2026-12-03")
        return ids[opp]

    case("OPP-6901")
    case("OPP-6902", "Quoted")
    case("OPP-6903", "Lost")
    case("OPP-6904", "Quoted")
    case("OPP-6905", "Quoted")
    case("OPP-6906")
    case("OPP-6907", "Quoted")
    repo.commit()
    yield {"conn": conn, "repo": repo, "path": path, "ids": ids, "tmp": tmp_path, "state": tmp_path / "fs.json",
           "record": tmp_path / "exports.json", "reports": tmp_path / "reports"}
    conn.close()


def run(w, fake, **kw):
    kw.setdefault("today", TODAY)
    result = fs.sync(w["path"], client=fake, state_file=w["state"], reports_dir=w["reports"], record=w["record"], **kw)
    w["conn"].rollback()                       # read what the run's own connection wrote
    return result


def events(w, opp):
    return w["conn"].execute("SELECT * FROM status_event WHERE enquiry_id = ? ORDER BY id", (w["ids"][opp],)).fetchall()


def answer(fake, action_id):
    return fake.docs["actionResults/" + action_id]


# --------------------------------------------------------- the types --
def test_each_type_is_applied_answered_and_meta_sync_comes_last(world):
    w = world
    fake = FakeSite()
    fake.act("a1", at(1), "assign", "OPP-6901", to="Ana")
    fake.act("a2", at(2), "quote", "OPP-6901", date="2026-12-14", what="Air SAI - MIA all in", sentBy="Ben")
    fake.act("a3", at(3), "quote", "OPP-6902", date="2026-12-14", what="Lower rate via MIA", sentBy="Cara")
    fake.act("a4", at(4), "followup", "OPP-6905", date="2026-12-14", how="WhatsApp", answered=True,
             note="Rang them on WhatsApp")
    fake.act("a5", at(5), "waiting", "OPP-6904", date="2026-12-14", what="Can you include the customs fee?",
             who="Cara")
    result = run(w, fake)
    assert result.ok, result.reason
    conn = w["conn"]
    case = {r["opp_no"]: r for r in conn.execute("SELECT * FROM enquiry")}
    # assign: Handled by, the sheet's way
    assert case["OPP-6901"]["owner"] == "Ana"
    assert answer(fake, "a1") == {"ok": True, "msg": "OPP-6901 is now handled by Ana.", "by": "dee@site.test",
                                  "type": "assign", "opp": "OPP-6901", "at": "REQUEST_TIME"}
    # quote on a New case: her Quoted button, with what and who sent it
    quoted = events(w, "OPP-6901")[-1]
    assert (quoted["status"], quoted["kind"], quoted["at"], quoted["source"]) == ("Quoted", "status", "2026-12-14", "web")
    assert quoted["note"] == "Air SAI - MIA all in (sent by Ben)" and case["OPP-6901"]["status"] == "Quoted"
    # quote on a Quoted case: her Re-quoted button
    requote = events(w, "OPP-6902")[-1]
    assert (requote["kind"], requote["note"]) == ("requote", "Re-quote: Lower rate via MIA (sent by Cara)")
    assert answer(fake, "a3")["msg"] == "OPP-6902 re-quoted on 2026-12-14."
    # follow-up by WhatsApp, answered: FU1 and a reply that does not ask
    fu, reply = events(w, "OPP-6905")[-2:]
    assert (fu["kind"], fu["seq"], fu["channel"], fu["note"], fu["source"]) == \
        ("followup", 1, "whatsapp", "Rang them on WhatsApp", "web")
    assert detect_followups.note_kind(reply["note"]) == "noted" and reply["source"] == "web"
    assert answer(fake, "a4")["msg"] == "FU1 on OPP-6905 logged (WhatsApp, 2026-12-14). Their answer was logged too."
    row = conn.execute("SELECT * FROM v_enquiry WHERE opp_no = 'OPP-6905'").fetchone()
    assert tracking.fu1_answered(row["last_event_on"], row["last_fu_on"])
    # waiting: a question that needs an answer; Handled by follows
    asked = events(w, "OPP-6904")[-1]
    assert detect_followups.note_kind(asked["note"]) == "asks" and asked["note"].startswith("Reply from Heron Cargo")
    assert case["OPP-6904"]["owner"] == "Cara"
    # recorded once each, with the events they wrote
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM site_action")}
    assert set(rows) == {"a1", "a2", "a3", "a4", "a5"} and all(r["answered_at"] for r in rows.values())
    assert json.loads(rows["a4"]["event_ids"]) == [fu["id"], reply["id"]]
    assert rows["a5"]["by_name"] == "Dee Rowan" and json.loads(rows["a5"]["fields"])["who"] == "Cara"
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE source = 'web'").fetchone()[0] > 0
    # the lists already show it, and who logged it; pulledAt the readTime, last
    pending = fake.docs["lists/assign"]["pending"]
    assert [(p["opp"], p["by"], p["owner"]) for p in pending] == [("OPP-6904", "Dee Rowan", "Cara")]
    tl = fake.docs["cases/OPP-6905"]["tl"]
    assert tl[1] == {"at": "2026-12-14", "text": "FU1 sent", "sub": "whatsapp · logged by Dee Rowan",
                     "src": "web"}
    assert fake.commits[0] == ["actionResults/a{}".format(n) for n in range(1, 6)]
    assert fake.commits[-1] == ["meta/sync"] and fake.docs["meta/sync"]["pulledAt"] == READ_TIME
    assert "sync W3" in fake.docs["meta/sync"]["version"]


def test_each_refusal_says_why_and_writes_nothing(world):
    w = world
    fake = FakeSite()
    fake.act("r1", at(1), "followup", "OPP-6999", date="2026-12-14", how="Email")
    fake.act("r2", at(2), "followup", "OPP-6903", date="2026-12-14", how="Email")
    fake.act("r3", at(3), "quote", "OPP-6903", date="2026-12-14", what="Again", sentBy="Ben")
    fake.act("r4", at(4), "waiting", "OPP-6901", date="2026-12-14", what="Any news?", who="")
    fake.act("r5", at(5), "assign", "OPP-6901", to="Zed")
    fake.act("r6", at(6), "archive", "OPP-6901")
    fake.act("r7", at(7), "followup", "OPP-6902", date="14/12/2026", how="Email")
    before = w["conn"].execute("SELECT COUNT(*) FROM status_event").fetchone()[0]
    assert run(w, fake).ok
    said = {i: answer(fake, i) for i in ("r1", "r2", "r3", "r4", "r5", "r6", "r7")}
    assert not any(a["ok"] for a in said.values())
    assert said["r1"]["msg"] == "OPP-6999 is not in OppList, so the follow-up was not logged."
    assert said["r2"]["msg"] == "OPP-6903 is closed (Lost), so the follow-up was not logged."
    assert said["r3"]["msg"] == "OPP-6903 is closed (Lost), so the quote was not logged."
    assert said["r4"]["msg"] == "OPP-6901 is New, not Quoted, so the question was not logged."
    assert said["r5"]["msg"] == "'Zed' is not a Handled by name in OppList, so OPP-6901 was not assigned."
    assert said["r6"]["msg"] == "OppList does not know the action 'archive', so nothing was done on OPP-6901."
    assert said["r7"]["msg"] == "OPP-6902: '14/12/2026' is not a date, so the follow-up was not logged."
    assert w["conn"].execute("SELECT COUNT(*) FROM status_event").fetchone()[0] == before
    assert w["conn"].execute("SELECT owner FROM enquiry WHERE opp_no = 'OPP-6901'").fetchone()[0] is None
    assert {r[0] for r in w["conn"].execute("SELECT result FROM site_action")} == {"refused"}


def test_the_same_action_is_applied_once_and_the_order_is_at(world):
    w = world
    fake = FakeSite()
    fake.act("late", at(9), "assign", "OPP-6901", to="Ben")         # written first, later at
    fake.act("early", at(2), "assign", "OPP-6901", to="Ana")
    fake.act("fu", at(5), "followup", "OPP-6902", date="2026-12-14", how="Email")
    assert run(w, fake).ok
    assert w["conn"].execute("SELECT owner FROM enquiry WHERE opp_no = 'OPP-6901'").fetchone()[0] == "Ben"
    assert [r[0] for r in w["conn"].execute("SELECT id FROM site_action ORDER BY taken_at, rowid")] == \
        ["early", "fu", "late"]
    # the query comes again from the start: nothing is applied twice, nothing answered twice
    state = json.loads(w["state"].read_text(encoding="utf-8"))
    state["pulled_at"] = ""
    w["state"].write_text(json.dumps(state), encoding="utf-8")
    commits = len(fake.commits)
    again = run(w, fake)
    assert again.ok and again.pull.skipped == 3 and again.pull.applied == 0
    assert [e["kind"] for e in events(w, "OPP-6902")].count("followup") == 1
    assert not any(c[0].startswith("actionResults/") for c in fake.commits[commits:])


def test_an_action_that_throws_stops_the_pull_and_the_next_run_finishes_it(world, monkeypatch):
    w = world
    fake = FakeSite()
    fake.act("b1", at(1), "assign", "OPP-6901", to="Ana")
    fake.act("b2", at(2), "followup", "OPP-6902", date="2026-12-14", how="Email")
    fake.act("b3", at(3), "assign", "OPP-6904", to="Ben")
    real = fp._HANDLERS["followup"]

    def broken(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setitem(fp._HANDLERS, "followup", broken)
    first = run(w, fake)
    assert not first.ok and "pull stopped at action b2" in first.reason and "disk full" in first.reason
    assert [r[0] for r in w["conn"].execute("SELECT id FROM site_action")] == ["b1"]
    assert "actionResults/b1" in fake.docs and "actionResults/b2" not in fake.docs
    # pulledAt does not move past it; the lists are still pushed, meta/sync last
    assert fake.docs["meta/sync"]["pulledAt"] == fp.iso(at(1)) and fake.commits[-1] == ["meta/sync"]
    assert "stopped" in fs.status(w["state"])["pull"]
    monkeypatch.setitem(fp._HANDLERS, "followup", real)
    second = run(w, fake)
    assert second.ok and second.pull.applied == 2
    assert w["conn"].execute("SELECT owner FROM enquiry WHERE opp_no = 'OPP-6904'").fetchone()[0] == "Ben"
    assert fake.docs["meta/sync"]["pulledAt"] == READ_TIME


# --------------------------------------------------------- duplicates --
def test_the_site_second_voids_the_mail_event_and_keeps_its_mid(world):
    w = world
    repo = w["repo"]
    repo.add_followup(w["ids"]["OPP-6905"], "2026-12-10", channel="mail",
                      note="FU detected: RE: RATE REQUEST [mid:<fu6905@heron.example>]", source="mail")
    repo.set_status(w["ids"]["OPP-6906"], "Quoted", "2026-12-12", note="Quoted from the mail", source="mail")
    repo.commit()
    fake = FakeSite()
    fake.act("d1", at(1), "followup", "OPP-6905", date="2026-12-11", how="Email", note="Chased by mail")
    fake.act("d2", at(2), "quote", "OPP-6906", date="2026-12-12", what="Air all in", sentBy="Ben")
    assert run(w, fake).ok
    mail_fu, site_fu = [e for e in events(w, "OPP-6905") if e["kind"] == "followup"]
    assert mail_fu["voided"] == 1 and site_fu["voided"] == 0 and site_fu["seq"] == 1
    assert site_fu["note"] == "Chased by mail [mid:<fu6905@heron.example>]"
    assert "replaced by this one" in answer(fake, "d1")["msg"]
    quotes = [e for e in events(w, "OPP-6906") if e["status"] == "Quoted"]
    assert [(q["source"], q["voided"], q["kind"]) for q in quotes] == [("mail", 1, "status"), ("web", 0, "status")]
    assert w["conn"].execute("SELECT status FROM enquiry WHERE opp_no = 'OPP-6906'").fetchone()[0] == "Quoted"
    assert w["conn"].execute("SELECT COUNT(*) FROM status_event").fetchone()[0] > 0     # nothing deleted


def mail(sent, internal, top, mid, sender="Ines Quarry", address="ines@heron.example"):
    return SimpleNamespace(sender_internal=internal, sender_address=address, sender_name=sender, sent_at=sent,
                           received_at=sent, received_on=sent[:10], body_excerpt=top, subject="RE: RATE REQUEST",
                           message_id=mid, entry_id="",
                           to_addresses=["ines@heron.example"] if internal else [])   # T4: ours go to the requester


def test_the_mail_second_is_skipped_as_already_on_the_case(world, monkeypatch):
    w = world
    fake = FakeSite()
    fake.act("m1", at(1), "followup", "OPP-6907", date="2026-12-09", how="Phone")
    fake.act("m2", at(2), "waiting", "OPP-6907", date="2026-12-09", what="Is the rate valid until January?", who="")
    assert run(w, fake).ok
    thread = [mail("2026-12-10 10:00", True, "Just following up on our quote, any update?", "<fu6907@jojo.example>",
                   sender="Desk", address="desk@jojo.example"),
              mail("2026-12-10 15:00", False, "Could you confirm the rate is valid until January?", "<re6907@heron.example>")]
    monkeypatch.setattr(detect_followups, "thread_of", lambda search, row, conn=None: thread)
    monkeypatch.setattr(detect_followups, "requester_domains", lambda conn, search, row: {"heron.example"})
    conn = w["conn"]
    row = conn.execute("SELECT * FROM v_enquiry WHERE opp_no = 'OPP-6907'").fetchone()
    report = detect_followups.Report()
    before = len(events(w, "OPP-6907"))
    detect_followups.scan_case(conn, db.Repository(conn, source="import"), None, row, {"jojo.example"}, report,
                               dry_run=False)
    assert (report.followups, report.replies, report.skipped) == (0, 0, 2)
    assert len(events(w, "OPP-6907")) == before      # T4b: the follow-up is not an answer either


# ------------------------------------------------- Pending on us at once --
def test_a_question_from_the_site_is_pending_at_once_and_answered_takes_it_off(world):
    w = world
    conn = w["conn"]
    # a reply the scan finds today waits for tomorrow, as before
    w["repo"].add_note(w["ids"]["OPP-6902"], TODAY, detect_followups.reply_note("Heron Cargo", "Could you add insurance?",
                                                                               kind="asks"), source="mail")
    w["repo"].commit()
    fake = FakeSite()
    fake.act("w1", at(1), "waiting", "OPP-6904", date=TODAY, what="Can you quote the trucking too?", who="")
    fake.act("w2", at(2), "answered", "OPP-6905", date=TODAY, note="Sent the trucking rate")
    assert run(w, fake).ok
    assert [p.opp_no for p in assign_list.pending_listed(conn, TODAY)] == ["OPP-6904"]
    rows = {r["opp"]: r for r in fake.docs["lists/inquiries"]["rows"]}
    assert rows["OPP-6904"]["pend"]["by"] == "Dee Rowan" and rows["OPP-6904"]["pend"]["on"] == TODAY
    assert rows["OPP-6902"]["pend"] is None
    # nothing pending: accepted, nothing written
    assert answer(fake, "w2") == dict(answer(fake, "w2"), ok=True,
                                      msg="OPP-6905 had no question waiting, so nothing was written.")
    assert [e for e in events(w, "OPP-6905") if e["source"] == "web"] == []
    # answered on the same day: after the question, off Pending on us, the ladder again
    fake.act("w3", later(fake, 19), "answered", "OPP-6904", date=TODAY, note="Sent it")
    assert run(w, fake).ok
    last = events(w, "OPP-6904")[-1]
    assert last["note"] == "Answered by Dee Rowan: Sent it" and last["at"] == TODAY
    assert assign_list.pending_listed(conn, TODAY) == []
    assert answer(fake, "w3")["msg"] == "OPP-6904 left Pending on us: answered by Dee Rowan on 2026-12-14."
    row = conn.execute("SELECT * FROM v_enquiry WHERE opp_no = 'OPP-6904'").fetchone()
    assert tracking.ladder(conn, row, TODAY)["clock"] == TODAY


# --------------------------------------------------------------- notes --
def test_a_site_note_stays_on_the_site(world):
    """CX1: a note with a case is not taken in - no quick note, no site_note
    row - and no case document carries quick notes."""
    w = world
    conn = w["conn"]
    fake = FakeSite()
    base = dict(remind="", done=False, by="dee@site.test", name="Dee Rowan", at=at(1))
    fake.notes["n1"] = dict(base, text="Ask Ben about the crate size", opp="OPP-6902", updatedAt=at(1))
    fake.notes["n2"] = dict(base, text="Forwarded by Ana from the Lima desk", opp="OPP-6901", updatedAt=at(2))
    first = run(w, fake)
    assert first.ok and not first.pull.error
    assert conn.execute("SELECT COUNT(*) FROM case_note").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM site_note").fetchone()[0] == 0
    assert "qnotes" not in fake.docs["cases/OPP-6902"] and "notes" not in first.pull.line()
    assert len(fake.notes) == 2                                           # the site's own notes, as they were


# ----------------------------------------------------------- schema v43 --
def test_v43_rebuilds_status_event_and_keeps_every_row(tmp_path):
    path = tmp_path / "old.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    case = add_enquiry(repo, "OPP-6950", add_party(repo, "Heron Cargo", country="Chile"), received="2026-12-01")
    repo.set_status(case, "Quoted", "2026-12-03")
    repo.add_followup(case, "2026-12-05", channel="call")
    repo.commit()
    count = conn.execute("SELECT COUNT(*) FROM status_event").fetchone()[0]
    # back to v42: the old CHECK lists, no site tables
    old = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'status_event'").fetchone()[0]
    old = old.replace(",'whatsapp','teams','other'", "").replace(",'web'", "")
    conn.execute("PRAGMA foreign_keys = OFF")
    for (view,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall():
        conn.execute("DROP VIEW {}".format(view))
    conn.execute(old.replace("CREATE TABLE status_event", "CREATE TABLE status_event_old", 1))
    conn.execute("INSERT INTO status_event_old SELECT * FROM status_event")
    conn.execute("DROP TABLE status_event")
    conn.execute("ALTER TABLE status_event_old RENAME TO status_event")
    conn.execute("DROP TABLE site_action")
    conn.execute("DROP TABLE site_note")
    conn.execute("UPDATE setting SET value = '42' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION >= 43
    assert any(n.startswith("status_event rebuilt ({} rows".format(count)) for n in notes)
    assert any(n.startswith("restore point") for n in notes)
    assert conn.execute("SELECT COUNT(*) FROM status_event").fetchone()[0] == count
    assert conn.execute("SELECT COUNT(*) FROM v_enquiry").fetchone()[0] == 1
    repo = db.Repository(conn, source="test")
    repo.add_followup(case, "2026-12-08", channel="teams", source="web")
    repo.commit()
    assert conn.execute("SELECT channel, source, seq FROM status_event ORDER BY id DESC LIMIT 1").fetchone()[:] == \
        ("teams", "web", 2)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO site_action (id, type, at, result) VALUES ('x', 'assign', 'now', 'maybe')")
    conn.close()
