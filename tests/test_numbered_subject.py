"""Batch N2: a draft's subject is 'RE: [OPP-nnnn] X', one prefix only, and
stays so however many times it goes back and forth. The routes' own
subjects are checked in their tests (Acknowledge, After Register, the
forward, Follow-ups, Tracking, Ask for feedback). Invented subjects; cases
from OPP-6301.
"""

from __future__ import annotations

import pytest

from opplist import ack, parsing

X = "RATE REQUEST // OAKHAVEN - IQUIQUE // 1 X 40HQ"


@pytest.mark.parametrize("subject,expected", [
    (X, "RE: [OPP-6301] " + X),
    ("RE: " + X, "RE: [OPP-6301] " + X),
    ("[OPP-6301] RE: " + X, "RE: [OPP-6301] " + X),                     # the old form
    ("RE: [OPP-6301] RE: " + X, "RE: [OPP-6301] " + X),
    ("RE: [OPP-6301] RE: RE: " + X, "RE: [OPP-6301] " + X),
    ("RE: [OPP-6299] " + X, "RE: [OPP-6301] " + X),                    # another case's number
    ("回复：　RE: " + X, "RE: [OPP-6301] " + X),       # 回复： with a full-width colon and space
    ("答复: " + X, "RE: [OPP-6301] " + X),                     # 答复:
    ("FW: RE: " + X, "RE: [OPP-6301] " + X),
    ("AW: " + X, "RE: [OPP-6301] " + X),
    ("Res: rv: Fwd: " + X, "RE: [OPP-6301] " + X),
    ("RE: Cancelled: " + X, "RE: [OPP-6301] Cancelled: " + X),         # a calendar word stays
    ("RE:   rate   request  ", "RE: [OPP-6301] rate request"),
    ("", "RE: [OPP-6301]"),
    ("RE: [OPP-6301]", "RE: [OPP-6301]"),
])
def test_one_prefix_and_the_number_after_it(subject, expected):
    assert ack.numbered_subject(subject, "OPP-6301") == expected


def test_ten_round_trips_give_the_same_subject():
    subject = ack.numbered_subject("RE: " + X, "OPP-6302")
    for _ in range(10):
        assert ack.numbered_subject(subject, "OPP-6302") == subject           # our own draft again
        subject = ack.numbered_subject("RE: " + subject, "OPP-6302")         # their reply, then ours
    assert subject == "RE: [OPP-6302] " + X


def test_the_matching_still_reads_the_number_in_either_place():
    assert parsing.case_numbers(ack.numbered_subject(X, "OPP-6303")) == ["OPP-6303"]
    assert parsing.case_numbers("[OPP-6303] RE: " + X) == ["OPP-6303"]
