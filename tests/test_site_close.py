"""Batch W2b (2026-10-29): Won or Lost from the team site, OppList's half
(..\\OppListWeb\\INTERFACE.md section 6). A colleague's close is refused with
a message or taken in as one pending status suggestion, source 'web'; its
pasted mail is saved under data\\site-evidence\\<id>\\, its forwarded one is
looked for in the mail archive until it is there; pendingClose is pushed
while it waits; her Accept or Dismiss goes back as actionResults once per
decision.

Every company, person, mail, place and date here is invented; cases from
OPP-7601.
"""

from __future__ import annotations

import base64
import datetime as dt
from pathlib import Path

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, firebase_sync as fs, mail_archive, paths, site_close, status, suggestions
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app
from test_firebase_pull import FakeSite

TODAY = "2026-12-14"
MIRA = ("mira@lodestarfreight.test", "Mira Quell", "Cara")
PIC1, PIC2 = b"\xff\xd8\xff\xe0 first picture", b"\xff\xd8\xff\xe0 second picture"
PASTED = "Hi Cara,\nWe booked with another forwarder this time, their rate was lower.\nRegards, Oskar"


def at(minute, hour=15):
    return dt.datetime(2026, 12, 14, hour, minute, 0, tzinfo=dt.timezone.utc)


def jpeg(data):
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def close(fake, action_id, when, opp, result, person=MIRA, **fields):
    fields.setdefault("date", "2026-12-12")
    fields.setdefault("reason", "")
    fields.setdefault("rateFrom", "")
    fields.setdefault("evidence", "forwarded")
    fake.actions[action_id] = dict(fields, type="close", opp=opp, result=result, by=person[0], name=person[1],
                                   owner=person[2], app="test", at=when)


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    path = tmp_path / "close.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "owners", ["Ana", "Ben", "Cara"])
    db.set_setting(conn, "internal_domains", ["lodestarfreight.test"])
    requester = add_party(repo, "Orrin Cargo", country="Chile")
    vendors = {"Tarnwick Freight": add_party(repo, "Tarnwick Freight", country="USA"),
               "Lodestar QVR": add_party(repo, "Lodestar QVR", ptype="Office", country="USA"),
               "Brisk Air Lines": add_party(repo, "Brisk Air Lines", ptype="Carrier", country="USA")}
    ids, requests = {}, {}

    def case(opp, status_="Quoted", asked=(), **extra):
        extra.setdefault("scope", "In")
        ids[opp] = add_enquiry(repo, opp, requester, received="2026-12-01", origin_loc="SAI", dest_loc="MIA",
                               service_mode="Air", air_weight_kg=260.0,
                               source_subject="RATE REQUEST // LIM - ORD // {}".format(opp), **extra)
        for key in asked:
            requests[(opp, key)] = repo.insert("vendor_request", {
                "enquiry_id": ids[opp], "vendor_party_id": vendors[key], "asked_on": "2026-12-02", "source": "manual"})
        if status_ != "New":
            repo.set_status(ids[opp], status_, "2026-12-03")
        return ids[opp]

    case("OPP-7601", "New")
    case("OPP-7602", asked=("Tarnwick Freight", "Lodestar QVR"))
    case("OPP-7603", "Lost")
    case("OPP-7604", handled_by="YUL")
    case("OPP-7605")
    case("OPP-7606")
    case("OPP-7607")
    case("OPP-7608", asked=("Lodestar QVR", "Brisk Air Lines"))
    repo.commit()
    yield {"conn": conn, "repo": repo, "path": path, "ids": ids, "requests": requests, "vendors": vendors,
           "archive": archive, "state": tmp_path / "fs.json", "record": tmp_path / "exports.json",
           "reports": tmp_path / "reports", "tmp": tmp_path}
    conn.close()


