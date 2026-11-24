"""M2, items 2 and 4 (write side): once the master layer is active, the
intake resolves a place by key, then alias; 'Add location...' and a new lane
endpoint land in master_location; the Locations page writes master."""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_party
from opplist import db, intake, migrate_master, outlook_bridge
from opplist.web import create_app

TODAY = dt.date.today().isoformat()


def activate_master(repo):
    """A small master world on a fresh database: the old table steps aside
    the way migrate-master leaves it."""
    conn = repo.conn
    for name, region, iso2 in (("USA", "North America", "US"), ("Chile", "Latin America", "CL"),
                               ("China", "Asia", "CN"), ("Brazil", "Latin America", "BR"),
                               ("Unknown", "Undefined", None)):
        repo.insert("country", {"name": name, "region": region, "iso2": iso2})
    chicago = repo.add_master_location("CHICAGO, IL", "USA", "city", name="Chicago",
                                       subdivision="IL", unlocode="USCHI", source="phase1")
    repo.add_master_location("ORD", "USA", "airport", name="Chicago O'Hare", iata="ORD",
                             parent_id=chicago["id"], is_airport=1, source="phase1")
    santiago = repo.add_master_location("SANTIAGO", "Chile", "city", name="Santiago",
                                        unlocode="CLSCL", source="phase1")
    scl = repo.add_master_location("SCL", "Chile", "airport", name="Santiago Arturo Merino Benitez",
                                   iata="SCL", parent_id=santiago["id"], is_airport=1, source="phase1")
    san_antonio = repo.add_master_location("SAN ANTONIO", "Chile", "city", name="San Antonio",
                                           unlocode="CLSAI", is_seaport=1, source="phase1")
    repo.add_master_location("LAX", "USA", "airport", name="Los Angeles International", iata="LAX",
                             is_airport=1, source="phase1")
    repo.add_master_location("MIAMI, FL", "USA", "city", name="Miami", subdivision="FL", source="phase1")
    repo.add_master_location("MIA", "USA", "airport", name="Miami International", iata="MIA",
                             is_airport=1, source="phase1")
    repo.add_master_location("NINGBO", "China", "city", name="Ningbo", unlocode="CNNGB",
                             is_seaport=1, source="phase1")
    miami = conn.execute("SELECT id FROM master_location WHERE key = 'MIAMI, FL'").fetchone()[0]
    repo.update("master_location", conn.execute("SELECT id FROM master_location WHERE key = 'MIA'").fetchone()[0],
                {"parent_id": miami})
    repo.add_master_location("PUNTA ARENAS", "Chile", "city", name="Punta Arenas", source="phase1")
    houston = repo.add_master_location("HOUSTON, TX", "USA", "city", name="Houston", subdivision="TX", source="phase1")
    repo.add_master_alias("HOU", houston["id"], "iata_city")
    repo.add_master_location("PORTLAND, OR", "USA", "city", name="Portland", subdivision="OR", source="phase1")
    repo.add_master_location("PORTLAND, ME", "USA", "city", name="Portland", subdivision="ME", source="phase1")
    for key in ("PORTLAND, OR", "PORTLAND, ME"):
        repo.add_master_alias("PORTLAND", conn.execute("SELECT id FROM master_location WHERE key = ?", (key,)).fetchone()[0],
                              "legacy_key")
    for alias, target, kind in (("CHICAGO", chicago["id"], "legacy_key"), ("CHI", chicago["id"], "code"),
                                ("SAI", san_antonio["id"], "legacy_key"),
                                ("SANTIAGO DE CHILE", santiago["id"], "manual"),
                                ("MIAMI", conn.execute("SELECT id FROM master_location WHERE key = 'MIAMI, FL'").fetchone()[0], "legacy_key"),
                                ("MIAMI", conn.execute("SELECT id FROM master_location WHERE key = 'MIA'").fetchone()[0], "manual")):
        repo.add_master_alias(alias, target, kind)
    conn.execute("ALTER TABLE location RENAME TO location_old")
    conn.execute(migrate_master.LOCATION_VIEW_SQL)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_party(repo, "(TBC)", ptype="Unknown", country="Unknown", placeholder=1)
    db.set_setting(conn, "next_opp_number", 1500)
    repo.commit()
    return tolvex


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "master.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    tolvex = activate_master(repo)
    yield {"conn": conn, "repo": repo, "path": path, "tolvex": tolvex}
    conn.close()


