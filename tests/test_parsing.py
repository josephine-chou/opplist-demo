"""Subjects and quantities, against the sample subjects and quantity strings in fixtures."""

from __future__ import annotations

import csv

import pytest

from conftest import FIXTURES
from opplist.parsing import clean_subject, normalise_container, parse_qty, thread_key


def _expected_rows():
    with (FIXTURES / "thread_keys_expected.csv").open(encoding="utf-8-sig",
                                                      newline="") as fh:
        return list(csv.DictReader(fh))


EXPECTED = _expected_rows()


def test_fixture_files_line_up():
    subjects = [line.rstrip("\n") for line in
                (FIXTURES / "subject_samples.txt").read_text(
                    encoding="utf-8").splitlines() if line.strip()]
    assert len(subjects) == len(EXPECTED) == 57          # AL1: six invented numbered subjects
    assert [row["subject"] for row in EXPECTED] == subjects


@pytest.mark.parametrize("row", EXPECTED, ids=[r["subject"][:40] for r in EXPECTED])
def test_clean_subject_and_thread_key(row):
    """Must match Get-CleanSubject / Get-ThreadKey in Build-RateRequestBoard.ps1."""
    cleaned = clean_subject(row["subject"])
    assert cleaned == row["clean_subject"]
    assert thread_key(cleaned) == row["thread_key"]


def test_thread_key_is_stable_across_reply_prefixes():
    a = "RATE REQUEST // TPA - GYE"
    for prefix in ("RE: ", "FW: ", "RES: ", "AW: ", "Fwd: ", "RE: RE: FW: "):
        assert thread_key(clean_subject(prefix + a)) == thread_key(clean_subject(a))


def test_clean_subject_handles_empty():
    assert clean_subject(None) == ""
    assert clean_subject("") == ""
    assert thread_key("") == ""