def run(w, fake):
    result = fs.sync(w["path"], client=fake, state_file=w["state"], reports_dir=w["reports"], record=w["record"],
                     today=TODAY)
    w["conn"].rollback()
    assert result.ok, result.reason
    return result


def answer(fake, action_id):
    return fake.docs["actionResults/" + action_id]


def times_answered(fake, action_id):
    return sum(paths_.count("actionResults/" + action_id) for paths_ in fake.commits)


def web_suggestions(w, opp):
    return w["conn"].execute("SELECT * FROM suggestion WHERE enquiry_id = ? AND source = 'web' ORDER BY id",
                             (w["ids"][opp],)).fetchall()


def row_of(fake, opp):
    return next(r for r in fake.docs["lists/inquiries"]["rows"] if r["opp"] == opp)


def store(w, *mails):
    conn = mail_archive.open_archive(w["archive"])
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in mails])
    conn.commit()
    conn.close()


def forward(at_, sender, subject, mid):
    return MailInfo(subject=subject, received_on=at_[:10], received_at=at_, sent_at=at_, entry_id="EID-" + mid,
                    conversation_id="CONV-" + mid, conversation_topic=subject, sender_name=sender[1],
                    sender_address=sender[0], sender_internal=True, to_addresses=["josie@lodestarfreight.test"],
                    body_excerpt="---------- Forwarded message ----------\nWe confirm the booking.\n",
                    folder_path="\\Mailbox\\Inbox", display_to="Josie", message_id="<{}@x>".format(mid))


# ----------------------------------------------------------- refusals --
def test_each_refusal_says_why_and_writes_nothing(world):
    w = world
    fake = FakeSite()
    close(fake, "r1", at(1), "OPP-7699", "Won")
    close(fake, "r2", at(2), "OPP-7603", "Won")
    close(fake, "r3", at(3), "OPP-7604", "Lost", reason="Price too high")
    close(fake, "r4", at(4), "OPP-7605", "Won", date="2026-12-32")
    close(fake, "r5", at(5), "OPP-7605", "Won", date="2026-12-15")
    close(fake, "r6", at(6), "OPP-7605", "Maybe")
    close(fake, "r7", at(7), "OPP-7606", "Won")
    close(fake, "r8", at(8), "OPP-7606", "Lost", reason="Price too high")
    result = run(w, fake)
    assert (result.pull.applied, result.pull.refused) == (1, 7)
    said = {i: answer(fake, i) for i in ("r1", "r2", "r3", "r4", "r5", "r6", "r8")}
    assert all(a["ok"] is False and a["type"] == "close" for a in said.values())
    assert said["r1"]["msg"] == "OPP-7699 is not in OppList, so the Won was not sent."
    assert said["r2"]["msg"] == "OPP-7603 is closed (Lost) already, so the Won was not sent."
    assert said["r3"]["msg"] == "OPP-7604 is handled by YUL, so the Lost was not sent."
    assert said["r4"]["msg"] == "OPP-7605: '2026-12-32' is not a date, so the Won was not sent."
    assert said["r5"]["msg"] == "OPP-7605: 2026-12-15 is after today, so the Won was not sent."
    assert said["r6"]["msg"] == "OPP-7605: 'Maybe' is not Won or Lost, so nothing was sent."
    # a second close while one waits
    assert answer(fake, "r7") == {"ok": True, "msg": "OPP-7606 Won is waiting for Iris's confirmation.",
                                  "by": MIRA[0], "type": "close", "opp": "OPP-7606", "at": "REQUEST_TIME"}
    assert said["r8"]["msg"] == "OPP-7606 already has a Won waiting for Iris."
    assert [s["proposed_status"] for s in w["conn"].execute("SELECT * FROM suggestion WHERE source = 'web'")] == ["Won"]
    assert [r["status"] for r in w["conn"].execute("SELECT status FROM enquiry WHERE opp_no LIKE 'OPP-760%' "
                                                   "ORDER BY opp_no")] == \
        ["New", "Quoted", "Lost", "Quoted", "Quoted", "Quoted", "Quoted", "Quoted"]


