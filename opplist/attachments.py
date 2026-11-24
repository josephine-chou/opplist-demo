"""Reading a mail's attachments (batch 14).

Register from Outlook has Outlook save a copy of each attachment into a
temporary folder of the app's own (outlook_bridge.read_attachments), reads
the copies here and deletes them afterwards; the mailbox is never changed.

What is read: .xlsx / .xlsm (openpyxl), .xls (xlrd), .docx (its XML, with
zipfile), a PDF with a text layer (pypdf), .csv and .txt as text, and an
attached mail (.msg, opened through Outlook) for its body and its own
attachments one level down. What is not: an image (skipped; an inline one
like image001.png is not even named), a scanned PDF with no text layer, an
encrypted or broken file, a file over MAX_BYTES, any other kind. Each file
comes back as a FileText, read or not, with the reason when not.

Nothing here touches Outlook or the database: the bridge hands over paths,
intake reads the text.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

#: Files bigger than this are not read (10 MB).
MAX_BYTES = 10 * 1024 * 1024
#: Below this many characters of text a PDF has no text layer worth the name.
MIN_PDF_TEXT = 20

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".emf", ".wmf",
             ".svg", ".webp", ".heic", ".ico"}
#: Outlook's names for pictures pasted into a mail body (signatures, logos).
INLINE_IMAGE = re.compile(r"^image\d{3}\.(png|jpe?g|gif|bmp)$", re.IGNORECASE)
TEXT_EXT = {".txt"}
SHEET_EXT = {".xlsx", ".xlsm"}
READABLE = SHEET_EXT | {".xls", ".docx", ".pdf", ".csv", ".txt", ".msg"}

NOT_READ_IMAGE = "an image - not read"
NOT_READ_SIZE = "over 10 MB - not read"
NOT_READ_SCAN = "a scanned PDF with no text layer - not read"
NOT_READ_LOCKED = "encrypted - not read"
NOT_READ_BROKEN = "could not be opened ({}) - not read"
NOT_READ_KIND = "a {} file - not a kind the reader knows"
NOT_READ_DEEP = "an attachment of an attachment of an attachment - one level down only"
NOT_READ_EMPTY = "no text in it"


@dataclass
class FileText:
    """One attachment: its name as the mail lists it, the text read (one
    line per entry), or why it was not read."""

    name: str
    lines: list = field(default_factory=list)
    read: bool = False
    reason: str = ""
    #: for an attached mail: the files attached to it, one level down
    children: list = field(default_factory=list)
    #: a sheet with a header row: each data row as its own lines,
    #: 'Header: value' one per line, so the label readers read each row as
    #: one consignment (the display keeps one line per row)
    records: list = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def is_image(name: str) -> bool:
    return Path(name or "").suffix.lower() in IMAGE_EXT


def is_inline_image(name: str) -> bool:
    return bool(INLINE_IMAGE.match(name or ""))


def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).split())


def sheet_parts(rows) -> tuple:
    """(lines, records) for a sheet. A first row of labels (two or more
    text cells, no number) over rows of values is a header: each later row
    is one line, 'Header: value; Header: value', and one record - the same
    pairs one per line, so the label readers work on it. Without a header,
    each row is its cells joined with two spaces and there are no records."""
    rows = [[_clean(c) for c in row] for row in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return [], []
    head = rows[0]
    filled = [c for c in head if c]
    is_header = (len(rows) > 1 and len(filled) >= 2
                 and all(not re.fullmatch(r"[\d.,\s%$-]+", c) for c in filled))
    if not is_header:
        return ["  ".join(c for c in r if c) for r in rows], []
    lines, records = [], []
    for row in rows[1:]:
        parts = ["{}: {}".format(h or "Column {}".format(i + 1), v)
                 for i, (h, v) in enumerate(zip(head + [""] * len(row), row)) if v]
        if parts:
            lines.append("; ".join(parts))
            records.append(parts)
    return lines, records


def rows_to_lines(rows) -> list:
    return sheet_parts(rows)[0]


def _sheets(named_rows, many) -> tuple:
    lines, records = [], []
    for title, rows in named_rows:
        got, recs = sheet_parts(rows)
        if got and many:
            lines.append("[{}]".format(title))
        lines.extend(got)
        records.extend(recs)
    return lines, records


def _xlsx(path) -> tuple:
    import openpyxl
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return _sheets(((s.title, s.iter_rows(values_only=True)) for s in book.worksheets),
                       len(book.worksheets) > 1)
    finally:
        book.close()


def _xls(path) -> tuple:
    import xlrd
    book = xlrd.open_workbook(path, on_demand=True)
    try:
        return _sheets(((s.name, (s.row_values(i) for i in range(s.nrows))) for s in book.sheets()),
                       book.nsheets > 1)
    finally:
        book.release_resources()


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx(path) -> list:
    """Paragraphs in order; a table row is one line, two cells 'A: B',
    more cells joined with two spaces."""
    with zipfile.ZipFile(path) as zf:
        root = ElementTree.fromstring(zf.read("word/document.xml"))
    body = root.find(_W + "body")
    out = []

    def para_text(p):
        return _clean("".join(t.text or "" for t in p.iter(_W + "t")))

    for child in (body if body is not None else []):
        if child.tag == _W + "p":
            text = para_text(child)
            if text:
                out.append(text)
        elif child.tag == _W + "tbl":
            for tr in child.iter(_W + "tr"):
                cells = [" ".join(filter(None, (para_text(p) for p in tc.iter(_W + "p"))))
                         for tc in tr.iter(_W + "tc")]
                cells = [c for c in cells if c]
                if len(cells) == 2:
                    out.append("{}: {}".format(cells[0].rstrip(" :"), cells[1]))
                elif cells:
                    out.append("  ".join(cells))
    return out


class _Locked(Exception):
    pass


class _NoText(Exception):
    pass


def _pdf(path) -> list:
    import pypdf
    reader = pypdf.PdfReader(str(path))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise _Locked()
        except _Locked:
            raise
        except Exception:
            raise _Locked()
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    if len("".join(text.split())) < MIN_PDF_TEXT:
        raise _NoText()
    return [_clean(line) for line in text.splitlines() if _clean(line)]


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _csv(path) -> list:
    text = _decode(Path(path).read_bytes())
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return sheet_parts(csv.reader(io.StringIO(text), dialect))


def _txt(path) -> list:
    return [_clean(l) for l in _decode(Path(path).read_bytes()).splitlines() if _clean(l)]


READERS = {".xlsx": _xlsx, ".xlsm": _xlsx, ".xls": _xls, ".docx": _docx, ".pdf": _pdf,
           ".csv": _csv, ".txt": _txt}


def read_file(path, name: str, size: int | None = None) -> FileText:
    """One saved attachment as a FileText. A .msg is the bridge's to open
    (it needs Outlook); here it is only refused by size."""
    out = FileText(name=name)
    ext = Path(name or str(path)).suffix.lower()
    if is_image(name):
        out.reason = NOT_READ_IMAGE
        return out
    if size is None:
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = 0
    if size > MAX_BYTES:
        out.reason = NOT_READ_SIZE
        return out
    reader = READERS.get(ext)
    if reader is None:
        out.reason = NOT_READ_KIND.format(ext or "nameless")
        return out
    try:
        got = reader(path)
        out.lines, out.records = got if isinstance(got, tuple) else (got, [])
    except _Locked:
        out.reason = NOT_READ_LOCKED
        return out
    except _NoText:
        out.reason = NOT_READ_SCAN
        return out
    except Exception as exc:                       # a broken file is a message, never a crash
        text = str(exc) or exc.__class__.__name__
        if "encrypt" in text.lower() or "password" in text.lower():
            out.reason = NOT_READ_LOCKED
        else:
            out.reason = NOT_READ_BROKEN.format(text[:80])
        return out
    if not out.lines:
        out.reason = NOT_READ_EMPTY
        return out
    out.read = True
    return out


def every_file(files) -> list:
    """The files and, under an attached mail, its own files - flat, in order."""
    out = []
    for f in files:
        out.append(f)
        out.extend(f.children)
    return out


# ------------------------------------------------- SDS, section 14 ------
#: 'Section 14. Transport information', '14. TRANSPORT INFORMATION',
#: 'SECTION 14: Transport information'
_SDS_14 = re.compile(r"^\W{0,3}(?:section\s*)?14\s*[.:)\-]?\s*transport\s+information", re.IGNORECASE)
_SDS_15 = re.compile(r"^\W{0,3}(?:section\s*)?15\s*[.:)\-]?\s*[A-Za-z]", re.IGNORECASE)
_MODES = r"(?:IMO/IMDG|IMDG|IATA(?:\s*\(Cargo\))?|ICAO|DOT|ADR|RID|ADN|TDG|IMO|UN/NA|US DOT)"
#: the table form: 'IMO/IMDG 3082 ENVIRONMENTALLY HAZARDOUS SUBSTANCE,
#: LIQUID, N.O.S. (LIQUID EPOXY RESIN) Class 9 III'
_SDS_ROW = re.compile(
    r"(?:\b" + _MODES + r"\b[\s:]*|\bUN\s?-?)(?P<un>\d{4})\b[\s,:-]*(?P<name>[A-Z][A-Za-z0-9 ,.()\-/'’]{3,}?)"
    r"\s*,?\s*(?:Class\s*:?\s*)?(?P<cls>[1-9](?:\.\d)?)\s*(?:\(\s*[^)]*\))?\s*,?\s*(?:PG\s*:?\s*)?(?P<pg>III|II|I)\b")
_SDS_UN = re.compile(r"\bUN[\s-]*(?:number|No\.?|#)?\s*[:.]?\s*(?:UN\s*-?)?(?P<un>\d{4})\b", re.IGNORECASE)
_SDS_NAME = re.compile(r"proper\s+shipping\s+name\s*[:.]?\s*(?P<name>[^:]{3,120}?)(?=\s+(?:UN\s+)?(?:transport\s+)?"
                       r"hazard\s+class|\s+class\b|\s+packing\s+group|\s+environmental|\s+marine|$)", re.IGNORECASE)
_SDS_CLASS = re.compile(r"(?:hazard\s+)?class(?:\(es\))?\s*[:.]?\s*(?P<cls>[1-9](?:\.\d)?)\b", re.IGNORECASE)
_SDS_PG = re.compile(r"packing\s+group\s*[:.]?\s*(?P<pg>III|II|I)\b", re.IGNORECASE)
_SDS_NOT = re.compile(r"\bnot\s+(?:regulated|controlled|restricted|dangerous|classified)\b|\bnon[\s-]?regulated\b",
                      re.IGNORECASE)


@dataclass
class DangerousGood:
    un: str
    name: str = ""
    cls: str = ""
    pg: str = ""
    lines: list = field(default_factory=list)     # the section's lines it was read from

    def text(self, source: str = "") -> str:
        """'UN1993, Class 3, PG III (from SDS.pdf)'."""
        parts = ["UN" + self.un] + (["Class " + self.cls] if self.cls else []) + \
                (["PG " + self.pg] if self.pg else [])
        return ", ".join(parts) + (" (from {})".format(source) if source else "")


def sds_section_14(lines) -> list:
    """The lines of an SDS's section 14 (Transport information), or []."""
    start = next((i for i, l in enumerate(lines) if _SDS_14.match(l)), None)
    if start is None:
        return []
    out = []
    for line in lines[start:]:
        if out and _SDS_15.match(line):
            break
        out.append(line)
    return out


def dangerous_goods(lines) -> list:
    """[DangerousGood] off an SDS's section 14: the UN number, the proper
    shipping name, the class and the packing group, once per UN number
    (the same product is listed for every mode of transport). A section
    that only says 'not regulated' gives none."""
    section = sds_section_14(lines)
    if not section:
        return []
    joined = " ".join(" ".join(section).split())
    found = {}
    for m in _SDS_ROW.finditer(joined):
        un = m.group("un")
        if un not in found:
            found[un] = DangerousGood(un=un, name=m.group("name").strip(" ,"), cls=m.group("cls"),
                                      pg=m.group("pg"), lines=section)
    if not found:
        for m in _SDS_UN.finditer(joined):
            if m.group("un") not in found:
                found[m.group("un")] = DangerousGood(un=m.group("un"), lines=section)
        if len(found) == 1:
            good = next(iter(found.values()))
            name, cls, pg = _SDS_NAME.search(joined), _SDS_CLASS.search(joined), _SDS_PG.search(joined)
            good.name = name.group("name").strip(" ,") if name else ""
            good.cls = cls.group("cls") if cls else ""
            good.pg = pg.group("pg") if pg else ""
    return list(found.values())
