"""Batch 14: Register from Outlook reads the attachments too.

Every file here is built by the test - a small xlsx with a header row, a
docx, a PDF with text, a PDF with no text layer, an encrypted PDF, an SDS
with section 14, an attached .msg through a mocked Outlook - and every
company, person, place pairing, number and product is invented; cases from
OPP-3801. No real attachment is used.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import attachments as att
from opplist import db, intake, outlook_bridge
from opplist.web import create_app


# ------------------------------------------------------- invented files --
def text_pdf(path, lines):
    """A one-page PDF whose text layer is `lines` (Helvetica, no package)."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 10 Tf 13 TL 50 800 Td " + " ".join("({}) Tj T*".format(esc(l)) for l in lines) + " ET"
    _pdf(path, content)


def blank_pdf(path):
    """A page with a drawn line and no text at all - what a scan reads as."""
    _pdf(path, "0 0 m 200 200 l S")


def _pdf(path, content):
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 5 0 R >> >> "
        "/Contents 4 0 R >>",
        "<< /Length {} >>\nstream\n{}\nendstream".format(len(content.encode("latin-1")), content),
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += "{} 0 obj\n{}\nendobj\n".format(n, body).encode("latin-1")
    xref = len(out)
    out += "xref\n0 {}\n0000000000 65535 f \n".format(len(objects) + 1).encode()
    out += "".join("{:010d} 00000 n \n".format(o) for o in offsets).encode()
    out += "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{}\n%%EOF\n".format(len(objects) + 1, xref).encode()
    Path(path).write_bytes(out)


def locked_pdf(path, lines):
    import pypdf
    plain = Path(str(path) + ".plain.pdf")
    text_pdf(plain, lines)
    writer = pypdf.PdfWriter(clone_from=str(plain))
    writer.encrypt(user_password="kestrel-7", algorithm="RC4-128")
    with open(path, "wb") as handle:
        writer.write(handle)


def sheet_xlsx(path, rows):
    import openpyxl
    book = openpyxl.Workbook()
    for row in rows:
        book.active.append(row)
    book.save(path)


def word_docx(path, paragraphs, table=()):
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paras = "".join("<w:p><w:r><w:t>{}</w:t></w:r></w:p>".format(p) for p in paragraphs)
    rows = "".join("<w:tr>" + "".join("<w:tc><w:p><w:r><w:t>{}</w:t></w:r></w:p></w:tc>".format(c) for c in r)
                   + "</w:tr>" for r in table)
    doc = '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{}"><w:body>{}{}</w:body></w:document>'.format(
        w, paras, "<w:tbl>{}</w:tbl>".format(rows) if rows else "")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("word/document.xml", doc)


SDS_LINES = ["SAFETY DATA SHEET", "Product name: Tarnwell Primer 40", "Section 13. Disposal considerations",
             "Dispose of contents in accordance with local rules.", "Section 14. Transport information",
             "UN number: UN1993", "UN proper shipping name: FLAMMABLE LIQUID, N.O.S. (heptane)",
             "Transport hazard class(es): 3", "Packing group: III", "Section 15. Regulatory information",
             "Not listed."]


# ------------------------------------------------------------ readers ----
def test_a_sheet_with_a_header_row_is_one_line_per_row(tmp_path):
    path = tmp_path / "Load plan.xlsx"
    sheet_xlsx(path, [["Pieces", "Packing", "Gross weight (kg)", "Volume (cbm)", "Commodity"],
                      [5, "pallets", 310, 2.4, "Ceramic tiles"], [2, "crates", 120, 0.9, "Glaze drums"]])
    f = att.read_file(path, path.name)
    assert f.read
    assert f.lines == ["Pieces: 5; Packing: pallets; Gross weight (kg): 310; Volume (cbm): 2.4; Commodity: Ceramic tiles",
                       "Pieces: 2; Packing: crates; Gross weight (kg): 120; Volume (cbm): 0.9; Commodity: Glaze drums"]
    assert f.records[0][:3] == ["Pieces: 5", "Packing: pallets", "Gross weight (kg): 310"]
    # a sheet without one is its rows as written
    assert att.rows_to_lines([["Quote no.", 4471], ["Pallets", 7]]) == ["Quote no.  4471", "Pallets  7"]


