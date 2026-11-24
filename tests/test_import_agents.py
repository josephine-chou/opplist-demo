"""import-agents: the agent directory becomes
parties, contacts and a timeline. Pinned on a small workbook shaped like
the real one: a summary sheet to skip, two country sheets in the two
column layouts, and every matching case."""

from __future__ import annotations

import openpyxl
import pytest

from conftest import add_party, seed_reference
from opplist import import_agents


def workbook(path, chile_rows, brazil_rows=()):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Outreach Summary Report"
    ws.append(["S.C.A.D. AGENCY OUTREACH"])
    ws.append(["Country", "Agent Outreach"])
    ws.append(["Chile", 2])
    ws = wb.create_sheet("Chile")
    ws.append(["", None])
    ws.append(["Agent Name", "Agent Contact", "Outreach Date", "Successful Connection ",
               "Zoom Meeting (Y/N)", "Areas of Interest", "Opportunities Received", "Remarks"])
    for row in chile_rows:
        ws.append(row)
    ws = wb.create_sheet("Brazil")
    ws.append(["BRAZIL"])
    ws.append(["AGENT NAME ", "Agent Contact Name", "Contact Title", "Contact Email",
               "Outreach Date", "Successful Connection ", "Zoom Meeting (Y/N)",
               "Areas of Interest", "Opportunities Received", "Remarks"])
    for row in brazil_rows:
        ws.append(row)
    wb.save(path)
    return path


@pytest.fixture
def book(tmp_path):
    return workbook(tmp_path / "agent-directory.xlsx", chile_rows=[
        ["Tolvex Chile SpA", "Renata Vidal / renata@tolvex.example",
         "Email sent 5 Jan / 24 Feb", "YES", "YES", "Air / Ocean", "NO",
         "Strong in mining"],
        ["Bluefern Group SpA", "Mr. Nicolas Brandt / nicolas@bluefern.example",
         "Email sent 5 Jan / 24 Feb", None, None, None, None, None],
        ["Cumbre Andina Ltda", "info@cumbre.example",
         "First email outreach on the 28 january / March", None, "NO", None, None, None],
    ], brazil_rows=[
        ["Vexmora Logistica Ltda", "Livia Prado\nJoao Teixeira", "Director\nSales",
         "livia@vexmora.example\njoao@vexmora.example", "First email approach 1 of March",
         None, None, None, None, None],
        ["No Date Agent", "someone@nodate.example", "", "", "outreach by phone",
         None, None, None, None, None],
    ])


def run(repo, path, tmp_path):
    return import_agents.run(repo.conn, workbook=path,
                           review_path=tmp_path / "review.csv", repo=repo)


def test_dates_are_read_off_the_outreach_text():
    assert import_agents.outreach_dates("Email sent 5 Jan / 24 Feb") == \
        ["2026-01-05", "2026-02-24"]
    assert import_agents.outreach_dates(
        "First email outreach on the 28 january / 10 of March") == \
        ["2026-01-28", "2026-03-10"]
    assert import_agents.outreach_dates("Email Sent 9 feb / 3 March") == \
        ["2026-02-09", "2026-03-03"]
    assert import_agents.outreach_dates("First email outreach on the 28 january / March") == \
        ["2026-01-28"]
    assert import_agents.outreach_dates("outreach by phone") == []


def test_an_agent_known_by_domain_is_updated_not_duplicated(repo, book, tmp_path):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile", mfn_member=0)
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    repo.commit()
    report = run(repo, book, tmp_path)
    assert report.matched_by_domain == 1
    row = repo.conn.execute("SELECT key, mfn_member, notes, stage FROM party WHERE id = ?",
                            (tolvex,)).fetchone()
    assert row["key"] == "Tolvex" and row["mfn_member"] == 1
    assert "Agent directory (Chile)" in row["notes"] and row["stage"] == "active"
    assert repo.conn.execute("SELECT COUNT(*) FROM party WHERE key LIKE 'Tolvex%'"
                             ).fetchone()[0] == 1
    events = repo.conn.execute(
        "SELECT at, type, summary FROM party_event WHERE party_id = ? ORDER BY at, type",
        (tolvex,)).fetchall()
    assert [(e["at"], e["type"]) for e in events] == [
        ("2026-01-05", "outreach"), ("2026-02-24", "meeting"), ("2026-02-24", "note"),
        ("2026-02-24", "outreach"), ("2026-02-24", "reply")]
    assert "Strong in mining | interests: Air / Ocean" in [e["summary"] for e in events]
    contact = repo.conn.execute("SELECT name, email FROM contact WHERE party_id = ?",
                                (tolvex,)).fetchone()
    assert (contact["name"], contact["email"]) == ("Renata Vidal", "renata@tolvex.example")


