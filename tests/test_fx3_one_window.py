"""Batch FX3 (2026-11-11): one OppList at a time, the team site's daily
write guard, a lighter Who asked? box, the LATAM label on Enquiries and
shipments switched off.

Every company, person, place, number and date here is invented; cases from
OPP-9901, mail on .test domains.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import socket
from pathlib import Path

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import __main__ as cli
from opplist import db, firebase_sync as fs, paths, scheduled, shipments_switch, suggestions, tracking, web
from opplist.web import create_app
from test_firebase_sync import FakeFirestore

TODAY = "2026-11-11"


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "fx3.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "owners", ["Ana", "Ben"])
    agent = add_party(repo, "Halvery Freight", country="Chile")
    repo.commit()
    n = [9901]

    def case(status="Quoted", received="2026-11-02", **extra):
        extra.setdefault("scope", "In")
        got = add_enquiry(repo, "OPP-{}".format(n[0]), agent, status=status, received=received,
                          service_mode="Air", origin_country="USA", dest_country="Chile",
                          source_subject="AIR RATE // OPP-{}".format(n[0]), **extra)
        n[0] += 1
        repo.commit()
        return got
    yield conn, repo, case, path
    conn.close()


def client(path):
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client()


def body(c, url):
    return c.get(url).data.decode("utf-8")


# --------------------------------------------------- 1. one OppList at a time --
def test_serve_stops_when_something_answers_on_its_port_and_touches_nothing(world, tmp_path, capsys):
    conn, repo, case, path = world
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    db.set_setting(conn, "port", str(port))
    conn.commit()
    conn.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    restore = tmp_path / "restore"
    try:
        got = cli.main(["serve", "--db", str(path), "--restore-dir", str(restore)])
    finally:
        listener.close()
    assert got == cli.EXIT_ALREADY_RUNNING == 3
    out = capsys.readouterr().out
    assert "OppList is already running on http://127.0.0.1:{} - close that window first, or use it.".format(port) \
        in out
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before          # not opened, not upgraded
    assert not restore.exists()                                              # no restore point
    conn = db.open_db(path)                                                  # (the fixture closes it)


def test_a_free_port_answers_nothing_and_opplist_cmd_pauses_on_exit_3():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert not cli.answers_on(port, timeout=0.3)
    launcher = paths.PROJECT_DIR / "OppList.cmd"
    if not launcher.exists():
        # this edition ships run_demo.cmd and run_demo.sh, not the launcher of
        # the PC the app lives on; where the launcher is, it is checked
        return
    cmd = launcher.read_text(encoding="ascii")
    block = cmd[cmd.index('if "%ERRORLEVEL%"=="3" ('):cmd.index("if errorlevel 1 (")]
    assert "pause" in block and "exit /b 3" in block and "pip install" not in block


# ------------------------------------------------------- 2. the write guard --
def sync(path, fake, today=TODAY, **kw):
    return fs.sync(path, client=fake, state_file=fs.state_path(), today=today,
                   reports_dir=path.parent / "reports", record=path.parent / "exports.json", **kw)


def test_a_push_past_the_cap_is_not_sent_and_the_banner_says_so(world, monkeypatch):
    conn, repo, case, path = world
    # FX4: the Settings page counts the day's writes by web._today; the sync wrote them
    # under TODAY, so the page is given the same day (it failed from 12 Nov on)
    monkeypatch.setattr(web, "_today", lambda: TODAY)
    for _ in range(3):
        case()
    db.set_setting(conn, fs.CAP_SETTING, "12")
    conn.commit()
    fake = FakeFirestore()
    first = sync(path, fake)
    assert first.ok and not first.paused
    used = fs.writes_on(fs.load_state(), TODAY)
    assert used == first.written and used > 0
    # another push that changes every case would take the day past 12
    for _ in range(4):
        case()
    sent = len(fake.commits)
    second = sync(path, fake)
    assert second.paused and not second.ok and len(fake.commits) == sent
    state = fs.load_state()
    assert state["paused"]["day"] == TODAY and fs.writes_on(state, TODAY) == used
    line = fs.PAUSED_LINE.format(n=used, cap=12)
    assert line.startswith("Team site paused: {} writes today (cap 12). Look for a second OppList window".format(used))
    assert fs.guard_line(today=TODAY) == line
    assert scheduled.banner(now=dt.datetime(2026, 11, 11, 10, 0))["text"] == line
    page = body(client(path), "/settings")
    assert 'name="firebase_daily_cap" value="12"' in page and "{} written today".format(used) in page


def test_the_next_day_goes_again(world):
    conn, repo, case, path = world
    case()
    state = {"project": "demo-opplist", "writes": {"2026-11-10": 5000},
             "paused": {"day": "2026-11-10", "count": 5000, "cap": 5000}}
    fs.save_state(state)
    result = sync(path, FakeFirestore(), today="2026-11-11")
    assert result.ok and not result.paused
    state = fs.load_state()
    assert "paused" not in state and fs.writes_on(state, "2026-11-11") == result.written
    assert fs.guard_line(today="2026-11-11") == ""


def test_a_429_from_firebase_gives_the_quota_line(world):
    conn, repo, case, path = world
    case()

    class Quota(FakeFirestore):
        def commit(self, writes):
            raise fs.FirebaseError("Firebase said 429: Quota exceeded.")

    result = sync(path, Quota())
    assert not result.ok and "429" in result.reason
    assert fs.guard_line(today=TODAY) == fs.QUOTA_LINE
    assert fs.QUOTA_LINE == ("Team site: Firebase's free writes for today are used up; it takes updates again "
                             "after midnight Pacific (2 AM Chicago). Nothing is lost.")
    assert fs.QUOTA_LINE.replace("'", "&#39;") in body(client(path), "/tracking")


def test_the_cap_saves_from_settings_and_a_bad_one_is_refused(world):
    conn, repo, case, path = world
    c = client(path)
    c.post("/settings", data={"firebase_daily_cap": "6,500"})
    conn.rollback()
    assert fs.daily_cap(conn) == 6500
    c.post("/settings", data={"firebase_daily_cap": "lots"})
    conn.rollback()
    assert fs.daily_cap(conn) == 6500


@pytest.fixture
def scheduled_run(monkeypatch):
    monkeypatch.setattr(cli, "HANDOVER_SKIP", None)
    monkeypatch.setattr(scheduled, "guard", lambda *a, **k: None)
    monkeypatch.setattr(scheduled, "offsite_copy", lambda *a, **k: None)


def test_the_0815_push_is_left_to_the_app_when_it_pushed_in_the_last_ten_minutes(world, scheduled_run, monkeypatch,
                                                                                  capsys):
    conn, repo, case, path = world
    monkeypatch.setattr(fs, "configured", lambda conn: (True, ""))
    monkeypatch.setattr(fs, "service_account", lambda conn: "")
    monkeypatch.setattr(fs, "sync", lambda *a, **k: pytest.fail("pushed"))
    recent = (dt.datetime.now() - dt.timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
    fs.save_state({"last_app_push": recent})
    assert cli.main(["firebase-sync", "--scheduled", "--db", str(path)]) == 0
    entry = scheduled.load()["runs"]["firebase-sync"]
    assert entry["state"] == "done" and entry["reason"].startswith("the app pushed at {}".format(recent[11:16]))
    assert "push left to it" in capsys.readouterr().out
    assert fs.app_pushed_within(now=dt.datetime.now() + dt.timedelta(minutes=20)) == ""


# ------------------------------------------------ 4. the Who asked? box --
def test_the_who_asked_box_is_light_and_one_line_per_candidate(world):
    conn, repo, case, path = world
    app = create_app(path)
    who = [{"kind": "mail", "party_id": 7, "party_key": "Halvery Freight", "address": "",
            "evidence": "2026-11-09, desk@halvery.test: RE: AIR RATE", "mails": 3},
           {"kind": "mail", "party_id": None, "party_key": "", "address": "ops@brindlecargo.test",
            "evidence": "2026-11-08, ops@brindlecargo.test: FW: AIR RATE (forwarded from a.ng@brindlecargo.test)",
            "mails": 1, "suggested_key": "Brindle Cargo", "suggested_country": "Peru"}]
    with app.test_request_context("/"):
        box = app.jinja_env.get_template("_who_asked.html").render(who=who, who_case=5, who_back="/", t={})
    assert 'title="2026-11-09, desk@halvery.test: RE: AIR RATE · 3 mails">Halvery Freight</b>' in box
    assert ('title="2026-11-08, ops@brindlecargo.test: FW: AIR RATE (forwarded from a.ng@brindlecargo.test)">'
            'ops@brindlecargo.test</b> <span class="m">no party</span>') in box
    assert '<span class="m">2026-11' not in box                     # no evidence line under the name
    css = (paths.PROJECT_DIR / "opplist" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    rule = css[css.index(".who-asked {"):].split("}")[0]
    assert "#f2f6fc" in rule and "border-left" not in rule
    assert "--warn-bg: #fff4e6;" in css                             # DD1: the #f78600 family


# ------------------------------------------------------ 5. the LATAM label --
def test_the_latam_label_is_filled_and_sits_next_to_the_number(world):
    conn, repo, case, path = world
    got = case()
    listed = body(client(path), "/enquiries?view=all")
    assert 'OPP-9901</a><span class="lbl fill latam-chip">LATAM</span>' in listed
    css = (paths.PROJECT_DIR / "opplist" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".c-opp_no .latam-chip { margin-left: 8px; }" in css
    assert '<span class="pill latam-chip" id="latam-chip">LATAM</span>' in body(client(path), "/enquiries/{}".format(got))


# ---------------------------------------------------- 6. shipments off --
def won_case(case, conn, repo):
    got = case("Won", received="2026-10-20")
    return got


def test_shipments_are_off_by_default_and_nothing_of_them_shows(world):
    conn, repo, case, path = world
    won = won_case(case, conn, repo)
    assert not shipments_switch.enabled(conn)
    c = client(path)
    home = body(c, "/enquiries")
    assert ">Shipments</a>" not in home
    off = c.get("/shipments")
    assert off.status_code == 404 and shipments_switch.OFF_PAGE in off.data.decode("utf-8")
    assert c.get("/shipments/1").status_code == 404
    started = c.post("/enquiries/{}/shipment/start".format(won))
    assert started.status_code == 404
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM shipment").fetchone()[0] == 0
    page = body(c, "/enquiries/{}".format(won))
    assert "Start shipment" not in page and "Open shipment" not in page
    assert "record the rate used, start the shipment" not in body(c, "/enquiries?view=closed")


def test_r10_and_shipment_suggestions_come_back_when_switched_on(world, monkeypatch):
    conn, repo, case, path = world
    won = won_case(case, conn, repo)
    from opplist import shipments
    monkeypatch.setattr(shipments, "attention", lambda conn, today: [(4, won, "OPP-9901", ["ETA gone by"], 3, "")])
    sid = repo.insert("suggestion", {"enquiry_id": won, "opp_no": "OPP-9901", "kind": "shipment_milestone",
                                     "state": "pending", "source": "mail", "proposed_status": "Departed",
                                     "proposed_date": "2026-11-05", "note": "from a mail"})
    repo.commit()
    assert tracking.rule_shipment_attention(conn, TODAY) == []
    assert sid not in [r["id"] for r in suggestions.pending(conn)]
    c = client(path)
    c.post("/settings", data={"shipments": "on"})
    conn.rollback()
    assert shipments_switch.enabled(conn)
    assert len(tracking.rule_shipment_attention(conn, TODAY)) == 1
    assert sid in [r["id"] for r in suggestions.pending(conn)]
    assert ">Shipments</a>" in body(c, "/enquiries") and c.get("/shipments").status_code == 200
    assert "Start shipment" in body(c, "/enquiries/{}".format(won))


def test_propose_shipments_says_off_and_the_morning_run_records_done(world, scheduled_run, capsys):
    conn, repo, case, path = world
    assert cli.main(["propose-shipments", "--db", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "shipments are switched off"
    assert cli.main(["propose-shipments", "--scheduled", "--db", str(path)]) == 0
    entry = scheduled.load()["runs"]["propose-shipments"]
    assert (entry["state"], entry["reason"]) == ("done", "shipments are switched off")


def test_switch_off_once_dismisses_the_pending_and_copies_the_tables(world):
    conn, repo, case, path = world
    won = won_case(case, conn, repo)
    ship = repo.insert("shipment", {"enquiry_id": won, "mode": "air", "title": "Miami - Santiago"})
    repo.insert("shipment_event", {"shipment_id": ship, "kind": "milestone", "milestone": "Booked", "title": "Booked",
                                   "at": "2026-11-03 09:00"})
    pend = [repo.insert("suggestion", {"enquiry_id": won, "opp_no": "OPP-9901", "kind": kind, "state": "pending",
                                       "source": "mail", "proposed_status": "Arrived", "proposed_date": "2026-11-06",
                                       "note": "seen in a mail" if kind != "shipment_note" else None})
            for kind in ("shipment_milestone", "shipment_issue", "shipment_note")]
    kept = repo.insert("suggestion", {"enquiry_id": won, "opp_no": "OPP-9901", "kind": "shipment_milestone",
                                      "state": "accepted", "source": "mail", "proposed_status": "Booked",
                                      "proposed_date": "2026-11-03"})
    status_one = repo.insert("suggestion", {"enquiry_id": won, "opp_no": "OPP-9901", "kind": "status",
                                            "state": "pending", "source": "scan", "proposed_status": "Lost",
                                            "proposed_date": "2026-11-06"})
    repo.commit()
    lines = shipments_switch.switch_off_once(conn)
    assert lines[-1] == "Shipments switched off: 3 pending shipment suggestions dismissed (OPP-9901, OPP-9901, OPP-9901)."
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM suggestion")}
    assert [rows[i]["state"] for i in pend] == ["dismissed"] * 3
    assert rows[pend[0]]["note"] == "seen in a mail · shipments switched off on 11 Nov"
    assert rows[pend[2]]["note"] == "shipments switched off on 11 Nov"
    assert rows[kept]["state"] == "accepted" and rows[status_one]["state"] == "pending"
    logged = conn.execute("SELECT DISTINCT action_id, source FROM change_log WHERE table_name = 'suggestion' "
                          "AND field = 'state' AND new_value = 'dismissed'").fetchall()
    assert len(logged) == 1 and logged[0]["source"] == "migration"
    folder = Path(paths.DATA_DIR) / "shipments-20261111"
    with open(folder / "shipment_event.csv", encoding="utf-8-sig", newline="") as fh:
        got = list(csv.DictReader(fh))
    assert [(r["shipment_id"], r["title"]) for r in got] == [(str(ship), "Booked")]
    assert (folder / "shipment.csv").exists()
    assert conn.execute("SELECT COUNT(*) FROM shipment").fetchone()[0] == 1          # nothing deleted
    assert shipments_switch.switch_off_once(conn) == []


# ------------- shipments switched on: the scan and the close (replace two older tests) --
@pytest.fixture
def ship_world(world, tmp_path, monkeypatch):
    from opplist import mail_archive, shipments
    from opplist.outlook_bridge import MailInfo
    conn, repo, case, path = world
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    mail = MailInfo(subject="RE: FCL booking // Port Elmar - Valdora", received_on="2026-11-04",
                    received_at="2026-11-04 16:20", sent_at="2026-11-04 16:20", entry_id="EID-9901",
                    conversation_id="CONV-9901", conversation_topic="FCL booking // Port Elmar - Valdora",
                    sender_name="Tamsk Lines Desk", sender_address="desk@tamsklines.test", sender_internal=False,
                    to_addresses=["ops@quillmark.test"], body_excerpt="Dear team,\nThe container sailed on Nov 4.\n",
                    folder_path="\\Mailbox\\Inbox", display_to="Ops", message_id="<sail-9901@tamsklines.test>")
    aconn = mail_archive.open_archive(archive)
    mail_archive.store(aconn, [mail_archive.record_from_info(mail)])
    mail_archive.set_meta(aconn, "last_sync_at", "2026-11-05 08:15:00")
    aconn.commit()
    aconn.close()
    won = case("Quoted", received="2026-10-20", conversation_id="CONV-9901")
    repo.update("enquiry", won, {"service_mode": "FCL"})
    repo.set_status(won, "Won", "2026-10-28")
    db.set_setting(conn, shipments_switch.SETTING, "1")
    sid = shipments.start(repo, won, "Port Elmar to Valdora")
    repo.commit()
    return conn, repo, path, won, sid


def test_with_shipments_on_the_scan_proposes_and_accept_writes_the_event(ship_world):
    from opplist import mail_archive, shipments
    conn, repo, path, won, sid = ship_world
    with mail_archive.ArchiveSearch() as search:
        run = shipments.propose(conn, search, dry_run=False, today="2026-11-05")
    rows = suggestions.pending(conn, shipment_id=sid)
    assert run["written"] == len(rows) == 1, run
    assert (rows[0]["kind"], rows[0]["proposed_status"], rows[0]["message_id"]) == \
        ("shipment_milestone", "Departed", "<sail-9901@tamsklines.test>")
    assert suggestions.accept(repo, rows[0]["id"]).startswith("shipment: Departed logged on 2026-11-04")
    event = conn.execute("SELECT kind, milestone, source, sender FROM shipment_event WHERE shipment_id = ? "
                         "AND milestone = 'Departed'", (sid,)).fetchone()
    assert tuple(event) == ("milestone", "Departed", "mail", "Tamsk Lines Desk")
    # switched off, the same suggestion kinds leave the lists
    db.set_setting(conn, shipments_switch.SETTING, "0")
    assert suggestions.pending(conn, shipment_id=sid) == []


def test_with_shipments_on_a_shipment_closes_with_a_reason_on_its_page(ship_world):
    from opplist import shipments
    conn, repo, path, won, sid = ship_world
    with pytest.raises(ValueError):
        shipments.close(repo, sid, reason="Delivered")                         # not delivered yet
    conn.commit()
    c = client(path)
    page = body(c, "/shipments/{}".format(sid))
    assert '<option value="Delivered" disabled>' in page and '<option value="Registered in error" >' in page
    done = c.post("/shipments/{}/close".format(sid), data={"action": "close", "reason": "Not shipped"},
                  follow_redirects=True).data.decode("utf-8")
    assert "Shipment closed" in done and "closed: Not shipped" in done
    conn.rollback()
    assert shipments.list_rows(conn, "active", "2026-11-05") == []
    assert tracking.rule_shipment_attention(conn, "2026-11-30") == []


# ---- the Won picker with its per-route Start shipment (replaces a test in test_routes.py) --
@pytest.mark.parametrize("on", [True, False])
def test_the_won_picker_and_the_route_shipment_buttons(world, monkeypatch, on):
    conn, repo, case, path = world
    repo.insert("country", {"name": "Canada", "region": "North America"})
    for key, country in (("PORTHAVEN", "Brazil"), ("ELMSTEAD", "Canada"), ("WILLOWMERE", "Canada"),
                         ("DUNHOLT", "USA")):
        repo.insert("location", {"key": key, "country": country, "type": "City"})
    agent = conn.execute("SELECT id FROM party WHERE key = 'Halvery Freight'").fetchone()[0]
    got = add_enquiry(repo, "OPP-9940", agent, received="2026-11-02", scope="In", service_mode="FCL",
                      origin_loc="PORTHAVEN", origin_country="Brazil", dest_loc="ELMSTEAD", dest_country="Canada",
                      incoterm="DAP", source_subject="FCL RATE // OPP-9940")
    repo.set_status(got, "Quoted", "2026-11-03")
    b = repo.add_route(got, {"origin_loc": "PORTHAVEN", "origin_country": "Brazil", "dest_loc": "WILLOWMERE",
                             "dest_country": "Canada", "service_mode": "FCL", "incoterm": "DAP"})
    c_ = repo.add_route(got, {"origin_loc": "PORTHAVEN", "origin_country": "Brazil", "dest_loc": "DUNHOLT",
                              "dest_country": "USA", "service_mode": "FCL", "incoterm": "DDP"})
    a = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND seq = 1", (got,)).fetchone()[0]
    db.set_setting(conn, shipments_switch.SETTING, "1" if on else "0")
    repo.commit()
    import opplist.web as web_module
    monkeypatch.setattr(web_module, "_today", lambda: "2026-11-11")
    c = client(path)
    page = body(c, "/enquiries/{}".format(got))
    assert "Which routes did we win?" in page and 'name="won_route" value="{}"'.format(b) in page
    c.post("/enquiries/{}/status".format(got), data={
        "status": "Won", "at": "2026-11-10", "routes_present": "1", "won_route": [str(a), str(c_)],
        "lost_reason_{}".format(b): "Price too high", "rate_used": "own"}, follow_redirects=True)
    conn.rollback()
    assert conn.execute("SELECT status FROM enquiry WHERE id = ?", (got,)).fetchone()[0] == "Won"
    assert [tuple(r) for r in conn.execute("SELECT seq, result, lost_reason FROM enquiry_route WHERE enquiry_id = ? "
                                           "ORDER BY seq", (got,))] == \
        [(1, "Won", None), (2, "Lost", "Price too high"), (3, "Won", None)]
    assert [tuple(r) for r in conn.execute(
        "SELECT status, note, reason FROM status_event WHERE enquiry_id = ? AND at = '2026-11-10' AND kind = 'status' "
        "ORDER BY id", (got,))] == [("Lost", "B WILLOWMERE · Price too high", "Price too high"),
                                    ("Won", "A ELMSTEAD, C DUNHOLT", None)]
    page = body(c, "/enquiries/{}".format(got))
    assert "won A, C · lost B" in page
    # a won route offers Start shipment while shipments are on, a lost one never
    assert ('form="route-ship-{}"'.format(a) in page) is on and 'form="route-ship-{}"'.format(b) not in page
