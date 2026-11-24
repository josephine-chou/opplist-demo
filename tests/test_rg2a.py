"""Batch RG2a (2026-11-20, schema v57): Register from Outlook reads the
origin and destination right (A to F), keeps what the reader read beside
what was saved (register_read, layer 1), and remembers a place she
corrected as an alias with one click (layer 2).

Every company, person, mail, place, code and number here is invented:
cases OPP-9401 upward, domains .test, places such as PORT ALDER and
QUILLMOUTH, and two small official files written for the test.
"""

from __future__ import annotations

import json
import re

import pytest

from conftest import add_party, seed_reference
from opplist import db, intake, migrate_master, outlook_bridge, parsing, places, schema_migrations
from opplist.web import create_app

UNLOCODE = (
    ',"BR","QWM","Wrenmouth","Wrenmouth","","1-------","AI","2501","","",""\n'
    ',"CL","QRV","Ravelholm","Ravelholm","","--3-----","AI","2501","","",""\n'
)
AIRPORTS = (
    'id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,continent,iso_country,iso_region,municipality,'
    'scheduled_service,gps_code,iata_code,local_code,home_link,wikipedia_link,keywords\n'
    '1,ZQHF,large_airport,Quillmouth Harbour Field,-10.5,-40.25,10,SA,BR,BR-ZZ,Quillmouth,yes,ZQHF,QHF,,,,\n'
)


def master_world(repo, tmp_path):
    conn = repo.conn
    for name, region, iso2 in (("USA", "North America", "US"), ("Chile", "Latin America", "CL"),
                               ("Brazil", "Latin America", "BR"), ("Hong Kong", "Asia", "HK"),
                               ("Unknown", "Undefined", None)):
        repo.insert("country", {"name": name, "region": region, "iso2": iso2})
    alder = repo.add_master_location("PORT ALDER", "Chile", "city", name="Port Alder", source="phase1")
    repo.add_master_location("PAQ", "Chile", "airport", name="Port Alder Field", iata="PAQ", is_airport=1,
                             parent_id=alder["id"], source="phase1")
    repo.add_master_location("ALDER HARBOUR", "Chile", "seaport", name="Alder Harbour", is_seaport=1,
                             parent_id=alder["id"], source="phase1")
    repo.add_master_location("QUILLMOUTH", "Brazil", "city", name="Quillmouth", source="phase1")
    santiago = repo.add_master_location("SANTIAGO", "Chile", "city", name="Santiago", source="phase1")
    repo.add_master_location("SCL", "Chile", "airport", name="Santiago Field", iata="SCL", is_airport=1,
                             parent_id=santiago["id"], source="phase1")
    bransford = repo.add_master_location("BRANSFORD, IL", "USA", "city", name="Bransford", subdivision="IL",
                                         source="phase1")
    repo.add_master_alias("BRANSFORD", bransford["id"], "legacy_key")
    fenwick = repo.add_master_location("FENWICK, TX", "USA", "city", name="Fenwick", subdivision="TX",
                                       source="phase1")
    repo.add_master_alias("FENWICK", fenwick["id"], "legacy_key")
    # 'ALDER' names the city and its airport: the mode picks (addendum a)
    repo.add_master_alias("ALDER", alder["id"], "legacy_key")
    repo.add_master_alias("ALDER", conn.execute("SELECT id FROM master_location WHERE key = 'PAQ'").fetchone()[0],
                          "manual")
    conn.execute("ALTER TABLE location RENAME TO location_old")
    conn.execute(migrate_master.LOCATION_VIEW_SQL)
    ids = {"larkspur": add_party(repo, "Larkspur Freight", country="Chile"),
           "orvane": add_party(repo, "Orvane Lines", ptype="Carrier", country="Chile")}
    repo.insert("party_domain", {"domain": "larkspur.test", "party_id": ids["larkspur"]})
    add_party(repo, "(TBC)", ptype="Unknown", country="Unknown", placeholder=1)
    db.set_setting(conn, "internal_domains", ["jojofreight.test"])
    db.set_setting(conn, "next_opp_number", 9401)
    unlocode, airports = tmp_path / "unlocode.csv", tmp_path / "airports.csv"
    unlocode.write_text(UNLOCODE, encoding="utf-8")
    airports.write_text(AIRPORTS, encoding="utf-8")
    db.set_setting(conn, "official_unlocode_path", str(unlocode))
    db.set_setting(conn, "official_airports_path", str(airports))
    repo.commit()
    return ids


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "rg2a.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    ids = master_world(repo, tmp_path)
    yield {"conn": conn, "repo": repo, "path": path, "ids": ids}
    conn.close()


