"""Batch 23: the place pickers in a real browser - the list under the box
(the side's country first, "Other countries" after), Up / Down / Enter / Esc
and a click, Enter never saving the card, the chips and their x, "+ Add
location" and its small form, "+ Mark as a port", Air turning the Port of
discharge into the Airport of destination, the first place setting the
country, and the register page: its routes, its place choices and "Add as
route". The server side is in tests/test_place_pickers.py.

The app runs in this process on a free port of 127.0.0.1 over a database
under tmp_path; the browser is Playwright's Chromium, or on Windows the
installed Microsoft Edge. Skipped when Playwright for Python or a browser it
can start is not on this computer.

Every company, case, place and code here is invented; cases from OPP-3301."""

from __future__ import annotations

import glob
import os
import sys
import threading

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from opplist import db, outlook_bridge  # noqa: E402
from opplist.web import create_app  # noqa: E402
from test_place_pickers import build_world, mail, new_case, place_keys, route_a  # noqa: E402


def _launch(playwright):
    """Edge on Windows, else Playwright's own Chromium, else any Chromium
    Playwright has installed; None when none of them starts."""
    tries = [{"channel": "msedge"}] if sys.platform == "win32" else []
    tries.append({})
    home = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or ""
    for pattern in ("chromium-*/chrome-linux/chrome", "chromium-*/chrome-win/chrome.exe"):
        for exe in sorted(glob.glob(os.path.join(home, pattern)), reverse=True) if home else []:
            tries.append({"executable_path": exe})
    for options in tries:
        try:
            return playwright.chromium.launch(**options)
        except Exception:                      # not installed here: try the next one
            continue
    return None


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        found = _launch(playwright)
        if found is None:
            pytest.skip("no browser Playwright can start on this computer")
        yield found
        found.close()


@pytest.fixture
def site(tmp_path):
    """The invented world served on a free port; the case OPP-3301 (route A
    Willow Bend, TX to Quillmouth, FCL) and its ids."""
    from werkzeug.serving import make_server

    path = tmp_path / "browser.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    agent = build_world(repo)
    case = new_case(repo, "OPP-3301", agent)
    ids = {"agent": agent, "case": case, "a": route_a(conn, case)}
    repo.commit()
    conn.close()
    app = create_app(path)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}".format(server.server_port), path, ids
    finally:
        server.shutdown()
        thread.join(5)


@pytest.fixture
def tab(browser):
    context = browser.new_context(viewport={"width": 1400, "height": 1000})
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda exc: page.errors.append(str(exc)))
    yield page
    context.close()


def open_route_a(tab, base, ids):
    tab.goto("{}/enquiries/{}".format(base, ids["case"]))
    tab.click('[data-unit="route-{}"] [data-edit]'.format(ids["a"]))
    return "#r{}-".format(ids["a"])


def typed(tab, picker, text):
    """Click the picker's box and type, the way she does."""
    tab.click(picker + " .pk-box")
    tab.keyboard.type(text)
    tab.wait_for_selector(picker + ".open .pk-list")


def rows(tab, picker):
    """The list under the box, row by row: (what the row is, its key or its
    words, what it says the place is, the country and code on the right)."""
    return [tuple(r) for r in tab.eval_on_selector_all(picker + " .pk-list > div", """els => els.map(e => [
        e.classList.contains('pk-dv') ? 'divider' : (e.classList.contains('pk-act') ? 'action' : 'place'),
        e.querySelector('.k') ? e.querySelector('.k').textContent : e.textContent,
        e.querySelector('.t') ? e.querySelector('.t').textContent : '',
        e.querySelector('.c') ? e.querySelector('.c').textContent : ''])""")]


def places_listed(tab, picker):
    return [r[1] for r in rows(tab, picker) if r[0] == "place"]


def value(tab, picker):
    return tab.eval_on_selector(picker + " input.pk-value", "el => el.value")


def saved(tab, base, ids, path):
    with tab.expect_response(lambda r: r.url.endswith("/enquiries/{}/routes".format(ids["case"]))
                             and r.request.method == "POST") as answer:
        tab.click("#r{}-form button[type=submit]".format(ids["a"]))
    assert answer.value.json()["ok"], answer.value.text()
    return db.open_db(path)


