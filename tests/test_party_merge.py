"""Possible duplicate parties and the merge (schema v8)."""

from __future__ import annotations

import json

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import party_dupes, schema_migrations


@pytest.fixture
def pair(repo):
    seed_reference(repo)
    a = add_party(repo, "Tolvex", country="Chile", mfn_member=0)
    b = add_party(repo, "Tolvex Chile SpA", country="Chile", mfn_member=1)
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": a})
    repo.insert("contact", {"party_id": b, "name": "Renata", "email": "renata@tolvex.example"})
    case = add_enquiry(repo, "OPP-0001", b, received="2026-08-01", status="Won",
                       awarded_vendor_id=b, rate_used="agent")
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": b,
                                   "asked_on": "2026-08-02"})
    repo.insert("party_event", {"party_id": b, "at": "2026-08-03", "type": "note",
                                "summary": "met at the fair"})
    repo.commit()
    return repo, a, b


def test_the_schema_is_current_with_the_new_columns(repo):
    assert schema_migrations.current_version(repo.conn) == schema_migrations.CURRENT_VERSION
    assert "rate_used" in {r[1] for r in repo.conn.execute("PRAGMA table_info(enquiry)")}
    assert "voided" in {r[1] for r in repo.conn.execute("PRAGMA table_info(vendor_request)")}
    assert {"aliases", "merged_into"} <= {r[1] for r in repo.conn.execute("PRAGMA table_info(party)")}


def test_duplicates_are_found_by_domain_and_by_name(pair):
    repo, a, b = pair
    add_party(repo, "Seavio", country="USA")
    add_party(repo, "Seavio Miami", country="USA")
    add_party(repo, "Seavio Transport Inc", country="USA")     # same words once stripped
    add_party(repo, "Chile Haulage", country="Chile")            # 'chile' alone: no
    add_party(repo, "(TBC)", ptype="Unknown", placeholder=1)
    repo.commit()
    pairs = party_dupes.find_duplicates(repo.conn)
    found = {(p.a_key, p.b_key): p.why for p in pairs}
    assert found[("Tolvex", "Tolvex Chile SpA")] == "same domain tolvex.example"
    assert found[("Seavio", "Seavio Miami")] == "'Seavio' is contained in 'Seavio Miami'"
    assert found[("Seavio", "Seavio Transport Inc")] == "same name after the suffixes"
    assert not any("Chile Haulage" in k for k in found)


def test_merging_moves_everything_and_keeps_both_rows(pair):
    repo, a, b = pair
    moved = repo.merge_party(a, b, today="2026-09-10")
    repo.commit()
    conn = repo.conn
    assert moved["enquiry.requester_party_id"] == 1 and moved["enquiry.awarded_vendor_id"] == 1
    assert moved["vendor_request.vendor_party_id"] == 1 and moved["contact.party_id"] == 1
    assert moved["party_event.party_id"] == 1 and moved["party_domain"] == 0
    assert tuple(conn.execute("SELECT requester_party_id, awarded_vendor_id FROM enquiry"
                              ).fetchone()) == (a, a)
    kept = conn.execute("SELECT * FROM party WHERE id = ?", (a,)).fetchone()
    dropped = conn.execute("SELECT * FROM party WHERE id = ?", (b,)).fetchone()
    assert json.loads(kept["aliases"]) == ["Tolvex Chile SpA"]
    assert kept["mfn_member"] == 1                       # the merged party knew
    assert "Merged Tolvex Chile SpA into this party on 2026-09-10" in kept["notes"]
    assert dropped["merged_into"] == a
    assert conn.execute("SELECT COUNT(*) FROM party").fetchone()[0] == 2
    logged = conn.execute(
        "SELECT table_name, field FROM change_log WHERE new_value = ? "
        "AND field IN ('requester_party_id','awarded_vendor_id','vendor_party_id','party_id')",
        (str(a),)).fetchall()
    assert len(logged) == 5
    # the survivor is what the next mail from tolvex.example finds
    assert party_dupes.find_duplicates(conn) == []


def test_a_merged_party_cannot_be_merged_again(pair):
    repo, a, b = pair
    repo.merge_party(a, b)
    with pytest.raises(ValueError):
        repo.merge_party(b, a)
    with pytest.raises(ValueError):
        repo.merge_party(a, a)
