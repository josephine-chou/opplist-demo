"""Report section 7, 'Why we lost' (report v9, schema v21)."""

from __future__ import annotations

import openpyxl
import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, monthly, monthly_export
from opplist.web import create_app


@pytest.fixture
def lost_book(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    base = add_party(repo, "Keel Cargo", country="Chile", mfn_member=1)

    def case(opp, party, received, **extra):
        return add_enquiry(repo, opp, party, received=received, service_mode="FCL",
                           origin_country="USA", origin_loc="MIA", dest_country="Chile",
                           dest_loc="SAI", **extra)

    price = case("OPP-0001", tolvex, "2026-08-05")
    repo.set_status(price, "Quoted", "2026-08-08")
    repo.set_status(price, "Lost", "2026-08-20", closed_reason="Price too high",
                    note="Customer booked with a rival quoting USD 1,640 including THC. [mid: <q7@mail.example>]")
    repo.update("enquiry", price, {"lost_their_price": "USD 1,640 incl. THC",
                                   "lost_our_price": "USD 1,905 door to port"})
    silent = case("OPP-0002", base, "2026-08-06")
    repo.set_status(silent, "Quoted", "2026-08-09")
    repo.set_status(silent, "Lost", "2026-08-25", closed_reason="No response from customer",
                    note="Reply from nobody")
    cancelled = case("OPP-0003", base, "2026-08-07")
    repo.set_status(cancelled, "Lost", "2026-08-28", closed_reason="Shipment cancelled")
    dropped = case("OPP-0004", tolvex, "2026-08-07")
    repo.set_status(dropped, "Dropped", "2026-08-10", closed_reason="Registered in error")
    # the two months before: one lost each, one of them on price
    earlier = case("OPP-0005", tolvex, "2026-07-03")
    repo.set_status(earlier, "Lost", "2026-07-20", closed_reason="Price too high", note="cheaper elsewhere")
    earliest = case("OPP-0006", tolvex, "2026-06-03")
    repo.set_status(earliest, "Lost", "2026-06-20", closed_reason="Went with another forwarder")
    outside = case("OPP-0007", tolvex, "2026-05-03")                # before the window
    repo.set_status(outside, "Lost", "2026-05-20", closed_reason="Price too high")
    repo.commit()
    return repo


def rows(table):
    return {r.label: r.cells for r in table.rows}


# test_lost_by_reason_counts_this_month_and_the_last_three: replaced in batch C1 by tests/test_cancelled_counts.py (it pinned the old Lost / cancelled words).


# test_a_month_with_no_lost_case_says_so: removed in batch 17a - the old section 7 (lost by reason, What we heard) is gone from the report; see tests/test_monthly.py


# test_what_we_heard_leaves_no_response_out_and_puts_notes_first: removed in batch 17a - the old section 7 (lost by reason, What we heard) is gone from the report; see tests/test_monthly.py


# test_the_page_cuts_the_note_at_140_and_keeps_the_whole_in_the_title: removed in batch 17a - the old section 7 (lost by reason, What we heard) is gone from the report; see tests/test_monthly.py


# test_more_than_ten_says_and_n_more: removed in batch 17a - the old section 7 (lost by reason, What we heard) is gone from the report; see tests/test_monthly.py


# test_the_three_files_carry_section_7: removed in batch 17a - the old section 7 (lost by reason, What we heard) is gone from the report; see tests/test_monthly.py

# --------------------------------------------------- the two price boxes --
def test_the_prices_are_stored_from_the_lost_form_with_change_log(tmp_path):
    path = tmp_path / "lost-web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-0001", party, received="2026-08-05")
    repo.set_status(case, "Quoted", "2026-08-08")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    body = client.get("/enquiries/{}".format(case)).data.decode("utf-8")
    assert 'id="lost-price-wrap"' in body and 'placeholder="USD 950 + local charges"' in body
    assert 'title="lost on price, even to another forwarder">Price too high' in body
    assert 'title="price was not the reason, or the reason is unknown">Went with another forwarder' in body

    page = client.post("/enquiries/{}/status".format(case), data={
        "status": "Lost", "at": "2026-08-20", "closed_reason": "Price too high",
        "note": "Rival came in at USD 1,640.", "lost_their_price": "USD 1,640 incl. THC",
        "lost_our_price": "USD 1,905 door to port"}, follow_redirects=True)
    assert page.status_code == 200
    assert "Their price USD 1,640 incl. THC" in page.data.decode("utf-8")
    conn = db.open_db(path)
    row = conn.execute("SELECT status, lost_their_price, lost_our_price FROM v_enquiry WHERE id = ?",
                       (case,)).fetchone()
    assert tuple(row) == ("Lost", "USD 1,640 incl. THC", "USD 1,905 door to port")
    logged = {r["field"]: r["new_value"] for r in conn.execute(
        "SELECT field, new_value FROM change_log WHERE table_name = 'enquiry' AND row_id = ? "
        "AND field LIKE 'lost_%'", (case,))}
    assert logged == {"lost_their_price": "USD 1,640 incl. THC", "lost_our_price": "USD 1,905 door to port"}
    conn.close()


def test_the_export_contract_is_untouched(lost_book, tmp_path):
    from opplist import export_csv, export_xlsx

    lines = export_csv.export_opp_index(lost_book.conn, tmp_path / "opp-index.csv") \
        .read_bytes().decode("utf-8-sig").split("\r\n")
    assert lines[0].count(",") == 7                                   # 8 columns
    agents = export_csv.export_agents(lost_book.conn, tmp_path / "agents.csv") \
        .read_bytes().decode("utf-8-sig").split("\r\n")
    assert agents[0].count(",") == 4                                  # 5 columns
    wb = openpyxl.load_workbook(export_xlsx.export(lost_book.conn, tmp_path / "out.xlsx"), read_only=True)
    try:
        headers = [c.value for c in next(wb["Opportunities"].iter_rows(max_row=1))]
        assert len(headers) == 41 and headers == export_xlsx.OPPORTUNITY_COLUMNS
    finally:
        wb.close()