def test_the_list_puts_the_side_country_first_and_says_what_each_place_is(tab, site):
    base, path, ids = site
    p = open_route_a(tab, base, ids) + "dest_loc"
    typed(tab, p, "port")
    assert rows(tab, p) == [("place", "PORT ALDER", "port", "CLZPA"), ("divider", "Other countries", "", ""),
                            ("place", "PORT HOLLIS", "port", "Peru"), ("action", "+ Add location \u201cport\u201d", "", "")]
    assert "PORT ALDER" in tab.inner_text(p + " .pk-it.act")      # the first row is the one Enter takes
    for text, key, kind in (("bramble", "BRAMBLE BAY", "port, airport"), ("fenwick", "FENWICK", "city"),
                            ("alder field", "ALDER FIELD", "airport"), ("marrow", "MARROW POINT", "port (to confirm)"),
                            ("clzpa", "PORT ALDER", "port"), ("harbour", "PORT ALDER", "port")):
        tab.fill(p + " .pk-q", text)                              # key, name, code, alias
        first = rows(tab, p)[0]
        assert first[:3] == ("place", key, kind), (text, first)
    tab.fill(p + " .pk-q", "old wharf")                           # retired: never listed
    assert places_listed(tab, p) == []
    assert not tab.errors


def test_enter_takes_the_highlighted_place_and_never_saves_the_card(tab, site):
    base, path, ids = site
    prefix = open_route_a(tab, base, ids)
    p = prefix + "dest_loc"
    posts = []
    tab.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    typed(tab, p, "port")
    tab.keyboard.press("ArrowDown")                               # PORT ALDER -> PORT HOLLIS
    assert "PORT HOLLIS" in tab.inner_text(p + " .pk-it.act")
    tab.keyboard.press("ArrowUp")
    assert "PORT ALDER" in tab.inner_text(p + " .pk-it.act")
    tab.keyboard.press("Enter")
    assert value(tab, p) == "QUILLMOUTH|PORT ALDER"
    assert tab.locator(p + " .pk-or").count() == 1
    tab.wait_for_timeout(300)
    assert posts == []                                            # nothing was sent
    assert tab.evaluate("window.__caseUnits()") == ["route-{}*".format(ids["a"])]   # open, not saved
    # Esc closes the list and keeps the card open; a click takes a place
    tab.keyboard.type("bram")
    tab.wait_for_selector(p + ".open .pk-list")
    tab.keyboard.press("Escape")
    assert not tab.is_visible(p + " .pk-list")
    assert tab.evaluate("window.__caseUnits()") == ["route-{}*".format(ids["a"])]
    tab.keyboard.type("b")
    tab.click(p + " .pk-list .pk-it >> nth=0")
    assert value(tab, p) == "QUILLMOUTH|PORT ALDER|BRAMBLE BAY"
    conn = saved(tab, base, ids, path)
    assert place_keys(conn, ids["a"], "dest") == ["QUILLMOUTH", "PORT ALDER", "BRAMBLE BAY"]
    conn.close()
    row = "#route-row-{}".format(ids["a"])
    tab.wait_for_function("s => document.querySelector(s).innerText.includes(' or ')", arg=row)   # the page read again
    assert "Quillmouth or Port Alder or Bramble Bay, Chile" in tab.inner_text(row)
    assert not tab.errors


def test_a_click_on_one_x_takes_off_only_that_place(tab, site):
    base, path, ids = site
    p = open_route_a(tab, base, ids) + "dest_loc"
    typed(tab, p, "port alder")
    tab.keyboard.press("Enter")
    tab.keyboard.type("bramble")
    tab.wait_for_selector(p + ".open .pk-list")
    tab.keyboard.press("Enter")
    assert value(tab, p) == "QUILLMOUTH|PORT ALDER|BRAMBLE BAY"
    tab.click(p + ' .pk-x[data-i="1"]')
    assert value(tab, p) == "QUILLMOUTH|BRAMBLE BAY"
    assert tab.locator(p + " .pk-chip").count() == 2
    assert not tab.errors


