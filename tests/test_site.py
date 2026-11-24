"""The offline team site (site/) and the snapshot it runs on.

The site is JavaScript and its own tests run in Node or in a browser
(site/tests/). What Python can check without either is checked here: the
snapshot the demo command writes is what the site's store expects, it is the
same for the same seed and date, it is made by OppList's own push code, the
site's files name no host and import no cloud client, and the app serves the
folder.
"""

import datetime as dt
import hashlib
import json
import re

import pytest

from opplist import demo, leakcheck, paths, site_snapshot, site_snapshot_extras
from opplist.web import create_app

TODAY = dt.date(2026, 11, 24)
SITE = paths.PROJECT_DIR / "site"
MODULES = sorted(p for p in SITE.rglob("*.js") if "vendor" not in p.parts) + sorted(SITE.rglob("*.mjs"))
TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}$")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("site-demo")
    result = demo.generate(out, seed=42, today=TODAY)
    file = out / site_snapshot.FOLDER / site_snapshot.SNAPSHOT_NAME
    return out, result, file, json.loads(file.read_text(encoding="utf-8"))


def by_collection(snapshot) -> dict:
    out = {}
    for path, fields in snapshot["docs"].items():
        out.setdefault(path.split("/")[0], {})[path.split("/", 1)[1]] = fields
    return out


# ------------------------------------------------------------ the snapshot --
def test_the_demo_command_writes_the_snapshot(built):
    out, result, file, snapshot = built
    assert file.exists() and result["site_snapshot"] == str(file)
    assert result["site_docs"] == len(snapshot["docs"])
    assert snapshot["made"] == "42-2026-11-24" and snapshot["now"] == "2026-11-24T09:30:00"
    assert any("team site" in line for line in demo.report_lines(result, out))


def test_the_same_seed_and_date_give_the_same_bytes(built):
    out, _, file, _ = built
    first = hashlib.sha256(file.read_bytes()).hexdigest()
    again = site_snapshot.write(out, out / demo.DB_NAME, out / "mail-archive" / demo.ARCHIVE_NAME, TODAY, 42)
    assert hashlib.sha256(again.read_bytes()).hexdigest() == first
    other = site_snapshot.build(out / demo.DB_NAME, out / "mail-archive" / demo.ARCHIVE_NAME,
                                TODAY + dt.timedelta(days=1), 42)
    assert other["made"] == "42-2026-11-25" and other["now"].startswith("2026-11-25")


def test_making_the_snapshot_leaves_the_demo_as_it_was(built):
    out, _, _, _ = built
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (out / demo.DB_NAME, out / "mail-archive" / demo.ARCHIVE_NAME)}
    site_snapshot.write(out, out / demo.DB_NAME, out / "mail-archive" / demo.ARCHIVE_NAME, TODAY, 42)
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in (out / demo.DB_NAME, out / "mail-archive" / demo.ARCHIVE_NAME)}
    assert after == before
    assert not list(out.rglob("*-wal")) and not list(out.rglob("*-shm"))


def test_the_pushed_documents_are_the_ones_the_push_writes(built):
    """The site's half of the interface: what the pages read is there, under
    the names the push gives it."""
    _, result, _, snapshot = built
    cols = by_collection(snapshot)
    assert set(cols["lists"]) == {"assign", "inquiries", "parties", "places", "vendors"}
    meta = cols["meta"]["sync"]
    for key in ("pushedAt", "pulledAt", "owners", "quoteRules", "editLists", "lostReasons", "reports", "teamCc"):
        assert key in meta, key
    assert meta["waitReq"] is True and meta["options"] is True and meta["readQuote"]["on"] is True
    assert set(meta["quoteRules"]) == {"markupPct", "marginFloorPct", "officesNoMarkup"}
    rows = cols["lists"]["inquiries"]["rows"]
    assert len(rows) == len(cols["cases"]) == meta["counts"]["cases"]
    assert {r["opp"] for r in rows} == set(cols["cases"])
    for opp, case in cols["cases"].items():
        assert case["opp"] == opp and isinstance(case["tl"], list) and isinstance(case["rr"], list)
    assert set(cols["lists"]["assign"]) == {"waiting", "pending"}
    assert len(cols["lists"]["assign"]["waiting"]) == meta["counts"]["waiting"]
    places = cols["lists"]["places"]
    assert places["rows"] and set(places["rows"][0]) == {"k", "name", "country", "type", "code", "aliases"}
    [report] = cols["reports"].values()
    assert report["html"].lstrip().lower().startswith("<!doctype html") and report["month"] in cols["reports"]


