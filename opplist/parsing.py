r"""Subject and quantity parsing.

Everything here is a *suggestion*. Parsed values are pre-filled into the form
and only stored once the user confirms them. The one
exception is migration, which fills structured quantity columns from the
already-recorded raw text and says so in system_notes.

The clean-subject and thread-key rules are a direct port of Get-CleanSubject
and Get-ThreadKey in scripts\Build-RateRequestBoard.ps1. They must produce
byte-identical keys, otherwise the mail pipeline and the app disagree about
which thread belongs to which case.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ------------------------------------------------------------ case numbers --
#: Where a case number is read from a mail (AL1, 2026-10-01): the
#: folder it is filed in, and its subject. One place for both.
#:
#: Case mail is filed under a folder named after the case: 'OPP-1287 (PET
#: Flakes)' or just '-0327 (Industrial Fans and Parts)'.
CASE_FOLDER = re.compile(r"(?:OPP)?\s*-\s*0*(\d{3,4})\b", re.IGNORECASE)

#: Since October Iris acknowledges a case with its number in the
#: subject - '[OPP-1504] ' in front, or right after 'RE: ' - and the agent's
#: replies carry it. Read in any case, with spaces inside the brackets, a
#: space or nothing for the hyphen, and without brackets; never inside a
#: longer word ('OPPORTUNITY', 'XOPP-1504'). Whether the case exists is the
#: matcher's question (case_numbers), not the parser's.
_CASE_NUMBER = re.compile(r"(?<![A-Za-z0-9])OPP[ \-]?(\d{3,4})(?![A-Za-z0-9])", re.IGNORECASE)
_CASE_NUMBER_TAG = re.compile(
    r"\[\s*OPP[ \-]?\d{3,4}\s*\]|(?<![A-Za-z0-9])OPP[ \-]?\d{3,4}(?![A-Za-z0-9])", re.IGNORECASE)


def case_numbers(subject: str | None) -> list:
    """['OPP-1504', ...] - the case numbers a subject carries, in order,
    each once."""
    out = []
    for found in _CASE_NUMBER.finditer(subject or ""):
        number = "OPP-{:04d}".format(int(found.group(1)))
        if number not in out:
            out.append(number)
    return out


def strip_case_numbers(subject: str | None) -> str:
    """The subject without its case numbers (and their brackets), so that
    'RE: [OPP-1504] X', '[OPP-1504] RE: X' and 'X' clean to the same."""
    return _CASE_NUMBER_TAG.sub(" ", subject or "")


# ----------------------------------------------------------- clean subject --
#: The reply and forward prefixes a mail client puts in front of a subject,
#: in the languages the mailbox sees. The Chinese ones (2026-09-23) are what
#: the Shanghai office's client writes - 回复： in front of RE: in front of
#: 回复： - with a full-width colon and full-width spaces (U+3000), so both
#: widths are accepted for the colon and the spaces around it.
_REPLY_PREFIX = re.compile(
    r"^[\s　]*(RE|RES|RV|FW|FWD|ENC|ANTW|AW|TR|CANCELAR|CANCELADA?|CANCELLED|CANCELED|RECALL"
    r"|回复|回覆|答复|答覆|转发|轉發|轉寄|回信)"
    r"[\s　]*[:：][\s　]*",
    re.IGNORECASE,
)

# A leading reference looks like QTNRV311-0826 or QX41/72640: at least five
# characters and containing a digit, so that a real word such as MIAMI is
# never mistaken for one.
_LEADING_REF = re.compile(
    r"^\s*(?=[A-Za-z0-9/.\-]*[0-9])[A-Za-z0-9][A-Za-z0-9/.\-]{4,}\s*(//|[-–—:])\s*"
)

_TRAILING_REF = re.compile(
    r"\s*//\s*(?=[A-Za-z0-9/.\-]*[0-9])[A-Za-z0-9][A-Za-z0-9/.\-]{4,}\s*$"
)

_MAX_PASSES = 8


#: The Message-ID a detected reply leaves in its note ('[mid: <...>]') is
#: for finding the mail again, not for reading, and it cannot wrap. The
#: note in the database keeps it; a page or a report shows the words only
#: (2026-09-21; moved here from the web layer for the month report).
_MID_TAG = re.compile(r"\s*\[mid:\s*<[^>]*>\]")


def hide_mid(text) -> str:
    return _MID_TAG.sub("", text or "").strip()


def has_reply_prefix(subject: str | None) -> bool:
    """True for 'RE:', 'RES:', 'FW:' and the rest of the reply / forward prefix list.

    A mail with one of these is an answer inside a conversation somebody else
    may have started; a mail without one was written from scratch.
    """
    return bool(_REPLY_PREFIX.match(subject or ""))


#: MF1: a letter of any script ('061' has none, 'MIAMI' and '上海' do)
_LETTER = re.compile(r"[^\W\d_]")


def has_letter(text) -> bool:
    return bool(_LETTER.search(text or ""))


#: TK2 (2026-11-13): the words a subject says about any request. A leading
#: reference followed by these alone stays ('KLX-552018 - IMPORT AIR': KLX
#: writes every import air request so, and IMPORTAIR alone joined them all).
#: Compared without case or accents.
COMMON_SUBJECT_WORDS = frozenset((
    "IMPORT", "EXPORT", "AIR", "AIRFREIGHT", "OCEAN", "SEA", "SEAFREIGHT", "LCL", "FCL", "FTL", "LTL", "TRUCK",
    "TRUCKING", "QUOTE", "QUOTES", "QUOTATION", "RATE", "RATES", "REQUEST", "INQUIRY", "ENQUIRY", "PLEASE", "PLS",
    "NEW", "SHIPMENT", "CARGO", "FREIGHT", "URGENT", "RFQ", "PRICE", "PRICING",
    "COTIZACION", "COTACAO", "SOLICITUD", "PEDIDO", "IMPORTACION", "EXPORTACION", "AEREO", "MARITIMO", "FLETE",
    "FRETE",
    # D7b (2026-11-13)
    "AEREA", "MARITIMA", "IMPORTACAO", "EXPORTACAO", "COTIZACIONES"))


def _plain_upper(word: str) -> str:
    return unicodedata.normalize("NFKD", word).encode("ascii", "ignore").decode("ascii").upper()


def only_common_words(text) -> bool:
    """TK2: the text has words and every one is a common subject word."""
    words = [w for w in re.split(r"[^\w]+|_", text or "") if w]
    return bool(words) and all(_plain_upper(w) in COMMON_SUBJECT_WORDS for w in words)


def clean_subject(subject: str | None, keep_bare_ref: bool = True, keep_common_ref: bool = True) -> str:
    """Strip case numbers, reply prefixes and reference numbers from a mail
    subject (AL1: the case numbers first, then as before).

    MF1: a leading reference whose removal would leave no letter - a bare
    number, or nothing - stays: 'RE: BRN-QT-207' cleans to 'BRN-QT-207',
    not '061'. TK2: a leading reference whose removal would leave common
    words only (COMMON_SUBJECT_WORDS) stays too: 'KLX-552018 - IMPORT AIR'
    keys KLX552018IMPORTAIR, not IMPORTAIR. `keep_bare_ref=False` is the
    rule before MF1 and `keep_common_ref=False` the rule before TK2, for the
    rekey-threads command to tell which stored keys they made."""
    if not subject:
        return ""
    text = strip_case_numbers(subject)
    for _ in range(_MAX_PASSES):
        before = text
        text = _REPLY_PREFIX.sub("", text)
        stripped = _LEADING_REF.sub("", text)
        bare = keep_bare_ref and not has_letter(stripped)
        common = keep_common_ref and stripped != text and only_common_words(stripped)
        if not bare and not common:
            text = stripped
        if text == before:
            break
    text = _TRAILING_REF.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def thread_key(clean: str | None) -> str:
    """Upper-case the clean subject and drop everything that is not A-Z0-9."""
    return re.sub(r"[^A-Z0-9]", "", (clean or "").upper())


def subject_thread_key(subject: str | None, keep_bare_ref: bool = True, keep_common_ref: bool = True) -> str:
    return thread_key(clean_subject(subject, keep_bare_ref, keep_common_ref))


# ---------------------------------------------------------- quantity text --
#: Normalised container types, matching setting 'container_types'.
_SUFFIX_MAP = {"HC": "HQ", "": None, None: None}


def normalise_container(size: str, suffix: str | None) -> str:
    """'40' + 'HC' -> '40HQ'. A bare size gets the default box for that size."""
    suffix = (suffix or "").upper()
    if suffix == "HC":
        suffix = "HQ"
    if not suffix:
        suffix = "HQ" if size == "45" else "GP"
    return f"{size}{suffix}"


#: CL1: the container types the setting container_types holds by default
CONTAINER_TYPES = ("20GP", "40GP", "40HQ", "45HQ", "20OT", "40OT", "20FR", "40FR", "20RF", "40RF", "40NOR")
#: the words Claude (and people) write for the standard box
_STANDARD_BOX = {"ST": "GP", "STD": "GP", "DRY": "GP", "DV": "GP", "FT": "GP", "GP": "GP"}
_CONTAINER_TEXT = re.compile(r"^(\d\d)(?:FT)?(GP|HQ|HC|OT|FR|RF|NOR|STD|ST|DRY|DV|FT)?$")


def container_type_of(text, allowed=CONTAINER_TYPES) -> str:
    """CL1: a container type as written ('40'HC', '20 STD', '40’HQ', '40'')
    read as the rule reader reads one: apostrophes and spaces ignored, the
    standard box's words as GP, HC as HQ, a bare size its default box; ''
    when it cannot be read or is not one of `allowed`."""
    raw = re.sub(r"[\s'\u2019\u2018`\u00b4\"]", "", str(text or "")).upper()
    found = _CONTAINER_TEXT.match(raw)
    if not found:
        return ""
    box = _STANDARD_BOX.get(found.group(2) or "", found.group(2))
    kind = normalise_container(found.group(1), box)
    return kind if kind in set(allowed or CONTAINER_TYPES) else ""


_MODE_PREFIX = re.compile(r"(FCL|AIR|LCL)\s*:", re.IGNORECASE)
_CONTAINER_GROUP = re.compile(
    r"(\d+)\s*[Xx]\s*(\d\d)\s*'?\s*(GP|HQ|HC|OT|FR|RF|NOR)?", re.IGNORECASE
)
_BARE_SIZE = re.compile(r"(?<!\d)(\d\d)\s*'?\s*(GP|HQ|HC|OT|FR|RF|NOR)?(?!\d)", re.IGNORECASE)
# The whole segment must be the number, optionally with its unit. 'Air:200'
# and 'LCL:2 CBM' are readable; 'Air:1-1.6 TONS' is a range in the wrong unit
# and filling 1 kg from it would be worse than filling nothing.
_NUMBER = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:KGS?|KILOS?|CBM|M3)?$", re.IGNORECASE)

#: Words inside an FCL segment that mean 'this is not really an FCL booking'.
_FCL_MODE_WORDS = {"RORO": "RORO", "RO-RO": "RORO", "PROJECT": "Project"}

_MODE_CANON = {"FCL": "FCL", "AIR": "Air", "LCL": "LCL"}


@dataclass
class QtyParse:
    """What can be suggested from a quantity string."""

    mode_hint: str | None = None
    container_type: str | None = None
    container_qty: int | None = None
    air_weight_kg: float | None = None
    lcl_cbm: float | None = None
    notes: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            (
                self.container_type,
                self.container_qty,
                self.air_weight_kg,
                self.lcl_cbm,
            )
        )


def _segments(raw: str) -> list[tuple[str, str]]:
    """Split 'Air:200 | LCL:1.5' into [('Air', '200 | '), ('LCL', '1.5')]."""
    matches = list(_MODE_PREFIX.finditer(raw))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        out.append((_MODE_CANON[m.group(1).upper()], raw[m.end() : end]))
    return out


def _to_number(text: str) -> float | None:
    m = _NUMBER.match(text.strip().strip("|").strip())
    return float(m.group(1)) if m else None


def parse_qty(raw: str | None) -> QtyParse:
    """Parse 'FCL:3 x 40'HQ', 'Air:216', 'LCL:6.6', 'Air:200 | LCL:1.5'."""
    result = QtyParse()
    if not raw or not raw.strip():
        return result

    segments = _segments(raw)
    if not segments:
        return result

    modes = []
    for mode, body in segments:
        if mode not in modes:
            modes.append(mode)
        if mode == "Air":
            result.air_weight_kg = _to_number(body)
        elif mode == "LCL":
            result.lcl_cbm = _to_number(body)
        else:
            _parse_fcl(body, result)

    if len(modes) > 1:
        result.mode_hint = "Multi-mode"
    elif result.mode_hint is None:
        result.mode_hint = modes[0]
    return result


def _parse_fcl(body: str, result: QtyParse) -> None:
    upper = body.upper()
    for word, mode in _FCL_MODE_WORDS.items():
        if word in upper:
            result.mode_hint = mode
            result.notes.append(f"qty text says {word}")
            return

    groups = _CONTAINER_GROUP.findall(body)
    if len(groups) == 1:
        qty, size, suffix = groups[0]
        result.container_type = normalise_container(size, suffix)
        result.container_qty = int(qty)
        return
    if len(groups) > 1:
        result.container_type = "Mixed"
        result.container_qty = sum(int(g[0]) for g in groups)
        return

    bare = _BARE_SIZE.search(body)
    if bare:
        result.container_type = normalise_container(bare.group(1), bare.group(2))


# ----------------------------------------------------- subject candidates --
# The clean subject is cut on '//' and each piece is classified. A piece can
# hit several types and every hit becomes a candidate: nothing here is ever
# chosen automatically, the form pre-fills and the user confirms.

MODE_KEYWORDS = [
    # SA1: a real sea-air move, ahead of AIR (and of the Multi-mode a
    # quantity in two modes suggests); 'SEA & AIR' asks to quote both
    # and is none of these
    ("SEA-AIR", "Sea-Air"),
    ("SEA/AIR", "Sea-Air"),
    ("SEA AIR", "Sea-Air"),
    ("SEAAIR", "Sea-Air"),
    # SA1: a truck that itself crosses a border; two countries alone say
    # nothing (an IPI leg has two countries too)
    ("CROSS-BORDER", "Cross-border Truck"),
    ("CROSS BORDER", "Cross-border Truck"),
    ("CROSSBORDER", "Cross-border Truck"),
    ("BREAK BULK", "Break Bulk"),
    ("TRUCK LOAD", "Truck"),            # TK1: the mode Truck (was Domestic)
    ("TRUCKING", "Truck"),
    ("DRAYAGE", "Truck"),
    ("PROJECT", "Project"),
    ("RORO", "RORO"),
    ("FCL", "FCL"),
    ("LCL", "LCL"),
    ("AIR", "Air"),
    ("FTL", "Truck"),
    ("LTL", "Truck"),
    ("OCEAN", None),          # a keyword, but not one of the six service modes
    # 'COTACAO DE FRETE MARITIMO' (13g): the Spanish / Portuguese sea word,
    # read like OCEAN - a sea shipment, the mode left for her to pick
    ("MARITIMO", None),
    ("MAR\u00cdTIMO", None),
    ("MARITIMA", None),
    ("MAR\u00cdTIMA", None),
]

#: The subject keywords above that say 'by sea' without naming the mode.
SEA_KEYWORDS = ("OCEAN", "MARITIMO", "MAR\u00cdTIMO", "MARITIMA", "MAR\u00cdTIMA")
#: The service modes that move by sea.
SEA_MODES = ("FCL", "LCL", "RORO", "Break Bulk", "Project")
#: SA1: the truck modes, which a mail that says IPI never gets.
TRUCK_MODES = ("Truck", "Cross-border Truck")
_IPI = re.compile(r"\bIPI\b", re.IGNORECASE)
_SEA_AIR = re.compile(r"\bsea\s?[-/]?\s?air\b", re.IGNORECASE)
_CROSS_BORDER = re.compile(r"\bcross\s?-?\s?border\b", re.IGNORECASE)


def says_ipi(*texts) -> str:
    """'IPI' as the first of the texts writes it, or '' (SA1, Victor's
    rule: a truck leg of an ocean or air move - IPI, a delivery to or from
    a port or an airport - takes the mode the cargo came by)."""
    for text in texts:
        found = _IPI.search(text or "")
        if found:
            return found.group(0)
    return ""
#: Sea words in a mail, English, Spanish or Portuguese (13g).
_SEA_WORDS = re.compile(
    r"\b(?:ocean(?:\s*freight)?|sea\s*freight|seafreight|by\s+sea|vessel|fcl|lcl|"
    r"mar[i\u00ed]tim[oa]|navio|buque)\b", re.IGNORECASE)


def sea_word(text) -> str:
    """The first sea word in the text, as written, or ''."""
    found = _SEA_WORDS.search(text or "")
    return found.group(0) if found else ""


def subject_sea_word(subject) -> str:
    """The subject's sea keyword ('OCEAN', 'MARITIMO'), or its sea mode, or ''."""
    for keyword, mode in subject.modes:
        if keyword in SEA_KEYWORDS or mode in SEA_MODES:
            return keyword
    return ""

INCOTERM_WORDS = ["EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP",
                  "DAP", "DPU", "DDP", "DDU"]
_EXW_SPELLINGS = ("EX WORKS", "EXWORKS", "EXWORK", "EX WORK")

# Keyword in the subject -> tag in setting 'special_handling', where one fits.
SPECIAL_KEYWORDS = [
    ("HAZMAT", "DG"),
    ("OPEN TOP", None),
    ("FLAT RACK", None),
    ("OVERWEIGHT", "Overweight"),
    ("REEFER", "Reefer"),
    ("OOG", "OOG"),
    ("DG", "DG"),
]

#: DD1 (2026-11-12): the cargo cannot be stacked - in the subject or on one
#: line of the text the cargo is read from (a hyphen at the end of a line
#: joins the next). Not 'stackable' alone, not 'stackable: yes', and never a
#: No on another line (a table: 'Hazardous: No' then 'Stackable: Yes').
#: FX5: non stack, non-stack, nonstack and non stacking too; never a question
#: (non_stackable_line).
NON_STACKABLE = re.compile(
    r"\bnon\s*-?\s*stack(?:able|ing)?\b"
    r"|\bnot\s+stackable\b"
    r"|\bunstackable\b"
    r"|\bno\s+stack(?:ing)?\b"
    r"|\bdo\s+not\s+stack\b"
    r"|\b(?:cannot|can\s*not|can['\u2019]t)\s+be\s+stacked\b"
    r"|\bstackable\s*(?:[:?|\-\u2013]\s*)+no\b"
    r"|\bno\s+(?:apilable|apilar|estibable|remontable)\b"
    r"|\bn[\u00e3a]o\s+empilh(?:[\u00e1a]vel|ar)\b",
    re.IGNORECASE)
