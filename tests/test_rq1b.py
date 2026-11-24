"""Batch RQ1b (2026-11-16, no schema change): Read quote's warnings are one
sentence a kind and option, never cut without a word, and a file sent that
gave no price says so. Prompt version 3.

A fake API and a fake Outlook; no network. Every company, person, place,
amount, date and reference here is invented: cases OPP-9990 to OPP-9994,
domains .test.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from conftest import add_enquiry, add_party
from opplist import db, outlook_bridge, paths, read_quote as rq
from test_q2d import FakeAPI, FakeOutlook, KEY, answer, line, option, pdf_bytes, response

TODAY = "2027-06-08"


@pytest.fixture
def world(tmp_path, monkeypatch):
    conn = db.open_db(tmp_path / "rq1b.db")
    repo = db.Repository(conn, source="test")
    for name, region, iso2 in (("USA", "North America", "US"), ("Peru", "Latin America", "PE")):
        repo.insert("country", {"name": name, "region": region, "iso2": iso2})
    agent = add_party(repo, "Quenby Freight", country="Peru")
    vendor = add_party(repo, "Tolland Drayage", ptype="Carrier", country="USA")
    repo.insert("party_domain", {"domain": "tolland.test", "party_id": vendor})
    ids = {}
    # OPP-9990: FCL, 3 x 40HQ; OPP-9991: LCL
    for opp, mode in (("OPP-9990", "FCL"), ("OPP-9991", "LCL")):
        ids[opp] = add_enquiry(repo, opp, agent, received="2027-06-01", scope="In", service_mode=mode,
                               origin_country="USA", dest_country="Peru", incoterm="FOB")
        ids["vr" + opp] = repo.insert("vendor_request", {
            "enquiry_id": ids[opp], "vendor_party_id": vendor, "asked_on": "2027-06-02",
            "thread_key": "RATE REQUEST // " + opp, "parts": "Freight"})
    repo.add_cargo_line(ids["OPP-9990"], {"container_qty": 3, "container_type": "40HQ", "commodity": "Kiln shelves"})
    repo.add_cargo_line(ids["OPP-9991"], {"pieces": 3, "pack_unit": "Pallets", "weight_value": 410, "weight_unit": "kg",
                                          "cbm_value": 2.2, "commodity": "Kiln shelves"})
    repo.commit()
    (paths.DATA_DIR / "secrets").mkdir(parents=True, exist_ok=True)
    (paths.DATA_DIR / "secrets" / "anthropic-key.txt").write_text(KEY + "\n", encoding="utf-8")
    monkeypatch.setattr(rq, "SLEEP", lambda s: None)
    monkeypatch.setattr(rq, "_now", lambda: dt.datetime(2027, 6, 8, 10, 0, 0))
    yield {"conn": conn, "ids": ids}
    conn.close()


def read(w, opp, ans, source=None):
    api = FakeAPI(response(ans))
    got = rq.read(w["conn"], w["ids"]["vr" + opp], source or rq.from_paste("Rates below", "2027-06-07", TODAY),
                  today=TODAY, transport=api)
    return got, api


def draft(w, got):
    return json.loads(w["conn"].execute("SELECT draft FROM quote_read_draft WHERE quote_read_id = ? ORDER BY id",
                                        (got.id,)).fetchone()["draft"])


def yard(label, dray, pull):
    return option([line("Haul", seg="Origin", unit="", rate=dray, quote="Haul ${}".format(dray)),
                   line("Pre-pull", seg="Origin", unit="", rate=pull, quote="Pre-pull ${}".format(pull)),
                   line("Fuel", seg="Origin", unit="", percent=6, percent_of="haul", quote="haul + 6% fuel"),
                   line("Gate fee", seg="Origin", unit="CNTR", rate=30, quote="Gate fee $30 approx.")], label=label)


# ---------------------------------------------------------------- point 1 --
def test_one_sentence_a_kind_and_option(world):
    got, _ = read(world, "OPP-9990", answer((None, [yard("Yard Pell", 380, 95), yard("Yard Amory", 455, 120)])))
    assert draft(world, got)["warnings"] == [
        "Yard Pell: percentages of other charges: Fuel (6% of haul); work out the amounts or leave them out",
        "Yard Pell: the vendor says approximate: Gate fee",
        "Yard Pell: no unit in the mail for Haul, Pre-pull; the drawer counts each once (per container it would be "
        "3 times)",
        "Yard Amory: percentages of other charges: Fuel (6% of haul); work out the amounts or leave them out",
        "Yard Amory: the vendor says approximate: Gate fee",
        "Yard Amory: no unit in the mail for Haul, Pre-pull; the drawer counts each once (per container it would be "
        "3 times)"]


def test_with_one_option_no_label_and_each_cut_at_200(world):
    many = [line("Charge number {}".format(n), seg="Origin", unit="", rate=10 + n) for n in range(30)]
    got, _ = read(world, "OPP-9990", answer((None, [option(many, label="Yard Pell")])))
    (only,) = draft(world, got)["warnings"]
    assert only.startswith("no unit in the mail for Charge number 0, Charge number 1")
    assert len(only) <= rq.WARN_LEN and only.endswith("...")


# ---------------------------------------------------------------- point 2 --
def test_past_eight_the_eighth_says_how_many_more():
    assert rq.capped(["w{}".format(n) for n in range(8)]) == ["w{}".format(n) for n in range(8)]
    assert rq.capped(["w{}".format(n) for n in range(11)]) == ["w0", "w1", "w2", "w3", "w4", "w5", "w6",
                                                               "4 more warnings: check each line against the mail"]


def test_a_draft_with_nine_warnings_names_the_rest(world):
    got, _ = read(world, "OPP-9990", answer((None, [yard("Yard Pell", 380, 95), yard("Yard Amory", 455, 120),
                                                    yard("Yard Holt", 470, 120)])))
    w = draft(world, got)["warnings"]
    assert len(w) == 8 and w[-1] == "2 more warnings: check each line against the mail"
    assert w[6] == "Yard Holt: percentages of other charges: Fuel (6% of haul); work out the amounts or leave them out"


# ---------------------------------------------------------------- point 3 --
def mail_with_files(fake_files):
    mail = outlook_bridge.MailInfo(subject="RE: RATE REQUEST // MIA - CLL", entry_id="E9", message_id="<9@tolland.test>",
                                   sender_name="Imre Sallow", sender_address="imre@tolland.test",
                                   received_at="2027-06-07 16:20:00", attachment_names=[f.name for f in fake_files])
    rq.OUTLOOK = FakeOutlook(body="Please find our rates attached.", files=fake_files)
    try:
        return rq.from_mail(mail), mail
    finally:
        rq.OUTLOOK = None


FILES = [outlook_bridge.AttachedFile(name="TOLLAND lcl 07-06.pdf", size=900, data=pdf_bytes(1)),
         outlook_bridge.AttachedFile(name="TOLLAND air 07-06.pdf", size=900, data=pdf_bytes(1)),
         outlook_bridge.AttachedFile(name="booking form.pdf", size=900, data=pdf_bytes(1)),
         outlook_bridge.AttachedFile(name="image001.png", size=300, inline=True)]
USED = [{"name": "TOLLAND lcl 07-06.pdf", "used": True, "why": ""},
        {"name": "TOLLAND air 07-06.pdf", "used": False, "why": "Air rates; the route is LCL"}]


def test_a_file_sent_that_gave_no_price_is_warned_about(world):
    source, _ = mail_with_files(FILES)
    got, api = read(world, "OPP-9991", answer((None, [option([line("LCL FREIGHT", unit="CBM", rate=64)])]),
                                               files=USED), source=source)
    assert draft(world, got)["warnings"] == [
        "TOLLAND air 07-06.pdf: no prices taken from it (Air rates; the route is LCL)",
        "booking form.pdf: the reading did not say whether any price came from it"]
    kept = {f["name"]: (f["sent"], f.get("used"), f.get("used_why")) for f in got.files}
    assert kept == {"TOLLAND lcl 07-06.pdf": (True, True, ""),
                    "TOLLAND air 07-06.pdf": (True, False, "Air rates; the route is LCL"),
                    "booking form.pdf": (True, None, ""), "image001.png": (False, None, None)}
    row = world["conn"].execute("SELECT files FROM quote_read WHERE id = ?", (got.id,)).fetchone()[0]
    assert json.loads(row)[1]["used_why"] == "Air rates; the route is LCL"
    # the prompt names the files sent, in order; each PDF carries its name
    content = api.calls[0]["body"]["messages"][0]["content"]
    assert [b.get("title") for b in content[:3]] == ["TOLLAND lcl 07-06.pdf", "TOLLAND air 07-06.pdf", "booking form.pdf"]
    assert ("Files sent with it, in this order: TOLLAND lcl 07-06.pdf (a PDF); TOLLAND air 07-06.pdf (a PDF); "
            "booking form.pdf (a PDF)") in content[-1]["text"]
    assert '"files": one entry for each file named under "Files sent with it"' in rq.SYSTEM_PROMPT
    assert "files" in rq.answer_schema()["required"]


def test_no_files_sent_no_file_warnings(world):
    got, _ = read(world, "OPP-9991", answer((None, [option([line("LCL FREIGHT", unit="CBM", rate=64)])])))
    assert draft(world, got)["warnings"] == [] and rq.READ_QUOTE_PROMPT == 4


def test_the_reports_list_the_files_with_used_or_why(world, monkeypatch):
    conn = world["conn"]
    source, _ = mail_with_files(FILES)
    first, _ = read(world, "OPP-9991", answer((None, [option([line("LCL FREIGHT", unit="CBM", rate=64)])]),
                                              files=USED), source=source)
    monkeypatch.setattr(rq, "OUTLOOK", FakeOutlook(body="Please find our rates attached.", files=FILES))
    api = FakeAPI(response(answer((None, [option([line("LCL FREIGHT", unit="CBM", rate=64)])]), files=USED + [
        {"name": "booking form.pdf", "used": False, "why": "a booking form"}])))
    text = rq.again(conn, object(), [first.id], today=TODAY, transport=api).read_text(encoding="utf-8")
    assert ("Files: TOLLAND lcl 07-06.pdf (sent; prices taken); TOLLAND air 07-06.pdf (sent; no prices taken: Air rates; "
            "the route is LCL); booking form.pdf (sent; no prices taken: a booking form); image001.png (not sent: a "
            "picture in the mail's body or signature)") in text
    assert "booking form.pdf (sent; the reading did not say)" in text            # the earlier reading's line
    assert "- warning: booking form.pdf: no prices taken from it (a booking form)" in text
