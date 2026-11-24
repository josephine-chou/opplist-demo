"""Batch Q2d (2026-11-16, schema v53): Read quote - a vendor's reply read
with the Anthropic API into drafts for the team site's rates drawer
(INTERFACE.md section 11).

A fake API and a fake Outlook; no network. Every company, person, place,
amount, date and reference here is invented: cases OPP-9930 to OPP-9959,
domains .test, invented vendor names.
"""

from __future__ import annotations

import datetime as dt
import io
import json

import pytest

from conftest import add_enquiry, add_party
from opplist import (case_mail, db, firebase_pull, firebase_sync as fs, migrate_master, outlook_bridge, paths,
                     ratecard_sync, read_quote as rq, schema_migrations)
from opplist import web as web_module
from opplist.attachments import FileText
from opplist.web import create_app
from test_firebase_sync import FakeFirestore

TODAY = "2027-05-12"
KEY = "demo-key-INVENTED-7781"
SECRET_BODY = "Plinth Avenue loading bay 4 gate code 5521"      # mail text that must never reach the site


# ------------------------------------------------------------ the fakes --
def response(answer, usage=None, stop="end_turn"):
    return 200, {"content": [{"type": "text", "text": json.dumps(answer)}], "stop_reason": stop,
                 "usage": usage or {"input_tokens": 12000, "output_tokens": 900, "cache_creation_input_tokens": 0,
                                    "cache_read_input_tokens": 0}}


