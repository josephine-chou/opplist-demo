"""Batch H2: a register save is never refused for its size.

The register form carries the mail's text in hidden fields; Werkzeug 3.1
refuses a form over MAX_FORM_MEMORY_SIZE (500,000 bytes by default) or a
multipart one over MAX_FORM_PARTS (1,000). create_app sets both far above
any mail, and a 413 that still happens is the app's own page plus a log
line. A mocked Outlook; the spreadsheet is built by the test. Every
company, person, place, number and product is invented; cases from
OPP-6301.
"""

from __future__ import annotations

import logging

import pytest

from conftest import add_party, seed_reference
from opplist import attachments as att
from opplist import db, outlook_bridge
from opplist.web import create_app
from test_attachments import sheet_xlsx
from test_register_v3 import fields_of


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "h2.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Brindlecove Logistics", country="Chile")
    repo.insert("party_domain", {"domain": "brindlecove.example", "party_id": party})
    db.set_setting(conn, "next_opp_number", 6301)
    repo.commit()
    conn.close()
    holder = {"mail": None, "files": []}
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: holder["mail"])
    monkeypatch.setattr(outlook_bridge, "read_attachments", lambda entry_id: holder["files"])
    app = create_app(path)
    app.config["TESTING"] = True
    return app, path, holder


def a_mail(names):
    return outlook_bridge.MailInfo(
        subject="Quote request ORLANDO - VALPARAISO", received_on="2026-11-18", received_at="2026-11-18 10:37",
        sender_address="pell@brindlecove.example", sender_name="Pell Varga", sender_internal=False,
        to_addresses=["desk@jojofreight.example"],
        body_excerpt="Hello,\nPlease quote LCL, details in the attached sheet.\nPell\n",
        entry_id="ENTRY-6301", conversation_id="CONV-6301", attachment_names=list(names))


def big_sheet(path):
    """Three cargo rows, then a long parts list - well over 1 MB of text."""
    rows = [["Pieces", "Packing", "Gross weight (kg)", "Volume (cbm)", "Commodity"],
            [2, "Pallets", 400, 1.8, "Garden benches"],
            [1, "Crate", 150, 0.9, "Garden benches"],
            [], ["Part list"]]
    rows += [["BX-{:05d}".format(n), "Slat, larch, {} mm, oiled, bundle of twelve".format(600 + n % 400),
              "Bay {}".format(n % 40), "Lot {}".format(n % 9)] for n in range(14000)]
    sheet_xlsx(path, rows)


def test_a_register_save_with_a_sheet_over_1_mb_creates_the_case(world, tmp_path):
    app, path, holder = world
    client = app.test_client()
    big_sheet(tmp_path / "Parts list.xlsx")
    holder["files"] = [att.read_file(tmp_path / "Parts list.xlsx", "Parts list.xlsx")]
    holder["mail"] = a_mail(("Parts list.xlsx",))
    page = client.post("/enquiries/from-outlook")
    assert page.status_code == 200
    fields = fields_of(page.data.decode("utf-8"))
    assert len(fields["mail_cargo_text"][0].encode("utf-8")) > 1024 * 1024
    fields.update(origin_country=["USA"], dest_country=["Chile"], service_mode=["LCL"], ack_after=["nothing"])
    saved = client.post("/enquiries/new", data=fields)
    assert saved.status_code in (200, 302), saved.status_code
    conn = db.open_db(path)
    assert [r[0] for r in conn.execute("SELECT opp_no FROM enquiry")] == ["OPP-6301"]
    conn.close()


def test_a_multipart_form_with_3000_fields_is_read(world):
    app, path, holder = world
    seen = {}

    @app.route("/probe-h2", methods=["POST"])
    def probe():
        seen["n"] = len(request_form())
        return "ok"

    data = {"field_{}".format(n): "value {}".format(n) for n in range(3000)}
    data["upload"] = (__import__("io").BytesIO(b"invented"), "note.txt")   # makes it multipart
    answer = app.test_client().post("/probe-h2", data=data, content_type="multipart/form-data")
    assert answer.status_code == 200
    assert seen["n"] == 3000


def request_form():
    from flask import request
    return request.form


def test_a_413_is_the_apps_own_page_and_a_log_line(world, caplog):
    app, path, holder = world
    app.config["MAX_FORM_MEMORY_SIZE"] = 1000
    with caplog.at_level(logging.INFO, logger="opplist"):
        answer = app.test_client().post("/enquiries/new", data={"remarks": "x" * 5000})
    body = answer.data.decode("utf-8")
    assert answer.status_code == 413
    assert "This form is too large for OppList to save" in body and "Nothing was saved." in body
    assert "Request Entity Too Large" not in body
    assert "history.back()" in body
    lines = [r.getMessage() for r in caplog.records if "too large" in r.getMessage()]
    assert lines and "/enquiries/new" in lines[0] and "bytes" in lines[0]
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM enquiry").fetchone()[0] == 0
    conn.close()


def test_create_app_sets_the_limits(world):
    app = world[0]
    assert app.config["MAX_FORM_MEMORY_SIZE"] == 64 * 1024 * 1024
    assert app.config["MAX_FORM_PARTS"] == 20000
    assert app.config["MAX_CONTENT_LENGTH"] is None
    with app.test_request_context("/", method="POST"):
        from flask import request
        assert request.max_form_memory_size == 64 * 1024 * 1024 and request.max_form_parts == 20000
