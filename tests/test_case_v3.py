"""Case page v3, part 1 (batch 15a): every card saves itself, and status is
a row of actions. The browser behaviour (a unit kept open and typed in
while other writes go, the outline, the dot and the bottom bar, leaving)
is checked by tests/browser/case_page_probes.py; this file checks what the
server renders and writes.

Every company, case, place, number and note here is invented."""

from __future__ import annotations

import datetime as dt
import html.parser
import re
from pathlib import Path

import pytest

from conftest import add_enquiry, add_party, form_nesting, seed_reference
from opplist import db
from opplist.web import create_app

JS = (Path(__file__).resolve().parents[1] / "opplist" / "web" / "static" / "app.js").read_text(encoding="utf-8")
SAVE_FIRST = {"X-Save-First": "1", "Accept": "application/json"}
FUTURE = (dt.date.today() + dt.timedelta(days=20)).isoformat()


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "case-v3.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    kestrel = add_party(repo, "Kestrel Forwarding", country="Brazil")
    office = add_party(repo, "JoJo JJK", ptype="Office", country="USA")
    ids = {"kestrel": kestrel, "office": office}
    ids["three"] = add_enquiry(repo, "OPP-2501", kestrel, received="2026-11-02", origin_loc="MIA",
                               origin_country="USA", dest_loc="SAI", dest_country="Chile",
                               service_mode="FCL", end_customer="Lumen Paper SA", remarks="two pickups",
                               special_handling="DG")
    ids["b"] = repo.add_route(ids["three"], {"origin_loc": "MIA", "origin_country": "USA",
                                             "dest_loc": "LAX", "dest_country": "USA", "dest_alt": "Dock 4, Harbor Way",
                                             "service_mode": "FCL"})
    ids["c"] = repo.add_route(ids["three"], {"origin_loc": "MIA", "origin_country": "USA",
                                             "dest_loc": "MEM", "dest_country": "USA", "service_mode": "FCL"})
    ids["a"] = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND seq = 1",
                            (ids["three"],)).fetchone()[0]
    repo.insert("vendor_request", {"enquiry_id": ids["three"], "vendor_party_id": office,
                                   "asked_on": "2026-11-03"})
    ids["quoted"] = add_enquiry(repo, "OPP-2502", kestrel, received="2026-11-02", origin_loc="MIA",
                                origin_country="USA", dest_loc="SAI", dest_country="Chile", service_mode="Air")
    repo.set_status(ids["quoted"], "Quoted", "2026-11-04")
    ids["won"] = add_enquiry(repo, "OPP-2503", kestrel, received="2026-11-02")
    repo.set_status(ids["won"], "Won", "2026-11-05")
    ids["by_office"] = add_enquiry(repo, "OPP-2504", office, received="2026-11-02")
    ids["handled"] = add_enquiry(repo, "OPP-2505", kestrel, received="2026-11-02", handled_by="JJK")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, ids


def page(client, case):
    return client.get("/enquiries/{}".format(case)).data.decode("utf-8")


def case_row(path, case):
    conn = db.open_db(path)
    try:
        return dict(conn.execute("SELECT * FROM enquiry WHERE id = ?", (case,)).fetchone())
    finally:
        conn.close()


