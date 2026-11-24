"""Batch 12 follow-up (2026-09-25): the FU templates survive a Settings save
(CRLF posted), an old default is recognised whatever its line breaks, an
unknown blank is refused, the repair; the Request card without the lanes
line, the lanes in More, the Subjects block without keys or dates."""

from __future__ import annotations

import html as htmllib
import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, followup, repair_fu_templates, schema_migrations
from opplist.web import create_app

TODAY = "2026-09-24"
POOL = {k: len([p for p in followup.VERSION_SEPARATOR.split(v) if p.strip()])
        for k, v in schema_migrations.FU_SETTINGS if k.startswith("fu_template_")}


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "b12.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    ids = {"tolvex": tolvex}
    ids["quoted"] = add_enquiry(repo, "OPP-1901", tolvex, received="2026-09-21", service_mode="Air",
                                origin_loc="MIA", origin_country="USA", dest_loc="SAI", dest_country="Chile",
                                source_subject="RATE REQUEST // MIA - SAI // AIR", thread_key="RATEREQUESTMIASAIAIR")
    repo.set_status(ids["quoted"], "Quoted", "2026-09-23")
    ids["lane2"] = add_enquiry(repo, "OPP-1902", tolvex, received="2026-09-21", service_mode="Air",
                               origin_loc="MIA", origin_country="USA", dest_country="China")
    repo.set_status(ids["lane2"], "Dropped", "2026-09-22", closed_reason="Registered in error")
    repo.commit()
    repo.join_request(ids["lane2"], ids["quoted"], TODAY)
    repo.commit()
    conn.close()
    import opplist.web as web_module
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, ids


def textareas(body) -> dict:
    return {m.group(1): htmllib.unescape(m.group(2)) for m in re.finditer(
        r'<textarea name="(fu_template_\d)"[^>]*>(.*?)</textarea>', body, re.S)}


def stored(path, key):
    conn = db.open_db(path)
    try:
        return db.get_setting(conn, key, "")
    finally:
        conn.close()


def counts(path):
    conn = db.open_db(path)
    try:
        return {k: len(followup.versions(conn, int(k[-1]))) for k in POOL}
    finally:
        conn.close()


# ---------------------------------------------------------- templates ----
# FX4: test_saving_settings_untouched_keeps_every_version, test_an_old_default_with_crlf_is_recognised,
# test_her_own_template_is_kept_with_lf and test_an_unknown_blank_is_refused were rewritten with invented
# data on the new behaviour (no template boxes in Settings) in tests/test_fx4_rewritten.py


def test_the_pool_is_what_it_is():
    assert POOL == {"fu_template_1": 5, "fu_template_2": 6, "fu_template_3": 4}


def test_a_draft_never_shows_a_raw_blank(world):
    assert followup.fill("Hi {first_name},\nOn {quote_date} we quoted {shipment}.\n{shipment}\nThanks.",
                         {"first_name": "Ana"}) == "Hi Ana,\nOn we quoted.\nThanks.\n"


# test_the_case_page_offers_the_versions_of_fu1 moved to test_draft_fu_note.py with invented data (24c: Another version is gone)


def test_the_repair_empties_old_defaults_only(world, tmp_path):
    client, path, ids = world
    conn = db.open_db(path)
    olds = followup.OLD_FU_TEMPLATES
    db.set_setting(conn, "fu_template_1", olds["fu_template_1"][0].replace("\n", "\r\n"))
    db.set_setting(conn, "fu_template_2", "Hi {first_name}, my own words.")
    db.set_setting(conn, "fu_template_3", "")
    conn.commit()
    dry = repair_fu_templates.run(conn, dry_run=True)
    assert dry.changed == 1 and [r[1] for r in dry.rows] == ["old default -> empty", "her own, kept", "empty already"]
    assert db.get_setting(conn, "fu_template_1", "").startswith("Good day")
    done = repair_fu_templates.run(conn, dry_run=False)
    assert done.changed == 1 and db.get_setting(conn, "fu_template_1", "") == ""
    assert db.get_setting(conn, "fu_template_2", "") == "Hi {first_name}, my own words."
    [log] = conn.execute("SELECT table_name, field, new_value, source FROM change_log "
                         "WHERE table_name = 'setting'").fetchall()
    assert tuple(log) == ("setting", "fu_template_1", "", "migration")
    assert repair_fu_templates.run(conn, dry_run=False).changed == 0          # re-runnable
    conn.close()


# ------------------------------------------------------ Request card ----
def test_the_request_card_has_no_lanes_line_and_more_lists_them(world):
    client, path, ids = world
    body = client.get("/enquiries/{}".format(ids["quoted"])).data.decode("utf-8")
    request_card = body[body.index('id="request"'):body.index('id="cargo"')]
    assert "Lanes in this request" not in request_card and "OPP-1902" not in request_card
    menu = body[body.index('<details class="more-menu">'):]
    menu = menu[:menu.index("</details>")]
    item = '<a class="menu-item lane-item" href="/enquiries/{}">OPP-1902 &middot; MIA - China &middot; Air &middot; Dropped</a>'.format(ids["lane2"])
    assert item in menu                                    # an old request group, kept as history (v33)
    assert "Add a lane" not in menu and "Same request as" not in menu


def test_subjects_show_only_their_text(world):
    client, path, ids = world
    client.post("/enquiries/{}/subjects".format(ids["quoted"]),
                data={"subject": "RE: RATE REQUEST // MIA - SAI // AIR // REVISED"}, follow_redirects=True)
    body = client.get("/enquiries/{}".format(ids["quoted"])).data.decode("utf-8")
    subjects = body[body.index('id="subjects"'):body.index('class="add-subject"')]
    assert "RE: RATE REQUEST // MIA - SAI // AIR // REVISED" in subjects
    assert ">registered<" in subjects
    assert "added" not in subjects and "manual" not in subjects and "thread key" not in subjects
    assert "RATEREQUESTMIASAIAIR" not in body                 # no thread key anywhere on the page
    assert ">key<" not in subjects and "key " not in subjects
    # removing a subject works as before
    assert 'form="subject-line-' in subjects


def test_no_thread_key_in_the_flashes(world):
    client, path, ids = world
    page = client.post("/enquiries/{}/subjects".format(ids["quoted"]),
                       data={"subject": "RATE REQUEST // MIA - LIM // AIR"}, follow_redirects=True)
    body = page.data.decode("utf-8")
    assert "Subject added (" in body and "key" not in body[body.index("Subject added"):][:80]
