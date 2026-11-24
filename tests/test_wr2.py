"""Batch WR2 (2026-11-17, no schema change): Waiting for requester from the
team site - the site's waitreq and inforec actions taken in as her own
Waiting for requester and Info received buttons (WR1; INTERFACE.md
section 2).

A fake Firestore client; no network. Every company, person, place and date
here is invented: cases OPP-9101 upward, domains .test.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, firebase_pull, firebase_sync as fs, site_close, tracking, wait
from test_rq2 import SiteFake

RECEIVED = "2026-11-09"            # a Monday
WAITED = "2026-11-10"              # the wait, a Tuesday
TODAY = "2026-11-12"
NEED = "the dimensions of each piece; the pickup address"
AT = dt.datetime(2026, 11, 10, 15, 0, tzinfo=dt.timezone.utc)


class Site(SiteFake):
    """SiteFake whose readTime is the newest action's at, so an action put
    in after a pull is found by the next one."""

    def run_query(self, query):
        out = super().run_query(query)
        ats = [firebase_pull.iso(d["at"]) for p, d in self.docs.items()
               if p.startswith("actions/") and isinstance(d.get("at"), dt.datetime)]
        out["readTime"] = max(ats) if ats else ""
        return out


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "wr2.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    agent = add_party(repo, "Pellwick Forwarding", country="Chile")
    repo.insert("party_domain", {"domain": "pellwick.test", "party_id": agent})
    ids = {"path": path}
    for opp, extra in (("OPP-9101", {}), ("OPP-9102", {}), ("OPP-9103", {"status": "Quoted"}),
                       ("OPP-9104", {"status": "Lost"}), ("OPP-9105", {"handled_by": "JJC-NORTH DESK"}),
                       ("OPP-9106", {})):
        ids[opp] = add_enquiry(repo, opp, agent, received=RECEIVED, scope="In", service_mode="Air",
                               origin_country="USA", dest_country="Chile", **extra)
    repo.commit()
    yield {"conn": conn, "repo": repo, "ids": ids, "state": {}, "path": path}
    conn.close()


def put(client, action_id, opp, kind, minutes=0, name="Marta", **fields):
    client.docs["actions/" + action_id] = dict({
        "type": kind, "opp": opp, "by": "{}@site.test".format(name.lower()), "name": name, "owner": name,
        "app": "1117e", "at": AT + dt.timedelta(minutes=minutes)}, **fields)


def pull(w, client, today=TODAY):
    return firebase_pull.pull(w["conn"], client, w["state"], today)


def sync(w, client, today=TODAY):
    return fs.sync_once(w["conn"], client, w["state"], today=today, reports_dir=w["path"].parent / "none")


def result(client, action_id):
    doc = client.docs["actionResults/" + action_id]
    return doc["ok"], doc["msg"]


def wait_notes(conn, case):
    return [tuple(r) for r in conn.execute(
        "SELECT note, voided, source, status, at FROM status_event WHERE enquiry_id = ? AND kind = 'note' "
        "AND (note LIKE ? OR note = ?) ORDER BY id", (case, wait.PREFIX + "%", wait.INFO_RECEIVED))]


def rule_rows(conn, rule, today=TODAY):
    return {item.enquiry_id: item for s in tracking.run(conn, today) if s.rule == rule for item in s.items}


# ------------------------------------------------------------ waitreq --
def test_a_waitreq_on_a_new_case_writes_her_note_moves_it_to_r12_and_pushes_the_wait(world):
    w, client = world, Site()
    case = w["ids"]["OPP-9101"]
    assert case in rule_rows(w["conn"], "R1")
    put(client, "wReqOne0000000000001", "OPP-9101", "waitreq", date=WAITED, what="  " + NEED + " ")
    sync(w, client)
    assert result(client, "wReqOne0000000000001") == (True, "Waiting for the requester since Nov 10.")
    assert wait_notes(w["conn"], case) == [("Waiting for requester: " + NEED, 0, "web", "New", WAITED)]
    assert w["conn"].execute("SELECT status FROM enquiry WHERE id = ?", (case,)).fetchone()[0] == "New"
    # the person is kept with the action, as for the site's other actions
    kept = w["conn"].execute("SELECT by_name, by_email, result FROM site_action WHERE id = ?",
                             ("wReqOne0000000000001",)).fetchone()
    assert tuple(kept) == ("Marta", "marta@site.test", "applied")
    # out of R1, into R12 with FU1 due a working day after the wait (WR1's ladder)
    assert case not in rule_rows(w["conn"], "R1")
    item = rule_rows(w["conn"], "R12")[case]
    assert (item.since, item.extra["what"]) == (WAITED, NEED)
    rung = wait.ladder(w["conn"], wait.live(w["conn"], case), TODAY)
    assert (rung["next"], rung["next_due"]) == ("FU1", "2026-11-11")
    # wait and next pushed
    assert client.docs["cases/OPP-9101"]["wait"] == {"since": WAITED, "what": NEED, "next": "FU1 due"}
    row = next(r for r in client.docs["lists/inquiries"]["rows"] if r["opp"] == "OPP-9101")
    assert row["wait"] == WAITED
    # meta/sync tells the site it may offer the buttons
    assert client.docs["meta/sync"]["waitReq"] is True


def test_a_second_waitreq_replaces_the_first(world):
    w, client = world, Site()
    case = w["ids"]["OPP-9101"]
    put(client, "wReqOne0000000000001", "OPP-9101", "waitreq", date=WAITED, what=NEED)
    pull(w, client)
    put(client, "wReqTwo0000000000002", "OPP-9101", "waitreq", minutes=60, date="2026-11-11",
        what="the commodity", name="Tobias")
    pull(w, client)
    assert result(client, "wReqTwo0000000000002") == (True, "Waiting for the requester since Nov 11.")
    assert wait_notes(w["conn"], case) == [
        ("Waiting for requester: " + NEED, 1, "web", "New", WAITED),
        ("Waiting for requester: the commodity", 0, "web", "New", "2026-11-11")]
    assert wait.live(w["conn"], case).what == "the commodity"


def test_waitreq_is_refused_on_quoted_closed_another_offices_and_a_pending_close(world):
    w, client = world, Site()
    ids = w["ids"]
    # a Won waiting for her on OPP-9106 (the site's close, not confirmed yet)
    w["repo"].insert("suggestion", {"enquiry_id": ids["OPP-9106"], "opp_no": "OPP-9106", "kind": "status",
                                    "proposed_status": "Won", "proposed_date": WAITED, "source": "web",
                                    "note": "Won from the site " + site_close.mark("wClose00000000000009")})
    w["repo"].commit()
    assert site_close.waiting(w["conn"], ids["OPP-9106"]) is not None
    for n, opp in enumerate(("OPP-9103", "OPP-9104", "OPP-9105", "OPP-9106")):
        put(client, "wRefused00000000000{}".format(n), opp, "waitreq", minutes=n, date=WAITED, what=NEED)
    pull(w, client)
    said = {opp: result(client, "wRefused00000000000{}".format(n))
            for n, opp in enumerate(("OPP-9103", "OPP-9104", "OPP-9105", "OPP-9106"))}
    assert said == {
        "OPP-9103": (False, "OPP-9103 is Quoted now, so it was not set to wait for the requester."),
        "OPP-9104": (False, "OPP-9104 is closed (Lost), so it was not set to wait for the requester."),
        "OPP-9105": (False, "OPP-9105 is handled by JJC-NORTH DESK, so it was not set to wait for the requester."),
        "OPP-9106": (False, "OPP-9106 has a Won waiting to be confirmed, so it was not set to wait for the requester."),
    }
    for opp in said:
        assert wait_notes(w["conn"], ids[opp]) == []
    # the site's words never name her or OppList
    for ok, msg in said.values():
        assert "OppList" not in msg and "Iris" not in msg


# ------------------------------------------------------------ inforec --
def test_an_inforec_ends_the_wait_and_the_case_is_back_in_r1(world):
    w, client = world, Site()
    case = w["ids"]["OPP-9101"]
    put(client, "wReqOne0000000000001", "OPP-9101", "waitreq", date=WAITED, what=NEED)
    sync(w, client)
    assert "wait" in client.docs["cases/OPP-9101"]
    put(client, "wInfo00000000000001", "OPP-9101", "inforec", minutes=90, date="2026-11-12")
    sync(w, client)
    ok, msg = result(client, "wInfo00000000000001")
    assert ok is True and msg == "Info received on Nov 12; the case waits for our quote again."
    assert wait_notes(w["conn"], case)[-1] == ("Info received", 0, "web", "New", "2026-11-12")
    assert wait.live(w["conn"], case) is None
    assert case in rule_rows(w["conn"], "R1") and case not in rule_rows(w["conn"], "R12")
    assert "wait" not in client.docs["cases/OPP-9101"]
    row = next(r for r in client.docs["lists/inquiries"]["rows"] if r["opp"] == "OPP-9101")
    assert row["wait"] == ""


def test_an_inforec_with_no_live_wait_writes_nothing_and_answers_ok(world):
    w, client = world, Site()
    case = w["ids"]["OPP-9102"]
    before = w["conn"].execute("SELECT COUNT(*) FROM status_event").fetchone()[0]
    put(client, "wInfo00000000000002", "OPP-9102", "inforec", date=TODAY)
    pull(w, client)
    assert result(client, "wInfo00000000000002") == (True, "It was not waiting for the requester any more.")
    assert w["conn"].execute("SELECT COUNT(*) FROM status_event").fetchone()[0] == before
    assert wait_notes(w["conn"], case) == []


def test_a_waitreq_and_an_inforec_in_one_pull_both_apply_in_order(world):
    w, client = world, Site()
    case = w["ids"]["OPP-9101"]
    # written out of order; the pull takes them by at
    put(client, "wInfo00000000000003", "OPP-9101", "inforec", minutes=5, date=WAITED)
    put(client, "wReqOne0000000000001", "OPP-9101", "waitreq", date=WAITED, what=NEED)
    pull(w, client)
    assert result(client, "wReqOne0000000000001")[0] is True
    assert result(client, "wInfo00000000000003")[1].startswith("Info received on Nov 10")
    assert [n[0] for n in wait_notes(w["conn"], case)] == ["Waiting for requester: " + NEED, "Info received"]
    assert wait.live(w["conn"], case) is None
