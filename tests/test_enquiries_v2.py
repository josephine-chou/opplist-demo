"""Enquiries v2, the site-wide style rules and the case page's Request card
(2026-09-25). The list's grouping, sorting and filters run in the browser;
enquiries_client reads the rendered rows the way the page script does."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import enquiries_client
from conftest import add_enquiry, add_party, seed_reference
from opplist import db, shipments
from opplist.web import VIEW_WHERE, create_app

TODAY = "2026-09-24"          # a Thursday
CSS = (Path(__file__).resolve().parents[1] / "opplist" / "web" / "static" / "app.css").read_text(encoding="utf-8")
JS = (Path(__file__).resolve().parents[1] / "opplist" / "web" / "static" / "app.js").read_text(encoding="utf-8")
REMOVED = ("who asked, and when", "fields that do not apply to the mode are dimmed, never hidden",
           "who we asked for the buying rate", "the registered one, then every thread attached by hand",
           "No cargo lines: the fields below are the single consignment.",
           "its thread key joins this case", "where it stands; the history below, newest first",
           "FU1 after 1 working day", "from the archive, synced", "not live Outlook")


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "enq.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    klx = add_party(repo, "KLX", country="USA")
    seavio = add_party(repo, "Seavio", country="USA")
    ids = {}

    def case(opp, party, received, mode, **extra):
        return add_enquiry(repo, opp, party, received=received, service_mode=mode, origin_loc="MIA",
                           origin_country="USA", dest_loc="SAI", dest_country="Chile", **extra)

    ids["r1"] = case("OPP-1601", tolvex, "2026-09-10", "Air")                      # New, nobody asked
    ids["r9"] = case("OPP-1602", tolvex, "2026-09-15", "FCL")                      # New, a vendor answered
    repo.insert("vendor_request", {"enquiry_id": ids["r9"], "vendor_party_id": seavio,
                                   "asked_on": "2026-09-16", "replied_on": "2026-09-18"})
    ids["fu1"] = case("OPP-1603", klx, "2026-09-01", "Air")                        # quoted Tuesday: FU1 due
    repo.set_status(ids["fu1"], "Quoted", "2026-09-22")
    ids["won"] = case("OPP-1604", klx, "2026-08-05", "LCL")
    repo.set_status(ids["won"], "Quoted", "2026-08-10")
    repo.set_status(ids["won"], "Won", "2026-08-20", rate_used="own")
    ids["lost"] = case("OPP-1605", tolvex, "2026-07-10", "FCL")
    repo.set_status(ids["lost"], "Lost", "2026-07-30", closed_reason="Price too high")
    ids["dropped"] = case("OPP-1606", tolvex, "2026-08-12", "Air")
    repo.set_status(ids["dropped"], "Dropped", "2026-08-13", closed_reason="Registered in error")
    ids["round2"] = case("OPP-1607", klx, "2026-09-20", "FCL")
    repo.set_status(ids["round2"], "Quoted", "2026-09-21")
    repo.requote(ids["round2"], "2026-09-22")
    # a request of two lanes
    ids["lane1"] = case("OPP-1608", tolvex, "2026-09-02", "FCL")
    ids["lane2"] = case("OPP-1609", tolvex, "2026-09-02", "FCL")
    repo.update("enquiry", ids["lane2"], {"group_id": ids["lane1"]})
    repo.update("enquiry", ids["lane1"], {"group_id": ids["lane1"]})
    repo.commit()
    conn.close()
    import opplist.web as web_module
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, ids


def page(client, url):
    return enquiries_client.get(client, url)


# ----------------------------------------------------------------- groups --
def test_status_groups_come_in_their_order(world):
    client, path, ids = world
    p = page(client, "/enquiries?view=all&dropped=1")
    assert [k for k, _ in p.groups] == ["New", "Quoted", "Won", "Lost", "Dropped"]
    # received, newest first; one day's cases by number, highest first
    assert dict(p.groups)["New"] == ["OPP-1602", "OPP-1601", "OPP-1609", "OPP-1608"]


def test_requester_and_mode_groups_are_largest_first_and_months_newest_first(world):
    client, path, ids = world
    by_req = page(client, "/enquiries?view=all&group=requester")
    assert [k for k, _ in by_req.groups] == ["Tolvex", "KLX"]          # 5 shown against 3
    by_mode = page(client, "/enquiries?view=all&group=mode")
    assert [k for k, _ in by_mode.groups][0] == "FCL"
    by_month = page(client, "/enquiries?view=all&group=month")
    assert [k for k, _ in by_month.groups] == ["2026-09", "2026-08", "2026-07"]


def test_the_column_of_the_grouping_is_hidden(world):
    client, path, ids = world
    body = client.get("/enquiries?view=all").data.decode("utf-8")
    pool = body[body.index('id="enq-pool"'):body.index("</table>", body.index('id="enq-pool"'))]
    assert 'class="c-status col-status"' in pool and 'class="c-requester col-req req"' in pool
    assert 'class="c-service_mode col-mode"' in pool
    for rule in (".enquiries-page.group-status .col-status", ".enquiries-page.group-requester .col-req",
                 ".enquiries-page.group-mode .col-mode"):
        assert rule in CSS, rule
    assert 'main.classList.toggle("group-" + g, state.group === g)' in JS
    assert "var LIMIT = 15, BOARD_LIMIT = 12" in JS                # 15 rows, then Show all; 12 cards


def test_the_last_event_is_our_own_last_action(world):
    """2026-09-25: the newest live status change (re-quote and close
    included), follow-up, rate request sent or note typed by hand - not the
    requester's reply, not a vendor's answer, not a backfill's event."""
    client, path, ids = world
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    # after the quote of Sep 22: a follow-up, then their reply and a backfill's note
    repo.add_followup(ids["fu1"], "2026-09-23")
    repo.add_note(ids["fu1"], "2026-09-24", "Reply from Ana (needs an answer): can you redo it?", source="mail")
    repo.insert("status_event", {"enquiry_id": ids["fu1"], "at": "2026-09-24", "status": "Quoted",
                                 "kind": "note", "note": "fixed a lane", "source": "import"})
    # a note typed by hand is ours
    repo.add_note(ids["r1"], "2026-09-12", "called the client")
    repo.commit()
    conn.close()
    body = client.get("/enquiries?view=all").data.decode("utf-8")

    def cell(case_id):
        start = body.index('data-id="{}"'.format(case_id))
        row = body[start:body.index("</tr>", start)]
        return row[row.index('class="c-last_event_on last"'):]

    assert '<span class="lbl fill tone-navy">FU1</span><span class="on">Sep 23</span>' in cell(ids["fu1"])
    assert '<span class="lbl fill tone-grey">Note</span><span class="on">Sep 12</span>' in cell(ids["r1"])
    # the vendor answered on Sep 18; our last action is the rate request of Sep 16
    assert '<span class="lbl fill tone-amber">Rate request</span><span class="on">Sep 16</span>' in cell(ids["r9"])
    assert '<span class="lbl fill tone-green">Won</span>' in cell(ids["won"])
    assert '<span class="lbl fill tone-rose">Lost</span>' in cell(ids["lost"])
    assert '<span class="lbl fill tone-accent">Re-quoted</span>' in cell(ids["round2"])
    assert '<span class="lbl fill tone-accent">round 2</span>' in body
    # the Last event sort reads the same date
    start = body.index('data-id="{}"'.format(ids["fu1"]))
    assert 'data-last="2026-09-23"' in body[start - 1200:start + 1200]


