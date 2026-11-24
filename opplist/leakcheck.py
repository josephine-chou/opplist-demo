"""Leak check: no string from a forbidden list may appear in the repository.

The list itself lives outside the repository (it is made of the very strings
that must not be published) and is never committed. Its format:

    one term per line; lines starting with '#' are comments;
    'tel:<digits>' is a phone number, compared on its digits only.

Matching is case-insensitive. A single word of 6 or more characters, or a
term that contains '@', '.', '/' or a backslash, matches anywhere (inside a
word, a URL, a JSON string). Anything else - a short word, a name of several
words, a number of any length - matches only where it is not glued to more letters or digits, so a
short first name does not hit every identifier that contains it and
'Blue Logistics' does not hit 'True Blue Logistics'.
"""

from __future__ import annotations

import re
import hashlib
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Text files are read as bytes and decoded leniently; a SQLite database is
#: read through sqlite3, every text value of every table (sqlite_text).
TEXT_LIKE = {".py", ".sql", ".html", ".css", ".js", ".json", ".csv", ".txt", ".md",
             ".cmd", ".sh", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".db", ".log",
             ".gitignore", ""}
#: Never scanned: images and font files carry no text a reader would see.
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".xlsx", ".pyc", ".ttf"}
#: Third-party files shipped as published, by path and SHA-256. Such a file
#: names its own authors, and a first name among them can equal a forbidden
#: one; it is not ours to edit. It is skipped only while its bytes are the
#: pinned ones: a changed file is scanned like any other.
#: jsPDF 4.2.1: compared with dist/jspdf.umd.min.js of the npm package
#: jspdf@4.2.1, the file is the same byte for byte except its last line,
#: the sourceMappingURL comment, which this copy does not have.
THIRD_PARTY = {
    "site/vendor/jspdf-4.2.1.umd.min.js": "97cff153c9765f25b5bafb6938f33239fb98f3211759e1aa56ff9a5186c81649",
}


def is_pinned_third_party(path: Path, root: Path) -> bool:
    try:
        rel = path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return False
    want = THIRD_PARTY.get(rel)
    if want is None:
        return False
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == want
    except OSError:
        return False
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}

_PHONE = re.compile(r"(?:\+|\(|\b)\d[\d ().\-]{7,}\d")


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    term: str
    text: str


@dataclass
class Forbidden:
    terms: list          # casefolded plain terms
    phones: set          # digit strings

    @property
    def size(self) -> int:
        return len(self.terms) + len(self.phones)


def load(list_path: str | Path) -> Forbidden:
    terms, phones = [], set()
    for raw in Path(list_path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("tel:"):
            digits = re.sub(r"\D", "", line[4:])
            if digits:
                phones.add(digits)
        else:
            terms.append(line.casefold())
    return Forbidden(sorted(set(terms)), phones)


def _substring_term(term: str) -> bool:
    if any(ch in term for ch in "@./\\"):
        return True
    return len(term) >= 6 and " " not in term


def _compile(forbidden: Forbidden):
    """One regex for the substring terms, one for the whole-word terms, one
    for the numbers.

    A number (a membership id, a reference) is that number wherever no digit
    stands before or after it: 'ID:482913' and 'MFN482913' are hits, the same
    six digits in the middle of a time stamp are not."""
    numbers = [t for t in forbidden.terms if t.isdigit()]
    subs = [t for t in forbidden.terms if not t.isdigit() and _substring_term(t)]
    words = [t for t in forbidden.terms if not t.isdigit() and not _substring_term(t)]
    # longest first so the reported term is the most specific one
    for group in (numbers, subs, words):
        group.sort(key=len, reverse=True)
    sub_re = re.compile("|".join(re.escape(t) for t in subs)) if subs else None
    word_re = (re.compile(r"(?<![0-9a-z])(?:{})(?![0-9a-z])".format(
        "|".join(re.escape(t) for t in words))) if words else None)
    number_re = (re.compile(r"(?<![0-9])(?:{})(?![0-9])".format("|".join(numbers))) if numbers else None)
    return sub_re, word_re, number_re


#: A run of this many hexadecimal characters is a generated id (a hash, a
#: mail's entry id), and a few digits in it are not a number anyone wrote.
HEX_ID_LEN = 16
_HEX = set("0123456789abcdef")


def _inside_hex_id(folded: str, start: int, end: int) -> bool:
    """True when the digits at [start, end) are part of an unbroken run of
    HEX_ID_LEN or more hexadecimal characters that has a letter a-f in it
    (a run of digits only never reaches here: the number rule already
    refuses a digit on either side)."""
    left = start
    while left > 0 and folded[left - 1] in _HEX:
        left -= 1
    right = end
    while right < len(folded) and folded[right] in _HEX:
        right += 1
    return right - left >= HEX_ID_LEN


def scan_text(text: str, forbidden: Forbidden, compiled=None):
    """[(line number, term, line)] for one text."""
    patterns = compiled or _compile(forbidden)
    out = []
    number_re = patterns[2] if len(patterns) > 2 else None
    for no, line in enumerate(text.splitlines(), 1):
        folded = line.casefold()
        for pattern in patterns:
            if pattern is None:
                continue
            for m in pattern.finditer(folded):
                if pattern is number_re and _inside_hex_id(folded, m.start(), m.end()):
                    continue
                out.append((no, m.group(0), line.strip()[:160]))
        if forbidden.phones:
            for m in _PHONE.finditer(line):
                digits = re.sub(r"\D", "", m.group(0))
                if any(p in digits for p in forbidden.phones if len(p) >= 9):
                    out.append((no, "tel:" + digits, line.strip()[:160]))
    return out


def repo_files(root: str | Path) -> list:
    """What a commit would contain: tracked files plus untracked ones that
    .gitignore does not exclude. Outside a git work tree, every file."""
    root = Path(root)
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root, capture_output=True, check=True).stdout.decode("utf-8")
        return [root / p for p in listed.split("\0") if p and (root / p).is_file()]
    except (OSError, subprocess.CalledProcessError):
        return all_files(root)