NON_STACKABLE_TAG = "Non-stackable"


def joined_lines(lines) -> list:
    """The lines with a hyphen at the end of one joining the next
    ('non-' / 'stackable' is 'non-stackable')."""
    out = []
    for line in lines or []:
        line = " ".join(str(line or "").split())
        if out and out[-1].endswith("-"):
            out[-1] = out[-1] + line
        elif line:
            out.append(line)
    return out


#: FX5: a sentence ends at '.', '?' or '!' followed by a space, and at the end of the line
_SENTENCE_BREAK = re.compile(r"(?<=[.?!])\s+")
#: what may close a question after its '?': quotes and brackets
_CLOSERS = " \"'\u201d\u2019)]"


def sentences(line) -> list:
    """FX5: the (start, end) of each sentence of a line."""
    spans, start = [], 0
    for m in _SENTENCE_BREAK.finditer(line):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(line)))
    return spans


def non_stackable_line(lines) -> str:
    """DD1: the first sentence that says the cargo cannot be stacked, '' when
    none. FX5: never from a question - the words count only when the
    sentence they end in does not end with '?' ('Is this stackable ... for
    non-stackable?' is ours, asking; 'Stackable? No' is an answer, its words
    ending in 'No'). Returns the sentences the words run across."""
    for line in joined_lines(lines):
        spans = sentences(line)
        for m in NON_STACKABLE.finditer(line):
            first = next(i for i, (a, b) in enumerate(spans) if m.start() < b or i == len(spans) - 1)
            last = next(i for i, (a, b) in enumerate(spans) if m.end() <= b or i == len(spans) - 1)
            end = line[spans[last][0]:spans[last][1]].rstrip(_CLOSERS)
            if end.endswith("?"):
                continue
            return line[spans[first][0]:spans[last][1]]
    return ""


_LANE_SPLIT = re.compile(r"\s(?:[-–—]|->|\u2192)\s")
#: 'Miami port', 'Manila airport': the place is the word before.
_PORT_WORD = re.compile(r"\s+(?:sea\s*port|port|airport|apt|harbou?r)\s*$", re.IGNORECASE)
#: '1X20GP', '2x40HQ': a container count, never a reference number.
_CONTAINER_TOKEN = re.compile(r"^\d{1,3}[xX\u00d7]\d\d(?:GP|HQ|HC|OT|FR|RF|NOR)?'?$",
                              re.IGNORECASE)
_REFERENCE = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9/.\-]{4,})\b")
#: 'Sea Quote #582': a number after '#' is their reference however short.
_HASH_REF = re.compile(r"#\s*(\d{3,})\b")
_PLACE_CODE = re.compile(r"^[A-Z]{3}$")
_LOOKS_LIKE_REF = re.compile(r"^(?=[A-Za-z0-9/.\-]*\d)[A-Za-z0-9][A-Za-z0-9/.\-]{4,}$")
_END_CUSTOMER_SLASH = re.compile(r"(?:^|[\s(/])C\s*/\s*-?\s*([A-Za-z0-9][^/]*)",
                                 re.IGNORECASE)
_END_CUSTOMER_WORD = re.compile(r"\b(?:CNEE|CLIENTE)\s+([A-Za-z0-9][^/]*)",
                                re.IGNORECASE)
_REEFER_BOX = re.compile(r"(\d\d)\s*'?\s*REEFER", re.IGNORECASE)

#: Longest a lane endpoint may be before it is clearly a sentence, not a place.
MAX_PLACE_LEN = 40

# ------------------------------------------- countries in other spellings --
#: How the agents write a country, mapped to the name in the country table.
#: Spanish and Portuguese first (LATAM writes to us in both), then the usual
#: English abbreviations. Keys are compared with dots and spaces removed, so
#: 'EE.UU.', 'EE. UU.' and 'EEUU' are one entry.
COUNTRY_ALIASES = {
    "USA": ("EEUU", "EE.UU.", "USA", "U.S.A.", "US", "U.S.", "Estados Unidos",
            "Estados Unidos de America", "Estados Unidos de América",
            "United States", "United States of America", "Norteamerica",
            "Norteamérica"),
    "Brazil": ("Brasil",),
    "Mexico": ("México", "Mejico", "Méjico"),
    "Peru": ("Perú",),
    "Spain": ("España", "Espanha"),
    "Germany": ("Alemania", "Alemanha"),
    "France": ("Francia", "França"),
    "Italy": ("Italia", "Itália"),
    "UK": ("Reino Unido", "United Kingdom", "Great Britain", "Inglaterra", "England"),
    "Netherlands": ("Países Bajos", "Paises Bajos", "Holanda", "Países Baixos"),
    "Belgium": ("Bélgica", "Belgica"),
    "Switzerland": ("Suiza", "Suíça", "Suica"),
    "Japan": ("Japón", "Japon", "Japão"),
    "South Korea": ("Corea del Sur", "Corea", "Coreia do Sul", "Coreia", "Korea"),
    "China": ("Chine",),
    "Taiwan": ("Taiwán",),
    "India": ("Índia",),
    "Thailand": ("Tailandia", "Tailândia"),
    "Vietnam": ("Vietnã", "Viet Nam"),
    "Singapore": ("Singapur", "Singapura"),
    "Turkey": ("Turquía", "Turquia", "Türkiye"),
    "South Africa": ("Sudáfrica", "Sudafrica", "África do Sul"),
    "Australia": ("Austrália",),
    "New Zealand": ("Nueva Zelanda", "Nova Zelândia"),
    "UAE": ("Emiratos Árabes Unidos", "Emiratos Arabes Unidos",
            "Emirados Árabes Unidos", "United Arab Emirates"),
    "Canada": ("Canadá",),
    "Panama": ("Panamá",),
    "Dominican Republic": ("República Dominicana", "Republica Dominicana"),
    "Puerto Rico": ("Porto Rico",),
    "Colombia": ("Colômbia",),
    "Uruguay": ("Uruguai",),
    "Paraguay": ("Paraguai",),
    "Bolivia": ("Bolívia",),
    "Ecuador": ("Equador",),
    "Nicaragua": ("Nicarágua",),
    "Guatemala": (),
    "Honduras": (),
    "El Salvador": (),
    "Costa Rica": (),
    "Cuba": (),
    "Argentina": (),
    "Chile": (),
    "Venezuela": (),
    "Portugal": (),
}


def _country_token(text: str) -> str:
    return re.sub(r"[\s.]+", "", (text or "")).casefold()


_COUNTRY_BY_TOKEN = {}
for _name, _spellings in COUNTRY_ALIASES.items():
    _COUNTRY_BY_TOKEN.setdefault(_country_token(_name), _name)
    for _spelling in _spellings:
        _COUNTRY_BY_TOKEN.setdefault(_country_token(_spelling), _name)


def country_alias(text: str) -> str | None:
    """'EEUU' -> 'USA', 'Brasil' -> 'Brazil', 'Estados Unidos' -> 'USA';
    None when the text is not a country spelling this list knows."""
    return _COUNTRY_BY_TOKEN.get(_country_token(text))


@dataclass
class SubjectParse:
    """Everything the subject can suggest, all of it optional."""

    clean: str = ""
    thread_key: str = ""
    segments: list = field(default_factory=list)
    lanes: list = field(default_factory=list)        # [(origin, dest), ...]
    #: Lanes grouped by the segment they came from. Several lanes out of ONE
    #: segment is a genuine multi-lane request ('BKK / CNX - ATL'); lanes out
    #: of DIFFERENT segments are more likely one real lane and one false
    #: reading, so only the first group is offered ticked.
    lane_groups: list = field(default_factory=list)
    modes: list = field(default_factory=list)        # [(keyword, service_mode)]
    incoterms: list = field(default_factory=list)
    end_customers: list = field(default_factory=list)
    containers: list = field(default_factory=list)   # [(qty, type)]
    special: list = field(default_factory=list)      # [(keyword, tag)]
    references: list = field(default_factory=list)
    names: list = field(default_factory=list)        # party name candidates
    unknown_locations: list = field(default_factory=list)
    #: 'TEXAS- EEUU' with no lane anywhere: [(place, country, as written)],
    #: offered as the destination. Only when the subject names no lane.
    single_places: list = field(default_factory=list)
    #: 'SMRA': three to five capitals in a segment of their own that are not
    #: a location code - possibly the end customer's abbreviation.
    abbreviations: list = field(default_factory=list)
    #: batch 23: destinations written as alternatives in one segment ('MIA -
    #: SAI OR VAP', 'MIA - SAI / VAP' with both on file), upper case, in the
    #: order written: one route with all of them, not a route each.
    alternatives: list = field(default_factory=list)

    @property
    def service_mode(self):
        for _, mode in self.modes:
            if mode:
                return mode
        return None

    @property
    def incoterm(self):
        return self.incoterms[0] if self.incoterms else None

    @property
    def end_customer(self):
        return self.end_customers[0] if self.end_customers else None

    @property
    def special_tags(self) -> list:
        out = []
        for _, tag in self.special:
            if tag and tag not in out:
                out.append(tag)
        return out


#: Words that never appear in a place name. Without this, 'PICKUP REQUEST FOR
#: KESTREL - BOGOTA' and 'EC WCSA - Rate for QX77B31' read as lanes.
LANE_NOISE = {"RATE", "RATES", "REQUEST", "QUOTE", "QUOTATION", "FOR", "TO",
              "FROM", "PICKUP", "LOAD", "WEEKEND", "ADVICE", "SHIPMENT",
              "UPDATE", "NEW", "REVISE", "REF", "REF.", "ORDER",
              # Service scope, not places. 'DOOR - AIRPORT' and 'DOOR - PORT'
              # say how far we carry it, not between which two cities.
              "DOOR", "PORT", "AIRPORT", "WAREHOUSE", "RAMP", "TERMINAL",
              "DESTINATION", "ORIGIN",
              # Spanish / Portuguese request words: 'SOLICITUD TARIFA
              # ENVIO FLORIDA' is a request about Florida, not a place called
              # 'Envio Florida'.
              "SOLICITUD", "COTIZACIÓN", "COTIZACION", "COTIZAÇÃO",
              "COTIZACAO", "COTAÇÃO", "COTACAO", "DESPACHO", "ENVÍO", "ENVIO", "EMBARQUE", "CARGA",
              "TRANSPORTE", "FLETE", "FRETE", "TARIFA", "TARIFAS", "PEDIDO",
              "ORÇAMENTO", "ORCAMENTO", "CHARGES", "ORIGEN", "ORIGEM",
              "DESTINO", "SALIDA", "LLEGADA", "CHEGADA",
              # labels, never places
              "POL", "POD", "AOL", "AOD"}

#: Articles and prepositions that never start a multi-word place: 'DE
#: SANTIAGO' is 'from Santiago', not a two-word city. Alone, 'LA' can still
#: be Los Angeles, so this only applies inside longer candidates.
_LEADING_STOPWORDS = {"de", "del", "da", "do", "dos", "das", "la", "el", "los",
                      "las", "a", "para", "por", "en", "em", "al", "ao"}

#: A place is at most three words: 'PUERTO MONTT, CHILE', 'SPARTANBURG, SC 29301'.
MAX_PLACE_WORDS = 3

#: 'BKK / CNX - ATL' is a request for two lanes. A segment listing nine ports
#: is a rate sheet, not a request, and guessing lanes out of it is worse than
#: offering none: cap both sides and the product.
MAX_ENDPOINTS_PER_SIDE = 3
MAX_LANES_PER_SEGMENT = 6


def looks_like_place(text: str) -> bool:
    """A lane endpoint: not a reference number and not a whole sentence."""
    text = (text or "").strip().strip(".,")
    if not text or len(text) > MAX_PLACE_LEN or ":" in text:
        return False
    if " " not in text and _LOOKS_LIKE_REF.match(text):
        return False                      # ZQTU4471, RB8812K, K2057A
    words = text.split()
    if len(words) > MAX_PLACE_WORDS:
        return False
    if any(w.strip("(),.").upper() in LANE_NOISE for w in words):
        return False
    first = words[0].strip("(),.")
    if not first.replace("-", "").isalpha():
        return False
    if len(words) > 1 and first.casefold() in _LEADING_STOPWORDS:
        return False
    # Three letters is the usual port or airport code; two is a state or
    # country code, but only when it is written in capitals ('WY - SAI').
    return len(first) >= 3 or (len(first) == 2 and first.isupper())


def lane_place(text: str) -> str:
    """An endpoint as written, minus 'port' / 'airport' on the end."""
    return _PORT_WORD.sub("", (text or "").strip().strip(" .;:*")).strip()


def _endpoints(side: str, known=()) -> list:
    """'KUL / PEN' and 'SEA/TAC' both mean two places; batch 23: so does
    'SAN ANTONIO OR VALPARAISO'. 'PORTLAND, OR' is Oregon, not a choice:
    an 'or' right after a comma is a state."""
    parts = [lane_place(p) for p in re.split(r"\s*/\s*|" + _OR_SPLIT, side, flags=re.IGNORECASE) if p.strip()]
    # RG2a: a place on file is a place even with a lane word in it (PORT SAID)
    return [p for p in parts if looks_like_place(p) or p.upper() in known]


#: 'San Antonio or Valparaiso': any of them will do (batch 23). Not after
#: a comma: 'MEDFORD, OR' is the state of Oregon.
_OR_SPLIT = r"(?<!,)\s+or\s+"
_OR_WORD = re.compile(_OR_SPLIT, re.IGNORECASE)
_AND_WORD = re.compile(r"\s+(?:and|&)\s+", re.IGNORECASE)


def written_as_alternatives(text, places, known=()) -> bool:
    """Batch 23 (item 4): the places of one side written as alternatives -
    'X or Y' (any of them will do: one route with both places), or 'X / Y'
    when every one of them is a place on file (`known`, upper case). 'X and
    Y', 'X & Y', or 'X / Y' with a place not on file stay what they were:
    separate destinations, each a route of its own (13 item 17)."""
    if len(places or []) < 2:
        return False
    text = text or ""
    if _AND_WORD.search(text):
        return False
    if _OR_WORD.search(text):
        return True
    if "/" in text:
        wanted = {str(k).upper() for k in (known or ())}
        return all(str(p).upper() in wanted for p in places)
    return False


def _tight_lanes(segment: str, known) -> list:
    """'PUERTO MONTT-IAH' with no spaces round the dash: a lane only when
    both sides are places on file (a key or an alias), so that '27-QRT/04'
    and 'DOOR-TO-DOOR' stay what they are."""
    lanes = []
    if not known:
        return lanes
    for piece in re.split(r"\s*,\s*", segment or ""):
        piece = piece.strip()
        if piece.count("-") != 1 or " - " in piece:
            continue
        left, right = piece.split("-", 1)
        left, right = lane_place(left).upper(), lane_place(right).upper()
        if left in known and right in known and (left, right) not in lanes:
            lanes.append((left, right))
    return lanes


def _lanes_in(segment: str, known=()) -> list:
    # Exactly one separator. Two of them means a sentence with dashes in it,
    # not 'origin - destination'.
    pieces = _LANE_SPLIT.split(segment)
    if len(pieces) == 1:
        return _tight_lanes(segment, known)
    if len(pieces) != 2:
        return []
    origins = _endpoints(pieces[0], known)
    dests = _endpoints(pieces[1], known)
    if not origins or not dests:
        return []
    if len(origins) > MAX_ENDPOINTS_PER_SIDE or len(dests) > MAX_ENDPOINTS_PER_SIDE:
        return []
    lanes = [(o, d) for o in origins for d in dests]
    return lanes if len(lanes) <= MAX_LANES_PER_SEGMENT else []


_NOISE_WORDS = {"RATE", "RATES", "REQUEST", "QUOTE", "QUOTATION", "URGENT",
                "BOOKING", "DESTINATION", "REVISED", "REVISE", "NEW", "UPDATE",
                "ADVICE", "SHIPMENT", "SHIPMENTS", "UNKNOWN", "SHIPPER", "AND",
                "FOR", "SOLICITUD", "DE", "COTIZACION", "COTIZACIÓN",
                "WEEKEND", "LOAD", "PICKUP", "FREIGHT", "EXPORT", "IMPORT",
                "OCEAN", "WAREHOUSE", "CNEE", "CLIENTE", "TO"}

_MODE_WORDS = {keyword for keyword, _ in MODE_KEYWORDS}
_SPECIAL_WORDS = {keyword for keyword, _ in SPECIAL_KEYWORDS}


def _is_name_segment(segment: str):
    """A short all-caps segment naming a company, or None.

    'RATE REQUEST // RIDGEWAY PARTS // FOB // KUL / PEN - MEM' is how Victor
    writes the customer when he does not use the C/ convention. The vocabulary
    of the subject line - modes, incoterms, handling words, 'RATE REQUEST' -
    is stripped first, and what is left has to still look like a name.
    """
    text = segment.strip()
    if not text or len(text) > 40 or "-" in text or "/" in text:
        return None
    if any(ch.isdigit() for ch in text) or text.upper() != text:
        return None
    if text.upper() in _SPECIAL_WORDS or text.upper() in _MODE_WORDS:
        return None

    kept = []
    for word in re.split(r"\s+", text):
        word = word.strip("\"'")
        upper = word.upper()
        if upper in _NOISE_WORDS or upper in _MODE_WORDS \
                or upper in INCOTERM_WORDS or upper in _SPECIAL_WORDS:
            continue
        kept.append(word)
    if not kept or len(kept) > 4:
        return None
    name = " ".join(kept)
    return name if len(name) >= 3 else None


def _subject_endpoints(text: str, known=()) -> list:
    """Like _body_endpoints, but 'Barcelona, Spain' keeps its country: the
    part after the comma is carried along for the place lookup."""
    places = []
    for part in _ENDPOINT_SPLIT.split(text or ""):
        place = body_place(part, known)
        if not place:
            continue
        pieces = [p.strip() for p in part.split(",") if p.strip()]
        if len(pieces) >= 2 and place.casefold() == lane_place(pieces[0]).casefold():
            tail = lane_place(pieces[1])
            if tail and looks_like_place(tail) and len(tail.split()) <= 2:
                place = "{}, {}".format(place, tail)
        if place.upper() not in {p.upper() for p in places}:
            places.append(place)
    return places[:MAX_ENDPOINTS_PER_SIDE]


def _trimmed_places(side, keep_right, known=()) -> list:
    """'Rates USA to Manila airport': the words nearest the joining word are
    the place. Tries the longest run first, from the side nearest the join."""
    words = (side or "").split()
    for n in range(len(words), 0, -1):
        piece = " ".join(words[-n:] if keep_right else words[:n])
        if country_alias(piece.strip(" .,;:")):
            return [piece.strip(" .,;:")]          # RG2a: 'United States of America', whole
        places = _subject_endpoints(piece, known)
        if places:
            return places
    return []


