"""Case page v2e (2026-09-25): a fixed section order whatever
the status, Rate used as its own card after Rate requests on Won cases,
and the condensed header's identity line."""

from __future__ import annotations

import re

import pytest

from conftest import add_enquiry, add_party, seed_reference, form_nesting
from opplist import db
from opplist.web import create_app

ORDER = ["status", "followups", "request", "routes", "cargo", "vendors", "mail"]


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "case-v2e.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    seavio = add_party(repo, "Seavio", country="USA")
    quoted = add_enquiry(repo, "OPP-1407", tolvex, received="2026-09-07", service_mode="FCL",
                         container_type="40GP", container_qty=1, incoterm="EXW",
                         origin_country="USA", origin_loc="MIA", origin_alt="1187 Juniper Crossing Road",
                         dest_country="Chile", dest_loc="SAI")
    repo.set_status(quoted, "Quoted", "2026-09-14")
    won = add_enquiry(repo, "OPP-1307", tolvex, received="2026-09-01", service_mode="Air",
                      origin_country="USA", dest_country="Chile")                       # no incoterm
    repo.set_status(won, "Quoted", "2026-09-03")
    repo.set_status(won, "Won", "2026-09-10", rate_used="agent")
    repo.insert("vendor_request", {"enquiry_id": won, "vendor_party_id": seavio,
                                   "asked_on": "2026-09-02", "machine_status": "Quoted"})
    lost = add_enquiry(repo, "OPP-1412", tolvex, received="2026-08-03", service_mode="FCL",
                       container_type="20GP", container_qty=2, dest_country="Chile")     # one country only
    repo.set_status(lost, "Quoted", "2026-08-05")
    repo.set_status(lost, "Lost", "2026-08-20", closed_reason="Price too high")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), {"quoted": quoted, "won": won, "lost": lost}


def page(client, case_id):
    response = client.get("/enquiries/{}".format(case_id))
    assert response.status_code == 200
    return response.data.decode("utf-8")


def rail_keys(body):
    start = body.index('<ol class="sections">')
    return re.findall(r'<li class="sec [^"]*"><a href="#(\w+)">', body[start:body.index("</ol>", start)])


def section_order(body):
    found = [(body.index('id="{}"'.format(k)), k) for k in ORDER + ["won"] if 'id="{}"'.format(k) in body]
    return [k for _, k in sorted(found)]


def test_the_order_is_fixed_and_rate_used_only_on_won(world):
    client, ids = world
    assert section_order(page(client, ids["quoted"])) == ORDER
    assert section_order(page(client, ids["lost"])) == ORDER
    body = page(client, ids["won"])
    assert section_order(body) == ["status", "followups", "request", "routes", "cargo", "vendors", "won", "mail"]
    assert 'id="rate-used-form"' in body and "Save rate used" in body
    # the Follow-ups card stays in the top row on a closed case, one line
    two = body[body.index('<div class="two">'):body.index('id="request"')]
    assert 'id="followups"' in two and ">Closed Sep 10</p>" in two
    for key in ("quoted", "won", "lost"):
        deepest, ids_ = form_nesting(page(client, ids[key]))
        assert deepest <= 1, ids_


def test_the_rail_follows_the_same_order(world):
    client, ids = world
    assert rail_keys(page(client, ids["quoted"])) == ORDER
    assert rail_keys(page(client, ids["lost"])) == ORDER
    assert rail_keys(page(client, ids["won"])) == ["status", "followups", "request", "routes", "cargo", "vendors", "won", "mail"]
    body = page(client, ids["won"])
    assert '<a href="#won">' in body and ">Rate used</span>" in body


def test_the_condensed_line_names_the_case(world):
    client, ids = world
    body = page(client, ids["quoted"])
    def cond(body):
        # 2026-09-25: the requester in a box, the mode as its outline label,
        # the incoterm and the lane in dark text - the words as before
        inner = re.search(r'<span class="cond-line">(.*?)</span>\s*\n', body, re.S).group(1)
        return inner, " ".join(re.sub(r"<[^>]+>", " ", inner).replace("&middot;", "·").split())

    inner, text = cond(body)
    assert text == "Tolvex FCL EXW · USA → Chile"
    assert '<span class="req-box">Tolvex</span>' in inner and '<span class="lbl tone-accent">FCL</span>' in inner
    assert '<span class="dark">EXW</span>' in inner and '<span class="dark">USA → Chile</span>' in inner
    assert body.index('class="pill big s-quoted"') < body.index('class="cond-line"')
    assert cond(page(client, ids["won"]))[1] == "Tolvex Air · USA → Chile"
    assert cond(page(client, ids["lost"]))[1] == "Tolvex FCL · Chile"
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / "opplist" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".case-page .cond-line { display: none; }" in css
    assert ".case-page .head.condensed .cond-line { display: inline-block;" in css
    assert "--due: #7d0100;" in css
