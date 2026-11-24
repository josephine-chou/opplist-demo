"""Batch FX3 item 3 (2026-11-11): a Won or Lost sent on the team site and
accepted in Suggested updates is the site's on the Timeline - source web,
in the action's event_ids, its note what the pasted mail adds - and the
closes accepted before FX3 are repaired once.

Every company, person, place and date here is invented; cases from
OPP-9951, mail on .test domains.
"""

from __future__ import annotations

import json

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, firebase_pull, site_close, suggestions

WHO = ("rena@quillmark.test", "Rena Vosk", "Ana")


@pytest.fixture
def world(tmp_path):
    conn = db.open_db(tmp_path / "close.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "closed_reasons_lost", ["Price too high", "No response from agent", "Other"])
    agent = add_party(repo, "Ostrava Cargo", country="Chile")
    repo.commit()
    n = [9951]

    def close(result="Lost", reason="No response from agent", quote=None, action_id=None):
        """A Quoted case with a close from the site waiting for her."""
        opp = "OPP-{}".format(n[0])
        n[0] += 1
        case = add_enquiry(repo, opp, agent, received="2026-11-02", scope="In", service_mode="Air",
                           source_subject="AIR RATE // {}".format(opp))
        repo.set_status(case, "Quoted", "2026-11-03")
        action_id = action_id or "act{}".format(case)
        repo.insert("site_action", {"id": action_id, "type": "close", "opp_no": opp, "enquiry_id": case,
                                    "by_email": WHO[0], "by_name": WHO[1], "by_owner": WHO[2],
                                    "at": "2026-11-10T15:00:00.000000Z", "result": "applied",
                                    "fields": json.dumps({"result": result, "reason": reason, "date": "2026-11-10"})})
        sid = repo.insert("suggestion", {
            "enquiry_id": case, "opp_no": opp, "kind": "status", "state": "pending", "source": "web",
            "proposed_status": result, "closed_reason": reason if result == "Lost" else None,
            "proposed_date": "2026-11-10", "evidence_sender": WHO[1], "evidence_date": "2026-11-10",
            "evidence_quote": quote, "confidence": "high",
            "note": "Sent on the team site by {} {}".format(WHO[1], site_close.mark(action_id))})
        repo.commit()
        return case, sid, action_id
    yield conn, repo, close
    conn.close()


def closing(conn, case):
    return conn.execute("SELECT * FROM status_event WHERE enquiry_id = ? AND voided = 0 AND kind = 'status' "
                        "ORDER BY id DESC LIMIT 1", (case,)).fetchone()


def event_ids(conn, action_id):
    return json.loads(conn.execute("SELECT event_ids FROM site_action WHERE id = ?", (action_id,)).fetchone()[0]
                      or "[]")


def test_an_accepted_close_is_the_sites_with_the_pasted_text(world):
    conn, repo, close = world
    pasted = "Hello Ana, the shipper booked with their own forwarder in Lima, so we can close this one. " * 6
    case, sid, action = close(quote=pasted.strip())
    suggestions.accept(db.Repository(conn), sid, today="2026-11-11")
    ev = closing(conn, case)
    assert (ev["status"], ev["source"], ev["reason"]) == ("Lost", "web", "No response from agent")
    assert ev["note"] == pasted.strip()[:400]
    assert event_ids(conn, action) == [ev["id"]]
    assert firebase_pull.site_by(conn)[ev["id"]] == WHO[1]


@pytest.mark.parametrize("quote, note", [
    ("No response from agent", None),
    ("no response from agent.", None),
    (site_close.FORWARDED, "Mail forwarded to Iris"),
    (None, None),
])
def test_the_note_is_empty_or_says_the_mail_was_forwarded(world, quote, note):
    conn, repo, close = world
    case, sid, action = close(quote=quote)
    suggestions.accept(db.Repository(conn), sid, today="2026-11-11")
    ev = closing(conn, case)
    assert ev["note"] == note and ev["source"] == "web"
    assert "Rena Vosk" not in (ev["note"] or "")


def test_an_edited_close_is_still_the_sites(world):
    conn, repo, close = world
    case, sid, action = close(quote="They chose a cheaper airline; our rate was 2.40/kg.")
    row = conn.execute("SELECT * FROM suggestion WHERE id = ?", (sid,)).fetchone()
    suggestions.decide(db.Repository(conn), row, {"action": "lost", "at": "2026-11-10", "note": "lost on price",
                                                   "reason": "Price too high"}, "2026-11-11")
    ev = closing(conn, case)
    assert (ev["source"], ev["note"], ev["reason"]) == ("web", "lost on price", "Price too high")
    assert event_ids(conn, action) == [ev["id"]]


def test_a_scan_suggestion_stays_mail(world):
    conn, repo, close = world
    case, sid, action = close()
    repo.update("suggestion", sid, {"source": "scan", "note": "seen in a mail"})
    repo.commit()
    suggestions.accept(db.Repository(conn), sid, today="2026-11-11")
    assert closing(conn, case)["source"] == "mail" and event_ids(conn, action) == []


def test_repair_once_makes_an_old_accept_the_sites(world):
    conn, repo, close = world
    case, sid, action = close(quote="No response from agent")
    # what Accept wrote before FX3: source mail, "<sender> <quote>", no event_ids
    old = db.Repository(conn, source="ui")
    old.set_status(case, "Lost", "2026-11-11", note="Rena Vosk No response from agent",
                   closed_reason="No response from agent", source="mail")
    ev = closing(conn, case)
    old.update("suggestion", sid, {"state": "accepted", "decided_as": "lost", "event_ref": "status_event:{}".format(ev["id"])})
    # one she edited by hand keeps its note
    case2, sid2, action2 = close(quote="Their own forwarder took it")
    old.set_status(case2, "Lost", "2026-11-11", note="checked with Rena by phone",
                   closed_reason="No response from agent", source="mail")
    ev2 = closing(conn, case2)
    old.update("suggestion", sid2, {"state": "accepted", "decided_as": "lost", "event_ref": "status_event:{}".format(ev2["id"])})
    old.commit()
    last = conn.execute("SELECT MAX(id) FROM change_log").fetchone()[0]

    lines = site_close.repair_once(conn)
    assert lines == ["Team site closes on the Timeline repaired: OPP-9951 Lost (status_event {}), "
                     "OPP-9952 Lost (status_event {}).".format(ev["id"], ev2["id"])]
    after, after2 = closing(conn, case), closing(conn, case2)
    assert (after["source"], after["note"]) == ("web", None)
    assert (after2["source"], after2["note"]) == ("web", "checked with Rena by phone")
    assert event_ids(conn, action) == [ev["id"]] and event_ids(conn, action2) == [ev2["id"]]
    logged = conn.execute("SELECT DISTINCT action_id, source FROM change_log WHERE id > ?", (last,)).fetchall()
    assert len(logged) == 1 and logged[0]["source"] == "migration"
    assert site_close.repair_once(conn) == []