def test_times_are_wall_clock_and_the_pull_is_before_the_page_opens(built):
    _, _, _, snapshot = built
    seen = []

    def walk(value):
        if isinstance(value, dict):
            if set(value) == {"__time"}:
                seen.append(value["__time"])
                return
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(snapshot["docs"])
    assert len(seen) > 5 and all(TIME.match(t) for t in seen)
    meta = snapshot["docs"]["meta/sync"]
    assert meta["pulledAt"]["__time"] == meta["pushedAt"]["__time"] == "2026-11-24T09:25:00.000"
    assert meta["pushedAt"]["__time"][:19] < snapshot["now"]


def test_the_people_the_site_can_be_viewed_as(built):
    _, _, _, snapshot = built
    users = by_collection(snapshot)["users"]
    assert len(users) == 3
    roles = sorted((u["role"], u["costs"], u["report"]) for u in users.values())
    assert roles == [("admin", True, True), ("member", False, False), ("member", True, True)]
    owners = snapshot["docs"]["meta/sync"]["owners"]
    for email, user in users.items():
        assert user["email"] == email and email.endswith("@jojofreight.example") and user["active"] is True
        assert user["owner"] in owners


def test_the_documents_only_the_site_writes(built):
    _, _, _, snapshot = built
    cols = by_collection(snapshot)
    rows = {r["opp"]: r for r in cols["lists"]["inquiries"]["rows"]}
    users = cols["users"]
    for note in cols["notes"].values():
        assert note["by"] in users and note["text"] and (not note["opp"] or note["opp"] in rows)
    # one Won waits for the administrator: the one action after OppList's last pull, with what was pasted
    pulled = snapshot["docs"]["meta/sync"]["pulledAt"]["__time"]
    [(action_id, action)] = [(k, a) for k, a in cols["actions"].items() if a["at"]["__time"] > pulled]
    assert action["type"] == "close" and action["result"] == "Won" and rows[action["opp"]]["st"] == "Quoted"
    assert action["by"] in users and cols["evidence"][action_id]["text"]
    # every other action was taken in before the pull, and OppList answered it
    for k, a in cols["actions"].items():
        if k != action_id:
            assert cols["actionResults"][k]["ok"] is True, k


def test_the_rates_and_quotes_the_site_wrote_are_the_ones_opplist_took_in(built):
    """The demo's work on the site: two vendors' rates (one with two options)
    and two quotes with their costs, as documents in the snapshot and as rows
    in the demo database, the same values in both."""
    import sqlite3
    out, _, _, snapshot = built
    cols = by_collection(snapshot)
    conn = sqlite3.connect("file:{}?mode=ro".format((out / demo.DB_NAME).as_posix()), uri=True)
    conn.row_factory = sqlite3.Row
    rates = cols["vendorRates"]
    assert len(rates) == 3 and len({r["opp"] for r in rates.values()}) == 1
    by_vendor = {}
    for doc in rates.values():
        by_vendor.setdefault(doc["vendor"], []).append(doc.get("label", ""))
    assert sorted(len(v) for v in by_vendor.values()) == [1, 2]
    for site_id, doc in rates.items():
        row = conn.execute("SELECT * FROM vendor_rate WHERE site_id = ?", (site_id,)).fetchone()
        assert row is not None and row["total"] == doc["total"] and (row["label"] or "") == doc.get("label", "")
    quotes, costs = cols["quotes"], cols["quoteCosts"]
    assert sorted(q["n"] for q in quotes.values()) == [1, 2] and set(costs) == set(quotes)
    for site_id, doc in quotes.items():
        row = conn.execute("SELECT * FROM quote WHERE site_id = ?", (site_id,)).fetchone()
        assert row["total"] == doc["total"] and row["with_costs"] == 1 and row["cost"] == costs[site_id]["cost"]
        assert sum(line["amount"] for line in doc["lines"]) == doc["total"]
        assert costs[site_id]["profit"] == doc["total"] - costs[site_id]["cost"] > 0
    opp = next(iter(quotes.values()))["opp"]
    case = cols["cases"][opp]
    assert case["st"] == "Quoted" and {r["vendor"] for r in case["rr"]} >= set(by_vendor)
    conn.close()