def _from_to_lanes(text: str, known=()) -> list:
    """Lanes written as a sentence: 'from Norfolk port to Valencia, Spain',
    'USA to Jakarta airport', 'de Valparaiso a Savannah', 'desde Guayaquil
    hasta Newark', 'Arica hacia Oakland', 'origen: Rosario / destino: Tampa',
    'POL Paita / POD Baltimore'. Each side is cut to the place, and both
    sides have to read as places or nothing is returned."""
    lanes = []
    for pattern in (_FROM_TO, _TO_ONLY, _LABEL_PAIR, _ES_FROM_TO, _ES_TO_ONLY):
        for found in pattern.finditer(text or ""):
            origins = _trimmed_places(found.group("a"), True, known)
            dests = _trimmed_places(found.group("b"), False, known)
            for origin in origins:
                for dest in dests:
                    if (origin, dest) not in lanes:
                        lanes.append((origin, dest))
        if lanes:
            break
    return lanes


def _dest_side_text(segment: str) -> str:
    """The text of a subject segment's destination side: after its one
    ' - ', else what a from-to sentence names as the destination."""
    pieces = _LANE_SPLIT.split(segment or "")
    if len(pieces) == 2:
        return pieces[1]
    for pattern in (_FROM_TO, _TO_ONLY, _LABEL_PAIR, _ES_FROM_TO, _ES_TO_ONLY):
        found = pattern.search(segment or "")
        if found:
            return found.group("b")
    return ""


def _single_place(segment: str, known_countries=()) -> tuple | None:
    """'SOLICITUD TARIFA ENVIO OHIO- EEUU.' -> ('OHIO', 'USA').

    A place followed by a country and nothing else: the writer named where
    the cargo goes (or comes from) but not both ends. Only a country the
    alias list or the country table knows counts, so 'RATE - URGENT' does
    not become a place in a country called Urgent.
    """
    found = _PLACE_COUNTRY.match((segment or "").strip())
    if not found:
        return None
    country = country_alias(found.group("country"))
    if not country:
        name = found.group("country").strip(" .")
        country = next((c for c in known_countries if c.casefold() == name.casefold()), None)
    if not country:
        return None
    places = _trimmed_places(found.group("place"), True)
    if not places:
        return None
    place = places[0]
    if country_alias(place) or place.casefold() == country.casefold():
        return None                         # 'USA - USA' says nothing
    return place, country, segment.strip()


_ABBREVIATION = re.compile(r"^[A-Z]{3,5}$")


def _abbreviation(segment: str, known) -> str | None:
    """'SMRA' in a segment of its own: possibly the end customer's short
    name. Not when it is a location code, a mode, an incoterm or a handling
    word - those mean what they say."""
    text = (segment or "").strip()
    if not _ABBREVIATION.match(text) or text in known:
        return None
    if text in _MODE_WORDS or text in INCOTERM_WORDS or text in _SPECIAL_WORDS \
            or text in LANE_NOISE or text in _NOISE_WORDS:
        return None
    return text


def parse_subject(subject, known_locations=None, known_countries=None) -> SubjectParse:
    """Classify a mail subject.

    `known_locations` is the set of location keys. A three letter code that is
    already a location is reported as known; one that is not still becomes a
    candidate and triggers 'Add location...' when the case is saved.
    `known_countries` (names in the country table) lets 'TEXAS- EEUU' and
    'Lima, Peru' be read as a place in a country.
    """
    clean = clean_subject(subject)
    result = SubjectParse(clean=clean, thread_key=thread_key(clean))
    if not clean:
        return result

    known = {str(k).upper() for k in (known_locations or ())}
    countries = tuple(known_countries or ())
    result.segments = [s.strip() for s in clean.split("//")]
    # 'KV0318-27' on the end is stripped for the thread key (5.1) but it is
    # still their reference, so it goes to References from the raw subject.
    raw = _REPLY_PREFIX.sub("", subject or "")
    for _ in range(_MAX_PASSES):
        before = raw
        raw = _REPLY_PREFIX.sub("", raw)
        if raw == before:
            break
    for token in _REFERENCE.findall(raw):
        if any(ch.isdigit() for ch in token) and len(token) >= 5 \
                and token not in result.references \
                and not _CONTAINER_TOKEN.match(token):
            result.references.append(token)
    for token in _HASH_REF.findall(raw):
        if token not in result.references:
            result.references.append(token)

    for segment in result.segments:
        if not segment:
            continue
        upper = segment.upper()

        group = []
        for origin, dest in _lanes_in(segment, known):
            pair = (origin.upper(), dest.upper())
            if pair not in result.lanes:
                result.lanes.append(pair)
                group.append(pair)
        if not group:
            # 'Export shipment from Norfolk port to Valencia, Spain 1X40HQ'
            for origin, dest in _from_to_lanes(segment, known):
                pair = (origin.upper(), dest.upper())
                if pair not in result.lanes:
                    result.lanes.append(pair)
                    group.append(pair)
        if group:
            result.lane_groups.append(group)
            # batch 23: 'MIA - SAI OR VAP' is one route to either place
            dests = []
            for _, dest in group:
                if dest not in dests:
                    dests.append(dest)
            if len(dests) > 1 and written_as_alternatives(_dest_side_text(segment), dests, known):
                result.alternatives.append(tuple(dests))

        for keyword, mode in MODE_KEYWORDS:
            if re.search(r"\b{}\b".format(re.escape(keyword)), upper) and \
                    (keyword, mode) not in result.modes:
                result.modes.append((keyword, mode))

        for spelling in _EXW_SPELLINGS:
            if spelling in upper and "EXW" not in result.incoterms:
                result.incoterms.append("EXW")
        for word in INCOTERM_WORDS:
            if re.search(r"\b{}\b".format(word), upper) and \
                    word not in result.incoterms:
                result.incoterms.append(word)

        for pattern in (_END_CUSTOMER_SLASH, _END_CUSTOMER_WORD):
            for match in pattern.finditer(segment):
                name = match.group(1).strip(" .,;-\"'")
                if name and name.upper() not in [
                        c.upper() for c in result.end_customers]:
                    result.end_customers.append(name)

        for qty, size, suffix in _CONTAINER_GROUP.findall(segment):
            box = (int(qty), normalise_container(size, suffix))
            if box not in result.containers:
                result.containers.append(box)
        for size in _REEFER_BOX.findall(segment):
            box = (None, "{}RF".format(size))
            if box not in result.containers:
                result.containers.append(box)

        for keyword, tag in SPECIAL_KEYWORDS:
            if re.search(r"\b{}\b".format(re.escape(keyword)), upper) and \
                    (keyword, tag) not in result.special:
                result.special.append((keyword, tag))

        name = _is_name_segment(segment)
        if name and name not in result.names:
            result.names.append(name)

        short = _abbreviation(segment, known)
        if short and short not in result.abbreviations:
            result.abbreviations.append(short)

    if not result.lanes:
        for segment in result.segments:
            single = _single_place(segment, countries)
            if single and single not in result.single_places:
                result.single_places.append(single)

    for candidate in result.end_customers:
        if candidate not in result.names:
            result.names.append(candidate)
    result.unknown_locations = sorted(
        {place for lane in result.lanes for place in lane
         if _PLACE_CODE.match(place) and place not in known})
    if says_ipi(clean):
        result.modes = [m for m in result.modes if m[1] not in TRUCK_MODES]
    return result


# ------------------------------------------------- the body scan (5.2) ----
#: Where the writer's own words stop and the quoted mail starts. Below this
#: line the lane belongs to somebody else's mail, not to this one.
_QUOTE_START = re.compile(
    r"^\s*(?:-{2,}\s*)?(?:original message|forwarded message|mensaje original|"
    r"mensagem original)\b"
    # AL6a: Chinese 'original message' (Simplified, Traditional), with or
    # without the dashes around it
    r"|^\s*(?:-{2,}\s*)?(?:\u539f\u59cb\u90ae\u4ef6|\u539f\u59cb\u90f5\u4ef6)"
    # the header labels; AL6a: Simplified Chinese 'From' (\u53d1\u4ef6\u4eba),
    # and the colon ASCII or full width
    r"|^\s*(?:From|Sent|To|Subject|De|Enviada em|Para|Assunto|Da|Von|Van|"
    r"\u5bc4\u4ef6\u8005|\u5bc4\u4ef6\u4eba|\u767c\u4ef6\u4eba|\u53d1\u4ef6\u4eba)\s*[:\uff1a]"
    r"|^\s*_{10,}\s*$", re.IGNORECASE)

#: How far to read. The routing is stated near the top; below it are
#: signatures, disclaimers and cargo tables that only produce false lanes.
BODY_SCAN_LINES = 40

#: Only labels that name a port or a city. A pickup address is deliberately
#: not one of them: 'Pick up address: Harlow Pumps, LLC, 72 Quarry Bend
#: Road' would make the consignor's company the origin.
_ORIGIN_LABELS = {"POL", "POR", "ORIGIN", "PORT OF LOADING", "LOADING PORT",
                  "PLACE OF RECEIPT", "AOL", "AIRPORT OF LOADING", "PORT OF ORIGIN",
                  "ORIGIN AIRPORT", "ORIGIN PORT", "AIRPORT OF ORIGIN", "ORIGIN CITY",
                  "FROM AIRPORT", "FROM PORT", "DEPARTURE AIRPORT", "DEPARTURE PORT",
                  # Spanish / Portuguese
                  "ORIGEN", "ORIGEM", "SALIDA", "PARTIDA", "PUERTO DE ORIGEN",
                  "PUERTO DE CARGA", "PUERTO DE EMBARQUE", "AEROPUERTO DE ORIGEN",
                  "PORTO DE ORIGEM", "PORTO DE EMBARQUE", "AEROPORTO DE ORIGEM"}
_DEST_LABELS = {"POD", "DEST", "DESTINATION", "FINAL DESTINATION",
                "PORT OF DISCHARGE", "DISCHARGE PORT", "PLACE OF DELIVERY",
                "AOD", "AIRPORT OF DISCHARGE", "AIRPORT OF DESTINATION",
                "PORT OF DESTINATION", "DESTINATION AIRPORT", "DESTINATION PORT",
                "DEST AIRPORT", "DEST PORT", "DESTINATION CITY", "TO AIRPORT", "TO PORT",
                "ARRIVAL AIRPORT", "ARRIVAL PORT",
                # Spanish / Portuguese
                "DESTINO", "LLEGADA", "CHEGADA", "PUERTO DE DESTINO",
                "PUERTO DE DESCARGA", "AEROPUERTO DE DESTINO", "PORTO DE DESTINO",
                "PORTO DE DESCARGA", "AEROPORTO DE DESTINO"}

#: RG2a (B): labels a table writes above its value ('POL' alone on a line,
#: 'Shanghai' on the next) beside the ones above. Their value is taken only
#: when it is a place on file or a country: a pick-up label is followed by
#: the consignor's company as often as by a town.
_WEAK_ORIGIN_LABELS = {"FROM", "PICK UP", "PICKUP", "PICK-UP", "AIRPORT OF DEPARTURE", "PORT OF DEPARTURE",
                       "AEROPUERTO DE SALIDA", "PUERTO DE SALIDA", "RECOGIDA", "COLETA"}
_WEAK_DEST_LABELS = {"TO", "DELIVERY", "DELIVERY TO", "ENTREGA", "LUGAR DE ENTREGA"}
_LABEL_CHARS = " \t:*-•·#.："


def lane_label(text) -> tuple | None:
    """RG2a (B): ('origin' | 'dest', weak) when the text is a lane label and
    nothing else ('POL', 'Port of Discharge:', 'Destino'); None otherwise."""
    label = " ".join((text or "").strip(_LABEL_CHARS).split()).upper()
    if not label or len(label) > 30:
        return None
    if label in _ORIGIN_LABELS:
        return "origin", False
    if label in _DEST_LABELS:
        return "dest", False
    if label in _WEAK_ORIGIN_LABELS:
        return "origin", True
    if label in _WEAK_DEST_LABELS:
        return "dest", True
    return None


_CELL_SPLIT = re.compile(r"\t+|\s{2,}|\s*\|\s*")


def joined_lane_lines(lines) -> list:
    """RG2a (B): [(line, label)] - a lane label alone on its line takes the
    next non-empty line as its value ('POL' / 'CHICAGO, US' -> 'POL:
    CHICAGO, US'), and a header row with a lane label among its cells takes
    the cell under it in the next row; `label` is the lane_label() of a
    joined line, None for the lines as they were."""
    lines = [" ".join((l or "").split()) if "\t" not in (l or "") else (l or "").strip() for l in lines]
    lines = [l for l in lines if l]
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        found = lane_label(line)
        if found and i + 1 < len(lines) and lane_label(lines[i + 1]) is None and len(lines[i + 1]) <= 80 \
                and not _LABEL_LINE.match(lines[i + 1]):
            out.append(("{}: {}".format(line.strip(_LABEL_CHARS), lines[i + 1]), found))
            i += 2
            continue
        cells = [c for c in _CELL_SPLIT.split(line) if c.strip()]
        if len(cells) >= 2 and i + 1 < len(lines) and any(lane_label(c) for c in cells):
            under = [c for c in _CELL_SPLIT.split(lines[i + 1]) if c.strip()]
            if len(under) == len(cells) and not any(lane_label(c) for c in under):
                for head, value in zip(cells, under):
                    label = lane_label(head)
                    if label:
                        out.append(("{}: {}".format(head.strip(_LABEL_CHARS), value.strip()), label))
                i += 2
                continue
        out.append((" ".join(line.split()), None))
        i += 1
    return out


_LABEL_LINE = re.compile(
    r"^[\W\d]{0,4}(?P<label>[A-Za-z][A-Za-z .()/\-]{1,28}?)\s*:\s*(?P<value>.+)$")
_FROM_TO = re.compile(
    r"\bfrom\s+(?P<a>.{2,60}?)\s+to\s+(?P<b>.{2,60}?)"
    r"(?=[.;]|,(?!\s*[A-Z][A-Za-z])|\s+\d|\s+(?:for|with|on|by|via|in|at|and|please|as)\b|$)",
    re.IGNORECASE)
#: 'USA to Manila airport' with no 'from': up to three words a side.
_TO_ONLY = re.compile(
    r"(?<![\w,])(?P<a>(?:[A-Za-z][\w.'-]*\s+){0,2}[A-Za-z][\w.'-]*)\s+to\s+"
    r"(?P<b>(?:[A-Za-z][\w.'-]*[ ,]+){0,2}[A-Za-z][\w.'-]*)"
    r"(?=[.;]|,(?!\s*[A-Z][A-Za-z])|\s+\d|\s+(?:for|with|on|by|via|in|at|and|please|as)\b|$)",
    re.IGNORECASE)
_ENDPOINT_SPLIT = re.compile(r"\s*/\s*|\s+and\s+|\s+&\s+|" + _OR_SPLIT, re.IGNORECASE)

#: Where a Spanish / Portuguese lane sentence ends: the next preposition,
#: a number, punctuation, or the end of the text.
_ES_END = (r"(?=[.;]|,(?!\s*[A-Z][A-Za-z])|\s+\d|"
           r"\s+(?:para|con|por|v\u00eda|via|en|em|y|e|favor|el|la|los|las|del|de|"
           r"com|para|urgente|please|for|with)\b|$)")
#: 'de Santiago a Houston', 'desde Lima hasta Miami', 'de Lima para Miami'
_ES_FROM_TO = re.compile(
    r"\b(?:desde|de)\s+(?P<a>.{2,60}?)\s+(?:hasta|hacia|a|para)\s+(?P<b>.{2,60}?)" + _ES_END,
    re.IGNORECASE)
#: 'Santiago hacia Houston' with no 'de': up to three words a side.
_ES_TO_ONLY = re.compile(
    r"(?<![\w,])(?P<a>(?:[A-Za-z\u00c0-\u00ff][\w.'\-\u00c0-\u00ff]*\s+){0,2}"
    r"[A-Za-z\u00c0-\u00ff][\w.'\-\u00c0-\u00ff]*)\s+(?:hacia|hasta)\s+"
    r"(?P<b>(?:[A-Za-z\u00c0-\u00ff][\w.'\-\u00c0-\u00ff]*[ ,]+){0,2}"
    r"[A-Za-z\u00c0-\u00ff][\w.'\-\u00c0-\u00ff]*)" + _ES_END,
    re.IGNORECASE)
#: 'origen: Santiago / destino: Houston', 'POL Callao - POD Houston',
#: 'salida Lima, llegada Miami', 'AOL SCL AOD IAH' - two labels on one line.
_LABEL_PAIR = re.compile(
    r"\b(?:origen|origem|origin|salida|partida|pol|aol)\s*:?\s+(?P<a>.{2,40}?)"
    r"\s*(?:[/,;|\u2013\u2014-]\s*)?"
    r"\b(?:destino|destination|dest|llegada|chegada|pod|aod)\s*:?\s+"
    r"(?P<b>.{2,40}?)" + _ES_END,
    re.IGNORECASE)
#: 'TEXAS- EEUU.', 'Lima, Peru', 'Santos - Brasil': a place and its country.
_PLACE_COUNTRY = re.compile(
    r"^(?P<place>.+?)\s*[-\u2013\u2014,]\s*(?P<country>[A-Za-z\u00c0-\u00ff][A-Za-z\u00c0-\u00ff. ]{1,40}?)\s*\.?$")

SNIPPET_LEN = 90


@dataclass
class BodyParse:
    """What the body says about where the cargo goes (body scan).

    Each side is filled only when the body says one thing. Two lines that
    disagree ('POD: San Antonio' and 'POD: Antofagasta') fill nothing and go
    to `conflicts` for the user to choose from: a guess between two stated
    destinations is worse than an empty field she has to fill in herself.
    """

    origins: list = field(default_factory=list)
    dests: list = field(default_factory=list)
    #: side -> the line it was read from, for the form to quote back.
    evidence: dict = field(default_factory=dict)
    #: side -> [(place, line), ...] when the body says several different things.
    conflicts: dict = field(default_factory=dict)
    scanned_lines: int = 0
    #: batch 23: the destinations were written as alternatives ('POD: San
    #: Antonio or Valparaiso'): one route with all of them, not a route each.
    dest_alternatives: bool = False
    #: RG2a (B): side -> True when it was read from a label above its value
    #: (or a column header), the way a table writes it
    tabled: dict = field(default_factory=dict)

    @property
    def lanes(self) -> list:
        return [(o, d) for o in self.origins for d in self.dests]


def body_lines(body) -> list:
    """The writer's own words: down to the quoted mail, at most 40 lines.

    Blank lines do not count against the forty - an Outlook body is mostly
    blank lines, and spending the budget on them would stop the scan halfway
    down the first paragraph.
    """
    lines = []
    for raw in (body or "").splitlines():
        line = raw.strip()
        if _QUOTE_START.match(line):
            break
        if not line:
            continue
        lines.append(line)
        if len(lines) >= BODY_SCAN_LINES:
            break
    return lines


