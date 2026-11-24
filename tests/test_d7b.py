"""Batch D7b (2026-11-13, no schema change): the team site changes Cargo and
Route - OppList's half (INTERFACE.md section 8) - and two small fixes.

1. cases/{OPP}.ed on the cases the site may change; lists/places and
   meta/sync.editLists;
2. the action 'edit': a change applied field by field (change_log source
   web, one action), a field she changed in between becomes one 'edit'
   suggestion, Accept writes the site's value, Dismiss keeps hers and
   rewrites the answer; add and strike; refusals; places picked from the
   official lists;
3. one Timeline line per applied action, read from change_log;
4. the common words take five more, and the Mail card's plain-subject
   fallback never runs on common words alone.

Every company, person, place, code and number here is invented (the country
codes are real ones, the places in them are not); cases from OPP-9840,
domains .test.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_enquiry, add_party
from opplist import (case_mail, db, firebase_pull, firebase_sync, mail_archive, migrate_master, parsing,
                     site_edit, suggestions)
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app

TODAY = "2027-03-02"
AT = dt.datetime(2027, 3, 2, 15, 0, tzinfo=dt.timezone.utc)
UNLOCODE = "\n".join([",BR,,.BRAZIL,,,,,,,,", ",BR,QSZ,Quarrosa,Quarrosa,SP,1-------,AI,2101,,2357S 04619W,",
                      ",CL,QVL,Quellavista,Quellavista,VS,1-------,AI,2101,,3302S 07138W,"])
AIRPORTS = "\n".join([
    "id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,continent,iso_country,iso_region,municipality,"
    "scheduled_service,gps_code,iata_code,local_code,home_link,wikipedia_link,keywords",
    "1,SBQZ,large_airport,Quarrosa International,-23.43,-46.47,2459,SA,BR,BR-SP,Quarrosa,yes,SBQZ,QZR,,,,"])


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    path = tmp_path / "d7b.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    for name, region, iso2 in (("USA", "North America", "US"), ("Chile", "Latin America", "CL"),
                               ("Brazil", "Latin America", "BR"), ("Unknown", "Undefined", None)):
        repo.insert("country", {"name": name, "region": region, "iso2": iso2})
    miami = repo.add_master_location("MIAMI, FL", "USA", "city", name="Miami", subdivision="FL", source="phase1")
    repo.add_master_location("MIA", "USA", "airport", name="Miami International", iata="MIA", is_airport=1,
                             source="phase1")
    sai = repo.add_master_location("SAN ANTONIO", "Chile", "seaport", name="San Antonio", unlocode="CLSAI",
                                   is_seaport=1, source="phase1")
    vap = repo.add_master_location("VALPARAISO", "Chile", "seaport", name="Valparaiso", unlocode="CLVAP",
                                   is_seaport=1, source="phase1")
    repo.add_master_alias("SAI", sai["id"], "legacy_key")
    conn.execute("ALTER TABLE location RENAME TO location_old")
    conn.execute(migrate_master.LOCATION_VIEW_SQL)
    db.set_setting(conn, "container_types", ["20GP", "40HQ"])
    files = tmp_path / "official"
    files.mkdir()
    (files / "codes.csv").write_text(UNLOCODE, encoding="utf-8")
    (files / "airports.csv").write_text(AIRPORTS, encoding="utf-8")
    db.set_setting(conn, "official_unlocode_path", str(files / "codes.csv"))
    db.set_setting(conn, "official_airports_path", str(files / "airports.csv"))
    agent = add_party(repo, "Brindmoor Freight", country="Chile")
    ids = {"path": path, "agent": agent, "miami": miami["id"], "sai": sai["id"], "vap": vap["id"]}
    for n, status, extra in ((9840, "New", {}), (9841, "Quoted", {"handled_by": "QZX"}), (9842, "New", {"legacy": 1}),
                             (9843, "Won", {}), (9844, "Quoted", {})):
        ids[n] = add_enquiry(repo, "OPP-{}".format(n), agent, status=status, received="2027-03-01", scope="In",
                             service_mode="LCL", origin_country="USA", dest_country="Chile", incoterm="FOB", **extra)
        route = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (ids[n],)).fetchone()[0]
        repo.edit_route(route, {"origin_loc_id": miami["id"], "origin_loc": "MIAMI, FL", "dest_places": [sai["id"]],
                                "service_scope": "Door to port"})
        ids["route", n] = route
        ids["line1", n] = repo.add_cargo_line(ids[n], {"pieces": 10, "pack_unit": "Pallets", "weight_value": 7000,
                                                       "weight_unit": "kg", "commodity": "Slate tiles"})
        ids["line2", n] = repo.add_cargo_line(ids[n], {"pieces": 2, "pack_unit": "Crates", "weight_value": 300,
                                                       "weight_unit": "kg", "commodity": "Grout"})
    repo.commit()
    yield {"conn": conn, "ids": ids, "path": path, "files": files}
    conn.close()


def act(w, action_id, opp="OPP-9840", **fields):
    """One action taken in as the pull takes it in."""
    repo = db.Repository(w["conn"], source="web")
    action = {"type": "edit", "opp": opp, "by": "ava@jojofreight.example", "name": "Ava", "owner": "Ava", "app": "site",
              "at": AT}
    action.update(fields)
    ok, message = firebase_pull.take_in(repo, action_id, action, TODAY)
    repo.commit()
    return ok, message


def line(w, line_id):
    return w["conn"].execute("SELECT * FROM enquiry_cargo WHERE id = ?", (line_id,)).fetchone()


def case_page(w, case):
    app = create_app(w["path"])
    app.config["TESTING"] = True
    return app.test_client().get("/enquiries/{}".format(case)).data.decode("utf-8")


# --------------------------------------------------------------- 1. push --
def test_ed_only_on_the_cases_the_site_may_change(world):
    w = world
    built = firebase_sync.build(w["conn"], today=TODAY)
    assert "ed" in built.cases["OPP-9840"] and "ed" in built.cases["OPP-9844"]
    for opp in ("OPP-9841", "OPP-9842", "OPP-9843"):          # another office's, legacy, Won
        assert "ed" not in built.cases[opp], opp
    ed = built.cases["OPP-9840"]["ed"]
    first = ed["cargo"][0]
    assert (first["id"], first["n"], first["pieces"], first["pack"], first["wt"], first["wu"], first["wb"],
            first["com"], first["hs"], first["val"], first["cbm"]) == \
        (w["ids"]["line1", 9840], 1, 10, "Pallets", 7000, "kg", "total", "Slate tiles", None, None, None)
    route = ed["routes"][0]
    assert (route["n"], route["oc"], route["dc"], route["o"], route["d"], route["pod"], route["mode"], route["inc"],
            route["scope"]) == ("A", "USA", "Chile", {"k": "MIAMI, FL"}, [{"k": "SAN ANTONIO"}], [], "LCL", "FOB",
                                "Door to port")


def test_lists_places_and_edit_lists(world):
    w = world
    places = site_edit.places_doc(w["conn"])
    # D7c: maps - Firestore takes no array inside an array
    assert {"k": "SAN ANTONIO", "name": "San Antonio", "country": "Chile", "type": "seaport", "code": "CLSAI",
            "aliases": "SAI"} in places["rows"]
    assert {"k": "MIA", "name": "Miami International", "country": "USA", "type": "airport", "code": "MIA",
            "aliases": ""} in places["rows"]
    assert {"name": "Chile", "iso2": "CL"} in places["countries"] and {"name": "Unknown", "iso2": ""} in places["countries"]
    firebase_sync.to_fields(places)                                     # Firestore takes it
    lists = site_edit.edit_lists(w["conn"])
    assert lists["containers"] == ["20GP", "40HQ"] and "Door to port" in lists["scopes"]
    assert "LCL" in lists["modes"] and "FOB" in lists["incoterms"] and "Pallets" in lists["packing"]


# ------------------------------------------------- 2. the edit action --
def test_a_change_applied_field_by_field_with_one_timeline_line(world):
    w = world
    conn = w["conn"]
    events = conn.execute("SELECT COUNT(*) FROM status_event").fetchone()[0]
    ok, message = act(w, "e1", part="cargo", op="change", id=w["ids"]["line1", 9840],
                      **{"from": {"wt": 7000, "pieces": 10}, "to": {"wt": 7200, "pieces": 12}})
    assert ok and message == "OPP-9840: cargo line 1 changed (weight, pieces)."
    got = line(w, w["ids"]["line1", 9840])
    assert (got["weight_value"], got["weight_unit"], got["pieces"], got["weight_kg"]) == (7200, "kg", 12, 7200)
    action = conn.execute("SELECT change_action FROM site_action WHERE id = 'e1'").fetchone()[0]
    logged = conn.execute("SELECT DISTINCT action_id, source FROM change_log WHERE table_name = 'enquiry_cargo' "
                          "AND row_id = ? AND field IN ('weight_value', 'pieces')", (w["ids"]["line1", 9840],)).fetchall()
    assert [tuple(r) for r in logged] == [(action, "web")]
    assert conn.execute("SELECT COUNT(*) FROM status_event").fetchone()[0] == events      # never a note
    items = site_edit.timeline(conn, w["ids"][9840])
    assert [(i["by"], i["text"]) for i in items] == [("Ava", "Cargo 1: Pieces 10 → 12; Weight 7000 kg → 7200 kg")]
    tl = firebase_sync.build(conn, today=TODAY).cases["OPP-9840"]["tl"]
    assert [l for l in tl if l["src"] == "web"] == [{"at": dt.date.today().isoformat(), "text": "Changed by Ava",
                                                    "sub": "Cargo 1: Pieces 10 → 12; Weight 7000 kg → 7200 kg",
                                                    "src": "web"}]
    page = case_page(w, w["ids"][9840])
    assert "Cargo 1: Pieces 10 → 12; Weight 7000 kg → 7200 kg" in page and "team site &middot; Ava" in page


def test_a_field_she_changed_becomes_one_suggestion_and_accept_writes_the_sites(world):
    w = world
    conn = w["conn"]
    mine = db.Repository(conn, source="ui")
    mine.edit_cargo_line(w["ids"]["line1", 9840], {"pieces": 11})        # hers, after the push
    mine.commit()
    ok, message = act(w, "e2", part="cargo", op="change", id=w["ids"]["line1", 9840],
                      **{"from": {"wt": 7000, "pieces": 10}, "to": {"wt": 7300, "pieces": 15}})
    assert ok and message == ("OPP-9840: cargo line 1 changed (weight). "
                              "OPP-9840: 1 change is waiting for Iris (she changed it in OppList).")
    assert (line(w, w["ids"]["line1", 9840])["weight_value"], line(w, w["ids"]["line1", 9840])["pieces"]) == (7300, 11)
    s = conn.execute("SELECT * FROM suggestion WHERE kind = 'edit'").fetchone()
    assert (s["state"], s["source"], s["proposed_status"], s["evidence_sender"]) == ("pending", "web", "pieces", "Ava")
    assert site_edit.conflict_lines(conn, s) == ["Pieces: 11 in OppList, 15 from Ava"]
    page = case_page(w, w["ids"][9840])
    card = page[page.index('id="sg-{}"'.format(s["id"])):]
    card = card[:card.index("</tr>")]
    assert "Team site change" in card and "Pieces: 11 in OppList, 15 from Ava" in card and "data-sg-edit" not in card
    said = suggestions.accept(db.Repository(conn, source="ui"), s["id"])
    assert said == "OPP-9840: the team site's pieces written (cargo line 1)."
    assert line(w, w["ids"]["line1", 9840])["pieces"] == 15
    assert site_edit.answers(db.Repository(conn, source="web")) == 1
    row = conn.execute("SELECT * FROM site_action WHERE id = 'e2'").fetchone()
    assert row["message"] == "Iris accepted the team site's pieces on OPP-9840." and row["answered_at"] is None
    texts = [(i["text"], i["accepted"]) for i in site_edit.timeline(conn, w["ids"][9840])]
    assert ("Cargo 1: Pieces 11 → 15", "Iris") in texts and ("Cargo 1: Weight 7000 kg → 7300 kg", "") in texts


def test_dismiss_keeps_hers_and_rewrites_the_answer(world):
    w = world
    conn = w["conn"]
    mine = db.Repository(conn, source="ui")
    mine.edit_route(w["ids"]["route", 9840], {"incoterm": "EXW"})
    mine.commit()
    ok, message = act(w, "e3", part="route", op="change", id=w["ids"]["route", 9840],
                      **{"from": {"inc": "FOB"}, "to": {"inc": "CIF"}})
    assert ok and message == "OPP-9840: 1 change is waiting for Iris (she changed it in OppList)."
    s = conn.execute("SELECT * FROM suggestion WHERE kind = 'edit'").fetchone()
    suggestions.dismiss(db.Repository(conn, source="ui"), s["id"])
    site_edit.answers(db.Repository(conn, source="web"))
    row = conn.execute("SELECT * FROM site_action WHERE id = 'e3'").fetchone()
    assert row["message"] == "Iris kept OppList's incoterm on OPP-9840."
    from opplist import site_close
    assert site_close.answer_ok(conn, row) is False
    assert conn.execute("SELECT incoterm FROM enquiry_route WHERE id = ?", (w["ids"]["route", 9840],)).fetchone()[0] == "EXW"


def test_add_and_strike_and_the_last_route_is_not_struck(world):
    w = world
    conn = w["conn"]
    ok, message = act(w, "e4", part="cargo", op="add", to={"pieces": 4, "pack": "Crates", "wt": 120, "wu": "kg",
                                                         "com": "Kiln shelves", "hs": "6903.90", "val": 1800, "cur": "usd"})
    assert ok and message == "OPP-9840: cargo line 3 added."
    added = conn.execute("SELECT * FROM enquiry_cargo WHERE enquiry_id = ? ORDER BY id DESC", (w["ids"][9840],)).fetchone()
    assert (added["hs_code"], added["cargo_value"], added["cargo_value_cur"], added["source"]) == ("6903.90", 1800, "USD", "manual")
    ok, message = act(w, "e5", part="cargo", op="strike", id=w["ids"]["line2", 9840])
    assert ok and message == "OPP-9840: cargo line 2 struck out." and line(w, w["ids"]["line2", 9840])["voided"] == 1
    ok, message = act(w, "e6", part="route", op="strike", id=w["ids"]["route", 9840])
    assert not ok and "last route" in message
    ok, message = act(w, "e7", part="route", op="add", to={"oc": "USA", "dc": "Chile", "o": {"k": "MIA"},
                                                         "d": [{"k": "VALPARAISO"}], "mode": "Air", "inc": "FCA"})
    assert ok and message == "OPP-9840: route B added."
    ok, message = act(w, "e8", part="route", op="strike", id=w["ids"]["route", 9840])
    assert ok and message == "OPP-9840: route A struck out."
    texts = [i["text"] for i in site_edit.timeline(conn, w["ids"][9840])]
    assert {"Cargo 3 added", "Cargo 2 struck out", "Route B added", "Route A struck out"} <= set(texts)


@pytest.mark.parametrize("opp, fields, why", [
    ("OPP-9843", {"part": "cargo", "op": "change", "from": {"pieces": 10}, "to": {"pieces": 11}}, "can no longer be changed"),
    ("OPP-9840", {"part": "cargo", "op": "change", "from": {"zz": 1}, "to": {"zz": 2}}, "does not know the cargo field 'zz'"),
    ("OPP-9840", {"part": "cargo", "op": "change", "from": {"com": "Slate tiles"}, "to": {"com": "x" * 201}},
     "longer than 200"),
    ("OPP-9840", {"part": "cargo", "op": "change", "from": {"hs": None}, "to": {"hs": "25A7"}}, "HS code"),
    ("OPP-9840", {"part": "route", "op": "change", "from": {"mode": "LCL"}, "to": {"mode": "Rocket"}}, "not one OppList offers"),
])
def test_refused_actions(world, opp, fields, why):
    w = world
    fields = dict(fields)
    fields.setdefault("id", w["ids"]["line1", 9840] if fields["part"] == "cargo" else w["ids"]["route", 9840])
    if opp == "OPP-9843":
        fields["id"] = w["ids"]["line1", 9843]
    ok, message = act(w, "r-" + why[:5], opp=opp, **fields)
    assert not ok and why in message and "nothing was changed" in message
    row = w["conn"].execute("SELECT result FROM site_action ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row["result"] == "refused"


# ------------------------------------------------------------- places --
def test_an_official_pick_is_a_location_with_its_code_and_the_typed_alias(world):
    w = world
    conn = w["conn"]
    off = {"sys": "UN/LOCODE", "cc": "BR", "code": "QSZ", "name": "Quarrosa", "sub": "SP", "kind": "seaport",
           "lat": -23.95, "lon": -46.3167}
    ok, message = act(w, "p1", part="route", op="change", id=w["ids"]["route", 9840],
                      **{"from": {"dc": "Chile", "d": [{"k": "SAN ANTONIO"}]},
                         "to": {"dc": "Brazil", "d": [{"t": "quarosa port", "off": off}]}})
    assert ok, message
    place = conn.execute("SELECT * FROM master_location WHERE unlocode = 'BRQSZ'").fetchone()
    assert (place["key"], place["status"], place["source"], place["country"], place["type"]) == \
        ("QUARROSA", "active", "manual", "Brazil", "seaport")
    assert "team site" in place["note"] and "Ava" in place["note"] and "BRQSZ" in place["note"]
    aliases = [r[0] for r in conn.execute("SELECT alias FROM master_location_alias WHERE location_id = ?", (place["id"],))]
    assert aliases == ["QUAROSA PORT"]
    assert conn.execute("SELECT dest_loc FROM enquiry_route WHERE id = ?", (w["ids"]["route", 9840],)).fetchone()[0] == "QUARROSA"


def test_a_pick_not_in_the_files_is_to_confirm_and_a_known_code_is_the_existing_place(world):
    w = world
    conn = w["conn"]
    off = {"sys": "UN/LOCODE", "cc": "BR", "code": "QQQ", "name": "Querinha", "sub": None, "kind": "city",
           "lat": None, "lon": None}
    ok, _ = act(w, "p2", part="route", op="change", id=w["ids"]["route", 9840],
                **{"from": {"o": {"k": "MIAMI, FL"}}, "to": {"o": {"t": "Querinha", "off": off}}})
    assert ok
    assert conn.execute("SELECT status FROM master_location WHERE unlocode = 'BRQQQ'").fetchone()[0] == "to_confirm"
    known = {"sys": "UN/LOCODE", "cc": "CL", "code": "SAI", "name": "San Antonio", "sub": "VS", "kind": "seaport",
             "lat": -33.6, "lon": -71.6}
    db.Repository(conn, source="ui").edit_route(w["ids"]["route", 9844], {"dest_places": [w["ids"]["vap"]]})
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM master_location").fetchone()[0]
    ok, _ = act(w, "p3", opp="OPP-9844", part="route", op="change", id=w["ids"]["route", 9844],
                **{"from": {"d": [{"k": "VALPARAISO"}]}, "to": {"d": [{"t": "San Antonio harbour", "off": known}]}})
    assert ok and conn.execute("SELECT COUNT(*) FROM master_location").fetchone()[0] == before
    assert conn.execute("SELECT dest_loc FROM enquiry_route WHERE id = ?", (w["ids"]["route", 9844],)).fetchone()[0]         == "SAN ANTONIO"
    assert conn.execute("SELECT 1 FROM master_location_alias WHERE alias = 'SAN ANTONIO HARBOUR' AND location_id = ?",
                        (w["ids"]["sai"],)).fetchone() is not None


# -------------------------------------------------------- 4. the fixes --
def test_the_five_more_common_words():
    assert parsing.only_common_words("Cotizaciones importação marítima aérea exportação")


def test_no_plain_subject_fallback_on_common_words(world, tmp_path):
    w = world
    archive = mail_archive.ARCHIVE_DB
    store = mail_archive.open_archive(archive)
    mail = MailInfo(subject="IMPORT AIR - QXR-551877", received_on="2027-03-01", received_at="2027-03-01 09:00",
                    sent_at="2027-03-01 09:00", entry_id="EID-x", conversation_id="CONV-x",
                    conversation_topic="IMPORT AIR - QXR-551877", sender_name="Nara Quell",
                    sender_address="nara@quarrow.test", to_addresses=["desk@jojofreight.example"], body_excerpt="Hi",
                    folder_path="\\Mailbox\\Inbox", message_id="<x@d7b.test>")
    mail_archive.store(store, [mail_archive.record_from_info(mail)])
    store.commit()
    store.close()
    assert not case_mail.fallback_subject("IMPORT AIR") and case_mail.fallback_subject("RATE REQUEST QUARROSA")
    with mail_archive.ArchiveSearch(archive) as search:
        assert case_mail.mails_for(search, "", [], {"IMPORTAIR"}, ["IMPORT AIR"]) == []
        found = case_mail.mails_for(search, "", [], {"NOTHING"}, ["IMPORT AIR - QXR"])
    assert [m.entry_id for m in found] == ["EID-x"]                 # a subject with more than common words still falls back