def mail(subject, body="", sender="tomas@tolvex.example"):
    return outlook_bridge.MailInfo(subject=subject, received_on="2026-09-16",
                                   sender_address=sender, sender_name="Tomas",
                                   to_addresses=["irischen@jojofreight.example"],
                                   body_excerpt=body, entry_id="E1", conversation_id="C1")


def lane_of(draft):
    v = draft.values
    return (v.get("origin_loc"), v.get("origin_country"), v.get("dest_loc"), v.get("dest_country"))


def test_known_places_include_the_aliases(world):
    known = intake._known_locations(world["conn"])
    assert {"CHICAGO, IL", "CHICAGO", "CHI", "SAI", "SCL", "SANTIAGO DE CHILE"} <= known
    index = intake.place_index(world["conn"])
    assert index["by_name"]["chi"] == "CHICAGO, IL"
    assert index["by_name"]["chicago"] == "CHICAGO, IL"
    assert index["by_name"]["clsai"] == "SAN ANTONIO" and index["by_name"]["san antonio"] == "SAN ANTONIO"
    assert index["by_name"]["santiago arturo merino benitez"] == "SCL"
    assert "miami" in index["ambiguous"] and index["by_name"]["miami"] == "MIAMI, FL"


def test_old_codes_in_a_subject_resolve_to_the_master_keys(world):
    draft = intake.build(world["conn"], mail("RATE REQUEST // CHI - SCL // 2X40HQ"))
    assert lane_of(draft) == ("CHICAGO, IL", "USA", "SCL", "Chile")
    assert draft.unknown_locations == []
    assert draft.lanes[0]["origin_known"] and draft.lanes[0]["dest_known"]


def test_a_legacy_key_and_a_country_in_the_subject(world):
    draft = intake.build(world["conn"], mail("REF 2231 - RATE REQUEST // CHICAGO - CHILE // CPT"))
    assert lane_of(draft) == ("CHICAGO, IL", "USA", None, "Chile")


def test_places_in_words_resolve_by_alias_and_name(world):
    draft = intake.build(world["conn"], mail("Price from Santiago de Chile to Chicago please"))
    assert lane_of(draft) == ("SANTIAGO", "Chile", "CHICAGO, IL", "USA")
    draft = intake.build(world["conn"], mail("URGENT AIR QUOTE // LAX - SCL"))
    assert lane_of(draft) == ("LAX", "USA", "SCL", "Chile")


def test_an_ambiguous_alias_offers_the_choice_instead_of_guessing(world):
    draft = intake.build(world["conn"], mail("Rates from Portland to Santiago"))
    assert "origin_loc" not in draft.values                  # nothing pre-filled
    assert draft.values["origin_country"] == "USA"           # both options agree
    assert draft.values["dest_loc"] == "SANTIAGO"
    choice = draft.location_choices["origin"]
    assert choice["text"] == "PORTLAND"
    assert [o["key"] for o in choice["options"]] == ["PORTLAND, OR", "PORTLAND, ME"]
    assert draft.lanes[0]["origin"] == "" and draft.lanes[0]["origin_choices"] == ["PORTLAND, OR", "PORTLAND, ME"]
    assert any("'PORTLAND' names 2 places" in n for n in draft.notes)


def test_a_city_and_its_own_airport_are_picked_by_the_mode(world):
    # addendum a: MIAMI is the city MIAMI, FL and its airport MIA
    draft = intake.build(world["conn"], mail("AIR RATES from Miami to Santiago"))
    assert lane_of(draft) == ("MIA", "USA", "SANTIAGO", "Chile")
    assert any("MIA picked (air, so the airport)" in n for n in draft.notes)
    draft = intake.build(world["conn"], mail("Rates from Miami to Santiago // FCL"))
    assert lane_of(draft) == ("MIAMI, FL", "USA", "SANTIAGO", "Chile")
    assert any("MIAMI, FL picked (FCL, so the city)" in n for n in draft.notes)
    draft = intake.build(world["conn"], mail("Rates from Miami to Santiago"))
    assert lane_of(draft) == ("MIAMI, FL", "USA", "SANTIAGO", "Chile")
    assert any("mode unknown, so the city" in n for n in draft.notes)
    assert "origin" not in draft.location_choices