def mail(subject, body="", sender="tess@larkspur.test", name="Tess Wren", internal=False, key="9401"):
    return outlook_bridge.MailInfo(subject=subject, received_on="2027-03-04", received_at="2027-03-04 09:40",
                                   sender_address=sender, sender_name=name, sender_internal=internal,
                                   to_addresses=["desk@jojofreight.test"], to_names=["JJC-DESK"],
                                   body_excerpt=body, entry_id="ENTRY-" + key, conversation_id="CONV-" + key)


def lane_of(draft):
    v = draft.values
    return (v.get("origin_loc"), v.get("origin_country"), v.get("dest_loc"), v.get("dest_country"))


# ---------------------------------------------- A: the quoted inquiry first --
INQUIRY = ("From: Tess Wren <tess@larkspur.test>\nSent: Tuesday, March 2, 2027 10:14 AM\n"
           "To: JJC-LENA PARK <lena@jojofreight.test>\nSubject: Rate request, one container\n\n"
           "Hello team,\nPlease quote from Bransford to Port Alder, 2 pallets, 300 kg.\nTess\n")


def test_a_colleagues_forward_reads_the_lane_from_the_quoted_inquiry(world):
    # the forwarder's own lines name another place: the inquiry's lane wins
    top = "Lena here - please register.\nOrigin: Fenwick\n\n"
    draft = intake.build(world["conn"], mail("FW: Rate request, one container", top + INQUIRY, sender="lena@jojofreight.test",
                                             name="Lena Park", internal=True))
    assert lane_of(draft) == ("BRANSFORD, IL", "USA", "PORT ALDER", "Chile")
    assert draft.sources["origin_loc"].startswith(intake.FROM_QUOTED)
    assert intake.read_from(draft, "origin_loc") == "quoted inquiry"
    assert draft.values["requester_party_id"] == world["ids"]["larkspur"]


def test_the_forwarders_lines_fill_only_what_the_inquiry_leaves_out(world):
    inquiry = INQUIRY.replace("from Bransford to Port Alder", "to Port Alder")
    inquiry = inquiry.replace("Please quote to Port Alder", "Please quote, destination below")
    inquiry = inquiry.replace("destination below,", "destination below.\nDestination: Port Alder\n")
    top = "Lena here.\nOrigin: Fenwick\nDestination: Quillmouth\n\n"
    draft = intake.build(world["conn"], mail("FW: Rate request, one container", top + inquiry, sender="lena@jojofreight.test",
                                             name="Lena Park", internal=True))
    assert draft.values["dest_loc"] == "PORT ALDER"                      # the inquiry said it
    assert draft.values["origin_loc"] == "FENWICK, TX"                   # the inquiry did not: ours fills it
    assert draft.sources["origin_loc"] == intake.FROM_BODY


# --------------------------------------------- B: a label above its value --
def test_a_label_alone_on_its_line_takes_the_next_line_as_its_value(world):
    body = "Dear team,\nPlease quote below.\nPOL\nBransford\nPort of Discharge:\nPort Alder\nCommodity\nValves\n"
    draft = intake.build(world["conn"], mail("Rate request 1x40HQ", body))
    assert lane_of(draft) == ("BRANSFORD, IL", "USA", "PORT ALDER", "Chile")
    assert intake.read_from(draft, "origin_loc") == "table" and intake.read_from(draft, "dest_loc") == "table"


def test_a_value_under_its_column_header_is_read():
    lines = ["Origin\tDestination\tWeight", "Bransford\tPort Alder\t300 kg"]
    assert parsing.joined_lane_lines(lines) == [("Origin: Bransford", ("origin", False)),
                                                ("Destination: Port Alder", ("dest", False))]
    body = parsing.parse_body_lanes("\n".join(lines), known_locations={"BRANSFORD", "PORT ALDER"})
    assert body.origins == ["Bransford"] and body.dests == ["Port Alder"] and body.tabled == {"origin": True, "dest": True}


def test_a_table_cell_that_is_a_company_or_an_address_is_not_a_place():
    body = parsing.parse_body_lanes("Pick up\nHolloway Mills LLC\nPOD\nPort Alder\n",
                                    known_locations={"PORT ALDER"})
    assert body.origins == [] and body.dests == ["Port Alder"]
    # a label above a 'label: value' line leaves that line its own
    body = parsing.parse_body_lanes("Destination:\nPOD: Port Alder\n", known_locations={"PORT ALDER"})
    assert body.dests == ["Port Alder"]