# ---------------------------------------------------- a Lost, pasted --
def test_a_lost_pasted_saves_the_pictures_and_text_and_waits_for_her(world):
    w = world
    fake = FakeSite()
    close(fake, "c1", at(1), "OPP-7601", "Lost", reason="Went with another forwarder", evidence="pasted")
    fake.docs["evidence/c1"] = {"images": [jpeg(PIC1), jpeg(PIC2)], "text": PASTED, "by": MIRA[0],
                                "name": MIRA[1], "at": "REQUEST_TIME"}
    run(w, fake)
    s, = web_suggestions(w, "OPP-7601")
    assert (s["kind"], s["state"], s["proposed_status"], s["closed_reason"], s["proposed_date"]) == \
        ("status", "pending", "Lost", "Went with another forwarder", "2026-12-12")
    assert (s["evidence_sender"], s["evidence_date"]) == ("Mira Quell", "2026-12-12")
    assert s["evidence_quote"] == " ".join(PASTED.split())
    folder = Path(paths.DATA_DIR) / "site-evidence" / "c1"
    assert (folder / "1.jpg").read_bytes() == PIC1 and (folder / "2.jpg").read_bytes() == PIC2
    assert (folder / "text.txt").read_text(encoding="utf-8") == PASTED
    assert answer(fake, "c1")["msg"] == "OPP-7601 Lost is waiting for Iris's confirmation."
    # pushed: pendingClose on the row and the case, lostReasons on meta/sync
    mark = {"result": "Lost", "by": "Mira Quell", "at": "2026-12-12", "id": "c1", "evidence": "pasted"}
    assert row_of(fake, "OPP-7601")["pendingClose"] == mark
    assert fake.docs["cases/OPP-7601"]["pendingClose"] == mark
    assert row_of(fake, "OPP-7602")["pendingClose"] is None
    assert fake.docs["meta/sync"]["lostReasons"] == status.closed_reasons(w["conn"], "Lost")
    # the case page shows who, the pictures (full size behind them) and the text
    w["conn"].commit()
    app = create_app(w["path"])
    app.config["TESTING"] = True
    client = app.test_client()
    body = client.get("/enquiries/{}".format(w["ids"]["OPP-7601"])).data.decode("utf-8")
    assert "Sent on the team site by <strong>Mira Quell</strong>" in body
    assert body.count('src="/site-evidence/c1/') == 2 and 'href="/site-evidence/c1/1.jpg"' in body
    assert "We booked with another forwarder this time" in body
    picture = client.get("/site-evidence/c1/2.jpg")
    assert picture.status_code == 200 and picture.data == PIC2 and picture.mimetype == "image/jpeg"
    assert client.get("/site-evidence/c1/text.txt").status_code == 404
    assert client.get("/site-evidence/c1/9.jpg").status_code == 404


def test_a_missing_evidence_document_is_not_a_refusal(world):
    w = world
    fake = FakeSite()
    close(fake, "c1", at(1), "OPP-7601", "Lost", reason="Price too high", evidence="pasted")
    run(w, fake)
    s, = web_suggestions(w, "OPP-7601")
    assert s["state"] == "pending" and s["evidence_quote"] == site_close.UNREADABLE
    assert answer(fake, "c1")["ok"] is True
    assert site_close.view_of(w["conn"], s)["unreadable"] is True
    # it shows up later: the next pull fetches it
    fake.docs["evidence/c1"] = {"images": [jpeg(PIC1)], "text": ""}
    run(w, fake)
    s, = web_suggestions(w, "OPP-7601")
    assert s["evidence_quote"] == "Pictures of the mail"
    assert site_close.view_of(w["conn"], s)["images"] == ["1.jpg"]


