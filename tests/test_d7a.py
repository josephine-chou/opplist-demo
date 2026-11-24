"""Batch D7a (2026-11-13, schema v50): HS code and cargo value on each cargo
line, the suggestion kinds D7 needs (tag, edit), the UN/LOCODE names read
right, and the official code lists for the team site.

1. v50: enquiry_cargo gains hs_code, cargo_value, cargo_value_cur; the
   suggestion table is rebuilt (kind tag and edit), FX5's tag rows move to
   kind 'tag', a plain note stays;
2. HS code and value: saved on the Cargo card, refused when malformed, on
   cases/{OPP}.cargo[]; read at registration for one line, and for two lines
   only each line's own; hs-codes dry run and apply;
3. coordinates._decode reads a UTF-8 and a cp1252 release;
4. official-places on a tiny invented release and airports file.

Every company, person, mail, place, code and amount here is invented; cases
from OPP-9900, domains .test, the country codes QY and QZ.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sqlite3

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import __main__ as cli
from opplist import (cargo, coordinates, db, firebase_sync, hs_codes, intake, mail_archive, official_places,
                     outlook_bridge, parsing, schema_migrations, suggestions)
from opplist.web import create_app

AGENT = ("ola@brindlefreight.test", "Ola Brindle")


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "d7a.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    db.set_setting(conn, "special_handling", ["DG", "Non-stackable"])
    agent = add_party(repo, "Brindle Freight", country="Chile")
    repo.insert("party_domain", {"domain": "brindlefreight.test", "party_id": agent})
    ids = {"agent": agent, "path": path}
    for n in range(9900, 9905):
        ids[n] = add_enquiry(repo, "OPP-{}".format(n), agent, received="2027-01-05", service_mode="LCL",
                             origin_country="USA", dest_country="Chile", scope="In",
                             entry_id="EID-REG-{}".format(n), conversation_id="CONV-{}".format(n),
                             source_subject="LCL RATE // OAKVALE - PELLAN OPP-{}".format(n),
                             thread_key=parsing.subject_thread_key("LCL RATE // OAKVALE - PELLAN OPP-{}".format(n)))
    repo.commit()
    app = create_app(path)
    app.config["TESTING"] = True
    yield {"conn": conn, "repo": repo, "ids": ids, "client": app.test_client(), "path": path}
    conn.close()


def mail(body, subject="LCL RATE // NINGBO - SAN ANTONIO", key="1", sender=AGENT, at="2027-01-05 09:00",
         conversation="CONV-NEW"):
    return outlook_bridge.MailInfo(subject=subject, received_on=at[:10], received_at=at, sent_at=at,
                                   sender_address=sender[0], sender_name=sender[1], to_addresses=["desk@jojofreight.example"],
                                   body_excerpt=body, entry_id="EID-" + key, conversation_id=conversation,
                                   message_id="<{}@d7a.test>".format(key), folder_path="\\Mailbox\\Inbox")


# ------------------------------------------------------------ 1. v50 --
def _back_to_v49(path):
    """The database as v49 had it: suggestion's kind without tag and edit,
    enquiry_cargo without the three columns."""
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys = OFF")
    for (name,) in raw.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall():
        raw.execute("DROP VIEW {}".format(name))
    sql = raw.execute("SELECT sql FROM sqlite_master WHERE name = 'suggestion'").fetchone()[0]
    assert "'case_number','tag','edit'" in sql
    raw.execute(sql.replace("'case_number','tag','edit'", "'case_number'").replace(
        "CREATE TABLE suggestion", "CREATE TABLE suggestion_v49", 1))
    raw.execute("INSERT INTO suggestion_v49 SELECT * FROM suggestion")
    raw.execute("DROP TABLE suggestion")
    raw.execute("ALTER TABLE suggestion_v49 RENAME TO suggestion")
    raw.execute("CREATE INDEX ix_suggestion_case ON suggestion (enquiry_id, state)")
    sql = re.sub(r"--[^\n]*", "", raw.execute("SELECT sql FROM sqlite_master WHERE name = 'enquiry_cargo'").fetchone()[0])
    sql = re.sub(r",\s*hs_code[^,]*,\s*cargo_value[^,]*,\s*cargo_value_cur\s+TEXT", "", sql)
    columns = [r[1] for r in raw.execute("PRAGMA table_info(enquiry_cargo)")
               if r[1] not in ("hs_code", "cargo_value", "cargo_value_cur")]
    raw.execute(sql.replace("CREATE TABLE enquiry_cargo", "CREATE TABLE enquiry_cargo_v49", 1))
    raw.execute("INSERT INTO enquiry_cargo_v49 ({c}) SELECT {c} FROM enquiry_cargo".format(c=", ".join(columns)))
    raw.execute("DROP TABLE enquiry_cargo")
    raw.execute("ALTER TABLE enquiry_cargo_v49 RENAME TO enquiry_cargo")
    raw.execute("UPDATE setting SET value = '49' WHERE key = 'schema_version'")
    raw.commit()
    raw.close()


def test_v50_moves_a_tag_row_keeps_a_plain_note_and_every_row(world):
    w = world
    repo, conn = w["repo"], w["conn"]
    common = {"opp_no": "OPP-9900", "enquiry_id": w["ids"][9900], "evidence_sender": "Ola Brindle <ola@brindlefreight.test>",
              "evidence_date": "2027-01-06", "evidence_quote": "The crates are non-stackable.", "source": "scan"}
    tag = repo.insert("suggestion", dict(common, kind="note", note="Add special handling: Non-stackable",
                                         state="dismissed", decided_at="2027-01-07 10:00", decided_as="dismissed",
                                         created_at="2027-01-06 08:15:00"))
    plain = repo.insert("suggestion", dict(common, kind="note", note="Thanks, noted.", state="pending",
                                           created_at="2027-01-06 08:15:00"))
    repo.add_cargo_line(w["ids"][9900], {"pieces": 3, "pack_word": "crates", "weight_value": 120, "weight_unit": "kg"})
    repo.commit()
    conn.close()
    _back_to_v49(w["path"])
    notes = []
    conn = db.open_db(w["path"], upgrade_notes=notes)
    try:
        assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION >= 50
        assert {"enquiry_cargo.hs_code added", "enquiry_cargo.cargo_value added",
                "enquiry_cargo.cargo_value_cur added"} <= set(notes)
        assert any(n.startswith("suggestion rebuilt (2 rows before, 2 after") for n in notes)
        assert "tag suggestions moved from note rows: 1: OPP-9900 Non-stackable (dismissed)" in notes
        moved = conn.execute("SELECT * FROM suggestion WHERE id = ?", (tag,)).fetchone()
        assert (moved["kind"], moved["note"], moved["state"], moved["decided_at"], moved["created_at"],
                moved["evidence_quote"]) == ("tag", "Non-stackable", "dismissed", "2027-01-07 10:00",
                                             "2027-01-06 08:15:00", "The crates are non-stackable.")
        kept = conn.execute("SELECT kind, note, state FROM suggestion WHERE id = ?", (plain,)).fetchone()
        assert tuple(kept) == ("note", "Thanks, noted.", "pending")
        assert conn.execute("SELECT COUNT(*) FROM enquiry_cargo").fetchone()[0] == 1
        assert conn.execute("SELECT source FROM change_log WHERE table_name = 'suggestion' AND row_id = ? "
                            "AND field = 'kind'", (tag,)).fetchone()[0] == "migration"
        repo = db.Repository(conn, source="test")
        repo.insert("suggestion", dict(common, kind="edit", note="a site change met hers"))      # allowed now
        assert schema_migrations._to_v50(conn)[-1].startswith("tag suggestions moved from note rows: 0")
    finally:
        conn.close()


def test_the_non_stackable_suggestion_end_to_end_with_kind_tag(world, tmp_path, monkeypatch):
    w = world
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    later = mail("Hi,\nThe 3 crates are non stack.\nOla", subject="RE: LCL RATE // OAKVALE - PELLAN OPP-9901",
                 key="later", at="2027-01-06 10:00", conversation="CONV-9901")
    store = mail_archive.open_archive(archive)
    mail_archive.store(store, [mail_archive.record_from_info(later)])
    store.commit()
    store.close()
    with mail_archive.ArchiveSearch(archive) as search:
        got = suggestions.scan_non_stackable(db.Repository(w["conn"], source="scan"), search)
    assert got == [("OPP-9901", "The 3 crates are non stack.", "Ola Brindle <ola@brindlefreight.test>", "2027-01-06")]
    row = w["conn"].execute("SELECT * FROM suggestion WHERE opp_no = 'OPP-9901'").fetchone()
    assert (row["kind"], row["note"], suggestions.describe(row)) == ("tag", "Non-stackable", "Add Non-stackable")
    case = w["ids"][9901]
    page = w["client"].get("/enquiries/{}".format(case)).data.decode()
    card = page[page.index('id="sg-{}"'.format(row["id"])):]
    card = card[:card.index("</tr>")]
    assert "Add Non-stackable" in card and "data-sg-edit" not in card
    with pytest.raises(ValueError):
        suggestions.record_as(w["repo"], row["id"], "note")
    w["client"].post("/suggestions/{}/accept".format(row["id"]), data={"next": "/"})
    assert w["conn"].execute("SELECT special_handling FROM enquiry WHERE id = ?", (case,)).fetchone()[0] == "Non-stackable"
    with mail_archive.ArchiveSearch(archive) as search:
        assert suggestions.scan_non_stackable(db.Repository(w["conn"], source="scan"), search) == []


# ------------------------------------------------- 2. HS code and value --
def test_the_typed_values_are_checked():
    assert cargo.hs_code_of("2507.00.10, 8471.30.0100") == ("2507.00.10, 8471.30.0100", None)
    assert cargo.hs_code_of("8471 30 01") == ("8471 30 01", None)
    for bad in ("25A7.00", "123", "12345678901", "2507-00"):
        assert cargo.hs_code_of(bad)[1] == cargo.HS_REFUSED, bad
    assert cargo.value_of("12480")[:2] == (12480.0, "USD")
    assert cargo.value_of("EUR 9.750,50")[:2] == (9750.5, "EUR")
    assert cargo.value_of("18,400 gbp")[:2] == (18400.0, "GBP")
    assert cargo.value_of("twelve")[2] == cargo.VALUE_REFUSED
    assert cargo.value_text(12480.0, "USD") == "USD 12,480" and cargo.value_text(9750.5, "EUR") == "EUR 9,750.50"


def test_the_cargo_card_saves_them_refuses_bad_ones_and_the_site_shows_them(world):
    w = world
    case = w["ids"][9902]
    back = w["client"].post("/enquiries/{}/cargo".format(case), data={
        "action": "add", "pieces": "4", "pack_unit": "", "weight_value": "300", "weight_unit": "kg",
        "commodity": "Brass fittings", "hs_code": "7412.20, 8481.80.9000", "cargo_value": "9,000"},
        follow_redirects=True).data.decode()
    line = w["conn"].execute("SELECT * FROM enquiry_cargo WHERE enquiry_id = ?", (case,)).fetchone()
    assert (line["hs_code"], line["cargo_value"], line["cargo_value_cur"]) == ("7412.20, 8481.80.9000", 9000.0, "USD")
    assert "HS 7412.20, 8481.80.9000 · USD 9,000" in back
    assert 'name="hs_code" data-f="hs_code" value="7412.20, 8481.80.9000"' in back
    assert 'name="cargo_value" data-f="cargo_value" value="USD 9,000"' in back
    before = w["conn"].execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    page = w["client"].post("/enquiries/{}/cargo".format(case), data={
        "action": "save", "line_id": line["id"], "pieces": "4", "commodity": "Brass fittings",
        "hs_code": "74A2", "cargo_value": "lots"}, follow_redirects=True).data.decode()
    assert "Cargo line not saved" in page and "4 to 10 digits each" in page
    assert w["conn"].execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == before
    item = firebase_sync._cargo(w["conn"], {"id": case})[0]
    assert (item["hs"], item["value"]) == ("7412.20, 8481.80.9000", "USD 9,000")
    w["repo"].edit_cargo_line(line["id"], {"hs_code": None, "cargo_value": None, "cargo_value_cur": None})
    w["repo"].commit()
    item = firebase_sync._cargo(w["conn"], {"id": case})[0]
    assert "hs" not in item and "value" not in item


def test_one_packing_line_gets_the_mails_hs_code_and_value(world):
    """In place of test_cargo_lines' singapore test: the code
    goes to hs_code, the commodity stays as written."""
    body = ("Hi,\nPlease quote LCL Ningbo to San Antonio.\n8 pallets 120 x 100 x 100 cm, 4500 kg\n"
            "Commodity: printed books\nHS code: 4901.99\nInvoice value: USD 18,400\nThanks\nOla")
    draft = intake.build(world["conn"], mail(body))
    assert len(draft.cargo_lines) == 1
    line = draft.cargo_lines[0]
    assert (line["pieces"], line["pack_word"], line["weight_kg"], line["commodity"]) == (8, "pallets", 4500, "printed books")
    assert (line["length_cm"], line["width_cm"], line["height_cm"]) == (120, 100, 100)
    assert (line["hs_code"], line["cargo_value"], line["cargo_value_cur"]) == ("4901.99", 18400.0, "USD")
    assert cargo.summarise(draft.cargo_lines, "LCL")["lines"][0]["volume_cbm"] == 9.6


def test_two_lines_take_only_their_own_text(world):
    body = ("Hi,\nPlease quote air Ningbo to Santiago.\n"
            "4 crates 100 x 80 x 60 cm, 300 kg, HS 8481.80, cargo value USD 9,000\n"
            "2 pallets 120 x 100 x 90 cm, 500 kg\nThanks\nOla")
    draft = intake.build(world["conn"], mail(body, subject="AIR RATE // NGB - SCL"))
    first, second = draft.cargo_lines
    assert (first["hs_code"], first["cargo_value"]) == ("8481.80", 9000.0)
    assert (second.get("hs_code"), second.get("cargo_value")) == (None, None)       # never guessed


def test_no_line_from_the_reader_gives_the_register_pages_line_and_a_charge_is_never_a_value(world):
    body = "Hi,\nPlease quote 1x40HQ Ningbo to San Antonio, furniture, HS 9403.60, FOB value EUR 22.000.\nThanks"
    draft = intake.build(world["conn"], mail(body, subject="FCL RATE // NGB - SAI // 1X40HQ"))
    assert draft.cargo_lines == [] and draft.line_extra == {"hs_code": "9403.60", "cargo_value": 22000.0,
                                                            "cargo_value_cur": "EUR"}
    assert intake.goods_value_in(["FOB charges: USD 150 per shipment"])[0] is None
    assert intake.goods_value_in(["Ocean freight USD 2,500/40HC"])[0] is None
    assert intake.goods_value_in(["Declared value: 1.3 million USD"])[:2] == (1300000.0, "USD")
    assert parsing.hs_codes_in(["H.S. 8471.30.0100 and 8473.30", "NCM Code: 70139900"])[0] == \
        "8471.30.0100, 8473.30, 70139900"
    assert parsing.hs_codes_in(["Please send the HS code"]) == ("", "")


def test_hs_codes_dry_run_and_apply_on_a_line(world, tmp_path):
    w = world
    repo = w["repo"]
    moved = repo.add_cargo_line(w["ids"][9903], {"pieces": 2, "weight_value": 40, "weight_unit": "kg",
                                                 "commodity": "Grey clay (HS 2507.00.10)"})
    has = repo.add_cargo_line(w["ids"][9903], {"pieces": 1, "commodity": "Kiln shelf (HS 6903.90)", "hs_code": "6903.90"})
    left = repo.add_cargo_line(w["ids"][9904], {"pieces": 1, "commodity": "Battery rack (HS codes: 8419899890 Rack: 7308909000)"})
    repo.commit()
    out = tmp_path / "proposed-hs-codes.csv"
    assert cli.main(["hs-codes", "--db", str(w["path"]), "--out", str(out)]) == 0
    with out.open(encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    assert rows == [list(hs_codes.HEADERS), ["OPP-9903", "1", "Grey clay (HS 2507.00.10)", "2507.00.10", "Grey clay"]]
    assert w["conn"].execute("SELECT hs_code FROM enquiry_cargo WHERE id = ?", (moved,)).fetchone()[0] is None
    report = hs_codes.find(w["conn"])
    assert [left_row[0] for left_row in report.left] == ["OPP-9904"]          # more than the codes: left for her
    assert [p.opp for p in report.proposals] == ["OPP-9903"]
    assert cli.main(["hs-codes", "--db", str(w["path"]), "--apply", "--no-backup"]) == 0
    conn = db.open_db(w["path"])
    try:
        line = conn.execute("SELECT hs_code, commodity FROM enquiry_cargo WHERE id = ?", (moved,)).fetchone()
        assert tuple(line) == ("2507.00.10", "Grey clay")
        assert conn.execute("SELECT commodity FROM enquiry_cargo WHERE id = ?", (has,)).fetchone()[0] == "Kiln shelf (HS 6903.90)"
        assert conn.execute("SELECT commodity FROM enquiry_cargo WHERE id = ?", (left,)).fetchone()[0].startswith("Battery rack (HS")
        assert conn.execute("SELECT source FROM change_log WHERE table_name = 'enquiry_cargo' AND row_id = ? "
                            "AND field = 'hs_code'", (moved,)).fetchone()[0] == "hs-codes"
        # the case's commodity follows its lines
        assert conn.execute("SELECT commodity FROM enquiry WHERE id = ?", (w["ids"][9903],)).fetchone()[0] == \
            "Grey clay; Kiln shelf (HS 6903.90)"
    finally:
        conn.close()


# --------------------------------------------------------- 3. the decoder --
def test_the_decoder_reads_utf8_and_cp1252():
    assert coordinates._decode("Pórtovel,São Vento".encode("utf-8")) == "Pórtovel,São Vento"
    assert coordinates._decode("Pórtovel,São Vento".encode("cp1252")) == "Pórtovel,São Vento"
    assert coordinates._decode(b"\xef\xbb\xbfQuellen") == "Quellen"                   # a BOM


# -------------------------------------------------- 4. official-places --
UNLOCODE = [
    ",QZ,,.QUAZARIA,,,,,,,,",
    ",QZ,PVL,Pórtovel,Portovel,NW,1--4----,AI,2101,,1230N 04510W,",
    ",QZ,BRK,Brackmoor,Brackmoor,,--3-----,RL,2101,,,",
    "X,QZ,OLD,Oldhaven,Oldhaven,,1-------,AI,2101,,,",
    ",QZ,GON,Gonewick,Gonewick,,1-------,XX,2101,,,",
    ",QZ,ANV,Anvel Bay,Anvel Bay,,1-3-5-7B,AF,2101,,0105S 00230E,",
]
AIRPORTS = [
    "id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,continent,iso_country,iso_region,municipality,"
    "scheduled_service,gps_code,iata_code,local_code,home_link,wikipedia_link,keywords",
    "1,QZPV,large_airport,Portovel International,12.49671,-45.20113,30,SA,QZ,QZ-NW,Pórtovel,yes,QZPV,PVQ,,,,",
    "2,QYQF,medium_airport,Quellen Field,3.25,10.5,100,AF,QY,QY-01,Quellen,yes,QYQF,QYA,,,,",
    "3,QZSM,small_airport,Smallmoor Strip,1,1,0,SA,QZ,QZ-NW,Smallmoor,no,QZSM,SMQ,,,,",
    "4,QZNI,medium_airport,No Code Field,2,2,0,SA,QZ,QZ-NW,Nowhere,no,QZNI,,,,,",
]


def test_official_places_writes_the_format_exactly(world, tmp_path):
    release = tmp_path / "unlocode" / "release"
    (release / "csv").mkdir(parents=True)
    (release / "csv" / "UNLOCODE CodeListPart1.csv").write_bytes("\n".join(UNLOCODE).encode("utf-8"))
    (release / "UNLOCODE.ttl").write_text("@prefix x: <urn:x> .\n# UN/LOCODE Version 2099-1 test\n", encoding="utf-8")
    airports = tmp_path / "airports.csv"
    airports.write_text("\n".join(AIRPORTS), encoding="utf-8")
    out = tmp_path / "official"
    out.mkdir()
    (out / "README.txt").write_text("kept", encoding="utf-8")
    args = ["official-places", "--db", str(world["path"]), "--unlocode", str(tmp_path / "unlocode"),
            "--airports", str(airports), "--out-dir", str(out)]
    assert cli.main(args) == 0
    assert sorted(p.name for p in out.iterdir()) == ["README.txt"]                  # a dry run writes nothing
    assert cli.main(args + ["--apply"]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["QY.json", "QZ.json", "README.txt", "index.json"]
    assert (out / "QZ.json").read_bytes() == (
        '{"c":"QZ","loc":[["ANV","Anvel Bay","","ptofb",-1.0833,2.5],["BRK","Brackmoor","","t",null,null],'
        '["PVL","Pórtovel","NW","pa",12.5,-45.1667]],"air":[["PVQ","Portovel International","Pórtovel",12.4967,'
        '-45.2011]]}').encode("utf-8")
    assert json.loads((out / "QY.json").read_text(encoding="utf-8")) == \
        {"c": "QY", "loc": [], "air": [["QYA", "Quellen Field", "Quellen", 3.25, 10.5]]}
    assert (out / "index.json").read_bytes() == (
        '{{"v":"2099-1","built":"{}","countries":{{"QY":1,"QZ":4}}}}'.format(dt.date.today().isoformat())).encode("utf-8")
    conn = db.open_db(world["path"])
    try:
        assert db.get_setting(conn, "official_unlocode_path") == str((tmp_path / "unlocode").resolve())
        assert db.get_setting(conn, "official_airports_path") == str(airports.resolve())
    finally:
        conn.close()
    assert official_places.flags_of("--34-6--") == "tam" and official_places.flags_of("--------") == ""
