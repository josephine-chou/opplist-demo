"""Roles and groups on the Parties pages."""

from __future__ import annotations

import pytest

from conftest import add_party, seed_reference
from opplist import db
from opplist.web import create_app


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "p.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, "test")
    seed_reference(repo)
    repo.insert("country", {"name": "Peru", "region": "Latin America"})
    cl = add_party(repo, "Marvis Logistics Chile SpA", country="Chile")
    pe = add_party(repo, "Marvis Logistics Peru S.A.C.", country="Peru")
    other = add_party(repo, "Tolvex", country="Chile")
    group = repo.add_party_group("MARVIS")
    repo.update("party", cl, {"group_id": group, "roles": "agent"})
    repo.update("party", pe, {"group_id": group, "roles": "agent"})
    repo.commit()
    conn.close()
    application = create_app(path)
    application.config["TESTING"] = True
    return {"client": application.test_client(), "path": path, "cl": cl, "pe": pe, "other": other}


def test_the_list_shows_the_group_and_filters_on_it(world):
    page = world["client"].get("/parties").get_data(as_text=True)
    assert page.count('href="/parties?group=MARVIS"') == 2
    page = world["client"].get("/parties?group=marvis").get_data(as_text=True)
    assert "Marvis Logistics Chile SpA" in page and "Marvis Logistics Peru" in page and "Tolvex" not in page


def test_each_marvis_office_sees_the_other(world):
    c = world["client"]
    page = c.get("/parties/{}".format(world["cl"])).get_data(as_text=True)
    assert "Other offices in this group" in page
    assert 'href="/parties/{}"'.format(world["pe"]) in page and "Marvis Logistics Peru" in page
    page = c.get("/parties/{}".format(world["pe"])).get_data(as_text=True)
    assert 'href="/parties/{}"'.format(world["cl"]) in page and "Marvis Logistics Chile SpA" in page
    page = c.get("/parties/{}".format(world["other"])).get_data(as_text=True)
    assert "Other offices in this group" not in page
    assert 'name="roles" value="carrier_air"' in page and 'name="new_group"' in page


def save(client, party_id, **extra):
    data = {"key": extra.pop("key"), "full_name": "", "type": "Agent", "country": "Chile",
            "stage": "active", "cadence_days": ""}
    data.update(extra)
    return client.post("/parties/{}".format(party_id), data=data, follow_redirects=True)


def test_ticking_roles_writes_the_column_and_the_contract_view_follows(world):
    c = world["client"]
    save(c, world["other"], key="Tolvex", roles=["carrier_air", "agent", "warehouse"])
    conn = db.open_db(world["path"])
    assert conn.execute("SELECT roles FROM party WHERE id = ?", (world["other"],)).fetchone()[0] == \
        "agent|carrier_air|warehouse"                                  # stored in the spec's order
    assert conn.execute("SELECT roles FROM v_master_party WHERE key = 'Tolvex'").fetchone()[0] == \
        "agent|carrier_air|warehouse"
    assert conn.execute("SELECT old_value, new_value FROM change_log WHERE table_name = 'party' "
                        "AND row_id = ? AND field = 'roles'", (world["other"],)).fetchone()[:] == \
        ("", "agent|carrier_air|warehouse")
    conn.close()
    save(c, world["other"], key="Tolvex")                              # nothing ticked: empty
    conn = db.open_db(world["path"])
    assert conn.execute("SELECT roles FROM party WHERE id = ?", (world["other"],)).fetchone()[0] == ""
    conn.close()


def test_the_group_can_be_picked_or_created_and_cleared(world):
    c = world["client"]
    save(c, world["other"], key="Tolvex", group_key="MARVIS")
    conn = db.open_db(world["path"])
    assert conn.execute("SELECT group_key FROM v_master_party WHERE key = 'Tolvex'").fetchone()[0] == "MARVIS"
    conn.close()
    page = c.get("/parties/{}".format(world["cl"])).get_data(as_text=True)
    assert "Tolvex" in page                                             # now an office of the group
    save(c, world["other"], key="Tolvex", group_key="MARVIS", new_group="nexus")
    conn = db.open_db(world["path"])
    assert conn.execute("SELECT key, name FROM party_group WHERE key = 'NEXUS'").fetchone()[:] == ("NEXUS", "NEXUS")
    assert conn.execute("SELECT group_key FROM v_master_party WHERE key = 'Tolvex'").fetchone()[0] == "NEXUS"
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'party_group'").fetchone()[0] == 2
    conn.close()
    save(c, world["other"], key="Tolvex", group_key="")
    conn = db.open_db(world["path"])
    assert conn.execute("SELECT group_id FROM party WHERE id = ?", (world["other"],)).fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM party_group").fetchone()[0] == 2   # groups are never deleted
    conn.close()