def test_a_docx_a_text_pdf_a_csv_and_a_txt(tmp_path):
    word_docx(tmp_path / "Request.docx", ["Please quote air to Santiago.", "Ready on 12 Nov."],
              table=[["Gross weight", "88 kg"], ["Pieces", "3 cartons"]])
    f = att.read_file(tmp_path / "Request.docx", "Request.docx")
    assert f.lines == ["Please quote air to Santiago.", "Ready on 12 Nov.", "Gross weight: 88 kg", "Pieces: 3 cartons"]
    text_pdf(tmp_path / "Note.pdf", ["Collection at Unit 9, Larch Way", "Total cartons: 3"])
    f = att.read_file(tmp_path / "Note.pdf", "Note.pdf")
    assert f.read and "Total cartons: 3" in f.lines
    (tmp_path / "list.csv").write_text("Pieces,Gross weight (kg)\n6,240\n", encoding="utf-8")
    assert att.read_file(tmp_path / "list.csv", "list.csv").lines == ["Pieces: 6; Gross weight (kg): 240"]
    (tmp_path / "x.txt").write_bytes("Pallets: 2\r\n\r\nWeight: 50 kg\r\n".encode("cp1252"))
    assert att.read_file(tmp_path / "x.txt", "x.txt").lines == ["Pallets: 2", "Weight: 50 kg"]


def test_what_is_not_read_says_why(tmp_path):
    blank_pdf(tmp_path / "Scan 0042.pdf")
    assert att.read_file(tmp_path / "Scan 0042.pdf", "Scan 0042.pdf").reason == att.NOT_READ_SCAN
    locked_pdf(tmp_path / "Locked.pdf", ["Pallets: 2"])
    assert att.read_file(tmp_path / "Locked.pdf", "Locked.pdf").reason == att.NOT_READ_LOCKED
    (tmp_path / "Broken.xlsx").write_bytes(b"not a workbook at all")
    assert att.read_file(tmp_path / "Broken.xlsx", "Broken.xlsx").reason.startswith("could not be opened")
    (tmp_path / "Big.pdf").write_bytes(b"%PDF")
    assert att.read_file(tmp_path / "Big.pdf", "Big.pdf", size=att.MAX_BYTES + 1).reason == att.NOT_READ_SIZE
    assert att.read_file(tmp_path / "x.png", "Photo of crate.png").reason == att.NOT_READ_IMAGE
    assert att.read_file(tmp_path / "x.zip", "Drawings.zip").reason == att.NOT_READ_KIND.format(".zip")
    assert att.is_inline_image("image001.png") and not att.is_inline_image("Photo of crate.png")


def test_an_sds_gives_the_un_number_class_and_packing_group(tmp_path):
    text_pdf(tmp_path / "SDS Tarnwell Primer.pdf", SDS_LINES)
    f = att.read_file(tmp_path / "SDS Tarnwell Primer.pdf", "SDS Tarnwell Primer.pdf")
    goods = att.dangerous_goods(f.lines)
    assert [g.text("SDS Tarnwell Primer.pdf") for g in goods] == [
        "UN1993, Class 3, PG III (from SDS Tarnwell Primer.pdf)"]
    assert goods[0].name.startswith("FLAMMABLE LIQUID")
    # the table form, one product listed for two modes, and a second product
    table = ["14. TRANSPORT INFORMATION", "IMDG 1263 PAINT", "Class 3 II", "IATA 1263 PAINT Class 3 II",
             "UN1139, COATING SOLUTION, 3, III", "15. REGULATORY INFORMATION"]
    assert [g.text() for g in att.dangerous_goods(table)] == ["UN1263, Class 3, PG II", "UN1139, Class 3, PG III"]
    assert att.dangerous_goods(["14. Transport information", "Not regulated for transport.", "15. Other"]) == []
    assert att.dangerous_goods(["UN1993 mentioned outside section 14"]) == []


