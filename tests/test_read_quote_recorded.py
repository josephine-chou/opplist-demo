"""Read quote on recorded answers (this edition): no key, no call.

With setting read_quote_recorded on, read_quote.call answers a request from
the recording in fixtures/read_quote whose words are in the text sent, and
the answer goes through the same check, rules and cost arithmetic as one
from the API. Every mail and answer here is invented.
"""

from __future__ import annotations

import copy
import json

import pytest

from conftest import seed_reference
from opplist import db, paths, read_quote, read_quote_recorded

MAIL = ("Hi Rafe,\n\nOur offer for 1 x 40'HQ Savannah to Callao:\n"
        "Ocean freight USD 1,840 per 40'HQ\nTHC at origin USD 210 per container\nDoc fee USD 65 per BL\n"
        "Valid until 30 June 2027. Transit about 19 days, direct service with Seabright Line.\n"
        "Free time 10 days at destination.\n\nRegards,\nOna")


def content(text):
    return [{"type": "text", "text": "The case: FCL, 1 x 40'HQ."}, {"type": "text", "text": text}]


@pytest.fixture
def conn(tmp_path, monkeypatch):
    connection = db.open_db(tmp_path / "recorded.db")
    seed_reference(db.Repository(connection, source="test"))
    connection.commit()
    # neither the network function nor a test's transport: only the recordings can answer
    monkeypatch.setattr(read_quote, "TRANSPORT", None)

    def no_call(*args, **kwargs):
        raise AssertionError("the network function was reached")
    monkeypatch.setattr(read_quote, "_requests_transport", no_call)
    yield connection
    connection.close()


def test_every_recording_passes_the_answer_check():
    found = read_quote_recorded.recordings()
    assert found, "no recording in fixtures/read_quote"
    for doc in found:
        assert doc["about"] and all(isinstance(w, str) and w.strip() for w in doc["match"]), doc["name"]
        read_quote.check_answer(read_quote.nulls(copy.deepcopy(doc["answer"])))
        assert read_quote.cost_of(read_quote.DEFAULT_MODEL, doc.get("usage") or {}) >= 0


def test_off_by_default_and_then_a_key_is_what_is_missing(conn):
    assert not read_quote.recorded(conn)
    assert read_quote.key_state(conn) == (False, "no key at {}".format(paths.DATA_DIR / "secrets" / "anthropic-key.txt"))
    with pytest.raises(read_quote.ReadQuoteError, match="no key at"):
        read_quote.call(conn, read_quote.DEFAULT_MODEL, content(MAIL))


def test_switched_on_the_recording_answers_without_a_key(conn):
    db.set_setting(conn, read_quote.RECORDED_SETTING, "1")
    assert read_quote.key_state(conn) == (True, "not needed: recorded answers")
    got = read_quote.call(conn, read_quote.DEFAULT_MODEL, content(MAIL))
    assert got.error == "" and got.answer["priced"] is True
    [option] = got.answer["routes"][0]["options"]
    assert [(l["charge"], l["unit"], l["rate"]) for l in option["lines"]] == [
        ("Ocean freight", "40'HQ", 1840), ("THC at origin", "CNTR", 210), ("Documentation", "BL", 65)]
    assert all(l["min"] is None and l["qty"] is None for l in option["lines"])      # -1 in the recording
    assert (option["valid_until"], option["carrier"]) == ("2027-06-30", "Seabright Line")
    # the cost is worked out from the recording's usage, as from a real answer's
    assert got.usage["input_tokens"] == 8600 and read_quote.cost_of(read_quote.DEFAULT_MODEL, got.usage) > 0
    assert json.loads(got.raw)["recording"] == "fcl_offer_with_validity_and_free_time.json"
    assert not (paths.DATA_DIR / "secrets").exists()


def test_a_mail_no_recording_fits_is_an_error_not_a_guess(conn):
    db.set_setting(conn, read_quote.RECORDED_SETTING, "1")
    got = read_quote.call(conn, read_quote.DEFAULT_MODEL, content("Hi,\nAir freight USD 3.40 per kg.\nBo"))
    assert got.answer is None and "no recorded answer fits this mail" in got.error


def test_the_longest_match_wins_and_spacing_does_not_matter(tmp_path):
    def write(name, match, carrier):
        answer = {"priced": True, "why_not": "", "routes": [], "files": [], "carrier": carrier}
        (tmp_path / name).write_text(json.dumps({"about": "x", "match": match, "answer": answer}), encoding="utf-8")
    write("a.json", ["ocean freight"], "one")
    write("b.json", ["ocean freight", "seabright   line"], "two")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert [d["name"] for d in read_quote_recorded.recordings(tmp_path)] == ["a.json", "b.json"]
    assert read_quote_recorded.find("OCEAN  FREIGHT usd 5\nwith Seabright Line", tmp_path)["answer"]["carrier"] == "two"
    assert read_quote_recorded.find("Ocean freight only", tmp_path)["answer"]["carrier"] == "one"
    assert read_quote_recorded.find("air freight", tmp_path) is None
