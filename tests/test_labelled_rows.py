"""An air request written as a table (invented sample): a request
written as a table, one label per row, the value on the next line. One
consignment, not two; the sender's chargeable weight is a cross-check,
never an actual weight; gross wins over net; Volume fills the cbm field
for Air too."""

from __future__ import annotations

from conftest import add_party, seed_reference
from opplist import cargo, db, intake, outlook_bridge, parsing, paths

TABLE = "air_request_labelled_rows_table.json"
ROWS = ["Reference", "DEMO-MAA-PHX",
        "Routing", "Air from Chennai (MAA), India - door delivery, Flagstaff, AZ 86001, USA",
        "Terms", "DDU - import duties for the receiver",
        "Commodity", "Brass table lamps without bulbs, boxed. General cargo, no lithium, "
                     "not dangerous. 480 gift boxes in 48 cartons",
        "Gross weight", "365.65 kg", "Volume", "2.863 cbm", "Dimensions", "40 x 35.5 x 42 cm x 48 cartons",
        "Chargeable weight", "477.15 kg on volumetric", "Cargo ready", "Early October"]


def test_a_bare_label_takes_the_next_line_as_its_value():
    joined = parsing.join_label_values(["Gross weight", "365.65 kg", "Volume", "", "2.863 cbm", "Cargo ready", "TBC"])
    assert joined == ["Gross weight: 365.65 kg", "Volume: 2.863 cbm", "Cargo ready", "TBC"]
    assert parsing.join_label_values(["Weight:"]) == ["Weight"]              # a label with nothing after it


def test_the_labelled_rows_are_one_item_and_the_chargeable_line_is_not_a_weight():
    pk = parsing.parse_packing(ROWS)
    assert [(i.pieces, i.word, i.dims_text, i.weight_text) for i in pk.items] == [(48, "cartons", "40 x 35.5 x 42 cm", "365.65 kg")]
    assert pk.actual_kg == 365.65 and round(pk.volumetric_kg, 1) == 477.1 and round(pk.chargeable_kg, 1) == 477.1
    assert round(pk.cbm, 3) == 2.863 and pk.qty_raw == "48 cartons 40 x 35.5 x 42 cm, 365.65 kg"
    assert pk.stated_chargeable_kg == 477.15 and pk.chargeable_check == "sender's chargeable 477.15 kg matches"
    # the same rows written inline, tab-separated, as another mail client does
    inline = ["{}\t{}".format(a, b) for a, b in zip(ROWS[::2], ROWS[1::2])]
    assert parsing.parse_packing(inline).qty_raw == pk.qty_raw
    # theirs off by more than 2 %: said so
    far = parsing.parse_packing(ROWS[:-4] + ["Chargeable weight", "610 kg"])
    assert far.chargeable_check == "sender's chargeable 610 kg differs from ours (477.1 kg)" and far.actual_kg == 365.65
    # the other spellings of the sender's figure
    for label in ("Volumetric weight 118 kg", "Vol. weight: 118 kg", "Volume weight 118 kg", "C.W. 118 kg", "Chg wt 118 kg"):
        one = parsing.parse_packing(["3 cartons 50 x 40 x 35 cm", "Weight 26 kg", label])
        assert one.actual_kg == 26 and one.stated_chargeable_kg == 118, label
    # a pieces count equal to the current item's never opens a second item
    same = parsing.parse_packing(["48 cartons", "Gross weight 365 kg", "Dimensions 40 x 35.5 x 42 cm x 48 cartons"])
    assert len(same.items) == 1 and same.items[0].dims_text == "40 x 35.5 x 42 cm"
    # a different count still does
    other = parsing.parse_packing(["48 cartons", "Gross weight 365 kg", "3 pallets 110 x 90 x 95 cm, 410 kg"])
    assert [i.pieces for i in other.items] == [48, 3]