# --------------------------------------------------- a mocked Outlook ----
class FakeAttachment:
    def __init__(self, name, data=b"", kind=1, size=None, content_id=""):
        self.FileName, self.DisplayName, self.Type = name, name, kind
        self._data, self.Size = data, size if size is not None else len(data)
        self.PropertyAccessor = type("PA", (), {"GetProperty": lambda _s, _tag: content_id})()

    def SaveAsFile(self, path):
        Path(path).write_bytes(self._data)


class FakeAttachments:
    def __init__(self, items):
        self._items = items
        self.Count = len(items)

    def Item(self, index):
        return self._items[index - 1]


class FakeItem:
    def __init__(self, body="", attachments=()):
        self.Body, self.Attachments, self.closed = body, FakeAttachments(list(attachments)), None

    def Close(self, how):
        self.closed = how


def test_an_attached_mail_is_opened_through_outlook_one_level_down(tmp_path, monkeypatch):
    text_pdf(tmp_path / "inner.pdf", ["Total pallets: 2", "Gross weight: 640 kg"])
    inner = FakeItem("From the shipper:\nPickup at Dock 3, 18 Wren Street\nPallets: 2",
                     [FakeAttachment("Pallet list.pdf", (tmp_path / "inner.pdf").read_bytes()),
                      FakeAttachment("Older thread.msg", b"x", kind=5)])
    opened = []

    class Session:
        def OpenSharedItem(self, path):
            opened.append(Path(path))
            return inner

    app = type("App", (), {"Session": Session()})()
    outer = FakeItem("see below", [
        FakeAttachment("image001.png", b"\x89PNG", content_id="logo@01"),
        FakeAttachment("Crate photo.jpg", b"\xff\xd8"),
        FakeAttachment("Huge scan.pdf", b"%PDF", size=att.MAX_BYTES + 5),
        FakeAttachment("RE Rates for crates", b"msg bytes", kind=outlook_bridge.OL_EMBEDDED_ITEM)])
    found = outlook_bridge._read_item_attachments(app, outer, tmp_path)
    flat = att.every_file(found)
    assert [(f.name, f.read, f.reason) for f in flat] == [
        ("Crate photo.jpg", False, att.NOT_READ_IMAGE),
        ("Huge scan.pdf", False, att.NOT_READ_SIZE),
        ("RE Rates for crates.msg", True, ""),
        ("RE Rates for crates.msg / Pallet list.pdf", True, ""),
        ("RE Rates for crates.msg / Older thread.msg", False, att.NOT_READ_DEEP)]
    assert "Pickup at Dock 3, 18 Wren Street" in flat[2].lines
    assert opened and inner.closed == outlook_bridge.OL_DISCARD


def test_read_attachments_deletes_its_copies(tmp_path, monkeypatch):
    import contextlib
    import tempfile
    made = []
    real = tempfile.mkdtemp

    def mkdtemp(prefix=""):
        made.append(Path(real(prefix=prefix, dir=tmp_path)))
        return str(made[-1])

    item = FakeItem("", [FakeAttachment("Note.txt", b"Pallets: 3\n")])
    app = type("App", (), {"Session": type("S", (), {"GetItemFromID": lambda _s, _e: item})()})()
    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(outlook_bridge, "_com", contextlib.nullcontext)
    monkeypatch.setattr(outlook_bridge, "_application", lambda: app)
    files = outlook_bridge.read_attachments("ENTRY-3801")
    assert files[0].lines == ["Pallets: 3"]
    assert made and not made[0].exists()


# ------------------------------------------------- the draft, merged -----
@pytest.fixture
def book(tmp_path):
    conn = db.open_db(tmp_path / "att.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Quarrystone Freight", country="Chile")
    repo.insert("party_domain", {"domain": "quarrystone.example", "party_id": party})
    db.set_setting(conn, "next_opp_number", 3801)
    repo.commit()
    yield conn, repo, party
    conn.close()