def test_the_list_has_no_next_and_asks_tracking_nothing(world, monkeypatch):
    client, path, ids = world
    from opplist import tracking

    def boom(*a, **k):
        raise AssertionError("the Enquiries page must not run the Tracking computation")

    monkeypatch.setattr(tracking, "page_model", boom)
    monkeypatch.setattr(tracking, "run", boom)
    body = client.get("/enquiries?view=all").data.decode("utf-8")
    assert "c-next" not in body and ">Next<" not in body
    for label in ("FU1 due", "Set status", "Vendor replied", "Chase vendor"):
        assert label not in body, label
    menu = body[body.index('class="columns-form menu"'):body.index("</form>", body.index('class="columns-form menu"'))]
    assert "Next" not in menu
    assert not hasattr(tracking, "next_steps")


def test_the_view_counts_are_the_old_tabs(world):
    client, path, ids = world
    conn = db.open_db(path)
    body = client.get("/enquiries").data.decode("utf-8")
    for view, clause in VIEW_WHERE.items():
        parts = [c for c in (clause, "scope = 'In'") if c] + ([] if view == "closed" else ["status <> 'Dropped'"])
        want = conn.execute("SELECT COUNT(*) FROM v_enquiry WHERE " + " AND ".join(parts)).fetchone()[0]
        assert re.search(r'data-view="{}"[^>]*>[^<]*<b>{}</b>'.format(view, want), body), view
        assert len(page(client, "/enquiries?view=" + view).shown) == want, view
    conn.close()


