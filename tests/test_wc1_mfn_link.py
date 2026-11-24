"""WC1: Agent parties linked to the MFN directory (invented data only)."""

from __future__ import annotations

import csv
import json
import sqlite3

import pytest

from conftest import add_enquiry, add_party
from opplist import __main__ as cli
from opplist import db, mfn_link

OFFICE_COLUMNS = ["member_id", "company", "formerly_known_as", "office_type", "office_city", "city",
                  "country", "country_code", "web_domain", "email_domain"]
CONTACT_COLUMNS = ["member_id", "company", "name", "title", "email", "email_domain"]

COUNTRIES = (("Chile", "CL", "Latin America"), ("Peru", "PE", "Latin America"),
             ("Uruguay", "UY", "Latin America"), ("El Salvador", "SV", "Latin America"),
             ("Singapore", "SG", "Asia"), ("USA", "US", "North America"), ("Unknown", None, "Undefined"))


def office(member, company, city, country, code, web="", mail="", fka="", kind=""):
    return {"member_id": member, "company": company, "formerly_known_as": fka, "office_type": kind,
            "office_city": city, "city": city, "country": country, "country_code": code,
            "web_domain": web, "email_domain": mail}


def contact(member, name, email, title=""):
    return {"member_id": member, "company": "", "name": name, "title": title, "email": email,
            "email_domain": email.split("@")[1]}