def test_the_reading_comes_from_read_quote_and_the_recorded_answer(built):
    """rateDrafts/<id> is what OppList pushes after a reading; the demo's one
    reading is made by read_quote.read on the recorded answer, and the site's
    stand-in answers a Read quote request with a copy of it."""
    _, _, _, snapshot = built
    cols = by_collection(snapshot)
    [draft] = cols["rateDrafts"].values()
    assert draft["status"] == "open" and draft["taken"] == [""] and draft["opp"] in cols["cases"]
    assert draft["vendor"] in {r["vendor"] for r in cols["cases"][draft["opp"]]["rr"]}
    [option] = draft["options"]
    assert [(line["charge"], line["rate"], line["unit"]) for line in option["lines"]] == [
        ("Ocean freight", 1840, "40'HQ"), ("THC at origin", 210, "CNTR"), ("Documentation", 65, "BL")]
    assert option["carrier"] == "Seabright Line" and option["valid"] == "2027-06-30"
    assert draft["readAt"]["__time"] == "2026-11-24T09:25:00.000"
    answer = cols["demo"]["readAnswer"]["draft"]
    assert answer["options"] == draft["options"] and "opp" not in answer and "vendor" not in answer


def test_encode_keeps_plain_values_and_marks_times():
    moment = dt.datetime(2026, 11, 24, 9, 25, 0, 123456, tzinfo=dt.timezone.utc)
    assert site_snapshot.encode({"a": [1, "x", None, True, moment], "b": {"c": 2.5}}) == {
        "a": [1, "x", None, True, {"__time": "2026-11-24T09:25:00.123"}], "b": {"c": 2.5}}
    assert site_snapshot_extras.address("Ava Tran") == "ava.tran@jojofreight.example"


# ---------------------------------------------------------- the site's files --
def test_the_page_modules_import_the_local_store_and_nothing_from_outside():
    assert len(MODULES) >= 30
    spec = re.compile(r"""(?:^|[\s;])(?:import|export)\b[^'"`;]*?\bfrom\s*["']([^"']+)["']|\bimport\s*\(\s*["']([^"']+)["']""", re.M)
    for path in MODULES:
        text = path.read_text(encoding="utf-8")
        for a, b in spec.findall(text):
            target = a or b
            assert target.startswith(("./", "../")), "{} imports {}".format(path.name, target)
            assert (path.parent / target).resolve().is_file(), "{} imports a file that is not there: {}".format(path.name, target)
            assert "firebase" not in target.lower(), path.name
    store = (SITE / "store" / "firestore.js").read_text(encoding="utf-8")
    assert "fetch(" not in store and "XMLHttpRequest" not in store and "WebSocket" not in store


def test_the_sites_own_files_name_no_host():
    """No address of a server anywhere in the site's own files: the one
    request it makes is for its snapshot, by a relative path."""
    url = re.compile(r"""(?:https?|wss?|ftp):/{2}[^\s"'`<>)]+""")
    # the SVG namespace of the icon (a name, not an address) and the visitor's own machine in the README
    allowed = ("http://www.w3.org/2000/svg", "http://127.0.0.1:")
    found = []
    for path in sorted(SITE.rglob("*")):
        if not path.is_file() or "vendor" in path.parts or path.suffix not in (".js", ".mjs", ".html", ".css", ".md"):
            continue
        for m in url.finditer(path.read_text(encoding="utf-8")):
            if not m.group(0).startswith(allowed):
                found.append("{}: {}".format(path.relative_to(SITE).as_posix(), m.group(0)))
    assert not found, found
    fetches = []
    for path in MODULES:
        if "tests" in path.parts:
            continue
        for m in re.finditer(r"\bfetch\(([^)]*)\)", path.read_text(encoding="utf-8")):
            fetches.append((path.relative_to(SITE).as_posix(), m.group(1)))
    # the snapshot, the PDF's fonts, and the official place lists (switched off in this copy)
    assert sorted({name for name, _ in fetches}) == ["quotepdf.js", "store/boot.js", "views/edit.js"]
    assert all(arg.startswith(("new URL(\"../\" + SNAPSHOT", "`vendor/fonts/", "\"official/", "`official/")) for _, arg in fetches), fetches
    assert "const OFFICIAL = false;" in (SITE / "views" / "edit.js").read_text(encoding="utf-8")