# ------------------------------------------------- a Won, forwarded --
def test_a_won_forwarded_is_found_in_the_archive_on_a_later_scan(world):
    w = world
    fake = FakeSite()
    close(fake, "c2", at(1), "OPP-7602", "Won", rateFrom="Tarnwick Freight")
    # not hers: another sender, and one of hers from before the day before
    store(w, forward("2026-12-12 09:00", ("ana@lodestarfreight.test", "Ana"), "FW: OPP-7602 booking", "o1"),
          forward("2026-12-10 09:00", MIRA, "FW: OPP-7602 first quote", "o2"))
    run(w, fake)
    s, = web_suggestions(w, "OPP-7602")
    assert (s["evidence_quote"], s["entry_id"]) == ("Forwarded to Iris", None)
    view = site_close.view_of(w["conn"], s)
    assert (view["evidence"], view["found"]) == ("forwarded", False)
    assert row_of(fake, "OPP-7602")["pendingClose"]["evidence"] == "forwarded"
    # her forward arrives; the next run finds it
    store(w, forward("2026-12-14 10:20", MIRA, "FW: RE: OPP-7602 RATE REQUEST // LIM - ORD", "f1"))
    run(w, fake)
    s, = web_suggestions(w, "OPP-7602")
    assert (s["entry_id"], s["message_id"]) == ("EID-f1", "<f1@x>")
    assert s["evidence_quote"] == "Forwarded to Iris · 2026-12-14 10:20 · FW: RE: OPP-7602 RATE REQUEST // LIM - ORD"
    app = create_app(w["path"])
    app.config["TESTING"] = True
    body = app.test_client().get("/enquiries/{}".format(w["ids"]["OPP-7602"])).data.decode("utf-8")
    assert 'name="entry_id" value="EID-f1"' in body and "Forwarded mail not seen yet" not in body
    # Edit: Won with the request of the vendor they named preselected
    assert '<option value="{}" selected>Tarnwick Freight</option>'.format(
        w["requests"][("OPP-7602", "Tarnwick Freight")]) in body


# ------------------------------------------------------- rate used --
def accept(w, opp):
    s, = [r for r in web_suggestions(w, opp) if r["state"] == "pending"]
    return suggestions.accept(db.Repository(w["conn"]), s["id"], today=TODAY)


def test_the_rate_used_comes_from_the_vendor_own_or_nothing(world):
    w = world
    fake = FakeSite()
    close(fake, "c2", at(1), "OPP-7602", "Won", rateFrom="Tarnwick Freight")
    close(fake, "c5", at(2), "OPP-7605", "Won", rateFrom="own")
    close(fake, "c6", at(3), "OPP-7606", "Won", rateFrom="")
    close(fake, "c8", at(4), "OPP-7608", "Won", rateFrom="Lodestar QVR")
    run(w, fake)
    for opp in ("OPP-7602", "OPP-7605", "OPP-7606", "OPP-7608"):
        assert "set to Won on 2026-12-12" in accept(w, opp)
    case = {r["opp_no"]: r for r in w["conn"].execute("SELECT * FROM enquiry")}
    assert {o: case[o]["status"] for o in ("OPP-7602", "OPP-7605", "OPP-7606", "OPP-7608")} == dict.fromkeys(
        ("OPP-7602", "OPP-7605", "OPP-7606", "OPP-7608"), "Won")
    # a vendor key: an agent, its request awarded
    assert (case["OPP-7602"]["rate_used"], case["OPP-7602"]["awarded_vendor_id"]) == \
        ("agent", w["vendors"]["Tarnwick Freight"])
    awarded = w["conn"].execute("SELECT id FROM vendor_request WHERE enquiry_id = ? AND awarded = 1",
                                (w["ids"]["OPP-7602"],)).fetchall()
    assert [r["id"] for r in awarded] == [w["requests"][("OPP-7602", "Tarnwick Freight")]]
    # one of our offices's key: office, that office
    assert (case["OPP-7608"]["rate_used"], case["OPP-7608"]["awarded_vendor_id"]) == \
        ("office", w["vendors"]["Lodestar QVR"])
    assert (case["OPP-7605"]["rate_used"], case["OPP-7605"]["awarded_vendor_id"]) == ("own", None)
    assert case["OPP-7606"]["rate_used"] is None                           # R6 asks later
    edited = w["conn"].execute("SELECT edited FROM suggestion WHERE source = 'web'").fetchall()
    assert [r["edited"] for r in edited] == [0, 0, 0, 0]


