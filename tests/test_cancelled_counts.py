"""Batch C1: what counts a Cancelled case, and where it is offered. These
replace older tests that pinned 'Lost or cancelled' (they were written on
cases of the first data set, so they were rewritten here rather than edited): the month
report's KPIs and Why we lost, the analysis report's outcome buckets and
trend, the Tracking hints and the re-quote row, the check page, and the
mail phrases around a cancellation.

Every company, person, place, date and number here is invented; cases from
OPP-3121."""

from __future__ import annotations

import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, monthly, propose, report, tracking
from opplist.web import create_app


def _rows(table):
    return {r.label: r.cells for r in table.rows}


# ------------------------------------------------------------ month report --
@pytest.fixture
def book(repo):
    seed_reference(repo)
    for key, country in (("OSORNO", "Chile"), ("TEMUCO", "Chile")):
        repo.insert("location", {"key": key, "country": country, "type": "City"})
    fern = add_party(repo, "Fernhill Cargo", country="Chile", mfn_member=1)
    tarn = add_party(repo, "Tarnwick Logistics", country="USA")

    def case(opp, party, status, received="2026-08-06", reason=None, **extra):
        values = dict(received=received, service_mode="Air", origin_country="USA",
                      dest_country="Chile", dest_loc="OSORNO")
        values.update(extra)
        case_id = add_enquiry(repo, opp, party, **values)
        if status != "New":
            # closed the way the app closes a case, so the routes follow
            repo.set_status(case_id, status, received[:8] + "20", closed_reason=reason)
        return case_id

    case("OPP-3121", fern, "Won")
    case("OPP-3122", fern, "Lost", reason="Price too high")
    case("OPP-3123", tarn, "Lost", reason="Unable to handle", dest_country="USA", dest_loc=None)
    case("OPP-3124", fern, "Cancelled", reason="Shipment cancelled")
    case("OPP-3125", tarn, "Cancelled", reason="Order not confirmed")
    case("OPP-3126", fern, "Quoted")
    case("OPP-3127", fern, "Dropped", reason="Duplicate of OPP-3121")      # in no number
    case("OPP-3128", fern, "Lost", received="2026-07-14", reason="Rate too slow")
    case("OPP-3129", fern, "Cancelled", received="2026-07-15", reason="Shipment cancelled")
    repo.commit()
    return repo


def test_the_kpis_count_lost_only_with_the_cancelled_under_it(book):
    built = monthly.build(book.conn, "2026-08")
    assert built.kpis == [
        ("6 / 0", "Agents / Our offices"), ("5 / 1", "LATAM / Rest of world"),       # 17l
        ("6 / 0", "Air / Ocean"), ("1", "Won"), ("2", "Lost")]           # 17a: five tiles
    assert built.kpi_notes == {"Lost": "2 cancelled"}
    assert "Lost                        2 (2 cancelled)" in monthly.render_text(built)
    # a Dropped row moves nothing, with or without a reason
    book.insert("enquiry", {"opp_no": "OPP-3130", "requester_party_id": 1, "received_on": "2026-08-07",
                            "service_mode": "Air", "status": "Dropped"})
    book.commit()
    assert monthly.build(book.conn, "2026-08").kpis == built.kpis
    # section 1's Lost column counts Lost only
    assert _rows(built.by_scope)["Total"][-2:] == ["1", "2"]          # Won, Lost


def test_why_we_lost_counts_lost_cases_only(book):
    """17a: section 2's bars are the month's Lost cases by reason; the
    Cancelled ones are listed under the bars, never a bar."""
    built = monthly.build(book.conn, "2026-08")
    assert built.lost_bars == [("Price too high", 1), ("Unable to handle", 1)]
    assert not any(r in dict(built.lost_bars) for r in ("Shipment cancelled", "Order not confirmed"))
    assert {c["opp_no"] for c in built.cancelled} >= {"OPP-3124", "OPP-3125"}


# ------------------------------------------------------- analysis report --
def test_the_outcome_buckets_split_lost_and_cancelled(book):
    built = report.build(book.conn, "2026-08", "2026-09-09")
    outcome = next(b for b in built.sections if b.title.startswith("4."))
    total = dict(zip(report.OUTCOMES, {r[0]: r for r in outcome.rows}["Total"][1:-1]))
    assert (total["Won"], total["Lost"], total["Cancelled"]) == (1, 2, 2)
    august = next(r for r in built.trend.rows if r[0] == "2026-08")
    tail = dict(zip(report.OUTCOMES, august[-len(report.OUTCOMES):]))
    assert (tail["Won"], tail["Lost"], tail["Cancelled"]) == (1, 2, 2)
    july = next(r for r in built.trend.rows if r[0] == "2026-07")
    assert july[-1] == ""                                   # no outcome columns before the month


