"""Batch 11's case header, month-first dates and the batch-10 phrases gone,
and schema v32's case_note table (its quick notes went in CX1, 2026-11-04;
the table stays). Written again with invented data in CX1, replacing
test_quick_notes_header_dates. Every company, person, place and date here
is invented; cases from OPP-8811."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import dates, db, outlook_bridge, schema_migrations
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app

TODAY = "2026-10-22"          # a Thursday
JS = (Path(__file__).resolve().parents[1] / "opplist" / "web" / "static" / "app.js").read_text(encoding="utf-8")
CSS = (Path(__file__).resolve().parents[1] / "opplist" / "web" / "static" / "app.css").read_text(encoding="utf-8")


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "header.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    lindqvist = add_party(repo, "Lindqvist Cargo", country="Chile")
    add_party(repo, "JoJo JJV", ptype="Office", country="USA")
    ids = {"lindqvist": lindqvist}
    ids["quoted"] = add_enquiry(repo, "OPP-8811", lindqvist, received="2026-10-05", service_mode="FCL",
                                origin_loc="HOU", origin_country="USA", dest_loc="SAI", dest_country="Chile",
                                incoterm="FOB", commodity="Pump housings", created_by="migration",
                                created_at="2026-10-07 11:05:12")
    repo.set_status(ids["quoted"], "Quoted", "2026-10-12")
    ids["out"] = add_enquiry(repo, "OPP-8812", lindqvist, received="2026-10-08", scope="Out",
                             origin_country="USA", dest_country="Chile")
    ids["yvr"] = add_enquiry(repo, "OPP-8813", lindqvist, received="2026-10-08", handled_by="JJV",
                             origin_country="USA", dest_country="Chile")
    repo.commit()
    conn.close()
    import opplist.web as web_module
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, ids


def html(client, url) -> str:
    return client.get(url).data.decode("utf-8")


def rail(body) -> str:
    return body[body.index('<aside class="rail">'):body.index("</aside>")]


def to_yvr() -> MailInfo:
    """Lindqvist's mail to JJV's desk, JJC on To by an alias."""
    return MailInfo(subject="RATE REQUEST // HOUSTON - SAN ANTONIO // 1 X 40HQ", sender_name="Elin Varga",
                    sender_address="elin@lindqvistcargo.test", received_on="2026-10-08",
                    received_at="2026-10-08 09:10", to_addresses=["yvr.desk@jojofreight.example", "jjc-mfn@jojofreight.example"],
                    display_to="JJV-Desk Lund; #JJC-MFN", to_names=["JJV-Desk Lund", "#JJC-MFN"],
                    conversation_id="CONV-8813", entry_id="EID-8813",
                    body_excerpt="Hello Desk,\nPlease quote 1 x 40HQ Houston to San Antonio, pump housings.\nElin")


# ------------------------------------------------------------ dates --
def test_the_date_helper_puts_the_month_first():
    assert dates.month_day("2026-10-05") == "Oct 5"
    assert dates.month_day("2026-10-05", weekday=True) == "Mon Oct 5"
    assert dates.month_day("2026-10-18", year=True) == "Oct 18, 2026"
    assert dates.month_day("2026-10-18 10:14", weekday=True, year=True) == "Sun Oct 18, 2026"
    assert dates.month_day(dt.date(2026, 10, 7)) == "Oct 7"
    assert dates.month_day(dt.datetime(2026, 10, 7, 8, 0)) == "Oct 7"
    assert dates.month_day("") == "" and dates.month_day(None) == "" and dates.month_day("soon") == ""


def test_the_browser_has_the_one_twin():
    assert "function dayMonth(value, weekday, year)" in JS
    assert JS.count("function dayMonth(") == 1
    assert 'var MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]' in JS


def test_the_pages_show_month_first_and_keep_iso_and_inputs(world):
    client, path, ids = world
    body = html(client, "/enquiries/{}".format(ids["quoted"]))
    facts = body[body.index('class="facts"'):body.index('<div class="layout">')]
    assert ">Oct 5<" in facts and ">Oct 12<" in facts
    assert not re.search(r">\d{1,2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)<", body)
    assert 'value="2026-10-05"' in body                        # the date input stays ISO
    assert "2026-10-12" in body                                # the timeline stays ISO
    assert "<b>Thu Oct 22, 2026</b>" in html(client, "/tracking")
    listing = html(client, "/enquiries?view=all")
    assert '<td class="c-received_on">Oct 5</td>' in listing


# ---------------------------------------------- schema v32's table --
def test_v32_creates_case_note_after_a_restore_point(tmp_path):
    path = tmp_path / "v31.db"
    conn = db.open_db(path)
    conn.execute("DROP TABLE case_note")
    conn.execute("UPDATE setting SET value = '31' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    current = schema_migrations.CURRENT_VERSION                  # 32 and every step after it
    assert schema_migrations.current_version(conn) == current >= 32
    assert any(n.startswith("restore point opplist_before_schema{}".format(current)) for n in notes), notes
    assert list((tmp_path / "restore-points").glob("opplist_before_schema{}_*.db".format(current)))
    assert "case_note table created (quick notes; nothing backfilled)" in notes
    cols = [r[1] for r in conn.execute("PRAGMA table_info(case_note)")]
    assert cols == ["id", "enquiry_id", "body", "created_at", "updated_at", "deleted_at"]
    assert schema_migrations._to_v32(conn) == ["case_note already on file"]
    conn.close()


# ------------------------------------------------------------ header --
def test_the_sub_line_has_no_type_place_scope_or_registered(world):
    client, path, ids = world
    body = html(client, "/enquiries/{}".format(ids["quoted"]))
    sub = body[body.index('<div class="sub">'):]
    sub = sub[:sub.index("</div>")]
    assert "Lindqvist Cargo</a>" in sub
    assert "(agent, Chile)" not in sub and "Scope" not in sub and "switch" not in sub.lower()
    assert "registered" not in sub.lower()
    head = body[body.index('<div class="card head"'):body.index('<div class="facts">')]
    assert "Out of scope" not in head
    menu = body[body.index('<details class="more-menu">'):body.index("</details>", body.index('<details class="more-menu">'))]
    assert '<div class="muted small reg-line">Registered Oct 7 &middot; migration</div>' in menu
    assert menu.index("reg-line") < menu.index("Switch scope to Out")
    assert "Add a lane" not in menu and "Same request as" not in menu       # v33: routes instead


def test_an_out_of_scope_case_shows_one_amber_label(world):
    client, path, ids = world
    body = html(client, "/enquiries/{}".format(ids["out"]))
    head = body[body.index('<div class="card head"'):body.index('<div class="facts">')]
    assert head.count("Out of scope") == 1 and '<span class="pill tone-amber out-of-scope">Out of scope</span>' in head
    assert "Switch scope to In" in body


def test_the_condensed_header_styles():
    assert ".case-page .head h1 .pill.big, .case-page .head.condensed h1 .pill.big { font-size: 12px; padding: 3px 8px; }" in CSS
    assert ".case-page .cond-line .req-box { background: #F1F4FA; border-radius: 5px; padding: 2px 8px; }" in CSS
    assert "gap: 14px" in CSS and ".case-page .cond-line .sep { margin: 0 -8px; }" in CSS


# ------------------------------------------------- batch-10 phrases --
def test_neither_phrase_on_any_page(world, monkeypatch):
    client, path, ids = world
    monkeypatch.setattr(outlook_bridge, "read_selection", to_yvr)
    for url in ("/enquiries/{}".format(ids["yvr"]), "/enquiries?view=others", "/enquiries?view=all",
                "/tracking", "/enquiries/from-outlook", "/parties/{}".format(ids["lindqvist"]), "/dashboard",
                "/report?month=2026-10"):
        body = html(client, url)
        assert "JJC copied" not in body and "not ours to chase" not in body, url
    body = html(client, "/enquiries/{}".format(ids["yvr"]))
    tile = body[body.index('<div class="f-l">Next</div>'):][:200]
    assert "Handled by JJV" in tile and '<div class="f-s"></div>' in tile
    r = rail(body)
    assert '<div class="n-title">Handled by JJV</div>' in r and "n-sub" not in r[r.index("n-title"):r.index("</div>", r.index("n-title")) + 60]
