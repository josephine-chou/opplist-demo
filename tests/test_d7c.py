"""Batch D7c (2026-11-13, no schema change): three fixes to D7b's half of
editing Cargo and Route, and a direct customer's question on Pending on us
(her choice A of 13 Nov).

1. no array inside an array reaches Firestore: lists/places is maps, and
   to_value refuses a list directly inside a list before anything is sent;
2. an official pick compares by its system, country and code - typing the
   old To Confirm words and picking the official place is a change; a pick
   of the place the route has changes nothing and logs nothing;
3. a size the site changes is never read back from the old words, and
   cargo[].dims shows the size as the Cargo card does;
4. Waiting on us and Answered for a direct customer's Quoted case, on the
   case page, Tracking, Pending on us and the site - the ladder stays off.

Every company, person, place and number here is invented; cases from
OPP-9840 (the world of test_d7b) and OPP-9860, domains .test.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import assign_list, db, firebase_pull, firebase_sync as fs, site_edit, tracking
from opplist import web as web_module
from opplist.web import create_app
from test_d7b import act, line, world  # noqa: F401  (the D7b world and its take-in)


# ------------------------------------------------- 1. arrays in arrays --
def test_to_value_refuses_a_list_inside_a_list_before_anything_is_sent(tmp_path):
    with pytest.raises(TypeError, match="no array inside an array"):
        fs.to_fields({"rows": [["SAN ANTONIO", "San Antonio"]]})
    fs.to_fields({"rows": [{"k": "SAN ANTONIO", "aliases": ["SAI"]}]})          # a list in a map is fine
    from opplist import local_store
    client = local_store.LocalStore("demo-opplist", tmp_path / "store")
    with pytest.raises(TypeError):
        client.commit([fs.Write("lists/assign", {"waiting": []}), fs.Write("lists/places", {"rows": [["X", "Y"]]})])
    assert not (tmp_path / "store").exists()                                  # nothing of the commit was written


def test_lists_places_is_maps_and_firestore_takes_it(world):
    places = site_edit.places_doc(world["conn"])
    assert all(isinstance(r, dict) and set(r) == {"k", "name", "country", "type", "code", "aliases"}
               for r in places["rows"])
    assert all(set(c) == {"name", "iso2"} for c in places["countries"])
    assert [r["k"] for r in places["rows"]] == sorted(r["k"] for r in places["rows"])     # the order as before
    fs.to_fields(places)
    built = fs.build(world["conn"], today="2027-03-02")
    for doc in built.cases.values():
        fs.to_fields(doc)                                                    # every case document too


# ---------------------------------------------- 2. an official pick --
def test_the_old_to_confirm_words_and_an_official_pick_is_a_change(world):
    w = world
    conn = w["conn"]
    route = w["ids"]["route", 9840]
    repo = db.Repository(conn, source="ui")
    repo.edit_route(route, {"origin_loc_id": None, "origin_loc": "Quarrosa", "origin_country": "Brazil"})
    repo.commit()
    assert site_edit.route_ed(conn, conn.execute("SELECT * FROM enquiry_route WHERE id = ?", (route,)).fetchone())["o"] \
        == {"t": "Quarrosa"}
    off = {"sys": "UN/LOCODE", "cc": "BR", "code": "QSZ", "name": "Quarrosa", "sub": "SP", "kind": "seaport",
           "lat": -23.95, "lon": -46.3167}
    ok, message = act(w, "c1", part="route", op="change", id=route,
                      **{"from": {"o": {"t": "Quarrosa"}}, "to": {"o": {"t": "Quarrosa", "off": off}}})
    assert ok and message == "OPP-9840: route A changed (origin).", message
    place = conn.execute("SELECT * FROM master_location WHERE unlocode = 'BRQSZ'").fetchone()
    assert (place["status"], place["source"]) == ("active", "manual") and "team site" in place["note"]
    assert conn.execute("SELECT origin_loc_id, origin_loc FROM enquiry_route WHERE id = ?", (route,)).fetchone()[:] == \
        (place["id"], place["key"])


def test_a_pick_of_the_place_the_route_has_changes_and_logs_nothing(world):
    w = world
    conn = w["conn"]
    known = {"sys": "UN/LOCODE", "cc": "CL", "code": "SAI", "name": "San Antonio", "sub": "VS", "kind": "seaport",
             "lat": -33.6, "lon": -71.6}
    logged = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    ok, message = act(w, "c2", part="route", op="change", id=w["ids"]["route", 9840],
                      **{"from": {"d": [{"k": "SAN ANTONIO"}]}, "to": {"d": [{"t": "San Antonio", "off": known}]}})
    assert ok and message == "OPP-9840: route A already had those values; nothing to change."
    # the site_action row is the only write
    assert conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == logged
    assert site_edit.timeline(conn, w["ids"][9840]) == []


# ---------------------------------------------------------- 3. the size --
def test_a_size_the_site_changes_is_kept_and_pushed_as_the_card_shows_it(world):
    w = world
    conn = w["conn"]
    repo = db.Repository(conn, source="ui")
    line_id = w["ids"]["line1", 9840]
    repo.edit_cargo_line(line_id, {"dims_text": "120x100x100 cm", "dim_l": 120, "dim_w": 100, "dim_h": 100,
                                   "dims_unit": "cm"})
    repo.commit()
    ok, message = act(w, "c3", part="cargo", op="change", id=line_id,
                      **{"from": {"l": 120, "h": 100}, "to": {"l": 130, "h": 90}})
    assert ok, message
    got = line(w, line_id)
    assert (got["dim_l"], got["dim_h"], got["length_cm"], got["height_cm"]) == (130, 90, 130, 90)
    assert got["dims_text"] == "120x100x100 cm"                          # kept as written, never read back
    pushed = fs._cargo(conn, {"id": w["ids"][9840]})[0]
    assert pushed["dims"] == "130 x 100 x 90 cm"
    # a line with the words only
    other = w["ids"]["line2", 9840]
    repo.edit_cargo_line(other, {"dims_text": "about 1 m each", "dim_l": None, "dim_w": None, "dim_h": None})
    repo.commit()
    assert fs._cargo(conn, {"id": w["ids"][9840]})[1]["dims"] == "about 1 m each"


# ---------------------------------------- 4. a direct customer's question --
@pytest.fixture
def direct(tmp_path, monkeypatch):
    path = tmp_path / "d7c.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "owners", ["Ana", "Ben"])
    customer = add_party(repo, "Holloway Pumps", ptype="Direct Customer", country="USA")
    ids = {"path": path}
    for n in (9860, 9861):
        ids[n] = add_enquiry(repo, "OPP-{}".format(n), customer, status="Quoted", received="2026-10-01", scope="In",
                             service_mode="Air", origin_country="USA", dest_country="Chile")
        repo.set_status(ids[n], "Quoted", "2026-10-02")
    repo.commit()
    monkeypatch.setattr(web_module, "_today", lambda: "2026-11-13")
    app = create_app(path)
    app.config["TESTING"] = True
    yield {"conn": conn, "ids": ids, "client": app.test_client()}
    conn.close()


def test_a_direct_customers_question_waits_on_us_everywhere_and_answered_clears_it(direct):
    w = direct
    conn, client = w["conn"], w["client"]
    page = client.get("/enquiries/{}".format(w["ids"][9860])).data.decode("utf-8")
    assert 'id="waiting-form"' in page and "Direct customer" in page                # the block, and no ladder
    assert 'id="fu-log"' not in page
    client.post("/enquiries/{}/waiting-on-us".format(w["ids"][9860]),
                data={"at": "2026-11-12", "what": "Can the pumps go as two crates?", "who": ""})
    page = client.get("/enquiries/{}".format(w["ids"][9860])).data.decode("utf-8")
    assert "Waiting on us since" in page and 'id="answered-form"' in page
    assert [r.opp_no for r in assign_list.pending_listed(conn, "2026-11-13")] == ["OPP-9860"]
    replied = [i for i in tracking.rule_quoted_no_answer(conn, "2026-11-13") if i.extra.get("step") == "Replied"]
    assert [i.label for i in replied] == ["OPP-9860"]
    # the ladder stays off: OPP-9861, quoted six weeks ago, is no FU1 nor Decide
    assert not [i for i in tracking.rule_quoted_no_answer(conn, "2026-11-13") if i.label == "OPP-9861"]
    built = fs.build(conn, today="2026-11-13")
    assert [p["opp"] for p in built.assign["pending"]] == ["OPP-9860"]
    assert built.cases["OPP-9860"]["pend"] is not None and built.cases["OPP-9861"]["pend"] is None
    # Answered on the team site clears it
    repo = db.Repository(conn, source="web")
    ok, message = firebase_pull.take_in(repo, "a1", {"type": "answered", "opp": "OPP-9860", "by": "ana@jojofreight.example",
                                                     "name": "Ana", "owner": "Ana", "app": "site",
                                                     "at": "2026-11-13T15:00:00Z", "date": "2026-11-13",
                                                     "note": "Yes, two crates"}, "2026-11-13")
    repo.commit()
    assert ok and "left Pending on us" in message
    assert assign_list.pending_listed(conn, "2026-11-13") == []