def test_no_module_reads_the_computers_clock_itself():
    """The demo is relative to its day: every date comes from lib.js (now)."""
    allowed = {"lib.js": None, "core.js": 2}           # core.js measures a double click, twice
    for path in MODULES:
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        n = len(re.findall(r"\bDate\.now\(\)|new Date\(\)", text))
        rel = path.relative_to(SITE).as_posix()
        if rel in allowed:
            assert allowed[rel] is None or n == allowed[rel], rel
        else:
            assert n == 0, "{} reads the clock {} time(s)".format(rel, n)


def test_no_image_is_shipped_and_the_third_party_files_are_the_pinned_ones():
    images = [p.name for p in SITE.rglob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp")]
    assert images == []
    vendor = sorted(p.relative_to(SITE).as_posix() for p in (SITE / "vendor").rglob("*") if p.is_file())
    assert vendor == ["vendor/fonts/Inter-Medium.ttf", "vendor/fonts/Inter-Regular.ttf", "vendor/fonts/Inter-SemiBold.ttf",
                      "vendor/fonts/Newsreader-Italic.ttf", "vendor/fonts/Newsreader-Regular.ttf",
                      "vendor/fonts/OFL-Inter.txt", "vendor/fonts/OFL-Newsreader.txt", "vendor/jspdf-4.2.1.umd.min.js"]
    for rel, want in leakcheck.THIRD_PARTY.items():
        file = paths.PROJECT_DIR / rel
        assert hashlib.sha256(file.read_bytes()).hexdigest() == want, rel
        assert leakcheck.is_pinned_third_party(file, paths.PROJECT_DIR)
    for name in ("OFL-Inter.txt", "OFL-Newsreader.txt"):
        assert "SIL OPEN FONT LICENSE" in (SITE / "vendor" / "fonts" / name).read_text(encoding="utf-8").upper()


def test_a_changed_third_party_file_is_scanned_again(tmp_path):
    rel = next(iter(leakcheck.THIRD_PARTY))
    copy = tmp_path / rel
    copy.parent.mkdir(parents=True)
    copy.write_bytes((paths.PROJECT_DIR / rel).read_bytes() + b"\n// changed")
    assert not leakcheck.is_pinned_third_party(copy, tmp_path)


def test_the_test_page_lists_every_test_file():
    page = (SITE / "tests" / "run.html").read_text(encoding="utf-8")
    listed = set(json.loads(re.search(r"const ALL = (\[[^\]]+\]);", page).group(1)))
    assert listed == {p.name[:-len("_test.mjs")] for p in (SITE / "tests").glob("*_test.mjs")}


# ------------------------------------------------------------------ served --
def test_the_app_serves_the_site_and_its_snapshot(built, monkeypatch):
    out, _, file, _ = built
    monkeypatch.setattr(paths, "DATA_DIR", out)
    app = create_app(db_path=out / demo.DB_NAME)
    app.config["TESTING"] = True
    client = app.test_client()
    assert client.get("/site").status_code in (301, 302, 308)
    page = client.get("/site/")
    assert page.status_code == 200 and b'<script type="module" src="app.js">' in page.data
    assert client.get("/site/store/firestore.js").status_code == 200
    got = client.get("/site/data/snapshot.json")
    assert got.status_code == 200 and got.data == file.read_bytes()
    assert client.get("/site/nothing-here.js").status_code == 404
    assert client.get("/site/../schema.sql").status_code == 404


def test_the_snapshot_can_be_copied_beside_the_site(tmp_path):
    out, beside = tmp_path / "demo", tmp_path / "pages" / "data"
    result = demo.generate(out, seed=7, today=TODAY, site_data=beside)
    assert (beside / site_snapshot.SNAPSHOT_NAME).read_bytes() == (out / "site" / "snapshot.json").read_bytes()
    assert json.loads((beside / "snapshot.json").read_text(encoding="utf-8"))["made"] == "7-2026-11-24"
    assert result["site_docs"] > 100