# ------------------------------------------------------------------ 5.3 qty --
def _qty_cases():
    cases = []
    for line in (FIXTURES / "qty_samples.txt").read_text(
            encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(" | ")
        raw = " | ".join(parts[:-1])
        expected = parts[-1].split(",")
        cases.append((raw, expected))
    return cases


QTY_CASES = _qty_cases()


def _value(text):
    return None if text == "-" else text


@pytest.mark.parametrize("raw,expected", QTY_CASES, ids=[c[0] for c in QTY_CASES])
def test_parse_qty(raw, expected):
    mode, ctype, cqty, air, lcl = expected
    result = parse_qty(raw)
    assert result.mode_hint == _value(mode)
    assert result.container_type == _value(ctype)
    assert result.container_qty == (None if cqty == "-" else int(cqty))
    assert result.air_weight_kg == (None if air == "-" else float(air))
    assert result.lcl_cbm == (None if lcl == "-" else float(lcl))


def test_parse_qty_ignores_text_it_cannot_read():
    assert parse_qty("").is_empty()
    assert parse_qty(None).is_empty()
    assert parse_qty("call the agent").is_empty()


def test_parse_qty_refuses_a_range_or_a_foreign_unit():
    """'Air:2-2.5 TONS' must not become 2 kg. Filling nothing is the safe answer."""
    for raw in ("Air:2-2.5 TONS", "Air:approx 3 tons", "LCL:4-5", "Air:3 TONS"):
        result = parse_qty(raw)
        assert result.air_weight_kg is None and result.lcl_cbm is None


def test_parse_qty_accepts_a_stated_unit():
    assert parse_qty("Air:340 KG").air_weight_kg == 340
    assert parse_qty("LCL:3 CBM").lcl_cbm == 3


def test_container_normalisation():
    assert normalise_container("40", "HC") == "40HQ"
    assert normalise_container("40", None) == "40GP"
    assert normalise_container("20", "") == "20GP"
    assert normalise_container("45", None) == "45HQ"
    assert normalise_container("20", "OT") == "20OT"


# --------------------------------------------- 5.2 subject candidates ----
from opplist.parsing import looks_like_place, parse_subject   # noqa: E402


def test_a_house_format_subject_yields_every_field():
    p = parse_subject("RE: RATE REQUEST // C/BRENVO // PDX - PRG // 1 X 40' // DDP")
    assert p.lanes == [("PDX", "PRG")]
    assert p.service_mode is None            # no mode word in this subject
    assert p.incoterm == "DDP"
    assert p.end_customer == "BRENVO"
    assert p.containers == [(1, "40GP")]
    assert p.thread_key == "RATEREQUESTCBRENVOPDXPRG1X40DDP"


def test_two_origins_make_two_lanes():
    p = parse_subject("RATE REQUEST // ORVANE PUMPS // FCA // SGN / HPH - CVG")
    assert p.lanes == [("SGN", "CVG"), ("HPH", "CVG")]
    assert p.incoterm == "FCA"
    assert p.names == ["ORVANE PUMPS"]


def test_two_destinations_make_two_lanes():
    p = parse_subject("RATE REQUEST // CHARLESTON - ARICA / IQUIQUE CHILE")
    assert p.lanes == [("CHARLESTON", "ARICA"), ("CHARLESTON", "IQUIQUE CHILE")]


def test_a_slash_without_spaces_also_splits():
    p = parse_subject("RATE REQUEST // 20' REEFER // LGB/OAK - PUERTO BOLIVAR, ECUADOR")
    assert p.lanes == [("LGB", "PUERTO BOLIVAR, ECUADOR"), ("OAK", "PUERTO BOLIVAR, ECUADOR")]
    assert p.containers == [(None, "20RF")]
    assert p.special_tags == ["Reefer"]


def test_a_reference_number_is_not_a_destination():
    p = parse_subject("RE: RATE REQUEST // OMAHA, NE - QINGDAO / USOQ4418")
    assert p.lanes == [("OMAHA, NE", "QINGDAO")]
    assert "USOQ4418" in p.references


def test_a_state_code_and_a_zip_survive():
    p = parse_subject("RATE REQUEST // DULUTH, MN - MARQUETTE, MI 49855")
    assert p.lanes == [("DULUTH, MN", "MARQUETTE, MI 49855")]


def test_a_two_letter_state_is_a_place():
    p = parse_subject("RE: PICKUP REQUEST FOR DELMORA - AZURE // ND - VAP")
    assert ("ND", "VAP") in p.lanes


def test_a_sentence_with_dashes_is_not_a_lane():
    """'PICKUP REQUEST FOR DELMORA - AZURE' is a description, not a lane."""
    p = parse_subject("RE: PICKUP REQUEST FOR DELMORA - AZURE // ND - VAP")
    assert p.lanes == [("ND", "VAP")]


def test_a_rate_sheet_listing_ten_ports_offers_no_lane():
    p = parse_subject(
        "FW: //Update PMLQX rates//KR ECSA - Rate for EA40912K/EA40913K/WB22719K "
        "(KR to SANTOS/PARANAGUA/ITAJAI /RIO GRANDE / MONTEVIDEO / BUENOS AIRES/"
        "NAVEGANTES/SALVADOR/SUAPE/PECEM)")
    assert p.lanes == [], "guessing lanes out of a rate sheet is worse than none"


@pytest.mark.parametrize("subject,expected", [
    ("RATE REQUEST // C/QUILLON // BNA - AMS", "QUILLON"),
    ("RATE REQUEST // C/ 7RT // SGN - SAV", "7RT"),
    ('HT2284 // RUSH AIR QUOTE // CNEE TORREVA PERU"', "TORREVA PERU"),
    ("RATE REQUEST // CLIENTE VENTRIS", "VENTRIS"),
])
def test_the_end_customer_conventions(subject, expected):
    assert parse_subject(subject).end_customer == expected


@pytest.mark.parametrize("subject,mode", [
    ("RATE REQUEST // LCL // VIETNAM - ARGENTINA", "LCL"),
    ("QZ63/40991 // RES: RATE REQUEST // ITAJAI - MOBILE // FCL", "FCL"),
    ("RE: Air cost needed for sample parcel", "Air"),
    ("RE: TRUCK LOAD // C/TARNOVA // DAYTON - FRESNO", "Truck"),      # TK1
    ("RATE REQUEST // RORO // JAX - SJU", "RORO"),
])
def test_mode_keywords(subject, mode):
    assert parse_subject(subject).service_mode == mode


def test_ex_works_spellings_all_mean_exw():
    for spelling in ("EXW", "EX WORKS", "Exworks", "EXWORK"):
        assert parse_subject("QUOTE // {} // TPA - GYE".format(spelling)).incoterm \
            == "EXW"


def test_a_three_letter_code_not_in_the_location_list_is_flagged():
    p = parse_subject("RATE REQUEST // ZZZ - CVG", known_locations={"CVG"})
    assert p.lanes == [("ZZZ", "CVG")]
    assert p.unknown_locations == ["ZZZ"]


def test_a_known_code_is_not_flagged():
    p = parse_subject("RATE REQUEST // TPA - CVG", known_locations={"TPA", "CVG"})
    assert p.unknown_locations == []


def test_looks_like_place():
    assert looks_like_place("JAX")
    assert looks_like_place("PUERTO BOLIVAR, ECUADOR")
    assert looks_like_place("ND")
    assert not looks_like_place("USOQ4418")
    assert not looks_like_place("1 X 40'")
    assert not looks_like_place("Rate for EA40912K")
    assert not looks_like_place("a" * 60)


def test_an_empty_subject_parses_to_nothing():
    p = parse_subject("")
    assert p.lanes == [] and p.thread_key == "" and p.names == []


def test_every_fixture_subject_parses_without_error():
    """Whatever comes in, the parser offers candidates and never throws."""
    for row in EXPECTED:
        parsed = parse_subject(row["subject"])
        assert parsed.thread_key == row["thread_key"]
        assert isinstance(parsed.lanes, list)


# ------------------------------------------------- the body scan (5.2) ----
#: The locations already on file, as the app would hand them over.
KNOWN = {"IAH", "CLEVELAND", "PORTLAND", "LIMA", "CHITTAGONG", "NORFOLK, VA",
         "PHX", "RUH", "PERU", "CALLAO"}


def _body_blocks():
    """fixtures/body_samples.txt: '=== name' then the body as Outlook gave it."""
    blocks, name, lines = {}, None, []
    for line in (FIXTURES / "body_samples.txt").read_text(
            encoding="utf-8").splitlines():
        if line.startswith("==="):
            if name:
                blocks[name] = "\n".join(lines).strip("\n")
            name, lines = line[3:].strip(), []
        elif name is not None:
            lines.append(line)
        # lines before the first '===' are the header comment
    if name:
        blocks[name] = "\n".join(lines).strip("\n")
    return blocks


BODIES = _body_blocks()


def _body_expectations():
    text = (FIXTURES / "body_lanes_expected.csv").read_text(encoding="utf-8-sig")
    rows = [line for line in text.splitlines() if not line.startswith("#")]
    return list(csv.DictReader(rows))


BODY_EXPECTED = _body_expectations()


def test_body_fixture_files_line_up():
    assert {row["name"] for row in BODY_EXPECTED} == set(BODIES)


@pytest.mark.parametrize("row", BODY_EXPECTED, ids=lambda r: r["name"])
def test_every_body_sample_reads_the_way_it_should(row):
    from opplist.parsing import parse_body_lanes

    result = parse_body_lanes(BODIES[row["name"]], known_locations=KNOWN)
    assert (result.origins[0] if result.origins else "") == row["prefill_origin"]
    assert (result.dests[0] if result.dests else "") == row["prefill_dest"]

    found = "|".join("{}={}".format(side, place)
                     for side in ("origin", "dest")
                     for place, _ in result.conflicts.get(side, []))
    assert found == row["candidates"]


def test_a_body_lane_carries_the_line_it_came_from():
    from opplist.parsing import parse_body_lanes

    result = parse_body_lanes(BODIES["pol-pod-labels"], known_locations=KNOWN)
    assert result.evidence["origin"] == "POL: Cleveland, U.S"
    assert result.evidence["dest"] == "POD: Chittagong, Bangladesh"


def test_two_origins_in_one_sentence_are_two_lanes():
    from opplist.parsing import parse_body_lanes

    result = parse_body_lanes(BODIES["two-origins-from-and-to"],
                              known_locations=KNOWN)
    assert result.origins == ["Chennai", "Tuticorin"]
    assert result.lanes == [("Chennai", "Norfolk"), ("Tuticorin", "Norfolk")]


def test_the_scan_stops_at_the_quoted_mail():
    """The lane in 'Assunto: RE: ... DETROIT - PERU' belongs to the older mail."""
    from opplist.parsing import parse_body_lanes

    result = parse_body_lanes(BODIES["reply-quotes-the-lane-in-portuguese"],
                              known_locations=KNOWN)
    assert result.origins == [] and result.dests == []
    assert result.scanned_lines == 3          # everything below 'De:' is ignored


def test_the_scan_reads_at_most_forty_lines():
    from opplist import parsing

    body = "\n".join(["line {}".format(n) for n in range(60)] + ["POD: Chittagong"])
    result = parsing.parse_body_lanes(body, known_locations=KNOWN)
    assert result.scanned_lines == parsing.BODY_SCAN_LINES
    assert result.dests == []                 # the POD line is out of range


def test_blank_lines_do_not_use_up_the_forty():
    from opplist import parsing

    body = "\n\n".join(["Hello"] * 20 + ["POL: Cleveland", "POD: Chittagong"])
    result = parsing.parse_body_lanes(body, known_locations=KNOWN)
    assert result.origins == ["Cleveland"] and result.dests == ["Chittagong"]


def test_an_empty_body_is_not_an_error():
    from opplist.parsing import parse_body_lanes

    for body in (None, "", "   \n\n"):
        result = parse_body_lanes(body)
        assert result.origins == [] and result.dests == [] and result.lanes == []


# --------------------------------------------- the From: line of a quote ---
def test_the_quoted_from_line_gives_the_address():
    from opplist.parsing import quoted_senders

    assert quoted_senders(BODIES["reply-quotes-the-lane-in-portuguese"]) == [
        "victorlang@jojofreight.example"]


def test_quoted_from_lines_are_read_in_every_language_outlook_writes():
    from opplist.parsing import quoted_senders

    body = ("Hi,\n\nSee below.\n\n"
            "De: Tomas Ibanez <tomas.ibanez@tolvex.example>\n"
            "Para: VictorLang@jojofreight.example\n\n"
            "From: Bianca Duarte <bduarte@rotaline.example>\n")
    assert quoted_senders(body) == ["tomas.ibanez@tolvex.example",
                                    "bduarte@rotaline.example"]


def test_a_body_with_no_quote_has_no_quoted_sender():
    from opplist.parsing import quoted_senders

    assert quoted_senders(BODIES["pol-pod-labels"]) == []
    assert quoted_senders("") == []


# ----------------------------------------- the body scan: quantity (5.2) ---
def test_the_body_gives_the_boxes_and_the_mode():
    from opplist.parsing import parse_body_qty

    result = parse_body_qty(BODIES["three-pod-options"])
    assert result.qty_raw == "FCL:3 x 40HQ"       # 'QTY: 3x40HC' in the mail
    assert result.mode == "FCL"
    assert result.evidence == "QTY: 3x40HC"


def test_a_bare_size_needs_a_count():
    from opplist.parsing import parse_body_qty

    assert parse_body_qty("Our lanes normally run in 40HQ boxes\n").qty_raw == ""
    assert parse_body_qty("Kindly quote 1 X 20' ex Cleveland\n").qty_raw == \
        "FCL:1 x 20GP"


def test_kilos_read_as_air_and_cbm_as_lcl():
    from opplist.parsing import parse_body_qty

    air = parse_body_qty("Gross Weight: 212.50 kg\n")
    assert (air.qty_raw, air.mode) == ("Air:212.5", "Air")
    lcl = parse_body_qty("Total volume 4.8 cbm\n")
    assert (lcl.qty_raw, lcl.mode) == ("LCL:4.8", "LCL")
    assert parse_body_qty("Weight 1,250 kgs\n").qty_raw == "Air:1250"


def test_boxes_win_over_weight():
    from opplist.parsing import parse_body_qty

    result = parse_body_qty("3 x 40HQ, total 24,500 kgs\n")
    assert result.qty_raw == "FCL:3 x 40HQ" and result.mode == "FCL"


def test_several_boxes_are_one_mixed_load():
    from opplist.parsing import parse_body_qty

    assert parse_body_qty("Need 3 x 40HQ and 2 x 20\n").qty_raw == \
        "FCL:3 x 40HQ / 2 x 20GP"


def test_a_weight_per_crate_fills_nothing():
    """Two crates, two weights: adding them up would invent a total."""
    from opplist.parsing import parse_body_qty

    result = parse_body_qty(
        "1. Main Crate\nGross Weight: 212.50 kg\n"
        "2. Accessory Crate\nGross Weight: 96 kgs\n")
    assert result.qty_raw == "" and result.mode is None
    assert [value for value, _ in result.conflicts] == ["212.5", "96"]


def test_what_the_body_says_below_the_quote_does_not_count():
    from opplist.parsing import parse_body_qty

    assert parse_body_qty(BODIES["reply-quotes-the-lane-in-portuguese"]).qty_raw == ""


def test_dimensions_and_decimal_commas_in_a_packing_list():
    from opplist.parsing import parse_body_qty, quoted_mails

    body = ("Cargo details:\n"
            "*\t5 pcs, 31,5 kg - 72 × 58 × 64 cm each\n"
            "*\t2 pcs , 9,5 kg - 45 × 38 × 30 cm each\n")
    result = parse_body_qty(body)
    assert result.qty_raw == "Air:176.5"                  # 5 x 31.5 + 2 x 9.5, not 72 boxes
    assert result.mode == "Air"
    assert result.evidence.endswith("each (+1 more line)")
    assert parse_body_qty("3 x 40HQ\n").qty_raw == "FCL:3 x 40HQ"
    assert parse_body_qty("crate 2 x 40 x 30 cm\n").qty_raw == ""
    assert parse_body_qty("Peso: 31,5 kg\n").qty_raw == "Air:31.5"
    assert parse_body_qty("Weight 1,250 kg\n").qty_raw == "Air:1250"
    assert parse_body_qty("3 pallets 450 kg\n").qty_raw == "Air:450"      # no 'each': the total
    mixed = parse_body_qty("3 pallets 450 kg\nGross weight 900 kg\n")
    assert mixed.qty_raw == "" and len(mixed.conflicts) == 2
    quoted = quoted_mails("x\n\nFrom: Tomas Ibanez <j@tolvex.example <mailto:j@tolvex.example> >\n"
                          "Sent: Tuesday, August 18, 2026 9:41 AM\nTo: y\nSubject: s\n\nhi\n")
    assert quoted[0].name == "Tomas Ibanez"


# ------------------------------- mode, packing, addresses, terms (5.2) --
def test_the_body_mode_words():
    from opplist.parsing import body_mode

    line = "We need an air freight offer for the order attached, please treat as urgent."
    assert body_mode([line]) == ("Air", line)
    assert body_mode(["Airline : QR"])[0] == "Air"
    assert body_mode(["AWB Fee : $35.00/AWB"])[0] == "Air"
    assert body_mode(["ocean freight, 3 x 40HQ FCL"])[0] == "FCL"
    assert body_mode(["sea freight please, LCL"])[0] == "LCL"
    assert body_mode(["vessel option"])[0] == "Ocean"
    assert body_mode(["Could UPS or DHL courier handle this more cheaply?"]) == ("", "")
    assert body_mode(["air freight or sea freight - whichever costs less"]) == ("", "")


def test_a_packing_list_gives_the_chargeable_weight():
    from opplist.parsing import parse_packing

    pk = parse_packing(["1 Carton", "Size: 22” x 14” x 12”", "Weight: 9LBS", "Material: x"])
    assert pk.qty_raw == "1 carton 22 x 14 x 12 in, 9 lbs (4.1 kg)"
    assert round(pk.chargeable_kg, 1) == 10.1 and round(pk.actual_kg, 2) == 4.08
    assert pk.formula == "chargeable = max(actual 4.1 kg, volumetric 22 x 14 x 12 in = 60566.6 cm3 / 6000 = 10.1 kg)"
    assert pk.evidence == "1 Carton"
    pk = parse_packing(["3 CTNS", "Dims: 50 x 40 x 30 cm", "Gross weight: 20 kg each"])
    assert pk.qty_raw == "3 ctns 50 x 40 x 30 cm, 20 kg each (60 kg)"
    assert pk.chargeable_kg == 60
    pk = parse_packing(["2 pallets 120x80x100cm, 300 kg"])
    assert pk.qty_raw == "2 pallets 120 x 80 x 100 cm, 300 kg"
    assert pk.chargeable_kg == 320
    assert parse_packing(["Gross weight: 212.50 kg"]).is_packing_list is False
    pk = parse_packing(["2 pallets", "Dimensions: 120 x 80 x 100", "Weight: 450"])
    assert pk.chargeable_kg is None and pk.candidates == ["120", "80", "100", "450", "2 pallets"]


def test_pickup_and_delivery_addresses():
    from opplist.parsing import parse_addresses

    lines = ["Pick-up:", "Redrock Abrasives Inc", "4471 W Sandstone Way", "Sedona, AZ 86336",
             "Hours: Monday-Friday 7:30AM-2:00PM", "Delivery to: 615 Tollway Ct, Bismarck, ND 58501"]
    found = parse_addresses(lines)
    assert found["origin"] == {"text": "Redrock Abrasives Inc, 4471 W Sandstone Way, Sedona, AZ 86336",
                               "company": "Redrock Abrasives Inc", "country": "USA", "line": "Pick-up:"}
    assert found["dest"]["text"] == "615 Tollway Ct, Bismarck, ND 58501"
    assert found["dest"]["company"] == "" and found["dest"]["country"] == "USA"
    found = parse_addresses(["Pick up from Sedona, AZ 86336", "Trucking : $185.00 Min"])
    assert found["origin"]["text"] == "Sedona, AZ 86336"
    found = parse_addresses(["Delivery address:",
                             "*\t7420 KESTREL POINT DR STE 4 ORLANDO, FLORIDA 32824, ESTADOS UNIDOS",
                             "I'll wait for your reply"])
    assert found["dest"]["text"] == "7420 KESTREL POINT DR STE 4 ORLANDO, FLORIDA 32824, ESTADOS UNIDOS"
    assert found["dest"]["country"] == ""            # no two-letter state and ZIP
    assert parse_addresses(["nothing here"]) == {}


def test_terms_lines_and_more_commodity_labels():
    from opplist.parsing import terms_lines, body_commodity

    assert terms_lines(["We need this door-to-door.", "Terms: FCA Chennai", "door to door please"]) == \
        ["We need this door-to-door.", "door to door please"]
    assert body_commodity(["Material: Carbon Brush Blocks"]) == \
        ("Carbon Brush Blocks", "Material: Carbon Brush Blocks")
    assert body_commodity(["Description: spare parts"])[0] == "spare parts"
    assert body_commodity(["Mercancía: repuestos"])[0] == "repuestos"


def test_cargo_labels_terms_service_and_our_quote():
    from opplist.parsing import (parse_cargo_labels, body_incoterm, air_service_lines,
                                 our_quote_line, terms_lines, parse_addresses, parse_subject)

    lines = ["Total Units: 540", "Total Cartons: 45", "Gross Weight (KGs): 188",
             "Volume (CBMs): 0.8412", "Chargeable: 188.00"]
    labels = parse_cargo_labels(lines)
    assert labels.qty_raw == "45 cartons, 188 kg, 0.8412 cbm"             # 13b: no chargeable words
    assert labels.chargeable_kg == 188 and labels.formula == "chargeable weight as stated"
    labels = parse_cargo_labels(["Pieces: 4", "G.W.: 310 lbs", "Volume: 1.2 cbm"])
    assert labels.qty_raw == "4 pieces, 140.6 kg, 1.2 cbm"
    assert labels.formula == "chargeable = max(actual 140.6 kg, volumetric 1.2 cbm x 166.7 = 200 kg)"
    assert parse_cargo_labels(["Weight: 450 kg"]).found is False       # a lone weight line: the old way
    assert body_incoterm(["Po Terms:FCA"]) == ("FCA", "Po Terms:FCA")
    assert body_incoterm(["Terms: door to door"]) == ("", "Terms: door to door")
    assert body_incoterm(["nothing"]) == ("", "")
    assert air_service_lines(["Air Service Type Requested:7 - 9 DAYS Economy Service"]) == \
        ["Air service: 7 - 9 days economy"]
    assert our_quote_line(["Hi Marco,", "Please note destination charges :Airline THC  USD 210"]) == \
        "Please note destination charges :Airline THC USD 210"
    assert our_quote_line(["Please quote", "Handling USD 145"]) == "Handling USD 145"
    assert our_quote_line(["Please quote this for me", "thanks"]) == ""
    long = "**Quoted transit must be door-to-door, from uplift at the origin airport to delivery at our DC** " * 2
    assert terms_lines([long, "We need this door-to-door."]) == ["We need this door-to-door."]
    assert parse_addresses(["Pick up USD 95 + IVA ( 10%)"]) == {}
    found = parse_addresses(["Final Destination Address:VALDORA HOGAR S.A. AVENIDA LOS LAPACHOS 2275 ASUNCION 1536 PY",
                             "Freight Payment:Collect"])
    assert found["dest"]["text"] == "VALDORA HOGAR S.A. AVENIDA LOS LAPACHOS 2275 ASUNCION 1536 PY"
    assert parse_subject("RE: Air Quote #7426  request for Lumen Brands").references == ["7426"]


def test_labelled_airports_with_names_and_countries():
    from opplist.parsing import parse_body_lanes

    result = parse_body_lanes("Origin Airport: Dhaka\n"
                              "Destination Airport:Asuncion/Silvio Pettirossi International Airport, Paraguay\n")
    assert result.origins == ["Dhaka"]
    assert result.dests == ["Asuncion", "Silvio Pettirossi International"]
    result = parse_body_lanes("Port of Loading: Guayaquil\nDestination Port: Tampa, FL\n")
    assert (result.origins, result.dests) == (["Guayaquil"], ["Tampa"])