class FakeAPI:
    """The Messages API: hands back what it was given, in order; keeps
    every call (url, headers, body)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class NoCall:
    def __call__(self, *a, **k):
        raise AssertionError("the API was called")


def said(fuel="notsaid", local="notsaid", minimum="notsaid"):
    return {"fuel": {"value": fuel, "quote": ""}, "local": {"value": local, "quote": ""},
            "min": {"value": minimum, "quote": ""}}


def place(p="", c="", q=""):
    return {"place": p, "country": c, "quote": q}


def line(charge, seg="Freight", unit="KG", rate=None, cur="USD", word="", minimum=None, qty=None, amount=None,
         breaks=None, leg="none", quote=None, as_written="", percent=None, percent_of="", approx=False):
    return {"seg": seg, "charge": charge, "unit": unit, "unit_as_written": as_written, "currency": cur, "rate": rate,
            "word": word, "min": minimum, "qty": qty, "amount": amount, "breaks": breaks or [], "door_leg": leg,
            "quote": quote if quote is not None else "{} {} {} {}".format(charge, cur, rate, unit),
            "percent": percent, "percent_of": percent_of, "approx": approx}


def option(lines, label="", cur="USD", valid="", valid_text="", conditions=None, origin=None, dest=None, notes="",
           transit="", carrier="", free="", s=None):
    return {"label": label, "currency": cur, "lines": lines, "valid_until": valid, "valid_text": valid_text,
            "transit": transit, "carrier": carrier, "free_time": free, "said": s or said(),
            "conditions": conditions or [], "quoted_origin": origin or place(), "quoted_dest": dest or place(),
            "notes": notes}


def answer(*routes, priced=True, why="", files=None):
    return {"priced": priced, "why_not": why, "routes": [{"route": r, "options": opts} for r, opts in routes],
            "files": files or []}


class FakeOutlook:
    def __init__(self, body="", files=None, fail=None):
        self.body, self.files, self.fail = body, files or [], fail
        self.calls = []

    def read_item(self, entry_id, message_id="", folders=()):
        self.calls.append(("read_item", entry_id, message_id))
        if self.fail:
            raise outlook_bridge.OutlookError(self.fail)
        return outlook_bridge.MailInfo(subject="RE: RATE REQUEST // LAX - SCL", entry_id=entry_id, message_id=message_id,
                                       body_excerpt=self.body,
                                       attachment_names=[f.name for f in self.files])

    def read_files(self, entry_id, message_id="", folders=()):
        self.calls.append(("read_files", entry_id, message_id))
        return list(self.files)


def pdf_bytes(pages):
    import pypdf
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ------------------------------------------------------------ the world --
@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "q2d.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    for name, region, iso2 in (("USA", "North America", "US"), ("Chile", "Latin America", "CL"),
                               ("Costa Rica", "Latin America", "CR"), ("Unknown", "Undefined", None)):
        repo.insert("country", {"name": name, "region": region, "iso2": iso2})
    lax = repo.add_master_location("LAX", "USA", "airport", name="Los Angeles International", iata="LAX",
                                   is_airport=1, source="phase1")
    scl = repo.add_master_location("SCL", "Chile", "airport", name="Santiago Merino Benitez", iata="SCL",
                                   is_airport=1, source="phase1")
    mia = repo.add_master_location("MIAMI, FL", "USA", "city", name="Miami", subdivision="FL", source="phase1")
    sai = repo.add_master_location("SAN ANTONIO", "Chile", "seaport", name="San Antonio", unlocode="CLSAI",
                                   is_seaport=1, source="phase1")
    cal = repo.add_master_location("CALDERA", "Costa Rica", "seaport", name="Caldera", unlocode="CRCAL",
                                   is_seaport=1, source="phase1")
    conn.execute("ALTER TABLE location RENAME TO location_old")
    conn.execute(migrate_master.LOCATION_VIEW_SQL)
    agent = add_party(repo, "Tessaly Logistics", country="Chile")
    harlow = add_party(repo, "Harlow Freight Lines", ptype="Carrier", country="USA")
    repo.insert("party_domain", {"domain": "harlow.test", "party_id": harlow})
    ids = {"agent": agent, "harlow": harlow}

    def case(opp, **extra):
        extra.setdefault("scope", "In")
        extra.setdefault("status", "New")
        ids[opp] = add_enquiry(repo, opp, agent, received="2027-05-01", **extra)
        return ids[opp]

    # one route, Air, Door to port: LAX - SCL, 500 kg 3 cbm (500 kg chargeable)
    case("OPP-9930", service_mode="Air", origin_country="USA", dest_country="Chile", incoterm="FCA")
    ids["r9930"] = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (ids["OPP-9930"],)).fetchone()[0]
    repo.edit_route(ids["r9930"], {"origin_loc_id": lax["id"], "origin_loc": "LAX", "dest_places": [scl["id"]],
                                   "service_scope": "Door to port", "service_mode": "Air"})
    repo.add_cargo_line(ids["OPP-9930"], {"pieces": 4, "pack_unit": "Pallets", "weight_value": 500, "weight_unit": "kg",
                                          "cbm_value": 3, "commodity": "Copper fittings"})
    # two routes: A FCL Miami - San Antonio (Port to port), B LCL Miami - San Antonio
    case("OPP-9931", service_mode="FCL", origin_country="USA", dest_country="Chile", incoterm="FOB")
    ids["a9931"] = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (ids["OPP-9931"],)).fetchone()[0]
    repo.edit_route(ids["a9931"], {"origin_loc_id": mia["id"], "origin_loc": "MIAMI, FL", "dest_places": [sai["id"]],
                                   "service_scope": "Port to port"})
    ids["b9931"] = repo.add_route(ids["OPP-9931"], {"origin_country": "USA", "origin_loc_id": mia["id"],
                                                    "origin_loc": "MIAMI, FL", "dest_country": "Chile",
                                                    "dest_places": [sai["id"]], "service_mode": "LCL",
                                                    "service_scope": "Port to port"})
    repo.add_cargo_line(ids["OPP-9931"], {"container_qty": 1, "container_type": "40HQ", "commodity": "Tiles",
                                          "route_ids": str(ids["a9931"])})
    case("OPP-9932", status="Lost", service_mode="Air", origin_country="USA", dest_country="Chile")
    case("OPP-9933", scope="Out", service_mode="Air", origin_country="USA", dest_country="Chile")
    case("OPP-9934", handled_by="JJH", service_mode="Air", origin_country="USA", dest_country="Chile")
    # FCL Miami - Caldera, Costa Rica, Port to door
    case("OPP-9935", service_mode="FCL", origin_country="USA", dest_country="Costa Rica", incoterm="DAP")
    ids["r9935"] = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (ids["OPP-9935"],)).fetchone()[0]
    repo.edit_route(ids["r9935"], {"origin_loc_id": mia["id"], "origin_loc": "MIAMI, FL", "dest_places": [cal["id"]],
                                   "service_scope": "Port to door"})
    for opp in ("OPP-9930", "OPP-9931", "OPP-9932", "OPP-9933", "OPP-9934", "OPP-9935"):
        ids["vr" + opp[-2:]] = repo.insert("vendor_request", {
            "enquiry_id": ids[opp], "vendor_party_id": harlow, "asked_on": "2027-05-02",
            "thread_key": "RATE REQUEST // " + opp, "parts": "Freight|Origin"})
    repo.commit()
    (paths.DATA_DIR / "secrets").mkdir(parents=True, exist_ok=True)
    (paths.DATA_DIR / "secrets" / "anthropic-key.txt").write_text(KEY + "\n", encoding="utf-8")
    db.set_setting(conn, rq.ON_SETTING, "1")
    conn.commit()
    monkeypatch.setattr(rq, "SLEEP", lambda s: None)
    monkeypatch.setattr(rq, "_now", lambda: dt.datetime(2027, 5, 12, 9, 30, 0))
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    yield {"conn": conn, "ids": ids, "path": path, "repo": repo}
    conn.close()


def paste(text="Air freight USD 3.10 per kg", day="2027-05-11"):
    return rq.from_paste(text, day, TODAY)


def reading(w, opp, ans, source=None, api=None, **kw):
    api = api or FakeAPI(response(ans))
    got = rq.read(w["conn"], w["ids"]["vr" + opp[-2:]], source or paste(), today=TODAY, transport=api, **kw)
    return got, api


def drafts(w, got):
    return [json.loads(r["draft"]) for r in w["conn"].execute(
        "SELECT draft FROM quote_read_draft WHERE quote_read_id = ? ORDER BY id", (got.id,))]


# ================================================================ point 5
def test_one_route_null_is_the_case_route_and_the_draft_document(world):
    w = world
    ans = answer((None, [option([line("AIR FREIGHT", rate=3.1, quote="Air freight USD 3.10/kg"),
                                 line("PICK UP LAX", seg="Origin", unit="SHIPMENT", rate=95, leg="pickup",
                                      quote="Pickup at LAX area USD 95")],
                                valid="2027-05-31", transit="2 days", carrier="Corvel Air",
                                s=said(fuel="incl", local="excl"))]))
    got, api = reading(w, "OPP-9930", ans)
    assert got.status == "read" and len(got.drafts) == 1
    doc = rq.draft_docs(w["conn"])["rateDrafts/rq{}-1".format(got.id)]
    assert str(doc.pop("readAt")) == "2027-05-12T{}:30:00.000000Z".format(
        dt.datetime(2027, 5, 12, 9, 30).astimezone().astimezone(dt.timezone.utc).strftime("%H"))
    assert doc == {
        "schema": 1, "opp": "OPP-9930", "vendor": "Harlow Freight Lines", "route": 1, "on": "2027-05-11",
        "options": [{"label": "", "cur": "USD", "valid": "2027-05-31", "transit": "2 days", "carrier": "Corvel Air",
                     "free": "", "said": {"fuel": "incl", "local": "excl", "min": "notsaid"}, "note": "",
                     "lines": [{"seg": "Freight", "charge": "AIR FREIGHT", "unit": "KG", "rate": 3.1, "min": None,
                                "qty": None, "amount": None, "word": "", "quote": "Air freight USD 3.10/kg",
                                "asWritten": ""},
                               {"seg": "Origin", "charge": "PICK UP LAX", "unit": "SHIPMENT", "rate": 95, "min": None,
                                "qty": None, "amount": None, "word": "", "quote": "Pickup at LAX area USD 95",
                                "asWritten": ""}]}],
        "warnings": [], "source": {"kind": "paste", "subject": "", "sent": "2027-05-11", "files": []},
        "readBy": "Iris", "status": "open", "savedAs": "", "taken": [""]}
    # the request: structured output, the system prompt, max_tokens, the key in the header only
    body = api.calls[0]["body"]
    assert body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 16000
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"] == rq.SYSTEM_PROMPT and api.calls[0]["timeout"] == 120
    assert api.calls[0]["headers"]["x-api-key"] == KEY
    assert KEY not in json.dumps(body)


def test_context_has_routes_cargo_and_what_was_asked_never_the_requester(world):
    w = world
    got, api = reading(w, "OPP-9930", answer((None, [option([line("AIR FREIGHT", rate=3.1)])])))
    text = api.calls[0]["body"]["messages"][0]["content"][-1]["text"]
    assert "Case: OPP-9930" in text and "Vendor: Harlow Freight Lines" in text
    assert "Asked of the vendor: Freight, Origin; routes 1" in text
    assert "Route 1 (A): Air; from LAX (USA) to SCL (Chile)" in text and "incoterm FCA" in text
    assert "service scope Door to port" in text and "500 kg chargeable" in text and "commodity Copper fittings" in text
    assert "Tessaly" not in text


def test_two_routes_two_drafts_and_a_route_the_case_does_not_have(world):
    w = world
    ans = answer((1, [option([line("OCEAN FREIGHT", unit="40'HQ", rate=2100)])]),
                 (2, [option([line("LCL FREIGHT", unit="W/M", rate=85)])]),
                 (7, [option([line("BARGE", unit="SHIPMENT", rate=300)])]))
    got, _ = reading(w, "OPP-9931", ans)
    docs = drafts(w, got)
    assert [d["route"] for d in docs] == [1, 2, None]
    assert docs[2]["warnings"] == ["The vendor's route 7 is not a route of this case: pick the route in the drawer"]
    assert w["conn"].execute("SELECT site_id FROM quote_read_draft ORDER BY id").fetchall()[2][0] == \
        "rq{}-0".format(got.id)


def test_a_new_reading_replaces_the_open_draft_of_that_vendor_and_route(world):
    w = world
    first, _ = reading(w, "OPP-9930", answer((None, [option([line("AIR FREIGHT", rate=3.1)])])))
    second, _ = reading(w, "OPP-9930", answer((None, [option([line("AIR FREIGHT", rate=2.9)])])))
    states = dict(w["conn"].execute("SELECT site_id, status FROM quote_read_draft").fetchall())
    assert states == {"rq{}-1".format(first.id): "replaced", "rq{}-1".format(second.id): "open"}


def test_mixed_currency_keeps_the_words_and_no_number(world):
    w = world
    ans = answer((None, [option([line("AIR FREIGHT", rate=3.1), line("SCREENING", unit="SHIPMENT", rate=45),
                                 line("DESTINATION HANDLING", seg="Destination", unit="SHIPMENT", rate=350, cur="BRL")])]))
    got, _ = reading(w, "OPP-9930", ans)
    opt = drafts(w, got)[0]["options"][0]
    assert opt["cur"] == "USD"
    assert opt["lines"][2]["charge"] == "DESTINATION HANDLING (BRL 350 as written)" and opt["lines"][2]["rate"] is None
    assert "Lines in BRL: the drawer keeps one currency; type them or leave them out" in drafts(w, got)[0]["warnings"]


def test_weight_breaks_with_the_chargeable_weight(world):
    w = world
    breaks = [{"from_kg": None, "to_kg": 45, "rate": 4.5}, {"from_kg": 45, "to_kg": None, "rate": 3.8},
              {"from_kg": 100, "to_kg": None, "rate": 3.4}, {"from_kg": 500, "to_kg": None, "rate": 2.95},
              {"from_kg": 1000, "to_kg": None, "rate": 2.7}]
    got, _ = reading(w, "OPP-9930", answer((None, [option([line("AIR FREIGHT", breaks=breaks)])])))
    opt = drafts(w, got)[0]["options"][0]
    assert opt["lines"][0]["rate"] == 2.95                  # 500 kg chargeable: the +500 break
    assert opt["note"] == "AIR FREIGHT breaks: -45 kg 4.5, +45 kg 3.8, +100 kg 3.4, +500 kg 2.95, +1,000 kg 2.7"
    assert drafts(w, got)[0]["warnings"] == ["Door pickup asked; no pickup charge in the mail"]


def test_weight_breaks_without_a_chargeable_weight(world):
    w = world
    ans = answer((2, [option([line("LCL FREIGHT", unit="CBM", breaks=[{"from_kg": None, "to_kg": 1000, "rate": 80},
                                                                       {"from_kg": 1000, "to_kg": None, "rate": 70}])])]))
    got, _ = reading(w, "OPP-9931", ans)
    d = drafts(w, got)[0]
    assert d["options"][0]["lines"][0]["rate"] is None
    assert d["options"][0]["note"] == "LCL FREIGHT breaks: -1,000 kg 80, +1,000 kg 70"
    assert d["warnings"] == ["Weight breaks on LCL FREIGHT and no chargeable weight on route B: pick the rate in the drawer"]


def test_a_country_that_differs_and_a_door_leg_missing(world):
    w = world
    ans = answer((None, [option([line("OCEAN FREIGHT", unit="40'HQ", rate=1900)],
                                dest=place("Caldera", "Chile", "to Caldera, Chile"))]))
    got, _ = reading(w, "OPP-9935", ans)
    assert drafts(w, got)[0]["warnings"] == [
        "The vendor wrote Caldera, Chile; route A goes to CALDERA, Costa Rica",
        "Door delivery asked; no delivery charge in the mail"]
    # the same country under another name is not a difference
    ans = answer((None, [option([line("OCEAN FREIGHT", unit="40'HQ", rate=1900),
                                 line("DELIVERY", seg="Destination", unit="TRIP", rate=400, leg="delivery")],
                                origin=place("Miami", "United States"), dest=place("Caldera", "CR"))]))
    got, _ = reading(w, "OPP-9935", ans)
    assert drafts(w, got)[0]["warnings"] == []


def test_door_pickup_from_the_scope(world):
    w = world
    got, _ = reading(w, "OPP-9930", answer((None, [option([line("AIR FREIGHT", rate=3.1)])])))
    assert drafts(w, got)[0]["warnings"] == ["Door pickup asked; no pickup charge in the mail"]


def test_a_unit_not_in_the_list_and_a_line_read_twice(world):
    w = world
    ans = answer((None, [option([line("AIR FREIGHT", rate=3.1, leg="pickup", quote="AF 3.10"),
                                 line("STORAGE", seg="Other", unit="", as_written="per pallet per week", rate=12),
                                 line("AIR FREIGHT", rate=3.1, quote="AF 3.10")])]))
    got, _ = reading(w, "OPP-9930", ans)
    d = drafts(w, got)[0]
    assert [l["charge"] for l in d["options"][0]["lines"]] == ["AIR FREIGHT", "STORAGE"]
    assert d["options"][0]["lines"][1]["unit"] == "" and d["options"][0]["lines"][1]["asWritten"] == "per pallet per week"
    assert d["warnings"] == ["Unit not in the list: 'per pallet per week' (STORAGE); pick one in the drawer",
                             "Read twice, one kept: AIR FREIGHT"]


def test_41_lines_keep_the_first_40(world):
    w = world
    lines = [line("CHARGE {}".format(n), seg="Other", unit="SHIPMENT", rate=n, leg="pickup", quote="C{}".format(n))
             for n in range(1, 42)]
    got, _ = reading(w, "OPP-9930", answer((None, [option(lines)])))
    d = drafts(w, got)[0]
    assert len(d["options"][0]["lines"]) == 40 and d["options"][0]["lines"][-1]["charge"] == "CHARGE 40"
    assert d["warnings"] == ["41 lines read; the drawer takes 40, so the last 1 are left out"]


def test_the_note_cut_at_300(world):
    w = world
    conditions = ["Waiting time USD 75 per hour after 2 free hours"] * 3 + ["Storage USD {} per day".format(n)
                                                                          for n in range(10, 30)]
    got, _ = reading(w, "OPP-9930", answer((None, [option([line("AIR FREIGHT", rate=3.1, leg="pickup")],
                                                          conditions=conditions, valid_text="valid till ETD",
                                                          notes="Rates per kg, subject to space")])))
    note = drafts(w, got)[0]["options"][0]["note"]
    assert len(note) == 300 and note.endswith("...")
    assert note.startswith("Waiting time USD 75 per hour after 2 free hours; ")


def test_valid_words_go_to_the_note_when_there_is_no_date(world):
    w = world
    got, _ = reading(w, "OPP-9930", answer((None, [option([line("AIR FREIGHT", rate=3.1, leg="pickup")],
                                                          valid="next week", valid_text="valid till ETD")])))
    opt = drafts(w, got)[0]["options"][0]
    assert opt["valid"] == "" and opt["note"] == "Valid: valid till ETD"


def test_a_paste_takes_her_day_and_the_mail_its_sent_day(world):
    w = world
    got, _ = reading(w, "OPP-9930", answer((None, [option([line("AIR FREIGHT", rate=3.1)])])),
                     source=paste(day="2027-05-09"))
    assert drafts(w, got)[0]["on"] == "2027-05-09"
    with pytest.raises(rq.ReadQuoteError, match="after today"):
        rq.from_paste("USD 3", "2027-05-13", TODAY)
    with pytest.raises(rq.ReadQuoteError, match="20,000"):
        rq.from_paste("x" * 20001, TODAY, TODAY)


def test_priced_false_makes_no_draft(world):
    w = world
    got, _ = reading(w, "OPP-9930", answer(priced=False, why="checking with the airline"))
    assert got.status == "no_prices" and got.why_not == "checking with the airline" and got.drafts == []
    assert w["conn"].execute("SELECT COUNT(*) FROM quote_read_draft").fetchone()[0] == 0
    assert rq.view(w["conn"], got.id)["why_not"] == "checking with the airline"


def test_three_options_at_most_each_labelled(world):
    w = world
    opts = [option([line("AIR FREIGHT", rate=3.1 - n / 10, leg="pickup", cur="EUR" if n == 1 else "USD")],
                   label="Carrier {}".format(n),
                   cur="EUR" if n == 1 else "USD") for n in range(3)]
    got, _ = reading(w, "OPP-9930", answer((None, opts)))
    d = drafts(w, got)[0]
    assert [o["label"] for o in d["options"]] == ["Carrier 0", "Carrier 1", "Carrier 2"]
    assert [o["cur"] for o in d["options"]] == ["USD", "EUR", "USD"]


# ============================================================ the files
def test_the_files_sent_and_not_sent(world):
    w = world
    files = [
        outlook_bridge.AttachedFile(name="rates.pdf", size=900, data=pdf_bytes(2),
                                    text=FileText(name="rates.pdf", lines=["AIR FREIGHT USD 3.10"], read=True)),
        outlook_bridge.AttachedFile(name="manual.pdf", size=9000, data=pdf_bytes(40)),
        outlook_bridge.AttachedFile(name="image001.png", size=300, inline=True),
        outlook_bridge.AttachedFile(name="photo.jpg", size=4, data=b"\xff\xd8\xff\xd9"),
        outlook_bridge.AttachedFile(name="tariff.zip", size=4000, data=b"PK"),
        outlook_bridge.AttachedFile(name="sheet.xlsx", size=500, data=b"x",
                                    text=FileText(name="sheet.xlsx", lines=["Charge | USD", "Screening | 45"], read=True)),
    ]
    mail = outlook_bridge.MailInfo(subject="RE: RATE REQUEST", entry_id="E1", message_id="<m1@harlow.test>",
                                   sender_name="Dara Vell", sender_address="dara@harlow.test",
                                   received_at="2027-05-10 14:05:00", attachment_names=[f.name for f in files])
    fake = FakeOutlook(body="Dear team,\nPlease see attached.\n" + SECRET_BODY, files=files)
    rq.OUTLOOK = fake
    try:
        source = rq.from_mail(mail)
    finally:
        rq.OUTLOOK = None
    got, api = reading(w, "OPP-9930", answer((None, [option([line("AIR FREIGHT", rate=3.1, leg="pickup")])])),
                       source=source)
    sent = {f["name"]: (f["sent"], f["why"]) for f in got.files}
    assert sent == {"rates.pdf": (True, ""), "manual.pdf": (False, "40 pages: past the 30 pages a reading sends"),
                    "image001.png": (False, "a picture in the mail's body or signature"), "photo.jpg": (True, ""),
                    "tariff.zip": (False, "a .zip file: not a kind Read quote sends"), "sheet.xlsx": (True, "")}
    content = api.calls[0]["body"]["messages"][0]["content"]
    assert [b["type"] for b in content] == ["document", "image", "text"]          # the files before the text
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert "=== Attachment: sheet.xlsx ===\nCharge | USD" in content[2]["text"]
    assert "From: Dara Vell <dara@harlow.test>\nSent: 2027-05-10 14:05" in content[2]["text"]
    row = w["conn"].execute("SELECT * FROM quote_read WHERE id = ?", (got.id,)).fetchone()
    assert row["source"] == "mail" and row["message_id"] == "<m1@harlow.test>" and row["sent_at"] == "2027-05-10 14:05"
    assert "AIR FREIGHT USD 3.10" in row["text_sent"]                 # the PDF's text, for the record
    doc = drafts(w, got)[0]
    assert doc["on"] == "2027-05-10" and doc["source"] == {"kind": "mail", "subject": "RE: RATE REQUEST // LAX - SCL",
                                                          "sent": "2027-05-10 14:05",
                                                          "files": ["rates.pdf", "photo.jpg", "sheet.xlsx"]}
    # nothing of the mail on the site but each line's quote, the subject, the day and the file names
    pushed = json.dumps(rq.draft_docs(w["conn"]), default=str)
    assert SECRET_BODY not in pushed and "Please see attached" not in pushed and "Screening | 45" not in pushed


def test_outlook_closed_is_a_sentence(world):
    rq.OUTLOOK = FakeOutlook(fail=outlook_bridge.NOT_RUNNING)
    try:
        with pytest.raises(rq.ReadQuoteError, match="Outlook is not open"):
            rq.from_mail(outlook_bridge.MailInfo(entry_id="E1"))
    finally:
        rq.OUTLOOK = None


def test_the_text_is_cut_at_60000(world):
    w = world
    got, api = reading(w, "OPP-9930", answer((None, [option([line("AIR FREIGHT", rate=3.1)])])),
                       source=rq.Source(kind="mail", text="y" * 70000, sent="2027-05-10 08:00",
                                        files=[outlook_bridge.AttachedFile(
                                            name="more.txt", size=10, data=b"z",
                                            text=FileText(name="more.txt", lines=["z"], read=True))]))
    record = w["conn"].execute("SELECT text_sent FROM quote_read").fetchone()[0]
    assert record.startswith("y" * 60000) and record[60000] != "y"
    sent = api.calls[0]["body"]["messages"][0]["content"][-1]["text"].split("No files sent with it.\n\n")[1]
    assert sent == "y" * 60000
    assert got.files[0]["why"] == "past the 60,000 characters a reading sends"


# ============================================================== the gate
def gate_of(w, opp, **kw):
    conn = w["conn"]
    case = conn.execute("SELECT * FROM v_enquiry WHERE opp_no = ?", (opp,)).fetchone()
    return rq.gate(conn, case, rq._request(conn, w["ids"]["vr" + opp[-2:]]), **kw)


def test_the_gate(world, monkeypatch):
    w = world
    assert gate_of(w, "OPP-9930") == {"show": True, "enabled": True, "why": ""}
    assert gate_of(w, "OPP-9932") == {"show": False, "enabled": False, "why": "the case is Lost"}
    assert gate_of(w, "OPP-9933")["why"] == "the case is not in the lists"
    assert gate_of(w, "OPP-9934")["why"] == "the case is handled by JJH"
    # a server over a copy: no button
    assert gate_of(w, "OPP-9930", db_path=w["path"])["show"] is False
    monkeypatch.setattr(rq, "TRANSPORT", FakeAPI())
    assert gate_of(w, "OPP-9930", db_path=w["path"])["show"] is True
    db.set_setting(w["conn"], rq.ON_SETTING, "0")
    assert gate_of(w, "OPP-9930") == {"show": False, "enabled": False, "why": "Read quote is off in Settings"}
    assert gate_of(w, "OPP-9930", assume=True)["show"] is True
    db.set_setting(w["conn"], rq.ON_SETTING, "1")
    (paths.DATA_DIR / "secrets" / "anthropic-key.txt").unlink()
    assert gate_of(w, "OPP-9930")["why"].startswith("no key at ")


def test_the_cap_reached_disables_it_with_the_reason(world):
    w = world
    db.set_setting(w["conn"], rq.CAP_SETTING, "0.05")
    reading(w, "OPP-9930", answer((None, [option([line("AIR FREIGHT", rate=3.1)])])),
            api=FakeAPI(response(answer((None, [option([line("AIR FREIGHT", rate=3.1)])])),
                                 usage={"input_tokens": 30000, "output_tokens": 1000})))
    g = gate_of(w, "OPP-9930")
    assert g["show"] is True and g["enabled"] is False
    assert g["why"] == ("This month's readings have cost USD 0.07, the cap of USD 0.05: Read quote waits for next "
                        "month, or raise the cap in Settings.")
    with pytest.raises(rq.ReadQuoteError, match="the cap of USD 0.05"):
        reading(w, "OPP-9930", answer(priced=False), api=NoCall())


def test_never_from_a_server_over_a_copy(world):
    w = world
    with pytest.raises(rq.ReadQuoteError, match="only from the app over the live database"):
        rq.read(w["conn"], w["ids"]["vr30"], paste(), today=TODAY, db_path=w["path"])


# ============================================================ the failures
@pytest.mark.parametrize("status,body,sentence", [
    (401, {"error": {"type": "authentication_error", "message": "invalid x-api-key"}}, "The API key was refused"),
    (400, {"error": {"type": "invalid_request_error", "message": "Your credit balance is too low to access the API"}},
     "no credits left"),
    (400, {"error": {"type": "invalid_request_error", "message": "You have reached your specified workspace API usage limits"}},
     "spend limit in the Console is reached"),
    (404, {"error": {"type": "not_found_error", "message": "model: claude-sonnet-5-5"}},
     "The model claude-sonnet-5-5 is no longer offered: pick another in Settings"),
    (400, {"error": {"type": "invalid_request_error", "message": "max_tokens: bad"}}, "Anthropic refused the reading (400"),
])
def test_each_api_failure_is_a_sentence(world, status, body, sentence):
    w = world
    api = FakeAPI((status, body))
    got, _ = reading(w, "OPP-9930", None, api=api)
    assert got.status == "failed" and sentence in got.error and len(api.calls) == 1
    assert sentence in db.get_setting(w["conn"], rq.ERROR_SETTING)


def test_busy_is_retried_once_after_five_seconds(world, monkeypatch):
    w = world
    slept = []
    monkeypatch.setattr(rq, "SLEEP", slept.append)
    busy = (529, {"error": {"type": "overloaded_error", "message": "Overloaded"}})
    got, api = reading(w, "OPP-9930", None, api=FakeAPI(busy, busy))
    assert got.error == rq.BUSY and len(api.calls) == 2 and slept == [5]
    ok = response(answer((None, [option([line("AIR FREIGHT", rate=3.1)])])))
    got, api = reading(w, "OPP-9930", None, api=FakeAPI((500, {"error": {"type": "api_error"}}), ok))
    assert got.status == "read" and len(api.calls) == 2
    got, api = reading(w, "OPP-9930", None, api=FakeAPI((429, {"error": {"type": "rate_limit_error"}}), ok))
    assert got.status == "read"


def test_a_dropped_connection_is_retried_and_a_timeout_is_not(world):
    w = world
    ok = response(answer((None, [option([line("AIR FREIGHT", rate=3.1)])])))
    got, api = reading(w, "OPP-9930", None, api=FakeAPI(ConnectionError(), ok))
    assert got.status == "read" and len(api.calls) == 2
    got, api = reading(w, "OPP-9930", None, api=FakeAPI(ConnectionError(), ConnectionError()))
    assert got.error == rq.NO_CONNECTION
    got, api = reading(w, "OPP-9930", None, api=FakeAPI(TimeoutError()))
    assert got.error == rq.TIMED_OUT and len(api.calls) == 1


def test_refusal_cut_off_and_a_bad_answer_are_failed_readings(world):
    w = world
    refused = (200, {"content": [], "stop_reason": "refusal", "stop_details": {"category": "cyber"},
                     "usage": {"input_tokens": 100, "output_tokens": 0}})
    got, _ = reading(w, "OPP-9930", None, api=FakeAPI(refused))
    assert got.status == "failed" and got.error == "Claude declined to read this mail (cyber): type the rates in the drawer."
    got, _ = reading(w, "OPP-9930", None, api=FakeAPI(response({"priced": True}, stop="max_tokens")))
    assert got.error == rq.CUT_OFF
    got, _ = reading(w, "OPP-9930", None, api=FakeAPI((200, {"content": [{"type": "text", "text": "not json"}],
                                                             "stop_reason": "end_turn", "usage": {}})))
    assert got.error == rq.NOT_JSON
    bad = answer((None, [option([line("AIR FREIGHT", rate=3.1)])]))
    bad["routes"][0]["options"][0]["lines"][0]["seg"] = "Ocean"
    got, _ = reading(w, "OPP-9930", None, api=FakeAPI(response(bad)))
    assert got.status == "failed" and "is not a part" in got.error
    four = answer((None, [option([line("A", rate=1)])] * 4))
    with pytest.raises(rq.BadAnswer, match="more than 3 options"):
        rq.check_answer(four)
    # a failed reading still costs what its usage says and is kept
    assert w["conn"].execute("SELECT COUNT(*) FROM quote_read WHERE status = 'failed'").fetchone()[0] == 4


def test_the_key_is_never_in_a_log_line_a_record_a_page_or_a_draft(world, monkeypatch):
    w = world
    got, _ = reading(w, "OPP-9930", answer((None, [option([line("AIR FREIGHT", rate=3.1)])])))
    reading(w, "OPP-9930", None, api=FakeAPI((401, {"error": {"type": "authentication_error", "message": "bad key"}})))
    dump = "\n".join(w["conn"].iterdump())
    assert KEY not in dump
    assert KEY not in json.dumps(rq.draft_docs(w["conn"]), default=str)
    logs_text = "".join(p.read_text(encoding="utf-8", errors="replace") for p in paths.DATA_DIR.rglob("*.log"))
    logs_text += "".join(p.read_text(encoding="utf-8", errors="replace") for p in w["path"].parent.glob("*.log"))
    assert KEY not in logs_text
    monkeypatch.setattr(rq, "TRANSPORT", FakeAPI())
    client = create_app(w["path"]).test_client()
    for url in ("/enquiries/{}?read={}&qr={}".format(w["ids"]["OPP-9930"], w["ids"]["vr30"], got.id), "/settings"):
        page = client.get(url).get_data(as_text=True)
        assert KEY not in page
    import os
    assert all(KEY not in v for v in os.environ.values())


def test_the_cost_from_usage():
    usage = {"input_tokens": 10000, "output_tokens": 2000, "cache_creation_input_tokens": 1000,
             "cache_read_input_tokens": 5000}
    # Sonnet 5.5: 10,000 x 2 + 2,000 x 10 + 1,000 x 2.50 + 5,000 x 0.10 per million
    assert rq.cost_of("claude-sonnet-5-5", usage) == pytest.approx(0.043)
    # Haiku 5.5 under 100,000 tokens and over
    assert rq.cost_of("claude-haiku-5-5", usage) == pytest.approx((10000 * 0.10 + 2000 * 0.50 + 1000 * 0.125
                                                                   + 5000 * 0.01) / 1e6)
    big = dict(usage, input_tokens=120000)
    assert rq.cost_of("claude-haiku-5-5", big) == pytest.approx((120000 * 0.50 + 2000 * 2.50 + 1000 * 0.625
                                                                 + 5000 * 0.05) / 1e6)
    assert rq.cost_of("claude-sonnet-5-5", {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 100,
                                            "cache_creation": {"ephemeral_1h_input_tokens": 100}}) == pytest.approx(0.0004)


def test_the_schema_is_strict_everywhere():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False and set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(rq.answer_schema())


# ============================================================ saves and discards
class Docs:
    def __init__(self):
        self.docs = {}

    def get(self, path):
        return self.docs.get(path)


def save(w, action_id, opp, draft=None, option=0, how="read", lines=None, cur="USD", note="", route=1, on="2027-05-11",
         valid="2027-05-31", vendor="Harlow Freight Lines", said_=None):
    docs = Docs()
    doc = {"opp": opp, "vendor": vendor, "cur": cur, "total": 1600.0,
           "lines": lines or [{"seg": "Freight", "charge": "AIR FREIGHT", "unit": "KG", "rate": 3.1, "min": None,
                               "qty": 501, "amount": 1553.1, "word": None}],
           "valid": valid, "transit": "2 days", "carrier": "Corvel Air", "free": "",
           "said": said_ or {"fuel": "incl", "local": "excl", "min": "notsaid"}, "note": note,
           "by": "ana@jojofreight.test", "name": "Ana", "at": dt.datetime(2027, 5, 12, 16, 0, tzinfo=dt.timezone.utc),
           "route": route, "on": on}
    if draft is not None:
        doc.update({"draft": draft, "option": option, "how": how})
    docs.docs["vendorRates/" + action_id] = doc
    repo = db.Repository(w["conn"], source="web")
    ok, message = firebase_pull.take_in(repo, action_id, {
        "type": "rates", "opp": opp, "by": "ana@jojofreight.test", "name": "Ana", "owner": "Ana", "app": "site",
        "at": doc["at"], "vendor": vendor, "route": route, "on": on}, TODAY, client=docs)
    repo.commit()
    return ok, message


def _read_one(w):
    ans = answer((None, [option([line("AIR FREIGHT", rate=3.1, quote="Air freight USD 3.10/kg", leg="pickup"),
                                 line("AWB FEE", seg="Other", unit="AWB", rate=35, quote="AWB USD 35")],
                                valid="2027-05-31", transit="2 days", carrier="Corvel Air", s=said(fuel="incl", local="excl"))]))
    got, _ = reading(w, "OPP-9930", ans)
    return got, "rq{}-1".format(got.id)


def test_a_save_from_a_draft_keeps_how_read_and_the_changes(world):
    w = world
    got, site_id = _read_one(w)
    ok, message = save(w, "s1", "OPP-9930", draft=site_id, lines=[
        {"seg": "Freight", "charge": "AIR FREIGHT", "unit": "KG", "rate": 2.95, "min": None, "qty": 501, "amount": 1478,
         "word": None},
        {"seg": "Origin", "charge": "PICK UP", "unit": "SHIPMENT", "rate": 90, "min": None, "qty": 1, "amount": 90,
         "word": None}], note="checked with Dara")
    assert ok and message == "OPP-9930: rates from Harlow Freight Lines kept (USD 1,600, valid until May 31)."
    rate = w["conn"].execute("SELECT * FROM vendor_rate WHERE site_id = 's1'").fetchone()
    assert rate["how"] == "read" and rate["draft_id"] == site_id
    d = w["conn"].execute("SELECT * FROM quote_read_draft WHERE site_id = ?", (site_id,)).fetchone()
    assert (d["status"], d["vendor_rate_id"], d["option"], d["confirmed_by"]) == ("saved", rate["id"], 0, "Ana")
    diff = json.loads(d["changes"])
    assert diff["fields"] == {"note": {"read": "", "saved": "checked with Dara"}}
    assert diff["lines"] == [
        {"what": "changed", "n": 1, "charge": "AIR FREIGHT", "quote": "Air freight USD 3.10/kg",
         "fields": {"rate": {"read": 3.1, "saved": 2.95}}},
        {"what": "changed", "n": 2, "charge": "PICK UP", "quote": "AWB USD 35",
         "fields": {"seg": {"read": "Other", "saved": "Origin"}, "charge": {"read": "AWB FEE", "saved": "PICK UP"},
                    "unit": {"read": "AWB", "saved": "SHIPMENT"}, "rate": {"read": 35, "saved": 90.0}}}]
    assert rq.draft_docs(w["conn"])["rateDrafts/" + site_id]["status"] == "saved"
    assert rq.draft_docs(w["conn"])["rateDrafts/" + site_id]["savedAs"] == "s1"
    # a correction from the same draft: how read, the same draft, only the first save's changes kept
    ok, _ = save(w, "s2", "OPP-9930", draft=site_id, note="second look")
    assert ok
    assert w["conn"].execute("SELECT how, draft_id FROM vendor_rate WHERE site_id = 's2'").fetchone()[:] == ("read", site_id)
    assert json.loads(w["conn"].execute("SELECT changes FROM quote_read_draft WHERE site_id = ?",
                                        (site_id,)).fetchone()[0]) == diff


def test_lines_added_and_removed(world):
    w = world
    got, site_id = _read_one(w)
    save(w, "s1", "OPP-9930", draft=site_id, lines=[
        {"seg": "Freight", "charge": "AIR FREIGHT", "unit": "KG", "rate": 3.1, "min": None, "qty": 501, "amount": 1553.1,
         "word": None},
        {"seg": "Customs", "charge": "DUTIES", "unit": "SHIPMENT", "rate": None, "min": None, "qty": None, "amount": None,
         "word": "AT COST"},
        {"seg": "Other", "charge": "AWB FEE", "unit": "AWB", "rate": 35, "min": None, "qty": 1, "amount": 35, "word": None}])
    diff = json.loads(w["conn"].execute("SELECT changes FROM quote_read_draft").fetchone()[0])
    assert diff["lines"] == [{"what": "added", "n": 2, "charge": "DUTIES", "quote": "",
                              "fields": {"seg": {"read": None, "saved": "Customs"},
                                         "charge": {"read": None, "saved": "DUTIES"},
                                         "unit": {"read": None, "saved": "SHIPMENT"},
                                         "word": {"read": None, "saved": "AT COST"}}}]
    assert diff["fields"] == {}


def test_a_save_naming_an_unknown_draft_is_kept_as_typed(world):
    w = world
    _read_one(w)
    ok, message = save(w, "s1", "OPP-9930", draft="rq999-1")
    assert ok and message.endswith(" The reading rq999-1 was not linked: it is not an open draft of this case and vendor.")
    assert w["conn"].execute("SELECT how, draft_id FROM vendor_rate WHERE site_id = 's1'").fetchone()[:] == ("web", None)
    # a draft of another case is not linked either
    other, _ = reading(w, "OPP-9935", answer((None, [option([line("OCEAN FREIGHT", unit="40'HQ", rate=1900)])])))
    ok, message = save(w, "s3", "OPP-9930", draft="rq{}-1".format(other.id))
    assert "was not linked" in message


def test_discard_and_its_refusals(world):
    w = world
    got, site_id = _read_one(w)
    repo = db.Repository(w["conn"], source="web")
    act = {"type": "discard", "opp": "OPP-9930", "by": "ana@jojofreight.test", "name": "Ana", "owner": "Ana",
           "app": "site", "at": dt.datetime(2027, 5, 12, 17, 0, tzinfo=dt.timezone.utc), "draft": site_id}
    ok, message = firebase_pull.take_in(repo, "d1", act, TODAY)
    repo.commit()
    assert ok and message == "OPP-9930: the reading of Harlow Freight Lines's mail was set aside."
    assert w["conn"].execute("SELECT status FROM quote_read_draft").fetchone()[0] == "discarded"
    ok, message = firebase_pull.take_in(repo, "d2", dict(act), TODAY)
    assert not ok and message == "OPP-9930: the reading of Harlow Freight Lines's mail is already set aside, so nothing was set aside."
    ok, message = firebase_pull.take_in(repo, "d3", dict(act, opp="OPP-9935"), TODAY)
    assert not ok and message == "OPP-9935: {} is not a reading of this case, so nothing was set aside.".format(site_id)


def test_expiry_a_closed_case_or_14_days(world, monkeypatch):
    w = world
    first, _ = reading(w, "OPP-9930", answer((None, [option([line("AIR FREIGHT", rate=3.1)])])))
    other, _ = reading(w, "OPP-9935", answer((None, [option([line("OCEAN FREIGHT", unit="40'HQ", rate=1900)])])))
    repo = db.Repository(w["conn"], source="test")
    assert rq.expire(repo, "2027-05-25") == 0
    repo.set_status(w["ids"]["OPP-9935"], "Lost", "2027-05-20")
    repo.commit()
    assert rq.expire(repo, "2027-05-25") == 1
    assert rq.expire(repo, "2027-05-26") == 1
    assert [r[0] for r in w["conn"].execute("SELECT status FROM quote_read_draft ORDER BY id")] == ["expired", "expired"]


def test_the_rate_card_how_and_confirmed_by(world):
    w = world
    _got, site_id = _read_one(w)
    save(w, "s1", "OPP-9930", draft=site_id)
    save(w, "s9", "OPP-9930", on="2027-05-12")
    docs = ratecard_sync.build(w["conn"], {})
    hows = sorted((d["seq"], d["how"], d["confirmedBy"]) for d in docs.values())
    assert hows == [(1, "read", "Ana"), (2, "web", "")]


def test_the_sync_pushes_the_drafts_and_never_calls_the_api(world, monkeypatch, tmp_path):
    w = world
    monkeypatch.setattr(rq, "TRANSPORT", NoCall())
    monkeypatch.setattr(fs, "_archive_search", lambda: None)
    _read_one_with = FakeAPI(response(answer((None, [option([line("AIR FREIGHT", rate=3.1)])]))))
    got, _ = reading(w, "OPP-9930", None, api=_read_one_with)
    client = FakeFirestore()
    state = {}
    result = fs.sync_once(w["conn"], client, state, today=TODAY, reports_dir=tmp_path / "none")
    assert result.ok, result.reason
    path = "rateDrafts/rq{}-1".format(got.id)
    assert client.docs[path]["status"] == "open" and client.docs[path]["opp"] == "OPP-9930"
    # unchanged: not written again; discarded: written again
    client2 = FakeFirestore()
    fs.sync_once(w["conn"], client2, state, today=TODAY, reports_dir=tmp_path / "none")
    assert path not in client2.docs
    w["conn"].execute("UPDATE quote_read_draft SET status = 'discarded'")
    w["conn"].commit()
    fs.sync_once(w["conn"], client2, state, today=TODAY, reports_dir=tmp_path / "none")
    assert client2.docs[path]["status"] == "discarded"


# ============================================================ report and trial
def test_the_weekly_report(world):
    w = world
    _got, site_id = _read_one(w)
    save(w, "s1", "OPP-9930", draft=site_id, lines=[
        {"seg": "Freight", "charge": "AIR FREIGHT", "unit": "KG", "rate": 2.95, "min": None, "qty": 501, "amount": 1478,
         "word": None},
        {"seg": "Other", "charge": "AWB FEE", "unit": "AWB", "rate": 35, "min": None, "qty": 1, "amount": 35, "word": None}])
    reading(w, "OPP-9935", None, api=FakeAPI((529, {"error": {"type": "overloaded_error"}}),
                                             (529, {"error": {"type": "overloaded_error"}})))
    reading(w, "OPP-9930", answer(priced=False, why="checking"))
    path = rq.write_report(w["conn"], 7, TODAY)
    assert path == paths.DATA_DIR / "reports" / "read-quote-20270512.md"
    text = path.read_text(encoding="utf-8")
    assert "Readings 3: 1 read, 1 no prices, 1 failed." in text
    assert "Drafts 1: 0 saved unchanged, 1 saved with changes, 0 set aside, 0 expired, 0 replaced, 0 still open." in text
    assert "- line rate: 1" in text
    assert "- Harlow Freight Lines: 3 readings, 1 saved with changes" in text
    assert "- line 1 changed (AIR FREIGHT): rate read 3.1 / saved 2.95" in text
    assert '  - quote: "Air freight USD 3.10/kg"' in text
    assert "OPP-9935 Harlow Freight Lines: Anthropic is busy right now" in text
    assert "- the week: 24,000 input, 1,800 output" in text


def test_the_trial_picks_and_its_file_push_nothing(world, monkeypatch):
    w = world
    conn = w["conn"]
    conn.execute("UPDATE vendor_request SET replied_on = '2027-05-10'")
    conn.commit()

    def mail(n, vr_opp, body, files=(), sender="dara@harlow.test", day="2027-05-10"):
        return outlook_bridge.MailInfo(subject="RE: RATE REQUEST {} #{}".format(vr_opp, n), entry_id="E{}".format(n),
                                       message_id="<{}@harlow.test>".format(n), sender_name="Dara Vell",
                                       sender_address=sender, received_at="{} 1{}:00:00".format(day, n % 10),
                                       body_excerpt=body, attachment_names=list(files))

    mails = {"RATE REQUEST // OPP-9930": [mail(1, "9930", "Air USD 3.10 per kg"), mail(2, "9930", "We will revert"),
                                           mail(3, "9930", "see file", files=["tariff.pdf"])],
             "RATE REQUEST // OPP-9931": [mail(4, "9931", "OCEAN 2100 USD"), mail(5, "9931", "noted", sender="x@jojofreight.example")],
             "RATE REQUEST // OPP-9935": [mail(6, "9935", "USD 1900 per 40HQ", day="2027-04-01")]}
    monkeypatch.setattr(case_mail, "vendor_mails", lambda search, vr, conn=None: mails.get(vr["thread_key"], []))
    picks = rq.trial_picks(conn, object(), limit=10, since="2027-05-01")
    # FX6: per rate request the earliest reply with prices (OPP-9930: mail 1, not the later file)
    assert [(p["case"]["opp_no"], p["id"]) for p in picks] == [("OPP-9931", "<4@harlow.test>"),
                                                               ("OPP-9930", "<1@harlow.test>")]
    rq.OUTLOOK = FakeOutlook(body="Air USD 3.10 per kg")
    try:
        api = FakeAPI(*[response(answer((None, [option([line("AIR FREIGHT", rate=3.1)])]))) for _ in range(2)])
        monkeypatch.setattr(rq, "trial_picks", lambda *a, **k: picks)
        path = rq.trial(conn, object(), limit=10, since="2027-05-01", today=TODAY, transport=api)
    finally:
        rq.OUTLOOK = None
    text = path.read_text(encoding="utf-8")
    assert path.name == "read-quote-trial-20270512.md"
    assert "## 1. OPP-9931 Harlow Freight Lines (FCL)" in text and "| Freight | AIR FREIGHT | KG | 3.1 |" in text
    assert "## Totals" in text and "2 read, 0 no prices, 0 failed" in text
    assert conn.execute("SELECT COUNT(*) FROM quote_read WHERE trial = 1").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM quote_read_draft").fetchone()[0] == 0      # never pushed
    assert rq.month_spend(conn, "2027-05")[1] == 2


# ============================================================ Settings and the page
def test_settings_block_and_status(world, monkeypatch, capsys):
    w = world
    client = create_app(w["path"]).test_client()
    page = client.get("/settings").get_data(as_text=True)
    assert "<h2>Read quote</h2>" in page and "USD 0.00 of 20.00, 0 readings" in page and "This week's report" in page
    form = {"read_quote_present": "1", "read_quote_model": "claude-haiku-5-5", "read_quote_cap_usd": "12.5",
            "read_quote_key": ""}
    client.post("/settings", data=form)
    conn = db.open_db(w["path"])
    try:
        assert rq.is_on(conn) is False and rq.model(conn) == "claude-haiku-5-5" and rq.cap(conn) == 12.5
        client.post("/settings", data=dict(form, read_quote_key=str(paths.PROJECT_DIR / "key.txt")))
        conn2 = db.open_db(w["path"])
        assert db.get_setting(conn2, rq.KEY_SETTING, "") == ""
        conn2.close()
    finally:
        conn.close()
    from opplist.__main__ import main
    (paths.DATA_DIR / "secrets" / "anthropic-key.txt").unlink()
    assert main(["read-quote", "--status", "--db", str(w["path"])]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Read quote: off" and out[1].startswith("key: no key at ") and out[2] == "model: claude-haiku-5-5"
    assert out[3] == "this month: USD 0.00 of 12.50, 0 readings" and out[4] == "last error: none"
    client.get("/settings/read-quote-report")
    assert (paths.DATA_DIR / "reports" / "read-quote-20270512.md").exists()


def test_the_case_page_button_panel_and_a_reading(world, monkeypatch):
    w = world
    api = FakeAPI(response(answer((None, [option([line("AIR FREIGHT", rate=3.1, quote="AF USD 3.10")])]))))
    monkeypatch.setattr(rq, "TRANSPORT", api)
    client = create_app(w["path"]).test_client()
    case_id = w["ids"]["OPP-9930"]
    page = client.get("/enquiries/{}".format(case_id)).get_data(as_text=True)
    assert 'class="link rq-open" href="/enquiries/{}?read={}#read-quote"'.format(case_id, w["ids"]["vr30"]) in page
    page = client.get("/enquiries/{}?read={}".format(case_id, w["ids"]["vr30"])).get_data(as_text=True)
    assert "Read quote &middot; Harlow Freight Lines" in page and "Paste a message instead" in page
    resp = client.post("/enquiries/{}/read-quote/{}".format(case_id, w["ids"]["vr30"]),
                       data={"paste": "AF USD 3.10 per kg", "paste_day": "2027-05-11"})
    assert resp.status_code == 302 and "qr=" in resp.headers["Location"]
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Check and save on the team site" in page
    assert "https://team-site.example/#/case?opp=OPP-9930&amp;rates=Harlow%20Freight%20Lines&amp;route=1&amp;draft=rq1-1" in page
    assert 'title="AF USD 3.10"' in page and "Door pickup asked; no pickup charge in the mail" in page
    # another office's case: no button, and the POST is refused without a call
    other = w["ids"]["OPP-9934"]
    assert "rq-open" not in client.get("/enquiries/{}".format(other)).get_data(as_text=True)
    client.post("/enquiries/{}/read-quote/{}".format(other, w["ids"]["vr34"]), data={"paste": "USD 1"})
    assert len(api.calls) == 1


def test_panel_mails_pick_the_newest_from_the_vendor(world, monkeypatch):
    w = world
    mails = [outlook_bridge.MailInfo(subject="RE: one", entry_id="E1", message_id="<1@x>", sender_address="dara@harlow.test",
                                     sender_name="Dara Vell", received_at="2027-05-09 10:00:00",
                                     attachment_names=["image001.png", "rates.pdf"]),
             outlook_bridge.MailInfo(subject="RE: two", entry_id="E2", message_id="<2@x>", sender_address="dara@harlow.test",
                                     sender_name="Dara Vell", received_at="2027-05-10 10:00:00"),
             outlook_bridge.MailInfo(subject="RE: ours", entry_id="E3", message_id="<3@x>", sender_address="me@jojofreight.example",
                                     sender_internal=True, received_at="2027-05-11 10:00:00")]
    monkeypatch.setattr(case_mail, "vendor_mails", lambda search, vr, conn=None: mails)
    got = rq.panel_mails(w["conn"], object(), rq._request(w["conn"], w["ids"]["vr30"]))
    assert [(m["id"], m["picked"], m["vendor"]) for m in got] == [("<3@x>", False, False), ("<2@x>", True, True),
                                                                   ("<1@x>", False, True)]
    assert got[2]["files"] == ["rates.pdf"] and got[1]["sent"] == "2027-05-10 10:00"


# ============================================================== the migration
def test_v53_from_v52(tmp_path):
    path = tmp_path / "old.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    repo.insert("country", {"name": "USA", "region": "North America", "iso2": "US"})
    p = add_party(repo, "Pellow Air", ptype="Carrier")
    e = add_enquiry(repo, "OPP-9950", p, received="2027-01-02", scope="In")
    repo.insert("vendor_rate", {"enquiry_id": e, "vendor_party_id": p, "site_id": "v1", "cur": "USD"})
    repo.commit()
    conn.execute("DROP TABLE quote_read_draft")
    conn.execute("DROP TABLE quote_read")
    # vendor_rate as v52 had it: without how and draft_id
    import re
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'vendor_rate'").fetchone()[0]
    # (Q2e: and without v55's label)
    old_sql = re.sub(r"(received_on\s+TEXT),([^\n]*)\n\s*how\s[^\n]*\n\s*draft_id[^\n]*(?:\n\s*label\s[^\n]*)?",
                     r"\1\2", sql)
    assert "how" not in old_sql.split("received_on", 1)[1] and "label" not in old_sql.split("received_on", 1)[1]
    columns = [c for c in (r[1] for r in conn.execute("PRAGMA table_info(vendor_rate)"))
               if c not in ("how", "draft_id", "label")]
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.execute("ALTER TABLE vendor_rate RENAME TO vendor_rate_v53")
    conn.execute(old_sql)
    conn.execute("INSERT INTO vendor_rate ({0}) SELECT {0} FROM vendor_rate_v53".format(", ".join(columns)))
    conn.execute("DROP TABLE vendor_rate_v53")
    conn.execute("PRAGMA legacy_alter_table = OFF")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("UPDATE setting SET value = '52' WHERE key = 'schema_version'")
    conn.commit()
    notes = schema_migrations.upgrade(conn)
    assert "quote_read table created" in notes and "quote_read_draft table created" in notes
    assert "vendor_rate.how added (1 versions on file, each 'web')" in notes
    assert conn.execute("SELECT how, draft_id FROM vendor_rate").fetchone()[:] == ("web", None)
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION
    conn.close()
