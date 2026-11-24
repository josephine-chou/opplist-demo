"""Batch 21: direct customers, counted as their own group.

A direct customer's case is In when it is registered, like any other; scope
only decides whether a case counts, and Out is her own choice. SC1: the
reports leave a direct customer's case before October 2026 (and from
October one that is not LATAM) out by their own rule, scope untouched; the
one-off that set Out is retired (scope-back puts it back). Direct customers are a group of their own in the month
report, the dashboard and report.py, and nobody chases them: no R3 row, no
ladder, no draft. Every value here is invented.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import dashboard, db, direct_customers_out, intake, monthly, report, tracking
from opplist.web import create_app

TODAY = "2026-11-18"                                   # a Wednesday


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "dc.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    ids = {
        "fernhill": add_party(repo, "Fernhill Ceramics", ptype="Direct Customer", country="USA"),
        "quillon": add_party(repo, "Quillon Cargo", country="Brazil"),          # an agent
        "office": add_party(repo, "JoJo ZZQ", ptype="Office", country="USA"),
    }
    repo.commit()
    return conn, repo, ids, path


# ------------------------------------------------------------ scope -----
def test_a_direct_customers_case_is_registered_in(world):
    conn, repo, ids, _ = world
    case = repo.create_enquiry({"requester_party_id": ids["fernhill"], "received_on": "2026-11-16",
                                "service_mode": "Air", "origin_country": "USA",
                                "dest_country": "Brazil"}, "2026-11-16")
    repo.commit()
    assert conn.execute("SELECT scope FROM enquiry WHERE id = ?", (case,)).fetchone()[0] == "In"
    # Out stays her own choice: a case registered Out by hand stays Out
    out = repo.create_enquiry({"requester_party_id": ids["fernhill"], "received_on": "2026-11-16",
                               "scope": "Out"}, "2026-11-16")
    assert conn.execute("SELECT scope FROM enquiry WHERE id = ?", (out,)).fetchone()[0] == "Out"


def test_a_direct_customers_mail_is_drafted_without_scope(world):
    conn, repo, ids, _ = world
    repo.insert("party_domain", {"domain": "fernhill-ceramics.example", "party_id": ids["fernhill"]})
    repo.commit()

    class Mail:
        subject = "Quote needed // ORD - GRU"
        received_on = "2026-11-16"
        received_at = "2026-11-16 09:10"
        entry_id = "E-3401"
        conversation_id = "C-3401"
        conversation_topic = subject
        sender_name = "Mara Ilsted"
        sender_address = "mara@fernhill-ceramics.example"
        sender_internal = False
        to_addresses = ["desk@jojofreight.example"]
        cc_addresses = []
        recipients = to_addresses
        body_excerpt = "Hello, could you quote this one please."
        selection_count = 1
        notes = []

    draft = intake.build(conn, Mail())
    assert draft.values.get("requester_party_id") == ids["fernhill"]
    assert draft.values.get("scope") in (None, "In")
    assert "scope" not in draft.sources


def test_a_party_turned_direct_customer_leaves_its_cases_alone(world):
    conn, repo, ids, _ = world
    first = add_enquiry(repo, "OPP-3401", ids["quillon"], received="2026-08-04")
    second = add_enquiry(repo, "OPP-3402", ids["quillon"], received="2026-11-02")
    repo.update("party", ids["quillon"], {"type": "Direct Customer"})
    repo.commit()
    scopes = [r[0] for r in conn.execute("SELECT scope FROM enquiry WHERE id IN (?, ?) ORDER BY id",
                                         (first, second))]
    assert scopes == ["In", "In"]
    assert not conn.execute("SELECT 1 FROM change_log WHERE table_name = 'enquiry' AND field = 'scope'"
                            ).fetchone()


# ------------------------------------------------- the list (SC1) ---
def test_the_list_names_the_cases_the_report_leaves_out_and_writes_nothing(world):
    # SC1: was the one-off that set them Out; now read only, scope untouched
    conn, repo, ids, _ = world
    for opp, party, received, extra in (
            ("OPP-3403", "fernhill", "2026-08-19", {}), ("OPP-3404", "fernhill", "2026-09-30", {"status": "Won"}),
            ("OPP-3405", "fernhill", "2026-05-07", {"legacy": 1, "status": "Dropped"}),
            ("OPP-3406", "fernhill", "2026-07-01", {"scope": "Out"}),
            ("OPP-3407", "fernhill", "2026-10-01", {}), ("OPP-3408", "fernhill", "2026-11-03", {}),
            ("OPP-3409", "quillon", "2026-09-08", {}), ("OPP-3410", "office", "2026-09-09", {})):
        add_enquiry(repo, opp, ids[party], received=received, **extra)
    repo.insert("enquiry", {"opp_no": "OPP-3411", "requester_party_id": ids["fernhill"], "status": "New"})
    repo.commit()
    logged = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    listed = direct_customers_out.run(conn)
    assert [(r[0], r[4], r[7]) for r in listed.rows] == [
        ("OPP-3411", "before October", "In"), ("OPP-3405", "before October", "In"),
        ("OPP-3406", "before October", "Out"), ("OPP-3403", "before October", "In"),
        ("OPP-3404", "before October", "In")]
    assert conn.execute("SELECT COUNT(*) FROM enquiry WHERE scope = 'Out'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == logged


# ------------------------------------------------------------ groups ----
def _month_world(world):
    conn, repo, ids, path = world
    # SC1: to Brazil - a direct customer's case from October counts when it is LATAM
    a = add_enquiry(repo, "OPP-3412", ids["fernhill"], received="2026-11-09", service_mode="Air",
                    origin_country="USA", dest_country="Brazil")
    repo.set_status(a, "Quoted", "2026-11-10")
    repo.set_status(a, "Won", "2026-11-12", rate_used="own")
    add_enquiry(repo, "OPP-3413", ids["fernhill"], received="2026-11-16", service_mode="FCL",
                origin_country="USA", dest_country="Brazil")
    add_enquiry(repo, "OPP-3414", ids["quillon"], received="2026-11-10", service_mode="Air",
                origin_country="USA", dest_country="Brazil")
    # SC1: from Brazil - an office's case from October counts when it is LATAM
    add_enquiry(repo, "OPP-3415", ids["office"], received="2026-11-11", service_mode="LCL",
                origin_country="Brazil", dest_country="USA")
    repo.commit()
    return conn


def test_the_month_report_has_a_direct_customers_line(world):
    conn = _month_world(world)
    built = monthly.build(conn, "2026-11")
    rows = [(r.label, r.cells, r.cls) for r in built.by_scope.rows]
    assert ("Direct customers", ["2", "1", "1", "0", "1", "0"], "indent") in rows   # 17d: Total, Air, FCL, LCL, Won, Lost
    assert not any(label == "Other" for label, _, _ in rows)
    assert [r.cells[0] for r in built.by_scope.rows if r.label == "Total"] == ["4"]
    # the agents' groups stay agents only; since 17a-2 the direct
    # customers have their own group after them
    group, by_group = None, {}
    for r in built.top_agents.rows:
        if r.cls == "head":
            group = r.label
        elif r.cls != "note":
            by_group.setdefault(group, []).append(r.label)
    assert by_group.pop("Direct customers")
    assert by_group.pop("Our offices") == ["JoJo ZZQ"]       # 17i: the offices' own group
    assert sum(by_group.values(), []) == ["Quillon Cargo"]


def test_report_py_counts_direct_customers_outside_other(world):
    conn = _month_world(world)
    block = next(s for s in report.build(conn, "2026-11", TODAY).sections if s.title.startswith("1."))
    rows = {r[0]: r for r in block.rows}
    assert rows["Direct customers"][1] == 2
    assert rows["Other"][1] == 0                                   # Other agents + Unidentified
    assert rows["Total"][1] == 4
    trend = report.trend(conn, "2026-11", TODAY)
    assert "Direct customers %" in trend.columns


def test_the_dashboard_shows_direct_customers_in_orange(world):
    conn = _month_world(world)
    assert dashboard.GROUP_COLOURS["Direct customers"] == "#f2a93b"
    assert dashboard.group_of({"requester_type": "Direct Customer"}) == "Direct customers"
    dash = dashboard.build(conn, TODAY)
    assert ("Direct customers", 2, "#f2a93b") in dash.mix
    assert sum(dash.series["Direct customers"]) == 2
    assert sum(dash.series["Other agents"]) == 0                   # Quillon is a LATAM agent
    top = {r["key"]: r for r in dash.requesters}
    assert top["Fernhill Ceramics"]["customer"] == 1 and top["Quillon Cargo"]["customer"] == 0
    frag = dashboard.fragments(dash)
    assert "Direct customers" in frag["group_legend"] and "#f2a93b" in frag["mix_legend"]


# -------------------------------------------------------- follow-ups ----
def _quoted(repo, opp_no, party, quoted_on="2026-11-02"):
    case = add_enquiry(repo, opp_no, party, received="2026-10-29", service_mode="Air",
                       origin_country="USA", dest_country="Brazil")
    repo.set_status(case, "Quoted", quoted_on)
    return case


def test_no_r3_rows_and_no_badge_for_a_direct_customer(world):
    conn, repo, ids, _ = world
    _quoted(repo, "OPP-3416", ids["fernhill"])
    _quoted(repo, "OPP-3417", ids["quillon"])
    repo.commit()
    sections = {s.rule: s for s in tracking.run(conn, today=TODAY)}
    assert [i.label for i in sections["R3"].items] == ["OPP-3417"]
    # FX4: OPP-3417's step is Decide, and the Decide card is hidden by default: not in the badge
    assert [i.extra["step"] for i in sections["R3"].items] == ["Decide"]
    assert tracking.focus_count(conn, today=TODAY) == 0
    # with Decide shown again: the agent's row, never the direct customer's
    db.set_setting(conn, tracking.HIDDEN_SETTING, "")
    conn.commit()
    assert tracking.focus_count(conn, today=TODAY) == 1
    # other rules still see it (R8 asks for the place details); R3's cards do not
    model = tracking.page_model(conn, today=TODAY)
    chased = [row["opp"] for card in model["today_cards"] + model["backlog_cards"]
              for row in card.get("rows", []) if row.get("rule") == "R3"]
    assert chased == ["OPP-3417"]


def test_a_direct_customers_new_case_is_still_in_new_nothing_done_yet(world):
    conn, repo, ids, _ = world
    add_enquiry(repo, "OPP-3418", ids["fernhill"], received="2026-11-12")
    repo.commit()
    r1 = [i.label for i in tracking.rule_new_no_action(conn, TODAY, 1)]
    assert r1 == ["OPP-3418"]
    assert tracking.new_cases_off_every_card(conn, TODAY) == []


def test_the_case_page_says_direct_customer_no_follow_ups(world):
    conn, repo, ids, path = world
    dc = _quoted(repo, "OPP-3419", ids["fernhill"])
    agent = _quoted(repo, "OPP-3420", ids["quillon"])
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    body = client.get("/enquiries/{}".format(dc)).data.decode("utf-8")
    card = body[body.index('id="followups"'):]
    card = card[:card.index("</section>")]
    assert "Direct customer · no follow-ups" in card
    assert 'class="ladder"' not in card and 'id="fu-text"' not in card and "Log as sent" not in card
    other = client.get("/enquiries/{}".format(agent)).data.decode("utf-8")
    card = other[other.index('id="followups"'):]
    card = card[:card.index("</section>")]
    # FX4: no draft; the date and the ways a follow-up went out
    assert "Direct customer" not in card and 'class="ladder"' in card and 'id="fu-log"' in card
    assert 'id="fu-text"' not in card


# ------------------------------------- replacements for the v9-era tests --
def test_report_py_direct_customers_and_placeholders_are_their_own_lines(world):
    """Replaces test_report's Kalsec / (TBC) test: Other is now only the
    placeholders and the agents outside LATAM. SC1: a placeholder is not
    counted (the report rule's three requester types), its line stays at 0."""
    conn, repo, ids, _ = world
    tbc = add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    for opp_no, party in (("OPP-3421", ids["fernhill"]), ("OPP-3422", tbc)):
        repo.insert("enquiry", {"opp_no": opp_no, "requester_party_id": party,
                                "received_on": "2026-11-04", "status": "New"})
    repo.commit()
    block = next(s for s in report.build(conn, "2026-11", TODAY).sections if s.title.startswith("1."))
    rows = {r[0]: r for r in block.rows}
    assert rows["Direct customers"][1] == 1                        # no countries: not decided, counted
    assert rows["Unidentified"][1] == 0
    assert rows["Other"][1] == 0
    assert rows["Total"][1] == 1
    labels = [r[0] for r in block.rows]
    assert labels.index("Our offices") < labels.index("Direct customers") < labels.index("Other")


def test_the_dashboard_pipeline_and_the_weekly_series_in_four_groups(world):
    """Replaces test_dashboard's pipeline / weekly test (three groups)."""
    conn, repo, ids, _ = world
    tarnby = add_party(repo, "Tarnby Logistics", country="USA")                    # an agent outside LATAM
    a = add_enquiry(repo, "OPP-3423", ids["quillon"], received="2026-11-09", service_mode="Air",
                    origin_country="USA", dest_country="Brazil")                  # Mon, quoted Wed: 2 days
    repo.set_status(a, "Quoted", "2026-11-11")
    b = add_enquiry(repo, "OPP-3424", tarnby, received="2026-10-23", service_mode="FCL",
                    origin_country="China", dest_country="USA")                   # Fri, quoted Tue: 2 days
    repo.set_status(b, "Quoted", "2026-10-27")
    repo.set_status(b, "Won", "2026-10-30", rate_used="own")
    # SC1: the direct customer's cases go to Brazil (LATAM), so the rule counts them
    c = add_enquiry(repo, "OPP-3425", ids["fernhill"], received="2026-10-15", service_mode="Air",
                    origin_country="USA", dest_country="Brazil")                   # Thu, quoted Tue: 3 days
    repo.set_status(c, "Quoted", "2026-10-20")
    repo.set_status(c, "Lost", "2026-10-22", closed_reason="Price too high")
    add_enquiry(repo, "OPP-3426", ids["office"], received="2026-11-16", service_mode="LCL",
                origin_country="Brazil", dest_country="USA")                      # an office, New (SC1: LATAM)
    add_enquiry(repo, "OPP-3427", ids["fernhill"], received="2026-11-17", service_mode="Air",
                origin_country="USA", dest_country="Brazil")                       # a direct customer, New
    f = add_enquiry(repo, "OPP-3428", ids["quillon"], received="2026-11-03", service_mode="Air",
                    origin_country="USA", dest_country="Brazil")
    repo.set_status(f, "Dropped", "2026-11-04", closed_reason="Registered in error")
    repo.commit()

    dash = dashboard.build(conn, TODAY)
    C = dashboard.C
    assert dash.pipeline["parts"] == [("New", 2, C["new"]), ("Quoted", 1, C["quoted"])]
    assert (dash.pipeline["won_90"], dash.pipeline["lost_90"], dash.pipeline["win_rate_90"]) == (1, 1, 50)
    assert dash.weeks == ["Sep 28", "Oct 5", "Oct 12", "Oct 19", "Oct 26", "Nov 2", "Nov 9", "Nov 16"]
    assert dash.series == {"LATAM agents": [0, 0, 0, 0, 0, 1, 1, 0],
                           "Other agents": [0, 0, 0, 1, 0, 0, 0, 0],
                           "Our offices": [0, 0, 0, 0, 0, 0, 0, 1],
                           "Direct customers": [0, 0, 1, 0, 0, 0, 0, 1]}
    assert dash.speed == [(None, 0), (None, 0), (0, 1), (100, 1), (None, 0), (None, 0), (100, 1), (None, 0)]
    assert dash.mix == [("LATAM agents", 2, C["latam"]), ("Other agents", 0, C["other"]),
                        ("Our offices", 1, C["office"]), ("Direct customers", 1, "#f2a93b")]