def rows(path, sql, *args):
    conn = db.open_db(path)
    try:
        return [tuple(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


class Forms(html.parser.HTMLParser):
    """The forms inside <main> (not the top bar's search) and the controls
    each carries, by name: what the case page itself posts."""

    def __init__(self):
        super().__init__()
        self.in_main = 0
        self.form = None
        self.forms = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "main":
            self.in_main += 1
        if not self.in_main:
            return
        if tag == "form":
            self.form = {"id": a.get("id", ""), "action": a.get("action", ""), "names": []}
            self.forms.append(self.form)
        elif tag in ("input", "select", "textarea", "button") and a.get("name"):
            owner = a.get("form")
            target = next((f for f in self.forms if owner and f["id"] == owner), self.form)
            if target is not None:
                target["names"].append(a["name"])

    def handle_endtag(self, tag):
        if tag == "main":
            self.in_main -= 1
        if tag == "form":
            self.form = None


def forms_of(body):
    parser = Forms()
    parser.feed(body)
    return parser.forms


# ----------------------------------------------------------- the units ---
def test_a_unit_posts_only_its_own_fields_and_nothing_else_is_blanked(world):
    client, path, ids = world
    before = case_row(path, ids["three"])
    # the Request card without received_on, the requester or Special handling
    ok = client.post("/enquiries/{}".format(ids["three"]), data={"end_customer": "Marten Tiles SpA"},
                     headers=SAVE_FIRST)
    assert ok.status_code == 200 and ok.get_json()["ok"] is True
    after = case_row(path, ids["three"])
    assert after["end_customer"] == "Marten Tiles SpA"
    for name in ("received_on", "requester_party_id", "remarks", "special_handling", "source_subject",
                 "commodity", "handled_by", "qty_raw"):
        assert after[name] == before[name], name
    # Special handling saves only with its marker, and can be emptied
    client.post("/enquiries/{}".format(ids["three"]), data={"remarks": "one pickup"}, headers=SAVE_FIRST)
    assert case_row(path, ids["three"])["special_handling"] == "DG"
    client.post("/enquiries/{}".format(ids["three"]), data={"special_handling_present": "1"}, headers=SAVE_FIRST)
    after = case_row(path, ids["three"])
    assert after["special_handling"] is None and after["remarks"] == "one pickup"
    assert after["end_customer"] == "Marten Tiles SpA" and after["received_on"] == before["received_on"]


def test_every_unit_has_its_own_form_and_no_form_spans_cards(world):
    client, path, ids = world
    body = page(client, ids["three"])
    assert "Save changes" not in body and 'id="enquiry-form"' not in body and 'form="enquiry-form"' not in body
    deepest, found = form_nesting(body)
    assert deepest <= 1, found
    units = re.findall(r'data-unit="([^"]+)"', body)
    for wanted in ("request", "route-{}".format(ids["a"]), "route-{}".format(ids["b"]), "route-new",
                   "special", "cargo-add", "act-quoted", "act-won", "act-lost", "act-hold", "act-note"):
        assert wanted in units, wanted
    assert len([u for u in units if u.startswith("vendor-")]) == 1
    forms = forms_of(body)
    request_form = next(f for f in forms if "requester_party_id" in f["names"])
    assert set(request_form["names"]) >= {"received_on", "requester_party_id", "end_customer", "handled_by",
                                          "remarks", "source_subject"}
    assert "commodity" not in request_form["names"] and "special_handling" not in request_form["names"]
    special = next(f for f in forms if "special_handling_present" in f["names"])
    assert set(special["names"]) == {"special_handling_present", "special_handling"}
    route_b = next(f for f in forms if f["id"] == "r{}-form".format(ids["b"]))
    assert "dest_alt" in route_b["names"] and "end_customer" not in route_b["names"]
    # the route editor's ids are its own: route B's pickers do not clash with A's
    assert 'id="r{}-dest_loc"'.format(ids["b"]) in body and 'id="r{}-dest_loc"'.format(ids["a"]) in body


def test_a_refused_request_save_writes_nothing_and_says_why_by_field(world):
    client, path, ids = world
    before = case_row(path, ids["three"])
    bad = client.post("/enquiries/{}".format(ids["three"]),
                      data={"end_customer": "Should Not Save", "handled_by": "NOWHERE"}, headers=SAVE_FIRST)
    assert bad.status_code == 422
    errors = bad.get_json()["errors"]
    assert [e["field"] for e in errors] == ["handled_by"] and "one of our office codes" in errors[0]["message"]
    assert case_row(path, ids["three"])["end_customer"] == before["end_customer"]
    # the page script keeps the unit open and puts the reason beside the field
    part = JS[JS.index("case page v3 (15a)"):]
    for text in ("function markErrors", "x.res.ok", "insertBefore(note", '"X-Save-First": "1"'):
        assert text in part, text


def test_the_request_card_reads_as_text_with_the_edit_inputs_folded(world):
    client, path, ids = world
    body = page(client, ids["three"])
    card = body[body.index('<section class="card sec-card" id="request">'):body.index('id="routes"')]
    read = card[card.index('class="u-read kv"'):card.index("<form")]
    assert "Nov 2, 2026" in read and ">Mon<" in read
    assert ">Kestrel Forwarding</a> <span class=\"m req-type\">Agent, Brazil</span>" in read
    assert "Lumen Paper SA" in read and ">JJC<" in read and "two pickups" in read
    form = card[card.index("<form"):card.index("</form>")]
    assert "hidden" in form[:form.index(">")]
    assert "Registered subject" in form and 'name="source_subject"' in form
    assert "Open party" in form and "+ New" in form
    assert "Source subject" not in card
    # one of our offices as the requester: nothing follows its name
    body = page(client, ids["by_office"])
    card = body[body.index('<section class="card sec-card" id="request">'):body.index('id="routes"')]
    assert ">JoJo JJK</a></div>" in card and "req-type" not in card


def test_the_add_subject_row_saves_without_edit(world):
    client, path, ids = world
    body = page(client, ids["three"])
    forms = forms_of(body)
    add = next(f for f in forms if f["id"] == "subject-add")
    assert add["names"] == ["subject"]
    # outside the Request unit's form: posting it touches nothing else
    request_form = next(f for f in forms if "requester_party_id" in f["names"])
    assert "subject" not in request_form["names"]
    before = case_row(path, ids["three"])
    done = client.post("/enquiries/{}/subjects".format(ids["three"]),
                       data={"subject": "RE: PALLET RATE // MIA - SAI // REVISED"}, follow_redirects=True)
    assert b"Subject added" in done.data
    after = case_row(path, ids["three"])
    assert {k: after[k] for k in ("end_customer", "received_on", "remarks")} == \
        {k: before[k] for k in ("end_customer", "received_on", "remarks")}
    assert "RE: PALLET RATE // MIA - SAI // REVISED" in page(client, ids["three"])


def test_a_route_is_a_unit_added_struck_out_and_put_back(world):
    client, path, ids = world
    body = page(client, ids["three"])
    assert "?route=" not in body                       # no editor page: the editor opens in its row
    routes_card = body[body.index('id="routes"'):body.index('id="cargo"')]
    assert routes_card.count('class="r-edit u-edit" hidden') == 4      # three routes and the new one
    assert "Route D" in routes_card and 'data-open-unit="route-new"' in routes_card
    client.post("/enquiries/{}/routes".format(ids["three"]), data={
        "form": "route", "route_id": "", "origin_country": "USA", "origin_loc": "MIA", "dest_country": "China",
        "dest_loc": "NGB", "service_mode": "FCL", "incoterm": "FOB"}, follow_redirects=True)
    d = rows(path, "SELECT id, seq FROM enquiry_route WHERE enquiry_id = ? AND dest_loc = 'NGB'", ids["three"])
    assert d and d[0][1] == 4
    done = client.post("/enquiries/{}/routes".format(ids["three"]), data={"route_id": d[0][0], "action": "void"},
                       follow_redirects=True).data.decode("utf-8")
    assert "Route D struck out." in done and "Put back route D: NGB" in done
    client.post("/enquiries/{}/routes".format(ids["three"]), data={"route_id": d[0][0], "action": "unvoid"})
    assert rows(path, "SELECT voided FROM enquiry_route WHERE id = ?", d[0][0]) == [(0,)]
    # saving route B writes route B only
    before = case_row(path, ids["three"])
    ok = client.post("/enquiries/{}/routes".format(ids["three"]), data={
        "form": "route", "route_id": ids["b"], "origin_country": "USA", "origin_loc": "MIA", "dest_country": "USA",
        "dest_loc": "LAX", "dest_alt": "Dock 7, Harbor Way", "service_mode": "FCL"}, headers=SAVE_FIRST)
    assert ok.get_json()["ok"] is True
    assert rows(path, "SELECT dest_alt FROM enquiry_route WHERE id = ?", ids["b"]) == [("Dock 7, Harbor Way",)]
    assert case_row(path, ids["three"])["end_customer"] == before["end_customer"]


def test_a_vendor_row_is_a_unit_with_its_edit_folded_under_it(world):
    client, path, ids = world
    body = page(client, ids["three"])
    vid = rows(path, "SELECT id FROM vendor_request WHERE enquiry_id = ?", ids["three"])[0][0]
    unit = body[body.index('data-unit="vendor-{}"'.format(vid)):]
    unit = unit[:unit.index("</tbody>")]
    assert "data-edit" in unit and 'id="vendor-edit-{}" hidden'.format(vid) in unit and 'id="vendor-{}"'.format(vid) in unit
    assert "data-cancel" in unit
    ok = client.post("/enquiries/{}/vendors".format(ids["three"]), data={
        "vendor_request_id": vid, "action": "save", "asked_on": "2026-11-03", "replied_on": "2026-11-06",
        "their_ref": "KX-4410", "notes": "rate held to month end"}, follow_redirects=True)
    assert ok.status_code == 200
    assert rows(path, "SELECT replied_on, their_ref FROM vendor_request WHERE id = ?", vid) == [("2026-11-06", "KX-4410")]


def test_the_timeline_folds_voided_events_and_each_event_has_its_own_edit(world):
    client, path, ids = world
    client.post("/enquiries/{}/status".format(ids["three"]), data={"status": "note", "at": "2026-11-06",
                                                                    "note": "asked two lines"})
    client.post("/enquiries/{}/status".format(ids["three"]), data={"status": "note", "at": "2026-11-07",
                                                                    "note": "one line answered"})
    event = rows(path, "SELECT id FROM status_event WHERE enquiry_id = ? AND note = 'one line answered'", ids["three"])[0][0]
    client.post("/enquiries/{}/events/{}".format(ids["three"], event), data={"action": "void"})
    body = page(client, ids["three"])
    assert '<details class="voided-events">' in body and "show 1 voided" in body
    assert "asked two lines" in body
    other = rows(path, "SELECT id FROM status_event WHERE enquiry_id = ? AND note = 'asked two lines'", ids["three"])[0][0]
    li = body[body.index('data-unit="event-{}"'.format(other)):]
    li = li[:li.index("</li>")]
    assert "data-edit" in li and 'id="event-{}" hidden'.format(other) in li and "data-cancel" in li


# ----------------------------------------------------- the status actions ---
def acts(body):
    return re.findall(r'<button type="button" class="act" data-act="([a-z]+)">', body)


def test_each_status_offers_its_own_actions_and_set_status_is_gone(world):
    client, path, ids = world
    # C1: Cancelled after Lost on an open case; a Won case can still be cancelled
    assert acts(page(client, ids["three"])) == ["quoted", "waiting", "won", "lost", "cancelled", "hold", "note"]
    assert acts(page(client, ids["quoted"])) == ["requoted", "won", "lost", "cancelled", "hold", "note"]
    assert acts(page(client, ids["won"])) == ["cancelled", "note"]
    assert acts(page(client, ids["handled"])) == ["quoted", "won", "lost", "cancelled", "hold", "note"]
    client.post("/enquiries/{}/status".format(ids["handled"]), data={
        "status": "Lost", "at": "2026-11-06", "closed_reason": "Price too high"})
    assert acts(page(client, ids["handled"])) == ["note"]
    body = page(client, ids["quoted"])
    assert "Set status" not in body and 'id="status-select"' not in body and 'id="status-form"' not in body
    # one way to record a re-quote: the action; the header has no Re-quoted button
    head = body[body.index('<div class="card head" id="head">'):body.index('<div class="facts">')]
    assert "Re-quoted" not in head and "requote" not in head
    status = body[body.index('id="status"'):body.index('id="followups"')]
    assert 'id="requote-form"' in status and status.count('name="requote_reason"') == 1
    # each action's fields are folded under the row until it is pressed
    for name in ("requoted", "won", "lost", "hold", "note"):
        block = status[status.index('data-unit="act-{}"'.format(name)):]
        assert "u-edit af" in block[:block.index(">", block.index("<form"))] and " hidden>" in block[:block.index("</form>")]


def test_quoted_and_note_record_their_events(world):
    client, path, ids = world
    client.post("/enquiries/{}/status".format(ids["three"]), data={"status": "Quoted", "at": "2026-11-05",
                                                                    "note": "sent both options"})
    assert case_row(path, ids["three"])["status"] == "Quoted"
    client.post("/enquiries/{}/status".format(ids["three"]), data={"status": "note", "at": "2026-11-06",
                                                                    "note": "customer reading it"})
    assert rows(path, "SELECT kind, note FROM status_event WHERE enquiry_id = ? ORDER BY id DESC LIMIT 2", ids["three"]) == [
        ("note", "customer reading it"), ("status", "sent both options")]
    refused = client.post("/enquiries/{}/status".format(ids["three"]), data={"status": "note", "at": "2026-11-06"},
                          headers=SAVE_FIRST)
    assert refused.status_code == 422 and refused.get_json()["errors"][0]["field"] == "note"


def test_won_on_three_routes_with_one_unticked_and_given_a_reason(world):
    client, path, ids = world
    status = page(client, ids["three"])
    won = status[status.index('data-unit="act-won"'):]
    won = won[:won.index("</form>")]
    assert won.count('name="won_route"') == 3 and won.count(" checked>") >= 3
    assert 'name="rate_used"' in won and 'name="at"' in won and 'name="note"' in won
    done = client.post("/enquiries/{}/status".format(ids["three"]), data={
        "status": "Won", "at": "2026-11-08", "routes_present": "1",
        "won_route": [str(ids["a"]), str(ids["c"])], "lost_reason_{}".format(ids["b"]): "Price too high",
        "rate_used": "office", "office_party_id": str(ids["office"]), "note": "booked for December"},
        headers=SAVE_FIRST)
    assert done.status_code in (200, 302)
    assert rows(path, "SELECT seq, result, lost_reason FROM enquiry_route WHERE enquiry_id = ? ORDER BY seq", ids["three"]) == [
        (1, "Won", None), (2, "Lost", "Price too high"), (3, "Won", None)]
    row = case_row(path, ids["three"])
    assert (row["status"], row["rate_used"], row["awarded_vendor_id"]) == ("Won", "office", ids["office"])
    # nothing ticked is refused, as JSON beside the routes
    other = client.post("/enquiries/{}/status".format(ids["quoted"]), data={
        "status": "Won", "at": "2026-11-08", "routes_present": "1", "rate_used": "own"}, headers=SAVE_FIRST)
    assert other.status_code == 422 and other.get_json()["errors"][0]["field"] == "won_route"


def test_the_won_form_lists_only_routes_without_a_result(world):
    client, path, ids = world
    client.post("/enquiries/{}/routes".format(ids["three"]), data={
        "form": "route", "route_id": ids["b"], "origin_country": "USA", "origin_loc": "MIA", "dest_country": "USA",
        "dest_loc": "LAX", "service_mode": "FCL", "result": "Lost", "lost_reason": "Rate too slow"})
    body = page(client, ids["three"])
    won = body[body.index('data-unit="act-won"'):]
    won = won[:won.index("</form>")]
    assert won.count('name="won_route"') == 2 and 'name="won_route" value="{}"'.format(ids["b"]) not in won


def test_lost_on_a_partly_decided_case_applies_to_the_routes_without_a_result(world):
    client, path, ids = world
    client.post("/enquiries/{}/routes".format(ids["three"]), data={
        "form": "route", "route_id": ids["b"], "origin_country": "USA", "origin_loc": "MIA", "dest_country": "USA",
        "dest_loc": "LAX", "service_mode": "FCL", "result": "Lost", "lost_reason": "Rate too slow"})
    client.post("/enquiries/{}/status".format(ids["three"]), data={
        "status": "Lost", "at": "2026-11-09", "closed_reason": "Price too high",
        "lost_their_price": "USD 2,100 all-in", "lost_our_price": "USD 2,450 all-in",
        "note": "their line had a direct sailing"})
    assert rows(path, "SELECT seq, result, lost_reason FROM enquiry_route WHERE enquiry_id = ? ORDER BY seq", ids["three"]) == [
        (1, "Lost", "Price too high"), (2, "Lost", "Rate too slow"), (3, "Lost", "Price too high")]
    row = case_row(path, ids["three"])
    assert (row["status"], row["closed_reason"], row["lost_their_price"], row["lost_our_price"]) == (
        "Lost", "Price too high", "USD 2,100 all-in", "USD 2,450 all-in")
    assert rows(path, "SELECT note FROM status_event WHERE enquiry_id = ? AND status = 'Lost' "
                      "AND kind = 'status' ORDER BY id DESC LIMIT 1", ids["three"]) == [("their line had a direct sailing",)]
    # a closed case offers only Note
    assert acts(page(client, ids["three"])) == ["note"]


def test_each_closing_status_only_takes_its_own_reasons(world):
    client, path, ids = world
    for status, reason in (("Lost", "Registered in error"), ("Dropped", "Price too high")):
        refused = client.post("/enquiries/{}/status".format(ids["three"]),
                              data={"status": status, "at": "2026-11-06", "closed_reason": reason}, headers=SAVE_FIRST)
        assert refused.status_code == 422 and "is not a {} reason".format(status) in refused.get_json()["errors"][0]["message"]
        assert case_row(path, ids["three"])["status"] == "New"
    body = page(client, ids["three"])
    lost = body[body.index('data-unit="act-lost"'):]
    lost = lost[:lost.index("</form>")]
    assert "Price too high" in lost and "Registered in error" not in lost
    # Dropped stays in More
    more = body[body.index('<details class="more-menu">'):]
    assert "Mark as duplicate of" in more and "Mark as registered in error" in more


def test_two_requoted_on_one_day_give_two_rounds(world):
    client, path, ids = world
    for note in ("option with transshipment", "direct option"):
        done = client.post("/enquiries/{}/requote".format(ids["quoted"]),
                           data={"at": "2026-11-06", "note": note, "requote_reason": "Lower rate"}, headers=SAVE_FIRST)
        assert done.status_code in (200, 302)
    conn = db.open_db(path)
    assert conn.execute("SELECT quote_rounds FROM v_enquiry WHERE id = ?", (ids["quoted"],)).fetchone()[0] == 3
    conn.close()
    refused = client.post("/enquiries/{}/requote".format(ids["three"]), data={"at": "2026-11-06"}, headers=SAVE_FIRST)
    assert refused.status_code == 422 and "Re-quote is for a quoted case" in refused.get_json()["errors"][0]["message"]


def test_on_hold_needs_an_until_after_today(world):
    client, path, ids = world
    url = "/enquiries/{}/status".format(ids["three"])
    today = dt.date.today().isoformat()
    for until, why in (("", "needs an Until date"), (today, "after today"), ("2020-01-01", "after today")):
        refused = client.post(url, data={"status": "hold", "at": today, "hold_reason": "tender closes later",
                                         "hold_until": until}, headers=SAVE_FIRST)
        assert refused.status_code == 422, until
        err = refused.get_json()["errors"][0]
        assert err["field"] == "hold_until" and why in err["message"]
    assert case_row(path, ids["three"])["hold_reason"] is None
    client.post(url, data={"status": "hold", "at": today, "hold_reason": "tender closes later", "hold_until": FUTURE})
    row = case_row(path, ids["three"])
    assert (row["hold_reason"], row["hold_until"], row["status"]) == ("tender closes later", FUTURE, "New")
    body = page(client, ids["three"])
    hold = body[body.index('data-unit="act-hold"'):]
    hold = hold[:hold.index("</form>")]
    assert 'name="hold_until"' in hold and "required" in hold and "+2 weeks" in hold and "+1 month" in hold and "End of month" in hold
    # Release hold, by hand
    done = client.post("/enquiries/{}/hold/release".format(ids["three"]), follow_redirects=True)
    assert b"Hold released" in done.data and b"Release hold" not in done.data
    assert case_row(path, ids["three"])["hold_reason"] is None


# ------------------------------------------------------------ the script ---
def test_the_page_script_saves_each_unit_alone_and_asks_before_leaving():
    part = JS[JS.index("case page v3 (15a)"):]
    for text in ('"X-Save-First": "1"', 'window.addEventListener("beforeunload"', "twin.replaceWith(u)",
                 "new DOMParser()", "function kept(u)", 'e.key === "Escape"', 'e.key === "Enter"',
                 "data-nav", "history.replaceState"):
        assert text in part, text
    # 12d's save-first is gone: a write never saves another unit
    assert "saveFirst" not in JS and "sendKeeping" not in JS and "tracked()" not in JS
    # 13c's number check still stops a form before it is sent
    assert 'window.addEventListener("submit"' in JS and '"needs a number"' in JS


# -------------------------------------------- 13e tidy-up: no scope field ---
def test_the_case_page_has_no_scope_field_in_its_own_forms(world):
    client, path, ids = world
    body = page(client, ids["three"])
    names = [n for f in forms_of(body) for n in f["names"]]
    assert "scope" not in [n for f in forms_of(body) if not f["action"].endswith("/scope") for n in f["names"]]
    assert "requester_party_id" in names                      # the parser does see the case page's forms
    assert "Switch scope to Out" in body
    client.post("/enquiries/{}".format(ids["three"]), data={"end_customer": "Marten Tiles SpA"})
    assert case_row(path, ids["three"])["scope"] in (None, "In")
    client.post("/enquiries/{}/scope".format(ids["three"]), data={"scope": "Out"})
    assert case_row(path, ids["three"])["scope"] == "Out"
    assert rows(path, "SELECT old_value, new_value FROM change_log WHERE table_name = 'enquiry' AND row_id = ? "
                      "AND field = 'scope'", ids["three"])[-1][1] == "Out"


def test_the_register_page_has_no_scope_or_owner_field_in_its_own_forms(world):
    client, path, ids = world
    body = client.get("/enquiries/new").data.decode("utf-8")
    names = [n for f in forms_of(body) for n in f["names"]]
    assert "received_on" in names and "scope" not in names and "owner" not in names


# --------------------------- what the replaced tests checked, still true ---
def test_the_header_rail_and_bottom_bar(world):
    client, path, ids = world
    body = page(client, ids["quoted"])
    assert '<main class="case-page">' in body and "Inquiries</a> &rsaquo; <b>OPP-2502</b>" in body
    # WA1: the number leads the h1
    assert re.search(r"<h1><span class=\"h-opp\">.*?>OPP-2502</b>.*?</span></span>\s*<span class=\"h-dot\">&middot;"
                     r"</span> <span class=\"h-title\">MIA to SAI</span>\s*<span class=\"pill big s-quoted\">Quoted</span>",
                     body, re.S)
    assert 'requester <a class="req-box"' in body and ">Kestrel Forwarding</a>" in body
    for anchor in ("request", "routes", "cargo", "vendors", "status", "followups", "mail"):
        assert 'id="{}"'.format(anchor) in body and 'href="#{}"'.format(anchor) in body
    assert 'id="update-outlook"' in body
    bar = body[body.index('class="bottombar"'):]
    assert "Mark as duplicate of" in bar and "Mark as registered in error" in bar and "Back to list" in bar
    assert "Save changes" not in bar and 'data-unsaved="Not saved yet: {} · Save or Cancel in the card"' in bar
    assert "use Re-quote" not in body


def test_the_status_card_runs_current_line_actions_timeline_newest_first(world):
    client, path, ids = world
    client.post("/enquiries/{}/status".format(ids["quoted"]), data={"status": "note", "at": "2026-11-06",
                                                                     "note": "they are comparing"})
    body = page(client, ids["quoted"])
    card = body[body.index('id="status"'):body.index('id="followups"')]
    assert card.index('class="current-status"') < card.index('class="acts-row"') < card.index('<ol class="timeline">')
    assert '<h3 class="tl-head">Timeline</h3>' in card and "since 2026-11-04" in card
    whens = re.findall(r'<span class="mono when">([^<]*)</span>', card[card.index('<ol class="timeline">'):])
    assert whens == sorted(whens, reverse=True) and whens[0] == "2026-11-06"


def test_the_route_editor_is_two_panels_and_the_register_page_shares_them(world):
    client, path, ids = world
    client.post("/enquiries/{}/routes".format(ids["three"]), data={
        "form": "route", "route_id": ids["a"], "origin_country": "USA", "origin_loc": "MIA", "dest_country": "Chile",
        "dest_loc": "SAI", "service_mode": "FCL", "incoterm": "EXW"})
    body = page(client, ids["three"])
    editor = body[body.index('id="r{}-form"'.format(ids["a"])):]
    editor = editor[:editor.index("</form>")]
    origin = editor[editor.index('id="r{}-route-origin"'.format(ids["a"])):editor.index('id="r{}-route-dest"'.format(ids["a"]))]
    dest = editor[editor.index('id="r{}-route-dest"'.format(ids["a"])):]
    assert "<h3>Origin</h3>" in origin and "<h3>Destination</h3>" in dest
    # batch 23: a place picker (its places, and a place added in its small form)
    for name in ("origin_country", "origin_loc", "origin_loc_new", "origin_alt"):
        assert 'name="{}"'.format(name) in origin, name
    for name in ("dest_country", "dest_loc", "dest_loc_new", "dest_alt"):
        assert 'name="{}"'.format(name) in dest, name
    assert 'name="remarks"' not in editor                               # Remarks: the Request card's
    assert 'class="f required" data-field="origin_alt"' in editor       # EXW: pickup address required
    css = (Path(__file__).resolve().parents[1] / "opplist" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    assert "background: #f6f7f9; border-radius: 8px; padding: 12px" in css
    new = client.get("/enquiries/new").data.decode("utf-8")
    assert '<div class="route-panels">' in new and 'id="origin_loc"' in new
    deepest, found = form_nesting(new)
    assert deepest <= 1, found


def test_dropped_from_more_with_its_own_reason(world):
    client, path, ids = world
    client.post("/enquiries/{}/status".format(ids["three"]),
                data={"status": "Dropped", "at": "2026-11-06", "closed_reason": "Registered in error"})
    row = case_row(path, ids["three"])
    assert (row["status"], row["closed_reason"]) == ("Dropped", "Registered in error")


def test_a_hold_shows_on_the_case_and_the_list_and_its_column_can_be_hidden(world):
    client, path, ids = world
    reason = ("customer waits for the tender result of the second quarter, back to us when the board "
              "has met and the budget for the new line is signed")
    assert len(reason) > 120
    client.post("/enquiries/{}/status".format(ids["three"]),
                data={"status": "hold", "at": dt.date.today().isoformat(), "hold_reason": reason, "hold_until": FUTURE})
    body = page(client, ids["three"])
    line = body[body.index('<div class="current-status">'):]
    line = line[:line.index("</div>")]
    pill = line[line.index('<span class="pill s-sourcing hold"'):]
    pill = pill[:pill.index("</span>")]
    assert 'title="on hold: {} · until {}"'.format(reason, FUTURE) in pill
    shown = pill[pill.index(">") + 1:]
    assert shown.startswith("on hold: " + reason[:60]) and "…" in shown and reason not in shown
    assert "Release hold" in line
    listing = client.get("/enquiries?view=open").data.decode("utf-8")
    assert ">on hold</span>" in listing
    cols = re.findall(r'<th[^>]*data-col="([a-z_]+)"', listing)
    assert "hold" in cols
    client.post("/enquiries/columns", data={"order": ",".join(cols), "action": "show",
                                            "visible": [c for c in cols if c != "hold"]})
    listing = client.get("/enquiries").data.decode("utf-8")
    assert "hold" not in re.findall(r'<th[^>]*data-col="([a-z_]+)"', listing)