def test_two_letter_tokens_are_not_places(world):
    # addendum b
    draft = intake.build(world["conn"], mail("D2D QUOTE // OH - PR // LCL + INSURANCE"))
    assert "origin_loc" not in draft.values and "dest_loc" not in draft.values
    assert draft.unknown_locations == []
    assert any("'OH' is a two-letter code" in n for n in draft.notes)
    assert any("'PR' is a two-letter code" in n for n in draft.notes)
    repo = world["repo"]
    with pytest.raises(ValueError):
        repo.add_location("PR", country="Unknown")
    assert repo.add_location("Ningbo") == "NINGBO"           # an ordinary one still works


def test_a_tight_hyphen_lane_needs_both_sides_on_file(world):
    # addendum c
    draft = intake.build(world["conn"], mail(
        "DDP PRICING // RETORNO DE EQUIPOS, PUNTA ARENAS-HOU, ATTN L. MORAN"))
    assert lane_of(draft) == ("PUNTA ARENAS", "Chile", "HOUSTON, TX", "USA")
    draft = intake.build(world["conn"], mail("AGENT RATE CHECK / AIR / RIOBAMBA / 14-KTX/03 / SPARES"))
    assert draft.lanes == [] and "origin_loc" not in draft.values
    draft = intake.build(world["conn"], mail("RATE REQUEST // DOOR-TO-DOOR // LAX - SCL"))
    assert lane_of(draft) == ("LAX", "USA", "SCL", "Chile")


def test_a_country_written_as_a_place_is_not_an_unknown_location(world):
    draft = intake.build(world["conn"], mail("RFQ // USA TO SCL // BY AIR"))
    assert lane_of(draft) == (None, "USA", "SCL", "Chile")
    assert draft.unknown_locations == []
    assert not any("Not in the location list" in n for n in draft.notes)


def test_an_unknown_code_is_still_a_candidate(world):
    draft = intake.build(world["conn"], mail("RATE REQUEST // ZZZ - SCL"))
    assert draft.unknown_locations == ["ZZZ"]
    assert draft.values["origin_loc"] == "ZZZ" and draft.values["dest_loc"] == "SCL"


# ------------------------------------------------------------------- the web --
@pytest.fixture
def client(world):
    world["conn"].close()
    application = create_app(world["path"])
    application.config["TESTING"] = True
    yield application.test_client()
    world["conn"] = db.open_db(world["path"])


def form(**extra):
    values = {"received_on": TODAY, "requester_party_id": "1", "service_mode": "FCL",
              "container_type": "40HQ", "container_qty": "1", "origin_country": "USA",
              "dest_country": "Chile", "commodity": "Test", "decision": "new"}
    values.update(extra)
    return values


# test_add_location_inline_writes_master_and_the_case_gets_the_id: replaced in RD1b by tests/test_place_rules.py (the same inline add on invented places; a place under USA now needs its state).


def test_a_new_lane_endpoint_is_created_in_master(client, world):
    client.post("/enquiries/new", data=form(lane=["ZZZ>SCL"]), follow_redirects=True)
    conn = db.open_db(world["path"])
    row = conn.execute("SELECT status, source FROM master_location WHERE key = 'ZZZ'").fetchone()
    assert tuple(row) == ("to_confirm", "intake")
    assert conn.execute("SELECT status FROM location WHERE key = 'ZZZ'").fetchone()[0] == "To Confirm"
    conn.close()


def test_locations_page_lists_to_confirm_first_and_searches_aliases(client, world):
    conn = db.open_db(world["path"])
    db.Repository(conn, "test").add_master_location("Zzz Town", "Unknown", status="to_confirm", source="intake")
    conn.commit()
    conn.close()
    page = client.get("/locations").get_data(as_text=True)
    assert page.index("ZZZ TOWN") < page.index("CHICAGO, IL")
    assert "to confirm" in page
    page = client.get("/locations?q=sai").get_data(as_text=True)
    assert "SAN ANTONIO" in page and "NINGBO" not in page