# --------------------------------------------- C: the subject's codes ------
def test_a_country_code_and_an_airport_code_joined_by_a_dash(world):
    draft = intake.build(world["conn"], mail("HK-SCL // AIR // 120 KG"))
    assert lane_of(draft) == (None, "Hong Kong", "SCL", "Chile")
    assert draft.sources["dest_loc"] == intake.FROM_SUBJECT


def test_ex_a_place_in_the_subject_is_where_it_comes_from(world):
    draft = intake.build(world["conn"], mail("RATE REQUEST ex QUILLMOUTH // 1X20GP"))
    assert draft.values["origin_loc"] == "QUILLMOUTH" and draft.values["origin_country"] == "Brazil"
    assert "dest_loc" not in draft.values


def test_a_country_to_a_country_and_the_body_wins_when_it_says_otherwise(world):
    draft = intake.build(world["conn"], mail("Quotation // BRAZIL TO CHILE // LCL"))
    assert lane_of(draft) == (None, "Brazil", None, "Chile")
    # the body names the destination: it wins over the subject's code
    draft = intake.build(world["conn"], mail("RFQ // QUILLMOUTH>SCL // AIR",
                                             "Hello,\nDestination: Port Alder\nThanks\n"))
    assert draft.values["dest_loc"] == "PORT ALDER"


# --------------------------------------------- D: the place the mode wants --
def test_an_air_case_reads_the_airport_of_a_city_and_an_ocean_case_the_port(world, monkeypatch):
    monkeypatch.setattr(intake, "MODE_PLACES", True)
    draft = intake.build(world["conn"], mail("AIR // PORT ALDER AIRPORT - SCL"))
    assert draft.values["origin_loc"] == "PAQ"
    draft = intake.build(world["conn"], mail("RATE REQUEST // 1X40HQ",
                                             "Please quote FCL.\nPOL: Port Alder port\nPOD: Quillmouth\n"))
    assert draft.values["origin_loc"] == "ALDER HARBOUR" and draft.values["dest_loc"] == "QUILLMOUTH"


def test_the_mode_step_is_off_and_a_city_named_alone_stays_the_city(world):
    # her cases keep the city for '<city> airport' as often as the airport (RG2a's measure)
    assert intake.MODE_PLACES is False
    draft = intake.build(world["conn"], mail("AIR // PORT ALDER AIRPORT - SCL"))
    assert draft.values["origin_loc"] == "PORT ALDER"


def test_a_city_and_its_own_airport_named_alike_are_picked_by_the_mode(world):
    draft = intake.build(world["conn"], mail("AIR RATES from Alder to Quillmouth"))
    assert lane_of(draft) == ("PAQ", "Chile", "QUILLMOUTH", "Brazil")
    draft = intake.build(world["conn"], mail("Rates from Alder to Quillmouth // FCL"))
    assert lane_of(draft) == ("PORT ALDER", "Chile", "QUILLMOUTH", "Brazil")
    draft = intake.build(world["conn"], mail("Rates from Alder to Quillmouth"))
    assert draft.values["origin_loc"] == "PORT ALDER" and "origin" not in draft.location_choices


# --------------------------------------------- E: a company is not a place --
def test_a_partys_name_or_a_company_word_is_refused_as_a_place(world):
    draft = intake.build(world["conn"], mail("RATE REQUEST // ORVANE LINES - PORT ALDER // 1X40HQ",
                                             sender="tess@larkspur.test"))
    assert "origin_loc" not in draft.values and draft.values["dest_loc"] == "PORT ALDER"
    assert draft.values["origin_alt"] == "ORVANE LINES"
    assert any("'ORVANE LINES' is a company's name (a party on file)" in n for n in draft.notes)
    draft = intake.build(world["conn"], mail("Rate Request // Air Freight From HOLLOWAY MILLS, INC to PORT ALDER"))
    assert "origin_loc" not in draft.values and draft.values["origin_alt"] == "HOLLOWAY MILLS, INC"
    assert any("is a company's name" in n for n in draft.notes)


def test_a_country_read_as_a_place_is_the_country(world):
    draft = intake.build(world["conn"], mail("Inquiry", "Hello,\nPlease quote a shipment from Quillmouth to Brazil.\n"))
    assert "dest_loc" not in draft.values and draft.values["dest_country"] == "Brazil"