def test_gross_wins_over_net_whichever_comes_first():
    first = parsing.parse_packing(["10 cartons", "Net weight 90 kg", "Gross weight 100 kg"])
    assert first.actual_kg == 100 and first.items[0].weight_text == "100 kg"
    second = parsing.parse_packing(["10 cartons", "Gross weight 100 kg", "Net weight 90 kg"])
    assert second.actual_kg == 100
    alone = parsing.parse_packing(["10 cartons", "Net weight 90 kg"])
    assert alone.actual_kg == 90


def test_the_table_fixture_prefills_one_consignment(repo):
    seed_reference(repo)
    for name in ("India",):
        if repo.conn.execute("SELECT 1 FROM country WHERE name = ?", (name,)).fetchone() is None:
            repo.insert("country", {"name": name, "region": "Asia"})
    add_party(repo, "JoJo JJC", ptype="Office", country="USA")
    repo.commit()
    draft = intake.build(repo.conn, outlook_bridge.read_fixture(paths.FIXTURES_DIR / TABLE))
    assert draft.values["service_mode"] == "Air"
    assert draft.values["air_weight_kg"] == 477.1 and draft.values["lcl_cbm"] == 2.863
    assert draft.values["qty_raw"] == "48 cartons 40 x 35.5 x 42 cm, 365.65 kg"
    assert draft.sources["air_weight_kg"].endswith("· sender's chargeable 477.15 kg matches")
    assert "volumetric 48 x 40 x 35.5 x 42 cm = 2862720 cm3 / 6000 = 477.1 kg" in draft.sources["air_weight_kg"]
    assert draft.sources["lcl_cbm"].endswith("as stated") and draft.evidence["lcl_cbm"] == "Volume: 2.863 cbm"
    assert not any("Chargeable weight" in n for n in draft.notes)          # matching: nothing to flag
    assert not any("dims give" in n for n in draft.notes)                   # 2.863 stated, 2.86 off the dims
    assert len(draft.cargo_lines) == 1
    line = draft.cargo_lines[0]
    assert (line["pieces"], line["pack_word"], line["dims_text"], line["weight_kg"], line["cbm"]) == \
        (48, "cartons", "40 x 35.5 x 42 cm", 365.65, 2.863)
    assert line["commodity"].startswith("Brass table lamps") and "differs" not in (line["notes"] or "")
    derived = cargo.derive(line, "Air")
    assert derived["cbm_from"] == "stated" and derived["cbm_note"] == "" and round(derived["chargeable_kg"], 1) == 477.2


def test_volume_fills_the_cbm_field_for_an_air_case_too(repo):
    seed_reference(repo)
    repo.commit()
    from opplist.outlook_bridge import MailInfo
    mail = MailInfo(subject="AIR QUOTE // MAA - PHX", sender_name="Anjali", sender_address="pricing@coromandel-air.example",
                    received_on="2026-09-23", received_at="2026-09-23 11:52", entry_id="E-1", conversation_id="C-1",
                    body_excerpt="\n".join(ROWS))
    draft = intake.build(repo.conn, mail)
    assert draft.values["service_mode"] == "Air" and draft.values["lcl_cbm"] == 2.863
    # dims only: the cbm off the dims, formula in the label
    mail.body_excerpt = "48 cartons\nDimensions 40 x 35.5 x 42 cm\nGross weight 365.65 kg\n"
    draft = intake.build(repo.conn, mail)
    assert draft.values["lcl_cbm"] == 2.86 and "48 x 40 x 35.5 x 42 cm = 2.86 cbm" in draft.sources["lcl_cbm"]
    # stated and dims far apart: the stated one, and a note
    mail.body_excerpt = "48 cartons\nDimensions 40 x 35.5 x 42 cm\nGross weight 365.65 kg\nVolume 4 cbm\n"
    draft = intake.build(repo.conn, mail)
    assert draft.values["lcl_cbm"] == 4 and any(n == "stated 4 cbm, dims give 2.86 cbm" for n in draft.notes)