def test_a_merged_vendor_is_followed_and_its_type_decides(world):
    w = world
    repo = w["repo"]
    kept = add_party(repo, "Brisk Air Cargo Lines", ptype="Carrier", country="USA")
    repo.update("party", w["vendors"]["Brisk Air Lines"], {"merged_into": kept})
    repo.commit()
    fake = FakeSite()
    close(fake, "c8", at(1), "OPP-7608", "Won", rateFrom="Brisk Air Lines")
    run(w, fake)
    s, = web_suggestions(w, "OPP-7608")
    assert suggestions.plan(w["conn"], s, TODAY).rate == ("carrier", kept, None, None)
    assert suggestions.prefill(w["conn"], s, TODAY)["rate_used"] == "carrier"


# --------------------------------------------- the scan's suggestion --
def test_the_scans_pending_closing_is_dismissed_and_not_suggested_again(world):
    w = world
    repo = w["repo"]
    scan = suggestions.record(repo, w["ids"]["OPP-7607"], "OPP-7607", {
        "kind": "status", "proposed_status": "Lost", "closed_reason": "Price too high",
        "proposed_date": "2026-12-11", "evidence_sender": "Oskar <oskar@orrincargo.test>",
        "evidence_date": "2026-12-11", "evidence_quote": "we will not go ahead", "source": "scan"})
    quoted = suggestions.record(repo, w["ids"]["OPP-7607"], "OPP-7607", {
        "kind": "status", "proposed_status": "Quoted", "proposed_date": "2026-12-04", "evidence_quote": "rate sent",
        "source": "scan"})
    repo.commit()
    fake = FakeSite()
    close(fake, "c7", at(1), "OPP-7607", "Lost", reason="Price too high")
    run(w, fake)
    gone = w["conn"].execute("SELECT * FROM suggestion WHERE id = ?", (scan,)).fetchone()
    assert (gone["state"], gone["decided_as"]) == ("dismissed", "dismissed")
    assert gone["note"] == "Mira Quell sent Lost on the team site on 2026-12-12"
    # not a closing: left for her
    assert w["conn"].execute("SELECT state FROM suggestion WHERE id = ?", (quoted,)).fetchone()[0] == "pending"

    class Proposal:
        verdict, opp_no, proposed_status, closed_reason, proposed_date = "change", "OPP-7607", "Won", "", "2026-12-13"
        evidence_sender, evidence_date, evidence_quote, confidence, note = "Oskar", "2026-12-13", "go ahead", "high", ""

    counts = suggestions.from_proposals(db.Repository(w["conn"], source="scan"), [Proposal()])
    assert (counts["written"], counts["same"]) == (0, 1)