# --------------------------------------------- F: the official lists ------
def register_form(conn, draft, **extra):
    values = {k: draft.values.get(k) or "" for k in ("received_on", "origin_country", "origin_loc", "dest_country",
                                                     "dest_loc", "service_mode", "incoterm", "commodity",
                                                     "container_type", "container_qty", "thread_key",
                                                     "conversation_id", "entry_id", "source_subject")}
    values.update({"requester_party_id": str(draft.values.get("requester_party_id") or ""),
                   "created_by": "outlook", "form_token": "tok-" + draft.values.get("entry_id", ""),
                   "reader_read": json.dumps(intake.reader_values(conn, draft)),
                   "official_new": json.dumps(draft.official_new),
                   "container_type": draft.values.get("container_type") or "40HQ",
                   "container_qty": str(draft.values.get("container_qty") or 1), "service_mode": "FCL",
                   "commodity": "valves"})
    values.update(extra)
    return values


def app_of(world):
    world["conn"].commit()
    application = create_app(world["path"])
    application.config["TESTING"] = True
    return application


def test_an_official_place_is_offered_and_created_only_at_register(world):
    conn = world["conn"]
    draft = intake.build(conn, mail("RATE REQUEST // WRENMOUTH, BRAZIL - PORT ALDER // 1X40HQ"))
    assert draft.values["origin_loc"] == "WRENMOUTH" and draft.values["origin_country"] == "Brazil"
    assert draft.official_new["WRENMOUTH"]["unlocode"] == "BRQWM"
    assert intake.read_from(draft, "origin_loc") == "official list"
    assert any("the official list has it (UN/LOCODE BRQWM, Wrenmouth)" in n for n in draft.notes)
    assert conn.execute("SELECT 1 FROM master_location WHERE key = 'WRENMOUTH'").fetchone() is None
    client = app_of(world).test_client()
    saved = client.post("/enquiries/new", data=register_form(conn, draft))
    assert saved.status_code == 302, saved.data.decode("utf-8")[:2000]
    row = conn.execute("SELECT type, status, source, unlocode, country, is_seaport FROM master_location "
                       "WHERE key = 'WRENMOUTH'").fetchone()
    assert tuple(row) == ("seaport", "active", "intake", "BRQWM", "Brazil", 1)
    assert conn.execute("SELECT origin_loc FROM enquiry WHERE opp_no = 'OPP-9401'").fetchone()[0] == "WRENMOUTH"


def test_an_official_airport_code_is_built_with_its_city(world):
    conn = world["conn"]
    draft = intake.build(conn, mail("AIR // QHF - SCL // 80 KG"))
    assert draft.values["origin_loc"] == "QHF" and draft.values["origin_country"] == "Brazil"
    spec = draft.official_new["QHF"]
    assert (spec["type"], spec["iata"], spec["name"]) == ("airport", "QHF", "Quillmouth Harbour Field")


def test_an_official_place_the_case_no_longer_holds_is_not_created(world):
    conn = world["conn"]
    draft = intake.build(conn, mail("RATE REQUEST // WRENMOUTH, BRAZIL - PORT ALDER // 1X40HQ", key="9402"))
    client = app_of(world).test_client()
    saved = client.post("/enquiries/new", data=register_form(conn, draft, origin_loc="QUILLMOUTH",
                                                             origin_country="Brazil"))
    assert saved.status_code == 302
    assert conn.execute("SELECT 1 FROM master_location WHERE key = 'WRENMOUTH'").fetchone() is None


# --------------------------------------------- layer 1: register_read ------
def test_register_read_rows_are_written_at_register_beside_the_value_saved(world):
    conn = world["conn"]
    draft = intake.build(conn, mail("RATE REQUEST // BRANSFORD - KELLAN // 1X40HQ", key="9403"))
    assert draft.values["dest_loc"] == "KELLAN" and draft.place_words["dest"] == "KELLAN"
    assert conn.execute("SELECT COUNT(*) FROM register_read").fetchone()[0] == 0
    client = app_of(world).test_client()
    saved = client.post("/enquiries/new", data=register_form(conn, draft, dest_loc="PORT ALDER", dest_country="Chile"))
    assert saved.status_code == 302
    rows = {r["field"]: tuple(r)[1:] for r in conn.execute(
        "SELECT field, value_read, read_from, value_saved, words FROM register_read ORDER BY id")}
    assert rows["origin_loc"] == ("BRANSFORD, IL", "subject", "BRANSFORD, IL", "BRANSFORD")
    assert rows["dest_loc"] == ("KELLAN", "subject", "PORT ALDER", "KELLAN")
    assert rows["requester_party_id"] == ("Larkspur Freight", "sender", "Larkspur Freight", None)
    assert rows["received_on"][0] == "2027-03-04"
    # nothing on the page changes because of it; a registration by hand writes none
    client.post("/enquiries/new", data={k: v for k, v in register_form(conn, draft, form_token="tok-x",
                                                                        entry_id="", conversation_id="",
                                                                        thread_key="", decision="new").items()
                                        if k not in ("created_by", "reader_read")})
    assert conn.execute("SELECT COUNT(DISTINCT enquiry_id) FROM register_read").fetchone()[0] == 1