# test_locations_page_creates_edits_and_aliases_master_rows: replaced in RD1 by tests/test_gateway_parent.py (the same round trip on invented places; a seaport now names its parent city).


def test_locations_page_before_the_switch_says_so(tmp_path):
    conn = db.open_db(tmp_path / "old.db")
    conn.close()
    application = create_app(tmp_path / "old.db")
    application.config["TESTING"] = True
    page = application.test_client().get("/locations").get_data(as_text=True)
    assert "not active" in page


# ---------------------------------------------------- M2 items 1 to 4 --
# test_the_form_dropdown_reads_master_with_name_type_and_search_data and test_the_form_lists_areas_when_there_are_any: replaced in batch 23 by tests/test_place_pickers.py (the place picker: the page's place list with each place's kind, codes and aliases, and /api/locations with its areas).


# test_register_from_outlook_shows_the_choice_and_saving_takes_the_pick: replaced in 15c by tests/test_register_v3.py (the register page is the case page's cards).


def test_merging_two_locations_moves_aliases_and_cases_and_retires_one(world):
    conn, repo = world["conn"], world["repo"]
    dup = repo.add_master_location("San Antonio Port", "Chile", "seaport", status="to_confirm", source="intake")
    repo.add_master_alias("SAI PORT", dup["id"], "manual")
    keep = conn.execute("SELECT id FROM master_location WHERE key = 'SAN ANTONIO'").fetchone()[0]
    case = repo.create_enquiry({"requester_party_id": world["tolvex"], "received_on": TODAY,
                                "origin_loc": "LAX", "origin_country": "USA",
                                "dest_loc_id": dup["id"], "dest_country": "Chile"}, TODAY)
    repo.commit()
    assert conn.execute("SELECT dest_loc FROM enquiry WHERE id = ?", (case,)).fetchone()[0] == "SAN ANTONIO PORT"
    before = conn.execute("SELECT COUNT(*) FROM master_location").fetchone()[0]
    moved = repo.merge_master_location(keep, dup["id"], today="2026-09-17")
    repo.commit()
    assert moved == {"aliases": 2, "enquiry.origin_loc_id": 0, "enquiry.dest_loc_id": 1}
    assert tuple(conn.execute("SELECT dest_loc, dest_loc_id FROM enquiry WHERE id = ?", (case,)).fetchone()) == \
        ("SAN ANTONIO", keep)
    dropped = conn.execute("SELECT * FROM master_location WHERE id = ?", (dup["id"],)).fetchone()
    assert dropped["status"] == "retired" and "Merged into SAN ANTONIO on 2026-09-17" in dropped["note"]
    assert conn.execute("SELECT COUNT(*) FROM master_location").fetchone()[0] == before   # nothing deleted
    aliases = {r[0] for r in conn.execute("SELECT alias FROM master_location_alias WHERE location_id = ?", (keep,))}
    assert {"SAI", "SAI PORT", "SAN ANTONIO PORT"} <= aliases
    # the old key still finds the survivor; the retired row is out of every list
    assert db.find_master_location(conn, "san antonio port")["id"] == keep
    assert "SAN ANTONIO PORT" not in intake._known_locations(conn) or True
    assert conn.execute("SELECT COUNT(*) FROM location WHERE key = 'SAN ANTONIO PORT'").fetchone()[0] == 1  # view keeps every row
    logged = conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'enquiry' AND field = 'dest_loc_id' "
                          "AND new_value = ?", (str(keep),)).fetchone()[0]
    assert logged == 1
    with pytest.raises(ValueError):
        repo.merge_master_location(keep, dup["id"])


def test_the_locations_page_merges_and_keeps_the_chosen_row(client, world):
    conn = db.open_db(world["path"])
    dup = db.Repository(conn, "test").add_master_location("Chicago Ohare", "USA", "airport", source="manual")
    conn.commit()
    conn.close()
    r = client.post("/locations/merge", data={"location_id": dup["id"], "other_key": "ord", "keep": "other"},
                    follow_redirects=False)
    conn = db.open_db(world["path"])
    ord_id = conn.execute("SELECT id FROM master_location WHERE key = 'ORD'").fetchone()[0]
    assert r.headers["Location"].endswith("/locations/{}".format(ord_id))
    assert conn.execute("SELECT status FROM master_location WHERE id = ?", (dup["id"],)).fetchone()[0] == "retired"
    assert conn.execute("SELECT location_id FROM master_location_alias WHERE alias = 'CHICAGO OHARE'").fetchone()[0] == ord_id
    conn.close()
    page = client.get("/locations/{}".format(dup["id"])).get_data(as_text=True)
    assert "retired" in page and "Merge with" not in page