def _snippet(line: str) -> str:
    line = " ".join((line or "").split())
    return line if len(line) <= SNIPPET_LEN else line[:SNIPPET_LEN - 1] + "\u2026"


def body_place(text, known=()) -> str | None:
    """One endpoint out of body text: 'Tacoma, WA, USA' -> 'Tacoma'.

    A known location wins over a longer reading, so 'Mobile, AL' stays whole
    when that is the location on file.
    """
    text = lane_place((text or "").strip().strip(" .,;:*-\u2013\u2014"))
    parts = [lane_place(p) for p in text.split(",") if p.strip()]
    if not parts:
        return None
    tries = [parts[0]]
    if len(parts) >= 2:
        tries.append("{}, {}".format(parts[0], parts[1]))
    for candidate in tries:
        if candidate.upper() in known:
            return candidate
    for candidate in tries:
        if looks_like_place(candidate):
            return candidate
    return None


def _body_endpoints(text, known) -> list:
    """'Mundra and Kolkata' and 'San Antonio / Valparaiso' are two places."""
    places = []
    for part in _ENDPOINT_SPLIT.split(text or ""):
        place = body_place(part, known)
        if place and place.upper() not in {p.upper() for p in places}:
            places.append(place)
    return places[:MAX_ENDPOINTS_PER_SIDE]


def parse_body_lanes(body, known_locations=None, tables=True) -> BodyParse:
    """Read the lane out of the mail body when the subject has none.

    Reads at most BODY_SCAN_LINES non-empty lines and stops at the quoted
    mail. Everything it finds is a suggestion for the form, like the subject.
    RG2a (B): with `tables`, a label above its value counts too, and where
    it and a sentence disagree the table's cell wins.
    """
    result = BodyParse()
    known = {str(k).upper() for k in (known_locations or ())}
    lines = body_lines(body)
    result.scanned_lines = len(lines)

    readings = {"origin": [], "dest": []}
    # RG2a (B): the readings that came from a label above its value
    tabled = set()
    # batch 23: the destination readings written as alternatives ('or')
    alternative = set()
    for line, joined in (joined_lane_lines(lines) if tables else [(l, None) for l in lines]):
        if joined is not None:
            # RG2a (B): 'POL' / 'Shanghai' - the label's side, the value its place
            # a table's cell holds a town as often as a company or an address:
            # the value counts when it is a place on file or a country
            side, _weak = joined
            value = line.split(":", 1)[1]
            places = [p for p in _body_endpoints(value, known) if p.upper() in known or country_alias(p)]
            if places:
                readings[side].append((places, _snippet(line)))
                tabled.add((side, tuple(p.upper() for p in places)))
                if side == "dest" and written_as_alternatives(value, places, known):
                    alternative.add(tuple(p.upper() for p in places))
            continue
        # 'AOL: SCL / AOD: IAH' on one line: both labels, before the first
        # label swallows the second as part of its value.
        pair = _LABEL_PAIR.search(line)
        if pair:
            origins = _trimmed_places(pair.group("a"), True)
            dests = _trimmed_places(pair.group("b"), False)
            if origins and dests:
                quoted = _snippet(pair.group(0))
                readings["origin"].append((origins, quoted))
                readings["dest"].append((dests, quoted))
                if written_as_alternatives(pair.group("b"), dests, known):
                    alternative.add(tuple(p.upper() for p in dests))
                continue
        match = _LABEL_LINE.match(line)
        if match:
            label = match.group("label").strip(" .").upper()
            side = ("origin" if label in _ORIGIN_LABELS else
                    "dest" if label in _DEST_LABELS else None)
            if side:
                places = _body_endpoints(match.group("value"), known)
                if places:
                    readings[side].append((places, _snippet(line)))
                    if side == "dest" and written_as_alternatives(match.group("value"), places, known):
                        alternative.add(tuple(p.upper() for p in places))
                continue
        for pattern in (_FROM_TO, _LABEL_PAIR, _ES_FROM_TO, _ES_TO_ONLY):
            hit = False
            for found in pattern.finditer(line):
                if pattern is _FROM_TO:
                    origins = _body_endpoints(found.group("a"), known)
                    dests = _body_endpoints(found.group("b"), known)
                else:
                    origins = _trimmed_places(found.group("a"), True)
                    dests = _trimmed_places(found.group("b"), False)
                if origins and dests:
                    # The sentence fragment, not the whole line: a long sentence
                    # truncated at 90 characters can cut off the destination it
                    # was quoted to show.
                    quoted = _snippet(found.group(0))
                    readings["origin"].append((origins, quoted))
                    readings["dest"].append((dests, quoted))
                    if written_as_alternatives(found.group("b"), dests, known):
                        alternative.add(tuple(p.upper() for p in dests))
                    hit = True
                    break
            if hit:
                break

    for side in ("origin", "dest"):
        distinct = []
        seen = set()
        for places, line in readings[side]:
            key = tuple(p.upper() for p in places)
            if key not in seen:
                seen.add(key)
                distinct.append((places, line))
        from_table = [d for d in distinct if (side, tuple(p.upper() for p in d[0])) in tabled]
        if len(distinct) > 1 and len(from_table) == 1:
            distinct = from_table                       # RG2a (B): the table's cell is the specific one
        if len(distinct) == 1:
            places, line = distinct[0]
            if side == "origin":
                result.origins = places
            else:
                result.dests = places
                result.dest_alternatives = tuple(p.upper() for p in places) in alternative
            result.evidence[side] = line
            if (side, tuple(p.upper() for p in places)) in tabled:
                result.tabled[side] = True
        elif len(distinct) > 1:
            result.conflicts[side] = [(place, line)
                                      for places, line in distinct
                                      for place in places]
    return result


#: The header lines of a quoted mail. Outlook writes them in the language of
#: the sender's own Office, so this list is not only English.
_QUOTED_FROM = re.compile(
    r"^\s*(?:>+\s*)?(?:From|De|Da|Von|Van|Fra|Fr\u00e5n|\u5bc4\u4ef6\u8005|"
    r"\u5bc4\u4ef6\u4eba|\u767c\u4ef6\u4eba)\s*:\s*(?P<rest>.+)$",
    re.IGNORECASE)
_ADDRESS = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def quoted_senders(body) -> list:
    """Addresses on the 'From:' lines of the mails quoted below.

    When Victor replies to an agent, the agent's own mail is quoted under his
    answer and its From: line is the only place the requester appears. Order is
    the order they appear, nearest quote first; duplicates are dropped.
    """
    found = []
    for raw in (body or "").splitlines():
        match = _QUOTED_FROM.match(raw)
        if not match:
            continue
        for address in _ADDRESS.findall(match.group("rest")):
            address = address.lower()
            if address not in found:
                found.append(address)
    return found


# ------------------------------------------ the body scan: quantity (5.2) --
#: '1x20', '2 x 40HQ', "1 X 20'". The count is required: a bare 40HQ in a
#: sentence is as likely to be a container type in a question as a booking.
#: Only the sizes containers come in, and never a dimension: '70 x 55 x 48
#: cm' is a carton, not seventy boxes.
_BODY_CONTAINERS = re.compile(
    # not the tail of a size ('30 x 20 x 20 inches', 2026-09-29): no 'x' before the count
    r"(?<![\w.])(?<![xX\u00d7*]\s)(?<![xX\u00d7*])(\d{1,3})\s*[xX\u00d7]\s*(20|40|45|53)\s*'?\s*(GP|HQ|HC|OT|FR|RF|NOR)?"
    r"(?!\s*[xX\u00d7]\s*\d)(?!\s*(?:cm|mm|m|in|inch|inches|ft)\b)\b",
    re.IGNORECASE)
#: 'kg', 'kgs', and 'KGSKG' / 'KGSKGSKG' - the kilo unit with stray KG
#: letters glued on, as one agent's template writes it (2026-09-24).
_KG_UNIT = r"kgs?(?:kgs?)*"
#: DD1 (2026-11-12): a weight written with five to seven digits and no
#: separator ('27318kgs', '43500 lbs') - only with its weight unit right
#: after it; every other number keeps its limit.
_BIG_WEIGHT = r"\d{5,7}(?:[.,]\d{1,2})?"
#: The body weight as read before DD1 (up to four figures without separators):
#: what decides whether a 'Weight: ...' line is kept as a note.
_SMALL_BODY_WEIGHT = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:[,.]\d{3})*(?:[.,]\d{1,2})?)\s*(?:" + _KG_UNIT + r"|KILOS?|KILOGRAMS?)\b",
    re.IGNORECASE)
#: '2,140 kg', '238.50 kg' and the LATAM '41,5 kg' (a decimal comma).
_BODY_WEIGHT = re.compile(
    r"(?<![\w.,])(" + _BIG_WEIGHT + r"|\d{1,3}(?:[,.]\d{3})*(?:[.,]\d{1,2})?)\s*(?:" + _KG_UNIT
    + r"|KILOS?|KILOGRAMS?)\b",
    re.IGNORECASE)
#: '5 pcs, 41,5 kg - 70 x 55 x 48 cm each': one line of a packing list. With
#: 'each' the kilos are per piece; without, the line's total.
_PIECES_LINE = re.compile(
    r"(?<!\d)(?P<n>\d{1,4})\s*(?:pcs?|pieces?|ctns?|cartons?|boxes?|bultos?|cajas?|"
    r"pallets?|plts?|ptls?|paletas?|piezas?)\b[^\n]*?"
    r"(?<![\w.,])(?P<w>" + _BIG_WEIGHT + r"|\d{1,3}(?:[,.]\d{3})*(?:[.,]\d{1,2})?)\s*(?:" + _KG_UNIT
    + r"|kilos?|kilograms?)\b"
    r"(?P<rest>[^\n]*)$", re.IGNORECASE)
_EACH = re.compile(r"\b(?:each|c/u|cada uno|cada una|cada|por pieza|por bulto)\b", re.IGNORECASE)
_BODY_VOLUME = re.compile(
    r"(?<![\w.,])(\d{1,4}(?:[.,]\d+)?)\s*(?:CBM|M3|M\u00b3)\b", re.IGNORECASE)


#: What decides how '9.100' reads (13g): Portuguese or Spanish words, or
#: another number written with a decimal comma ('36,5', '1.234,56'), mean
#: the dot separates thousands; an English decimal ('1.5', '1,250.50') means
#: it is a decimal point.
_PT_ES_WORDS = re.compile(
    r"\b(?:prezad[oa]s?|obrigad[oa]|atenciosamente|cota[c\u00e7][a\u00e3]o|frete|mercadoria|coleta|"
    r"equipamento|embarque|estimad[oa]s?|saludos|flete|cotizaci[o\u00f3]n|mercanc[i\u00ed]a|"
    r"recogida|gracias|favor\s+(?:cotar|de)|por\s+favor|peso\s+bruto|a\s+definir|destino)\b",
    re.IGNORECASE)
_DECIMAL_COMMA = re.compile(r"(?<![\d.,])\d{1,3}(?:\.\d{3})*,\d{1,2}(?![\d.,])")
_DECIMAL_DOT = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})*\.\d{1,2}(?![\d.,])|(?<![\d.,])\d{1,3}(?:,\d{3})+\.\d+(?![\d.,])")


def number_style(text) -> str:
    """'comma' when a dot before three digits separates thousands (the mail
    is Portuguese or Spanish, or writes decimals with a comma), 'dot' when
    it is a decimal point (an English decimal elsewhere), '' when nothing
    settles it."""
    text = text or ""
    if len({m.group(0).lower() for m in _PT_ES_WORDS.finditer(text)}) >= 2:
        return "comma"
    comma, dot = bool(_DECIMAL_COMMA.search(text)), bool(_DECIMAL_DOT.search(text))
    if comma and not dot:
        return "comma"
    if dot and not comma:
        return "dot"
    return ""


def settled_number(text, style) -> tuple:
    """(value as _number() writes it, other reading or None) for a weight
    or volume as written. '9.100' in a 'comma' mail is 9100; in a 'dot'
    mail 9.1; with no style it stays 9.1 with 9100 as the other reading,
    for the caller to ask. Anything that reads one way is unchanged."""
    text = (text or "").strip()
    found = _TWO_READINGS.match(text)
    if not found or found.group("sep") != ".":
        return _number(text), None
    thousands = found.group("a") + found.group("b")
    if style == "comma":
        return thousands, None
    if style == "dot":
        return _number(text), None
    return _number(text), thousands


def _amount(value) -> str:
    """'9100' -> '9,100', '12.5' -> '12.5' - a number for a note."""
    number = float(value)
    return "{:,}".format(int(number)) if number == int(number) else _fmt(number)


@dataclass
class BodyQty:
    """What the body says about how much cargo there is.

    `qty_raw` is written in the 'MODE:value' convention the workbook has
    always used, so the form's 'Parse into the fields' button understands it;
    `evidence` keeps the line it was actually read from.
    """

    qty_raw: str = ""
    mode: str | None = None
    evidence: str = ""
    #: Several different weights or volumes stated: nothing is filled in.
    conflicts: list = field(default_factory=list)
    #: 'Cargo weight: roughly 3tons' - a weight in words, kept as text.
    weight_note: str = ""
    #: '9.100 kg read as 9,100 kg' when the dot was read as thousands (13g).
    reading_note: str = ""
    #: (as written, one reading, the other) when nothing settles how the
    #: number reads: nothing is filled in, she is asked (13g).
    ask: tuple | None = None
    #: The kilos behind an 'Air:' qty_raw, for a sea mail's gross weight.
    weight_kg: float | None = None


def _number(text: str) -> str:
    """'2,140.50' -> '2140.5', '238.00' -> '238', '41,5' -> '41.5'.
    A comma before exactly three digits separates thousands; before one or
    two it is the LATAM decimal point. With both '.' and ',' in it (H1,
    2026-10-02) the last one is the decimal mark and the others group the
    thousands: '13.482,71' -> '13482.71', '1.234.567,8' -> '1234567.8'.
    Several dots and no comma can only be thousands: '1.234.567' -> '1234567'."""
    text = (text or "").strip()
    if "." in text and "," in text:
        mark = max(text.rfind("."), text.rfind(","))
        text = re.sub(r"[.,]", "", text[:mark]) + "." + text[mark + 1:]
    elif text.count(".") > 1:
        text = text.replace(".", "")
    text = re.sub(r",(?=\d{3}(?:\D|$))", "", text)
    text = text.replace(",", ".")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _fmt(value: float) -> str:
    text = "{:.2f}".format(value).rstrip("0").rstrip(".")
    return text or "0"


def parse_body_qty(body, style=None) -> BodyQty:
    """Read the quantity out of the mail body (body scan).

    Containers win over volume, volume over weight: a mail that names boxes is
    an FCL enquiry whatever else it lists. Several different weights (one line
    per crate) fill nothing - adding them up would invent a total nobody
    stated. `style` (number_style) says how '9.100' reads; by default it is
    read off the body itself.
    """
    if style is None:
        style = number_style(body)
    result = BodyQty()
    boxes, weights, volumes = [], [], []
    written = {}           # value -> (as written, the other reading or None)
    pieces = []            # [(line total kg, line)] from a packing list
    for line in body_lines(body):
        # DD1: the note is decided as before the five-to-seven-figure weights
        # (an FCL quantity keeps 'Weight : 20000kg' as text)
        if _WEIGHT_NOTE.match(line) and not _SMALL_BODY_WEIGHT.search(line) \
                and not result.weight_note:
            result.weight_note = " ".join(line.split())
        for count, size, suffix in _BODY_CONTAINERS.findall(line):
            box = "{} x {}".format(count, normalise_container(size, suffix))
            if box not in [b for b, _ in boxes]:
                boxes.append((box, _snippet(line)))
        packed = _PIECES_LINE.search(line)
        if packed:
            kilos = float(settled_number(packed.group("w"), style)[0])
            if _EACH.search(packed.group("rest")):
                kilos *= int(packed.group("n"))
            pieces.append((kilos, _snippet(line)))
            continue
        for found in _BODY_WEIGHT.finditer(line):
            value, other = settled_number(found.group(1), style)
            written.setdefault(("kg", value), (" ".join(found.group(0).split()), found.group(1), other))
            if value not in [v for v, _ in weights]:
                weights.append((value, _snippet(line)))
        for found in _BODY_VOLUME.finditer(line):
            value, other = settled_number(found.group(1), style)
            written.setdefault(("cbm", value), (" ".join(found.group(0).split()), found.group(1), other))
            if value not in [v for v, _ in volumes]:
                volumes.append((value, _snippet(line)))

    if boxes:
        # 'FCL:2 x 40HQ / 1 x 20GP' is the mixed-load spelling.
        result.qty_raw = "FCL:" + " / ".join(box for box, _ in boxes)
        result.mode = "FCL"
        result.evidence = boxes[0][1]
        if result.weight_note:
            # the weight in words rides along as text, never as air kg
            result.qty_raw += " ({})".format(result.weight_note)
        return result
    if pieces and not weights and not volumes:
        # A packing list: every weight line is 'N pcs, W kg [each]', so the
        # total is stated line by line and adding it up invents nothing.
        result.qty_raw = "Air:" + _fmt(sum(kilos for kilos, _ in pieces))
        result.mode = "Air"
        result.evidence = pieces[0][1] + (
            " (+{} more line{})".format(len(pieces) - 1, "s" if len(pieces) > 2 else "")
            if len(pieces) > 1 else "")
        return result
    weights = pieces + weights if pieces else weights
    weights = [(_fmt(v) if isinstance(v, float) else v, line) for v, line in weights]
    for values, mode, prefix, unit in ((volumes, "LCL", "LCL:", "cbm"), (weights, "Air", "Air:", "kg")):
        if not values:
            continue
        if len(values) > 1:
            result.conflicts = [(v, line) for v, line in values]
            return result
        value = values[0][0]
        as_written, number_text, other = written.get((unit, value), ("", "", None))
        result.evidence = values[0][1]
        if other is not None:
            # '9.100 kg' and nothing says how it reads: kept as written, asked
            result.qty_raw = as_written
            result.ask = (as_written, "{} {}".format(_amount(value), unit), "{} {}".format(_amount(other), unit))
            return result
        result.qty_raw = prefix + value
        result.mode = mode
        if unit == "kg":
            result.weight_kg = float(value)
        if number_text and float(_number(number_text)) != float(value):
            result.reading_note = "{} read as {} {}".format(as_written, _amount(value), unit)
        return result
    if result.weight_note:
        result.qty_raw = result.weight_note
        result.evidence = result.weight_note
    return result


