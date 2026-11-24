"""The follow-up mail on a quote, as text (2026-09-16; template
pool with rotation the same day).

Nothing here touches Outlook or any model: a template from the settings
(fu_template_1 / _2 / _3, editable on the Settings page; versions inside
one setting are separated by a line of ---) with its blanks filled from
the case and, when the mail archive has the thread, from the mails
themselves. The page shows the text with a Copy button; the CLI prints it.

The blanks:

- {first_name}: the first word of the sender's name on the mail the case
  was registered from (entry_id, else the earliest outside mail of the
  conversation); else the requester's primary contact. Unknown: the
  greeting loses the name ('Hi,').
- {rates}: 'the air / FCL / LCL rates below' by mode, else 'the rates below'.
- {client}: 'your client' for an agent, 'your customer' for one of our
  office, 'you' for anybody else.
- {valid_until_line}: 'Our rates are valid until Sep 30.' when our quote
  mail in the archive says so; otherwise the line disappears.
- {open_question_line}: the last open question of our quote mail, as
  'Also, ...'; otherwise the line disappears.

Which version of a template: the requester's earlier follow-ups at the same
step, counted over all their cases, modulo the number of versions - so the
same person does not get the same words twice in a row.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from . import branding, db, mail_archive, matching, parsing, schema_migrations

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_INDEX = {m.lower(): i + 1 for i, m in enumerate(MONTHS)}
_MONTH_INDEX.update({
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
    "august": 8, "september": 9, "sept": 9, "october": 10, "november": 11, "december": 12,
})
#: 'valid until Sep 30', 'valid through September 30, 2026', 'validity: 30/09/2026',
#: 'rates are valid till 2026-09-30'.
_VALID = re.compile(
    r"\bvalid(?:ity)?\s*(?::|until|till|thru|through|to|up to)?\s*(?:the\s+)?"
    r"(?P<date>(?:\d{4}-\d{2}-\d{2})|(?:\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})"
    r"|(?:[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)"
    r"|(?:\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?(?:,?\s+\d{4})?))",
    re.I)
#: The line between two versions of a template.
VERSION_SEPARATOR = re.compile(r"^\s*-{3,}\s*$", re.M)
#: A sentence of ours that still waits for their answer.
_ASKS = re.compile(r"^(please\s+confirm|please\s+advise|let\s+us\s+know|kindly)\b", re.I)
#: ... unless it is the closing courtesy every quote ends with, which the
#: templates already say in their own words.
_COURTESY = re.compile(
    r"if\s+you\s+(would\s+like|wish|want)\s+to\s+proceed|if\s+you\s+have\s+any|"
    r"if\s+there\s+is\s+anything|feel\s+free|any\s+questions?\b|further\s+(information|assistance)",
    re.I)
OPEN_QUESTION_MAX = 120
#: Earlier defaults (the first three of 2026-09-16 morning, the pool of
#: that evening with and without the blank line before the closing). A
#: stored template equal to one of these is an old default, not something
#: she wrote, so the current pool replaces it without a migration.
OLD_FU_TEMPLATES = {
    "fu_template_1": [
        'Good day {first_name},\n\nA quick check on the offer we sent on {quote_date} {shipment}. Does it fit what you had in mind?\n{valid_until_line}\nIf you want to book, or need us to look at anything more, just reply here.\n\nThank you.',
        'Hi {first_name},\n\nChecking back to hear whether {rates} {which} suit you. Tell us if you are ready to book, or if something else would help.\n{valid_until_line}\n{open_question_line}\n\nThank you.\n---\nHi {first_name},\n\nFollowing up on {rates} {which}. If a figure needs another pass, or you need anything more from us, drop us a line.\n{valid_until_line}\n{open_question_line}\n\nThank you.\n---\nHi {first_name},\n\nWe trust things are going well. Do {rates} {which} work for {client}? We are glad to revisit any piece of it.\n{valid_until_line}\n{open_question_line}\n\nThank you.\n---\nHi {first_name},\n\nA follow-up on the offer below {which}. Is anything still missing on our side before you can go ahead?\n{valid_until_line}\n{open_question_line}\n\nHoping to hear from you soon.',
        'Hi {first_name},\n\nChecking back to hear whether {rates} {which} suit you. Tell us if you are ready to book, or if something else would help.\n{valid_until_line}\n{open_question_line}\nThank you.\n---\nHi {first_name},\n\nFollowing up on {rates} {which}. If a figure needs another pass, or you need anything more from us, drop us a line.\n{valid_until_line}\n{open_question_line}\nThank you.\n---\nHi {first_name},\n\nWe trust things are going well. Do {rates} {which} work for {client}? We are glad to revisit any piece of it.\n{valid_until_line}\n{open_question_line}\nThank you.\n---\nHi {first_name},\n\nA follow-up on the offer below {which}. Is anything still missing on our side before you can go ahead?\n{valid_until_line}\n{open_question_line}\nHoping to hear from you soon.',
    ],
    "fu_template_2": [
        'Good day {first_name},\n\nA follow-up on our offer below {shipment}. Has your client given you any word on it?\n\nHoping to hear from you soon.',
        'Hi {first_name},\n\nA follow-up on the offer below {which}. Has {client} come back with any thoughts?\n\nThank you.\n---\nHi {first_name},\n\nChecking in once more on our offer below {which}. Is it still being reviewed, or have the cargo plans moved?\n\nThank you.\n---\nHi {first_name},\n\nAny news on the offer below {which}? If the dates have slipped, we can price it again when the goods are ready.\n\nHoping to hear from you soon.',
    ],
    "fu_template_3": [
        'Good day {first_name},\n\nMany thanks for letting us quote {shipment}. If the outcome is known, could you tell us how it went and whether our price was in range?\n\nAll feedback is welcome. Thank you.',
        'Hi {first_name},\n\nMany thanks for letting us quote {which}. Has the award been made, and was our price in range? Your view helps us with the next one.\n\nThank you.\n---\nHi {first_name},\n\nThe business below has likely been placed already. If it went elsewhere, it would help us to hear how our offer compared, so we can do better on the next round.\n\nThank you.',
    ],
}


def short_date(value) -> str:
    """'2026-09-14' -> 'Sep 14'."""
    try:
        day = dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return ""
    return "{} {}".format(MONTHS[day.month - 1], day.day)


def parse_loose_date(text: str, year_hint: int | None = None) -> dt.date | None:
    """A date the way people type it in a mail: 'Sep 30', 'September 30,
    2026', '30 Sep', '30/09/2026', '9/30/2026', '2026-09-30'."""
    text = (text or "").strip().rstrip(".")
    if not text:
        return None
    year = year_hint or dt.date.today().year
    found = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if found:
        try:
            return dt.date(int(found.group(1)), int(found.group(2)), int(found.group(3)))
        except ValueError:
            return None
    found = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})$", text)
    if found:
        a, b, y = int(found.group(1)), int(found.group(2)), int(found.group(3))
        if y < 100:
            y += 2000
        # US order first (9/30); day-first when the first number cannot be a month
        month, day = (a, b) if a <= 12 else (b, a)
        try:
            return dt.date(y, month, day)
        except ValueError:
            return None
    found = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?$", text)
    if found:
        month = _MONTH_INDEX.get(found.group(1).lower())
        if month:
            try:
                return dt.date(int(found.group(3) or year), month, int(found.group(2)))
            except ValueError:
                return None
    found = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?(?:,?\s+(\d{4}))?$", text)
    if found:
        month = _MONTH_INDEX.get(found.group(2).lower())
        if month:
            try:
                return dt.date(int(found.group(3) or year), month, int(found.group(1)))
            except ValueError:
                return None
    return None


