"""Probable duplicates, on invented cases shaped like the ones the rule was written for."""

from __future__ import annotations

import pytest

from conftest import add_party, seed_reference
from opplist import dedupe


@pytest.fixture
def corpus(repo):
    """Ridgeway Parts and KRV, plus a couple of unrelated cases."""
    seed_reference(repo)
    for key, country in (("MAA", "Unknown"), ("JFK", "USA"), ("DTW", "USA"),
                         ("PEN", "Unknown")):
        repo.insert("location", {"key": key, "country": country, "type": "City"})

    highline = add_party(repo, "Ridgeway Parts", ptype="Direct Customer")
    tsv = add_party(repo, "KRV Global Solution", country="Unknown")
    tolvex = add_party(repo, "Tolvex", country="Chile")

    def enquiry(opp_no, party, **extra):
        values = {"opp_no": opp_no, "requester_party_id": party,
                  "status": "New"}
        values.update(extra)
        return repo.insert("enquiry", values)

    ids = {}
    # OPP-1385 / 0393 / 0394, the Ridgeway Parts rows.
    ids["0385"] = enquiry("OPP-1385", highline, received_on="2026-09-01",
                          origin_loc="KUL", origin_country="Unknown",
                          dest_loc="MEM", dest_country="USA",
                          service_mode="Air",
                          commodity="Nitrile Gloves & Hose Clamps")
    ids["0393"] = enquiry("OPP-1393", highline, received_on="2026-09-03",
                          origin_loc="NGB", origin_country="China",
                          dest_loc="MEM", dest_country="USA",
                          service_mode="Air", commodity="ZKTM59174")
    ids["0394"] = enquiry("OPP-1394", highline, received_on="2026-09-03",
                          origin_loc="NGB", origin_country="China",
                          dest_loc="MEM", dest_country="USA",
                          service_mode="Air", commodity="ZKTM59174")
    # OPP-1399 / 0400: same day, same destination, one without an origin.
    ids["0399"] = enquiry("OPP-1399", tsv, received_on="2026-09-04",
                          origin_loc="JFK", origin_country="USA",
                          dest_loc="MAA", dest_country="Unknown",
                          service_mode="Air", commodity="Lead Acid Batteries")
    ids["0400"] = enquiry("OPP-1400", tsv, received_on="2026-09-04",
                          origin_loc=None, origin_country="USA",
                          dest_loc="MAA", dest_country="Unknown",
                          service_mode="Air", commodity="Hydraulic Power Pack")
    # Unrelated.
    ids["0350"] = enquiry("OPP-1350", tsv, received_on="2026-08-24",
                          origin_loc="MAA", origin_country="Unknown",
                          dest_loc="DTW", dest_country="USA",
                          service_mode="Air",
                          commodity="Forming Die & K41 Die Assembly")
    ids["0001"] = enquiry("OPP-0001", tolvex, received_on="2026-09-02",
                          origin_loc="LAX", origin_country="USA",
                          dest_loc="SAI", dest_country="Chile",
                          service_mode="FCL", commodity="PET flakes")

    # Background traffic, so that a port code is as common here as it is in
    # the real 386 cases. Without it MEM and MAA look like rare words and every
    # pair sharing a destination scores as a duplicate - exactly the mistake
    # that counting rarity over the candidate set would cause. Spread far
    # apart in time so the filler never becomes a candidate of anything.
    import datetime as dt
    start = dt.date(2024, 1, 1)
    n = 0
    for place in ("MEM", "NGB", "MAA", "JFK", "LAX", "SAI", "DTW", "KUL", "PEN"):
        for _ in range(4):
            n += 1
            enquiry("OPP-9{:03d}".format(n), tolvex,
                    received_on=(start + dt.timedelta(days=n * 100)).isoformat(),
                    origin_loc=place, dest_loc=place, service_mode="FCL")
    repo.commit()
    return repo.conn, ids


def score(conn, a, b):
    df = dedupe.document_frequency(conn)
    left = conn.execute("SELECT * FROM v_enquiry WHERE opp_no = ?", (a,)).fetchone()
    right = conn.execute("SELECT * FROM v_enquiry WHERE opp_no = ?", (b,)).fetchone()
    return dedupe.score_pair(dict(left), right, df)