def a_mail(body, subject="Rate request LAX - SAI", sender="lena@quarrystone.example", internal=False,
           to=("desk@jojofreight.example",), names=("Packing List.pdf",)):
    return outlook_bridge.MailInfo(subject=subject, received_on="2026-11-16", received_at="2026-11-16 09:20",
                                   sender_address=sender, sender_name="Lena Orvik", sender_internal=internal,
                                   to_addresses=list(to), body_excerpt=body, entry_id="ENTRY-3801",
                                   conversation_id="CONV-3801", attachment_names=list(names))


BODY = "Hello,\nPlease quote LCL.\nIncoterm: EXW\nTotal pallets: 4\nGross weight: 310 kg\nVolume: 2.4 cbm\nThanks"
PACKING = ["PACKING LIST", "Total pallets: 5", "Gross weight: 310 kg", "Volume: 2.4 cbm",
           "Commodity: Ceramic tiles", "Incoterm: FCA"]


def test_the_body_wins_an_attachment_fills_the_gaps_and_a_difference_is_a_check(book, tmp_path):
    conn, repo, party = book
    text_pdf(tmp_path / "Packing List.pdf", PACKING)
    draft = intake.build(conn, a_mail(BODY))
    assert intake.attachments_wanted(draft)
    intake.add_attachments(conn, draft, [att.read_file(tmp_path / "Packing List.pdf", "Packing List.pdf")])
    assert draft.values["incoterm"] == "EXW"                                   # the body's
    assert draft.values["commodity"] == "Ceramic tiles"
    assert draft.sources["commodity"] == "from the attachment Packing List.pdf"
    assert "the mail says 4 pallets, Packing List.pdf says 5 pallets" in draft.attachment_checks
    assert "Incoterm: the mail says EXW, Packing List.pdf says FCA" in draft.attachment_checks
    assert [l.get("pieces") for l in draft.cargo_lines] == [4]                 # the body's line kept
    assert "Total pallets: 5" in draft.attachment_used["Packing List.pdf"]


def test_a_body_with_no_cargo_takes_the_attachments_lines(book, tmp_path):
    conn, repo, party = book
    path = tmp_path / "Load plan.xlsx"
    sheet_xlsx(path, [["Pallets", "Gross weight (kg)", "Volume (cbm)"], [3, 450, 2.2], [1, 90, 0.4]])
    draft = intake.build(conn, a_mail("Hello,\nPlease quote LCL, details attached.\nThanks", names=(path.name,)))
    intake.add_attachments(conn, draft, [att.read_file(path, path.name)])
    lines = [(l["pieces"], l["weight_kg"], l["cbm"], l["attachment"]) for l in draft.cargo_lines]
    assert lines == [(3, 450.0, 2.2, "Load plan.xlsx"), (1, 90.0, 0.4, "Load plan.xlsx")]
    assert draft.attachment_checks == []


def test_an_sds_sets_dg_and_notes_the_line_and_several_products_give_several_lines(book, tmp_path):
    conn, repo, party = book
    text_pdf(tmp_path / "SDS Primer.pdf", SDS_LINES)
    text_pdf(tmp_path / "SDS Thinner.pdf", ["Section 14. Transport information", "UN number: UN1263",
                                            "UN proper shipping name: PAINT RELATED MATERIAL",
                                            "Transport hazard class(es): 3", "Packing group: II",
                                            "Section 15. Regulatory information"])
    draft = intake.build(conn, a_mail("Hello,\nPlease quote, DG.\nPieces: 6\nGross weight: 1200 kg\nThanks",
                                      names=("SDS Primer.pdf", "SDS Thinner.pdf")))
    files = [att.read_file(tmp_path / n, n) for n in ("SDS Primer.pdf", "SDS Thinner.pdf")]
    intake.add_attachments(conn, draft, files)
    assert "DG" in draft.special_tags
    assert draft.cargo_lines[0]["notes"] == "UN1993, Class 3, PG III (from SDS Primer.pdf)"
    assert draft.cargo_lines[0]["pieces"] == 6
    assert draft.cargo_lines[1]["notes"] == "UN1263, Class 3, PG II (from SDS Thinner.pdf)"
    assert draft.cargo_lines[1]["commodity"] == "Paint Related Material"