_WEIGHT_NOTE = re.compile(r"^[\W\d]{0,4}(?:cargo\s+|gross\s+|total\s+)?weight\s*:\s*\S",
                          re.IGNORECASE)
_COMMODITY_LINE = re.compile(
    r"\b(?:general\s+cargo|commodity|commodities|cargo|goods|material|description|"
    r"product|item|mercanc[i\u00ed]a|mercadoria|comm\.?)\s*[:\uff1a]\s*(?P<value>.+?)\s*$"
    # 'COMMODIYT GARDEN HOSES' (2026-09-24): the word misspelt and no colon
    r"|^\W{0,3}commod\w*\s*[:\uff1a]?\s+(?P<value2>[A-Za-z].+?)\s*$",
    re.IGNORECASE)
#: 'HS CODE 8424', 'HS: 8424.82' - a line of a cargo block (v50: the code
#: itself is read by hs_codes_in into the line's hs_code).
_HS_LINE = re.compile(r"^\W{0,3}HS\s*(?:CODE)?\s*[:\uff1a]?\s*(?P<code>\d{4,10}(?:\.\d+)?)\b", re.IGNORECASE)
#: D7a (v50): one HS code as written - dotted (2507.00.10, 8471.30.0100),
#: in pairs after the heading (8471 30 01), or plain digits (850440)
_HS_CODE = r"\d{4}(?:\.\d{1,4}){1,3}(?![\d.])|\d{4}(?:\s\d{2}){1,3}(?!\d)|\d{4,10}(?![\d.])"
#: D7a: the codes after HS, H.S., HS code, HTS, NCM or tariff code, with or
#: without a colon; several joined by commas, 'and', '&', '/' or ';'
_HS_LABELLED = re.compile(
    r"(?<![A-Za-z])(?:H\.?\s?S\.?(?:\s*codes?)?|HTS(?:US)?(?:\s*codes?)?|NCM(?:\s*codes?)?|tariff\s+codes?)(?![A-Za-z])"
    r"\s*(?:no\.?|number|#)?\s*[:\uff1a=\-]?\s*"
    r"(?P<codes>(?:" + _HS_CODE + r")(?:\s*(?:,|;|/|&|\band\b)\s*(?:" + _HS_CODE + r"))*)", re.IGNORECASE)


def hs_codes_in(lines) -> tuple:
    """D7a: (codes, line) - every HS code the lines name after its heading,
    each once, ', ' between them, and the first line it was read from;
    ('', '') when none."""
    found, where = [], ""
    for line in lines or []:
        for m in _HS_LABELLED.finditer(str(line or "")):
            for code in re.findall(_HS_CODE, m.group("codes")):
                code = " ".join(code.split())
                if code not in found and 4 <= len(re.sub(r"\D", "", code)) <= 10:
                    found.append(code)
                    where = where or " ".join(str(line).split())
    return ", ".join(found), where


def body_commodity(lines) -> tuple:
    """(commodity, line) from 'general cargo: pump housings', 'Commodity:
    boat trailer', 'Goods: ...'; ('', '') when no line says."""
    for line in lines:
        match = _COMMODITY_LINE.search(line)
        if not match:
            continue
        value = (match.group("value") or match.group("value2") or "").strip(" .;,-")
        if value and not value.replace(" ", "").isdigit():
            return value, " ".join(line.split())
    return "", ""


# ------------------------------------------------ the quoted mails (5.2) --
_SENT_LINE = re.compile(
    r"^\s*(?:>+\s*)?(?:Sent|Date|Enviada em|Enviado el|Enviado|Fecha|Gesendet|"
    r"Verzonden|Data|Datum|\u767c\u9001\u6642\u9593|\u65e5\u671f|\u53d1\u9001\u65f6\u95f4)"
    r"\s*:\s*(?P<rest>.+)$", re.IGNORECASE)
_SUBJECT_LINE = re.compile(
    r"^\s*(?:>+\s*)?(?:Subject|Assunto|Asunto|Betreff|Onderwerp|Oggetto|"
    r"\u4e3b\u65e8|\u4e3b\u9898)\s*:\s*(?P<rest>.*)$", re.IGNORECASE)
_OTHER_HEADER = re.compile(
    r"^\s*(?:>+\s*)?(?:To|Cc|Bcc|Para|Cc|An|Aan|A|Importance|"
    r"\u6536\u4ef6\u8005|\u6536\u4ef6\u4eba|\u526f\u672c|\u6284\u9001)\s*:", re.IGNORECASE)
_TO_HEADER = re.compile(
    r"^\s*(?:>+\s*)?(?:To|Para|An|Aan|A|\u6536\u4ef6\u8005|\u6536\u4ef6\u4eba)\s*:\s*(?P<rest>.*)$",
    re.IGNORECASE)
_CC_HEADER = re.compile(
    r"^\s*(?:>+\s*)?(?:Cc|CC|Kopie|\u526f\u672c|\u6284\u9001)\s*:\s*(?P<rest>.*)$")
_OFFICE_PREFIX = re.compile(r"^\s*([A-Z]{2,4})-")
_MONTHS = {name.lower(): number for number, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), start=1)}
_MONTHS.update({name: number for number, name in enumerate(
    ("janeiro", "fevereiro", "mar\u00e7o", "abril", "maio", "junho", "julho",
     "agosto", "setembro", "outubro", "novembro", "dezembro"), start=1)})
_MONTHS.update({name: number for number, name in enumerate(
    ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
     "septiembre", "octubre", "noviembre", "diciembre"), start=1)})
_MONTHS.update({name[:3]: number for name, number in list(_MONTHS.items())})
_DATE_WORDS = re.compile(
    r"(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?:de\s+)?(?P<m>[A-Za-z\u00e7]{3,10})\.?,?\s+(?:de\s+)?"
    r"(?P<y>\d{4})(?:\s+(?:at\s+|\u00e0s\s+|a las\s+)?(?P<t>\d{1,2}:\d\d)\s*(?P<ap>[AaPp][Mm])?)?")
_DATE_MDY = re.compile(
    r"(?P<m>[A-Za-z]{3,10})\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>\d{4})"
    r"(?:\s+(?:at\s+)?(?P<t>\d{1,2}:\d\d)\s*(?P<ap>[AaPp][Mm])?)?")
_DATE_NUMERIC = re.compile(
    r"(?P<y>\d{4})-(?P<m>\d\d)-(?P<d>\d\d)(?:[ T](?P<t>\d{1,2}:\d\d))?"
    r"|(?P<m2>\d{1,2})/(?P<d2>\d{1,2})/(?P<y2>\d{4})(?:\s+(?P<t2>\d{1,2}:\d\d)\s*(?P<ap2>[AaPp][Mm])?)?")


def parse_sent(text: str) -> str:
    """'Thursday, October 8, 2026 3:17 PM' -> '2026-10-08 15:17';
    'quarta-feira, 14 de outubro de 2026 09:42' -> '2026-10-14 09:42'.
    '' when the line does not read as a date."""
    text = " ".join((text or "").split())

    def stamp(y, m, d, t, ap):
        try:
            y, m, d = int(y), int(m), int(d)
            hour, minute = (int(p) for p in (t or "0:00").split(":"))
        except ValueError:
            return ""
        if ap:
            if ap.lower() == "pm" and hour < 12:
                hour += 12
            if ap.lower() == "am" and hour == 12:
                hour = 0
        try:
            return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}".format(y, m, d, hour, minute)
        except ValueError:
            return ""

    for found in _DATE_MDY.finditer(text):
        month = _MONTHS.get(found.group("m").lower())
        if month:
            return stamp(found.group("y"), month, found.group("d"),
                         found.group("t"), found.group("ap"))
    for found in _DATE_WORDS.finditer(text):
        month = _MONTHS.get(found.group("m").lower())
        if month:
            return stamp(found.group("y"), month, found.group("d"),
                         found.group("t"), found.group("ap"))
    found = _DATE_NUMERIC.search(text)
    if found:
        if found.group("y"):
            return stamp(found.group("y"), found.group("m"), found.group("d"),
                         found.group("t"), None)
        return stamp(found.group("y2"), found.group("m2"), found.group("d2"),
                     found.group("t2"), found.group("ap2"))
    return ""


@dataclass
class QuotedMail:
    """One mail quoted under the selected one, nearest first in the list."""

    name: str = ""
    address: str = ""
    sent_at: str = ""            # 'YYYY-MM-DD HH:MM' when the Sent line read
    subject: str = ""
    text: str = ""               # that mail's own lines, quotes below it cut
    to: str = ""                 # the To: line as written
    cc: str = ""                 # the Cc: line as written

    @property
    def sent_on(self) -> str:
        return self.sent_at[:10]

    @property
    def office(self) -> str:
        """'JJV' from 'JJV-MEI SUN': the office code our staff put in front."""
        found = _OFFICE_PREFIX.match(self.name or "")
        return found.group(1) if found else ""


def quoted_mails(body) -> list:
    """Every mail quoted under the selected one, with who sent it, when, and
    what it said. The header block Outlook writes - From / Sent /
    To / Cc / Subject - is read in any of the languages it comes in."""
    out = []
    current = None
    in_headers = False
    after_subject = False      # Gmail writes To / Cc after the Subject line
    for raw in (body or "").splitlines():
        line = raw.rstrip()
        if after_subject:
            after_subject = False
            to_line, cc_line = _TO_HEADER.match(line), _CC_HEADER.match(line)
            if to_line or cc_line:
                if to_line:
                    current.to = (current.to + " " + to_line.group("rest").strip()).strip()
                else:
                    current.cc = (current.cc + " " + cc_line.group("rest").strip()).strip()
                after_subject = True
                continue
        match = _QUOTED_FROM.match(line)
        if match:
            rest = match.group("rest").strip()
            addresses = _ADDRESS.findall(rest)
            name = re.sub(r"<[^>]*>|\[[^\]]*\]", "", rest)
            for address in addresses:
                name = name.replace(address, "")
            current = QuotedMail(name=" ".join(name.split()).strip(" ;,>"),
                                 address=addresses[0].lower() if addresses else "")
            out.append(current)
            in_headers = True
            continue
        if current is None:
            continue
        if in_headers:
            sent = _SENT_LINE.match(line)
            if sent:
                current.sent_at = parse_sent(sent.group("rest"))
                continue
            subject = _SUBJECT_LINE.match(line)
            if subject:
                current.subject = subject.group("rest").strip()
                in_headers = False
                after_subject = True
                continue
            to_line = _TO_HEADER.match(line)
            if to_line:
                current.to = (current.to + " " + to_line.group("rest").strip()).strip()
                continue
            cc_line = _CC_HEADER.match(line)
            if cc_line:
                current.cc = (current.cc + " " + cc_line.group("rest").strip()).strip()
                continue
            if _OTHER_HEADER.match(line) or not line.strip():
                continue
            in_headers = False
        text = line.strip()
        if text and len(current.text.splitlines()) < BODY_SCAN_LINES:
            current.text += text + "\n"
    return out


#: A forward (AL3): the prefixes a mail client puts on a forwarded mail,
#: and the line Gmail, Apple Mail or Outlook writes above the forwarded
#: header block in English, Spanish and Portuguese.
_FORWARD_PREFIX = re.compile(r"^[\s\u3000]*(FW|FWD|RV|ENC|TR)[\s\u3000]*[:\uff1a]", re.IGNORECASE)
_FORWARD_MARKER = re.compile(
    r"^\s*(?:-{2,}\s*)?(?:Forwarded message|Begin forwarded message|Mensaje reenviado|"
    r"Mensagem encaminhada|Mensaje original reenviado)\b", re.IGNORECASE | re.MULTILINE)


def is_forward(subject, body) -> bool:
    """A forwarded mail: its subject starts with FW / FWD / RV / ENC / TR
    (after any case number), or its body holds a forwarded-message line."""
    return bool(_FORWARD_PREFIX.match(strip_case_numbers(subject or "")) or
                _FORWARD_MARKER.search(body or ""))


# ------------------------------------------ the body scan: mode (5.2) --
#: Words that say how the cargo moves. Courier names (DHL, FedEx, UPS) are
#: deliberately absent: 'please check DHL as well' is an alternative, not
#: the mode asked for.
_AIR_WORDS = re.compile(
    r"\b(?:air\s*freight|airfreight|air\s*cargo|airline|awb|hawb|mawb|flight|by\s+air|"
    r"a[e\u00e9]reo|a[e\u00e9]rea)\b", re.IGNORECASE)
_OCEAN_WORDS = re.compile(
    r"\b(?:ocean\s*freight|sea\s*freight|seafreight|vessel|hbl|mbl|b/l|bl\s+fee|"
    r"mar[i\u00ed]timo|mar[i\u00ed]tima)\b", re.IGNORECASE)
_FCL_WORDS = re.compile(r"\bFCL\b", re.IGNORECASE)
_LCL_WORDS = re.compile(r"\bLCL\b", re.IGNORECASE)


def body_mode(lines) -> tuple:
    """(mode, line) from the words in the body: 'Air freight', 'Airline:',
    'AWB' -> Air; 'ocean freight', 'vessel', 'BL' -> FCL or LCL when either
    word is there, else 'Ocean' (the caller decides what to do with a mode
    the form does not have). Both families -> ('', '') - no guess.

    SA1: 'sea-air' (sea air, seaair, sea/air) -> Sea-Air ahead of both
    families ('sea and air' / 'air & sea' ask to quote both: not it);
    'cross border' -> Cross-border Truck, unless the text says IPI."""
    lines = list(lines)
    for line in lines:
        if _SEA_AIR.search(line):
            return "Sea-Air", " ".join(line.split())
    if not any(_IPI.search(line) for line in lines):
        for line in lines:
            if _CROSS_BORDER.search(line):
                return "Cross-border Truck", " ".join(line.split())
    air = ocean = None
    sub = None
    for line in lines:
        if air is None and _AIR_WORDS.search(line):
            air = line
        if ocean is None and (_OCEAN_WORDS.search(line) or _FCL_WORDS.search(line)
                              or _LCL_WORDS.search(line)):
            ocean = line
        if sub is None and _FCL_WORDS.search(line):
            sub = "FCL"
        if sub is None and _LCL_WORDS.search(line):
            sub = "LCL"
    if air and ocean:
        return "", ""
    if air:
        return "Air", " ".join(air.split())
    if ocean:
        return sub or "Ocean", " ".join(ocean.split())
    return "", ""


# --------------------------------- the body scan: packing lists (5.2) --
#: '1 Box', '2 pallets', '3 CTNS', 'Pcs: 8', '5 pcs, 41,5 kg ... each'
_PACK_WORDS = (r"(?:box(?:es)?|pcs?|pieces?|pc|ctns?|cartons?|crates?|pallets?|plts?|ptls?|"
               r"skids?|bultos?|cajas?|paletas?|piezas?|pkgs?|packages?|cases?|drums?|"
               r"bags?|rolls?|units?|colli|coli)")
_PIECES = re.compile(r"(?<![\w.,])(?P<n>\d{1,4})\s*(?:x\s*)?"
                     # '1 Wooden Case', '3 big bags' (2026-09-29): one word of kind before the packing
                     r"(?:(?:wooden|plywood|steel|metal|plastic|cardboard|big|jumbo|standard|euro|export|heavy)\s+)?"
                     r"(?P<w>" + _PACK_WORDS + r")\b",
                     re.IGNORECASE)
_PIECES_LABEL = re.compile(r"^\s*(?:pcs?|pieces?|qty|quantity|no\.?\s*of\s*(?:pcs|pieces|boxes|cartons))"
                           r"\s*:\s*(?P<n>\d{1,4})\b", re.IGNORECASE)
_NUM = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?(?!\d)|\d{1,4}(?:[.,]\d{1,3})?"
_DIM_UNIT = r"in(?:ch(?:es)?)?|\"|\u201d|cms?|mm|m"
#: 31" x 14" x 12", 70 x 55 x 48 cm, 110x90x75cm, 31in x 14in x 12in, and
#: (2026-09-24) '76*52*38(CM)' - the unit in parentheses after the triple
_DIMS = re.compile(
    r"(?P<l>" + _NUM + r")(?:\s*\(?\s*(?P<u1>" + _DIM_UNIT + r")\s*\)?)?\s*[xX\u00d7\*]\s*"
    r"(?P<w>" + _NUM + r")(?:\s*\(?\s*(?P<u2>" + _DIM_UNIT + r")\s*\)?)?\s*[xX\u00d7\*]\s*"
    r"(?P<h>" + _NUM + r")(?:\s*\(?\s*(?P<u3>" + _DIM_UNIT + r")\s*\)?)?(?![\w\"\u201d])",
    re.IGNORECASE)
#: '76*52*38(CM)*4', '110x90x75cm x 3': the count after the triple. It
#: counts only when the line states no other pieces count.
_DIMS_COUNT = re.compile(
    r"\s*[xX\u00d7\*]\s*(?P<n>\d{1,3})(?![\d.,])"
    r"(?!\s*(?:" + _DIM_UNIT + r"|kgs?|kilos?|lbs?|pounds?|cbm|m3)\b)", re.IGNORECASE)
#: What separates a label from its value: a colon, a tab (an Outlook table
#: row), or just the number that follows ('Gross weight 312.40 kg').
_LABEL_SEP = r"(?:\s*[:\uff1a]|\s*\t|\s+(?=[\d.]))"
_DIMS_LABEL = re.compile(r"^\s*(?:size|sizes|dimensions?|dims?|measurements?|medidas|dimens[o\u00f5]es)" + _LABEL_SEP,
                         re.IGNORECASE)
#: 'Weight:', 'Gross weight:', and (2026-09-24) 'Estimated weight:', 'Approx. weight:'
_WEIGHT_LABEL = re.compile(r"^\s*(?:(?:gross|total|net|cargo|estimated|est\.?|approx\.?|approximate)\s+)?"
                           r"(?:weight|peso|wt|g\.?\s?w\.?)" + _LABEL_SEP, re.IGNORECASE)
#: The sender's own chargeable figure (2026-09-24): never an actual weight,
#: kept to cross-check ours.
_CHARGEABLE_LABEL = re.compile(
    r"^\W{0,3}(?:chargeable(?:\s+weight|\s+wt\.?)?|volumetric(?:\s+weight)?|vol\.?\s*(?:weight|wt\.?)|volume\s+weight|"
    r"c\.?\s?w\.?|chg\.?\s*wt\.?)\b", re.IGNORECASE)
