"""Cargo lines (schema v28, 2026-09-24): one consignment per
line, the derived numbers, the totals written back to the case, the
split, the export contract, two numbered consignments, one LCL lot and
two shipments with crates and pallets as fixtures, and 'Add another subject' on the Request card."""

from __future__ import annotations

import openpyxl
import pytest

from conftest import add_enquiry, add_party, form_nesting, seed_reference
from opplist import cargo, case_mail, db, detect_followups, export_csv, export_xlsx, followup, intake, \
    mail_archive, outlook_bridge, parsing, paths, schema_migrations
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app

TODAY = "2026-09-24"
NUMBERED = "air_two_consignments_numbered.json"
LCL_LOT = "lcl_pallets_one_consignment.json"
TWO_SHIPMENTS = "air_two_shipments_crates_and_pallets.json"
SUBJECT = "CAN-ATL air enquiry DAP (2 shpts) 20260923"
LINE_1 = {"label": "1", "pieces": 6, "pack_word": "ptls", "dims_text": "110 x 70 x 28 cm",
          "length_cm": 110, "width_cm": 70, "height_cm": 28, "weight_kg": 1480, "cbm": 1.3,
          "deliver_to": "905 E. Linden Avenue, Marietta, GA 30060", "commodity": "General cargo", "source": "mail"}
LINE_2 = dict(LINE_1, label="2", pieces=12, weight_kg=2960, cbm=2.6,
              deliver_to="2210 HOLLOW CREEK DR./ ROME, GA 30161")


def fixture(name):
    return outlook_bridge.read_fixture(paths.FIXTURES_DIR / name)


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "cargo.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    for name in ("China", "South Korea"):
        if conn.execute("SELECT 1 FROM country WHERE name = ?", (name,)).fetchone() is None:
            repo.insert("country", {"name": name, "region": "Asia"})
    add_party(repo, "Pearl River Cargo", country="China")
    air = add_enquiry(repo, "OPP-1450", tolvex, received="2026-09-23", service_mode="Air", incoterm="DAP",
                      origin_country="China", dest_country="USA", dest_alt="905 E. Linden Avenue, Marietta, GA 30060",
                      commodity="General cargo", entry_id="ENTRY-CANATL", conversation_id="CONV-CANATL",
                      source_subject=SUBJECT, thread_key=parsing.subject_thread_key(SUBJECT))
    lcl = add_enquiry(repo, "OPP-1451", tolvex, received="2026-09-23", service_mode="LCL", incoterm="DDU",
                      origin_country="South Korea", dest_country="USA", source_subject="LCL BUSAN TO HOUSTON - DDU TX 77471 USA",
                      thread_key=parsing.subject_thread_key("LCL BUSAN TO HOUSTON - DDU TX 77471 USA"))
    other = add_enquiry(repo, "OPP-1452", tolvex, received="2026-09-20", service_mode="Air",
                        source_subject="RATE REQUEST // IAH - LIM", thread_key=parsing.subject_thread_key("RATE REQUEST // IAH - LIM"))
    db.set_setting(conn, "next_opp_number", 1453)
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, {"air": air, "lcl": lcl, "other": other, "tolvex": tolvex}


def info(subject, at, sender, name, body, conversation, internal=False, message_id=""):
    return MailInfo(subject=subject, received_on=at[:10], received_at=at, entry_id="EID-" + message_id.strip("<>"),
                    conversation_id=conversation, conversation_topic=subject, sender_name=name, sender_address=sender,
                    sender_internal=internal, body_excerpt=body, folder_path="\\Inbox", message_id=message_id)