def test_an_sds_note_goes_on_the_line_the_bodys_containers_make(book, tmp_path):
    conn, repo, party = book
    text_pdf(tmp_path / "SDS Primer.pdf", SDS_LINES)
    draft = intake.build(conn, a_mail("Hello,\nPlease quote FCL EXW, DG cargo.\n3 X 40 HC\nThanks",
                                      names=("SDS Primer.pdf",)))
    intake.add_attachments(conn, draft, [att.read_file(tmp_path / "SDS Primer.pdf", "SDS Primer.pdf")])
    assert len(draft.cargo_lines) == 1
    line = draft.cargo_lines[0]
    assert (line["container_qty"], line["container_type"]) == (3, "40HQ")
    assert line["notes"] == "UN1993, Class 3, PG III (from SDS Primer.pdf)"


def test_our_own_mail_giving_a_rate_has_no_attachments_read(book):
    conn, repo, party = book
    mail = a_mail("Dear Lena,\nPlease find our quotation attached.\nOcean freight USD 1,450 per container\nBest",
                  subject="RE: Rate request LAX - SAI", sender="", internal=True, to=("lena@quarrystone.example",))
    draft = intake.build(conn, mail)
    assert (draft.outbound or draft.reply_to_requester) and not intake.attachments_wanted(draft)


# --------------------------------------------------------- the pages -----
@pytest.fixture
def web(tmp_path, monkeypatch):
    path = tmp_path / "web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Quarrystone Freight", country="Chile")
    repo.insert("party_domain", {"domain": "quarrystone.example", "party_id": party})
    db.set_setting(conn, "next_opp_number", 3801)
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    holder = {"mail": None, "files": [], "asked": []}
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: holder["mail"])

    def read(entry_id):
        holder["asked"].append(entry_id)
        return holder["files"]
    monkeypatch.setattr(outlook_bridge, "read_attachments", read)
    return app.test_client(), path, holder


def test_the_register_page_shows_the_attachment_values_the_check_and_the_files(web, tmp_path):
    client, path, holder = web
    text_pdf(tmp_path / "Packing List.pdf", PACKING)
    blank_pdf(tmp_path / "Scan 0042.pdf")
    holder["files"] = [att.read_file(tmp_path / n, n) for n in ("Packing List.pdf", "Scan 0042.pdf")]
    holder["mail"] = a_mail(BODY, names=("Packing List.pdf", "Scan 0042.pdf", "image001.png"))
    body = client.post("/enquiries/from-outlook").data.decode("utf-8")
    assert holder["asked"] == ["ENTRY-3801"]
    assert "the mail says 4 pallets, Packing List.pdf says 5 pallets" in body
    assert re.search(r'class="pill chk-pill">check</span> Incoterm: the mail says EXW, Packing List.pdf says FCA', body)
    assert "from the attachment Packing List.pdf" in body
    source = body[body.index('id="cargo-source"'):]
    source = source[:source.index("</div>\n  {% endif %}") if "{% endif %}" in source else len(source)]
    assert '<div class="rh att-h">Packing List.pdf</div>' in source
    assert '<span class="num">5</span>' in source
    assert "Scan 0042.pdf: " + att.NOT_READ_SCAN in source
    # the checks ride along on a refused save
    hidden = re.search(r'name="attachment_checks" value="([^"]*)"', body).group(1)
    assert "Packing List.pdf says 5 pallets" in hidden.replace("&#34;", '"')


def test_an_sds_line_note_is_registered_with_its_line(web, tmp_path):
    client, path, holder = web
    text_pdf(tmp_path / "SDS Primer.pdf", SDS_LINES)
    holder["files"] = [att.read_file(tmp_path / "SDS Primer.pdf", "SDS Primer.pdf")]
    holder["mail"] = a_mail("Hello,\nPlease quote LCL LAX to SAI.\nPieces: 6\nGross weight: 1200 kg\n"
                            "Volume: 3 cbm\nThanks", names=("SDS Primer.pdf",))
    body = client.post("/enquiries/from-outlook").data.decode("utf-8")
    assert "Note: UN1993, Class 3, PG III (from SDS Primer.pdf)" in body
    assert re.search(r'<input type="checkbox" name="special_handling" value="DG"[^>]*checked', body)
    assert 'name="cargo_0_notes" data-f="notes" value="UN1993, Class 3, PG III (from SDS Primer.pdf)"' in body