def test_the_query_string_sets_the_state(world):
    client, path, ids = world
    p = page(client, "/enquiries?view=all&group=requester&sort=received_on&dir=asc&mode=FCL")
    assert [k for k, _ in p.groups] == ["Tolvex", "KLX"]
    assert dict(p.groups)["Tolvex"] == ["OPP-1605", "OPP-1609", "OPP-1608", "OPP-1602"]     # oldest first
    assert dict(p.groups)["KLX"] == ["OPP-1607"]
    assert "OPP-1601" not in p.shown                                     # Air, filtered out
    assert page(client, "/enquiries?view=all&latam=1").shown == page(client, "/enquiries?view=all&latam=Y").shown
    assert page(client, "/enquiries?view=all&month=2026-07").shown == ["OPP-1605"]
    conn = db.open_db(path)
    klx = conn.execute("SELECT id FROM party WHERE key = 'KLX'").fetchone()[0]
    conn.close()
    assert set(page(client, "/enquiries?view=all&requester={}".format(klx)).shown) == {"OPP-1603", "OPP-1604", "OPP-1607"}
    # 'dir' is the sort direction; a direction's name there is the filter
    assert page(client, "/enquiries?view=all&dir=US Export").shown ==         page(client, "/enquiries?view=all&direction=US Export").shown
    assert "URLSearchParams(location.search)" in JS and "history.replaceState" in JS
    assert 'href="/enquiries?view=closed"' in client.get("/enquiries?view=open").data.decode("utf-8")

# ---------------------------------------------------------- style rules --
def test_no_latam_label_and_no_left_bar_on_the_three_pages(world):
    client, path, ids = world
    for url in ("/enquiries", "/tracking", "/enquiries/{}".format(ids["r1"])):
        body = client.get(url).data.decode("utf-8")
        # SC1: the one LATAM mark is the case's chip (the case page's header, the list's row)
        assert "p-latam" not in body and ">LATAM<" not in body.replace('latam-chip">LATAM<', ""), url
        assert "border-left" not in body, url
    assert ".p-latam { display: none !important; }" in CSS
    assert ":is(.enquiries-page, .tracking-page, .case-page, .form-page) :is(.card, .tracking-section) { border-left-width: 1px; }" in CSS
    # the tones, once
    for tone in ("navy", "accent", "green", "rose", "amber", "grey"):
        assert CSS.count("--tone-{}:".format(tone)) == 1, tone
    assert "border-radius: 5px; background: transparent !important;" in CSS


def test_the_group_header_bands(world):
    # 2026-09-25 follow-up: #86A8C4 with a navy title (was #6490b3 with a white one)
    assert ".enquiries-page .g-head.band-steel { background: #86A8C4; }" in CSS
    assert ".enquiries-page .g-head.band-steel h2 { color: #002060; }" in CSS
    assert ".enquiries-page .g-head.band-steel .count { background: #fff !important;" in CSS
    assert 'Quoted: ["accent", "accent"], Won: ["green", "green"]' in JS
    assert 'New: ["grey", "steel"]' in JS and 'Dropped: ["grey", "steel"]' in JS


# ------------------------------------------------------------- case page --
def test_the_request_card_lists_other_lanes_only_when_there_are_some(world):
    client, path, ids = world
    body = client.get("/enquiries/{}".format(ids["lane1"])).data.decode("utf-8")
    card = body[body.index('id="request"'):body.index('id="cargo"')]
    assert "Lanes in this request" not in card and "OPP-1609" not in card           # in More now (12b)
    assert "OPP-1609 &middot;" in body[body.index('<details class="more-menu">'):]
    body = client.get("/enquiries/{}".format(ids["r1"])).data.decode("utf-8")
    card = body[body.index('id="request"'):body.index('id="cargo"')]
    assert "Lanes in this request" not in card and "only this one" not in card
    more = body[body.index('<details class="more-menu">'):]
    assert ">Add a lane</a>" not in more[:more.index("</details>")]         # v33: routes instead
    assert "Same request as" not in more[:more.index("</details>")]


def test_the_condensed_line_boxes_the_requester(world):
    client, path, ids = world
    body = client.get("/enquiries/{}".format(ids["fu1"])).data.decode("utf-8")
    assert '<span class="cond-line"><span class="req-box">KLX</span>' in body
    assert '<span class="lbl tone-navy">Air</span>' in body
    assert 'requester <a class="req-box"' in body
    assert ".case-page .req-box { display: inline; border: 0;" in CSS