def valid_until(text: str, year_hint: int | None = None) -> dt.date | None:
    """The validity date a quote mail states, or None."""
    for found in _VALID.finditer(text or ""):
        day = parse_loose_date(found.group("date"), year_hint)
        if day:
            return day
    return None


def open_question(top: str) -> str:
    """The last sentence of our quote mail that still asks them something:
    one ending in '?', or starting 'Please confirm / Please advise / Let us
    know / Kindly', up to 120 characters. Returned as 'Also, ...' with the
    first letter lowered and the sentence's own ending kept; '' when none."""
    found = ""
    for line in (top or "").splitlines():
        for sentence in re.split(r"(?<=[.?!])\s+", line.strip()):
            sentence = sentence.strip()
            if not sentence or len(sentence) > OPEN_QUESTION_MAX or _COURTESY.search(sentence):
                continue
            if sentence.endswith("?") or _ASKS.match(sentence):
                found = sentence
    if not found:
        return ""
    body = found[:1].lower() + found[1:]
    if body[-1] not in ".?!":
        body += "?" if body.lower().startswith(("please", "let", "kindly")) is False else "."
    return "Also, " + body


# ------------------------------------------------------------ the blanks --
def rates_phrase(row) -> str:
    mode = row["service_mode"] or ""
    if mode == "Air":
        return "the air rates below"
    if mode in ("FCL", "LCL"):
        return "the {} rates below".format(mode)
    return "the rates below"


def client_phrase(requester_type) -> str:
    """'your client' for an agent, 'your customer' for one of our offices,
    'you' for a direct customer or anybody unknown."""
    if requester_type == "Agent":
        return "your client"
    if requester_type == "Office":
        return "your customer"
    return "you"