_NET_LABEL = re.compile(r"^\W{0,3}net\s+(?:weight|wt)\b", re.IGNORECASE)
_GROSS_LABEL = re.compile(r"^\W{0,3}(?:gross\s+(?:weight|wt)|g\.?\s?w\.?)\b", re.IGNORECASE)
_VOLUME_LABEL = re.compile(r"^\W{0,3}(?:total\s+)?(?:volume|vol\.?|cbms?|m3|volumen)" + _LABEL_SEP, re.IGNORECASE)
_CARGO_DETAILS_LABEL = re.compile(r"^\W{0,3}(?:cargo\s+details?|packing(?:\s+list)?|packages?)" + _LABEL_SEP, re.IGNORECASE)
#: A label on a line of its own - Outlook renders a table cell that way
#: ('Gross weight' / '312.40 kg'): the next non-empty line is its value.
_BARE_LABEL = re.compile(
    r"^\W{0,3}(?:(?:gross|net|total|cargo|estimated|approx\.?)\s+)?(?:weight|wt|g\.?w\.?|volume|vol\.?|cbm|"
    r"dimensions?|dims?|size|sizes|measurements?|chargeable(?:\s+weight)?|volumetric(?:\s+weight)?|c\.?w\.?|"
    r"pieces|pcs|quantity|qty|no\.?\s*of\s*(?:pcs|pieces|cartons|boxes)|cargo\s+details?|packages?|"
    r"commodity|commodities|goods|description)\s*[:\uff1a]?\s*$",
    re.IGNORECASE)
_WEIGHTS = re.compile(r"(?<![\w.,])(?P<v>" + _BIG_WEIGHT + r"|" + _NUM + r")\s*(?P<u>lbs?|pounds?|" + _KG_UNIT
                      + r"|kilos?|kilograms?)\b",
                      re.IGNORECASE)
#: 'CRATE 1 : DIM : ...', 'pallet 10 : 122x102x137 cm/ 247.7kgs' - one piece
#: named by its number (2026-09-24, the Hanoi mail).
_ITEM_HEAD = re.compile(r"^\W{0,3}(?P<w>" + _PACK_WORDS + r")\s*#?\s*(?P<n>\d{1,3})\s*[:\-\u2013]", re.IGNORECASE)
#: 'TOTAL: 03 CRATE / total GW : 2,142 KG', 'Total : 10 pallets/ 2913 kgs'
_TOTAL_LINE = re.compile(r"^\W{0,3}(?:sub\s*)?total\b", re.IGNORECASE)
_EACH_WORD = re.compile(r"\b(?:each|c/u|cada uno|cada una|cada|por pieza|por bulto|por volumen?|per piece|per pc|"
                        r"per pallet|per package|per box|per carton|per crate|per unit)\b", re.IGNORECASE)
#: '1.940 kgs', '3,027 LBS': a dot or comma before exactly three digits may
#: separate thousands (2026-09-29).
_TWO_READINGS = re.compile(r"^(?P<a>[1-9]\d{0,2})(?P<sep>[.,])(?P<b>\d{3})$")
#: A volume on a line: '6.05 cbm', '1,2 m3', '0.5 CBM each'.
_LINE_VOLUME = re.compile(r"(?<![\w.,])(?P<v>\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?(?!\d)|\d{1,4}(?:[.,]\d{1,4})?)\s*(?:cbm|m3|m\u00b3|cubic\s+met(?:er|re)s?)\b",
                          re.IGNORECASE)
#: 'Volume: 6.05', 'CBM: 2' - the number after a volume label with no unit behind it.
_VOLUME_AFTER_LABEL = re.compile(r"^\W{0,3}(?:total\s+)?(?:volume|vol\.?|cbms?|m3|volumen)\s*(?:\([^)]*\))?\s*[:\uff1a]?\s*"
                                 r"(?P<v>\d{1,4}(?:[.,]\d{1,4})?)\b", re.IGNORECASE)
#: 'Gross weight 512 kg, chargeable weight 688 kg': the sender's chargeable
#: figure inside a line (2026-09-29); taken out before the weights are read.
_INLINE_CHARGEABLE = re.compile(
    r"[,;/(]?\s*\b(?:chargeable|volumetric|vol\.)\s*(?:weight|wt\.?)?\s*[:=]?\s*"
    r"(?P<v>" + _BIG_WEIGHT + r"|\d{1,4}(?:[.,]\d{1,3})?)\s*(?P<u>lbs?|pounds?|kgs?|kilos?|kilograms?)\b\)?",
    re.IGNORECASE)
#: A plausible density for general cargo, kg per cbm - what settles a
#: number that reads two ways. cargo_checks reads the limits from Settings.
DENSITY_MIN, DENSITY_MAX = 30.0, 3000.0


def readings(text) -> tuple:
    """(plain, other) for a number as written: '1.940' -> (1.94, 1940.0),
    '1,940' -> (1940.0, 1.94); other is None when the number reads one way
    ('1.5', '36,5', '1,250.50', '0.597')."""
    text = (text or "").strip()
    plain = float(_number(text)) if text else None
    found = _TWO_READINGS.match(text)
    if not found:
        return plain, None
    if found.group("sep") == ".":
        return plain, float(found.group("a") + found.group("b"))
    return plain, float(found.group("a") + "." + found.group("b"))


def plausible(kg, cbm) -> bool:
    return bool(kg and cbm) and DENSITY_MIN <= kg / cbm <= DENSITY_MAX
_TO_CM = {"in": 2.54, "inch": 2.54, "inches": 2.54, "\"": 2.54, "\u201d": 2.54,
          "cm": 1.0, "mm": 0.1, "m": 100.0}
LBS_TO_KG = 0.45359237
#: The sender's chargeable weight and ours may differ by this much before a note.
CHARGEABLE_TOLERANCE = 0.02
#: IATA: one cubic metre of air cargo weighs 166.67 kg, i.e. cm3 / 6000.
VOLUMETRIC_DIVISOR = 6000


@dataclass
class PackingItem:
    pieces: int = 1
    word: str = ""
    dims: tuple | None = None          # (L, W, H) in cm
    dims_text: str = ""                # as written, '27 x 10 x 10 in'
    weight_kg: float | None = None     # for the whole item
    weight_text: str = ""              # as written, '7 lbs'
    weight_kind: str = ""              # 'gross', 'net' or '' - gross wins over net (2026-09-24)
    lines: list = field(default_factory=list)
    # v34 (2026-09-29): the numbers as written, for the Cargo card and its checks
    pieces_stated: bool = False
    dims_nums: tuple | None = None     # (L, W, H) as written
    dims_unit: str | None = None       # 'cm', 'in', 'mm', 'm'; None = no unit written
    weight_value: float | None = None  # in weight_unit, per piece when weight_each
    weight_unit: str = ""              # 'kg' or 'lbs'
    weight_each: bool = False
    weight_written: str = ""           # '1.940 Kgs'
    weight_alt: float | None = None    # the other reading of weight_written
    weight_reading: str = ""           # 'auto': the other reading was taken for its density
    cbm_value: float | None = None
    cbm_each: bool = False
    cbm_written: str = ""
    from_label: bool = False           # opened by a 'Gross weight: ...' line alone

    @property
    def cbm_known(self) -> float | None:
        """The item's volume: stated, else from the size."""
        if self.cbm_value is not None:
            return self.cbm_value * (self.pieces if self.cbm_each else 1)
        if self.dims:
            return self.pieces * self.dims[0] * self.dims[1] * self.dims[2] / 1e6
        return None

    @property
    def volumetric_kg(self) -> float | None:
        if not self.dims:
            return None
        l, w, h = self.dims
        return self.pieces * l * w * h / VOLUMETRIC_DIVISOR


@dataclass
class Packing:
    """A packing list read out of the body: pieces, dimensions, weights.

    `chargeable_kg` is max(actual, volumetric) over all items;
    `formula` says how it was arrived at, for the source label. When the
    numbers do not add up to a weight, `candidates` lists every number with
    its unit so the user can pick, and nothing is computed.
    """

    items: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    evidence: str = ""
    #: A 'Total :' line after the items (2026-09-24): what the sender adds
    #: up to, kept apart from the items so nothing is counted twice.
    total_pieces: int | None = None
    total_kg: float | None = None
    total_text: str = ""
    #: 'Chargeable weight 367.20 kg on volumetric' (2026-09-24): the sender's
    #: own figure, never counted as a weight; `chargeable_check` compares it
    #: with ours.
    stated_chargeable_kg: float | None = None
    stated_chargeable_text: str = ""
    #: v34 (2026-09-29): the total as written, every line a total was read
    #: from, and a volume stated for the block as a whole
    total_word: str = ""
    total_weight_value: float | None = None
    total_weight_unit: str = ""
    total_weight_written: str = ""
    total_cbm: float | None = None
    total_cbm_written: str = ""
    total_lines: list = field(default_factory=list)

    @property
    def chargeable_check(self) -> str:
        """'' when the sender stated no chargeable weight; otherwise whether
        theirs matches ours (within 2 %) or differs, in one clause."""
        if self.stated_chargeable_kg is None or self.chargeable_kg is None:
            return ""
        ours = self.chargeable_kg
        if abs(self.stated_chargeable_kg - ours) / max(ours, 0.001) <= CHARGEABLE_TOLERANCE:
            return "sender's chargeable {} kg matches".format(_fmt2(self.stated_chargeable_kg))
        return "sender's chargeable {} kg differs from ours ({} kg)".format(
            _fmt2(self.stated_chargeable_kg), _fmt1(ours))

    @property
    def is_packing_list(self) -> bool:
        """Pieces or dimensions were stated - not just a weight line, which
        parse_body_qty already reads as 'Air:161'."""
        return any(i.dims or i.word for i in self.items)

    @property
    def actual_kg(self) -> float | None:
        weights = [i.weight_kg for i in self.items if i.weight_kg is not None]
        return sum(weights) if weights else None

    @property
    def volumetric_kg(self) -> float | None:
        vols = [i.volumetric_kg for i in self.items if i.volumetric_kg is not None]
        return sum(vols) if vols else None

    @property
    def chargeable_kg(self) -> float | None:
        values = [v for v in (self.actual_kg, self.volumetric_kg) if v is not None]
        return max(values) if values else None

    @property
    def cbm(self) -> float | None:
        """Sum of pieces x L x W x H over the items with a unit, in cbm
        (2026-09-24); None when no item has one."""
        vols = [i.pieces * i.dims[0] * i.dims[1] * i.dims[2] / 1e6 for i in self.items if i.dims]
        return sum(vols) if vols else None

    @property
    def cbm_formula(self) -> str:
        """'8 x 120 x 100 x 100 cm = 9.6 cbm', the items joined with ' + '."""
        if self.cbm is None:
            return ""
        bits = ["{}{}".format("{} x ".format(i.pieces) if i.pieces != 1 else "", i.dims_text)
                for i in self.items if i.dims]
        return "{} = {} cbm".format(" + ".join(bits), _fmt2(self.cbm))

    @property
    def pieces(self) -> int | None:
        counted = [i.pieces for i in self.items if i.word or i.dims or i.weight_kg is not None]
        return sum(counted) if counted else None

    @property
    def qty_raw(self) -> str:
        parts = []
        for item in self.items:
            piece = "{} {}".format(item.pieces, item.word or "pcs")
            if item.dims_text:
                piece += " " + item.dims_text
            if item.weight_text:
                piece += ", " + item.weight_text
            parts.append(piece)
        # v33: the text as received - no computed words ('volumetric N kg'
        # belongs in the formula, not in qty_raw)
        return " / ".join(parts)

    @property
    def formula(self) -> str:
        bits = []
        if self.actual_kg is not None:
            bits.append("actual {} kg".format(_fmt1(self.actual_kg)))
        if self.volumetric_kg is not None:
            dims = " + ".join(
                "{}{} = {} cm3".format(
                    "{} x ".format(i.pieces) if i.pieces != 1 else "",
                    i.dims_text, _fmt1(i.pieces * i.dims[0] * i.dims[1] * i.dims[2]))
                for i in self.items if i.dims)
            bits.append("volumetric {} / {} = {} kg".format(dims, VOLUMETRIC_DIVISOR, _fmt1(self.volumetric_kg)))
        return "chargeable = max({})".format(", ".join(bits)) if len(bits) > 1 else \
            (bits[0] if bits else "")


def _fmt1(value: float) -> str:
    text = "{:.1f}".format(value).rstrip("0").rstrip(".")
    return text or "0"


def _fmt2(value: float) -> str:
    text = "{:.2f}".format(value).rstrip("0").rstrip(".")
    return text or "0"


def join_label_values(lines) -> list:
    """A bare label line ('Gross weight') joined with the next non-empty
    line ('312.40 kg') as 'Gross weight: 312.40 kg' (2026-09-24)."""
    out = []
    pending = None
    for line in lines:
        text = " ".join((line or "").split())
        if not text:
            continue
        if pending is not None:
            out.append("{}: {}".format(pending, text))
            pending = None
            continue
        if _BARE_LABEL.match(text):
            pending = text.rstrip(" :\uff1a")
            continue
        out.append(text)
    if pending is not None:
        out.append(pending)
    return out


def _is_labelled(line: str) -> bool:
    """A line of the labelled kind (Gross weight / Volume / Dimensions /
    Chargeable weight / Pieces / Cargo details): such lines describe one
    consignment and never open a second item (2026-09-24)."""
    return bool(_DIMS_LABEL.match(line) or _WEIGHT_LABEL.match(line) or _PIECES_LABEL.match(line)
                or _CHARGEABLE_LABEL.match(line) or _VOLUME_LABEL.match(line) or _CARGO_DETAILS_LABEL.match(line))


def dims_from_text(text) -> tuple:
    """(L, W, H) in cm and the dims as written, off a typed '120 x 100 x
    100 cm'; (None, text) when there is no unit or no triple."""
    text = " ".join((text or "").split())
    found = _DIMS.search(text)
    if not found:
        return None, text
    return _dims_cm(found), _dims_text(found) or text


def _unit_written(unit: str) -> str:
    """'KGSKG' -> 'kg'; 'kgs', 'lbs' as written."""
    unit = (unit or "").lower()
    return "kg" if unit.startswith("kg") and len(unit) > 3 else unit


def _kg(value: str, unit: str) -> float:
    kilos = float(_number(value))
    if unit.lower().startswith(("lb", "pound")):
        kilos *= LBS_TO_KG
    return kilos


def _dims_cm(found) -> tuple | None:
    """(L, W, H) in cm, or None when no unit is written anywhere."""
    unit = (found.group("u3") or found.group("u2") or found.group("u1") or "").lower()
    if not unit:
        return None
    factor = _TO_CM.get(unit) or _TO_CM.get(unit[:2]) or (2.54 if unit.startswith("in") else None)
    if factor is None:
        return None
    return tuple(float(_number(found.group(k))) * factor for k in ("l", "w", "h"))


def _dims_text(found) -> str:
    unit = (found.group("u3") or found.group("u2") or found.group("u1") or "").lower()
    if unit in ("\"", "\u201d") or unit.startswith("in"):
        unit = "in"
    return "{} x {} x {} {}".format(_number(found.group("l")), _number(found.group("w")),
                                    _number(found.group("h")), unit).strip()


def _unit_of_dims(found) -> str | None:
    unit = (found.group("u3") or found.group("u2") or found.group("u1") or "").lower()
    if not unit:
        return None
    if unit in ("\"", "\u201d") or unit.startswith("in"):
        return "in"
    if unit.startswith("cm"):
        return "cm"
    return unit if unit in ("mm", "m") else None


def _weight_as_written(found):
    """(value, unit, written, other reading) of one _WEIGHTS match."""
    plain, other = readings(found.group("v"))
    unit = "lbs" if found.group("u").lower().startswith(("lb", "pound")) else "kg"
    return plain, unit, " ".join(found.group(0).split()), other


def _volume_as_written(line):
    """(value, written, other reading) of the volume on a line, or None."""
    found = _LINE_VOLUME.search(line) or _VOLUME_AFTER_LABEL.match(line)
    if not found:
        return None
    plain, other = readings(found.group("v"))
    return plain, " ".join(found.group(0).split()), other


def _item_kg(item) -> float | None:
    """The item's kg from what was written; None when the weight is per
    piece and the pieces are not known (rule 4: kept, not counted)."""
    if item.weight_value is None:
        return None
    kilos = item.weight_value * (LBS_TO_KG if item.weight_unit == "lbs" else 1.0)
    if item.weight_each:
        if not item.pieces_stated:
            return None
        kilos *= item.pieces
    return kilos


def _settle_reading(item) -> None:
    """A weight that reads two ways takes the reading whose density is
    plausible when the volume is known (rule 5); otherwise the plain one
    stays and the Cargo card asks."""
    if item.weight_alt is None or item.weight_value is None:
        return
    cbm = item.cbm_known
    if not cbm:
        return
    plain_kg = _item_kg(item)
    item.weight_value, item.weight_alt = item.weight_alt, item.weight_value
    other_kg = _item_kg(item)
    item.weight_value, item.weight_alt = item.weight_alt, item.weight_value
    if plain_kg is None or other_kg is None:
        return
    if not plausible(plain_kg, cbm) and plausible(other_kg, cbm):
        item.weight_value, item.weight_alt = item.weight_alt, item.weight_value
        item.weight_reading = "auto"


def _settle_total_reading(result, items) -> None:
    """A total that reads two ways ('1.940 Kgs' over lines of 1,939 kg)
    takes the reading nearer the lines' sum; with no weights on the lines,
    the one whose density is plausible for the volume (rule 5)."""
    if result.total_weight_value is None or not result.total_weight_written:
        return
    found = re.match(r"[\d.,]+", result.total_weight_written)
    other = readings(found.group(0))[1] if found else None
    if other is None:
        return
    factor = LBS_TO_KG if result.total_weight_unit == "lbs" else 1.0
    plain_kg, other_kg = result.total_weight_value * factor, other * factor
    weights = [i.weight_kg for i in items if i.weight_kg is not None]
    take_other = False
    if weights:
        lines_kg = sum(weights)
        take_other = abs(other_kg - lines_kg) < abs(plain_kg - lines_kg)
    else:
        cbm = result.total_cbm or sum(i.cbm_known or 0 for i in items) or None
        take_other = bool(cbm) and not plausible(plain_kg, cbm) and plausible(other_kg, cbm)
    if take_other:
        result.total_weight_value, result.total_kg = other, other_kg


