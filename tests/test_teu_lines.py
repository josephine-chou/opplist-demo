"""Batch 17m (schema v45): v_enquiry.teu from the case's FCL cargo lines, so
a case with two container types, or a type written as the mail had it, has
its TEU; routes counted as routes.totals counts containers.

Every company, place, date and number here is invented; cases from
OPP-8001.
"""

from __future__ import annotations

import sqlite3

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, monthly, monthly_requests, schema_migrations

MONTH = "2027-05"


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "teu.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    p = add_party(repo, "Penhallow Shipping", country="Chile")
    n = [8001]

    def case(lines=(), routes=0, **extra):
        """A FCL case in May with `routes` routes besides A and its lines,
        each (qty, type) or a dict of line values."""
        opp = "OPP-{}".format(n[0])
        n[0] += 1
        values = dict(service_mode="FCL", origin_country="USA", dest_country="Chile")
        values.update(extra)
        got = add_enquiry(repo, opp, p, received="2027-05-{:02d}".format(min(n[0] - 8001, 28)), **values)
        for _ in range(routes):
            repo.add_route(got, {"origin_country": "USA", "dest_country": "Chile", "service_mode": "FCL"})
        for line in lines:
            repo.add_cargo_line(got, line if isinstance(line, dict) else
                                {"container_qty": line[0], "container_type": line[1]})
        repo.commit()
        return got

    yield conn, repo, case, path
    conn.close()


def teu(conn, case_id):
    return conn.execute("SELECT teu FROM v_enquiry WHERE id = ?", (case_id,)).fetchone()["teu"]


def live_routes(conn, case_id):
    return [r["id"] for r in conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND voided = 0 "
                                          "ORDER BY seq", (case_id,))]


# ------------------------------------------------------------ the types --
def test_two_types_add_up(world):
    conn, repo, case, _ = world
    got = case([(1, "20GP"), (1, "40GP")])
    assert conn.execute("SELECT container_type FROM enquiry WHERE id = ?", (got,)).fetchone()[0] == "Mixed"
    assert teu(conn, got) == 3


@pytest.mark.parametrize("written, each", [
    ("40'HQ", 2), ("40’RF", 2), ("40' HC DV SOC", 2), ("45HQ", 2), ('40" HC', 2),
    ("20FT DRY", 1), ("20’", 1), ("20 DC", 1), ("‘20GP", 1), ("20gp", 1),
])
def test_a_type_as_the_mail_had_it(world, written, each):
    conn, repo, case, _ = world
    assert teu(conn, case([(3, written)])) == 3 * each


# ----------------------------------------------------------- the routes --
def test_a_line_counts_once_for_every_live_route_it_applies_to(world):
    conn, repo, case, _ = world
    got = case(routes=2)                                     # A, B, C
    a, b, c = live_routes(conn, got)
    repo.add_cargo_line(got, {"container_qty": 2, "container_type": "40HQ", "route_ids": "{},{}".format(a, c)})
    repo.commit()
    assert teu(conn, got) == 2 * 2 * 2
    # a line naming only a struck-out route counts on every live route
    other = case(routes=2)
    a2, b2, c2 = live_routes(conn, other)
    repo.void_route(c2)
    repo.add_cargo_line(other, {"container_qty": 1, "container_type": "20GP", "route_ids": str(c2)})
    repo.commit()
    assert teu(conn, other) == 1 * 1 * 2
    # the containers the lines give are the case's container_qty
    for case_id in (got, other):
        qty = conn.execute("SELECT container_qty FROM enquiry WHERE id = ?", (case_id,)).fetchone()[0]
        lines = conn.execute("SELECT SUM(qty * routes) FROM v_cargo_teu_line WHERE enquiry_id = ?",
                             (case_id,)).fetchone()[0]
        assert qty == lines


def test_a_case_without_routes_counts_once(world):
    conn, repo, case, _ = world
    got = case([(2, "40HQ")])
    # the repository keeps a case at least one route; older data may have none
    conn.execute("UPDATE enquiry_route SET voided = 1 WHERE enquiry_id = ?", (got,))
    conn.commit()
    assert live_routes(conn, got) == [] and teu(conn, got) == 4


# --------------------------------------------------- lines left out --
def test_voided_uncounted_and_not_specified_lines_are_left_out(world):
    conn, repo, case, _ = world
    got = case([(1, "40HQ"), (5, "20GP"), (4, "40GP"), {"container_qty": 9, "container_type": "Not specified"}])
    struck, uncounted = [r["id"] for r in conn.execute(
        "SELECT id FROM enquiry_cargo WHERE enquiry_id = ? AND seq IN (2, 3) ORDER BY seq", (got,))]
    repo.void_cargo_line(struck)
    repo.edit_cargo_line(uncounted, {"counted": 0, "counted_present": "1"})
    repo.commit()
    assert teu(conn, got) == 2


@pytest.mark.parametrize("bad", [{"container_qty": 1, "container_type": "Mixed"},
                                 {"container_qty": None, "container_type": "40HQ"},
                                 {"container_qty": 0, "container_type": "20GP"}])
def test_a_line_it_cannot_read_leaves_the_case_empty_and_on_the_check_list(world, bad):
    conn, repo, case, _ = world
    got = case([(2, "40HQ"), bad])
    assert teu(conn, got) is None
    [group] = [g for g in monthly.checks(conn, MONTH) if g["title"] == "No size for section 15"]
    assert [(i["opp_nos"], i["text"]) for i in group["items"]] == [(["OPP-8001"], "USA to Chile, FCL, no container count or type")]


def test_a_case_without_lines_reads_its_own_type(world):
    conn, repo, case, _ = world
    assert teu(conn, case(container_type="40HQ", container_qty=2)) == 4
    assert teu(conn, case(container_type="40'HQ", container_qty=2)) == 4          # read as in (c) too
    assert teu(conn, case(container_type="Not specified", container_qty=2)) is None


# ---------------------------------------------------------- the report --
def test_section_9_and_section_15_read_a_two_type_case(world):
    conn, repo, case, _ = world
    got = case([(2, "20GP"), (3, "40'HQ")], status="Won")
    assert teu(conn, got) == 8
    report = monthly.build(conn, MONTH)
    row = [r for r in report.top_agents.rows if r.label == "Penhallow Shipping"][0]
    assert row.cells[report.top_agents.columns.index("TEU") - 1] == "8"
    assert monthly_requests.sizes(conn, MONTH)["FCL"] == {"cases": [(8.0, "won")], "missing": 0}
    assert [g for g in monthly.checks(conn, MONTH) if g["title"] == "No size for section 15"] == []


# --------------------------------------------------------- the upgrade --
def test_the_upgrade_from_v44_keeps_every_row(world):
    conn, repo, case, path = world
    got = case([(1, "20GP"), (1, "40GP")])
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    before = {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in tables}
    conn.close()
    raw = sqlite3.connect(path)
    raw.execute("DROP VIEW v_cargo_teu_line")
    raw.execute("UPDATE setting SET value = '44' WHERE key = 'schema_version'")
    raw.commit()
    raw.close()
    conn = db.open_db(path)
    try:
        assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION
        assert {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in tables} == before
        assert teu(conn, got) == 3
    finally:
        conn.close()
