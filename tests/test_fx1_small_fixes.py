"""Batch FX1 (2026-11-11): Legacy only while Dropped, a Lost case's prices
editable on the Edit of its closing event, quotes in the folder settings.

Every company, person, place, number and date here is invented; cases from
OPP-9801, mail on .test domains.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, paths, scheduled, status as status_mod, undo
from opplist.web import create_app


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "fx1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "closed_reasons_lost", ["Price too high", "Other"])
    agent = add_party(repo, "Corvane Haulage", country="Chile")
    repo.commit()
    n = [9801]

    def case(status="New", received="2026-10-06", **extra):
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


def page(c, url):
    return c.get(url).data.decode("utf-8")


# ------------------------------------------------- 1. Legacy only Dropped --
def test_a_legacy_row_is_legacy_only_while_it_is_dropped(world):
    conn, repo, case, path = world
    dropped = case("Dropped", received="2026-03-02", legacy=1)
    lost = case("Lost", received="2026-03-03", legacy=1, closed_reason="Price too high")
    new = case("New", received="2026-03-04", legacy=1)
    c = client(path)
    legacy_view = page(c, "/enquiries?view=legacy")
    closed_view = page(c, "/enquiries?view=closed")
    open_view = page(c, "/enquiries?view=open")
    row = 'data-id="{}" data-status="{}"'
    assert row.format(dropped, "Legacy") in legacy_view and row.format(dropped, "Legacy") not in closed_view
    assert row.format(lost, "Lost") in closed_view
    assert 'data-id="{}"'.format(lost) not in legacy_view and 'data-id="{}"'.format(new) not in legacy_view
    assert row.format(new, "New") in open_view
    legacy_label = 's-legacy" title="Imported from the old workbook; outcome not recorded">Legacy</span>'
    assert legacy_label in page(c, "/enquiries/{}".format(dropped))
    lost_page = page(c, "/enquiries/{}".format(lost))
    assert legacy_label not in lost_page and '<span class="pill big s-lost">Lost</span>' in lost_page


# ------------------------------------------------- 2. the Lost event's Edit --
@pytest.fixture
def lost_case(world):
    conn, repo, case, path = world
    got = case("Quoted", received="2026-10-07")
    web = db.Repository(conn, source="ui")
    web.set_status(got, "Lost", "2026-10-20", closed_reason="Price too high")
    web.update("enquiry", got, {"lost_their_price": "USD 2.10/kg", "lost_our_price": "USD 2.45/kg"})
    web.commit()
    event = web.lost_closing_event(got)
    assert event is not None
    return conn, got, event, path


def prices(conn, case_id):
    return tuple(conn.execute("SELECT lost_their_price, lost_our_price FROM enquiry WHERE id = ?",
                              (case_id,)).fetchone())


def test_the_closing_lost_event_edit_shows_the_prices_and_other_events_do_not(lost_case):
    conn, got, event, path = lost_case
    body = page(client(path), "/enquiries/{}".format(got))
    form = body[body.index('id="event-{}"'.format(event)):]
    form = form[:form.index("</form>")]
    assert 'name="lost_their_price" value="USD 2.10/kg"' in form and 'name="lost_our_price" value="USD 2.45/kg"' in form
    assert body.count('name="lost_their_price" value="USD 2.10/kg"') == 1   # not on the Quoted event's Edit


def test_saving_the_lost_edit_writes_the_prices_in_one_action_and_clears_an_emptied_one(lost_case):
    conn, got, event, path = lost_case
    c = client(path)
    last = conn.execute("SELECT MAX(id) FROM change_log").fetchone()[0]
    c.post("/enquiries/{}/events/{}".format(got, event),
           data={"action": "save", "at": "2026-10-21", "status": "Lost", "reason": "Price too high", "note": "",
                 "lost_their_price": "USD 1.95/kg", "lost_our_price": ""})
    conn.rollback()
    assert prices(conn, got) == ("USD 1.95/kg", None)
    rows = conn.execute("SELECT * FROM change_log WHERE id > ? ORDER BY id", (last,)).fetchall()
    assert len({r["action_id"] for r in rows}) == 1
    assert {(r["table_name"], r["field"]) for r in rows} >= {("status_event", "at"), ("enquiry", "lost_their_price"),
                                                              ("enquiry", "lost_our_price")}
    # undoing that one action brings the date and both prices back
    repo = db.Repository(conn, source="ui")
    undo._revert(repo, rows, got)
    repo.commit()
    assert prices(conn, got) == ("USD 2.10/kg", "USD 2.45/kg")
    assert conn.execute("SELECT at FROM status_event WHERE id = ?", (event,)).fetchone()[0] == "2026-10-20"


def test_a_save_that_moves_the_event_off_lost_ignores_the_boxes(lost_case):
    conn, got, event, path = lost_case
    client(path).post("/enquiries/{}/events/{}".format(got, event),
                      data={"action": "save", "at": "2026-10-20", "status": "Cancelled", "reason": "", "note": "",
                            "lost_their_price": "", "lost_our_price": "USD 9/kg"})
    conn.rollback()
    assert prices(conn, got) == ("USD 2.10/kg", "USD 2.45/kg")


def test_a_250_character_price_is_cut_at_200_with_the_message(lost_case):
    conn, got, event, path = lost_case
    c = client(path)
    long = "USD 1.80/kg via Tollmere, " + "x" * 224
    assert len(long) == 250
    c.post("/enquiries/{}/events/{}".format(got, event),
           data={"action": "save", "at": "2026-10-20", "status": "Lost", "reason": "Price too high", "note": "",
                 "lost_their_price": long, "lost_our_price": "USD 2.45/kg"})
    conn.rollback()
    assert prices(conn, got)[0] == long[:200]
    assert "Their price cut at 200 characters." in page(c, "/enquiries/{}".format(got))
    assert status_mod.lost_prices({"lost_our_price": "y" * 250}.get) == ({"lost_our_price": "y" * 200}, ["Our price"])


def test_the_lost_form_keeps_200_characters(world):
    conn, repo, case, path = world
    got = case("Quoted")
    c = client(path)
    c.post("/enquiries/{}/status".format(got), data={
        "status": "Lost", "at": "2026-10-22", "closed_reason": "Price too high",
        "lost_their_price": "z" * 180, "lost_our_price": "w" * 210})
    conn.rollback()
    assert prices(conn, got) == ("z" * 180, "w" * 200)


# --------------------------------------------------- 3. the folder settings --
def test_folder_setting_strips_one_pair_of_quotes():
    assert paths.folder_setting('  "\\\\files.test\\Share\\Copies"  ') == "\\\\files.test\\Share\\Copies"
    assert paths.folder_setting("'D:\\Copies'") == "D:\\Copies"
    assert paths.folder_setting('"D:\\Copies') == '"D:\\Copies'          # not a pair
    assert paths.folder_setting(None) == ""


def test_a_quoted_folder_is_saved_without_its_quotes(world):
    conn, repo, case, path = world
    client(path).post("/settings", data={"offsite_backup_dir": ' "\\\\files.test\\Share\\OppList copies" ',
                                         "export_share_dir": "'\\\\files.test\\Share\\Exports'"})
    conn.rollback()
    assert db.get_setting(conn, "offsite_backup_dir") == "\\\\files.test\\Share\\OppList copies"
    assert db.get_setting(conn, "export_share_dir") == "\\\\files.test\\Share\\Exports"


def test_the_off_pc_copy_runs_with_a_quoted_stored_value(tmp_path):
    live = tmp_path / "live.db"
    conn = db.open_db(live)
    seed_reference(db.Repository(conn, source="test"))
    folder = tmp_path / "share" / "OppList copies"
    folder.mkdir(parents=True)
    db.set_setting(conn, scheduled.OFFSITE_SETTING, '"{}"'.format(folder))       # as Copy as path gives it
    conn.commit()
    conn.close()
    points = tmp_path / "restore-points"
    points.mkdir()
    (points / "opplist_20271109.db").write_bytes(b"the nightly copy")
    got = scheduled.offsite_copy(live, today=dt.date(2027, 11, 10))
    assert got["state"] == "copied"
    assert (folder / "OppList_20271109.db").read_bytes() == b"the nightly copy"
    assert scheduled.offsite_status(live, today=dt.date(2027, 11, 10))["folder"] == str(folder)