def parse_packing(lines) -> Packing:
    """Read '1 Crate / Size: 31" x 14" x 12" / Weight: 9LBS' and '5 pcs, 41,5
    kg - 70 x 55 x 48 cm each' into items with a chargeable weight.

    A pieces count starts an item; a size or weight line attaches to the
    current item (or starts one of a single piece). 'each' makes the weight
    per piece; otherwise it is the line's total. Several kilo values on one
    weight line ('9LBS / 4kgs / 6kgs') keep the largest.

    v34 (2026-09-29): every item keeps its numbers as written. A 'Total'
    line is the sender's total wherever it stands, and a lone 'Gross
    weight' / 'Volume' line is one when the mail also lists items: the
    lines are the cargo, the total only checks them (rule 2). The
    'chargeable weight' a line states is set aside, never a weight.
    """
    lines = flatten_vertical_tables(lines)
    result = Packing()
    current = None
    touched = []
    loose_cbm = []                                 # (value, written, other, line) on lines of their own
    for line in join_label_values(lines):
        if _CHARGEABLE_LABEL.match(line):
            # the sender's chargeable figure: kept apart, never a weight
            found = list(_WEIGHTS.finditer(line))
            if found and result.stated_chargeable_kg is None:
                result.stated_chargeable_kg = max(_kg(w.group("v"), w.group("u")) for w in found)
                result.stated_chargeable_text = " ".join(line.split())
            continue
        inline = _INLINE_CHARGEABLE.search(line)
        if inline:
            if result.stated_chargeable_kg is None:
                result.stated_chargeable_kg = _kg(inline.group("v"), inline.group("u"))
                result.stated_chargeable_text = " ".join(line.split())
            line = (line[:inline.start()] + line[inline.end():]).strip(" ,;")
        pieces = _PIECES_LABEL.match(line) or _PIECES.search(line)
        head = _ITEM_HEAD.match(line) if pieces is None else None
        dims = _DIMS.search(line)
        weights = list(_WEIGHTS.finditer(line))
        volume = _volume_as_written(line)
        labelled = _is_labelled(line)
        if _TOTAL_LINE.match(line) and (pieces or weights or volume):
            # 'Total : 10 pallets / 2913 kgs', 'Total Packages: 8': the sender's
            # sum wherever it stands - a check on the items, never one of them
            if pieces and result.total_pieces is None:
                result.total_pieces = int(pieces.group("n"))
                if not _PIECES_LABEL.match(line):
                    result.total_word = pieces.group("w").lower()
            if weights and result.total_kg is None:
                best = max(weights, key=lambda w: _kg(w.group("v"), w.group("u")))
                value, unit, written, _other = _weight_as_written(best)
                result.total_kg = _kg(best.group("v"), best.group("u"))
                result.total_weight_value, result.total_weight_unit = value, unit
                result.total_weight_written = written
            if volume and result.total_cbm is None:
                result.total_cbm, result.total_cbm_written = volume[0], volume[1]
            result.total_text = " ".join(line.split())
            result.total_lines.append(" ".join(line.split()))
            current = None
            continue
        if volume and not (pieces or head or dims or weights):
            # 'Volume: 6.05 cbm' on its own: the item's when there is one,
            # the block's otherwise - settled at the end
            loose_cbm.append(volume + (" ".join(line.split()),))
            continue
        if not (pieces or head or dims or weights or labelled):
            continue
        if not (pieces or head or dims or weights):
            touched.append(line)                  # 'Size:' with nothing readable
            if current is not None:
                current.lines.append(" ".join(line.split()))
            continue
        if pieces and current is not None and current.pieces_stated and int(pieces.group("n")) == current.pieces \
                and (not current.word or not _PIECES_LABEL.match(line)
                     and current.word.rstrip("s") == pieces.group("w").lower().rstrip("s")):
            # '63 cartons' again on the Dimensions line: the same count never
            # opens a second item (2026-09-24)
            if not current.word and not _PIECES_LABEL.match(line):
                current.word = pieces.group("w").lower()
            current.pieces_stated = True
            pieces = None
        elif pieces and labelled and current is not None and current.weight_kg is None and current.dims is None:
            # a labelled 'Pieces: 63' inside the block of an item begun by
            # another label: the count belongs to it
            current.pieces = int(pieces.group("n"))
            current.pieces_stated = True
            current.from_label = False
            if not _PIECES_LABEL.match(line):
                current.word = pieces.group("w").lower()
            pieces = None
        # a count line ('3 Wooden Case') takes the size that follows - unless
        # that line is complete, with its own count, size and weight (13b)
        if pieces and not (dims and current and current.dims is None and not current.weight_text
                           and not pieces.group(0).strip().isdigit() and not weights):
            current = PackingItem(pieces=int(pieces.group("n")),
                                  word=(pieces.group("w").lower()
                                        if not _PIECES_LABEL.match(line) else "pcs"),
                                  pieces_stated=True)
            result.items.append(current)
        elif head:
            # 'CRATE 1 : ...', 'pallet 7 : ...': one piece, named by number
            current = PackingItem(pieces=1, word=head.group("w").lower(), pieces_stated=True)
            result.items.append(current)
        if (dims or weights) and current is None:
            current = PackingItem(from_label=bool(weights and not dims and labelled))
            result.items.append(current)
        if dims and current.dims is None and not current.dims_text:
            current.dims = _dims_cm(dims)
            current.dims_text = _dims_text(dims)
            current.dims_nums = tuple(float(_number(dims.group(k))) for k in ("l", "w", "h"))
            current.dims_unit = _unit_of_dims(dims)
            current.from_label = False
            if current.dims is None:
                touched.append(line)
            elif not pieces and not head and not current.word:
                # '76*52*38(CM)*4': the count after the triple, when the
                # line counts the pieces no other way
                count = _DIMS_COUNT.match(line, dims.end())
                if count:
                    current.pieces = int(count.group("n"))
                    current.pieces_stated = True
        kind = "net" if _NET_LABEL.match(line) else ("gross" if _GROSS_LABEL.match(line) else "")
        if weights and current.weight_kg is not None and current.weight_kind == "net" and kind == "gross":
            current.weight_kg = None                  # gross wins over net (2026-09-24)
            current.weight_value = None
        if weights and current.weight_kg is None and current.weight_value is None:
            current.weight_kind = kind
            best = max(weights, key=lambda w: _kg(w.group("v"), w.group("u")))
            (current.weight_value, current.weight_unit, current.weight_written,
             current.weight_alt) = _weight_as_written(best)
            current.weight_each = bool(_EACH_WORD.search(line))
            kilos = max(_kg(w.group("v"), w.group("u")) for w in weights)
            written = " / ".join("{} {}".format(_number(w.group("v")), _unit_written(w.group("u")))
                                 for w in weights)
            in_lbs = all(w.group("u").lower().startswith(("lb", "pound")) for w in weights)
            if current.weight_each:
                kilos *= current.pieces
                written += " each"
            if in_lbs or len(weights) > 1 or (current.weight_each and current.pieces != 1):
                written += " ({} kg)".format(_fmt1(kilos))
            current.weight_kg = kilos
            current.weight_text = written
        if volume and current.cbm_value is None:
            current.cbm_value, current.cbm_written, _other = volume
            current.cbm_each = bool(_EACH_WORD.search(line[_LINE_VOLUME.search(line).end():])
                                    if _LINE_VOLUME.search(line) else False)
        current.lines.append(" ".join(line.split()))
        if (pieces or head) and dims and weights and not labelled:
            current = None                        # a complete line: next line is a new item
    items = [i for i in result.items if i.dims or i.weight_kg is not None or i.word or i.dims_nums]
    # a lone 'Gross weight: 487 kg' beside listed items is their total (rule 2)
    listed = [i for i in items if i.pieces_stated or i.dims_nums or i.word]
    if listed and len(listed) < len(items):
        for item in items:
            if item in listed:
                continue
            if item.weight_value is not None and result.total_kg is None:
                result.total_kg = _item_kg(item)
                result.total_weight_value, result.total_weight_unit = item.weight_value, item.weight_unit
                result.total_weight_written = item.weight_written
            result.total_lines.extend(item.lines)
        items = listed
    # '4 Wooden Case' after four listed cases: a count of them, not a fifth line
    counted_only = [i for i in items if i.pieces_stated and not i.dims_nums and i.weight_value is None
                    and i.cbm_value is None]
    for item in counted_only:
        others = [i for i in items if i is not item]
        if others and item.pieces == sum(i.pieces for i in others) and any(i.dims_nums or i.weight_value is not None
                                                                           for i in others):
            if result.total_pieces is None:
                result.total_pieces, result.total_word = item.pieces, item.word
            result.total_lines.extend(item.lines)
            items = others
    # a volume on a line of its own: the one item's, else the block's
    for value, written, _other, text in loose_cbm:
        if len(items) == 1 and items[0].cbm_value is None:
            items[0].cbm_value, items[0].cbm_written = value, written
            items[0].lines.append(text)
        elif result.total_cbm is None:
            result.total_cbm, result.total_cbm_written = value, written
            result.total_lines.append(text)
    if not items and (result.total_pieces is not None or result.total_kg is not None or result.total_cbm is not None):
        # only a total: it is the cargo
        item = PackingItem(pieces=result.total_pieces or 1, word=result.total_word or ("pcs" if result.total_pieces else ""),
                           pieces_stated=result.total_pieces is not None,
                           weight_value=result.total_weight_value, weight_unit=result.total_weight_unit or "kg",
                           weight_written=result.total_weight_written,
                           cbm_value=result.total_cbm, cbm_written=result.total_cbm_written,
                           lines=list(result.total_lines))
        if result.total_weight_written:
            item.weight_alt = readings(re.match(r"[\d.,]+", result.total_weight_written).group(0))[1]
        item.weight_kg = _item_kg(item)
        items = [item]
        result.total_pieces = result.total_kg = result.total_cbm = None
        result.total_weight_value = None
        result.total_weight_written = result.total_cbm_written = result.total_word = ""
        result.total_lines, result.total_text = [], ""
    for item in items:
        _settle_reading(item)
        item.weight_kg = _item_kg(item)
    # '8 packages, 7605 lbs' above eight packages that add up to it: their
    # total, though the word 'Total' is missing (rule 2)
    if len(items) >= 3 and result.total_kg is None and result.total_pieces is None:
        for item in items:
            others = [i for i in items if i is not item]
            weights = [i.weight_kg for i in others if i.weight_kg is not None]
            if not item.pieces_stated or item.dims_nums or item.weight_kg is None or len(weights) != len(others):
                continue
            if item.pieces == sum(i.pieces for i in others) and \
                    abs(item.weight_kg - sum(weights)) <= max(0.5, 0.01 * item.weight_kg):
                result.total_pieces, result.total_word = item.pieces, item.word
                result.total_kg = item.weight_kg
                result.total_weight_value, result.total_weight_unit = item.weight_value, item.weight_unit
                result.total_weight_written = item.weight_written
                result.total_lines.extend(item.lines)
                items = others
                break
    _settle_total_reading(result, items)
    result.items = items
    if items:
        result.evidence = items[0].lines[0] if items[0].lines else ""
        if len(items) > 1:
            result.evidence += " (+{} more item{})".format(len(items) - 1, "s" if len(items) > 2 else "")
    if touched or (items and result.chargeable_kg is None):
        # numbers that could not be turned into a weight: list them all
        seen = []
        for line in touched + [l for i in items for l in i.lines]:
            for found in re.finditer(
                    r"(?<![\w.,])(" + _NUM + r")\s*(in(?:ch(?:es)?)?|\"|\u201d|cm|mm|m|kgs?|kilos?|lbs?|pounds?|"
                    + _PACK_WORDS + r")?(?![\w\"\u201d])", line, re.IGNORECASE):
                unit = (found.group(2) or "").lower()
                unit = "in" if unit in ("\"", "\u201d") else unit
                token = (_number(found.group(1)) + " " + unit).strip()
                if token not in seen:
                    seen.append(token)
        result.candidates = seen
    return result


# ------------------------------ the body scan: address blocks (5.2) --
_PICKUP_LABEL = re.compile(
    r"^\W{0,3}(?:pick[\s-]*up(?:\s+address|\s+from|\s+at|\s+location)?|collection(?:\s+address)?|"
    r"collect\s+from|ship\s+from|shipper\s+address|origin\s+address|direcci[o\u00f3]n\s+de\s+(?:origen|retiro)|"
    r"retiro(?:\s+en)?)\s*[:\uff1a]?\s*(?P<rest>.*)$", re.IGNORECASE)
_DELIVERY_LABEL = re.compile(
    r"^\W{0,3}(?:deliver(?:y)?(?:\s+address|\s+to|\s+at)?|ship\s+to|consignee\s+address|"
    r"final\s+destination(?:\s+address)?|door\s+delivery(?:\s+address)?|"
    r"destination\s+address|direcci[o\u00f3]n\s+de\s+(?:entrega|destino)|entrega(?:\s+en)?)"
    r"\s*[:\uff1a]?\s*(?P<rest>.*)$", re.IGNORECASE)
#: 'POD CHICAGO', 'Destination : Cebu airport, Philippines' - where a
#: cargo line goes when no delivery address is written (2026-09-24)
_POD_LABEL = re.compile(
    r"^\W{0,3}(?:POD|destination|final\s+destination|port\s+of\s+discharge|discharge\s+port|"
    r"place\s+of\s+delivery|deliver(?:y)?\s+to)\s*[:\-\uff1a]?\s*(?P<rest>\S.*)$", re.IGNORECASE)
_POD_LEAD = re.compile(r"^(?:by\s+(?:air|sea|ocean|truck|road|rail)\s+)?to\s+", re.IGNORECASE)
#: 'Flagstaff, AZ 86001', 'Addison, IL 60101-4410': a US state and ZIP.
US_STATE_ZIP = re.compile(
    r"\b(?:A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[DLNA]|K[SY]|LA|M[EDAINSOT]|N[EVHJMYCD]|O[HKR]|PA|RI|"
    r"S[CD]|T[NX]|UT|V[TA]|W[AVIY])\s+\d{5}(?:-\d{4})?\b")
_COMPANY = re.compile(
    r"\b(?:corporation|corp\.?|inc\.?|incorporated|llc|l\.l\.c\.|ltd\.?|limited|co\.|company|"
    r"s\.?a\.?(?:\s+de\s+c\.?v\.?)?|ltda\.?|gmbh|spa|s\.p\.a\.|sac|s\.a\.c\.|sas|s\.a\.s\.|"
    r"srl|s\.r\.l\.|plc|bv|b\.v\.|nv|n\.v\.|ag|pty|pte)\b\.?$", re.IGNORECASE)
_ADDRESS_MAX_LINES = 5
#: Where the address stops and the letter resumes.
_SENTENCE_START = re.compile(
    r"^\W{0,3}(?:hours|if you|thank|please|regards|best|kind|saludos|atentamente|cordial|"
    r"looking forward|let me know|i[\u2019'\u00b4`]?\s?(?:ll|will|am|would)|we\b|our\b)", re.IGNORECASE)
#: A postal code of four to six digits ends an address line anywhere.
_POSTAL_CODE = re.compile(r"(?<!\d)\d{4,6}(?:-\d{3,4})?(?!\d)\s*(?P<tail>(?:,?\s*[A-Za-z .\u00c0-\u00ff]{2,30})?)\s*$")
#: '2716 S Quarry Loop' ends in a street, not in a postal code and country.
_STREET_WORD = re.compile(
    r"\b(?:st|street|ave|avenue|av|rd|road|loop|blvd|boulevard|dr|drive|ln|lane|way|ste|suite|"
    r"hwy|highway|pkwy|parkway|ct|court|pl|place|cir|circle|calle|avenida|rua|floor|fl|unit|bldg)\b\.?",
    re.IGNORECASE)


def _ends_with_postal(line: str) -> bool:
    found = _POSTAL_CODE.search(line or "")
    if not found:
        return False
    tail = found.group("tail") or ""
    return not (_STREET_WORD.search(tail) or len(tail.split()) > 3)
#: 'Pick up USD 85 + IVA (16%)' is a charge, not an address.
_MONEY = re.compile(r"(?:\bUSD\b|\bEUR\b|\bIVA\b|\$|%|\d+[.,]\d{2}\s*$)", re.IGNORECASE)


def parse_addresses(lines) -> dict:
    """{'origin': {...}, 'dest': {...}} from 'Pick-up:' / 'Delivery to' blocks.

    The address runs from the label to the line with a state and ZIP (or
    for at most five lines, stopping at the next 'Label:' line). A first
    line that ends in a legal form is the company - the end customer. A US
    state and ZIP make the country USA; anything else leaves it unknown.
    """
    out = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        side = None
        for name, pattern in (("origin", _PICKUP_LABEL), ("dest", _DELIVERY_LABEL)):
            found = pattern.match(line)
            if found:
                side = name
                rest = found.group("rest").strip(" :-")
                break
        if side is None or side in out or (rest and _MONEY.search(rest)):
            index += 1
            continue
        block = [rest] if rest else []
        stop = bool(rest and US_STATE_ZIP.search(rest))
        cursor = index + 1
        while not stop and cursor < len(lines) and len(block) < _ADDRESS_MAX_LINES:
            nxt = lines[cursor].strip()
            if _LABEL_LINE.match(nxt) or _PICKUP_LABEL.match(nxt) or _DELIVERY_LABEL.match(nxt) \
                    or _SENTENCE_START.match(nxt) or _MONEY.search(nxt):
                break
            block.append(nxt)
            cursor += 1
            if US_STATE_ZIP.search(nxt) or _ends_with_postal(nxt):
                stop = True
        block = [" ".join(b.strip(" *-•	>").split()) for b in block if b.strip(" *-•	>")]
        if not block:
            index += 1
            continue
        company = block[0] if _COMPANY.search(block[0]) else ""
        country = "USA" if any(US_STATE_ZIP.search(b) for b in block) else ""
        out[side] = {"text": ", ".join(block), "company": company, "country": country,
                     "line": " ".join(line.split())}
        index = max(cursor, index + 1)
    return out


# -------------------------------------- the body scan: terms lines (5.2) --
_TERMS_LINE = re.compile(
    r"\b(?:terms?|incoterms?|door[\s-]*to[\s-]*door|d2d|airport[\s-]*to[\s-]*airport|"
    r"port[\s-]*to[\s-]*port|door[\s-]*to[\s-]*(?:airport|port)|(?:airport|port)[\s-]*to[\s-]*door)\b",
    re.IGNORECASE)


TERMS_LINE_MAX = 120


def terms_lines(lines) -> list:
    """'Terms: door-to-door please': a service scope, not an incoterm. The
    lines are returned for the notes; no incoterm is filled from them.
    Only a short line counts - a paragraph of conditions is not a term."""
    out = []
    for line in lines:
        if len(line.strip()) > TERMS_LINE_MAX:
            continue
        if _TERMS_LINE.search(line) and not any(
                re.search(r"\b{}\b".format(w), line.upper()) for w in INCOTERM_WORDS):
            text = " ".join(line.split())
            if text not in out:
                out.append(text)
    return out


# ------------------------------------ the body scan: cargo labels (5.2) --
#: 'Total Cartons: 18', 'Gross Weight (KGs): 265', 'Volume (CBMs): 1.2840',
#: 'Chargeable: 265.00' - the labelled form an agent's request form writes.
_CARGO_NUM = r"(?P<v>\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?(?!\d)|\d{1,7}(?:[.,]\d{1,4})?)"
_CARGO_PIECES = re.compile(
    r"^\W{0,3}(?:total\s+|no\.?\s*of\s+|number\s+of\s+)?(?P<w>cartons?|ctns?|pieces?|pcs|pallets?|plts?|"
    r"boxes?|packages?|pkgs?|units?|colli|skids?|crates?)\s*(?:\([^)]*\))?\s*:\s*" + _CARGO_NUM,
    re.IGNORECASE)
