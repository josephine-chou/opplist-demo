"""Batch I1: OppList's tab icon, a pink pushpin.

The pages link web/static/favicon*, /favicon.ico answers with the same
file, and the two files the app writes to be opened from disk carry the
32 px PNG as a data: URI. An empty invented database; no case data.
"""

from __future__ import annotations

import base64
import re

import pytest

from conftest import seed_reference
from opplist import dashboard, db, favicon, monthly, monthly_export
from opplist.web import create_app


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "i1.db"
    conn = db.open_db(path)
    seed_reference(db.Repository(conn, source="test"))
    conn.commit()
    app = create_app(path)
    app.config["TESTING"] = True
    yield conn, app.test_client()
    conn.close()


def head_of(body):
    return body[:body.index("</head>")]


def test_favicon_ico_answers_with_the_icon(world):
    conn, client = world
    answer = client.get("/favicon.ico")
    assert answer.status_code == 200
    assert answer.content_type in ("image/vnd.microsoft.icon", "image/x-icon")
    assert answer.data == (favicon.STATIC / "favicon.ico").read_bytes()


def test_a_page_head_links_the_icon(world):
    conn, client = world
    head = head_of(client.get("/enquiries").data.decode("utf-8"))
    assert re.search(r'<link rel="icon" href="/static/favicon\.ico\?v=\d+" sizes="any">', head)
    assert re.search(r'<link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32\.png\?v=\d+">', head)
    assert re.search(r'<link rel="icon" type="image/png" sizes="64x64" href="/static/favicon-64\.png\?v=\d+">', head)
    assert client.get("/static/favicon-64.png").status_code == 200


def test_the_exported_month_report_and_dashboard_carry_the_data_uri(world):
    conn, client = world
    uri = favicon.data_uri()
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == (favicon.STATIC / "favicon-32.png").read_bytes()
    link = '<link rel="icon" type="image/png" sizes="32x32" href="{}">'.format(uri)
    assert link in head_of(monthly_export.render_html(monthly.build(conn, "2026-11")))
    assert link in head_of(dashboard.render_standalone(dashboard.build(conn, today="2026-11-20")))