def all_files(root: str | Path) -> list:
    root = Path(root)
    return [p for p in sorted(root.rglob("*"))
            if p.is_file() and not (set(p.relative_to(root).parts) & SKIP_DIRS)]


def _is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def sqlite_text(path: str | Path) -> str:
    """Every text value in a SQLite file, one per line: the schema, then each
    table's rows. The shadow tables of a full-text index are skipped - they
    hold binary token data derived from the indexed table, whose bytes spell
    short words by chance - and the virtual table itself is skipped because
    its text is the indexed table's text."""
    conn = sqlite3.connect("file:{}?mode=ro".format(Path(path).resolve().as_posix()), uri=True)
    parts = []
    try:
        virtual = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE 'CREATE VIRTUAL TABLE%'")]
        shadow = {"{}_{}".format(v, s) for v in virtual
                  for s in ("data", "idx", "docsize", "config", "content", "segments", "segdir", "stat")}
        for name, sql in conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name"):
            parts.append(sql or "")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in tables:
            if table in shadow or table in virtual:
                continue
            for row in conn.execute('SELECT * FROM "{}"'.format(table.replace('"', '""'))):
                parts.extend(v for v in row if isinstance(v, str))
    finally:
        conn.close()
    return "\n".join(parts)


def scan_files(files, forbidden: Forbidden, root: str | Path = ".") -> list:
    compiled = _compile(forbidden)
    root = Path(root)
    hits = []
    for path in files:
        path = Path(path)
        if path.suffix.lower() in SKIP_SUFFIXES or is_pinned_third_party(path, root):
            continue
        try:
            if _is_sqlite(path):
                text = sqlite_text(path)
            else:
                text = path.read_bytes().decode("utf-8", errors="replace")
        except (OSError, sqlite3.Error):
            continue
        # also catch names hidden in JSON unicode escapes
        if path.suffix.lower() == ".json" and "\\u" in text:
            try:
                text = text + "\n" + text.encode("latin-1", "backslashreplace").decode(
                    "unicode_escape", errors="replace")
            except (UnicodeError, ValueError):
                pass
        # and addresses or names inside URL-encoded links
        if "%2" in text or "%4" in text:
            from urllib.parse import unquote_plus
            text = text + "\n" + unquote_plus(text)
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        # a path name is part of what is published too
        for no, term, line in scan_text(rel, forbidden, compiled):
            hits.append(Hit(rel, 0, term, "(file name)"))
        for no, term, line in scan_text(text, forbidden, compiled):
            hits.append(Hit(rel, no, term, line))
    return hits


def run(list_path, root=".", every_file=False) -> tuple:
    """(hits, forbidden) for a folder: the repo's publishable files, or every
    file under `root` when every_file (a demo output folder)."""
    forbidden = load(list_path)
    files = all_files(root) if every_file else repo_files(root)
    return scan_files(files, forbidden, root), forbidden, len(files)