def first_name_of(sender_name: str) -> str:
    """'Renata Solis' -> 'Renata'; 'JJC-VICTOR LANG' -> ''; an address -> ''."""
    name = (sender_name or "").strip().strip("'\"")
    if not name or "@" in name:
        return ""
    if re.match(r"^[A-Z]{3}-", name):                 # one of our offices signature
        return ""
    name = re.sub(r"\s*[<(\[].*$", "", name)
    if "," in name:                                     # 'Rojas, Renata'
        name = name.split(",", 1)[1].strip()
    word = name.split()[0] if name.split() else ""
    word = word.strip(".,;:")
    if not word or not re.search(r"[A-Za-zÀ-ÿ]", word):
        return ""
    return word.capitalize() if word.isupper() else word[:1].upper() + word[1:]


def contact_first_name(conn, party_id) -> str:
    """The first word of the requester's primary contact's name, else the
    first contact's; '' when the party has no contacts with a name."""
    if not party_id:
        return ""
    for row in conn.execute(
            "SELECT name FROM contact WHERE party_id = ? AND name IS NOT NULL AND trim(name) <> '' "
            "ORDER BY is_primary DESC, id LIMIT 1", (party_id,)):
        return first_name_of(row["name"])
    return ""


class Sources:
    """What the archive contributes: the requester's first name, the
    validity our quote mail stated, and its open question. All empty when
    there is no archive."""

    def __init__(self, first_name: str = "", valid_until: dt.date | None = None,
                 open_question: str = ""):
        self.first_name = first_name
        self.valid_until = valid_until
        self.open_question = open_question


def from_archive(row, archive_path=None, internal=(), conn=None) -> Sources:
    """Read the case's thread out of the mail archive. Quietly nothing when
    the archive is missing or the thread is not in it. The thread is the
    Mail card's union (case_mail.mails_for): with `conn` the
    subjects attached by hand count too."""
    from . import case_mail

    out = Sources()
    try:
        search = mail_archive.ArchiveSearch(archive_path)
        search.__enter__()
    except mail_archive.ArchiveError:
        return out
    try:
        internal = {d.lower() for d in internal}
        registered = search.by_entry_id(row["entry_id"]) if row["entry_id"] else None
        if conn is not None:
            thread = case_mail.case_mails(conn, search, row)
        else:
            thread = case_mail.mails_for(
                search, row["entry_id"], [row["conversation_id"]] if row["conversation_id"] else [],
                {row["thread_key"]} if row["thread_key"] else set(), [row["source_subject"] or ""])
        outside = [m for m in thread if not m.sender_internal
                   and matching.domain_of(m.sender_address) not in internal]
        source = registered if registered is not None and not registered.sender_internal else None
        if source is None and outside:
            source = outside[0]
        if source is not None:
            out.first_name = first_name_of(source.sender_name)
        ours = [m for m in thread if m.sender_internal]
        if registered is not None and registered.sender_internal and registered not in ours:
            ours.append(registered)
        quoted = row["quoted_on"] or row["quoted_on_any"] or ""
        if ours:
            def distance(mail):
                if not quoted or not mail.received_on:
                    return 10 ** 6
                try:
                    return abs((dt.date.fromisoformat(mail.received_on[:10])
                                - dt.date.fromisoformat(quoted[:10])).days)
                except ValueError:
                    return 10 ** 6
            ours.sort(key=distance)
            out.open_question = open_question(mail_archive.top_part(ours[0].body_excerpt or ""))
            for mail in ours[:3]:
                year = None
                try:
                    year = int((mail.received_on or quoted)[:4])
                except ValueError:
                    pass
                day = valid_until(mail_archive.top_part(mail.body_excerpt or ""), year)
                if day:
                    out.valid_until = day
                    break
    finally:
        search.__exit__(None, None, None)
    return out


# ---------------------------------------------------------------- building --
def template_key(seq: int) -> str:
    return "fu_template_{}".format(3 if seq >= 3 else max(int(seq), 1))


#: The blanks fill() knows. A template naming any other is refused on
#: the Settings page (2026-09-25); one that slipped in loses it in fill().
BLANKS = ("first_name", "rates", "client", "valid_until_line", "open_question_line")
_BLANK = re.compile(r"\{(\w+)\}")