# ------------------------------------------------- her decision back --
def test_accept_closes_the_case_and_the_answer_goes_back_once(world):
    w = world
    fake = FakeSite()
    close(fake, "c1", at(1), "OPP-7601", "Lost", reason="Went with another forwarder", evidence="pasted")
    fake.docs["evidence/c1"] = {"images": [jpeg(PIC1)], "text": PASTED}
    run(w, fake)
    assert times_answered(fake, "c1") == 1
    said = accept(w, "OPP-7601")
    assert said.startswith("OPP-7601 set to Lost on 2026-12-12") and "Mira Quell sees it on the team site" in said
    case = w["conn"].execute("SELECT * FROM v_enquiry WHERE opp_no = 'OPP-7601'").fetchone()
    assert (case["status"], case["closed_reason"]) == ("Lost", "Went with another forwarder")
    run(w, fake)
    assert answer(fake, "c1") == {"ok": True, "msg": "Iris confirmed Lost on OPP-7601.", "by": MIRA[0],
                                  "type": "close", "opp": "OPP-7601", "at": "REQUEST_TIME"}
    assert row_of(fake, "OPP-7601")["pendingClose"] is None
    assert fake.docs["cases/OPP-7601"]["pendingClose"] is None
    run(w, fake)
    assert times_answered(fake, "c1") == 2                                  # once for the take-in, once now


def test_dismiss_and_put_back_each_answer_once(world):
    w = world
    fake = FakeSite()
    close(fake, "c5", at(1), "OPP-7605", "Won", rateFrom="own")
    run(w, fake)
    s, = web_suggestions(w, "OPP-7605")
    repo = db.Repository(w["conn"])
    assert suggestions.dismiss(repo, s["id"]) == \
        "OPP-7605: the Won from Mira Quell was not confirmed; they see it on the team site at the next sync"
    run(w, fake)
    assert answer(fake, "c5")["ok"] is False
    assert answer(fake, "c5")["msg"] == "Iris did not confirm Won on OPP-7605."
    assert row_of(fake, "OPP-7605")["pendingClose"] is None
    assert w["conn"].execute("SELECT status FROM enquiry WHERE opp_no = 'OPP-7605'").fetchone()[0] == "Quoted"
    run(w, fake)
    assert times_answered(fake, "c5") == 2
    # Put back: waiting again, said again
    suggestions.put_back(repo, s["id"])
    run(w, fake)
    assert answer(fake, "c5") == {"ok": True, "msg": "OPP-7605 Won is waiting for Iris's confirmation.",
                                  "by": MIRA[0], "type": "close", "opp": "OPP-7605", "at": "REQUEST_TIME"}
    assert row_of(fake, "OPP-7605")["pendingClose"]["id"] == "c5"
    assert times_answered(fake, "c5") == 3
    suggestions.dismiss(repo, s["id"])
    run(w, fake)
    assert times_answered(fake, "c5") == 4 and answer(fake, "c5")["ok"] is False


def test_the_same_action_twice_is_taken_in_once(world):
    w = world
    fake = FakeSite()
    close(fake, "c6", at(1), "OPP-7606", "Won")
    run(w, fake)
    # the next query returns it again (pulledAt went back)
    state = fs.load_state(w["state"])
    state["pulled_at"] = ""
    fs.save_state(state, w["state"])
    result = run(w, fake)
    assert (result.pull.applied, result.pull.skipped) == (0, 1)
    assert len(web_suggestions(w, "OPP-7606")) == 1
    assert times_answered(fake, "c6") == 1


def test_a_lost_reason_no_longer_in_settings_is_kept_and_edit_picks_one(world):
    w = world
    fake = FakeSite()
    close(fake, "c7", at(1), "OPP-7607", "Lost", reason="Too far inland")
    run(w, fake)
    s, = web_suggestions(w, "OPP-7607")
    assert s["closed_reason"] == "Too far inland"
    assert site_close.view_of(w["conn"], s)["reason_gone"] is True
    repo = db.Repository(w["conn"])
    with pytest.raises(ValueError, match="'Too far inland' is not a Lost reason"):
        suggestions.accept(repo, s["id"], today=TODAY)
    w["conn"].rollback()
    said = suggestions.decide(repo, s, {"action": "lost", "at": "2026-12-12", "note": "", "reason": "Price too high"},
                              TODAY)
    assert said.endswith("(edited)")
    assert w["conn"].execute("SELECT closed_reason FROM enquiry WHERE opp_no = 'OPP-7607'").fetchone()[0] == \
        "Price too high"