# ------------------------------------------------------------ the mail --
def _mail(at, body, internal=False, sender="Lia Moran", address="lia@fernhill.example"):
    return propose.ThreadMail(at=at, sender_name=sender, address=address, internal=internal,
                              subject="RE: RATE REQUEST // MIA - OSORNO", body=body, from_requester=not internal)


def test_no_longer_alone_is_not_a_cancellation():
    p = propose.classify([_mail("2026-11-15", "Dario no longer looks after this account; please copy me from now on.")],
                         "Fernhill Cargo", requester_known=True)
    assert p.proposed_status not in ("Lost", "Cancelled")
    p = propose.classify([_mail("2026-11-15", "Our buyer says they no longer need the cargo moved.")],
                         "Fernhill Cargo", requester_known=True)
    assert (p.proposed_status, p.closed_reason) == ("Cancelled", "Shipment cancelled")


def test_a_cancellation_after_our_booking_mail_is_still_a_cancellation():
    mails = [_mail("2026-11-12", "New booking for Fernhill on the Osorno lane, filed under this case.", internal=True,
                   sender="JJC-LENA PARK", address="lena@jojofreight.example"),
             _mail("2026-11-14", "Bad news from the plant: the whole order is cancelled.")]
    p = propose.classify(mails, "Fernhill Cargo", requester_known=True)
    assert (p.proposed_status, p.closed_reason, p.proposed_date) == ("Cancelled", "Shipment cancelled", "2026-11-14")


# ----------------------------------------------------------- tracking --
@pytest.fixture
def web(tmp_path, monkeypatch):
    path = tmp_path / "c1-web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    repo.insert("location", {"key": "OSORNO", "country": "Chile", "type": "City"})
    fern = add_party(repo, "Fernhill Cargo", country="Chile")
    case = add_enquiry(repo, "OPP-3131", fern, received="2026-11-02", origin_loc="MIA", origin_country="USA",
                       dest_loc="OSORNO", dest_country="Chile")
    repo.set_status(case, "Quoted", "2026-11-05")
    # FX4: Waiting on us is logged by hand (a scan's reply never asks)
    repo.add_note(case, "2026-11-10", "Reply from Lia Moran (needs an answer): can you update the rate?",
                  source="manual")
    plain = add_enquiry(repo, "OPP-3132", fern, received="2026-11-03")
    repo.update("enquiry", plain, {"status": "Lost"})        # Lost without a reason, for the check page
    repo.commit()
    conn.close()
    import opplist.web as web_module
    monkeypatch.setattr(web_module, "_today", lambda: "2026-11-11")
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, case


def test_the_replied_card_offers_cancelled_next_to_lost_and_says_so(web):
    client, path, case = web
    body = client.get("/tracking").data.decode("utf-8")
    assert "logged on the case page or on the team site; Answered clears it" in body
    start = body.index('id="replied"')
    replied = body[start:re.compile(r'class="card group|id="backlog"').search(body, start + 1).start()]
    assert replied.index(">Lost...</button>") < replied.index(">Cancelled...</button>")
    assert 'value="quick:Cancelled"' in replied and "Order not confirmed" in replied
    # Cancelled from the row: its reason from its own list, the Lost list left empty
    page = client.post("/tracking/act", data={
        "do": "quick:Cancelled", "rule": "R3", "kind": "enquiry", "ref_id": case, "enquiry_id": case,
        "at": "2026-11-11", "closed_reason": ["", "Order not confirmed"]}, follow_redirects=True)
    assert page.status_code == 200 and b"OPP-3131 set to Cancelled" in page.data
    conn = db.open_db(path)
    assert conn.execute("SELECT status, closed_reason FROM enquiry WHERE id = ?", (case,)).fetchone()[:] == \
        ("Cancelled", "Order not confirmed")
    assert tracking.rule_quoted_no_answer(conn, "2026-11-11") == []
    conn.close()


def test_re_quoted_from_the_replied_row_writes_the_requote_event(web):
    client, path, case = web
    page = client.post("/tracking/act", data={
        "do": "quick:requote", "rule": "R3", "kind": "enquiry", "ref_id": case,
        "enquiry_id": case, "at": "2026-11-11", "note": "2 pallets now"}, follow_redirects=True)
    assert b"OPP-3131 re-quoted on 2026-11-11" in page.data
    conn = db.open_db(path)
    assert conn.execute("SELECT at, status, kind, note FROM status_event WHERE enquiry_id = ? "
                        "ORDER BY id DESC LIMIT 1", (case,)).fetchone()[:] == \
        ("2026-11-11", "Quoted", "requote", "Re-quote: 2 pallets now")
    conn.close()


def test_the_check_page_lists_a_closed_case_without_a_reason_and_changes_nothing(web):
    client, path, case = web
    conn = db.open_db(path)
    before = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    conn.close()
    page = client.get("/check").data.decode("utf-8")
    assert "Lost / Cancelled / Dropped cases with no reason recorded" in page and "OPP-3132" in page
    assert "never changes anything" in page
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == before
    conn.close()
