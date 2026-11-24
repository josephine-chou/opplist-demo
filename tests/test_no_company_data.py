"""No string from the forbidden list may appear in anything the repo publishes.

The list is kept outside the repository (it consists of exactly the strings
that must not be published), next to the project folder as
../forbidden.txt, or wherever OPPLIST_FORBIDDEN points. Without it the test
is skipped - on CI, for instance.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from opplist import leakcheck, paths


def _list_path():
    named = os.environ.get("OPPLIST_FORBIDDEN")
    if named:
        return Path(named)
    return paths.PROJECT_DIR.parent / "forbidden.txt"


LIST = _list_path()


@pytest.mark.skipif(not LIST.exists(), reason="no forbidden list next to the repo")
def test_no_forbidden_string_in_any_tracked_file():
    hits, forbidden, scanned = leakcheck.run(LIST, paths.PROJECT_DIR)
    assert forbidden.size > 0 and scanned > 0
    shown = "\n".join("{}:{}: [{}] {}".format(h.path, h.line, h.term, h.text) for h in hits[:40])
    assert not hits, "{} forbidden string(s) found:\n{}".format(len(hits), shown)


def test_matching_rules(tmp_path):
    listed = tmp_path / "list.txt"
    listed.write_text("# comment\nAcme Shipping\nBob\ntel:+1 (555) 010-7788\n", encoding="utf-8")
    forbidden = leakcheck.load(listed)
    assert forbidden.terms == ["acme shipping", "bob"] and forbidden.phones == {"15550107788"}
    found = leakcheck.scan_text('x = "ACME SHIPPING Ltd"\nbobcat\nhi Bob\ncall 1-555-010-7788\n', forbidden)
    assert [(line, term) for line, term, _ in found] == [
        (1, "acme shipping"), (3, "bob"), (4, "tel:15550107788")]


def test_a_number_is_a_hit_unless_a_digit_stands_before_or_after_it(tmp_path):
    """A listed number is found after letters and after punctuation, and it
    is not found as part of a longer number (a time stamp, a longer id)."""
    listed = tmp_path / "list.txt"
    listed.write_text("482913\n", encoding="utf-8")
    forbidden = leakcheck.load(listed)
    lines = ["id 482913 here",                          # 1 hit: on its own
             "<ab12.20260806482913@mail.x.example>",    # 2 no: digits before it
             "MFN482913",                               # 3 hit: letters before it
             "ID:482913",                               # 4 hit: punctuation before it
             "A482913B",                                # 5 hit: letters on both sides
             "no. 482913.",                             # 6 hit: a full stop after it
             "4829130",                                 # 7 no: a digit after it
             "1482913",                                 # 8 no: a digit before it
             "ref_482913-x",                            # 9 hit: underscore and hyphen
             "E08D5AB2386C1E0F482913E61EB016FF",        # 10 no: inside a generated hex id
             "AB482913CDEF012"]                         # 11 hit: 15 hex characters are no id yet
    found = leakcheck.scan_text("\n".join(lines) + "\n", forbidden)
    assert [(line, term) for line, term, _ in found] == [
        (1, "482913"), (3, "482913"), (4, "482913"), (5, "482913"), (6, "482913"), (9, "482913"),
        (11, "482913")]


def test_file_names_and_json_escapes_are_scanned(tmp_path):
    listed = tmp_path / "list.txt"
    listed.write_text("Zürich Freight\nsecretco\n", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "secretco_mail.json").write_text('{"a": "Z\\u00fcrich Freight"}', encoding="utf-8")
    hits = leakcheck.scan_files(leakcheck.all_files(root), leakcheck.load(listed), root)
    assert {(h.line == 0, h.term) for h in hits} == {(True, "secretco"), (False, "zürich freight")}