THREAD = [
    info("DAP Inquiry CAN-ATL revised", "2026-09-24 09:00", "quote3@pearlriver-cargo.example", "Wendy Luo",
         "Hello Derek\n\nThe second lot got lighter, a fresh offer on the new figures please.\n\nWendy",
         "CONV-REVISED", message_id="<r1@x>"),
    info("RE: DAP Inquiry CAN-ATL revised", "2026-09-24 11:00", "DerekChan@jojofreight.example", "JJC-DEREK CHAN",
         "Hi Wendy,\n\nNew figures below. Rates valid until Sep 30.\n\nDerek", "CONV-REVISED",
         internal=True, message_id="<r2@x>"),
    info("Something unrelated", "2026-09-24 12:00", "x@y.example", "X", "hello", "CONV-OTHER", message_id="<o1@x>"),
]


@pytest.fixture
def archive(tmp_path, monkeypatch):
    path = tmp_path / "archive" / "mail.db"
    path.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", path)
    conn = mail_archive.open_archive(path)
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in THREAD])
    mail_archive.set_meta(conn, "last_sync_at", "2026-09-24 08:15:00")
    conn.commit()
    conn.close()
    return path


def case_row(path, enquiry_id):
    conn = db.open_db(path)
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    conn.close()
    return row


# --------------------------------------------------------- derived values ----
def test_the_derived_values_and_their_labels():
    dims = cargo.derive({"pieces": 7, "length_cm": 120, "width_cm": 110, "height_cm": 90, "weight_kg": 3850}, "LCL")
    assert dims["volume_cbm"] == 8.316 and dims["cbm_from"] == "dims"
    assert dims["cbm_formula"] == "7 x 120 x 110 x 90 cm = 8.32 cbm"
    assert round(dims["volumetric_kg"], 1) == 1386 and dims["chargeable_kg"] == 3850
    assert dims["revenue_ton"] == 8.316 and dims["revenue_formula"] == "max(8.32 cbm, 3850 kg / 1000 = 3.85 t) = 8.32"
    # a stated cbm is kept over the dims; a note when they are more than 10 % apart
    stated = cargo.derive(LINE_1, "Air")
    assert stated["volume_cbm"] == 1.3 and stated["cbm_from"] == "stated" and stated["cbm_note"] == ""
    assert stated["chargeable_kg"] == 1480 and stated["chargeable_formula"].startswith("max(1480 kg, 1.3 cbm x 166.67 = 216.7 kg)")
    far = cargo.derive(dict(LINE_1, cbm=1.8), "Air")
    assert far["volume_cbm"] == 1.8 and far["cbm_note"] == "stated 1.8 cbm, dims give 1.29 cbm"
    # a light line is charged on its volume
    light = cargo.derive({"pieces": 2, "cbm": 3, "weight_kg": 100}, "Air")
    assert round(light["chargeable_kg"], 1) == 500 and light["revenue_ton"] == 3
    assert cargo.line_text(LINE_1) == "6 ptls 110 x 70 x 28 cm, 1480 kg, 1.3 cbm"
    summary = cargo.summarise([LINE_1, LINE_2], "Air")
    assert summary["totals"]["chargeable_kg"] == 4440 and round(summary["totals"]["cbm"], 2) == 3.9
    # v33: qty_raw is the text as received, never built from the lines
    assert cargo.enquiry_fields(summary) == {"air_weight_kg": 4440.0, "lcl_cbm": 3.9}
    assert cargo.enquiry_fields(cargo.summarise([LINE_1, LINE_2], "LCL"))["air_weight_kg"] == 4440.0   # weight, not chargeable
    assert cargo.enquiry_fields(cargo.summarise([], "Air")) == {}