def normalise(text) -> str:
    """Line breaks as LF (a browser posts CRLF), no trailing spaces on a
    line, no blank lines at either end - before any comparison or split."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")


def pool_text(key: str) -> str:
    """The current default pool of a template setting."""
    return dict(schema_migrations.FU_SETTINGS).get(key, "")


def is_default(key: str, text) -> bool:
    """Empty, the current pool, or one of the old defaults (whatever its
    line breaks): not something she wrote."""
    norm = normalise(text).strip()
    if not norm:
        return True
    return norm in {normalise(t).strip() for t in list(OLD_FU_TEMPLATES.get(key, ())) + [pool_text(key)]}


def is_old_default(key: str, text) -> bool:
    norm = normalise(text).strip()
    return bool(norm) and norm in {normalise(t).strip() for t in OLD_FU_TEMPLATES.get(key, ())}


def effective_text(conn, key: str) -> str:
    """What a step uses: the stored template, or the pool when the stored
    one is empty or an old default. The Settings box shows this."""
    text = normalise(db.get_setting(conn, key, "") or "")
    if not text.strip() or is_old_default(key, text):
        return normalise(pool_text(key))
    return text


def unknown_blanks(text) -> list:
    """The {names} in a template that fill() does not know, in order."""
    out = []
    for name in _BLANK.findall(normalise(text)):
        if name not in BLANKS and name not in out:
            out.append(name)
    return out


def versions(conn, seq: int) -> list:
    """The versions of the template for a step: the setting split on the
    --- lines, the pool's default when the setting is empty or an old
    default (compared after the line breaks are normalised: a Settings save
    posts CRLF, 2026-09-25)."""
    text = effective_text(conn, template_key(seq))
    out = [part.strip("\n") for part in VERSION_SEPARATOR.split(text)]
    return [part for part in out if part.strip()] or [""]


def rotation_index(conn, requester_party_id, seq: int, count: int) -> int:
    """The requester's follow-ups at this step so far, over every case,
    modulo the number of versions."""
    if count <= 1:
        return 0
    sent = conn.execute(
        "SELECT COUNT(*) FROM status_event s JOIN enquiry e ON e.id = s.enquiry_id "
        "WHERE e.requester_party_id = ? AND s.kind = 'followup' AND s.voided = 0 AND s.seq = ?",
        (requester_party_id, int(seq))).fetchone()[0]
    return int(sent) % count


def fill(text: str, blanks: dict) -> str:
    """The template with its blanks filled and the slack taken up: a blank
    that stays empty loses the space before it, a line that held only an
    empty blank disappears, and no two blank lines follow each other."""
    out = text.replace("\r\n", "\n").replace("{client}", blanks.get("client", "") or "you")
    lines_out = []
    for line in out.split("\n"):
        held_blank = "{" in line and "}" in line
        for name in ("first_name", "rates", "valid_until_line", "open_question_line"):
            value = blanks.get(name, "") or ""
            line = line.replace(" {" + name + "}", " " + value if value else "")
            line = line.replace("{" + name + "}", value)
        # a blank the app does not fill never reaches the draft
        line = re.sub(r" ?\{\w+\}", "", line)
        line = re.sub(r"\s+([.,?!])", r"\1", line)
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        if held_blank and not line:
            continue                                  # the line was only an empty blank
        lines_out.append(line)
    text = "\n".join(lines_out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text + "\n"


@dataclass
class Built:
    text: str
    index: int            # 0-based version used
    count: int            # versions in the pool
    seq: int


def compose(conn, enquiry_id: int, seq: int | None = None, sources: Sources | None = None,
            archive_path=None, variant: int | None = None) -> Built:
    """The follow-up text for a case, with which version it is. `seq`
    defaults to the next number; `variant` (1-based, wraps) overrides the
    rotation; `sources` can be handed in (tests) instead of read from the
    archive."""
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    if row is None:
        raise ValueError("no such inquiry")
    if seq is None:
        seq = int(row["fu_count"] or 0) + 1
    if sources is None:
        sources = from_archive(row, archive_path=archive_path,
                               internal=db.get_setting_list(conn, "internal_domains") or [branding.INTERNAL_DOMAIN],
                               conn=conn)
    if not sources.first_name:
        sources.first_name = contact_first_name(conn, row["requester_party_id"])
    pool = versions(conn, seq)
    if variant is not None and variant > 0:
        index = (int(variant) - 1) % len(pool)
    else:
        index = rotation_index(conn, row["requester_party_id"], seq, len(pool))
    blanks = {
        "first_name": sources.first_name,
        "rates": rates_phrase(row),
        "client": client_phrase(row["requester_type"]),
        "valid_until_line": "Our rates are valid until {}.".format(short_date(sources.valid_until))
        if sources.valid_until else "",
        "open_question_line": sources.open_question or "",
    }
    return Built(fill(pool[index], blanks), index, len(pool), int(seq))


def build(conn, enquiry_id: int, seq: int | None = None, sources: Sources | None = None,
          archive_path=None, variant: int | None = None) -> str:
    return compose(conn, enquiry_id, seq, sources, archive_path, variant).text


def build_for_opp(conn, opp_no: str, seq: int | None = None, archive_path=None,
                  variant: int | None = None) -> str:
    row = conn.execute("SELECT id FROM enquiry WHERE opp_no = ?", (opp_no.strip().upper(),)).fetchone()
    if row is None:
        raise ValueError("no case {}".format(opp_no))
    return build(conn, row["id"], seq, archive_path=archive_path, variant=variant)