_CARGO_WEIGHT = re.compile(
    r"^\W{0,3}(?:total\s+|gross\s+|net\s+)?(?:weight|g\.?\s?w\.?|gw|wt|peso(?:\s+bruto)?)\s*"
    r"(?:\((?P<u1>kgs?|kilos?|lbs?|pounds?)\))?\s*:\s*" + _CARGO_NUM + r"\s*(?P<u2>kgs?|kilos?|lbs?|pounds?)?",
    re.IGNORECASE)
_CARGO_VOLUME = re.compile(
    r"^\W{0,3}(?:total\s+)?(?:volume|vol\.?|cbms?|m3|volumen)\s*(?:\([^)]*\))?\s*:\s*" + _CARGO_NUM,
    re.IGNORECASE)
_CARGO_CHARGEABLE = re.compile(
    r"^\W{0,3}(?:chargeable(?:\s+weight)?|c\.?\s?w\.?|chg\.?\s*wt\.?)\s*(?:\([^)]*\))?\s*:\s*"
    + _CARGO_NUM + r"\s*(?P<u2>kgs?|kilos?|lbs?|pounds?)?", re.IGNORECASE)
#: IATA: one cubic metre is 166.67 kg of air cargo.
CBM_TO_KG = 1000.0 / 6


@dataclass
class CargoLabels:
    pieces: int | None = None
    piece_word: str = ""
    weight_kg: float | None = None
    volume_cbm: float | None = None
    chargeable_kg: float | None = None
    chargeable_stated: bool = False
    lines: list = field(default_factory=list)

    @property
    def found(self) -> bool:
        """A labelled form, not just a weight line: pieces, a volume or a
        chargeable weight were stated (a lone 'Weight: 450 kg' is read the
        old way, as 'Air:450')."""
        return self.chargeable_kg is not None and (
            self.pieces is not None or self.volume_cbm is not None or self.chargeable_stated)

    @property
    def qty_raw(self) -> str:
        parts = []
        if self.pieces is not None:
            parts.append("{} {}".format(self.pieces, self.piece_word))
        if self.weight_kg is not None:
            parts.append("{} kg".format(_fmt1(self.weight_kg)))
        if self.volume_cbm is not None:
            parts.append("{} cbm".format(_fmt4(self.volume_cbm)))
        # v34 (2026-09-29): the text as received - a chargeable weight is a
        # figure to compare (the stated total), never part of the quantity,
        # and never on an ocean case
        return ", ".join(parts)

    @property
    def formula(self) -> str:
        if self.chargeable_stated:
            return "chargeable weight as stated"
        bits = []
        if self.weight_kg is not None:
            bits.append("actual {} kg".format(_fmt1(self.weight_kg)))
        if self.volume_cbm is not None:
            bits.append("volumetric {} cbm x {} = {} kg".format(
                _fmt4(self.volume_cbm), _fmt1(CBM_TO_KG), _fmt1(self.volume_cbm * CBM_TO_KG)))
        return "chargeable = max({})".format(", ".join(bits)) if len(bits) > 1 else (bits[0] if bits else "")


def _fmt4(value: float) -> str:
    text = "{:.4f}".format(value).rstrip("0").rstrip(".")
    return text or "0"


def parse_cargo_labels(lines, style=None) -> CargoLabels:
    """Pieces, weight, volume and chargeable weight off labelled lines. A
    stated chargeable weight is taken as is; otherwise chargeable =
    max(actual, cbm x 166.67). Pounds are turned into kilos. A weight or
    volume like '9.100' reads as number_style() says (13g)."""
    if style is None:
        style = number_style("\n".join(lines))
    result = CargoLabels()
    order = ("carton", "ctn", "piece", "pc", "pallet", "plt", "box", "package", "pkg",
             "unit", "colli", "skid", "crate")
    best_rank = len(order)
    for line in lines:
        found = _CARGO_PIECES.match(line)
        if found:
            word = found.group("w").lower()
            rank = next((i for i, w in enumerate(order) if word.startswith(w)), len(order))
            if rank < best_rank:
                best_rank = rank
                result.pieces = int(float(_number(found.group("v"))))
                result.piece_word = word if word.endswith("s") or word in ("pcs", "colli") else word + "s"
            result.lines.append(" ".join(line.split()))
            continue
        found = _CARGO_CHARGEABLE.match(line)
        if found and result.chargeable_kg is None:
            result.chargeable_kg = _kg(found.group("v"), found.group("u2") or "kg")
            result.chargeable_stated = True
            result.lines.append(" ".join(line.split()))
            continue
        found = _CARGO_WEIGHT.match(line)
        if found and result.weight_kg is None:
            result.weight_kg = _kg(settled_number(found.group("v"), style)[0],
                                   found.group("u2") or found.group("u1") or "kg")
            result.lines.append(" ".join(line.split()))
            continue
        found = _CARGO_VOLUME.match(line)
        if found and result.volume_cbm is None:
            result.volume_cbm = float(settled_number(found.group("v"), style)[0])
            result.lines.append(" ".join(line.split()))
    if result.chargeable_kg is None and (result.weight_kg is not None or result.volume_cbm is not None):
        candidates = [v for v in (result.weight_kg,
                                  result.volume_cbm * CBM_TO_KG if result.volume_cbm is not None else None)
                      if v is not None]
        result.chargeable_kg = max(candidates)
    return result


# ------------------------- the body scan: one block per shipment (5.2) --
#: '1/ Shipment 1 :', 'Shipment 2:', 'Quote 02', 'Option A' - a heading
#: that opens one consignment of several in the same mail (2026-09-24).
_BLOCK_WORD_HEAD = re.compile(
    r"^\W{0,3}(?:\d{1,2}\s*[./)\-:]\s*)?(?P<label>(?:shipment|shpt|quote|option|lot)\s*#?\s*(?:\d{1,3}|[A-Z]))\b",
    re.IGNORECASE)
#: '1.Please price the ...', '2) ...' - numbered paragraphs
_BLOCK_NUM_HEAD = re.compile(r"^\W{0,2}(?P<n>\d{1,2})\s*[.)/]\s*(?=[A-Za-z(])")
#: 'Term : FOB in USA', 'Add : Addison, IL 60101 USA' - lines kept verbatim
#: in a block's notes, because they say what differs between the blocks
_BLOCK_NOTE_LINE = re.compile(r"^\W{0,3}(?:terms?|incoterms?|add|addr|address|pick[\s-]*up)\s*[:\uff1a]\s*\S",
                              re.IGNORECASE)
_US_CITY_STATE = re.compile(r"([A-Za-z][A-Za-z .'\-]{1,40}?),?\s+([A-Z]{2})\s+\d{5}(?:-\d{4})?\b")


@dataclass
class CargoBlock:
    """One consignment as the mail describes it: its packing list, where
    it goes, what it is. `lines` are the block's own lines (full-width
    colons already normalised)."""

    label: str = ""
    lines: list = field(default_factory=list)
    packing: Packing | None = None
    deliver_to: str = ""
    deliver_line: str = ""
    commodity: str = ""
    hs_code: str = ""
    stated_cbm: float | None = None
    stated_cbm_text: str = ""
    incoterm: str = ""
    notes: list = field(default_factory=list)

    @property
    def has_cargo(self) -> bool:
        return bool(self.packing and self.packing.items)


def _is_cargo_line(line: str) -> bool:
    return bool(_PIECES_LABEL.match(line) or _PIECES.search(line) or _ITEM_HEAD.match(line)
                or _DIMS.search(line) or _WEIGHTS.search(line))


def _is_delivery_line(line: str) -> bool:
    return bool(_DELIVERY_LABEL.match(line) or _POD_LABEL.match(line))


def _split_by_heads(lines) -> list:
    """[(label, [lines])] by 'Shipment 1' headings when two or more, else
    by numbered paragraphs when two or more, else one block."""
    for pattern, how in ((_BLOCK_WORD_HEAD, "word"), (_BLOCK_NUM_HEAD, "num")):
        heads = [(i, pattern.match(l)) for i, l in enumerate(lines) if pattern.match(l)]
        if len(heads) < 2:
            continue
        blocks = []
        if heads[0][0] > 0:
            blocks.append(("", lines[:heads[0][0]]))
        for n, (start, found) in enumerate(heads):
            end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
            label = (" ".join(found.group("label").split()).capitalize() if how == "word"
                     else found.group("n"))
            blocks.append((label, lines[start:end]))
        return blocks
    return [("", list(lines))]


def _split_by_addresses(lines) -> list:
    """[('', [lines])] cut where a delivery / POD line repeats after cargo,
    or cargo follows a delivered block (2026-09-24)."""
    blocks, current = [], []
    delivered = cargo_before_delivery = has_cargo = False
    for line in lines:
        cargo, delivery = _is_cargo_line(line), _is_delivery_line(line)
        cut = (delivery and delivered and has_cargo) or (cargo and delivered and cargo_before_delivery)
        if cut and current:
            blocks.append(("", current))
            current, delivered, cargo_before_delivery, has_cargo = [], False, False, False
        current.append(line)
        if delivery:
            delivered = True
            cargo_before_delivery = has_cargo
        if cargo:
            has_cargo = True
    if current:
        blocks.append(("", current))
    return blocks


def _label_from_address(text: str) -> str:
    found = _US_CITY_STATE.search(text or "")
    if found:
        return "to {}, {}".format(found.group(1).strip().title(), found.group(2))
    short = " ".join((text or "").split())
    return "to " + (short[:30].rstrip() + ("..." if len(short) > 30 else "")) if short else ""


def _read_block(label, lines) -> CargoBlock:
    block = CargoBlock(label=label, lines=list(lines))
    block.packing = parse_packing(lines)
    found = parse_addresses(lines).get("dest")
    if found:
        block.deliver_to, block.deliver_line = found["text"], found["line"]
    else:
        for line in lines:
            pod = _POD_LABEL.match(line)
            if pod:
                block.deliver_to = _POD_LEAD.sub("", pod.group("rest").strip()).strip(" .,;")
                block.deliver_line = " ".join(line.split())
                break
    block.commodity = body_commodity(lines)[0]
    for line in lines:
        hs = _HS_LINE.match(line)
        if hs:
            block.hs_code = hs.group("code")
            break
    for line in lines:
        for value in _BODY_VOLUME.findall(line):
            block.stated_cbm = float(_number(value))
            block.stated_cbm_text = " ".join(line.split())
            break
        if block.stated_cbm is not None:
            break
    block.incoterm = body_incoterm(lines)[0]
    block.notes = [" ".join(l.split()) for l in lines if _BLOCK_NOTE_LINE.match(l)][:3]
    return block


def cargo_blocks(lines) -> list:
    """The body cut into one CargoBlock per consignment
    (2026-09-24): by 'Shipment N' / 'Quote NN' / 'Option A' headings, else
    by numbered paragraphs, else where a delivery address or POD line
    repeats. A block without cargo lines is folded into the block before
    it (the one after, at the top), so an address paragraph stays with
    its shipment. A mail with one consignment gives one block."""
    lines = join_label_values([l.replace("\uff1a", ": ") for l in flatten_vertical_tables(lines)])
    raw = _split_by_heads(lines)
    if len(raw) == 1:
        raw = _split_by_addresses(lines)
    blocks = [_read_block(label, chunk) for label, chunk in raw]
    merged = []
    pending = []                                  # cargo-less blocks waiting for an owner
    for block in blocks:
        if block.has_cargo:
            if pending and not merged:
                block = _read_block(block.label, [l for p in pending for l in p.lines] + block.lines)
            elif pending:
                last = merged.pop()
                block_lines = last.lines + [l for p in pending for l in p.lines]
                merged.append(_read_block(last.label, block_lines))
            pending = []
            merged.append(block)
        else:
            pending.append(block)
    if pending and merged:
        last = merged.pop()
        merged.append(_read_block(last.label, last.lines + [l for p in pending for l in p.lines]))
    if not merged:
        return blocks[:1] if blocks else []
    if len(merged) > 1 and not any(b.label for b in merged):
        for block in merged:
            block.label = _label_from_address(block.deliver_to)
    return merged


# ------------------------------- the body scan: terms and service (5.2) --
_TERMS_LABEL = re.compile(
    r"^\W{0,3}(?:po\s+terms?|terms?|incoterms?|trade\s+terms?|delivery\s+terms?|t[e\u00e9]rminos?)"
    r"\s*(?:\([^)]*\))?\s*:\s*(?P<value>.+)$", re.IGNORECASE)


def body_incoterm(lines) -> tuple:
    """('FOB', line) from 'Po Terms: FOB'; ('', line) when a terms label
    carries something that is not an incoterm; ('', '') when no label."""
    for line in lines:
        found = _TERMS_LABEL.match(line)
        if not found:
            continue
        value = found.group("value").strip()
        first = re.split(r"[\s,;/(-]+", value.upper(), 1)[0].strip(".")
        if first in INCOTERM_WORDS:
            return first, " ".join(line.split())
        return "", " ".join(line.split())
    return "", ""


_AIR_SERVICE = re.compile(
    r"^\W{0,3}(?:air\s+)?service(?:\s+type)?(?:\s+requested)?(?:\s+level)?\s*:\s*(?P<value>.+)$",
    re.IGNORECASE)


def air_service_lines(lines) -> list:
    """'Service Level: 2 - 3 DAYS Economy Service' ->
    ['Air service: 2 - 3 days economy'], for the notes."""
    out = []
    for line in lines:
        found = _AIR_SERVICE.match(line)
        if not found:
            continue
        value = " ".join(found.group("value").split()).lower()
        value = re.sub(r"\s*\bservice\b\s*$", "", value).strip(" .")
        note = "Air service: {}".format(value)
        if value and note not in out:
            out.append(note)
    return out


# ----------------------------------- our own quote with amounts (4.3) --
_QUOTE_WORDS = re.compile(r"\b(?:charges?|rates?|quot(?:e|es|ed|ation)|tariff|price|pricing|costs?)\b",
                          re.IGNORECASE)
_AMOUNT = re.compile(r"(?:\bUSD\b|\bEUR\b|\bUS\$|\$|\bGBP\b)\s*\d|\d[\d,]*(?:\.\d+)?\s*(?:\bUSD\b|\bEUR\b|\bGBP\b)",
                     re.IGNORECASE)


def our_quote_line(lines) -> str:
    """The line where our own mail quotes charges with an amount ('Please note
    destination charges: terminal handling USD 160'), or '' when it does not. A quote
    word on one line and the amounts on the next count as well."""
    quoted = False
    for line in lines:
        if _QUOTE_WORDS.search(line):
            quoted = True
            if _AMOUNT.search(line):
                return " ".join(line.split())
        elif quoted and _AMOUNT.search(line):
            return " ".join(line.split())
    return ""


# ------------------------------- packing tables, one cell per line (15b) --
#: A packing table pasted from a spreadsheet often reaches the body one cell
#: per line: 'Packing Type', 'Length', 'Width', 'Height', 'Qty',
#: 'Measurement Unit', then the values in the same order.
_TABLE_HEADS = {
    "packing type": "pack", "packing": "pack", "package type": "pack", "type": "pack",
    "length": "l", "width": "w", "height": "h",
    "qty": "n", "quantity": "n", "pcs": "n", "pieces": "n", "no. of pieces": "n",
    "measurement unit": "unit", "unit": "unit", "uom": "unit", "dim unit": "unit",
}
_TABLE_UNITS = {"inch": "in", "inches": "in", "in": "in", '"': "in", "cm": "cm", "cms": "cm",
                "centimeter": "cm", "centimeters": "cm", "mm": "mm", "m": "m", "meter": "m", "meters": "m"}
_TABLE_PACKS = {"skd": "skid", "skds": "skids", "plt": "pallet", "plts": "pallets", "ctn": "carton",
                "ctns": "cartons", "bx": "box", "crt": "crate", "pkg": "package", "pkgs": "packages"}


def flatten_vertical_tables(lines) -> list:
    """The lines with every one-cell-per-line packing table turned into
    ordinary lines, one per row: 'SKD / 40.00 / 48.00 / 40.00 / 1 / INCH'
    under its headings becomes '1 skid 40.00 x 48.00 x 40.00 in'. Lines
    that are not such a table pass through unchanged."""
    lines = list(lines or [])
    out, i = [], 0
    while i < len(lines):
        heads, j = [], i
        while j < len(lines):
            key = " ".join(lines[j].strip().strip(":|").split()).casefold()
            if key in _TABLE_HEADS and _TABLE_HEADS[key] not in [h for h in heads]:
                heads.append(_TABLE_HEADS[key])
                j += 1
                continue
            break
        if len(heads) >= 4 and {"l", "w", "h"} <= set(heads):
            rows, k = [], j
            while k + len(heads) <= len(lines):
                cells = [lines[k + n].strip().strip("|").strip() for n in range(len(heads))]
                row = dict(zip(heads, cells))
                if not all(re.fullmatch(r"\d[\d.,]*", row.get(x, "")) for x in ("l", "w", "h")):
                    break
                rows.append(row)
                k += len(heads)
            if rows:
                for row in rows:
                    count = row.get("n") if re.fullmatch(r"\d+", row.get("n", "")) else "1"
                    pack = row.get("pack", "").strip()
                    pack = _TABLE_PACKS.get(pack.casefold(), pack.lower() or "piece")
                    unit = _TABLE_UNITS.get(row.get("unit", "").strip().casefold(), "")
                    out.append("{} {} {} x {} x {}{}".format(count, pack, row["l"], row["w"], row["h"],
                                                           " " + unit if unit else ""))
                i = k
                continue
        out.append(lines[i])
        i += 1
    return out


#: '50 toneladas por mes, por destino', '50 tons per month', '50 t al mes'
_PER_MONTH = re.compile(
    r"(?P<n>\d[\d.,]*)\s*(?P<u>toneladas?|tonnes?|tons?|mt|t|kgs?|kilos?)\s*"
    r"(?:por|per|al|a|/|each|cada)\s*(?:m[e\u00ea]s|month|mes)\b"
    r"(?P<dest>\s*,?\s*(?:por destino|per destination|por destinaci[o\u00f3]n|each destination|cada destino))?",
    re.IGNORECASE)


def weight_per_month(text):
    """(kg a month, as written, per destination?) for the first weight a
    month the text states, or None."""
    found = _PER_MONTH.search(text or "")
    if not found:
        return None
    number = float(_number(found.group("n")))
    unit = found.group("u").lower()
    kg = number if unit.startswith("k") else number * 1000.0
    return kg, " ".join(found.group(0).split()), bool(found.group("dest"))