# ---------------------------------------------------------- the totals -----
def test_the_totals_are_written_back_on_add_edit_and_void(world):
    client, path, ids = world
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    first = repo.add_cargo_line(ids["air"], LINE_1)
    second = repo.add_cargo_line(ids["air"], LINE_2)
    repo.commit()
    row = conn.execute("SELECT air_weight_kg, lcl_cbm, gross_kg, volume_cbm, qty_raw FROM enquiry WHERE id = ?",
                       (ids["air"],)).fetchone()
    # v33: an Air case's air_weight_kg is the chargeable weight, lcl_cbm is empty
    # off LCL, gross and volume are their own columns, qty_raw stays as received
    assert (row["air_weight_kg"], row["lcl_cbm"], row["gross_kg"], row["volume_cbm"]) == (4440.0, None, 4440.0, 3.9)
    assert row["qty_raw"] is None
    assert [r["seq"] for r in conn.execute("SELECT seq FROM enquiry_cargo ORDER BY id")] == [1, 2]
    # edit: the totals follow
    repo.edit_cargo_line(second, dict(LINE_2, weight_kg=3050))
    repo.commit()
    assert conn.execute("SELECT air_weight_kg FROM enquiry WHERE id = ?", (ids["air"],)).fetchone()[0] == 4530.0
    # remove = strike out, never delete; put back
    repo.void_cargo_line(first)
    repo.commit()
    row = conn.execute("SELECT air_weight_kg, volume_cbm FROM enquiry WHERE id = ?", (ids["air"],)).fetchone()
    assert (row["air_weight_kg"], row["volume_cbm"]) == (3050.0, 2.6)
    assert conn.execute("SELECT COUNT(*) FROM enquiry_cargo").fetchone()[0] == 2
    repo.void_cargo_line(first, voided=False)
    repo.commit()
    assert conn.execute("SELECT air_weight_kg FROM enquiry WHERE id = ?", (ids["air"],)).fetchone()[0] == 4530.0
    # every write is logged
    logged = conn.execute("SELECT field, COUNT(*) AS n FROM change_log WHERE table_name = 'enquiry_cargo' "
                          "GROUP BY field ORDER BY field").fetchall()
    assert {r["field"]: r["n"] for r in logged} == {"(insert)": 2, "voided": 2, "weight_kg": 1}
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'enquiry' AND field = 'air_weight_kg'").fetchone()[0] >= 4
    # an LCL case sums the weight, not the chargeable weight
    repo.add_cargo_line(ids["lcl"], {"pieces": 7, "pack_word": "pallets", "length_cm": 120, "width_cm": 110,
                                     "height_cm": 90, "weight_kg": 3850})
    repo.commit()
    row = conn.execute("SELECT air_weight_kg, lcl_cbm, gross_kg FROM enquiry WHERE id = ?", (ids["lcl"],)).fetchone()
    assert (row["air_weight_kg"], row["lcl_cbm"], row["gross_kg"]) == (None, 8.316, 3850.0)
    conn.close()


# test_saving_the_case_form_keeps_the_totals_and_the_routes_write_lines: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


# --------------------------------------------------------------- split -----
def test_split_into_its_own_case_is_gone(world):
    """v33: a destination is a route of the OPP, not a case of its own -
    no Split button, and the action is refused."""
    client, path, ids = world
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    repo.add_cargo_line(ids["air"], LINE_1)
    second = repo.add_cargo_line(ids["air"], LINE_2)
    repo.commit()
    conn.close()
    body = client.get("/enquiries/{}".format(ids["air"])).data.decode("utf-8")
    assert "Split into its own case" not in body and 'value="split"' not in body
    page = client.post("/enquiries/{}/cargo".format(ids["air"]), data={"action": "split", "line_id": second})
    assert page.status_code == 400
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM enquiry").fetchone()[0] == 3
    conn.close()


# ------------------------------------------------------------- export -----
def test_the_export_keeps_41_columns_and_carries_the_totals(world, tmp_path):
    client, path, ids = world
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    repo.add_cargo_line(ids["air"], LINE_1)
    repo.add_cargo_line(ids["air"], LINE_2)
    repo.commit()
    book = export_xlsx.export(conn, tmp_path / "out.xlsx")
    csv_path = export_csv.export_opp_index(conn, tmp_path / "opp-index.csv")
    conn.close()
    wb = openpyxl.load_workbook(book)
    try:
        sheet = wb["Opportunities"]
        headers = [c.value for c in sheet[1]]
        assert len(headers) == 41 and headers == export_xlsx.OPPORTUNITY_COLUMNS
        rows = {r[0].value: r for r in sheet.iter_rows(min_row=2)}
        row = rows["OPP-1450"]
        assert row[headers.index("Air Weight (kgs)")].value == 4440         # the chargeable weight
        assert row[headers.index("LCL Volume (cbm)")].value is None        # LCL cases only (v33)
        assert row[headers.index("Source Qty (raw)")].value is None         # the text as received
    finally:
        wb.close()
    first = csv_path.read_bytes().split(b"\r\n")[0]
    assert first.count(b",") == 7