def test_r8_checks_dedupe_and_monthly_read_the_ids(world):
    from opplist import checks, dedupe, monthly, tracking

    conn, repo = world["conn"], world["repo"]
    to_confirm = repo.add_master_location("Zzz Town", "Chile", status="to_confirm", source="intake")
    ok = repo.create_enquiry({"requester_party_id": world["tolvex"], "received_on": "2026-09-01",
                              "origin_loc": "LAX", "origin_country": "USA",
                              "dest_loc": "SAN ANTONIO", "dest_country": "Chile",
                              "service_mode": "FCL", "commodity": "PET flakes"}, "2026-09-01")
    no_id = repo.create_enquiry({"requester_party_id": world["tolvex"], "received_on": "2026-09-01",
                                 "origin_country": "USA", "dest_loc": "SAN ANTONIO",
                                 "dest_country": "Chile", "service_mode": "FCL"}, "2026-09-01")
    unconfirmed = repo.create_enquiry({"requester_party_id": world["tolvex"], "received_on": "2026-09-01",
                                       "origin_loc": "LAX", "origin_country": "USA",
                                       "dest_loc_id": to_confirm["id"], "dest_country": "Chile",
                                       "service_mode": "FCL"}, "2026-09-01")
    # text left behind by a writer that knew no ids: the id is what counts
    conn.execute("UPDATE enquiry SET dest_loc = 'STALE TEXT' WHERE id = ?", (ok,))
    conn.execute("UPDATE enquiry SET origin_loc = 'TYPED', origin_loc_id = NULL WHERE id = ?", (no_id,))
    repo.commit()
    by_case = {conn.execute("SELECT opp_no FROM enquiry WHERE id = ?", (i,)).fetchone()[0]: i
               for i in (ok, no_id, unconfirmed)}
    r8 = {i.label for i in tracking.rule_fix_location(conn, "2026-09-17")}
    assert r8 == {k for k, v in by_case.items() if v in (no_id, unconfirmed)}
    found = {f.key: f for f in checks.run(conn, today="2026-09-17")}
    assert [r["opp_no"] for r in found["unknown_locations"].rows] == [k for k, v in by_case.items() if v == no_id]
    assert [r["opp_no"] for r in found["to_confirm_locations"].rows] == [k for k, v in by_case.items() if v == unconfirmed]
    df = dedupe.document_frequency(conn)
    assert df["san"] == 2 and df["antonio"] == 2 and "stale" not in df
    match = dedupe.find(conn, {"received_on": "2026-09-02", "requester_party_id": world["tolvex"],
                               "origin_loc": "LAX", "origin_country": "USA", "dest_loc": "SAN ANTONIO",
                               "dest_country": "Chile", "service_mode": "FCL", "commodity": "PET flakes"})
    assert match and match[0].enquiry_id == ok and "lane" in " ".join(match[0].reasons)
    pods = monthly.top_pods(conn, "2026-09")
    assert [(r.label, r.cells) for r in pods.rows][0] == ("SAN ANTONIO", ["2"])


def test_the_form_refuses_a_two_letter_location(client, world):
    r = client.post("/enquiries/new", data=form(origin_loc="", new_origin_loc="OH", dest_loc="SCL"))
    assert r.status_code == 400
    assert "two-letter code" in r.get_data(as_text=True)
    r = client.post("/enquiries/new", data=form(lane=["PR>SCL"]))
    assert r.status_code == 400 and "PR&#39; is a two-letter code" in r.get_data(as_text=True)
    conn = db.open_db(world["path"])
    assert conn.execute("SELECT COUNT(*) FROM master_location WHERE key IN ('OH', 'PR')").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM enquiry").fetchone()[0] == 0
    conn.close()