def test_add_location_opens_a_small_form_and_the_place_is_to_confirm(tab, site):
    base, path, ids = site
    p = open_route_a(tab, base, ids) + "dest_loc"
    typed(tab, p, "dunmorrow")
    assert rows(tab, p) == [("action", "+ Add location \u201cdunmorrow\u201d", "", "")]
    tab.keyboard.press("Enter")                                   # the only row: it opens the form
    form = "#r{}-route-dest .pk-addf".format(ids["a"])
    tab.wait_for_selector(form)
    assert tab.input_value(form + " .pk-ak") == "DUNMORROW" and tab.input_value(form + " .pk-an") == "Dunmorrow"
    assert tab.input_value(form + " .pk-ac") == "Chile"
    assert not tab.is_checked(form + " .pk-ap") and not tab.is_checked(form + " .pk-aa")
    tab.check(form + " .pk-ap")
    tab.press(form + " .pk-an", "Enter")                          # Enter adds; it never saves the card
    assert value(tab, p) == "QUILLMOUTH|DUNMORROW"
    box = tab.inner_text(p + " .pk-box")                           # RD1c: "new: DUNMORROW · port (to confirm)"
    assert "new: DUNMORROW" in box and "(to confirm)" in box
    conn = db.open_db(path)
    assert conn.execute("SELECT 1 FROM master_location WHERE key = 'DUNMORROW'").fetchone() is None   # not yet
    conn.close()
    conn = saved(tab, base, ids, path)
    row = conn.execute("SELECT status, source, is_seaport, country FROM master_location WHERE key = 'DUNMORROW'").fetchone()
    assert tuple(row) == ("to_confirm", "intake", 1, "Chile")
    conn.close()
    assert not tab.errors


def test_mark_as_a_port_and_air_turn_the_port_of_discharge_box(tab, site):
    base, path, ids = site
    prefix = open_route_a(tab, base, ids)
    p = prefix + "pod"
    typed(tab, p, "creston quay")
    assert rows(tab, p) == [("action", "+ Mark \u201cCRESTON QUAY\u201d as a port", "", "")]
    tab.keyboard.press("Enter")
    assert value(tab, p) == "CRESTON QUAY" and "marked as a port" in tab.inner_text(p + " .pk-box")
    tab.keyboard.type("bramble")                                  # a port and an airport
    tab.wait_for_selector(p + ".open .pk-list")
    tab.keyboard.press("Enter")
    tab.keyboard.type("port alder")
    tab.wait_for_selector(p + ".open .pk-list")
    tab.keyboard.press("Enter")
    assert value(tab, p) == "CRESTON QUAY|BRAMBLE BAY|PORT ALDER"
    # Air: Airport of destination, airports only; the ports go, a place that is both stays
    tab.select_option(prefix + "service_mode", "Air")
    assert value(tab, p) == "BRAMBLE BAY"
    label = '#r{}-form .pk-f[data-pk-field="pod"] .pk-label'.format(ids["a"])
    assert tab.text_content(label) == "Airport of destination"
    tab.click(p + " .pk-box")
    tab.keyboard.type("alder")
    tab.wait_for_selector(p + ".open .pk-list")
    assert places_listed(tab, p) == ["ALDER FIELD"]
    tab.keyboard.press("Escape")
    tab.select_option(prefix + "service_mode", "LCL")               # back to a sea mode
    assert tab.text_content(label) == "Port of discharge"
    assert value(tab, p) == "BRAMBLE BAY"
    tab.select_option(prefix + "service_mode", "FCL")               # FCL to LCL to FCL drops nothing
    assert value(tab, p) == "BRAMBLE BAY"
    conn = saved(tab, base, ids, path)
    assert place_keys(conn, ids["a"], "pod") == ["BRAMBLE BAY"]
    assert conn.execute("SELECT is_seaport FROM master_location WHERE key = 'CRESTON QUAY'").fetchone()[0] == 0
    conn.close()
    assert not tab.errors