# ---------------------------------------------------------- the parser -----
def test_the_formats_of_this_weeks_mails_parse():
    pk = parsing.parse_packing(["CARGO DETAILS 7 pallets (120x110x90cm)", "Estimated weight: 3,850kg"])
    assert [(i.pieces, i.word, i.dims_text, i.weight_text) for i in pk.items] == [(7, "pallets", "120 x 110 x 90 cm", "3850 kg")]
    assert pk.cbm == 8.316 and pk.cbm_formula == "7 x 120 x 110 x 90 cm = 8.32 cbm"
    pk = parsing.parse_packing(["piece/weight/vol:6Ptls/1480KGSKG/1.3CBM  110*70*28(CM)*6"])
    assert [(i.pieces, i.word, i.dims_text, i.weight_kg) for i in pk.items] == [(6, "ptls", "110 x 70 x 28 cm", 1480.0)]
    # the count after the triple counts only when nothing else counts the pieces
    pk = parsing.parse_packing(["110*70*28(CM)*12", "weight 2960KGSKGSKG"])
    assert [(i.pieces, i.dims_text, i.weight_kg) for i in pk.items] == [(12, "110 x 70 x 28 cm", 2960.0)]
    pk = parsing.parse_packing(["4 Ptls 100x80x90cm x 2, 640 kg"])
    assert [(i.pieces, i.word) for i in pk.items] == [(4, "ptls")]
    assert parsing._WEIGHT_LABEL.match("Estimated weight: 3,850kg") and parsing._WEIGHT_LABEL.match("Approx. weight: 12 kg")
    assert parsing._WEIGHT_LABEL.match("est. weight: 3 kg") and not parsing._WEIGHT_LABEL.match("Weighted: 3")
    # 'CRATE 1 :' item heads and the 'Total' line, as in the two-shipment mail
    pk = parsing.parse_packing(["CRATE 1 : DIM : 60 x 45 x 80” INCH, GW : 820 KG", "CRATE 2 : DIM : 60 x 45 x 80” INCH , GW: 390 KG",
                                "TOTAL:  02 CRATE / total GW : 1,210 KG"])
    assert [(i.pieces, i.word, i.weight_kg) for i in pk.items] == [(1, "crate", 820.0), (1, "crate", 390.0)]
    assert (pk.total_pieces, pk.total_kg, pk.pieces, pk.actual_kg) == (2, 1210.0, 2, 1210.0)
    # what was there keeps parsing the same way
    assert parsing.parse_body_qty("Gross Weight: 161.00 kg\n").qty_raw == "Air:161"
    assert parsing.parse_body_qty("Weight 1,250 kgs\n").qty_raw == "Air:1250"
    assert parsing.parse_packing(["2 pallets 120x80x100cm, 300 kg"]).qty_raw == "2 pallets 120 x 80 x 100 cm, 300 kg"
    assert parsing.dims_from_text("120 x 100 x 100 cm") == ((120.0, 100.0, 100.0), "120 x 100 x 100 cm")
    assert parsing.dims_from_text("120x100x100") == (None, "120 x 100 x 100")


# test_the_lcl_mail_gives_one_line_with_the_cbm pinned '(HS 6907)' in the commodity; D7a (v50)
# moved the code to hs_code. Its test is in tests/test_d7a.py
# (test_one_packing_line_gets_the_mails_hs_code_and_value).


