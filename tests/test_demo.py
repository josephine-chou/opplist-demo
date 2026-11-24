"""python -m opplist demo: the synthetic data set the portfolio copy runs on.

Generation into a temporary folder, the counts it promises, byte-identical
output for the same seed and date, the guard against overwriting a real
database, and the main pages rendering on it (the mail archive pointed at
the demo's archive the way conftest points it at an empty one).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
import sys

import pytest

from opplist import __main__ as cli, db, demo, mail_archive, shipments, tracking
from opplist.web import create_app

TODAY = dt.date.today()


def sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("demo")
    result = demo.generate(out, seed=42, today=TODAY)
    return out, result


@pytest.fixture
def client(built, monkeypatch):
    out, _ = built
    archive = out / "mail-archive" / "mail.db"
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    monkeypatch.setattr(mail_archive, "SYNC_LOG", out / "mail-archive" / "sync-log.txt")
    app = create_app(db_path=out / "opplist.db")
    app.config["TESTING"] = True
    return app.test_client()


def ro(path):
    conn = sqlite3.connect("file:{}?mode=ro".format(path.as_posix()), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------ generation --
def test_the_files_are_there_at_the_current_schema(built):
    out, _ = built
    assert (out / "opplist.db").exists() and (out / "mail-archive" / "mail.db").exists()
    assert not list(out.rglob("*-wal")) and not list(out.rglob("*-shm"))
    conn = ro(out / "opplist.db")
    from opplist import schema_migrations
    assert int(db.get_setting(conn, "schema_version")) == schema_migrations.CURRENT_VERSION
    assert db.get_setting(conn, demo.SEED_SETTING) == "42"
    assert conn.execute("SELECT type FROM sqlite_master WHERE name = 'location'").fetchone()[0] == "view"
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()


def test_counts_are_in_range(built):
    _, r = built
    assert 30 <= r["parties"] <= 40 and r["contacts"] >= r["parties"] - 5
    assert r["party_domains"] >= 25
    assert 85 <= r["locations"] <= 100 and r["located"] == r["locations"]
    assert 130 <= r["enquiries"] <= 150
    status = r["by_status"]
    assert set(status) == {"New", "Quoted", "Won", "Lost", "Dropped"}
    assert status["New"] >= 8 and status["Quoted"] >= 12 and status["Won"] >= 20 and status["Lost"] >= 40
    assert 2 <= status["Dropped"] <= 15
    assert r["vendor_requests"] >= 150 and r["followups"] >= 40 and r["requotes"] >= 2
    assert r["suggestions_accepted"] >= 12 and r["suggestions_pending"] == 5
    assert r["shipments"] == 3 and 30 <= r["shipment_events"] <= 48
    assert r["archive_messages"] >= 500


def test_the_data_carries_what_the_features_read(built):
    out, _ = built
    conn = ro(out / "opplist.db")
    one = lambda sql: conn.execute(sql).fetchone()[0]
    # offices, keyed through branding
    offices = {r[0] for r in conn.execute("SELECT key FROM party WHERE type = 'Office'")}
    assert offices == {"JoJo JJC", "JoJo JQM", "JoJo JJH", "JoJo JJS", "JoJo JJB"}
    assert one("SELECT COUNT(*) FROM party WHERE type = 'Carrier'") == 3
    assert one("SELECT COUNT(*) FROM party WHERE type = 'Direct Customer'") == 2
    # lost cases with what we heard, won cases with a rate, re-quote reasons
    assert one("SELECT COUNT(*) FROM enquiry WHERE lost_their_price IS NOT NULL AND lost_our_price IS NOT NULL") >= 5
    assert one("SELECT COUNT(*) FROM enquiry WHERE status = 'Lost' AND closed_reason IS NULL") == 0
    assert one("SELECT COUNT(*) FROM enquiry WHERE status = 'Won' AND rate_used IS NOT NULL") >= 15
    assert one("SELECT COUNT(*) FROM status_event WHERE kind = 'requote' AND reason IS NOT NULL") >= 2
    assert one("SELECT COUNT(*) FROM vendor_request WHERE their_ref IS NOT NULL") >= 50
    assert one("SELECT COUNT(*) FROM enquiry WHERE handled_by = 'JQM'") == 3
    assert one("SELECT COUNT(*) FROM enquiry WHERE scope = 'Out'") >= 3
    # every event of a shipment points at an archived mail
    archive = ro(out / "mail-archive" / "mail.db")
    mids = {r[0] for r in archive.execute("SELECT message_id FROM message")}
    events = conn.execute("SELECT message_id FROM shipment_event").fetchall()
    assert events and all(e[0] in mids for e in events)
    assert one("SELECT COUNT(*) FROM shipment_event WHERE kind = 'issue'") >= 5
    # the case's own mail is in the archive
    for row in conn.execute("SELECT entry_id, conversation_id FROM enquiry WHERE entry_id IS NOT NULL LIMIT 20"):
        assert archive.execute("SELECT 1 FROM message WHERE entry_id = ? AND conversation_id = ?",
                               (row[0], row[1])).fetchone()
    # nothing timestamped by the wall clock: the change log stays before tomorrow
    tomorrow = (TODAY + dt.timedelta(days=1)).isoformat()
    assert one("SELECT MAX(at) FROM change_log") < tomorrow
    archive.close()
    conn.close()


def test_tracking_has_something_in_every_chasing_rule(built):
    out, _ = built
    conn = ro(out / "opplist.db")
    sections = {s.rule: s for s in tracking.run(conn, today=TODAY.isoformat())}
    for rule in ("R1", "R2", "R3", "R9", "R10", "R5", "R6", "R8"):
        assert sections[rule].count >= 1, rule
    steps = {item.extra.get("step") for item in sections["R3"].items}
    assert {"FU1", "FU2", "Decide", "Replied"} <= steps
    conn.close()


def test_the_maps_draw_for_all_three_shipments(built):
    out, _ = built
    conn = ro(out / "opplist.db")
    kinds = {}
    for ship in conn.execute("SELECT * FROM shipment ORDER BY id").fetchall():
        case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (ship["enquiry_id"],)).fetchone()
        state = shipments.summary(conn, ship["id"], TODAY.isoformat())
        route = shipments.route_map(conn, ship, case, state)
        assert route["missing"] == [] and route["marker"] is not None, ship["title"]
        assert 'class="wmap"' in route["svg"] and "#e8456c" in route["svg"]
        assert 10 <= len(state["events"]) <= 16
        kinds[ship["mode"]] = route["kind"]
    assert kinds == {"ocean": "world", "air": "world", "truck": "us"}
    conn.close()


def test_the_features_that_came_later_are_on_show(built):
    """demo_showcase and the two later modes: every one of them is in the
    database where the pages read it."""
    from opplist import wait
    out, _ = built
    conn = ro(out / "opplist.db")
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    modes = {r[0]: r[1] for r in conn.execute("SELECT service_mode, COUNT(*) FROM enquiry WHERE status = 'New' "
                                                "GROUP BY service_mode")}
    assert modes.get("Sea-Air") == 1 and modes.get("Cross-border Truck") == 1
    # one New case with two vendors' rates (one of them with two options) and two quotes of the card
    [(case_id, n_rates)] = conn.execute("SELECT enquiry_id, COUNT(*) FROM vendor_rate GROUP BY enquiry_id").fetchall()
    assert n_rates == 3 and one("SELECT COUNT(DISTINCT vendor_party_id) FROM vendor_rate") == 2
    assert sorted(r[0] or "" for r in conn.execute("SELECT label FROM vendor_rate")) == ["", "Direct", "Via transshipment"]
    assert [tuple(r) for r in conn.execute("SELECT n, with_costs FROM quote WHERE enquiry_id = ? ORDER BY n", (case_id,))] == \
        [(1, 1), (2, 1)]
    assert one("SELECT status FROM enquiry WHERE id = ?", case_id) == "Quoted"
    assert one("SELECT COUNT(*) FROM site_action WHERE result = 'applied'") == one("SELECT COUNT(*) FROM site_action") == 6
    # a reading of Read quote waiting to be checked, from the recorded answer
    assert one("SELECT COUNT(*) FROM quote_read_draft WHERE status = 'open'") == 1
    # Waiting for requester, a case with two routes, cargo lines with an HS code and a value
    assert len(wait.live_waits(conn)) >= 1
    assert one("SELECT COUNT(*) FROM (SELECT enquiry_id FROM enquiry_route WHERE voided = 0 GROUP BY enquiry_id "
               "HAVING COUNT(*) > 1)") == 1
    assert one("SELECT COUNT(*) FROM enquiry_cargo WHERE hs_code IS NOT NULL AND cargo_value > 0") >= 3
    conn.close()


def test_same_seed_and_date_give_the_same_bytes(tmp_path):
    day = dt.date(2026, 9, 25)
    a = demo.generate(tmp_path / "a", seed=7, today=day, leak_list=None)
    b = demo.generate(tmp_path / "b", seed=7, today=day, leak_list=None)
    assert a["enquiries"] == b["enquiries"]
    for rel in ("opplist.db", "mail-archive/mail.db"):
        assert sha(tmp_path / "a" / rel) == sha(tmp_path / "b" / rel), rel
    # regenerating over a demo folder is allowed and gives the same bytes again
    demo.generate(tmp_path / "a", seed=7, today=day, leak_list=None)
    assert sha(tmp_path / "a" / "opplist.db") == sha(tmp_path / "b" / "opplist.db")
    # another seed is another world
    demo.generate(tmp_path / "c", seed=8, today=day, leak_list=None)
    assert sha(tmp_path / "c" / "opplist.db") != sha(tmp_path / "a" / "opplist.db")


def test_a_real_database_is_not_overwritten_without_force(tmp_path, capsys):
    out = tmp_path / "out"
    conn = db.open_db(out / "opplist.db")
    repo = db.Repository(conn, source="test")
    repo.insert("country", {"name": "Chile", "region": "Latin America"})
    repo.commit()
    conn.close()
    before = sha(out / "opplist.db")
    with pytest.raises(demo.DemoError):
        demo.generate(out, seed=42, leak_list=None)
    assert sha(out / "opplist.db") == before
    assert cli.main(["demo", "--out", str(out)]) == 1
    assert "refusing to overwrite" in capsys.readouterr().out
    # --force copies it aside first
    assert cli.main(["demo", "--out", str(out), "--force"]) == 0
    aside = list((out / "restore-points").glob("opplist_before_demo_*.db"))
    assert len(aside) == 1 and sha(aside[0]) == before
    assert "enquiries" in capsys.readouterr().out


# ----------------------------------------------------------------- pages --
def _ids(built):
    out, _ = built
    conn = ro(out / "opplist.db")
    ships = [r[0] for r in conn.execute("SELECT id FROM shipment ORDER BY id")]
    case = conn.execute(
        "SELECT e.id, m.message_id FROM enquiry e JOIN shipment s ON s.enquiry_id = e.id "
        "JOIN shipment_event m ON m.shipment_id = s.id ORDER BY s.id LIMIT 1").fetchone()
    quoted = conn.execute("SELECT id FROM enquiry WHERE status = 'Quoted' ORDER BY id LIMIT 1").fetchone()[0]
    location = conn.execute("SELECT id FROM master_location WHERE key = 'SAN ANTONIO'").fetchone()[0]
    party = conn.execute("SELECT id FROM party WHERE key = 'Andes Cumbre'").fetchone()[0]
    conn.close()
    return ships, case[0], quoted, location, party


def test_the_main_pages_render(built, client):
    ships, case_id, quoted, location, party = _ids(built)
    for url in ("/", "/dashboard", "/tracking", "/enquiries", "/enquiries/{}".format(quoted),
                "/enquiries/{}".format(case_id), "/shipments", "/shipments?show=all", "/locations",
                "/locations/{}".format(location), "/report", "/parties", "/parties/{}".format(party),
                "/check", "/settings"):
        response = client.get(url, follow_redirects=True)
        assert response.status_code == 200, url
    page = client.get("/tracking").get_data(as_text=True)
    assert "OPP-1" in page and "Shipment needs attention" in page


def test_a_case_page_shows_its_mail_thread(built, client):
    ships, case_id, quoted, location, party = _ids(built)
    page = client.get("/enquiries/{}".format(case_id)).get_data(as_text=True)
    assert "Camila Orrego" in page or "Tomas Vidaurre" in page          # the requester's mails
    assert "Iris Chen" in page


def test_every_shipment_page_draws_its_map(built, client):
    ships, *_ = _ids(built)
    assert len(ships) == 3
    for ship_id in ships:
        page = client.get("/shipments/{}".format(ship_id)).get_data(as_text=True)
        assert 'class="wmap"' in page and "#e8456c" in page and "no coordinates" not in page, ship_id
    listing = client.get("/shipments?show=all").get_data(as_text=True)
    for title in ("Chicago, IL to San Antonio", "Hong Kong to Chicago, IL", "Bolingbrook, IL to Dallas, TX"):
        assert title in listing


def test_update_from_outlook_without_pywin32_says_so(built, client, monkeypatch):
    ships, *_ = _ids(built)
    for name in ("win32com", "win32com.client", "pythoncom"):
        monkeypatch.setitem(sys.modules, name, None)            # import fails as if not installed
    response = client.post("/shipments/{}/update-from-outlook".format(ships[0]), follow_redirects=True)
    assert response.status_code == 200
    assert "pywin32 is not installed" in response.get_data(as_text=True)


def test_the_leak_guard_stops_on_a_real_collision(tmp_path):
    listed = tmp_path / "list.txt"
    listed.write_text("# an invented name that is also on the list\nAndes Cumbre\n", encoding="utf-8")
    with pytest.raises(demo.DemoError, match="forbidden list"):
        demo.generate(tmp_path / "out", seed=42, leak_list=listed)
    clean = tmp_path / "clean.txt"
    clean.write_text("Zyqqx Holdings\n", encoding="utf-8")
    result = demo.generate(tmp_path / "ok", seed=42, leak_list=clean)
    assert result["enquiries"] >= 130


def test_a_wording_salt_only_changes_the_sign_off():
    body = "\n".join(["Hi Camila,", "", "Please see our rates.", "", "Best regards,", "JJC-IRIS CHEN"])
    assert demo.MailBox(42).reword(body) == body
    salted = [demo.MailBox(42, salt).reword(body) for salt in range(1, 6)]
    assert any(text != body for text in salted)
    for text in salted:
        lines = text.split("\n")
        assert lines[:4] == body.split("\n")[:4] and lines[-1] == "JJC-IRIS CHEN"
        assert lines[4] in demo.SIGN_VARIANTS[0]

# ------------------------------------------------ what the live app has --
def test_quick_notes_and_the_new_pending_suggestions(built):
    out, _ = built
    conn = ro(out / "opplist.db")
    assert conn.execute("SELECT COUNT(*) FROM case_note WHERE deleted_at IS NULL").fetchone()[0] >= 3
    pending = conn.execute("SELECT * FROM suggestion WHERE state = 'pending'").fetchall()
    from opplist import detect_followups
    requoted = [s for s in pending if s["kind"] == "status" and s["proposed_status"] == "Quoted"]
    assert len(requoted) == 1 and requoted[0]["note"] == detect_followups.REQUOTED_NOTE
    starts = [s for s in pending if shipments.is_start(s)]
    assert len(starts) == 1 and starts[0]["proposed_status"] == "Picked up" + shipments.PLANNED
    case = conn.execute("SELECT status, handled_by FROM enquiry WHERE id = ?", (starts[0]["enquiry_id"],)).fetchone()
    assert tuple(case) == ("Won", None)
    assert not conn.execute("SELECT 1 FROM shipment WHERE enquiry_id = ?", (starts[0]["enquiry_id"],)).fetchone()
    conn.close()


def test_the_case_page_offers_the_start_shipment_and_the_requote(built, client):
    # suggestions wait on the case's own page (Today's work has no card for them)
    out, _ = built
    conn = ro(out / "opplist.db")
    pending = conn.execute("SELECT * FROM suggestion WHERE state = 'pending'").fetchall()
    [start] = [s for s in pending if shipments.is_start(s)]
    [requoted] = [s for s in pending if s["kind"] == "status" and s["proposed_status"] == "Quoted"]
    conn.close()
    body = client.get("/enquiries/{}".format(start["enquiry_id"])).get_data(as_text=True)
    assert '<span class="pill p-transit">Start shipment</span>' in body and "Picked up, planned" in body
    assert 'id="sg-{}"'.format(requoted["id"]) in client.get(
        "/enquiries/{}".format(requoted["enquiry_id"])).get_data(as_text=True)
    assert "Handled by JQM" in client.get("/enquiries?view=others").get_data(as_text=True) or \
        "JQM" in client.get("/enquiries?view=others").get_data(as_text=True)


def test_verify_checks_a_demo_database_against_the_demo_data(built):
    from opplist import verify
    out, _ = built
    conn = db.open_db(out / "opplist.db")
    try:
        result = verify.run(conn)
    finally:
        conn.close()
    [check] = [c for c in result.checks if c.name.startswith("2.")]
    assert check.ok and "demo data" in check.name
    assert any(line.startswith("Won ") for line in check.lines)
    assert not hasattr(verify, "EXPECTED_STATUS")