def test_our_quoting_mail_never_asks_outlook_for_its_attachments(web):
    client, path, holder = web
    holder["mail"] = a_mail("Dear Lena,\nPlease find our quotation attached.\nOcean freight USD 1,450 per container",
                            subject="RE: Rate request LAX - SAI", sender="", internal=True,
                            to=("lena@quarrystone.example",), names=("JoJo quote 3801.pdf",))
    body = client.post("/enquiries/from-outlook").data.decode("utf-8")
    assert holder["asked"] == []
    assert "Attachments not read: this is our own mail, not a request (JoJo quote 3801.pdf)." in body


@pytest.fixture
def case_web(tmp_path, monkeypatch):
    path = tmp_path / "case.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Quarrystone Freight", country="Chile")
    case = add_enquiry(repo, "OPP-3802", party, received="2026-11-16", service_mode="LCL",
                       origin_loc="LAX", origin_country="USA", dest_loc="SAI", dest_country="Chile")
    line = repo.add_cargo_line(case, {"pieces": 6, "pack_word": "drums", "weight_value": 1200,
                                      "weight_unit": "kg", "cbm_value": 3, "source": "manual"})
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    holder = {"files": []}
    monkeypatch.setattr(outlook_bridge, "read_attachments", lambda entry_id: holder["files"])
    client = app.test_client()
    with client.session_transaction() as s:
        s["att_mail"] = {"case": case, "entry_id": "ENTRY-3802", "names": ["SDS Primer.pdf"], "subject": "Docs"}
    return client, path, holder, case, line


def test_read_attachments_on_the_case_page_offers_suggestions_and_writes_nothing_by_itself(case_web, tmp_path):
    client, path, holder, case, line = case_web
    text_pdf(tmp_path / "SDS Primer.pdf", SDS_LINES)
    holder["files"] = [att.read_file(tmp_path / "SDS Primer.pdf", "SDS Primer.pdf")]
    before = client.get("/enquiries/{}".format(case)).data.decode("utf-8")
    assert "Attachments of the mail" in before and ">Read attachments</a>" in before
    page = client.get("/enquiries/{}?att=1".format(case)).data.decode("utf-8")
    panel = page[page.index('id="attachments"'):page.index("</table>", page.index('id="attachments"'))]
    assert "Special handling: DG" in panel
    assert "UN1993, Class 3, PG III (from SDS Primer.pdf)" in panel
    conn = db.open_db(path)
    assert conn.execute("SELECT special_handling FROM enquiry WHERE id = ?", (case,)).fetchone()[0] in (None, "")
    assert conn.execute("SELECT notes FROM enquiry_cargo WHERE id = ?", (line,)).fetchone()[0] in (None, "")
    conn.close()
    findings = [json.loads(m.replace("&#34;", '"').replace("&amp;", "&"))
                for m in re.findall(r'name="finding" value="([^"]*)"', panel)]
    for f in findings:
        client.post("/enquiries/{}/attachments/accept".format(case), data={"finding": json.dumps(f), "skip": ""})
    conn = db.open_db(path)
    assert conn.execute("SELECT special_handling FROM enquiry WHERE id = ?", (case,)).fetchone()[0] == "DG"
    assert conn.execute("SELECT notes FROM enquiry_cargo WHERE id = ?", (line,)).fetchone()[0] == \
        "UN1993, Class 3, PG III (from SDS Primer.pdf)"
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'enquiry_cargo' AND field = 'notes'"
                        ).fetchone()[0] == 1
    conn.close()
    # an accepted or dismissed suggestion leaves the list
    after = client.get("/enquiries/{}?att=1&skip={}".format(case, ",".join(f["key"] for f in findings))).data.decode("utf-8")
    assert "Nothing in the attachments to add to the case." in after