def test_the_numbered_mail_gives_two_lines_with_their_totals(world):
    client, path, ids = world
    conn = db.open_db(path)
    draft = intake.build(conn, fixture(NUMBERED))
    conn.close()
    assert draft.values["service_mode"] == "Air" and draft.values["incoterm"] == "DAP"
    assert [(l["label"], l["pieces"], l["pack_word"], l["dims_text"], l["weight_kg"], l["cbm"], l["deliver_to"])
            for l in draft.cargo_lines] == [
        ("1", 6, "ptls", "110 x 70 x 28 cm", 1480, 1.3, "905 E. Linden Avenue, Marietta, GA 30060"),
        ("2", 12, "ptls", "110 x 70 x 28 cm", 2960, 2.6, "2210 HOLLOW CREEK DR./ ROME, GA 30161")]
    summary = cargo.summarise(draft.cargo_lines, "Air")
    assert [l["chargeable_kg"] for l in summary["lines"]] == [1480, 2960]
    assert summary["totals"]["chargeable_kg"] == 4440 and round(summary["totals"]["cbm"], 2) == 3.9
    assert (draft.values["air_weight_kg"], draft.values["lcl_cbm"]) == (4440.0, 3.9)
    assert draft.sources["air_weight_kg"] == "totals of the 2 cargo lines"
    assert "volumetric" not in (draft.values.get("qty_raw") or "")            # the text as received (v33)
    assert draft.values["dest_alt"] == "905 E. Linden Avenue, Marietta, GA 30060"   # the first block's, as before
    assert not any("different quantities" in n for n in draft.notes)


# ---------------------------------------------------- the register form -----
# (batch 13b, 2026-09-29: the register form shows the Cargo card; its test is
# test_cargo_checks.test_register_from_outlook_shows_the_card_and_saves_what_was_read,
# and the two-shipment reading is test_cargo_checks.test_two_shipments_...)
# test_the_cargo_card_renders_the_lines_and_the_totals: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


# ------------------------------------------------------------ subjects -----
def test_add_another_subject_attaches_its_key_and_counts_the_archive(world, archive):
    client, path, ids = world
    page = client.post("/enquiries/{}/subjects".format(ids["air"]),
                       data={"subject": "  Fwd:  DAP Inquiry CAN-ATL revised "}, follow_redirects=True)
    body = page.data.decode("utf-8")
    assert "Subject added (2 mails match in the archive)." in body
    conn = db.open_db(path)
    rows = conn.execute("SELECT kind, value, note, source, voided FROM enquiry_thread WHERE enquiry_id = ?",
                        (ids["air"],)).fetchall()
    assert [tuple(r) for r in rows] == [("thread_key", "DAPINQUIRYCANATLREVISED", "Fwd: DAP Inquiry CAN-ATL revised", "manual", 0)]
    assert "DAPINQUIRYCANATLREVISED" in db.thread_values(conn, ids["air"])["thread_key"]
    conn.close()
    # the Subjects block: the registered subject first, then the added one, with strike out
    block = body[body.index('id="subjects"'):body.index('id="cargo"')]
    assert block.index(SUBJECT) < block.index("Fwd: DAP Inquiry CAN-ATL revised")
    assert "thread key" not in block and "DAPINQUIRYCANATLREVISED" not in block         # no keys shown (12b)
    assert "Strike out" in block and 'name="subject" form="subject-add"' in block
    # again: already this case's; a subject with no key: refused
    page = client.post("/enquiries/{}/subjects".format(ids["air"]), data={"subject": "RE: DAP Inquiry CAN-ATL revised"},
                       follow_redirects=True)
    assert b"already this case" in page.data
    page = client.post("/enquiries/{}/subjects".format(ids["air"]), data={"subject": "  "}, follow_redirects=True)
    assert b"Paste the subject first" in page.data
    # the registered subject's own key is refused too
    page = client.post("/enquiries/{}/subjects".format(ids["air"]), data={"subject": "RE: " + SUBJECT}, follow_redirects=True)
    assert b"already this case" in page.data
    # strike out, never delete
    conn = db.open_db(path)
    thread_id = conn.execute("SELECT id FROM enquiry_thread").fetchone()[0]
    conn.close()
    page = client.post("/enquiries/{}/threads/{}".format(ids["air"], thread_id), follow_redirects=True)
    assert b"struck out" in page.data
    conn = db.open_db(path)
    assert conn.execute("SELECT voided FROM enquiry_thread WHERE id = ?", (thread_id,)).fetchone()[0] == 1
    assert db.thread_values(conn, ids["air"])["thread_key"] == {parsing.subject_thread_key(SUBJECT)}
    conn.close()