def test_the_helper_lines_are_gone_everywhere(world):
    client, path, ids = world
    pages = [client.get("/enquiries/{}".format(ids["fu1"])).data.decode("utf-8"),
             client.get("/enquiries/new").data.decode("utf-8")]
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    sid = shipments.start(repo, ids["won"], "MIA to SAI")
    repo.commit()
    conn.close()
    pages.append(client.get("/shipments/{}".format(sid)).data.decode("utf-8"))
    for body in pages:
        for text in REMOVED:
            assert text not in body, text
    from opplist.web.text import TEXT
    for key in ("sec_request_hint", "sec_cargo_hint", "sec_vendors_hint", "subjects_hint", "cargo_none",
                "subj_add_hint", "sec_status_hint", "sec_followups_hint", "mail_hint"):
        assert key not in TEXT, key


# --------------------------------------------------------- follow-ups (2026-09-25) --
def test_the_case_page_texts_that_went_and_the_ones_that_changed(world):
    client, path, ids = world
    gone = ("Agent rate = ", "newest first", "Copy the text into a reply in Outlook",
            "the pickup address is required", "country and location on both sides",
            "select the rate request we sent in Outlook first", "A revised quote went out",
            "Daily scan", "nothing to chase")
    for case in ("fu1", "won", "r1", "lost"):
        body = client.get("/enquiries/{}".format(ids[case])).data.decode("utf-8")
        for text in gone:
            assert text not in body, (case, text)
    body = client.get("/enquiries/{}".format(ids["won"])).data.decode("utf-8")
    assert ">Closed Aug 20</p>" in body
    body = client.get("/enquiries/{}".format(ids["r1"])).data.decode("utf-8")
    assert "not quoted yet" in body and "No vendor asked yet." in body
    from opplist.web.text import TEXT
    for key in ("rate_used_hint", "newest_first", "fu_hint", "vendor_outlook_hint", "requote_hint", "scan_line"):
        assert key not in TEXT, key
    # an EXW case with no pickup place is still refused, with its message
    from opplist.web import forms
    errors = {}
    forms.validate({"incoterm": "EXW", "service_mode": "Air"}, errors)
    assert errors.get("origin_alt") == "EXW needs the pickup place"


def test_the_style_follow_ups(world):
    client, path, ids = world
    # 3: the version badge sits below the content on Enquiries and Tracking
    assert "main.enquiries-page, main.tracking-page { padding-bottom: 72px; }" in CSS
    assert "body:has(main.enquiries-page) footer.build, body:has(main.tracking-page) footer.build {" in CSS
    # 5: the lighter band, navy title, white dot, dark-blue hint and chevron
    assert ".enquiries-page .g-head.band-steel { background: #86A8C4; }" in CSS
    assert ".enquiries-page .g-head.band-steel h2 { color: #002060; }" in CSS
    assert ".enquiries-page .g-head.band-steel .dot { background: #fff !important; }" in CSS
    assert ".enquiries-page .g-head.band-steel .hint, .enquiries-page .g-head.band-steel .chev { color: #1f3f66; }" in CSS
    # 6: filled labels on the case page, one size
    assert ".case-page :is(.pill, .lbl, .count, .l-step), .enquiries-page .lbl.fill {" in CSS
    assert "color-mix(in srgb, var(--tone, var(--tone-grey)) 13%, #fff) !important" in CSS
    assert ".case-page .pill.big, .case-page h1 .pill.big { font-size: 11px; padding: 3px 8px; }" in CSS
    assert ".case-page h1 .pill.big { margin-left: 8px; }" in CSS
    # 7: the OPP number in navy, underlined on hover
    assert ".tracking-page .opp { color: #002060; }" in CSS
    assert "color: #002060; text-decoration: none; }" in CSS
    assert ".enquiries-page .opp:hover, .tracking-page .opp:hover { text-decoration: underline; }" in CSS
    body = client.get("/enquiries?view=all").data.decode("utf-8")
    assert '<span class="lbl fill tone-navy">Air</span>' in body and 'class="lbl fill s-quoted"' in body
    # 8: the requester in the header: no box, navy, semibold, 2px larger
    assert ".case-page .req-box { display: inline; border: 0; color: #002060; font-weight: 600; font-size: calc(1em + 2px);" in CSS
    body = client.get("/enquiries/{}".format(ids["fu1"])).data.decode("utf-8")
    assert '<span class="cond-line"><span class="req-box">KLX</span>' in body