# ----------------------------------------------------------- tokenisation --
def test_tokens_match_the_powershell_rules():
    assert dedupe.tokens("Nitrile Gloves & Hose Clamps") == [
        "nitrile", "gloves", "hose", "clamps"]
    assert "rate" not in dedupe.tokens("RATE REQUEST // MIA")
    assert dedupe.tokens("ab cd") == []                    # shorter than three


def test_name_tokens_drop_numbers_and_fold_plurals():
    assert dedupe.name_tokens("Toys 2026") == ["toy"]
    assert dedupe.name_tokens("Lead Acid Batteries") == ["lead", "acid", "batterie"]
    assert dedupe.name_tokens("ZKTM59174") == ["zktm59174"]   # not a bare number


# ------------------------------------------------------------- the pairs ---
def test_two_identical_highline_lanes_are_probable(corpus):
    conn, _ = corpus
    match = score(conn, "OPP-1393", "OPP-1394")
    assert match.band == "probable"
    # lane + mode, two days apart, and 'zktm59174' seen only on these two rows
    assert match.score == 60 + 15 + 20


def test_a_different_origin_is_not_a_duplicate(corpus):
    """OPP-1385 is KUL - MEM, the others NGB - MEM.

    The rule expects all three Ridgeway Parts rows to be probable, but
    section 5.4 gives this pair only the shared destination: different lane,
    different commodity, so nothing scores. Flagged rather than forced.
    """
    conn, _ = corpus
    match = score(conn, "OPP-1385", "OPP-1393")
    assert match.score < dedupe.PROBABLE
    assert match.band == "unlikely"


def test_the_same_thread_makes_them_probable_anyway(repo, corpus):
    """Registered from one mail thread, the lane no longer has to agree."""
    conn, _ = corpus
    for opp in ("OPP-1385", "OPP-1393", "OPP-1394"):
        row = conn.execute("SELECT id FROM enquiry WHERE opp_no = ?", (opp,)).fetchone()
        repo.update("enquiry", row["id"],
                    {"thread_key": "RATEREQUESTRIDGEWAYPARTSFOBKULPENMEM"})
    repo.commit()
    for a, b in (("OPP-1385", "OPP-1393"), ("OPP-1385", "OPP-1394"),
                 ("OPP-1393", "OPP-1394")):
        match = score(conn, a, b)
        assert match.band == "probable"
        assert match.same_thread


def test_a_missing_origin_lands_in_possible_not_probable(corpus):
    """OPP-1400 has no origin location, so the lane only matched on country."""
    conn, _ = corpus
    match = score(conn, "OPP-1399", "OPP-1400")
    assert match.score == 60
    assert match.band == "possible"
    assert "lane matched on country" in match.reasons[0]


def test_a_different_requester_and_lane_scores_below_the_threshold(corpus):
    conn, _ = corpus
    assert score(conn, "OPP-0001", "OPP-1399").score < dedupe.POSSIBLE
    assert score(conn, "OPP-1350", "OPP-1399").score < dedupe.POSSIBLE


def test_conversation_id_also_scores_a_hundred(corpus):
    conn, _ = corpus
    other = conn.execute(
        "SELECT * FROM v_enquiry WHERE opp_no = 'OPP-0001'").fetchone()
    conn.execute("UPDATE enquiry SET conversation_id = 'ABC123' "
                 "WHERE opp_no = 'OPP-0001'")
    other = conn.execute(
        "SELECT * FROM v_enquiry WHERE opp_no = 'OPP-0001'").fetchone()
    match = dedupe.score_pair({"conversation_id": "abc123"}, other,
                              dedupe.document_frequency(conn))
    assert match.score == 100 and match.same_thread