def test_a_subject_that_is_another_cases_is_refused(world):
    client, path, ids = world
    page = client.post("/enquiries/{}/subjects".format(ids["air"]),
                       data={"subject": "RE: RATE REQUEST // IAH - LIM"}, follow_redirects=True)
    body = page.data.decode("utf-8")
    assert "That subject is already OPP-1452" in body
    assert "This subject is already" in body and '/enquiries/{}"'.format(ids["other"]) in body
    # v33: no 'Same request as' - one mail is one OPP with its routes
    assert 'form="same-request-form"' not in body
    assert client.post("/enquiries/{}/same-request".format(ids["air"]), data={"of_opp_no": "OPP-1452"}).status_code == 404
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM enquiry_thread").fetchone()[0] == 0
    assert conn.execute("SELECT group_id FROM enquiry WHERE id = ?", (ids["air"],)).fetchone()[0] is None
    conn.close()

def test_from_archive_and_thread_of_see_the_added_subject(world, archive):
    client, path, ids = world
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    case = add_enquiry(repo, "OPP-1460", ids["tolvex"], received="2026-09-20", service_mode="Air",
                       source_subject="Nothing the archive knows", thread_key="NOTHINGTHEARCHIVEKNOWS")
    repo.set_status(case, "Quoted", "2026-09-24")
    repo.commit()
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    search, _ = case_mail.open_search()
    try:
        assert detect_followups.thread_of(search, row, conn) == []
        assert followup.from_archive(row, conn=conn).first_name == ""
        repo.attach_thread(case, "thread_key", "DAPINQUIRYCANATLREVISED", TODAY,
                           note="RE: DAP Inquiry CAN-ATL revised", source="manual")
        repo.commit()
        mails = detect_followups.thread_of(search, row, conn)
        assert [m.message_id for m in mails] == ["<r1@x>", "<r2@x>"]
        sources = followup.from_archive(row, conn=conn, internal=["jojofreight.example"])
        assert sources.first_name == "Wendy" and sources.valid_until is not None
        # without the connection the case's own values are all that count
        assert detect_followups.thread_of(search, row) == []
    finally:
        search.__exit__(None, None, None)
    conn.close()


# --------------------------------------------------------------- schema -----
def test_schema_v28_adds_the_table_and_nothing_else(tmp_path):
    path = tmp_path / "v28.db"
    conn = db.open_db(path)
    assert schema_migrations.CURRENT_VERSION >= 28
    assert db.get_setting(conn, "schema_version") == str(schema_migrations.CURRENT_VERSION)
    assert db._has_table(conn, "enquiry_cargo")
    # a v27 file: the table absent, the version behind
    conn.execute("DROP INDEX ix_enquiry_cargo_enquiry")
    conn.execute("DROP TABLE enquiry_cargo")
    conn.execute("UPDATE setting SET value = '27' WHERE key = 'schema_version'")
    conn.commit()
    before = {(r[0], r[1]) for r in conn.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    conn.close()
    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    after = {(r[0], r[1]) for r in conn.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    assert before <= after and after - before == {("table", "enquiry_cargo"), ("index", "ix_enquiry_cargo_enquiry")}
    assert any("enquiry_cargo table created" in n for n in notes) and db.get_setting(conn, "schema_version") == str(schema_migrations.CURRENT_VERSION)
    assert conn.execute("SELECT COUNT(*) FROM enquiry_cargo").fetchone()[0] == 0
    conn.close()
