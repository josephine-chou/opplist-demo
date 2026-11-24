"""Batch MR1: the team site's month report follows 8765 - the default month
(last month) is pushed live, built as 8765's report page builds it, until
she exports it; at the turn of the month a live month never exported is
exported, so it stays on the site.

Every company, place, date and number here is invented (test_firebase_sync's
world: cases OPP-7101 to 7110; this file adds from OPP-7151).
"""

from __future__ import annotations

import datetime as dt
import json

from conftest import add_enquiry
from opplist import firebase_sync as fs, monthly
from test_firebase_sync import FakeFirestore, TODAY, run, world  # noqa: F401  (the W1a fixture)

T1 = dt.datetime(2026, 12, 14, 9, 0)
T2 = dt.datetime(2026, 12, 14, 11, 30)


def state(w):
    return json.loads(w["state"].read_text(encoding="utf-8"))


def test_the_live_month_is_pushed_when_not_exported(world):
    w = world
    fake = FakeFirestore()
    result = run(w, fake, now=T1)
    assert result.ok and "2026-11 (live)" in result.reports
    doc = fake.docs["reports/2026-11"]
    assert {k: v for k, v in doc.items() if k != "html"} == {
        "month": "2026-11", "title": "NOVEMBER REPORT", "exportedAt": "", "live": True,
        "updatedAt": "2026-12-14T09:00:00"}
    # the same page as 8765's report and Export
    from opplist import monthly_export
    assert doc["html"] == monthly_export.render_html(monthly.build(w["conn"], "2026-11", dt.date(2026, 12, 14)))
    assert state(w)["live_report"]["month"] == "2026-11"


def test_an_exported_month_is_pushed_as_the_file_and_not_live(world):
    w = world
    fake = FakeFirestore()
    run(w, fake, now=T1)
    out = w["tmp"] / "out" / "NOVEMBER REPORT.html"
    out.parent.mkdir()
    out.write_text("<html>november, exported</html>", encoding="utf-8")
    fs.record_export("2026-11", "NOVEMBER REPORT", out, when=dt.datetime(2026, 12, 14, 10, 0), record=w["record"])
    run(w, fake, now=T2)
    assert fake.docs["reports/2026-11"] == {"month": "2026-11", "title": "NOVEMBER REPORT",
                                           "html": "<html>november, exported</html>",
                                           "exportedAt": "2026-12-14T10:00:00"}
    assert fake.docs["meta/sync"]["reports"] == [{"month": "2026-11", "title": "NOVEMBER REPORT",
                                                  "exportedAt": "2026-12-14T10:00:00"}]
    assert "live_report" not in state(w)


def test_an_unchanged_live_report_is_not_written_again_and_keeps_its_time(world):
    w = world
    fake = FakeFirestore()
    run(w, fake, now=T1)
    again = run(w, fake, now=T2)
    assert again.written == 1 and fake.commits[-1] == ["meta/sync"]
    assert fake.docs["reports/2026-11"]["updatedAt"] == "2026-12-14T09:00:00"
    assert fake.docs["meta/sync"]["reports"][0]["updatedAt"] == "2026-12-14T09:00:00"


def test_a_case_that_changes_rewrites_it_with_a_new_time(world):
    w = world
    fake = FakeFirestore()
    run(w, fake, now=T1)
    before = fake.docs["reports/2026-11"]["html"]
    add_enquiry(w["repo"], "OPP-7151", w["conn"].execute("SELECT id FROM party WHERE key = 'Kestrel Forwarding'"
                                                         ).fetchone()[0],
                received="2026-11-18", status="Quoted", service_mode="Air", origin_country="USA",
                dest_country="Chile")
    w["repo"].commit()
    run(w, fake, now=T2)
    doc = fake.docs["reports/2026-11"]
    assert doc["html"] != before and doc["updatedAt"] == "2026-12-14T11:30:00" and doc["live"] is True
    assert "reports/2026-11" in fake.commits[-2]


def test_the_turn_of_the_month_exports_the_old_live_month(world, caplog):
    w = world
    fake = FakeFirestore()
    run(w, fake, now=T1)
    pushed = fake.docs["reports/2026-11"]["html"]
    with caplog.at_level("INFO", logger="opplist.firebase"):
        result = run(w, fake, today="2027-01-04", now=dt.datetime(2027, 1, 4, 8, 15))
    assert result.ok
    # November: exported as Export writes it, recorded, on the site as the file
    record = json.loads(w["record"].read_text(encoding="utf-8"))["months"]["2026-11"]
    path = w["reports"] / "{}.html".format(monthly.file_stem("2026-11"))
    assert record["path"] == str(path.resolve()) and record["title"] == "NOVEMBER REPORT"
    nov = fake.docs["reports/2026-11"]
    assert "live" not in nov and nov["exportedAt"] == record["exportedAt"] and nov["html"] == pushed
    assert "2026-11 was live and never exported" in caplog.text
    # December is the live month now
    meta = {r["month"]: r for r in fake.docs["meta/sync"]["reports"]}
    assert sorted(meta) == ["2026-11", "2026-12"]
    assert meta["2026-12"]["live"] is True and meta["2026-12"]["updatedAt"] == "2027-01-04T08:15:00"
    assert "live" not in meta["2026-11"] and "updatedAt" not in meta["2026-11"]
    assert state(w)["live_report"]["month"] == "2026-12"


def test_meta_reports_carry_live_and_updated_at(world):
    w = world
    w["reports"].mkdir()
    (w["reports"] / "OCTOBER REPORT.html").write_text("<html>october</html>", encoding="utf-8")
    fake = FakeFirestore()
    run(w, fake, now=T1)
    assert [sorted(r) for r in fake.docs["meta/sync"]["reports"]] == [
        ["exportedAt", "month", "title"], ["exportedAt", "live", "month", "title", "updatedAt"]]
    assert [r["month"] for r in fake.docs["meta/sync"]["reports"]] == ["2026-10", "2026-11"]


def test_a_copy_never_pushes(world):
    w = world
    result = fs.sync(w["path"], state_file=w["state"], today=TODAY, reports_dir=w["reports"], record=w["record"])
    assert not result.ok and "only the live app pushes" in result.reason
    assert not w["reports"].exists() or not list(w["reports"].iterdir())