# ------------------------------------------------- rarity over the corpus --
def test_document_frequency_is_counted_over_every_enquiry(repo, corpus):
    """The trap: a word looks unique inside the window and pulls cases together.

    Adding more cases with the same word - outside the sixty day window, so
    they are never candidates - must still push the score down.
    """
    conn, _ = corpus
    before = score(conn, "OPP-1393", "OPP-1394").score

    highline = conn.execute(
        "SELECT id FROM party WHERE key = 'Ridgeway Parts'").fetchone()["id"]
    for index in range(6):
        repo.insert("enquiry", {
            "opp_no": "OPP-25{:02d}".format(index),
            "requester_party_id": highline,
            "received_on": "2025-01-{:02d}".format(index + 1),
            "commodity": "ZKTM59174",
            "status": "New",
        })
    repo.commit()

    after = score(conn, "OPP-1393", "OPP-1394").score
    assert after < before, "a word seen eight times must stop being rare"
    assert after == before - 20        # the rare-word points for zktm59174 go
    assert dedupe.document_frequency(conn)["zktm59174"] == 8


def test_rare_word_points_are_capped_at_fifty(repo, corpus):
    conn, _ = corpus
    tolvex = conn.execute(
        "SELECT id FROM party WHERE key = 'Tolvex'").fetchone()["id"]
    words = "zircon aluminate perchlorate benzoate stearate"
    a = repo.insert("enquiry", {
        "opp_no": "OPP-1601", "requester_party_id": tolvex,
        "received_on": "2026-09-02", "commodity": words, "status": "New"})
    repo.insert("enquiry", {
        "opp_no": "OPP-1602", "requester_party_id": tolvex,
        "received_on": "2026-09-02", "commodity": words, "status": "New"})
    repo.commit()
    match = score(conn, "OPP-1601", "OPP-1602")
    # five words at 20 each would be 100; the cap holds it at 50, and the lane
    # (both ends blank on each side) plus mode still match.
    # both lanes are blank on both sides, so the match is partial and the
    # date bonus does not apply
    assert match.score == 60 + 50


# ------------------------------------------------------------------ find ---
def test_find_returns_the_best_five_above_the_threshold(corpus):
    conn, ids = corpus
    row = conn.execute(
        "SELECT * FROM v_enquiry WHERE opp_no = 'OPP-1394'").fetchone()
    matches = dedupe.find(conn, dict(row))
    assert [m.opp_no for m in matches] == ["OPP-1393"]
    assert matches[0].band == "probable"
    assert len(matches) <= dedupe.MAX_RESULTS


def test_find_ignores_legacy_and_out_of_window_cases(repo, corpus):
    conn, _ = corpus
    highline = conn.execute(
        "SELECT id FROM party WHERE key = 'Ridgeway Parts'").fetchone()["id"]
    repo.insert("enquiry", {
        "opp_no": "OPP-1700", "requester_party_id": highline,
        "received_on": "2026-09-03", "origin_loc": "NGB", "origin_country": "China",
        "dest_loc": "MEM", "dest_country": "USA", "service_mode": "Air",
        "commodity": "ZKTM59174", "status": "Dropped", "legacy": 1})
    repo.insert("enquiry", {
        "opp_no": "OPP-1701", "requester_party_id": highline,
        "received_on": "2025-01-05", "origin_loc": "NGB", "origin_country": "China",
        "dest_loc": "MEM", "dest_country": "USA", "service_mode": "Air",
        "commodity": "ZKTM59174", "status": "New"})
    repo.commit()
    row = conn.execute(
        "SELECT * FROM v_enquiry WHERE opp_no = 'OPP-1394'").fetchone()
    found = [m.opp_no for m in dedupe.find(conn, dict(row))]
    assert "OPP-1700" not in found, "legacy cases are excluded"
    assert "OPP-1701" not in found, "outside the dedupe window"


def test_find_excludes_the_case_itself(corpus):
    conn, _ = corpus
    row = conn.execute(
        "SELECT * FROM v_enquiry WHERE opp_no = 'OPP-1393'").fetchone()
    assert "OPP-1393" not in [m.opp_no for m in dedupe.find(conn, dict(row))]


def test_report_rows_lists_each_probable_pair_once(corpus):
    conn, _ = corpus
    rows = dedupe.report_rows(conn)
    pairs = {(r["OppNoA"], r["OppNoB"]) for r in rows}
    assert ("OPP-1393", "OPP-1394") in pairs
    assert ("OPP-1394", "OPP-1393") not in pairs
    assert all(r["Score"] >= dedupe.PROBABLE for r in rows)
