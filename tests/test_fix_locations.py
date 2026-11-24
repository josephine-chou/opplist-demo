"""The places the master list was missing, the rows a subject line created
by accident, and the rule that stops the next one (2026-09-17)."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, fix_locations, intake
from opplist.outlook_bridge import MailInfo
from test_master_intake import activate_master           # noqa: F401  (a master world)

MORE_COUNTRIES = (("Saudi Arabia", "Middle East & Africa", "SA"),
                  ("South Korea", "Asia", "KR"),
                  ("Mexico", "Latin America", "MX"),
                  ("Panama", "Latin America", "PA"),
                  ("Guatemala", "Latin America", "GT"),
                  ("India", "Asia", "IN"))


@pytest.fixture
def world(repo):
    """A master world with the three rows a mail subject created, and the
    one case pointing at the worst of them."""
    activate_master(repo)
    conn = repo.conn
    for name, region, iso2 in MORE_COUNTRIES:
        repo.insert("country", {"name": name, "region": region, "iso2": iso2})
    # planted as the row was written, before the naming rules (RD1b) refused it
    junk = {"id": repo.insert("master_location", {"key": "RFQ 73105 USA-GHANA", "name": "RFQ 73105 USA-GHANA",
                                                  "type": "city", "country": "USA", "status": "to_confirm",
                                                  "source": "intake"})}
    repo.add_master_location("TERRALUX", "Saudi Arabia", "city", status="to_confirm", source="intake")
    repo.add_master_location("MUNDRA", "India", "city", status="to_confirm", source="intake")
    party = add_party(repo, "Seavio", country="USA")
    case = add_enquiry(repo, "OPP-1442", party, received="2026-09-16",
                       origin_alt="BRANDT FORGE WORKS, Allentown, PA 18101, USA")
    repo.update("enquiry", case, {"origin_loc_id": junk["id"]})
    repo.commit()
    return {"conn": conn, "repo": repo, "case": case}


def place(conn, key):
    return conn.execute("SELECT * FROM master_location WHERE key = ?", (key,)).fetchone()


def aliases(conn, key):
    return sorted(r[0] for r in conn.execute(
        "SELECT a.alias FROM master_location_alias a JOIN master_location l ON l.id = a.location_id "
        "WHERE l.key = ?", (key,)))


# --------------------------------------------------------------- the dry run
def test_the_dry_run_lists_everything_and_writes_nothing(world):
    conn = world["conn"]
    before = conn.execute("SELECT COUNT(*) FROM master_location").fetchone()[0]
    report = fix_locations.run(conn)
    assert conn.execute("SELECT COUNT(*) FROM master_location").fetchone()[0] == before
    assert conn.execute("SELECT status FROM master_location WHERE key = 'TERRALUX'").fetchone()[0] == "to_confirm"
    text = "\n".join(report.lines())
    assert "dry run: nothing was written" in text
    for key in ("RIYADH", "RUH", "BUSAN", "PUS", "MANZANILLO, MX", "MANZANILLO, PA",
                "PUERTO QUETZAL", "ITAJAI", "RIO GRANDE", "VERACRUZ", "ALTAMIRA"):
        assert any(line.startswith(key + " ") or line.startswith("{:<16}".format(key))
                   for line in report.added), key
    # a gateway names the city it will hang under, even before the city exists
    assert any("RUH" in line and "under RIYADH" in line for line in report.added)
    # Shekou has no parent city in this world, so it is left for later
    assert any("SHEKOU: parent 'SHENZHEN' is not on file" in line for line in report.skipped)
    assert any("RFQ 73105" in line for line in report.retired)
    assert any("MUNDRA" in line for line in report.completed)


# ----------------------------------------------------------------- applying
def test_apply_adds_the_places_with_their_codes_and_parents(world):
    conn, repo = world["conn"], world["repo"]
    repo.add_master_location("SHENZHEN", "China", "city", name="Shenzhen", source="phase1")
    repo.commit()
    fix_locations.run(conn, dry_run=False)

    riyadh, ruh = place(conn, "RIYADH"), place(conn, "RUH")
    assert (riyadh["country"], riyadh["unlocode"], riyadh["source"]) == ("Saudi Arabia", "SARUH", "manual")
    assert (ruh["type"], ruh["iata"], ruh["is_airport"], ruh["parent_id"]) == ("airport", "RUH", 1, riyadh["id"])
    busan, pus = place(conn, "BUSAN"), place(conn, "PUS")
    assert (busan["country_code"], busan["unlocode"], busan["is_seaport"]) == ("KR", "KRPUS", 1)
    assert pus["parent_id"] == busan["id"] and pus["iata"] == "PUS"
    assert place(conn, "SHEKOU")["parent_id"] == place(conn, "SHENZHEN")["id"]
    # the two Manzanillos are two rows, and neither one owns the bare name
    assert place(conn, "MANZANILLO, MX")["country"] == "Mexico"
    assert place(conn, "MANZANILLO, PA")["country"] == "Panama"
    assert place(conn, "MANZANILLO") is None
    assert conn.execute("SELECT COUNT(*) FROM master_location_alias WHERE alias = 'MANZANILLO'"
                        ).fetchone()[0] == 0
    assert aliases(conn, "MANZANILLO, MX") == ["MANZANILLO MEXICO", "MXZLO"]
    assert aliases(conn, "PUERTO QUETZAL") == ["GTPRQ", "QUETZAL"]
    assert place(conn, "ITAJAI")["unlocode"] == "BRITJ" and "ITAJAÍ" in aliases(conn, "ITAJAI")
    for key in ("PUERTO QUETZAL", "ITAJAI", "RIO GRANDE", "VERACRUZ", "ALTAMIRA", "BUSAN", "SHEKOU"):
        assert place(conn, key)["is_seaport"] == 1, key
        assert place(conn, key)["source"] == "manual", key
    # every new row is logged like any other write
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'master_location' "
                        "AND source = 'import'").fetchone()[0] >= 12


def test_apply_retires_the_junk_and_tidies_the_case(world):
    conn, repo, case = world["conn"], world["repo"], world["case"]
    report = fix_locations.run(conn, dry_run=False, today="2026-09-17")
    assert place(conn, "RFQ 73105 USA-GHANA")["status"] == "retired"
    assert place(conn, "TERRALUX")["status"] == "retired"
    row = conn.execute("SELECT origin_loc_id, origin_alt FROM enquiry WHERE id = ?", (case,)).fetchone()
    assert row["origin_loc_id"] is None
    assert row["origin_alt"] == "BRANDT FORGE WORKS, Allentown, PA 18101, USA"   # kept
    note = conn.execute("SELECT at, note, kind, source FROM status_event WHERE enquiry_id = ? "
                        "AND kind = 'note' ORDER BY id DESC LIMIT 1", (case,)).fetchone()
    assert note["at"] == "2026-09-17" and note["source"] == "import"
    assert "'RFQ 73105 USA-GHANA' was read out of the subject as a place" in note["note"]
    assert any("OPP-1442" in line for line in report.cases)
    # nothing is deleted: both rows are still there, out of the way
    assert conn.execute("SELECT COUNT(*) FROM master_location WHERE status = 'retired'").fetchone()[0] == 2
    # and the intake no longer offers a retired place
    assert "TERRALUX" not in intake._known_locations(conn)
    assert "MUNDRA" in intake._known_locations(conn)


def test_apply_completes_mundra(world):
    conn = world["conn"]
    fix_locations.run(conn, dry_run=False)
    mundra = place(conn, "MUNDRA")
    assert (mundra["name"], mundra["type"], mundra["country"]) == ("Mundra", "seaport", "India")
    assert (mundra["country_code"], mundra["unlocode"], mundra["is_seaport"]) == ("IN", "INMUN", 1)
    assert (mundra["status"], mundra["source"]) == ("active", "manual")
    assert "INMUN" in aliases(conn, "MUNDRA")


def test_running_it_twice_changes_nothing_the_second_time(world):
    conn = world["conn"]
    fix_locations.run(conn, dry_run=False)
    after_first = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    second = fix_locations.run(conn, dry_run=False)
    assert second.added == [] and second.aliases == [] and second.retired == []
    assert second.completed == [] and second.cases == []
    assert len(second.already) >= 11
    assert conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == after_first


def test_a_database_without_the_master_tables_says_so(conn):
    with pytest.raises(ValueError, match="master location tables are not active"):
        fix_locations.run(conn)


def test_the_command_dry_runs_by_default_and_applies_with_a_restore_point(world, tmp_path, capsys):
    from opplist.__main__ import main

    conn = world["conn"]
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.commit()
    assert main(["fix-locations", "--db", str(path)]) == 0
    assert "dry run: nothing was written" in capsys.readouterr().out
    restore = tmp_path / "restore"
    assert main(["fix-locations", "--db", str(path), "--apply", "--restore-dir", str(restore)]) == 0
    printed = capsys.readouterr().out
    assert "restore point:" in printed and "places retired      : 2" in printed
    assert [p.name[:29] for p in restore.iterdir()] == ["opplist_before_fix-locations_"]


# ------------------------------------------- what may never become a place
def test_junk_place_refuses_the_four_shapes():
    countries = {"usa", "poland", "chile", "mexico"}

    def is_country(part):
        return " ".join(part.split()).casefold() in countries

    assert intake.junk_place("RFQ 73306 USA-POLAND", is_country) == "carries a four-digit number"
    assert intake.junk_place("PO 6203418", is_country) == "carries a four-digit number"
    assert intake.junk_place("RATE REQUEST", is_country) == "contains 'RATE', a mail word"
    assert intake.junk_place("QUOTE FOR TWO CRATES", is_country) == "contains 'QUOTE', a mail word"
    assert intake.junk_place("SHIPMENT OF VALVES", is_country) == "contains 'SHIPMENT', a mail word"
    assert intake.junk_place("USA-POLAND", is_country) == "is a country-to-country lane"
    assert intake.junk_place("Chile - Mexico", is_country) == "is a country-to-country lane"
    assert intake.junk_place("CAUCEDO TRANSSHIPMENT HUB", is_country).startswith("is longer than 24")
    # the places we do want
    for good in ("MUNDRA", "RUH", "SAN ANTONIO", "CHICAGO, IL", "MANZANILLO, MX",
                 "PUERTO QUETZAL", "RIO GRANDE", "ITAJAI"):
        assert intake.junk_place(good, is_country) == "", good
    assert intake.junk_place("", is_country) == "" and intake.junk_place(None) == ""


def _mail(subject):
    return MailInfo(subject=subject, received_on="2026-09-17", sender_address="ana@tolvex.example",
                    sender_name="Ana", to_addresses=["VictorLang@jojofreight.example"],
                    body_excerpt="Please quote.\n", entry_id="E1", conversation_id="C1")


def test_a_junk_subject_fills_the_free_text_and_creates_no_place(world):
    conn = world["conn"]
    draft = intake.build(conn, _mail("RE: RFQ 73306 USA-POLAND - Corvane"))
    assert "origin_loc" not in draft.values
    assert draft.values["origin_alt"] == "RFQ 73306 USA-POLAND"
    assert draft.sources["origin_alt"] == intake.FROM_NOT_A_PLACE
    assert "RFQ 73306 USA-POLAND" not in draft.unknown_locations
    assert all(lane["origin"] != "RFQ 73306 USA-POLAND" for lane in draft.lanes)
    assert any("carries a four-digit number" in note and "free-text" in note for note in draft.notes)
    # nothing was written by reading the mail
    assert conn.execute("SELECT COUNT(*) FROM master_location WHERE key LIKE 'RFQ%'").fetchone()[0] == 1


def test_a_real_place_is_still_offered(world):
    conn = world["conn"]
    draft = intake.build(conn, _mail("AIR QUOTE // RUH - SAN ANTONIO"))
    assert draft.values["origin_loc"] == "RUH"
    assert "RUH" in draft.unknown_locations                      # not on file yet: offer to add it
    assert any("Add location" in note for note in draft.notes)
    assert not any("not offered as a place" in note for note in draft.notes)


# --------------------------------------------------- flags and the review --
def test_the_two_ports_that_were_not_flagged_are_flagged(world):
    conn, repo = world["conn"], world["repo"]
    repo.add_master_location("XIAMEN", "China", "city", name="Xiamen", unlocode="CNXMN",
                             is_airport=1, source="phase1")
    repo.add_master_location("IQUIQUE", "Chile", "city", name="Iquique", unlocode="CLIQQ",
                             source="phase1")
    repo.commit()

    report = fix_locations.run(conn)                       # dry run says what it would do
    assert any(line.startswith("XIAMEN: is_seaport 0 -> 1") for line in report.flags)
    assert any(line.startswith("IQUIQUE: is_seaport 0 -> 1") for line in report.flags)
    assert place(conn, "XIAMEN")["is_seaport"] == 0

    fix_locations.run(conn, dry_run=False)
    assert place(conn, "XIAMEN")["is_seaport"] == 1
    assert place(conn, "IQUIQUE")["is_seaport"] == 1
    # everything else about the row is left alone
    assert (place(conn, "XIAMEN")["type"], place(conn, "XIAMEN")["source"]) == ("city", "phase1")
    assert place(conn, "XIAMEN")["is_airport"] == 1
    # and the second run has nothing left to correct
    assert fix_locations.run(conn, dry_run=False).flags == []


def test_a_flag_for_a_place_that_is_not_there_is_reported_not_created(world):
    conn = world["conn"]
    report = fix_locations.run(conn)
    assert any("XIAMEN: not on file, no flag to set" in line for line in report.skipped)
    assert place(conn, "XIAMEN") is None


def test_the_review_lists_every_code_without_a_seaport_flag(world):
    conn, repo = world["conn"], world["repo"]
    repo.add_master_location("XIAMEN", "China", "city", name="Xiamen", unlocode="CNXMN", source="phase1")
    repo.add_master_location("VIENNA", "Unknown", "city", name="Vienna", unlocode="ATVIE", source="phase1")
    repo.add_master_location("NOWHERE", "Unknown", "city", name="Nowhere", source="phase1")
    repo.commit()
    report = fix_locations.run(conn, dry_run=False)
    text = "\n".join(report.review)
    assert "VIENNA" in text and "ATVIE" in text            # a code, no port flag: listed
    assert "NOWHERE" not in text                           # no code at all: not listed
    assert "XIAMEN" not in text                            # this run flagged it as a port
    assert "SAN ANTONIO" not in text                       # already a seaport
    assert "a UN/LOCODE but is_seaport = 0" in "\n".join(report.lines())
    # the review changes nothing
    assert place(conn, "VIENNA")["is_seaport"] == 0


def test_the_flag_list_is_the_ports_that_were_read_and_picked():
    """The list is hand-picked from the 'UN/LOCODE but no port flag'
    review. Fort Lauderdale stays off it: its port is PORT EVERGLADES,
    a row of its own (2026-09-17)."""
    keys = [wanted["key"] for wanted in fix_locations.FLAGS]
    assert keys == ["XIAMEN", "IQUIQUE", "SHENZHEN", "DALIAN", "GUANGZHOU", "FUZHOU",
                    "MUMBAI", "DAMMAM", "MEJILLONES", "PENANG", "TOKYO", "TAMPA, FL",
                    "WILMINGTON, NC"]
    assert "FORT LAUDERDALE, FL" not in keys
    assert all(wanted.get("is_seaport") == 1 and wanted.get("why") for wanted in fix_locations.FLAGS)
    assert len(set(keys)) == len(keys)


def test_every_named_port_is_flagged_and_nothing_else_is(world):
    conn, repo = world["conn"], world["repo"]
    for wanted in fix_locations.FLAGS:
        # planted raw: a non-US port under USA is refused by the naming rules (RD1b) since
        repo.insert("master_location", {"key": wanted["key"], "name": wanted["key"], "type": "city",
                                        "country": "USA", "unlocode": "USXXX", "source": "phase1"})
    repo.add_master_location("FORT LAUDERDALE, FL", "USA", "city", name="Fort Lauderdale",
                             unlocode="USFLL", source="phase1")
    repo.commit()
    report = fix_locations.run(conn, dry_run=False)
    assert len(report.flags) == len(fix_locations.FLAGS)
    for wanted in fix_locations.FLAGS:
        assert place(conn, wanted["key"])["is_seaport"] == 1, wanted["key"]
    assert place(conn, "FORT LAUDERDALE, FL")["is_seaport"] == 0
    assert any("FORT LAUDERDALE" in line for line in report.review)      # still on the list to read
