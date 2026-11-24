"""The PowerShell scripts clean a subject and make
its thread key exactly as parsing.py does - case numbers taken out first -
for every subject in fixtures/subject_samples.txt (the six numbered ones are
invented). Skipped where Windows PowerShell or ..\\scripts is not there (the
portfolio copy, a machine without the pipeline)."""

from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess

import pytest

from conftest import FIXTURES
from opplist import paths

SCRIPTS = paths.COMPANY_DIR / "scripts"
WITH_CLEAN = ("Build-RateRequestBoard.ps1", "Sort-Inbox.ps1", "Sort-Inbox-v2.ps1")
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def _function(source: str, name: str) -> str:
    """The text of one PowerShell function, braces balanced."""
    start = source.index("function {} {{".format(name))
    depth, i = 0, source.index("{", start)
    while True:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1


def _expected():
    with (FIXTURES / "thread_keys_expected.csv").open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.mark.skipif(not POWERSHELL or not SCRIPTS.exists(), reason="Windows PowerShell or ..\\scripts not here")
@pytest.mark.parametrize("script", WITH_CLEAN)
def test_the_powershell_clean_subject_and_key_match_the_fixtures(script, tmp_path):
    source = (SCRIPTS / script).read_text(encoding="utf-8-sig")
    rows = _expected()
    (tmp_path / "subjects.json").write_text(json.dumps([r["subject"] for r in rows], ensure_ascii=False),
                                            encoding="utf-8")
    key_takes_subject = "param([string]$Subject)" in _function(source, "Get-ThreadKey")
    runner = "\n".join([
        _function(source, "Get-CleanSubject"), _function(source, "Get-ThreadKey"),
        "$subjects = Get-Content -Raw -Encoding UTF8 -LiteralPath '{}' | ConvertFrom-Json".format(tmp_path / "subjects.json"),
        "$out = foreach ($s in $subjects) {",
        "  $c = Get-CleanSubject $s",
        "  $k = Get-ThreadKey {}".format("$s" if key_takes_subject else "$c"),
        "  [pscustomobject]@{ clean = $c; key = $k }",
        "}",
        "$out | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 -LiteralPath '{}'".format(tmp_path / "out.json"),
    ])
    (tmp_path / "run.ps1").write_bytes(b"\xef\xbb\xbf" + runner.encode("utf-8"))
    done = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(tmp_path / "run.ps1")],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    got = json.loads((tmp_path / "out.json").read_text(encoding="utf-8-sig"))
    wrong = []
    for row, out in zip(rows, got):
        if re.search(r"[回答转轉]", row["subject"]):
            continue                       # the Chinese prefixes are the app's only
        key = row["thread_key"] if not key_takes_subject or len(row["thread_key"]) >= 12 else ""
        if (out["clean"], out["key"]) != (row["clean_subject"], key):
            wrong.append((row["subject"], out, row["clean_subject"], key))
    assert wrong == []
    assert len(got) == len(rows) == 57
