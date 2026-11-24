"""Two events on one day (STAGE_RANK), history filled in after the fact,
placeholders that graduate, and the name a placeholder is offered."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, intake, status as status_mod
from opplist.web import create_app


def events(conn, enquiry_id):
    return [(r["at"], r["status"], r["source"]) for r in conn.execute(
        "SELECT at, status, source FROM status_event WHERE enquiry_id = ? "
        "AND kind = 'status' ORDER BY at, id", (enquiry_id,))]


def status_of(conn, enquiry_id):
    return conn.execute("SELECT status FROM enquiry WHERE id = ?",
                        (enquiry_id,)).fetchone()["status"]


@pytest.fixture
def case(repo):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    enquiry_id = add_enquiry(repo, "OPP-1500", party, received="2026-09-01", status="New")
    repo.commit()
    return repo, enquiry_id


# ------------------------------------------------------------ same day --
def test_the_furthest_stage_of_the_day_is_the_case(case):
    repo, enquiry_id = case
    assert repo.set_status(enquiry_id, "Quoted", "2026-09-01") is True
    assert status_of(repo.conn, enquiry_id) == "Quoted"
    # a Sourcing dated the same day, recorded afterwards: history, not a demotion
    assert repo.set_status(enquiry_id, "Sourcing", "2026-09-01") is False
    assert status_of(repo.conn, enquiry_id) == "Quoted"
    assert [e[1] for e in events(repo.conn, enquiry_id)] == ["New", "Quoted", "Sourcing"]
    # _restate agrees: New and Quoted on one day is Quoted
    latest = repo.latest_event(enquiry_id)
    assert latest["status"] == "Quoted"


def test_an_event_dated_before_the_latest_is_history(case):
    repo, enquiry_id = case
    repo.set_status(enquiry_id, "Quoted", "2026-09-05")
    assert repo.set_status(enquiry_id, "Sourcing", "2026-09-03") is False
    assert status_of(repo.conn, enquiry_id) == "Quoted"
    # and a later date still moves the case
    assert repo.set_status(enquiry_id, "Won", "2026-09-06") is True
    assert status_of(repo.conn, enquiry_id) == "Won"
    # closed statuses rank equal: the newer row wins on the same day
    assert repo.set_status(enquiry_id, "Lost", "2026-09-06", closed_reason="price") is True
    assert status_of(repo.conn, enquiry_id) == "Lost"


def test_a_backdated_event_leaves_the_case_fields_alone(case):
    repo, enquiry_id = case
    repo.set_status(enquiry_id, "Quoted", "2026-09-05")
    repo.set_status(enquiry_id, "Lost", "2026-09-02", closed_reason="too slow")
    row = repo.conn.execute("SELECT status, closed_reason FROM enquiry WHERE id = ?",
                            (enquiry_id,)).fetchone()
    assert (row["status"], row["closed_reason"]) == ("Quoted", None)


def test_restate_orders_by_stage_within_the_day(case):
    repo, enquiry_id = case
    # events written directly, out of order, as an import might
    quoted = repo.insert("status_event", {"enquiry_id": enquiry_id, "at": "2026-09-04",
                                          "status": "Quoted"})
    repo.insert("status_event", {"enquiry_id": enquiry_id, "at": "2026-09-04",
                                 "status": "Sourcing"})
    repo._restate(quoted)
    assert status_of(repo.conn, enquiry_id) == "Quoted"
    assert status_mod.stage_rank("Won") == status_mod.stage_rank("Dropped") > \
        status_mod.stage_rank("Quoted") > status_mod.stage_rank("New")


def test_registering_at_a_later_stage_starts_with_new(repo):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    db.set_setting(repo.conn, "next_opp_number", 600)
    enquiry_id = repo.create_enquiry(
        {"requester_party_id": party, "received_on": "2026-09-03", "status": "Sourcing"},
        status_at="2026-09-03", note="asked three carriers")
    assert events(repo.conn, enquiry_id) == [("2026-09-03", "New", "manual"),
                                             ("2026-09-03", "Sourcing", "manual")]
    assert status_of(repo.conn, enquiry_id) == "Sourcing"
    assert repo.latest_event(enquiry_id)["status"] == "Sourcing"
    plain = repo.create_enquiry(
        {"requester_party_id": party, "received_on": "2026-09-04", "status": "New"},
        status_at="2026-09-04")
    assert [e[1] for e in events(repo.conn, plain)] == ["New"]


# ---------------------------------------------------------- placeholders --
def changes(conn, party_id, field):
    return [(r["old_value"], r["new_value"]) for r in conn.execute(
        "SELECT old_value, new_value FROM change_log WHERE table_name = 'party' "
        "AND row_id = ? AND field = ? ORDER BY id", (party_id, field))]


def test_a_placeholder_given_a_type_stops_being_one(repo):
    seed_reference(repo)
    party_id = add_party(repo, "cargovia-qatar", ptype="Unknown", country="Unknown",
                         placeholder=1)
    assert repo.conn.execute("SELECT placeholder FROM party WHERE id = ?",
                             (party_id,)).fetchone()[0] == 1
    repo.update("party", party_id, {"type": "Agent"})
    assert repo.conn.execute("SELECT placeholder FROM party WHERE id = ?",
                             (party_id,)).fetchone()[0] == 0
    assert changes(repo.conn, party_id, "placeholder") == [("1", "0")]
    # created with a type straight away: not a placeholder either
    born = add_party(repo, "Ready Co", ptype="Agent", country="Chile", placeholder=1)
    assert repo.conn.execute("SELECT placeholder FROM party WHERE id = ?",
                             (born,)).fetchone()[0] == 0


def test_the_reserved_keys_stay_placeholders(repo):
    seed_reference(repo)
    for key in ("(TBC)", "Unknown", "COLIN"):
        party_id = add_party(repo, key, ptype="Unknown", country="Unknown", placeholder=1)
        repo.update("party", party_id, {"type": "Agent", "country": "Chile"})
        assert repo.conn.execute("SELECT placeholder FROM party WHERE id = ?",
                                 (party_id,)).fetchone()[0] == 1, key
        assert changes(repo.conn, party_id, "placeholder") == []


def test_names_from_domain_stems():
    assert intake.name_from_stem("cargovia-qatar") == "Cargovia Qatar"
    assert intake.name_from_stem("zenith_cargo") == "Zenith Cargo"
    assert intake.name_from_stem("zenithcargo") == "Zenithcargo"
    assert intake.suggested_party_name("cargovia-qatar.example") == "Cargovia Qatar"
    assert intake.suggested_party_name("us.harborline.example") == "Harborline"
    assert intake.suggested_party_key("bluefin-usa.example") == "Bluefin-Usa"      # import-tracker, unchanged
    assert intake.looks_like_domain_stem("cargovia-qatar")
    assert intake.looks_like_domain_stem("tolvex")
    assert not intake.looks_like_domain_stem("Zenith Cargo")
    assert not intake.looks_like_domain_stem("(TBC)")


def test_rename_keeps_the_old_key_as_an_alias(repo):
    seed_reference(repo)
    party_id = add_party(repo, "cargovia-qatar", ptype="Unknown", country="Unknown",
                         placeholder=1)
    add_party(repo, "Taken", country="Chile")
    assert repo.rename_party(party_id, "Taken") is False
    assert repo.rename_party(party_id, "Cargovia Qatar") is True
    row = repo.conn.execute("SELECT key, full_name, aliases, placeholder FROM party "
                            "WHERE id = ?", (party_id,)).fetchone()
    assert (row["key"], row["full_name"], row["aliases"]) == \
        ("Cargovia Qatar", "Cargovia Qatar", '["cargovia-qatar"]')
    assert row["placeholder"] == 1                       # still Unknown: still a placeholder
    assert changes(repo.conn, party_id, "key") == [("cargovia-qatar", "Cargovia Qatar")]


@pytest.fixture
def web(tmp_path):
    path = tmp_path / "web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    add_party(repo, "Tolvex", country="Chile")
    add_party(repo, "(TBC)", ptype="Unknown", country="Unknown", placeholder=1)
    add_party(repo, "Aardvark Cargo", country="Chile")
    stem = add_party(repo, "cargovia-qatar", ptype="Unknown", country="Unknown", placeholder=1)
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, stem


def test_placeholders_sort_last_in_the_pick_lists(web):
    client, path, stem = web
    body = client.get("/enquiries/new").data.decode("utf-8")
    order = [name for name in ("Aardvark Cargo", "Tolvex", "(TBC)", "cargovia-qatar")
             if name in body]
    positions = [body.index(">{}".format(name)) if ">{}".format(name) in body
                 else body.index(name) for name in order]
    assert positions == sorted(positions)
    assert body.index("Tolvex") < body.index("(TBC)") < body.index("cargovia-qatar")


def test_the_party_page_offers_a_name_and_applies_it(web):
    client, path, stem = web
    body = client.get("/parties/{}".format(stem)).data.decode("utf-8")
    assert "Suggested name:" in body and "Cargovia Qatar" in body
    page = client.post("/parties/{}/use-suggested-name".format(stem),
                       data={"key": "Cargovia Qatar"}, follow_redirects=True)
    body = page.data.decode("utf-8")
    assert "Renamed to Cargovia Qatar" in body
    assert "Also known as: cargovia-qatar" in body
    assert "Suggested name:" not in body
    # saving the page with a real type takes the placeholder badge away
    page = client.post("/parties/{}".format(stem), data={
        "key": "Cargovia Qatar", "type": "Agent", "country": "Chile", "stage": "active"},
        follow_redirects=True)
    conn = db.open_db(path)
    assert conn.execute("SELECT placeholder FROM party WHERE id = ?", (stem,)).fetchone()[0] == 0
    conn.close()