def write_directory(folder, offices, contacts=()):
    folder.mkdir(parents=True, exist_ok=True)
    for name, columns, rows in (("mfn-offices.csv", OFFICE_COLUMNS, offices),
                                ("mfn-contacts.csv", CONTACT_COLUMNS, contacts)):
        with open(folder / name, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "w.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, "test")
    for name, iso, region in COUNTRIES:
        repo.insert("country", {"name": name, "iso2": iso, "region": region})
    conn.commit()
    return {"conn": conn, "repo": repo, "path": path, "dir": tmp_path / "mfn",
            "out": tmp_path / "out", "reports": tmp_path / "reports"}


def party(world, key, country, domains=(), **extra):
    pid = add_party(world["repo"], key, country=country, **extra)
    for domain in domains:
        world["repo"].insert("party_domain", {"domain": domain, "party_id": pid})
    world["conn"].commit()
    return pid


def plan(world, offices, contacts=(), decisions=None):
    write_directory(world["dir"], offices, contacts)
    if decisions is not None:
        write_decisions(world, decisions)
    return mfn_link.run(world["conn"], directory_dir=world["dir"], out_dir=world["out"],
                        reports_dir=world["reports"], today="2026-11-05")


def write_decisions(world, rows):
    world["out"].mkdir(parents=True, exist_ok=True)
    with open(world["out"] / mfn_link.DECISIONS_NAME, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["party id", "decision", "group", "note"])
        writer.writerows(rows)


def row_of(result, pid):
    return next(r for r in result.rows if r.party.id == pid)


def proposed(world):
    with open(world["out"] / mfn_link.PROPOSED_NAME, encoding="utf-8-sig", newline="") as handle:
        return {int(r["party id"]): r for r in csv.DictReader(handle)}


def party_row(world, pid):
    return world["conn"].execute("SELECT * FROM party WHERE id = ?", (pid,)).fetchone()


# ------------------------------------------------------------- proposals ----
def test_each_proposal_kind(world):
    linked = party(world, "Quillon Freight", "Chile", ["quillon.test"])
    several = party(world, "Brisa Cargo", "Peru", ["brisa.test"])
    by_name = party(world, "Tordel Logistics", "Uruguay")
    abroad = party(world, "Velmora Cargo", "Chile", ["velmora.test"])
    absent = party(world, "Nobody Chile Freight", "Chile", ["nobodyhere.test"])
    unread = party(world, "Calima Transportes", "El Salvador", ["calima.test"])
    result = plan(world, [
        office("990001", "Quillon Freight SpA", "Santiago", "Chile", "CL", web="quillon.test"),
        office("990002", "Brisa Cargo SAC", "Lima", "Peru", "PE", web="brisa.test", kind="Head Office"),
        office("990003", "Brisa Cargo Callao", "Callao", "Peru", "PE", mail="brisa.test"),
        office("990004", "Tordel Logistics S.A.", "Montevideo", "Uruguay", "UY"),
        office("990005", "Velmora Cargo Pte. Ltd.", "Singapore", "Singapore", "SG", web="velmora.test"),
    ])
    kinds = {pid: row_of(result, pid).proposal for pid in (linked, several, by_name, abroad, absent, unread)}
    assert kinds == {linked: "link", several: "check", by_name: "check", abroad: "check",
                     absent: "not in MFN", unread: "wait"}
    assert row_of(result, linked).member == "990001"
    assert row_of(result, several).member == "990002" and "2 offices in Peru" in row_of(result, several).why
    assert row_of(result, by_name).member == "990004" and "name only" in row_of(result, by_name).why
    # a domain that is only another country's office: check, and no office offered
    assert row_of(result, abroad).member == "" and "Singapore" in row_of(result, abroad).why
    assert "El Salvador" in row_of(result, unread).why
    rows = proposed(world)
    assert rows[linked]["proposal"] == "link" and rows[linked]["MFN member id"] == "990001"
    assert "city (empty) -> Santiago" in rows[linked]["what changes"]


def test_a_contacts_domain_alone_links(world):
    pid = party(world, "Ostra Cargo", "Chile", ["ostracargo.test"])
    result = plan(world, [office("990010", "Ostra Cargo Ltda", "Valparaiso", "Chile", "CL")],
                  [contact("990010", "Rina Holt", "rina@ostracargo.test")])
    assert row_of(result, pid).proposal == "link" and row_of(result, pid).member == "990010"


def test_a_match_only_in_another_country_is_check_with_the_evidence(world):
    pid = party(world, "Harlow Shipping", "USA", ["qzharlowx.com.sg"], city="Singapore")
    add_enquiry(world["repo"], "OPP-8901", pid, origin_country="Singapore", dest_country="USA")
    world["conn"].commit()
    result = plan(world, [office("990020", "Harlow Shipping Pte. Ltd.", "Singapore", "Singapore", "SG",
                                web="qzharlowx.com.sg")])
    row = row_of(result, pid)
    assert row.proposal == "check" and row.member == "990020"
    shown = proposed(world)[pid]
    assert shown["MFN country"] == "Singapore"
    assert "OppList city Singapore" in shown["country evidence"]
    assert "domain codes SG" in shown["country evidence"]
    assert "cases Singapore 1, USA 1" in shown["country evidence"] or \
        "cases USA 1, Singapore 1" in shown["country evidence"]
    text = result.summary_path.read_text(encoding="utf-8")
    assert "USA -> Singapore" in text and "OPP-8901" in text
    # its own domain carries another country's code: listed
    assert "Harlow Shipping (id {}, USA): qzharlowx.com.sg".format(pid) in text


def test_a_same_name_abroad_is_not_offered_when_the_domain_carries_its_own_country(world):
    own = party(world, "Pemberly Logistics", "Peru", ["qzpemberly.com.pe"])
    bare = party(world, "Lorvan Cargo", "Peru")
    result = plan(world, [
        office("990030", "Pemberly Logistics S.A.", "Santiago", "Chile", "CL"),
        office("990031", "Lorvan Cargo Pty Ltd", "Singapore", "Singapore", "SG"),
        office("990032", "Somebody Else Peru", "Lima", "Peru", "PE"),
    ])
    assert row_of(result, own).proposal == "not in MFN" and row_of(result, own).member == ""
    assert row_of(result, bare).proposal == "check" and row_of(result, bare).member == "990031"
    assert "the same name only in Singapore" in row_of(result, bare).why


def test_countries_are_compared_by_iso_code_not_by_name(world):
    pid = party(world, "Quillon Freight", "Chile", ["quillon.test"])
    usa = party(world, "Amberlin Lines", "USA", ["amberlin.test"])
    result = plan(world, [
        office("990001", "Quillon Freight SpA", "Santiago", "Republica de Chile", "CL", web="quillon.test"),
        office("990040", "Amberlin Lines Inc", "Houston", "United States", "US", web="amberlin.test"),
        # the same name as an OppList country, another code: not the party's country
        office("990041", "Quillon Freight Annex", "Lima", "Chile", "PE", web="quillon.test"),
    ])
    assert row_of(result, pid).proposal == "link" and row_of(result, pid).member == "990001"
    assert "country" not in row_of(result, pid).changes
    assert row_of(result, usa).proposal == "link" and "country" not in row_of(result, usa).changes


def test_an_unknown_country_is_always_check(world):
    pid = party(world, "Keswick Cargo", "Unknown", ["keswick.test"])
    result = plan(world, [office("990050", "Keswick Cargo Ltda", "Santiago", "Chile", "CL", web="keswick.test")])
    row = row_of(result, pid)
    assert row.proposal == "check" and row.member == "990050" and row.target == ""
    assert "never linked by itself" in row.why


def test_two_parties_on_one_office_are_merge_and_left_alone(world):
    a = party(world, "Tallis Freight", "Chile", ["tallis.test"])
    b = party(world, "Tallis Freight Chile", "Chile", ["tallisfreight.test"])
    result = plan(world, [office("990060", "Tallis Freight SpA", "Santiago", "Chile", "CL",
                                 web="tallis.test", mail="tallisfreight.test")])
    assert row_of(result, a).merge_with == [b] and row_of(result, b).merge_with == [a]
    assert proposed(world)[a]["merge?"] == "Tallis Freight Chile"
    mfn_link.apply(world["conn"], result)
    assert party_row(world, a)["mfn_no"] is None and party_row(world, b)["mfn_no"] is None


# ------------------------------------------------------------- decisions ----
def test_the_decisions_file_is_made_once_and_never_rewritten(world):
    party(world, "Quillon Freight", "Chile", ["quillon.test"])
    offices = [office("990001", "Quillon Freight SpA", "Santiago", "Chile", "CL", web="quillon.test")]
    result = plan(world, offices)
    target = world["out"] / mfn_link.DECISIONS_NAME
    assert result.created_decisions
    with open(target, encoding="utf-8-sig") as handle:
        assert handle.read().strip() == "party id,decision,group,note"
    target.write_bytes("party id,decision,group,note\r\n1,skip,,not this one\r\n".encode("utf-8"))
    before = target.read_bytes()
    result = plan(world, offices)
    assert not result.created_decisions and target.read_bytes() == before
    assert proposed(world)[1]["decision"] == "skip" and proposed(world)[1]["note"] == "not this one"


def test_a_decision_wins_over_the_proposal(world):
    skipped = party(world, "Quillon Freight", "Chile", ["quillon.test"])
    chosen = party(world, "Tordel Logistics", "Uruguay")
    result = plan(world, [
        office("990001", "Quillon Freight SpA", "Santiago", "Chile", "CL", web="quillon.test"),
        office("990004", "Tordel Logistics S.A.", "Montevideo", "Uruguay", "UY"),
        office("990005", "Tordel Logistica Limitada", "Lima", "Peru", "PE"),
    ], decisions=[(skipped, "skip", "", ""), (chosen, "990005", "", "the Lima office")])
    assert row_of(result, skipped).target == "" and row_of(result, chosen).target == "990005"
    shown = proposed(world)
    assert shown[chosen]["decision"] == "990005" and shown[chosen]["MFN member id"] == "990005"
    mfn_link.apply(world["conn"], result)
    assert party_row(world, skipped)["mfn_no"] is None
    row = party_row(world, chosen)
    assert (row["mfn_no"], row["country"], row["city"]) == ("990005", "Peru", "Lima")
    text = result.summary_path.read_text(encoding="utf-8")
    assert "Uruguay -> Peru" in text and "Decision: 990005" in text


def test_a_copy_of_the_proposed_file_works_as_the_decisions_file(world):
    pid = party(world, "Tordel Logistics", "Uruguay")
    offices = [office("990004", "Tordel Logistics S.A.", "Montevideo", "Uruguay", "UY")]
    plan(world, offices)
    rows = list(csv.DictReader(open(world["out"] / mfn_link.PROPOSED_NAME, encoding="utf-8-sig")))
    rows[0]["decision"] = "link"
    with open(world["out"] / mfn_link.DECISIONS_NAME, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    result = plan(world, offices)
    assert row_of(result, pid).decision == "link" and row_of(result, pid).target == "990004"


# ----------------------------------------------------------------- apply ----
def test_apply_writes_exactly_the_link_fields(world):
    pid = party(world, "Quillon", "Chile", ["quillon.test"], stage="prospect", notes="met at a fair",
                aliases=json.dumps(["Quillon Old"]))
    world["repo"].insert("contact", {"party_id": pid, "name": "Ivo Marsh", "email": "IVO@quillon.test"})
    undecided = party(world, "Tordel Logistics", "Uruguay")
    world["conn"].commit()
    before = dict(party_row(world, pid))
    result = plan(world, [
        office("990001", "Quillon Freight SpA", "Santiago", "Chile", "CL", web="quillon.test",
               mail="quillonmail.test", fka="Quillon Cargo Ltda"),
        office("990004", "Tordel Logistics S.A.", "Montevideo", "Uruguay", "UY"),
    ], [contact("990001", "Ivo Marsh", "ivo@quillon.test", "Director"),
        contact("990001", "Lena Pike", "lena@quillon.test", "Sales"),
        contact("990001", "Lena Pike", "lena@quillon.test", "Sales manager")])
    done = mfn_link.apply(world["conn"], result)
    after = dict(party_row(world, pid))
    changed = {k for k in before if before[k] != after[k]} - {"updated_at"}
    assert changed == {"mfn_no", "mfn_member", "full_name", "city", "aliases"}
    assert (after["mfn_no"], after["mfn_member"], after["full_name"], after["city"]) == \
        ("990001", 1, "Quillon Freight SpA", "Santiago")
    assert json.loads(after["aliases"]) == ["Quillon Old", "Quillon Cargo Ltda"]
    assert (after["key"], after["stage"], after["notes"], after["type"]) == \
        ("Quillon", "prospect", "met at a fair", "Agent")
    domains = [r[0] for r in world["conn"].execute(
        "SELECT domain FROM party_domain WHERE party_id = ? ORDER BY domain", (pid,))]
    assert domains == ["quillon.test", "quillonmail.test"]
    contacts = world["conn"].execute("SELECT name, email, title, notes FROM contact WHERE party_id = ? "
                                     "ORDER BY id", (pid,)).fetchall()
    assert [tuple(c) for c in contacts] == [
        ("Ivo Marsh", "IVO@quillon.test", None, None),
        ("Lena Pike", "lena@quillon.test", "Sales", "MFN 990001, {}".format(result.directory.date))]
    assert done.contacts == 1 and done.domains == 1
    sources = {r[0] for r in world["conn"].execute(
        "SELECT DISTINCT source FROM change_log WHERE row_id = ? AND table_name = 'party' "
        "AND field = 'mfn_no'", (pid,))}
    assert sources == {"mfn-link"}
    # an undecided check row is left alone
    assert party_row(world, undecided)["mfn_no"] is None


def test_another_partys_domain_is_never_moved(world):
    pid = party(world, "Quillon Freight", "Chile", ["quillon.test"])
    other = party(world, "Quillon Mail Desk", "Peru", ["quillonmail.test"])
    result = plan(world, [office("990001", "Quillon Freight SpA", "Santiago", "Chile", "CL",
                                 web="quillon.test", mail="quillonmail.test")])
    assert "quillonmail.test is another party's" in proposed(world)[pid]["what changes"]
    done = mfn_link.apply(world["conn"], result)
    owner = world["conn"].execute("SELECT party_id FROM party_domain WHERE domain = 'quillonmail.test'").fetchone()
    assert owner[0] == other and "quillonmail.test" in done.held


def test_a_rerun_after_a_mfn_change_offers_only_that_change(world):
    pid = party(world, "Quillon Freight", "Chile", ["quillon.test"])
    offices = [office("990001", "Quillon Freight SpA", "Santiago", "Chile", "CL", web="quillon.test")]
    people = [contact("990001", "Lena Pike", "lena@quillon.test")]
    mfn_link.apply(world["conn"], plan(world, offices, people))
    again = plan(world, offices, people)
    assert row_of(again, pid).proposal == "linked" and row_of(again, pid).changes == {}
    # she renames the company by hand; MFN then moves the office and adds a person
    repo = db.Repository(world["conn"], "ui")
    repo.update("party", pid, {"full_name": "Quillon Freight (her spelling)"})
    repo.commit()
    offices[0].update(city="Valparaiso", company="Quillon Freight Chile SpA")
    people.append(contact("990001", "Omar Vey", "omar@quillon.test"))
    again = plan(world, offices, people)
    row = row_of(again, pid)
    assert row.proposal == "linked"
    assert set(row.changes) == {"city", "contacts"} and row.changes["city"] == "Valparaiso"
    assert [c["email"] for c in row.changes["contacts"]] == ["omar@quillon.test"]
    assert "Quillon Freight (id {}, member 990001): city Santiago -> Valparaiso".format(pid) in \
        again.summary_path.read_text(encoding="utf-8")
    mfn_link.apply(world["conn"], again)
    after = party_row(world, pid)
    assert after["city"] == "Valparaiso" and after["full_name"] == "Quillon Freight (her spelling)"


# ----------------------------------------------------------------- groups ---
def group_world(world):
    chile = party(world, "Marlowe Logistics Chile", "Chile", ["marlowe.test"])
    peru = party(world, "Marlowe Logistics Peru", "Peru", ["marlowe-pe.test"])
    hint = party(world, "Marlowe Logistics Uruguay", "Uruguay")
    offices = [
        office("990101", "Marlowe Logistics Chile SpA", "Santiago", "Chile", "CL", web="marlowe.test",
               kind="Head Office"),
        office("990102", "Marlowe Logistics Peru SAC", "Lima", "Peru", "PE", web="marlowe-pe.test",
               mail="marlowe.test"),
    ]
    return chile, peru, hint, offices


def test_offices_sharing_a_domain_make_one_group_and_a_hint_waits_for_her(world):
    chile, peru, hint, offices = group_world(world)
    result = plan(world, offices)
    group = result.groups["groups"]["MARLOWE"]
    assert group["members"] == [chile, peru]
    assert group["notes"] == "MFN {}: 2 offices in Chile, Peru; head office Marlowe Logistics Chile SpA, " \
        "Chile".format(result.directory.date)
    assert row_of(result, hint).hint == "join MARLOWE"
    assert proposed(world)[hint]["group hint"] == "join MARLOWE" and proposed(world)[hint]["group"] == ""
    mfn_link.apply(world["conn"], result)
    gid = world["conn"].execute("SELECT id, name, notes FROM party_group WHERE key = 'MARLOWE'").fetchone()
    assert gid["name"] == "Marlowe" and gid["notes"] == group["notes"]
    assert party_row(world, chile)["group_id"] == gid["id"] == party_row(world, peru)["group_id"]
    assert party_row(world, hint)["group_id"] is None
    # decided, the hint is written
    result = plan(world, offices, decisions=[(hint, "", "Marlowe", "")])
    mfn_link.apply(world["conn"], result)
    assert party_row(world, hint)["group_id"] == gid["id"]


def test_none_keeps_a_party_out_and_a_party_in_a_group_stays(world):
    chile, peru, hint, offices = group_world(world)
    third = party(world, "Marlowe Logistics Panama", "Peru", ["marlowe-pa.test"])
    offices.append(office("990103", "Marlowe Logistics Panama", "Lima", "Peru", "PE", mail="marlowe-pa.test",
                          web="marlowe.test"))
    group = world["repo"].add_party_group("MARVIS-LIKE")
    world["repo"].update("party", third, {"group_id": group})
    world["conn"].commit()
    # the office already in a group takes the others in; she keeps Peru out,
    # and a decision that would move the grouped party is a conflict
    result = plan(world, offices, decisions=[(peru, "", "none", ""), (third, "", "MARLOWE", "")])
    assert result.groups["groups"]["MARVIS-LIKE"]["members"] == [chile, third]
    assert any("Marlowe Logistics Panama is in group MARVIS-LIKE; decided MARLOWE" in c
               for c in result.groups["conflicts"])
    mfn_link.apply(world["conn"], result)
    assert party_row(world, peru)["group_id"] is None
    assert party_row(world, chile)["group_id"] == group
    assert party_row(world, third)["group_id"] == group
    assert world["conn"].execute("SELECT key FROM party_group WHERE id = ?", (group,)).fetchone()[0] == "MARVIS-LIKE"


def test_new_group_hints_need_two_countries_and_a_telling_word(world):
    a = party(world, "Pellucid Cargo (Chile)", "Chile")
    b = party(world, "Pellucid Cargo Peru", "Peru")
    c = party(world, "Cargo Logistics Chile", "Chile")
    d = party(world, "Cargo Logistics Peru", "Peru")
    result = plan(world, [office("990200", "Unrelated Office", "Lima", "Peru", "PE")])
    assert row_of(result, a).hint.startswith("new group PELLUCID CARGO")
    assert row_of(result, b).hint.startswith("new group PELLUCID CARGO")
    assert row_of(result, c).hint == "" and row_of(result, d).hint == ""
    mfn_link.apply(world["conn"], result)
    assert world["conn"].execute("SELECT COUNT(*) FROM party_group").fetchone()[0] == 0


# ------------------------------------------------------------------- cli ----
def test_the_command_makes_the_restore_point_before_writing(world, tmp_path, capsys):
    pid = party(world, "Quillon Freight", "Chile", ["quillon.test"])
    write_directory(world["dir"], [office("990001", "Quillon Freight SpA", "Santiago", "Chile", "CL",
                                          web="quillon.test")])
    world["conn"].close()
    restore = tmp_path / "restore"
    base = ["mfn-link", "--db", str(world["path"]), "--dir", str(world["dir"]), "--out-dir",
            str(world["out"]), "--reports-dir", str(world["reports"]), "--restore-dir", str(restore)]
    assert cli.main(base) == 0
    assert "dry run" in capsys.readouterr().out and not restore.exists()
    conn = sqlite3.connect(world["path"])
    assert conn.execute("SELECT mfn_no FROM party WHERE id = ?", (pid,)).fetchone()[0] is None
    conn.close()
    assert cli.main(base + ["--apply"]) == 0
    out = capsys.readouterr().out
    assert "restore point" in out and "parties written     : 1" in out
    points = list(restore.glob("opplist_before_mfn-link_*.db"))
    assert len(points) == 1
    old = sqlite3.connect(points[0])
    assert old.execute("SELECT mfn_no FROM party WHERE id = ?", (pid,)).fetchone()[0] is None
    assert old.execute("SELECT COUNT(*) FROM change_log WHERE source = 'mfn-link'").fetchone()[0] == 0
    old.close()
    conn = sqlite3.connect(world["path"])
    assert conn.execute("SELECT mfn_no FROM party WHERE id = ?", (pid,)).fetchone()[0] == "990001"
    conn.close()