def test_a_new_agent_is_a_mfn_prospect_with_its_domain_and_people(repo, book, tmp_path):
    seed_reference(repo)
    report = run(repo, book, tmp_path)
    assert report.created == 5 and report.sheets == 2 and report.agents == 5
    row = repo.conn.execute(
        "SELECT type, stage, source, country, mfn_member FROM party "
        "WHERE key = 'Vexmora Logistica Ltda'").fetchone()
    assert tuple(row) == ("Agent", "prospect", "directory", "Brazil", 1)
    assert repo.conn.execute("SELECT party_id FROM party_domain WHERE domain = 'vexmora.example'"
                             ).fetchone() is not None
    people = repo.conn.execute(
        "SELECT name, title, email FROM contact c JOIN party p ON p.id = c.party_id "
        "WHERE p.key = 'Vexmora Logistica Ltda' ORDER BY email").fetchall()
    assert [tuple(p) for p in people] == [
        ("Joao Teixeira", "Sales", "joao@vexmora.example"),
        ("Livia Prado", "Director", "livia@vexmora.example")]


def test_a_name_that_only_contains_anothers_words_goes_to_review(repo, book, tmp_path):
    seed_reference(repo)
    add_party(repo, "Cumbre", country="Chile")
    repo.commit()
    report = run(repo, book, tmp_path)
    assert report.matched_by_name == 0
    assert [(c, n, k) for c, n, k, _ in report.review] == [
        ("Chile", "Cumbre Andina Ltda", "Cumbre")]
    assert (tmp_path / "review.csv").read_text(encoding="utf-8-sig").startswith(
        "Country,Directory name,Existing party,Why")
    # not merged: the directory agent is its own new party
    assert repo.conn.execute("SELECT COUNT(*) FROM party WHERE key LIKE 'Cumbre%'"
                             ).fetchone()[0] == 2


def test_the_same_words_after_the_suffixes_is_a_match(repo, book, tmp_path):
    seed_reference(repo)
    bluefern = add_party(repo, "Bluefern Group", country="Chile")
    repo.commit()
    report = run(repo, book, tmp_path)
    assert report.matched_by_name == 1
    assert repo.conn.execute("SELECT mfn_member FROM party WHERE id = ?",
                             (bluefern,)).fetchone()[0] == 1


def test_no_date_means_the_first_of_january_with_the_text_kept(repo, book, tmp_path):
    seed_reference(repo)
    report = run(repo, book, tmp_path)
    assert report.undated == 1
    event = repo.conn.execute(
        "SELECT at, summary FROM party_event e JOIN party p ON p.id = e.party_id "
        "WHERE p.key = 'No Date Agent'").fetchone()
    assert (event["at"], event["summary"]) == ("2026-01-01", "outreach by phone")


def test_running_twice_adds_nothing(repo, book, tmp_path):
    seed_reference(repo)
    first = run(repo, book, tmp_path)
    counts = lambda: tuple(repo.conn.execute(
        "SELECT (SELECT COUNT(*) FROM party), (SELECT COUNT(*) FROM contact), "
        "(SELECT COUNT(*) FROM party_event), (SELECT COUNT(*) FROM party_domain)"
    ).fetchone())
    before = counts()
    second = run(repo, book, tmp_path)
    assert counts() == before
    assert second.created == 0 and second.events_added == 0
    assert second.events_skipped == first.events_added
    assert second.matched_by_domain == 5


def test_a_sheet_whose_country_is_unknown_is_still_read(repo, tmp_path):
    seed_reference(repo)
    path = tmp_path / "agent-directory.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Paraquay"
    ws.append(["Agent Name", "Agent Contact", "Outreach Date"])
    ws.append(["Belrix S.A.", "info@belrix.example", "Outreach 12 January"])
    ws = wb.create_sheet("Atlantis")
    ws.append(["Agent Name", "Agent Contact", "Outreach Date"])
    ws.append(["Poseidon Cargo", "p@poseidon.example", "Outreach 12 January"])
    wb.save(path)
    repo.insert("country", {"name": "Paraguay", "region": "Latin America"})
    repo.commit()
    report = run(repo, path, tmp_path)
    assert report.unknown_countries == ["Atlantis"]
    countries = dict(repo.conn.execute(
        "SELECT key, country FROM party WHERE source = 'directory'").fetchall())
    assert countries == {"Belrix S.A.": "Paraguay", "Poseidon Cargo": None}


def test_agent_domains_adds_what_is_missing_and_then_nothing(repo, book, tmp_path):
    seed_reference(repo)
    run(repo, book, tmp_path)
    before = repo.conn.execute("SELECT count(*) FROM party_domain").fetchone()[0]
    assert before >= 4
    # somebody removed two of them by hand
    repo.conn.execute("DELETE FROM party_domain WHERE domain IN ('tolvex.example', 'vexmora.example')")
    repo.commit()
    report = import_agents.add_domains(repo.conn, workbook=book)
    assert (report.added, report.taken) == (2, 0)
    assert report.matched == report.with_domains
    assert sorted(report.lines_added) == ["tolvex.example -> Tolvex Chile SpA", "vexmora.example -> Vexmora Logistica Ltda"]
    assert repo.conn.execute("SELECT count(*) FROM change_log WHERE table_name = 'party_domain'").fetchone()[0] >= 2
    again = import_agents.add_domains(repo.conn, workbook=book)
    assert again.added == 0 and again.already == before
    assert "domains added              : 0" in again.lines()