# --------------------------------------------- layer 2: remember an alias ---
def registered(world, words="KELLAN", saved="PORT ALDER", country="Chile", key="9404", **extra):
    conn = world["conn"]
    draft = intake.build(conn, mail("RATE REQUEST // BRANSFORD - {} // 1X40HQ".format(words), key=key))
    client = app_of(world).test_client()
    response = client.post("/enquiries/new", data=register_form(conn, draft, dest_loc=saved, dest_country=country,
                                                                **extra))
    assert response.status_code == 302
    enquiry_id = int(re.search(r"/enquiries/(\d+)", response.headers["Location"]).group(1))
    return client, enquiry_id


def test_a_correction_remembered_as_an_alias_is_read_on_the_next_mail(world):
    client, enquiry_id = registered(world)
    page = client.get("/enquiries/{}".format(enquiry_id)).data.decode("utf-8")
    assert "Remember &lsquo;KELLAN&rsquo; as PORT ALDER" in page
    done = client.post("/enquiries/{}/remember-place".format(enquiry_id), data={"side": "dest"},
                       follow_redirects=True).data.decode("utf-8")
    assert "Remembered &#39;KELLAN&#39; as PORT ALDER" in done or "Remembered 'KELLAN' as PORT ALDER" in done
    conn = world["conn"]
    assert conn.execute("SELECT a.kind FROM master_location_alias a JOIN master_location l ON l.id = a.location_id "
                        "WHERE a.alias = 'KELLAN' AND l.key = 'PORT ALDER'").fetchone()[0] == "manual"
    assert "Remember &lsquo;KELLAN&rsquo;" not in client.get("/enquiries/{}".format(enquiry_id)).data.decode("utf-8")
    draft = intake.build(conn, mail("RATE REQUEST // BRANSFORD - KELLAN // 1X40HQ", key="9405"))
    assert draft.values["dest_loc"] == "PORT ALDER" and draft.values["dest_country"] == "Chile"


def test_the_same_words_for_a_second_place_are_refused_with_why(world):
    client, first = registered(world)
    client.post("/enquiries/{}/remember-place".format(first), data={"side": "dest"})
    conn = world["conn"]
    # a later case: the reader read KELLAN as PORT ALDER, she saved QUILLMOUTH
    client, second = registered(world, saved="QUILLMOUTH", country="Brazil", key="9406", decision="new")
    assert places.remember_offers(conn, second)[0]["key"] == "QUILLMOUTH"
    body = client.post("/enquiries/{}/remember-place".format(second), data={"side": "dest"},
                       follow_redirects=True).data.decode("utf-8")
    assert "already names PORT ALDER: it cannot name QUILLMOUTH as well" in body.replace("&#39;", "'")
    assert conn.execute("SELECT COUNT(*) FROM master_location_alias WHERE alias = 'KELLAN'").fetchone()[0] == 1


def test_an_alias_already_there_is_not_offered_again(world):
    conn = world["conn"]
    client, enquiry_id = registered(world, words="FENWICK", saved="FENWICK, TX", country="USA", key="9407")
    # the reader read FENWICK as FENWICK, TX: nothing to remember
    assert places.remember_offers(conn, enquiry_id) == []


# --------------------------------------------- v57 -------------------------
def test_v57_from_v56_keeps_every_row(tmp_path):
    path = tmp_path / "old.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    p = add_party(repo, "Larkspur Freight", country="Unknown")
    repo.insert("enquiry", {"opp_no": "OPP-9409", "requester_party_id": p, "status": "New",
                            "received_on": "2027-03-01"})
    repo.commit()
    conn.execute("DROP TABLE register_read")
    conn.execute("UPDATE setting SET value = '56' WHERE key = 'schema_version'")
    conn.commit()
    notes = schema_migrations.upgrade(conn)
    assert "register_read table created" in notes and "foreign_key_check: nothing" in notes
    counted = next(n for n in notes if n.startswith("rows before and after: "))
    assert "enquiry 1 / 1" in counted
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION >= 57
    assert schema_migrations.upgrade(conn) == []
    assert [r[1] for r in conn.execute("PRAGMA table_info(register_read)")] == [
        "id", "enquiry_id", "field", "value_read", "read_from", "value_saved", "words", "created_at"]
    conn.close()