def test_the_first_place_of_a_side_sets_its_country(tab, site):
    base, path, ids = site
    prefix = open_route_a(tab, base, ids)
    p = prefix + "dest_loc"
    tab.click(p + ' .pk-x[data-i="0"]')
    tab.keyboard.type("hollis")
    tab.wait_for_selector(p + ".open .pk-list")
    tab.keyboard.press("Enter")
    assert tab.input_value(prefix + "dest_country") == "Peru"
    # the second place does not move it
    tab.keyboard.type("quillmouth")
    tab.wait_for_selector(p + ".open .pk-list")
    tab.keyboard.press("Enter")
    assert value(tab, p) == "PORT HOLLIS|QUILLMOUTH" and tab.input_value(prefix + "dest_country") == "Peru"
    # Cancel puts back what the route had
    tab.click("#r{}-form [data-cancel]".format(ids["a"]))
    assert value(tab, p) == "QUILLMOUTH"
    assert not tab.errors


def test_the_register_page_pickers_and_a_route_added_there(tab, site):
    base, path, ids = site
    tab.goto(base + "/enquiries/new")
    posts = []
    tab.on("request", lambda r: posts.append(r.url) if r.method == "POST" and "preview" not in r.url else None)
    p = "#dest_loc"
    tab.select_option("#dest_country", "Chile")
    typed(tab, p, "quill")
    tab.keyboard.press("Enter")
    tab.keyboard.type("bramble")
    tab.wait_for_selector(p + ".open .pk-list")
    tab.keyboard.press("Enter")
    assert value(tab, p) == "QUILLMOUTH|BRAMBLE BAY"
    assert posts == []                                            # Enter registered nothing
    tab.click("[data-add-route]")
    b = '.reg-route[data-route-key="1"] .picker[data-side="dest"]'
    assert tab.eval_on_selector(b + " input.pk-value", "el => el.value") == ""
    tab.click(b + " .pk-box")
    tab.keyboard.type("port alder")
    tab.wait_for_selector(b + ".open .pk-list")
    tab.keyboard.press("Enter")
    assert tab.eval_on_selector(b + " input.pk-value", "el => el.value") == "PORT ALDER"
    assert value(tab, p) == "QUILLMOUTH|BRAMBLE BAY"
    assert "QUILLMOUTH or BRAMBLE BAY" in tab.inner_text("#reg-title")
    assert not tab.errors


def test_the_register_page_place_choices_and_add_as_route_fill_the_pickers(tab, site, monkeypatch):
    base, path, ids = site
    mails = [mail("Quotation request", "Hello,\nPOL: Willow Bend\nDestination: Quillmouth\nPOD: Fenwick\n1x40HC\n"),
             mail("RFQ WILLOW BEND - QUILLMOUTH // FENWICK - BRAMBLE BAY", "Hello,\n1x40HC\n")]
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: mails[0])

    def from_outlook():
        tab.goto(base + "/enquiries")
        tab.evaluate("""url => { const f = document.createElement('form'); f.method = 'post'; f.action = url;
                                 document.body.appendChild(f); f.submit(); }""", base + "/enquiries/from-outlook")
        tab.wait_for_selector("#enquiry-form")
    # the body named two destinations: nothing is filled, a click puts one in route A's picker
    from_outlook()
    assert value(tab, "#dest_loc") == ""
    tab.click('.body-conflict .choice[data-place="FENWICK"]')
    assert value(tab, "#dest_loc") == "FENWICK" and "FENWICK" in tab.inner_text("#dest_loc .pk-box")
    # another reading of the subject, added as route B: its pickers hold its places
    mails.pop(0)
    from_outlook()
    assert value(tab, "#dest_loc") == "QUILLMOUTH"
    tab.click(".other-lanes [data-add-route]")
    b = '.reg-route[data-route-key="1"] .picker[data-side="{}"]'
    assert tab.eval_on_selector(b.format("origin") + " input.pk-value", "el => el.value") == "FENWICK"
    assert tab.eval_on_selector(b.format("dest") + " input.pk-value", "el => el.value") == "BRAMBLE BAY"
    assert "port, airport" in tab.inner_text(b.format("dest") + " .pk-box")
    assert not tab.errors
