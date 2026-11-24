"""Synthetic demo data: python -m opplist demo --out demo --seed 42.

Builds <out>/opplist.db at the current schema (through db.open_db, so every
migration and view is the app's own) and <out>/mail-archive/mail.db (the
schema mail_archive creates and reads), filled with an invented freight
forwarder's last five months: parties, places with coordinates, about 140
enquiries on every status, vendor requests, follow-ups on the ladder,
suggestions, three shipments with their events, and the mail threads
behind all of it.

Rules the generator keeps:

* Every business write goes through the repository (a subclass that only
  pins the clock), so change_log is as complete as the app's own.
* Dates are relative to `today` (the real date by default), so Tracking
  always has something due. Nothing reads the wall clock: created_at,
  change_log.at, the archive's sync stamps all come from the seed and the
  date. Same seed + same date -> byte-identical files (VACUUM at the end,
  journal mode DELETE, no -wal left behind).
* --out refuses to replace a database that is not a demo database unless
  --force is given; with --force the old file is copied aside first.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import branding, db, demo_showcase, mail_archive, migrate_master, parsing, site_snapshot, tracking
from . import demo_world as W

#: Settings that mark a database as the demo's (and let --out replace it).
SEED_SETTING = "demo_seed"
DATE_SETTING = "demo_today"
FIRST_OPP = 1101
TOTAL_CASES = 140
#: Cases handled by another JoJo office (Handled by JQM).
HANDLED_ELSEWHERE = 3
DB_NAME = "opplist.db"
ARCHIVE_NAME = "mail.db"
MAILBOX = "Iris Chen"


class DemoError(RuntimeError):
    """Generation refused (an existing non-demo database). For the user."""


# ------------------------------------------------------------- the clock --
class DemoRepository(db.Repository):
    """db.Repository with a pinned clock: every created_at / updated_at the
    database would default to now, and change_log.at, get self.stamp."""

    def __init__(self, conn):
        super().__init__(conn, source="demo")
        self.stamp = "2000-01-01 00:00:00"
        self._columns = {}

    def at(self, when) -> None:
        if isinstance(when, dt.datetime):
            self.stamp = when.strftime("%Y-%m-%d %H:%M:%S")
        else:
            self.stamp = "{} 09:00:00".format(when.isoformat() if isinstance(when, dt.date) else when)

    def _cols(self, table) -> set:
        if table not in self._columns:
            self._columns[table] = {r[1] for r in self.conn.execute("PRAGMA table_info({})".format(table))}
        return self._columns[table]

    def insert(self, table, values):
        values = dict(values)
        # taken_at: when OppList took a site action in (demo_showcase), else the database's own now
        for column in ("created_at", "updated_at", "taken_at"):
            if column in self._cols(table) and not values.get(column):
                values[column] = self.stamp
        return super().insert(table, values)

    def _log(self, table, row_id, field_name, old, new) -> None:
        self.conn.execute(
            "INSERT INTO change_log (at, table_name, row_id, field, old_value, new_value, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.stamp, table, row_id, field_name, db._norm(old), db._norm(new), self.source))

    def touch(self, table, row_id) -> None:
        self.conn.execute("UPDATE {} SET updated_at = ? WHERE id = ?".format(table), (self.stamp, row_id))


# ------------------------------------------------------------ calendars --
def last_bday(day: dt.date) -> dt.date:
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def bd_back(today: dt.date, n: int) -> dt.date:
    day = last_bday(today)
    for _ in range(max(0, n)):
        day -= dt.timedelta(days=1)
        while day.weekday() >= 5:
            day -= dt.timedelta(days=1)
    return day


def bd_fwd(day: dt.date, n: int) -> dt.date:
    return dt.date.fromisoformat(tracking.add_business_days(day.isoformat(), n))


def at_time(day: dt.date, minutes: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(0, 0)) + dt.timedelta(minutes=minutes)


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December")


def long_date(when: dt.datetime) -> str:
    """'Thursday, September 18, 2026 10:42 AM' without the locale."""
    hour = when.hour % 12 or 12
    return "{}, {} {}, {} {}:{:02d} {}".format(_WEEKDAYS[when.weekday()], _MONTHS[when.month - 1], when.day,
                                               when.year, hour, when.minute, "AM" if when.hour < 12 else "PM")


def short_day(day: dt.date) -> str:
    return "{} {}".format(day.day, _MONTHS[day.month - 1][:3])


def money(amount) -> str:
    return "USD {:,}".format(int(round(amount)))


# --------------------------------------------------------------- people --
@dataclass
class Person:
    name: str
    address: str
    display: str                  # what Outlook shows: 'JJC-IRIS CHEN', 'Camila Orrego'
    internal: bool
    party_key: str
    title: str = ""
    company: str = ""
    city: str = ""
    phone: str = ""
    lang: str = "en"

    @property
    def first(self) -> str:
        return self.name.split()[0]

    def signature(self) -> str:
        lines = [self.display if self.internal else self.name]
        lines.append("{} | {}".format(self.title, self.company) if self.title else self.company)
        if self.city:
            lines.append(self.city)
        if self.phone:
            lines.append("Tel {}".format(self.phone))
        lines.append(self.address)
        if self.internal:
            lines.append("www.{}".format(W.DOMAIN))
        return "\n".join(lines)


# -------------------------------------------------------------- the mail --
class MailBox:
    """The synthetic mailbox: Records for mail_archive.store, ids from a
    hash of the seed and a counter (never from the clock or a UUID)."""

    def __init__(self, seed: int, salt: int = 0):
        self.seed = seed
        self.salt = salt
        self.records = []
        self.count = 0
        self.conversations = 0
        self.folders = []

    def _hex(self, kind: str, n: int, length: int) -> str:
        text = hashlib.sha1("{}|{}|{}".format(self.seed, kind, n).encode("utf-8")).hexdigest().upper()
        while len(text) < length:
            text += hashlib.sha1(text.encode("utf-8")).hexdigest().upper()
        return text[:length]

    def reword(self, body: str) -> str:
        """With a salt (leak_guard), the sign-off of the mail's own words
        rotates through equivalent ones: the text shifts by a word here and
        there, and with it the bytes of the full-text index."""
        if not self.salt:
            return body
        lines = body.split("\n")
        for i, line in enumerate(lines):
            for variants in SIGN_VARIANTS:
                if line in variants:
                    lines[i] = variants[(self.count * 3 + self.salt) % len(variants)]
                    return "\n".join(lines)
        return body

    def conversation(self) -> str:
        self.conversations += 1
        return self._hex("conversation", self.conversations, 32)

    def folder(self, *parts) -> str:
        path = "\\" + "\\".join((MAILBOX,) + parts)
        if path not in self.folders:
            self.folders.append(path)
        return path

    def send(self, conversation, subject, sender: Person, to, when: dt.datetime, body: str,
             cc=(), attachments=(), prev=None, folder=None):
        self.count += 1
        domain = sender.address.split("@", 1)[1]
        message_id = "<{}.{}@mail.{}>".format(self._hex("mid", self.count, 20).lower(),
                                               when.strftime("%Y%m%d%H%M%S"), domain)
        text = self.reword(body).rstrip() + "\n"
        if prev is not None:
            text += "\n\nFrom: {} <{}>\nSent: {}\nTo: {}\nSubject: {}\n\n{}".format(
                prev.sender_name, prev.sender_address, long_date(dt.datetime.strptime(prev.received_at, "%Y-%m-%d %H:%M:%S")),
                prev.display_to, prev.subject, prev.body)
        if folder is None:
            folder = self.folder("Sent Items") if sender.address.startswith("iris.chen@") else self.folder("Inbox")
        to, cc = list(to), list(cc)
        stamp = when.strftime("%Y-%m-%d %H:%M:%S")
        record = mail_archive.Record(
            message_id=message_id, entry_id="00000000" + self._hex("entry", self.count, 62),
            store=MAILBOX, folder_path=folder, conversation_id=conversation,
            conversation_topic=parsing.clean_subject(subject) or subject,
            received_at=stamp, sent_at=stamp, sender_name=sender.display, sender_address=sender.address,
            sender_internal=sender.internal, to_addresses=[p.address for p in to],
            cc_addresses=[p.address for p in cc], display_to="; ".join(p.display for p in to + cc),
            subject=subject, body=text[:mail_archive.BODY_LIMIT], has_attachments=bool(attachments),
            attachment_names=list(attachments))
        record.seq = self.count
        self.records.append(record)
        return record


def mid_of(record) -> str:
    return record.message_id


# --------------------------------------------------------------- texts ----
GREET = {"en": "Hi {},", "es": "Estimada {},", "pt": "Prezada {},"}
GREET_M = {"en": "Hi {},", "es": "Estimado {},", "pt": "Prezado {},"}
SIGN = {"en": "Best regards,", "es": "Saludos cordiales,", "pt": "Atenciosamente,"}
#: Equivalent sign-offs MailBox.reword rotates through (each is one the Mail
#: card's signature cut knows, or the language's usual one).
SIGN_VARIANTS = (
    ("Best regards,", "Regards,", "Kind regards,", "Thank you & regards,", "Warm regards,"),
    ("Saludos cordiales,", "Saludos,", "Atentamente,", "Cordialmente,"),
    ("Atenciosamente,", "Obrigada, atenciosamente,"),
)


def mode_word(mode: str, lang: str) -> str:
    if mode == "Air":
        return {"en": "air freight", "es": "carga aerea", "pt": "carga aerea"}[lang]
    if mode == "Truck":
        return "FTL"
    if mode == "Sea-Air":
        return {"en": "sea-air", "es": "maritimo-aereo", "pt": "maritimo-aereo"}[lang]
    if mode == "Cross-border Truck":
        return "cross-border FTL"
    return mode


def request_body(lang, to_first, plan) -> str:
    q = plan.qty_text
    o, d = plan.origin_name, plan.dest_name
    if lang == "es":
        lines = [GREET["es"].format(to_first), "",
                 "Favor cotizar {} {} desde {} hasta {}.".format(mode_word(plan.mode, "es"), q, o, d),
                 "Mercaderia: {}. Incoterm: {}.".format(plan.commodity, plan.incoterm or "FOB"),
                 "Carga lista en {}.".format(short_day(plan.ready)), "",
                 "Quedo atento a tus comentarios.", "", SIGN["es"]]
    elif lang == "pt":
        lines = [GREET["pt"].format(to_first), "",
                 "Por favor, cotar {} {} de {} para {}.".format(mode_word(plan.mode, "pt"), q, o, d),
                 "Mercadoria: {}. Incoterm: {}.".format(plan.commodity, plan.incoterm or "FOB"),
                 "Carga pronta a partir de {}.".format(short_day(plan.ready)), "",
                 "Aguardo retorno.", "", SIGN["pt"]]
    else:
        lines = [GREET["en"].format(to_first), "",
                 "Please quote {} {} from {} to {}.".format(mode_word(plan.mode, "en"), q, o, d),
                 "Commodity: {}. Incoterm: {}.".format(plan.commodity, plan.incoterm or "FOB"),
                 "Cargo ready {}.".format(short_day(plan.ready))]
        if plan.special:
            lines.append("Special handling: {}.".format(plan.special.replace(";", ", ")))
        lines += ["", "Looking forward to your rates.", "", SIGN["en"]]
    return "\n".join(lines)


def rate_lines(plan, amount) -> list:
    if plan.mode == "FCL":
        return ["Ocean freight {} - {}: {} per {}".format(plan.origin_name, plan.dest_name, money(amount),
                                                          plan.container_type),
                "Pickup and drayage to port: {} per container".format(money(plan.rng_extra)),
                "Export documentation: USD 95 per shipment"]
    if plan.mode == "LCL":
        return ["LCL ocean freight: USD {} per cbm, minimum 1 cbm".format(int(amount / max(plan.cbm, 1))),
                "Origin CFS handling: USD 18 per cbm",
                "Export documentation: USD 85 per shipment",
                "Total estimate for {} cbm: {}".format(plan.cbm, money(amount))]
    if plan.mode == "Air":
        return ["Air freight {} - {}: USD {:.2f} per kg chargeable".format(
                    plan.origin_name, plan.dest_name, amount / max(plan.kg, 1)),
                "Fuel and security surcharge included",
                "Screening: USD 0.12 per kg, minimum USD 45",
                "Total estimate for {} kg: {}".format(plan.kg, money(amount))]
    if plan.mode == "Sea-Air":
        return ["Sea-air {} - {} (by sea to Los Angeles, then by air): USD {:.2f} per kg chargeable".format(
                    plan.origin_name, plan.dest_name, amount / max(plan.kg, 1)),
                "Transfer at Los Angeles included",
                "Total estimate for {} kg: {}".format(plan.kg, money(amount))]
    if plan.mode == "Cross-border Truck":
        return ["FTL 53' dry van {} - {}, through Laredo: {} all-in".format(plan.origin_name, plan.dest_name,
                                                                         money(amount)),
                "Mexican and US customs brokers' fees not included",
                "Detention after 2 free hours: USD 75 per hour"]
    if plan.mode == "Truck":
        return ["FTL 53' dry van {} - {}: {} all-in".format(plan.origin_name, plan.dest_name, money(amount)),
                "Liftgate at delivery if needed: USD 95",
                "Detention after 2 free hours: USD 75 per hour"]
    return ["{} {} - {}: {} lump sum".format(plan.mode, plan.origin_name, plan.dest_name, money(amount)),
            "Lashing and securing included"]


def quote_body(person_to, plan, amount, valid: dt.date, sender: Person, revised=False) -> str:
    head = ("As discussed, please see our revised rates for {} - {}:".format(plan.origin_name, plan.dest_name)
            if revised else
            "Thanks for thinking of us. Please see our rates for {} - {}:".format(plan.origin_name, plan.dest_name))
    lines = [GREET["en"].format(person_to.first), "", head, ""]
    lines += rate_lines(plan, amount)
    lines += ["", "Transit time: about {} days".format(plan.transit),
              "Rates valid until {}".format(short_day(valid)),
              "Subject to space and equipment at the time of booking.", "",
              SIGN["en"], sender.signature()]
    return "\n".join(lines)


FU_TEXT = {
    1: ("Checking back: would the rates below work for this move? We can book as soon as you confirm.",
        "Following up on the rates below: do they suit you? We are glad to rework any part of them."),
    2: ("Any update on where the quote below stands? Tell us if your side is still comparing.",
        "Following up once more on the offer below. If the cargo plan has moved on, a short note is enough."),
}


def followup_body(to: Person, seq: int, rng, sender: Person) -> str:
    return "\n".join([GREET["en"].format(to.first), "", rng.choice(FU_TEXT[min(seq, 2)]), "", "Thank you.", "",
                      SIGN["en"], sender.signature()])


REPLY_NOTED = {
    "en": "Thanks {}, well received. I will check with my client and revert.",
    "es": "Gracias {}, recibido. Lo reviso con mi cliente y te comento.",
    "pt": "Obrigada {}, recebido. Vou verificar com o cliente e retorno.",
}
REPLY_ASKS = {
    "en": "Thanks {}. Could you check if there is any room on the rate? The client has a target of {} all-in.",
    "es": "Gracias {}. Podrias revisar si hay margen en la tarifa? El cliente tiene un target de {} all-in.",
    "pt": "Obrigada {}. Consegue verificar se ha margem na tarifa? O cliente tem um target de {} all-in.",
}
WON_TEXT = {
    "en": "Good news, the client approved your rates. Please proceed with the booking; shipper details to follow.",
    "es": "Buenas noticias, el cliente aprobo la tarifa. Favor proceder con la reserva, te envio datos del embarcador.",
    "pt": "Boa noticia, o cliente aprovou a tarifa. Favor seguir com o booking, envio os dados do embarcador.",
}
LOST_TEXT = {
    "price": {"en": "Unfortunately the client went with another offer this time. For your reference, the rate they took was {}.",
              "es": "Lamentablemente el cliente eligio otra oferta esta vez. Como referencia, la tarifa que tomaron fue {}.",
              "pt": "Infelizmente o cliente escolheu outra oferta desta vez. Para referencia, a tarifa foi {}."},
    "cancelled": {"en": "The consignee has cancelled this order, so the shipment will not move. Thanks for your support.",
                  "es": "El consignatario cancelo la orden, el embarque no se realizara. Gracias por el apoyo.",
                  "pt": "O consignatario cancelou o pedido, o embarque nao vai acontecer. Obrigada pelo apoio."},
    "forwarder": {"en": "The shipper nominated their own forwarder at origin, so we will not handle this one.",
                  "es": "El embarcador nomino su propio forwarder en origen, asi que no manejaremos este embarque.",
                  "pt": "O embarcador nomeou o proprio agente na origem, entao nao vamos atender este embarque."},
    "slow": {"en": "The client needs the cargo sooner; they booked a faster routing with another carrier.",
             "es": "El cliente necesita la carga antes; reservaron una ruta mas rapida con otra naviera.",
             "pt": "O cliente precisa da carga antes; reservaram uma rota mais rapida com outro armador."},
}


def reply_body(lang, to: Person, text: str, sender: Person) -> str:
    greet = (GREET if lang != "en" else GREET)[lang].format(to.first)
    return "\n".join([greet, "", text, "", SIGN[lang], sender.signature()])


# ---------------------------------------------------------------- plans ---
@dataclass
class VendorPlan:
    party_key: str
    asked: dt.date
    replied: dt.date | None
    status: str                  # Asked / Quoted / Declined / Acknowledged
    amount: int = 0


@dataclass
class CasePlan:
    kind: str
    received: dt.date
    requester: str
    origin: str | None
    dest: str | None
    mode: str
    origin_country: str = ""
    dest_country: str = ""
    origin_name: str = ""
    dest_name: str = ""
    commodity: str = ""
    incoterm: str | None = None
    container_type: str | None = None
    container_qty: int | None = None
    kg: int = 0
    cbm: float = 0.0
    qty_text: str = ""
    qty_raw: str | None = None
    special: str | None = None
    end_customer: str | None = None
    ready: dt.date | None = None
    transit: int = 20
    rng_extra: int = 0
    amount: int = 0
    quoted: dt.date | None = None
    fus: list = field(default_factory=list)            # [date]
    replies: list = field(default_factory=list)        # [(date, 'asks'|'noted')]
    requote: tuple | None = None                       # (date, reason)
    close: tuple | None = None                         # (date, status, why)
    vendors: list = field(default_factory=list)
    handled_by: str | None = None
    rate_used: str | None = "auto"
    shipment: str | None = None
    suggest: str | None = None                         # 'accepted' | 'pending'
    group_with_previous: bool = False
    cargo_lines: int = 0
    lang: str = "en"
    owner: str = "Iris"


class World:
    """Everything the plans and the executor share."""

    def __init__(self, seed: int, today: dt.date, salt: int = 0):
        self.seed = seed
        self.today = today
        self.rng = random.Random(seed)
        self.parties = {}         # key -> dict(row + people)
        self.places = {}          # key -> row dict
        self.mailbox = MailBox(seed, salt)
        self.staff = {}
        self.thread_keys = set()
        self.ref_counter = {}

    # -- people -------------------------------------------------------------
    def person(self, party_key: str, index: int = 0) -> Person:
        return self.parties[party_key]["people"][index % len(self.parties[party_key]["people"])]

    def iris(self) -> Person:
        return self.staff["Iris Chen"]

    def colleague(self) -> Person:
        return self.staff[self.rng.choice(("Victor Lang", "Ava Tran", "Colin Hart"))]

    def place_name(self, key: str | None, country: str) -> str:
        """How a mail writes a place: 'Chicago, IL', 'ORD', 'San Antonio, Chile'."""
        if not key:
            return country
        row = self.places[key]
        if row["type"] == "airport":
            return key
        if row["country"] == "USA" and row.get("subdivision"):
            return "{}, {}".format(row["name"], row["subdivision"])
        return "{}, {}".format(row["name"], row["country"])

    def next_ref(self, prefix: str) -> str:
        self.ref_counter[prefix] = self.ref_counter.get(prefix, 0) + 1
        return "{}-{:02d}{:04d}".format(prefix, self.today.year % 100, 311 + self.ref_counter[prefix] * 7)


# ---------------------------------------------------------------- guard ---
def _is_demo_db(path: Path) -> bool:
    try:
        conn = sqlite3.connect("file:{}?mode=ro".format(path.as_posix()), uri=True)
        try:
            row = conn.execute("SELECT value FROM setting WHERE key = ?", (SEED_SETTING,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return row is not None


def _is_demo_archive(path: Path) -> bool:
    try:
        conn = sqlite3.connect("file:{}?mode=ro".format(path.as_posix()), uri=True)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (SEED_SETTING,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return row is not None


def _remove(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")):
        if candidate.exists():
            candidate.unlink()


def _set_aside(path: Path, out_dir: Path, today: dt.date) -> Path:
    """Copy a database that is not ours out of the way before --force
    replaces it (a restore point before anything)."""
    target_dir = out_dir / "restore-points"
    target_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        target = target_dir / "{}_before_demo_{}_{}{}".format(path.stem, today.isoformat(), n, path.suffix)
        if not target.exists():
            break
        n += 1
    shutil.copy2(path, target)
    return target


def prepare(out_dir: Path, force: bool, today: dt.date) -> list:
    """Clear the way for a new demo; notes for the user. Raises DemoError
    when an existing file is not a demo file and --force was not given."""
    notes = []
    targets = ((out_dir / DB_NAME, _is_demo_db), (out_dir / "mail-archive" / ARCHIVE_NAME, _is_demo_archive))
    for path, is_demo in targets:
        if path.exists() and not is_demo(path):
            if not force:
                raise DemoError("{} exists and is not a demo file; refusing to overwrite it "
                                "(pass --force to copy it aside and replace it).".format(path))
            notes.append("copied aside: {}".format(_set_aside(path, out_dir, today)))
    for path, _ in targets:
        _remove(path)
    return notes


# ------------------------------------------------------------ reference ---
def seed_reference(world: World, repo: DemoRepository) -> None:
    conn = repo.conn
    repo.at(bd_back(world.today, 110))
    for name, iso2, iso3, region in W.COUNTRIES:
        repo.insert("country", {"name": name, "iso2": iso2, "iso3": iso3, "region": region})
    ids = {}
    for key, name, ptype, country, sub, unlocode, iata, lat, lon, parent in W.PLACES:
        values = {"key": key, "name": name, "type": ptype, "country": country,
                  "country_code": next(c[1] for c in W.COUNTRIES if c[0] == country),
                  "subdivision": sub, "unlocode": unlocode, "iata": iata, "lat": lat, "lon": lon,
                  "is_seaport": 1 if ptype == "seaport" else 0, "is_airport": 1 if ptype == "airport" else 0,
                  "status": "active", "source": "phase1",
                  "parent_id": ids.get(parent) if parent else None}
        ids[key] = repo.insert("master_location", values)
        world.places[key] = dict(values, id=ids[key])
    for alias, key, kind in W.ALIASES:
        repo.add_master_alias(alias, ids[key], kind)
    for key, name, iso2, members in W.AREAS:
        area = repo.add_master_area(key, name, iso2)
        for member in members:
            repo.update("master_location", ids[member], {"area_id": area})
    # the master layer is the live one: the old table steps aside the way
    # migrate-master leaves it (schema, not business data)
    conn.execute("ALTER TABLE location RENAME TO location_old")
    conn.execute(migrate_master.LOCATION_VIEW_SQL)


def seed_parties(world: World, repo: DemoRepository) -> None:
    rng = world.rng
    phone_n = 0

    def add(key, full, ptype, country, city, domain, roles, mfn, lang, people, office=None):
        nonlocal phone_n
        values = {"key": key, "full_name": full, "type": ptype, "country": country, "city": city,
                  "mfn_member": mfn, "mfn_no": None, "stage": "active", "source": "manual", "roles": roles}
        party_id = repo.insert("party", values)
        if domain:
            repo.insert("party_domain", {"domain": domain, "party_id": party_id})
        persons = []
        for i, (name, local, title) in enumerate(people):
            address = "{}@{}".format(local, domain or W.DOMAIN)
            repo.insert("contact", {"party_id": party_id, "name": name, "email": address, "title": title,
                                    "is_primary": 1 if i == 0 else 0})
            phone_n += 1
            if office:
                phone = W.OFFICE_PHONES[office]
                display = "{}-{}".format(office, name.upper())
            else:
                phone = "{} 555-01{:02d}".format(W.COUNTRY_CALLING.get(country, "+1 312"), (phone_n * 7) % 100)
                display = name
            persons.append(Person(name=name, address=address, display=display, internal=office is not None,
                                  party_key=key, title=title, company=full, lang=lang,
                                  city="{}, {}".format(city, country) if city else country, phone=phone))
        world.parties[key] = {"id": party_id, "key": key, "type": ptype, "country": country, "city": city,
                              "domain": domain, "lang": lang, "people": persons, "roles": roles}
        return party_id

    for row in W.AGENTS + W.DIRECT_CUSTOMERS + W.CARRIERS:
        add(*row)
    for code, city, country, people in W.OFFICES:
        key = branding.office_key(code, repo.conn)
        add(key, "{} {}".format(branding.COMPANY_NAME, city), branding.OFFICE_TYPE, country, city, None,
            branding.OFFICE_ROLE, None, "en", people, office=code)
        for person in world.parties[key]["people"]:
            world.staff[person.name] = person
    # the two parking places the app expects
    for key in ("(TBC)", "Unknown"):
        repo.insert("party", {"key": key, "full_name": key, "type": "Unknown", "country": "Unknown",
                              "placeholder": 1, "source": "manual", "roles": ""})
        world.parties[key] = {"id": repo.conn.execute("SELECT id FROM party WHERE key = ?", (key,)).fetchone()[0],
                              "key": key, "type": "Unknown", "country": "Unknown", "city": None,
                              "domain": None, "lang": "en", "people": [], "roles": ""}
    # the network two Chilean agents belong to, and a prospect nobody wrote to yet
    group = repo.add_party_group("CONOSUR", "Cono Sur Freight Network", "Demo network of independent agents")
    for key in ("Andes Cumbre", "Rambla Logistics"):
        repo.update("party", world.parties[key]["id"], {"group_id": group})
    repo.update("party", world.parties["Maipo Freight"]["id"], {"stage": "contacted", "cadence_days": 30})
    repo.update("party", world.parties["Turia Cargo"]["id"], {"stage": "prospect"})
    # relationship timeline: outreach, calls, a visit
    kinds = ("outreach", "reply", "call", "meeting", "note")
    for key in [a[0] for a in W.AGENTS]:
        for _ in range(rng.choice((0, 1, 1, 2, 3))):
            day = bd_back(world.today, rng.randint(15, 130))
            repo.at(at_time(day, 600 + rng.randint(0, 400)))
            kind = rng.choice(kinds)
            summary = {"outreach": "Introduced our Midwest export service and asked for their tariff",
                       "reply": "Sent back their local charges and profile",
                       "call": "Call about volumes for the next quarter",
                       "meeting": "Video meeting: lanes, free time and payment terms",
                       "note": "Prefers quotes split origin / freight / destination"}[kind]
            repo.insert("party_event", {"party_id": world.parties[key]["id"], "at": day.isoformat(),
                                        "type": kind, "summary": summary, "source": "manual"})


# ---------------------------------------------------------------- lanes ---
LATAM = ("Chile", "Peru", "Brazil", "Mexico", "Colombia", "Uruguay")
ASIA = ("China", "Vietnam", "India", "Hong Kong")
EUROPE = ("Netherlands", "Germany", "Spain")


def _pick_mode(rng, allow_domestic=False) -> str:
    roll = rng.random()
    if allow_domestic and roll < 0.35:
        return "Truck"
    roll = rng.random()
    if roll < 0.40:
        return "FCL"
    if roll < 0.60:
        return "LCL"
    if roll < 0.92:
        return "Air"
    return rng.choice(("Break Bulk", "RORO", "Project"))


def _end_of(rng, region_ocean, region_air, country, mode):
    table = region_air if mode == "Air" else region_ocean
    return rng.choice(table[country])


def lane_for(world: World, requester: str):
    """(origin key, dest key, mode) for a requester, from demo_world's lanes."""
    rng = world.rng
    party = world.parties[requester]
    country = party["country"]
    us_ocean = lambda: rng.choice(W.US_OCEAN)
    us_air = lambda: rng.choice(W.US_AIR)
    us_end = lambda m: us_air() if m == "Air" else us_ocean()
    if party["type"] == "Direct Customer":
        mode = _pick_mode(rng, allow_domestic=True)
        if mode == "Truck":
            origin, dest = rng.sample(W.US_DOMESTIC, 2)
            return origin, dest, mode
        target = rng.choice(LATAM + ("Netherlands", "Germany"))
        dest = _end_of(rng, {**W.LATAM_OCEAN, **W.EUROPE_OCEAN}, {**W.LATAM_AIR, **W.EUROPE_AIR}, target, mode)
        return us_end(mode), dest, mode
    if requester == "Lakehead Freight":
        mode = rng.choice(("LCL", "Multi-mode", "Air"))
        return (us_air() if mode == "Air" else "CHICAGO, IL"), ("YYZ" if mode == "Air" else "TORONTO, ON"), mode
    if country in LATAM or requester == branding.office_key("JQM"):
        mode = _pick_mode(rng)
        target = country if country in LATAM else rng.choice(LATAM)
        latam_end = _end_of(rng, W.LATAM_OCEAN, W.LATAM_AIR, target, mode)
        if rng.random() < 0.72:
            return us_end(mode), latam_end, mode
        return latam_end, us_end(mode), mode
    if country in ASIA or party["type"] == "Office":
        mode = _pick_mode(rng)
        source = country if country in ASIA else "China"
        asia_end = _end_of(rng, W.ASIA_OCEAN, W.ASIA_AIR, source, mode)
        roll = rng.random()
        if roll < 0.58:
            return asia_end, us_end(mode), mode
        if roll < 0.85:
            target = rng.choice(LATAM)
            return asia_end, _end_of(rng, W.LATAM_OCEAN, W.LATAM_AIR, target, mode), mode
        return us_end(mode), asia_end, mode
    mode = _pick_mode(rng)
    europe_end = _end_of(rng, W.EUROPE_OCEAN, W.EUROPE_AIR, country, mode)
    if rng.random() < 0.55:
        return europe_end, us_end(mode), mode
    return us_end(mode), europe_end, mode


def fill_cargo(world: World, plan: CasePlan) -> None:
    rng = world.rng
    plan.commodity = plan.commodity or rng.choice(W.COMMODITIES)
    plan.incoterm = plan.incoterm or rng.choice(("FOB", "FOB", "EXW", "FCA", "CIF", "DAP", "DDP", "CFR"))
    plan.end_customer = plan.end_customer or (rng.choice(W.END_CUSTOMERS) if rng.random() < 0.55 else None)
    if plan.origin:
        plan.origin_country = world.places[plan.origin]["country"]
    if plan.dest:
        plan.dest_country = world.places[plan.dest]["country"]
    plan.origin_name = world.place_name(plan.origin, plan.origin_country)
    plan.dest_name = world.place_name(plan.dest, plan.dest_country)
    plan.ready = bd_fwd(plan.received, rng.randint(3, 12))
    if plan.mode == "FCL":
        if plan.commodity in W.REEFER_COMMODITIES:
            plan.container_type = "40RF"
            plan.special = "Reefer"
        else:
            plan.container_type = plan.container_type or rng.choice(("40HQ", "40HQ", "40HQ", "20GP", "20GP", "40GP"))
        plan.container_qty = plan.container_qty or rng.choice((1, 1, 1, 2, 2, 3, 4))
        plan.qty_text = "{}x{}".format(plan.container_qty, plan.container_type)
        plan.qty_raw = "FCL:{} x {}".format(plan.container_qty, plan.container_type)
        per = rng.randrange(1400, 4200, 25)
        plan.amount = per
        plan.rng_extra = rng.randrange(450, 1150, 25)
        plan.transit = rng.randint(14, 38)
    elif plan.mode in ("LCL", "Multi-mode"):
        plan.cbm = plan.cbm or round(rng.uniform(1.2, 16.0), 1)
        plan.qty_text = "{} cbm".format(plan.cbm)
        plan.qty_raw = "LCL:{} CBM".format(plan.cbm)
        plan.amount = int(plan.cbm * rng.randrange(48, 125) + 180)
        plan.transit = rng.randint(18, 42)
    elif plan.mode == "Air":
        plan.kg = plan.kg or rng.choice((45, 80, 120, 180, 260, 350, 480, 620, 900, 1250, 1800, 2400))
        plan.qty_text = "{} kg".format(plan.kg)
        plan.qty_raw = "Air:{}".format(plan.kg)
        plan.amount = int(plan.kg * rng.uniform(2.1, 6.8) + 95)
        plan.transit = rng.randint(2, 6)
        if rng.random() < 0.15:
            plan.special = "DG"
        plan.cargo_lines = rng.choice((0, 0, 1, 2))
    elif plan.mode == "Sea-Air":
        plan.kg = plan.kg or rng.choice((850, 1200, 1800, 2600))
        plan.cbm = plan.cbm or round(plan.kg / 220.0, 1)
        plan.qty_text = "{} kg, {} cbm".format(plan.kg, plan.cbm)
        plan.qty_raw = "Air:{}".format(plan.kg)
        plan.amount = int(plan.kg * rng.uniform(2.6, 3.6) + 150)
        plan.transit = rng.randint(16, 22)
    elif plan.mode == "Cross-border Truck":
        pallets = rng.randint(14, 26)
        plan.qty_text = "{} pallets".format(pallets)
        plan.qty_raw = "FTL 53' dry van, {} pallets, cross-border".format(pallets)
        plan.amount = rng.randrange(2900, 4800, 50)
        plan.transit = rng.randint(3, 5)
    elif plan.mode == "Truck":
        pallets = rng.randint(8, 26)
        plan.qty_text = "{} pallets".format(pallets)
        plan.qty_raw = "FTL 53' dry van, {} pallets".format(pallets)
        plan.amount = rng.randrange(1600, 3900, 50)
        plan.transit = rng.randint(1, 4)
    else:
        plan.qty_text = "1 unit, {} x 3.1 x 3.4 m".format(round(rng.uniform(8.0, 16.0), 1))
        plan.qty_raw = plan.qty_text
        plan.special = "OOG"
        plan.amount = rng.randrange(9000, 42000, 250)
        plan.transit = rng.randint(20, 45)
    plan.lang = world.parties[plan.requester]["lang"]


def vendor_candidates(world: World, plan: CasePlan) -> list:
    """Who we would ask for a rate on this lane (buy side)."""
    out = []
    oc, dc = plan.origin_country, plan.dest_country
    if plan.mode == "Truck":
        return ["Blue Mesa Transport", "Prairie Crossdock"]
    if plan.mode == "Cross-border Truck":
        return ["Blue Mesa Transport"] + [a[0] for a in W.AGENTS if a[3] == "Mexico" and a[0] != plan.requester][:1]
    if oc == "USA" and plan.mode in ("FCL", "LCL", "Break Bulk", "RORO", "Project", "Multi-mode"):
        out += ["ZIM", "Prairie Crossdock"]
    if oc == "USA" and plan.mode == "Air":
        out += ["Prairie Crossdock"]
    if dc == "USA":
        out += ["Harborview Customs", "Blue Mesa Transport"]
    for agent in W.AGENTS:
        key, country = agent[0], agent[3]
        if key == plan.requester or country == "USA":
            continue
        if country == dc or (country == oc and dc != "USA" and oc != "USA"):
            out.append(key)
    if oc in ("China", "Hong Kong") and plan.mode == "Air":
        out.append("Korean Air")
    if oc in ("China", "Hong Kong") and plan.requester not in (branding.office_key("JJS"), branding.office_key("JJH")):
        out.append(branding.office_key("JJS"))
    seen = []
    for key in out:
        if key not in seen:
            seen.append(key)
    return seen


# --------------------------------------------------------------- planning --
def requester_pool(world: World) -> list:
    weights = []
    for row in W.AGENTS:
        key, country = row[0], row[3]
        if country == "USA":
            continue
        weights.append((key, 3.0 if country in LATAM else (2.0 if country in ASIA else 1.4)))
    for code, weight in (("JJH", 2.0), ("JJS", 2.0), ("JJB", 1.2), ("JQM", 2.2)):
        weights.append((branding.office_key(code), weight))
    for row in W.DIRECT_CUSTOMERS:
        weights.append((row[0], 1.0))
    return weights


def choose(rng, weighted):
    total = sum(w for _, w in weighted)
    roll = rng.uniform(0, total)
    for item, w in weighted:
        roll -= w
        if roll <= 0:
            return item
    return weighted[-1][0]


def _vendors(world, plan, asked_on, n=None, replied=True, statuses=None, before=None) -> list:
    rng = world.rng
    candidates = vendor_candidates(world, plan)
    if not candidates:
        return []
    n = n if n is not None else rng.choice((1, 1, 2, 2, 3))
    picked = rng.sample(candidates, min(n, len(candidates)))
    out = []
    for key in picked:
        status = rng.choice(statuses) if statuses else rng.choice(
            ("Quoted", "Quoted", "Quoted", "Quoted", "Declined", "Acknowledged", "Asked"))
        reply = None
        if replied and status != "Asked":
            reply = bd_fwd(asked_on, rng.choice((0, 0, 1, 1, 2)))
            if before and reply > before:
                reply = before
        out.append(VendorPlan(key, asked_on, reply, status if reply or status == "Asked" else "Asked",
                              int(plan.amount * rng.uniform(0.72, 0.9))))
    return out


def plan_recent(world: World) -> list:
    """The cases of the last few weeks, each placed where a Tracking rule or
    a follow-up rung needs it (business days back from today)."""
    rng, today = world.rng, world.today
    b = lambda n: bd_back(today, n)
    pool = requester_pool(world)
    latam_pool = [(k, w) for k, w in pool if world.parties[k]["country"] in LATAM]
    specs = [
        # (kind, received bd ago, extra)
        ("new", 0, {}), ("new", 0, {}), ("new", 1, {}),
        ("r1", 2, {}), ("r1", 3, {}), ("r1", 5, {}), ("r8", 4, {}),
        ("tbc", 3, {}),
        ("silent", 3, {}), ("silent", 4, {}), ("silent", 6, {}),
        ("answered", 2, {}), ("answered", 4, {"pending_vendor": True}),
        ("q_wait", 1, {"q": 0}), ("q_wait", 1, {"q": 1}),
        ("q_fu1", 3, {"q": 2}), ("q_fu1", 4, {"q": 3}), ("q_fu1", 6, {"q": 4}),
        ("q_fu1_sent", 4, {"q": 3, "fu": [2]}),
        ("q_fu2", 10, {"q": 9, "fu": [7], "reply": [(6, "noted")]}),
        ("q_fu2", 9, {"q": 8, "fu": [6], "reply": [(5, "noted")]}),
        ("q_decide", 19, {"q": 17, "fu": [15, 9]}), ("q_decide", 22, {"q": 21, "fu": [19, 12], "pending_lost": True}),
        ("q_replied", 6, {"q": 5, "fu": [3], "reply": [(1, "asks")]}),
        ("q_replied", 8, {"q": 7, "fu": [5], "reply": [(2, "asks")]}),
        ("q_noted", 7, {"q": 6, "fu": [4], "reply": [(3, "noted")]}),
        ("q_requote", 13, {"q": 12, "reply": [(8, "asks")], "requote": (7, "Lower rate"), "fu": [5]}),
        ("won_recent", 9, {"q": 8, "close": 3}),
        ("won_recent", 11, {"q": 10, "close": 5, "no_rate_used": True}),
        ("lost_recent", 12, {"q": 11, "close": 4, "why": "price"}),
    ]
    plans = []
    for kind, back, extra in specs:
        requester = choose(rng, latam_pool if kind in ("r8",) else pool)
        if kind == "tbc":
            requester = "(TBC)"
        received = b(back)
        if kind == "tbc":
            origin, dest, mode = "CHICAGO, IL", "CALLAO", "FCL"
        elif kind == "r8":
            origin, dest, mode = rng.choice(W.US_OCEAN), None, "LCL"
        else:
            origin, dest, mode = lane_for(world, requester)
        plan = CasePlan(kind=kind, received=received, requester=requester, origin=origin, dest=dest, mode=mode)
        if kind == "r8":
            plan.dest_country = world.parties[requester]["country"]
        if kind == "tbc":
            plan.lang = "en"
        fill_cargo(world, plan) if requester != "(TBC)" else _fill_tbc(world, plan)
        if "q" in extra:
            plan.quoted = b(extra["q"])
        plan.fus = [b(n) for n in extra.get("fu", [])]
        plan.replies = [(b(n), k) for n, k in extra.get("reply", [])]
        if "requote" in extra:
            plan.requote = (b(extra["requote"][0]), extra["requote"][1])
        if "close" in extra:
            status = "Won" if kind.startswith("won") else "Lost"
            plan.close = (b(extra["close"]), status, extra.get("why", ""))
        if extra.get("no_rate_used"):
            plan.rate_used = None
        if extra.get("pending_lost"):
            plan.suggest = "pending_lost"
        if extra.get("pending_vendor"):
            plan.suggest = "pending_vendor"
        # vendors
        if kind == "silent":
            plan.vendors = _vendors(world, plan, received, n=2, replied=False)
            for v in plan.vendors:
                v.status, v.replied = "Asked", None
        elif kind == "answered":
            plan.vendors = _vendors(world, plan, received, n=2, statuses=("Quoted",))
            plan.vendors[0].replied = b(max(back - 1, 0))
            for v in plan.vendors[1:]:
                v.status, v.replied = "Asked", None
        elif kind == "tbc":
            plan.vendors = _vendors(world, plan, received, n=1, replied=False)
        elif plan.quoted:
            plan.vendors = _vendors(world, plan, received, before=plan.quoted)
            if plan.close and plan.close[1] == "Won" and not any(v.status == "Quoted" for v in plan.vendors) \
                    and plan.vendors:
                plan.vendors[0].status, plan.vendors[0].replied = "Quoted", plan.quoted
        plans.append(plan)
    return plans


def plan_later_modes(world: World) -> list:
    """Two requests of today in the modes OppList learned after the first
    demo: Sea-Air from China to Chile and Cross-border Truck from Mexico."""
    today = world.today
    by_country = lambda country: sorted(a[0] for a in W.AGENTS if a[3] == country)
    plans = [CasePlan(kind="new", received=today, requester=by_country("Chile")[0], origin=W.ASIA_OCEAN["China"][0],
                      dest=W.LATAM_AIR["Chile"][0], mode="Sea-Air", commodity="LED lighting", kg=1200, cbm=5.5),
             CasePlan(kind="new", received=today, requester=by_country("Mexico")[0], origin="MONTERREY",
                      dest="CHICAGO, IL", mode="Cross-border Truck", commodity="auto parts")]
    for plan in plans:
        fill_cargo(world, plan)
    return plans


def _fill_tbc(world, plan):
    plan.commodity = "machinery spare parts"
    plan.incoterm = "EXW"
    plan.origin_country, plan.dest_country = "USA", "Peru"
    plan.origin_name = world.place_name(plan.origin, "USA")
    plan.dest_name = world.place_name(plan.dest, "Peru")
    plan.container_type, plan.container_qty = "20GP", 1
    plan.qty_text, plan.qty_raw = "1x20GP", "FCL:1 x 20GP"
    plan.amount, plan.rng_extra, plan.transit = 2350, 650, 24
    plan.ready = bd_fwd(plan.received, 5)


def plan_shipments(world: World) -> list:
    """The three won cases that became shipments."""
    b = lambda n: bd_back(world.today, n)
    ocean = CasePlan(kind="ship_ocean", received=b(31), requester="Andes Cumbre", origin="CHICAGO, IL",
                     dest="SAN ANTONIO", mode="FCL", commodity="industrial pumps", incoterm="FOB",
                     container_type="40HQ", container_qty=2, end_customer="Cordillera Norte Mining")
    air = CasePlan(kind="ship_air", received=b(24), requester=branding.office_key("JJH"), origin="HKG",
                   dest="CHICAGO, IL", mode="Air", commodity="electronic components", incoterm="FCA", kg=480)
    truck = CasePlan(kind="ship_truck", received=b(15), requester="Lakehead Freight", origin="BOLINGBROOK, IL",
                     dest="DALLAS, TX", mode="Truck", commodity="restaurant equipment", incoterm="DAP",
                     special="Transload")
    for plan, q, close in ((ocean, 30, 27), (air, 23, 21), (truck, 14, 13)):
        fill_cargo(world, plan)
        plan.quoted = b(q)
        plan.close = (b(close), "Won", "")
        plan.shipment = plan.kind[5:]
        plan.vendors = _vendors(world, plan, plan.received, n=2, statuses=("Quoted",), before=plan.quoted)
    truck.special = "Transload"
    air.cargo_lines = 2
    ocean.suggest = air.suggest = "accepted_won"
    return [ocean, air, truck]


def plan_history(world: World, count: int) -> list:
    """The older cases, 12 to 106 business days back: mostly closed."""
    rng, today = world.rng, world.today
    pool = requester_pool(world)
    backs = sorted((rng.randint(12, 106) for _ in range(count)), reverse=True)
    limit = bd_back(today, 1)
    plans = []
    lost_whys = (("price", 30), ("no_response", 16), ("forwarder", 9), ("cancelled", 7), ("slow", 5),
                 ("no_vendor", 3), ("unable", 2))
    for i, back in enumerate(backs):
        received = bd_back(today, back)
        requester = choose(rng, pool)
        group = False
        if plans and i in (17, 58):
            # one request, two lanes: the same requester and mail
            prev = plans[-1]
            requester, received, group = prev.requester, prev.received, True
        origin, dest, mode = lane_for(world, requester)
        plan = CasePlan(kind="history", received=received, requester=requester, origin=origin, dest=dest, mode=mode,
                        group_with_previous=group)
        fill_cargo(world, plan)
        if world.parties[requester]["country"] in LATAM and requester != "(TBC)":
            rng.random()                 # the old Handled-by roll, kept so the random stream stays put
        roll = rng.random()
        outcome = "Won" if roll < 0.27 else ("Lost" if roll < 0.87 else ("Dropped" if roll < 0.95 else "Quoted"))
        if outcome == "Dropped":
            plan.close = (min(bd_fwd(received, rng.choice((0, 1, 2))), limit), "Dropped",
                          rng.choice(("duplicate", "duplicate", "error")))
            plans.append(plan)
            continue
        plan.quoted = min(bd_fwd(received, rng.choice((0, 0, 1, 1, 1, 2, 3))), limit)
        plan.vendors = _vendors(world, plan, received, before=plan.quoted)
        if outcome == "Quoted":
            plan.fus = [d for d in (bd_fwd(plan.quoted, 2), bd_fwd(plan.quoted, 8)) if d <= limit]
            plans.append(plan)
            continue
        if outcome == "Won":
            close = bd_fwd(plan.quoted, rng.randint(1, 9))
            if (close - plan.quoted).days > 4 and rng.random() < 0.6:
                plan.fus = [bd_fwd(plan.quoted, 2)]
            plan.close = (min(close, limit), "Won", "")
            if not any(v.status == "Quoted" for v in plan.vendors) and plan.vendors and rng.random() < 0.7:
                plan.vendors[0].status, plan.vendors[0].replied = "Quoted", plan.quoted
        else:
            why = choose(rng, lost_whys)
            if why == "no_response":
                plan.fus = [bd_fwd(plan.quoted, 2), bd_fwd(plan.quoted, 8)]
                close = bd_fwd(plan.quoted, 14)
            elif why == "no_vendor":
                plan.quoted = None
                for v in plan.vendors:
                    v.status, v.replied = ("Declined", bd_fwd(v.asked, 1)) if rng.random() < 0.5 else ("Asked", None)
                close = bd_fwd(received, rng.randint(4, 7))
            else:
                if rng.random() < 0.45:
                    plan.fus = [bd_fwd(plan.quoted, 2)]
                if rng.random() < 0.3:
                    plan.replies = [(bd_fwd(plan.quoted, 3), "noted")]
                close = bd_fwd(plan.quoted, rng.randint(3, 12))
            plan.fus = [d for d in plan.fus if d < min(close, limit)]
            plan.replies = [(d, k) for d, k in plan.replies if d < min(close, limit)]
            plan.close = (min(close, limit), "Lost", why)
        plans.append(plan)
    # exactly three cases handled by another JoJo office (JQM)
    eligible = [p for p in plans if world.parties[p.requester]["country"] in LATAM
                and p.requester != "(TBC)" and not p.group_with_previous]
    for plan in plans:
        plan.handled_by = None
    for plan in eligible[-HANDLED_ELSEWHERE:]:     # the latest, so the month's dashboard line shows them
        plan.handled_by = "JQM"
    # a second re-quote round that ended in a win
    for plan in plans:
        if plan.close and plan.close[1] == "Won" and plan.quoted and plan.close[0] >= bd_fwd(plan.quoted, 5) \
                and plan.handled_by is None and world.parties[plan.requester]["type"] != "Direct Customer":
            plan.replies = [(bd_fwd(plan.quoted, 2), "asks")]
            plan.requote = (bd_fwd(plan.quoted, 3), "Different routing or carrier")
            plan.fus = []
            plan.kind = "history_requote"
            break
    # which closings came in as accepted suggestions read from the mail
    marked = 0
    for plan in plans:
        if marked >= 6:
            break
        if plan.close and plan.close[1] in ("Won", "Lost") and plan.close[2] != "no_response" and \
                plan.close[2] != "no_vendor" and plan.handled_by is None:
            plan.suggest = "accepted_" + plan.close[1].lower()
            marked += 1
    return plans


# ------------------------------------------------------------- executing --
def _loc_ids(world, plan):
    o = world.places[plan.origin]["id"] if plan.origin else None
    d = world.places[plan.dest]["id"] if plan.dest else None
    return o, d


def _subject(world: World, plan: CasePlan) -> str:
    rng = world.rng
    o, d = plan.origin_name.upper() if plan.lang == "en" else plan.origin_name, plan.dest_name
    mode = "AIR" if plan.mode == "Air" else plan.mode.upper()
    options = {
        "en": ("RATE REQUEST // {m} {q} {o} - {d}", "RFQ {m} {o} to {d} - {c}", "Quote request {o} > {d} / {q}"),
        "es": ("Cotizacion {m} {q} {o} - {d}", "Solicitud de tarifa {o} / {d} - {c}"),
        "pt": ("Cotacao {m} {q} {o} x {d}", "Pedido de cotacao {o} - {d} ({c})"),
    }[plan.lang]
    base = rng.choice(options).format(m=mode, q=plan.qty_text, o=o, d=d.upper() if plan.lang == "en" else d,
                                      c=plan.commodity)
    subject = base
    n = 1
    while parsing.subject_thread_key(subject) in world.thread_keys:
        n += 1
        subject = "{} - {}".format(base, plan.end_customer or plan.commodity) if n == 2 else "{} ({})".format(base, n)
    world.thread_keys.add(parsing.subject_thread_key(subject))
    return subject


def _vendor_subject(world, plan, vendor_key) -> str:
    base = "RATE CHECK // {} {} - {} / {}".format(
        "AIR" if plan.mode == "Air" else plan.mode.upper(), plan.origin_name.upper(), plan.dest_name.upper(),
        plan.qty_text)
    subject, n = base, 1
    while parsing.subject_thread_key(subject) in world.thread_keys:
        n += 1
        subject = "{} ({})".format(base, n)
    world.thread_keys.add(parsing.subject_thread_key(subject))
    return subject


class Executor:
    def __init__(self, world: World, repo: DemoRepository):
        self.world = world
        self.repo = repo
        self.conn = repo.conn
        self.rng = world.rng
        self.cases = []           # (plan, enquiry_id, opp_no, context)
        self.suggestions = []

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _touches_latam(plan) -> bool:
        return plan.origin_country in LATAM or plan.dest_country in LATAM

    def t(self, day: dt.date, start_min: int, span: int) -> dt.datetime:
        return at_time(day, start_min + self.rng.randint(0, span))

    def run(self, plan: CasePlan, previous=None) -> dict:
        w, repo, rng = self.world, self.repo, self.rng
        box = w.mailbox
        iris = w.iris()
        me = w.staff["Lucia Reyna"] if plan.handled_by == "JQM" else iris
        office_requester = w.parties[plan.requester]["type"] == "Office"
        if plan.requester == "(TBC)":
            sender = None
        else:
            sender = w.person(plan.requester, rng.randint(0, 3))
        ctx = {"plan": plan, "sender": sender}
        # 1. the request mail (or, for (TBC), our own mail to a vendor)
        when = self.t(plan.received, 8 * 60 + 5, 215)
        if plan.group_with_previous and previous is not None:
            conversation, request, subject = previous["conversation"], previous["request"], previous["subject"]
        elif sender is None:
            conversation = box.conversation()
            subject = _subject(w, plan)
            request = None
        else:
            conversation = box.conversation()
            subject = _subject(w, plan)
            to = [w.staff["Lucia Reyna"]] if plan.handled_by == "JQM" else [iris]
            cc = [iris] if plan.handled_by == "JQM" else ([w.colleague()] if rng.random() < 0.4 else [])
            request = box.send(conversation, subject, sender, to, when,
                               request_body(plan.lang, to[0].first, plan) + "\n" + sender.signature(), cc=cc,
                               attachments=("packing list.pdf",) if rng.random() < 0.3 else ())
        ctx.update(conversation=conversation, request=request, subject=subject)
        o_id, d_id = _loc_ids(w, plan)
        values = {
            "received_on": plan.received.isoformat(), "requester_party_id": w.parties[plan.requester]["id"],
            "end_customer": plan.end_customer, "owner": plan.owner, "handled_by": plan.handled_by,
            "service_mode": plan.mode, "container_type": plan.container_type, "container_qty": plan.container_qty,
            "air_weight_kg": plan.kg or None, "lcl_cbm": plan.cbm or None, "qty_raw": plan.qty_raw,
            "origin_country": plan.origin_country or None, "origin_loc_id": o_id,
            "dest_country": plan.dest_country or None, "dest_loc_id": d_id,
            "origin_alt": "Shipper's plant, {}".format(plan.origin_name) if plan.incoterm == "EXW" else None,
            "dest_alt": "Door delivery, {}".format(plan.dest_name) if plan.incoterm in ("DAP", "DDP") else None,
            "incoterm": plan.incoterm, "commodity": plan.commodity, "special_handling": plan.special,
            "status": "New", "created_by": "outlook" if request is not None else "manual",
            "conversation_id": conversation, "entry_id": request.entry_id if request is not None else None,
            "source_subject": subject,
        }
        if plan.group_with_previous and previous is not None:
            values["group_id"] = previous["group"]
        repo.at(when + dt.timedelta(minutes=6))
        enquiry_id = repo.create_enquiry(values, status_at=plan.received.isoformat(),
                                         note="Registered from Outlook" if request is not None else None)
        opp_no = self.conn.execute("SELECT opp_no FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()[0]
        ctx.update(id=enquiry_id, opp_no=opp_no, group=values.get("group_id") or enquiry_id)
        if w.parties[plan.requester]["type"] == "Direct Customer" and not self._touches_latam(plan):
            # scope is set by hand only: a direct customer's own shipment with
            # nothing Latin American in it is put Out, as she does under More
            repo.update("enquiry", enquiry_id, {"scope": "Out"})
        if plan.cargo_lines:
            self._cargo(enquiry_id, plan)
        # 2. vendors
        ctx["vendor_rows"] = []
        for vendor in plan.vendors:
            ctx["vendor_rows"].append(self._vendor(plan, ctx, vendor))
        if plan.kind == "tbc":
            self._tbc_mail(plan, ctx)
        # 3. the quote and what follows
        last = request
        if plan.quoted:
            when = self.t(plan.quoted, 14 * 60 + 5, 170)
            to = sender or iris
            valid = plan.quoted + dt.timedelta(days=30)
            last = box.send(conversation, "RE: " + subject, me, [to], when,
                            quote_body(to, plan, plan.amount, valid, me),
                            prev=last, cc=[iris] if me is not iris else [],
                            attachments=("JoJo quote {}.pdf".format(opp_no),))
            repo.at(when + dt.timedelta(minutes=3))
            repo.set_status(enquiry_id, "Quoted", plan.quoted.isoformat(),
                            note="Quote sent: {}".format(rate_lines(plan, plan.amount)[0]))
            ctx["quote_mail"] = last
        events = [(d, "fu", None) for d in plan.fus] + [(d, "reply", k) for d, k in plan.replies]
        if plan.requote:
            events.append((plan.requote[0], "requote", plan.requote[1]))
        events.sort(key=lambda e: (e[0], {"fu": 0, "reply": 1, "requote": 2}[e[1]]))
        seq = 0
        for day, what, extra in events:
            if what == "fu":
                seq += 1
                when = self.t(day, 9 * 60 + 10, 90)
                to = sender or iris
                last = box.send(conversation, "RE: " + subject, me, [to], when,
                                followup_body(to, seq, rng, me), prev=last, cc=[iris] if me is not iris else [])
                repo.at(when + dt.timedelta(minutes=2))
                if rng.random() < 0.6:
                    repo.add_followup(enquiry_id, day.isoformat(), channel="mail", source="mail",
                                      note="FU detected: {} [mid:{}]".format(("RE: " + subject)[:60], last.message_id))
                else:
                    repo.add_followup(enquiry_id, day.isoformat(), channel="mail")
            elif what == "reply":
                when = self.t(day, 11 * 60, 50)
                text = (REPLY_ASKS[plan.lang].format(iris.first, money(plan.amount * 0.9 * (plan.container_qty or 1)))
                        if extra == "asks" else REPLY_NOTED[plan.lang].format(iris.first))
                last = box.send(conversation, "RE: " + subject, sender, [iris], when,
                                reply_body(plan.lang, iris, text, sender), prev=last)
                repo.at(when + dt.timedelta(minutes=40))
                from . import detect_followups
                by_hand = plan.kind == "q_replied" and extra == "asks"
                repo.add_note(enquiry_id, day.isoformat(),
                              detect_followups.reply_note(sender.display, text, last.message_id, extra),
                              source="manual" if by_hand else "mail")
            else:
                seq = 0
                when = self.t(day, 15 * 60, 90)
                revised = int(plan.amount * 0.93)
                last = box.send(conversation, "RE: " + subject, iris, [sender], when,
                                quote_body(sender, plan, revised, day + dt.timedelta(days=21),
                                           iris, revised=True), prev=last)
                repo.at(when + dt.timedelta(minutes=5))
                repo.requote(enquiry_id, day.isoformat(), note="revised to {}".format(money(revised)),
                             reason=extra)
        ctx["last_mail"] = last
        # 4. the outcome
        if plan.close:
            self._close(plan, ctx)
        if plan.suggest == "pending_vendor":
            self._pending_vendor(plan, ctx)
        if plan.suggest == "pending_lost":
            self._pending_lost(plan, ctx)
        self.cases.append(ctx)
        return ctx

    def _cargo(self, enquiry_id, plan):
        rng = self.rng
        per = max(plan.kg // max(plan.cargo_lines, 1), 20)
        for i in range(plan.cargo_lines):
            pieces = rng.randint(1, 6)
            length, width, height = rng.choice(((120, 100, 110), (120, 80, 95), (80, 60, 70), (100, 100, 150)))
            self.repo.add_cargo_line(enquiry_id, {
                "label": str(i + 1), "pieces": pieces, "pack_word": rng.choice(("pallets", "ctns", "crates")),
                "dims_text": "{} x {} x {} cm".format(length, width, height), "length_cm": length,
                "width_cm": width, "height_cm": height, "weight_kg": per, "source": "mail"})

    def _vendor(self, plan, ctx, vp: VendorPlan):
        w, repo, rng, box = self.world, self.repo, self.rng, self.world.mailbox
        iris = w.iris()
        party = w.parties[vp.party_key]
        person = party["people"][0]
        conversation = box.conversation()
        subject = _vendor_subject(w, plan, vp.party_key)
        when = self.t(vp.asked, 12 * 60, 45)
        body = "\n".join([GREET["en"].format(person.first), "",
                          "Please send us your best rate for {} {} from {} to {}.".format(
                              mode_word(plan.mode, "en"), plan.qty_text, plan.origin_name, plan.dest_name),
                          "Commodity: {}, cargo ready {}.".format(plan.commodity, short_day(plan.ready or vp.asked)),
                          "", "Thank you.", "", SIGN["en"], iris.signature()])
        ask = box.send(conversation, subject, iris, [person], when, body)
        values = {"enquiry_id": ctx["id"], "vendor_party_id": party["id"], "asked_on": vp.asked.isoformat(),
                  "machine_status": vp.status, "conversation_id": conversation,
                  "thread_key": parsing.subject_thread_key(subject),
                  "notes": "Logged from Outlook: {}".format(subject), "source": "manual"}
        if vp.replied:
            when = self.t(vp.replied, 13 * 60, 55)
            if vp.status == "Quoted":
                ref = w.next_ref("".join(ch for ch in vp.party_key.upper() if ch.isalpha())[:3])
                text = "Please see our rate: {} {}, valid 30 days. Our ref: {}".format(
                    plan.mode if plan.mode != "Truck" else "FTL", money(vp.amount), ref)
                values.update(their_ref=ref, gist="{} {}".format(money(vp.amount), plan.mode))
            elif vp.status == "Declined":
                text = "Sorry, we cannot offer this lane at the moment - no space with our carriers."
                values.update(gist="declined, no space")
            else:
                text = "Got it, thank you. Our carriers are looking at it and we will write again soon."
                values.update(gist="checking")
            box.send(conversation, "RE: " + subject, person, [iris], when,
                     "\n".join([GREET["en"].format(iris.first), "", text, "", SIGN["en"], person.signature()]),
                     prev=ask)
            values["replied_on"] = vp.replied.isoformat()
        repo.at(ask_stamp(ask))
        row_id = repo.insert("vendor_request", values)
        return row_id

    def _tbc_mail(self, plan, ctx):
        """(TBC): registered from a mail we sent out; who asked is not known yet."""
        w, repo = self.world, self.repo
        repo.update("enquiry", ctx["id"], {"system_notes": "Registered from our own mail to a vendor; "
                                                          "requester still to be identified"})

    def _close(self, plan, ctx):
        w, repo, rng, box = self.world, self.repo, self.rng, self.world.mailbox
        iris = w.iris()
        day, status, why = plan.close
        sender = ctx["sender"]
        eid = ctx["id"]
        if status == "Dropped":
            repo.at(self.t(day, 16 * 60, 60))
            earlier = [c for c in self.cases if c["plan"].requester == plan.requester and c["id"] != eid]
            if why == "duplicate" and earlier:
                repo.mark_duplicate(eid, earlier[-1]["id"], day.isoformat(),
                                    note="same mail registered twice")
            else:
                repo.mark_registered_in_error(eid, day.isoformat(), note="Registered in error: a newsletter, not a request")
            return
        when = self.t(day, 15 * 60, 140)
        mail = None
        if status == "Won":
            text = WON_TEXT[plan.lang]
            if sender is not None:
                mail = box.send(ctx["conversation"], "RE: " + ctx["subject"], sender, [iris], when,
                                reply_body(plan.lang, iris, text, sender), prev=ctx["last_mail"])
            vendor_rows = ctx["vendor_rows"]
            quoted = [(vp, row_id) for vp, row_id in zip(plan.vendors, vendor_rows) if vp.status == "Quoted"]
            rate_used, awarded, vr = "own", None, None
            if plan.rate_used is None:
                rate_used = None
            elif quoted:
                vp, vr = quoted[0]
                ptype = w.parties[vp.party_key]["type"]
                rate_used = {"Carrier": "carrier", "Office": "office"}.get(ptype, "agent")
                awarded = w.parties[vp.party_key]["id"]
            elif rng.random() < 0.1:
                rate_used = "not_recorded"
            repo.at(when + dt.timedelta(minutes=12))
            source = "mail" if plan.suggest == "accepted_won" else "manual"
            note = ("{} <{}> {}".format(sender.display, sender.address, text.split(";")[0])[:400]
                    if source == "mail" else "Booking confirmed by {}".format(sender.name if sender else "the requester"))
            repo.set_status(eid, "Won", day.isoformat(), note=note, source=source, rate_used=rate_used,
                            awarded_vendor_id=awarded,
                            awarded_note="{} rate".format(quoted[0][0].party_key)
                            if rate_used in ("agent", "carrier", "office") and quoted else None)
            if rate_used == "agent" and vr:
                repo.award_vendor(eid, vr)
            if source == "mail" and mail is not None:
                self._accepted(ctx, "status", mail, "Won", day, quote=text.split(";")[0])
        else:
            reasons = {"price": "Price too high", "no_response": "No response from customer",
                       "forwarder": "Went with another forwarder", "cancelled": "Shipment cancelled",
                       "slow": "Rate too slow", "no_vendor": "No vendor rate", "unable": "Unable to handle"}
            reason = reasons[why]
            ours = plan.amount * (plan.container_qty or 1)
            theirs = int(ours * rng.uniform(0.8, 0.95) / 10) * 10
            if why in ("price", "forwarder", "cancelled", "slow") and sender is not None:
                text = LOST_TEXT[why if why != "forwarder" else "forwarder"][plan.lang]
                if why == "price":
                    text = text.format(money(theirs) + " all-in")
                mail = box.send(ctx["conversation"], "RE: " + ctx["subject"], sender, [iris], when,
                                reply_body(plan.lang, iris, text, sender), prev=ctx["last_mail"])
                heard = {"price": "What we heard: they took {} against our {}".format(money(theirs), money(ours)),
                         "forwarder": "What we heard: the shipper nominated their own forwarder at origin",
                         "cancelled": "What we heard: the consignee cancelled the order",
                         "slow": "What we heard: they needed a faster routing"}[why]
            elif why == "no_response":
                heard = "No answer after two follow-ups"
            elif why == "no_vendor":
                heard = "No vendor could offer the lane"
            else:
                heard = "Out of our scope: a courier shipment"
            repo.at(when + dt.timedelta(minutes=25))
            source = "mail" if plan.suggest == "accepted_lost" and mail is not None else "manual"
            note = "{} <{}> {}".format(sender.display, sender.address, text)[:400] if source == "mail" else heard
            repo.set_status(eid, "Lost", day.isoformat(), note=note, closed_reason=reason, source=source)
            if why == "price":
                repo.update("enquiry", eid, {"lost_their_price": "{} all-in".format(money(theirs)),
                                             "lost_our_price": "{} all-in".format(money(ours))})
                repo.touch("enquiry", eid)
            if source == "mail":
                self._accepted(ctx, "status", mail, "Lost", day, quote=text, reason=reason)
                repo.add_note(eid, day.isoformat(), heard)

    # -- suggestions ---------------------------------------------------------
    def _accepted(self, ctx, kind, mail, status, day, quote="", reason=None, extra=None):
        values = {"enquiry_id": ctx["id"], "opp_no": ctx["opp_no"], "kind": kind, "proposed_status": status,
                  "closed_reason": reason, "proposed_date": day.isoformat(),
                  "evidence_sender": "{} <{}>".format(mail.sender_name, mail.sender_address),
                  "evidence_date": mail.received_at[:16], "evidence_quote": (quote or mail.subject)[:400],
                  "confidence": "high", "entry_id": mail.entry_id, "source": "scan", "state": "accepted",
                  "decided_at": (dt.datetime.strptime(mail.received_at, "%Y-%m-%d %H:%M:%S")
                                 + dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")}
        values.update(extra or {})
        self.repo.at(dt.datetime.strptime(mail.received_at, "%Y-%m-%d %H:%M:%S") + dt.timedelta(minutes=30))
        values = {k: v for k, v in values.items() if v is not None}
        self.suggestions.append(self.repo.insert("suggestion", values))

    def _pending_vendor(self, plan, ctx):
        """We wrote to a vendor from Outlook and never logged it: a pending
        vendor suggestion from that mail."""
        w, box = self.world, self.world.mailbox
        iris = w.iris()
        taken = {vp.party_key for vp in plan.vendors}
        key = next(k for k in ("Magdalena Freight", "Inca Puerto", "Serra Azul", "Elbe Nord") if k not in taken
                   and k != plan.requester)
        person = w.parties[key]["people"][0]
        conversation = box.conversation()
        subject = _vendor_subject(w, plan, key)
        day = last_bday(w.today)
        when = at_time(day, 8 * 60 + 20)
        mail = box.send(conversation, subject, iris, [person], when,
                        "\n".join([GREET["en"].format(person.first), "",
                                   "Could you send us your rate for {} from {} to {}?".format(
                                       plan.qty_text, plan.origin_name, plan.dest_name), "", "Thank you.", "",
                                   SIGN["en"], iris.signature()]))
        self.repo.at(when + dt.timedelta(minutes=20))
        self.suggestions.append(self.repo.insert("suggestion", {
            "enquiry_id": ctx["id"], "opp_no": ctx["opp_no"], "kind": "vendor", "vendor_key": key,
            "vendor_domain": w.parties[key]["domain"], "proposed_date": day.isoformat(),
            "evidence_sender": "{} <{}>".format(iris.display, iris.address), "evidence_date": day.isoformat(),
            "evidence_quote": subject, "confidence": "medium", "entry_id": mail.entry_id,
            "source": "mail", "state": "pending"}))

    def _pending_lost(self, plan, ctx):
        w, box = self.world, self.world.mailbox
        iris, sender = w.iris(), ctx["sender"]
        day = bd_back(w.today, 1)
        when = at_time(day, 16 * 60 + 12)
        text = LOST_TEXT["price"][plan.lang].format(money(plan.amount * 0.88 * (plan.container_qty or 1)) + " all-in")
        mail = box.send(ctx["conversation"], "RE: " + ctx["subject"], sender, [iris], when,
                        reply_body(plan.lang, iris, text, sender), prev=ctx["last_mail"])
        self.repo.at(when + dt.timedelta(hours=10))
        self.suggestions.append(self.repo.insert("suggestion", {
            "enquiry_id": ctx["id"], "opp_no": ctx["opp_no"], "kind": "status", "proposed_status": "Lost",
            "closed_reason": "Price too high", "proposed_date": day.isoformat(),
            "evidence_sender": "{} <{}>".format(sender.display, sender.address),
            "evidence_date": mail.received_at[:16], "evidence_quote": text[:400], "confidence": "high",
            "entry_id": mail.entry_id, "source": "scan", "state": "pending",
            "note": "the requester says the client took another offer"}))


def ask_stamp(record) -> dt.datetime:
    return dt.datetime.strptime(record.received_at, "%Y-%m-%d %H:%M:%S") + dt.timedelta(minutes=4)


# -------------------------------------------------------------- shipments --
def awb_number(rng) -> str:
    serial = rng.randint(1000000, 9999999)
    return "180-{}{}".format(serial, serial % 7)


def build_shipments(ex: Executor) -> list:
    """The three shipments, each with its events tied to archived mails."""
    w, repo, rng = ex.world, ex.repo, ex.rng
    b = lambda n: bd_back(w.today, n)
    by_kind = {c["plan"].shipment: c for c in ex.cases if c["plan"].shipment}
    out = []
    iris = w.iris()

    def ops_conversation(ctx, party_key, subject):
        conversation = w.mailbox.conversation()
        repo.at(dt.datetime.combine(ctx["plan"].close[0], dt.time(17, 5)))
        repo.attach_thread(ctx["id"], "conversation", conversation, ctx["plan"].close[0].isoformat(),
                           note=subject, source="manual")
        return conversation

    def start(ctx, values):
        day = ctx["plan"].close[0]
        stamp = "{} 17:00".format(day.isoformat())
        repo.at(dt.datetime.combine(day, dt.time(17, 0)))
        values = dict(values, enquiry_id=ctx["id"], created_at=stamp, updated_at=stamp)
        return repo.insert("shipment", values)

    def event(ship_id, day, minutes, kind, title, mail, milestone=None, detail=None, eta=None, resolves=None):
        when = at_time(day, minutes)
        stamp = when.strftime("%Y-%m-%d %H:%M")
        repo.at(when + dt.timedelta(minutes=15))
        event_id = repo.insert("shipment_event", {
            "shipment_id": ship_id, "at": stamp, "kind": kind, "milestone": milestone, "title": title,
            "detail": detail, "eta": eta, "source": "mail", "message_id": mail.message_id,
            "sender": mail.sender_name, "resolves_event_id": resolves,
            "created_at": (when + dt.timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M")})
        values = {"updated_at": (when + dt.timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M")}
        if eta:
            values["eta"] = eta
        repo.update("shipment", ship_id, values)
        return event_id

    def say(conversation, subject, sender, to, day, minutes, text, prev=None, attachments=()):
        return w.mailbox.send(conversation, subject, sender, to, at_time(day, minutes),
                              "\n".join([GREET["en"].format(to[0].first), "", text, "", SIGN["en"],
                                         sender.signature()]), prev=prev, attachments=attachments)

    # ------------------------------------------------------------ ocean --
    ctx = by_kind["ocean"]
    agent = ctx["sender"]
    zim = w.person("ZIM")
    booking = "ZIMUSAV{}".format(rng.randint(1000000, 9999999))
    subj = "BOOKING {} // 2X40HQ CHICAGO - SAN ANTONIO".format(booking)
    conv = ops_conversation(ctx, "ZIM", subj)
    won = ctx["plan"].close[0]
    dep = b(17)
    planned_arrival = dep + dt.timedelta(days=22)
    new_eta = planned_arrival + dt.timedelta(days=6)
    ship = start(ctx, {
        "mode": "ocean", "title": "Chicago, IL to San Antonio", "shipper_name": "Harlan Pump Company",
        "shipper_address": "Industrial park, Bolingbrook, IL", "consignee_name": "Cordillera Norte Mining",
        "consignee_address": "Camino a Lo Aguirre km 9, Santiago, Chile", "carrier": "ZIM",
        "carrier_detail": W.VESSELS[0], "booking_no": booking, "mbl": booking, "hbl": "NWCSAI{}".format(rng.randint(10000, 99999)),
        "our_file": "JJC-OE-{}".format(rng.randint(2600, 2699)), "agent_ref": "AC-IMP-{}".format(rng.randint(400, 899)),
        "price_note": "{} per 40HQ + local charges".format(money(ctx["plan"].amount)),
        "via_points": "SAVANNAH, GA; CARTAGENA", "planned_pickup": b(20).isoformat(),
        "planned_departure": dep.isoformat(), "planned_arrival": planned_arrival.isoformat(),
        "eta": planned_arrival.isoformat()})
    m1 = say(conv, subj, zim, [iris], bd_fwd(won, 1), 9 * 60 + 12,
             "Booking confirmed: {}, 2x40HQ Chicago rail ramp to San Antonio via Savannah and Cartagena. "
             "Vessel {}, ETD Savannah {}.".format(booking, W.VESSELS[0], short_day(dep)),
             attachments=("Booking confirmation {}.pdf".format(booking),))
    e_booked = event(ship, bd_fwd(won, 1), 9 * 60 + 12, "milestone", "Booked", m1, milestone="Booked",
                     detail="Booking confirmed with ZIM")
    m2 = say(ctx["conversation"], "RE: " + ctx["subject"], iris, [agent], bd_fwd(won, 1), 10 * 60 + 5,
             "Booking confirmed with ZIM, cut-off at the Chicago ramp {}. The pre-alert follows as soon as the "
             "vessel has sailed.".format(short_day(b(21))), prev=ctx["last_mail"])
    event(ship, bd_fwd(won, 1), 10 * 60 + 5, "note", "Booking details sent to Andes Cumbre", m2)
    m3 = say(conv, "RE: " + subj, zim, [iris], b(22), 8 * 60 + 40,
             "Empty release issued for 2x40HQ at the Chicago ramp, release number attached.", prev=m1)
    event(ship, b(22), 8 * 60 + 40, "info", "Empty release received", m3)
    m4 = say(conv, "RE: " + subj, iris, [zim], b(20), 15 * 60 + 20,
             "Both containers were picked up at the shipper today and are gated in at the Chicago ramp.", prev=m3)
    event(ship, b(20), 15 * 60 + 20, "milestone", "Picked up", m4, milestone="Picked up",
          detail="Both containers loaded at the shipper and gated in at the ramp")
    m5 = say(conv, "RE: " + subj, zim, [iris], b(19), 11 * 60 + 2,
             "Rail departed Chicago with both units, arrival at Savannah expected in two days.", prev=m4)
    event(ship, b(19), 11 * 60 + 2, "info", "Rail departed Chicago", m5)
    m6 = say(conv, "RE: " + subj, zim, [iris], b(18), 7 * 60 + 55,
             "Containers gated in at Savannah Garden City terminal, loading on {} as planned.".format(W.VESSELS[0]),
             prev=m5)
    event(ship, b(18), 7 * 60 + 55, "info", "Gate in Savannah", m6)
    m7 = say(conv, "RE: " + subj, zim, [iris], dep, 18 * 60 + 30,
             "The vessel has sailed from Savannah with both containers on board. Next port Cartagena for "
             "transshipment.", prev=m6)
    e_dep = event(ship, dep, 18 * 60 + 30, "milestone", "Departed", m7, milestone="Departed",
                  detail="Sailed Savannah on {}".format(W.VESSELS[0]))
    ex._accepted(ctx, "shipment_milestone", m7, "Departed", dep, quote=m7.body.splitlines()[2],
                 extra={"shipment_id": ship, "message_id": m7.message_id,
                        "proposed_date": "{} 18:30".format(dep.isoformat())})
    m8 = say(ctx["conversation"], "RE: " + ctx["subject"], iris, [agent], bd_fwd(dep, 1), 9 * 60 + 30,
             "Pre-alert: {} sailed Savannah {}, ETA San Antonio {}. Draft BL attached for your review.".format(
                 W.VESSELS[0], short_day(dep), short_day(planned_arrival)), prev=m2,
             attachments=("Draft BL {}.pdf".format(booking),))
    event(ship, bd_fwd(dep, 1), 9 * 60 + 30, "note", "Pre-alert and draft BL sent", m8)
    m9 = say(ctx["conversation"], "RE: " + ctx["subject"], agent, [iris], bd_fwd(dep, 2), 12 * 60 + 15,
             "Draft BL approved, please release as original.", prev=m8)
    event(ship, bd_fwd(dep, 2), 12 * 60 + 15, "info", "Draft BL approved", m9)
    cartagena = b(9)
    m10 = say(conv, "RE: " + subj, zim, [iris], cartagena, 10 * 60 + 45,
              "Both containers were discharged at Cartagena for transshipment and wait for the connecting vessel.",
              prev=m7)
    event(ship, cartagena, 10 * 60 + 45, "note", "At Cartagena for transshipment", m10)
    m11 = say(conv, "RE: " + subj, zim, [iris], bd_fwd(cartagena, 1), 9 * 60 + 5,
              "Please note the containers were rolled at Cartagena: the connecting vessel is delayed by congestion.",
              prev=m10)
    e_delay = event(ship, bd_fwd(cartagena, 1), 9 * 60 + 5, "issue", "Delay", m11,
                    detail="Rolled at Cartagena, connecting vessel delayed")
    m12 = say(conv, "RE: " + subj, zim, [iris], bd_fwd(cartagena, 2), 14 * 60 + 40,
              "Revised ETA San Antonio {} on {}.".format(short_day(new_eta), W.VESSELS[1]), prev=m11)
    event(ship, bd_fwd(cartagena, 2), 14 * 60 + 40, "issue", "ETA moved to {}".format(short_day(new_eta)), m12,
          detail="Connecting vessel {}".format(W.VESSELS[1]), eta=new_eta.isoformat())
    repo.update("shipment", ship, {"carrier_detail": "{} then {}".format(W.VESSELS[0], W.VESSELS[1])})
    m13 = say(ctx["conversation"], "RE: " + ctx["subject"], iris, [agent], bd_fwd(cartagena, 2), 15 * 60 + 30,
              "Update: the containers were rolled at Cartagena, new ETA San Antonio {}. We are watching it.".format(
                  short_day(new_eta)), prev=m9)
    event(ship, bd_fwd(cartagena, 2), 15 * 60 + 30, "note", "Delay advised to Andes Cumbre", m13)
    m14 = say(ctx["conversation"], "RE: " + ctx["subject"], agent, [iris], bd_fwd(cartagena, 3), 11 * 60 + 50,
              "Noted, thanks. The consignee asks to keep them posted on the new vessel.", prev=m13)
    event(ship, bd_fwd(cartagena, 3), 11 * 60 + 50, "info", "Consignee informed by the agent", m14)
    out.append(ship)

    # -------------------------------------------------------------- air --
    ctx = by_kind["air"]
    office = ctx["sender"]
    kal = w.person("Korean Air")
    awb = awb_number(rng)
    won = ctx["plan"].close[0]
    subj = "AWB {} HKG-ORD 480 KG".format(awb)
    conv = ops_conversation(ctx, "Korean Air", subj)
    flight_day = bd_fwd(won, 3)
    arrive = bd_fwd(flight_day, 1)
    ship_air = start(ctx, {
        "mode": "air", "title": "Hong Kong to Chicago, IL", "shipper_name": "Lumaray Electronics Ltd",
        "shipper_address": "Kwai Chung, New Territories, Hong Kong", "consignee_name": "Quanta Labs",
        "consignee_address": "Schaumburg, IL", "carrier": "Korean Air",
        "carrier_detail": "KE0232 HKG-ICN / KE0037 ICN-ORD", "awb": awb, "hbl": None,
        "our_file": "JJC-AI-{}".format(rng.randint(2600, 2699)), "agent_ref": "JJH-AE-{}".format(rng.randint(100, 999)),
        "via_points": "ICN; ORD", "planned_pickup": bd_fwd(won, 2).isoformat(),
        "planned_departure": flight_day.isoformat(), "planned_arrival": arrive.isoformat(),
        "planned_delivery": bd_fwd(arrive, 2).isoformat(), "eta": arrive.isoformat(),
        "price_note": "{} all-in".format(money(ctx["plan"].amount))})
    a1 = say(conv, subj, kal, [iris], bd_fwd(won, 1), 22 * 60 + 10,
             "Booking confirmed on KE0232/KE0037, AWB {}, 480 kg.".format(awb))
    event(ship_air, bd_fwd(won, 1), 22 * 60 + 10, "milestone", "Booked", a1, milestone="Booked",
          detail="Space confirmed on KE0232 / KE0037")
    a2 = say(ctx["conversation"], "RE: " + ctx["subject"], office, [iris], bd_fwd(won, 2), 3 * 60 + 40,
             "Cargo was picked up from the shipper this afternoon, 6 pallets 480 kg.", prev=ctx["last_mail"])
    event(ship_air, bd_fwd(won, 2), 3 * 60 + 40, "milestone", "Picked up", a2, milestone="Picked up")
    a3 = say(ctx["conversation"], "RE: " + ctx["subject"], office, [iris], bd_fwd(won, 2), 9 * 60 + 5,
             "Received at the HKG cargo terminal, screening done.", prev=a2)
    event(ship_air, bd_fwd(won, 2), 9 * 60 + 5, "info", "Received at HKG terminal", a3)
    a4 = say(conv, "RE: " + subj, kal, [iris], flight_day, 2 * 60 + 30,
             "Flight KE0232 departed HKG with all 6 pieces.", prev=a1)
    event(ship_air, flight_day, 2 * 60 + 30, "milestone", "Departed", a4, milestone="Departed",
          detail="KE0232 departed HKG")
    a5 = say(conv, "RE: " + subj, kal, [iris], flight_day, 9 * 60 + 50,
             "Transferred at ICN onto KE0037, on schedule.", prev=a4)
    event(ship_air, flight_day, 9 * 60 + 50, "info", "Transit ICN", a5)
    a6 = say(conv, "RE: " + subj, kal, [iris], arrive, 8 * 60 + 20,
             "KE0037 landed ORD, cargo arrived and is at the terminal for recovery.", prev=a5)
    event(ship_air, arrive, 8 * 60 + 20, "milestone", "Arrived", a6, milestone="Arrived", detail="Landed ORD")
    ex._accepted(ctx, "shipment_milestone", a6, "Arrived", arrive, quote=a6.body.splitlines()[2],
                 extra={"shipment_id": ship_air, "message_id": a6.message_id,
                        "proposed_date": "{} 08:20".format(arrive.isoformat())})
    broker = w.person("Harborview Customs")
    a7 = say(conv, "RE: " + subj, broker, [iris], arrive, 11 * 60 + 5,
             "Entry filed; CBP selected the shipment for an exam, we will advise the release.", prev=a6)
    e_hold = event(ship_air, arrive, 11 * 60 + 5, "issue", "Customs hold", a7, detail="CBP exam selected")
    a8 = say(conv, "RE: " + subj, broker, [iris], bd_fwd(arrive, 1), 15 * 60 + 35,
             "Exam completed, cargo released by CBP.", prev=a7)
    event(ship_air, bd_fwd(arrive, 1), 15 * 60 + 35, "info", "Resolved: Customs hold", a8, resolves=e_hold)
    a9 = say(ctx["conversation"], "RE: " + ctx["subject"], iris, [office], bd_fwd(arrive, 2), 16 * 60 + 10,
             "Delivered to Quanta Labs this afternoon, POD attached.", prev=a3, attachments=("POD.pdf",))
    event(ship_air, bd_fwd(arrive, 2), 16 * 60 + 10, "milestone", "Delivered", a9, milestone="Delivered")
    a10 = say(ctx["conversation"], "RE: " + ctx["subject"], iris, [office], bd_fwd(arrive, 4), 10 * 60 + 15,
              "Please find our invoice for the destination charges attached.", prev=a9,
              attachments=("Invoice JJC-AI.pdf",))
    event(ship_air, bd_fwd(arrive, 4), 10 * 60 + 15, "milestone", "Invoiced", a10, milestone="Invoiced")
    repo.at(at_time(bd_fwd(arrive, 4), 10 * 60 + 40))
    repo.update("shipment", ship_air, {"closed": 1, "closed_reason": "Delivered",
                                       "updated_at": repo.stamp[:16]})
    out.append(ship_air)

    # ------------------------------------------------------------ truck --
    ctx = by_kind["truck"]
    agent = ctx["sender"]
    mesa = w.person("Blue Mesa Transport")
    won = ctx["plan"].close[0]
    load = "BM-{}".format(rng.randint(400000, 499999))
    subj = "LOAD TENDER // BOLINGBROOK, IL - DALLAS, TX / 18 PALLETS"
    conv = ops_conversation(ctx, "Blue Mesa Transport", subj)
    pickup_plan = bd_fwd(won, 2)
    ship_truck = start(ctx, {
        "mode": "truck", "title": "Bolingbrook, IL to Dallas, TX", "shipper_name": "Prairie Crossdock",
        "shipper_address": "Bolingbrook, IL", "consignee_name": "Solis Kitchens",
        "consignee_address": "Deep Ellum, Dallas, TX (no dock)", "carrier": W.REPLACED_TRUCKER,
        "carrier_detail": "53' dry van", "load_no": None, "customer_ref": "LH-{}".format(rng.randint(10000, 99999)),
        "price_note": "{} all-in + liftgate USD 95".format(money(ctx["plan"].amount)),
        "special": "liftgate at delivery", "planned_pickup": pickup_plan.isoformat(),
        "planned_delivery": bd_fwd(pickup_plan, 2).isoformat(), "eta": bd_fwd(pickup_plan, 2).isoformat()})
    prairie = w.person("Prairie Crossdock")
    t1 = say(conv, subj, iris, [prairie], bd_fwd(won, 1), 11 * 60 + 30,
             "Load booked with {} for pickup {}.".format(W.REPLACED_TRUCKER, short_day(pickup_plan)))
    event(ship_truck, bd_fwd(won, 1), 11 * 60 + 30, "milestone", "Booked", t1, milestone="Booked",
          detail="Booked with {}".format(W.REPLACED_TRUCKER))
    t2 = say(conv, "RE: " + subj, prairie, [iris], pickup_plan, 16 * 60 + 45,
             "The truck was a no show today, nobody came for the 18 pallets.", prev=t1)
    e_missed = event(ship_truck, pickup_plan, 16 * 60 + 45, "issue", "Pickup missed", t2,
                     detail="{} did not show up".format(W.REPLACED_TRUCKER))
    t3 = say(conv, "RE: " + subj, mesa, [iris], bd_fwd(pickup_plan, 1), 8 * 60 + 5,
             "Load accepted, load number {}; driver will be at Bolingbrook by 13:00 today.".format(load),
             prev=t2)
    repo.at(at_time(bd_fwd(pickup_plan, 1), 8 * 60 + 20))
    repo.update("shipment", ship_truck, {"carrier": "Blue Mesa Transport", "load_no": load,
                                         "bol": "BOL {}".format(rng.randint(7000000, 7999999))})
    e_replaced = event(ship_truck, bd_fwd(pickup_plan, 1), 8 * 60 + 5, "info",
                       "Carrier replaced: {} -> Blue Mesa Transport".format(W.REPLACED_TRUCKER), t3,
                       detail="after the missed pickup")
    event(ship_truck, bd_fwd(pickup_plan, 1), 8 * 60 + 6, "info", "Resolved: Pickup missed", t3,
          resolves=e_missed)
    t4 = say(conv, "RE: " + subj, mesa, [iris], bd_fwd(pickup_plan, 1), 9 * 60 + 40,
             "Truck dispatched, driver Hector, unit 2214.", prev=t3)
    event(ship_truck, bd_fwd(pickup_plan, 1), 9 * 60 + 40, "milestone", "Dispatched", t4, milestone="Dispatched")
    t5 = say(conv, "RE: " + subj, mesa, [iris], bd_fwd(pickup_plan, 1), 14 * 60 + 25,
             "Picked up 18 pallets at 13:10, BOL signed.", prev=t4, attachments=("BOL signed.pdf",))
    event(ship_truck, bd_fwd(pickup_plan, 1), 14 * 60 + 25, "milestone", "Picked up", t5, milestone="Picked up")
    t6 = say(conv, "RE: " + subj, mesa, [iris], bd_fwd(pickup_plan, 2), 10 * 60 + 5,
             "In transit, truck passed Springfield, MO this morning.", prev=t5)
    event(ship_truck, bd_fwd(pickup_plan, 2), 10 * 60 + 5, "info", "In transit, Springfield MO", t6)
    first_try = bd_fwd(pickup_plan, 3)
    t7 = say(conv, "RE: " + subj, mesa, [iris], first_try, 9 * 60 + 20,
             "Consignee has no dock and our trailer has no liftgate, delivery refused. Please advise.", prev=t6)
    e_lift = event(ship_truck, first_try, 9 * 60 + 20, "issue", "Liftgate", t7,
                   detail="No dock at the consignee, trailer without liftgate")
    t8 = say(ctx["conversation"], "RE: " + ctx["subject"], iris, [agent], first_try, 11 * 60,
             "The consignee has no dock; we are rebooking the delivery with a liftgate truck for {}.".format(
                 short_day(bd_fwd(first_try, 2))), prev=ctx["last_mail"])
    event(ship_truck, first_try, 11 * 60, "note", "Redelivery with liftgate arranged", t8,
          eta=bd_fwd(first_try, 2).isoformat())
    t9 = say(conv, "RE: " + subj, mesa, [iris], bd_fwd(first_try, 1), 15 * 60 + 5,
             "We will bill a redelivery fee of USD 275 plus 3 hours detention. The liftgate was not on the tender.",
             prev=t7)
    event(ship_truck, bd_fwd(first_try, 1), 15 * 60 + 5, "issue", "Dispute", t9,
          detail="Redelivery USD 275 + detention disputed: the liftgate was in our tender")
    t10 = say(conv, "RE: " + subj, mesa, [iris], bd_fwd(first_try, 2), 8 * 60 + 30,
              "Liftgate unit assigned for today's redelivery.", prev=t9)
    event(ship_truck, bd_fwd(first_try, 2), 8 * 60 + 30, "info", "Resolved: Liftgate", t10, resolves=e_lift)
    repo.at(at_time(bd_fwd(first_try, 2), 8 * 60 + 45))
    repo.update("shipment", ship_truck, {"eta": bd_fwd(first_try, 2).isoformat()})
    # the delivery mail nobody has logged yet: a pending suggestion
    last_day = max(bd_fwd(first_try, 2), bd_back(w.today, 1))
    t11 = say(conv, "RE: " + subj, mesa, [iris], last_day, 16 * 60 + 40,
              "Delivered this afternoon with the liftgate truck, POD attached.", prev=t10, attachments=("POD.pdf",))
    repo.at(at_time(last_day, 18 * 60))
    ex.suggestions.append(repo.insert("suggestion", {
        "enquiry_id": ctx["id"], "opp_no": ctx["opp_no"], "kind": "shipment_milestone", "shipment_id": ship_truck,
        "message_id": t11.message_id, "proposed_status": "Delivered",
        "proposed_date": "{} 16:40".format(last_day.isoformat()), "note": t11.body.splitlines()[2],
        "evidence_sender": t11.sender_name, "evidence_date": t11.received_at[:16],
        "evidence_quote": t11.body.splitlines()[2], "confidence": "high", "entry_id": t11.entry_id,
        "source": "scan", "state": "pending"}))
    out.append(ship_truck)
    return out


# ---------------------------------------------------------------- extras --
def add_case_notes(ex: Executor) -> None:
    notes = ("Agent would rather use ONE here; push for 14 free days at the discharge port.",
             "Client is price driven - keep the origin charges separate in the quote.",
             "Check the hazmat class with the shipper before quoting air.",
             "Same consignee as last month's won case; reuse the delivery address.",
             "Victor says to hold the rate until the end of the month.",
             "Call Camila on Thursday about the second container.")
    picked = [c for c in ex.cases if c["plan"].kind in ("q_fu1", "q_fu2", "answered", "silent", "q_replied")]
    for ctx, body in zip(picked, notes):
        ex.repo.at(dt.datetime.combine(ctx["plan"].received, dt.time(17, 30)))
        ex.repo.insert("case_note", {"enquiry_id": ctx["id"], "body": body})


def extra_accepted(ex: Executor) -> None:
    """Accepted suggestions for a vendor request and a note, so the case
    page shows every kind the mail can bring in."""
    w, repo = ex.world, ex.repo
    iris = w.iris()
    done = 0
    for ctx in ex.cases:
        plan = ctx["plan"]
        if done >= 2 or plan.kind != "history" or not plan.quoted or plan.close is None or len(plan.vendors) > 1:
            continue
        taken = {vp.party_key for vp in plan.vendors}
        candidates = [k for k in vendor_candidates(w, plan) if k not in taken]
        if not candidates:
            continue
        key = candidates[0]
        person = w.parties[key]["people"][0]
        conversation = w.mailbox.conversation()
        subject = _vendor_subject(w, plan, key)
        when = at_time(plan.received, 12 * 60 + 50)
        mail = w.mailbox.send(conversation, subject, iris, [person], when,
                              "\n".join([GREET["en"].format(person.first), "",
                                         "Please quote {} from {} to {}.".format(plan.qty_text, plan.origin_name,
                                                                                 plan.dest_name),
                                         "", "Thank you.", "", SIGN["en"], iris.signature()]))
        repo.at(when + dt.timedelta(hours=3))
        repo.insert("vendor_request", {
            "enquiry_id": ctx["id"], "vendor_party_id": w.parties[key]["id"], "asked_on": plan.received.isoformat(),
            "source": "mail", "machine_status": "Asked", "conversation_id": conversation,
            "thread_key": parsing.subject_thread_key(subject),
            "notes": "From Outlook: {}".format(subject[:120])})
        ex._accepted(ctx, "vendor", mail, None, plan.received, quote=subject,
                     extra={"vendor_key": key, "vendor_domain": w.parties[key]["domain"], "source": "mail",
                            "confidence": "medium"})
        done += 1
    for ctx in ex.cases:
        plan = ctx["plan"]
        if plan.kind == "q_noted" and ctx.get("last_mail") is not None:
            mail = ctx["last_mail"]
            day = dt.date.fromisoformat(mail.received_at[:10])
            repo.at(dt.datetime.strptime(mail.received_at, "%Y-%m-%d %H:%M:%S") + dt.timedelta(hours=1))
            text = "Agent confirms the cargo ready date is unchanged"
            repo.add_note(ctx["id"], day.isoformat(), text)
            ex._accepted(ctx, "note", mail, None, day, quote=text, extra={"note": text, "source": "mail",
                                                                         "confidence": "low"})
            break


def pending_requote_and_start(ex: Executor) -> None:
    """Two pending suggestions the daily scan writes: a Re-quoted one (our
    revised quote read from the mail, the row detect_followups writes) and a
    Start shipment one on a planned pickup (the row shipments.propose_starts
    writes, from shipments.detect on the mail itself)."""
    from . import detect_followups, shipments
    w, repo = ex.world, ex.repo
    iris = w.iris()
    # Re-quoted: they asked us something, we sent revised rates this morning
    ctx = next(c for c in ex.cases if c["plan"].kind == "q_replied" and c.get("last_mail") is not None)
    plan = ctx["plan"]
    last = dt.datetime.strptime(ctx["last_mail"].received_at, "%Y-%m-%d %H:%M:%S")
    when = max(last + dt.timedelta(hours=3), at_time(last_bday(w.today), 9 * 60 + 15))
    revised = money(plan.amount * 0.94)
    line = "Ocean freight {} per {}".format(revised, plan.container_type or "40HQ") if plan.mode != "Air" \
        else "Air freight {} per kg".format(money(plan.amount * 0.94 / 1000))
    text = "Please see our revised rate below:\n\n{}\n\nValid until the end of the month.".format(line)
    mail = w.mailbox.send(ctx["conversation"], "RE: " + ctx["subject"], iris, [ctx["sender"]], when,
                          "\n".join([GREET["en"].format(ctx["sender"].first), "", text, "", SIGN["en"],
                                     iris.signature()]), prev=ctx["last_mail"])
    repo.at(when + dt.timedelta(minutes=40))
    ex.suggestions.append(repo.insert("suggestion", {
        "enquiry_id": ctx["id"], "opp_no": ctx["opp_no"], "kind": "status", "proposed_status": "Quoted",
        "proposed_date": when.date().isoformat(), "evidence_sender": "{} <{}>".format(iris.display, iris.address),
        "evidence_date": mail.received_at[:16], "evidence_quote": line, "confidence": "medium",
        "note": detect_followups.REQUOTED_NOTE, "source": "scan", "message_id": mail.message_id,
        "entry_id": mail.entry_id, "state": "pending"}))
    # Start shipment, planned: a recent home-office win with no shipment yet
    shipped = {row[0] for row in repo.conn.execute("SELECT enquiry_id FROM shipment")}
    won = [c for c in ex.cases if c["plan"].close and c["plan"].close[1] == "Won" and c["plan"].handled_by is None
           and c["id"] not in shipped and c["plan"].mode in ("FCL", "LCL", "Air") and c.get("sender") is not None]
    ctx = max(won, key=lambda c: (c["plan"].close[0], c["id"]))
    plan = ctx["plan"]
    after = max(plan.close[0], bd_back(w.today, 2))
    pickup = bd_fwd(w.today, 3)
    when = at_time(after, 10 * 60 + 5)
    body = ("Booking noted, thanks. Everything must be picked up by {} {} at the latest, "
            "as the warehouse shuts at 3 pm.".format(pickup.strftime("%b"), pickup.day))
    mail = w.mailbox.send(ctx["conversation"], "RE: " + ctx["subject"], ctx["sender"], [iris], when,
                          reply_body(plan.lang if plan.lang == "en" else "en", iris, body, ctx["sender"]),
                          prev=ctx.get("last_mail"))
    mode = shipments.mode_for(plan.mode)
    # detect() reads an archive search row: the top of the body is body_excerpt
    import types
    view = types.SimpleNamespace(body_excerpt=mail.body, received_at=mail.received_at, message_id=mail.message_id,
                                 sender_name=mail.sender_name, sender_address=mail.sender_address,
                                 entry_id=mail.entry_id)
    proposal = shipments.detect(view, mode=mode, today=w.today, plans=True)
    if proposal is None or proposal.kind != "milestone" or not proposal.planned:
        raise DemoError("the planned-pickup mail no longer reads as a planned milestone: {!r}".format(body))
    values = shipments.suggestion_values(proposal, view, source="scan")
    values["proposed_status"] = proposal.name + shipments.PLANNED
    values["note"] = "Start shipment: {}".format(proposal.detail) if proposal.detail else "Start shipment"
    values.update({"enquiry_id": ctx["id"], "opp_no": ctx["opp_no"], "state": "pending"})
    repo.at(when + dt.timedelta(minutes=50))
    ex.suggestions.append(repo.insert("suggestion", {k: v for k, v in values.items() if v is not None}))


# --------------------------------------------------------------- archive --
def write_archive(world: World, path: Path, today: dt.date) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    _remove(path)
    conn = mail_archive.open_archive(path)
    stamp = dt.datetime.combine(today, dt.time(7, 30))
    records = sorted(world.mailbox.records, key=lambda r: (r.received_at, r.seq))
    new, _ = mail_archive.store(conn, records, now=stamp)
    for position, folder in enumerate(sorted(world.mailbox.folders)):
        count = conn.execute("SELECT COUNT(*) FROM message_location WHERE folder_path = ?", (folder,)).fetchone()[0]
        last = conn.execute("SELECT MAX(m.received_at) FROM message_location l JOIN message m ON "
                            "m.message_id = l.message_id WHERE l.folder_path = ?", (folder,)).fetchone()[0]
        conn.execute("INSERT INTO folder_sync(folder_path, position, last_received_at, last_synced_at, last_count) "
                     "VALUES (?, ?, ?, ?, ?)", (folder, position, last, mail_archive._stamp(stamp), count))
    mail_archive.set_meta(conn, "last_sync_at", mail_archive._stamp(stamp))
    mail_archive.set_meta(conn, SEED_SETTING, str(world.seed))
    mail_archive.set_meta(conn, DATE_SETTING, today.isoformat())
    conn.commit()
    _finish(conn)
    return new


def _finish(conn: sqlite3.Connection) -> None:
    """Reproducible bytes: rollback journal, no WAL left, pages packed."""
    conn.commit()
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("VACUUM")
    conn.close()


# ------------------------------------------------------------ leak guard --
def forbidden_list():
    """The leak check's list, where test_no_company_data looks for it:
    OPPLIST_FORBIDDEN, else forbidden.txt next to the project folder.
    None when there is none (CI): the guard is then skipped."""
    import os

    from . import paths
    named = os.environ.get("OPPLIST_FORBIDDEN")
    path = Path(named) if named else paths.PROJECT_DIR.parent / "forbidden.txt"
    return path if path.exists() else None


def leak_guard(out_dir: Path, archive_path: Path, list_path) -> None:
    """DemoError when the generated data contains a string from the leak
    check's list (an invented name that collides with a real one). Reads the
    files the way `leakcheck` does: every text value of every table."""
    from . import leakcheck

    forbidden = leakcheck.load(list_path)
    compiled = leakcheck._compile(forbidden)
    for db_file in (out_dir / DB_NAME, archive_path):
        if leakcheck.scan_text(leakcheck.sqlite_text(db_file), forbidden, compiled):
            raise DemoError("the generated data in {} contains strings from the forbidden list; run "
                            "python -m opplist leakcheck --all --root {} and change the demo words"
                            .format(db_file.name, out_dir))
    # the team site's snapshot: the same data as documents, and the few the site writes itself
    snapshot = out_dir / site_snapshot.FOLDER / site_snapshot.SNAPSHOT_NAME
    if snapshot.exists() and leakcheck.scan_text(snapshot.read_text(encoding="utf-8"), forbidden, compiled):
        raise DemoError("the team site's snapshot {} contains strings from the forbidden list; run "
                        "python -m opplist leakcheck --all --root {} and change the demo words"
                        .format(snapshot.name, out_dir))


# ------------------------------------------------------------------ main --
def generate(out_dir, seed: int = 42, today=None, force: bool = False, leak_list="auto", site_data=None) -> dict:
    """Build the demo database, the archive and the team site's snapshot
    under out_dir. Returns counts (see counts()) plus 'notes'. `leak_list`
    is the forbidden list the output is checked against ('auto':
    forbidden_list(); None: no check). `site_data`: a folder that gets a copy
    of the snapshot (site/data, to serve the site as plain files)."""
    out_dir = Path(out_dir)
    today = today if isinstance(today, dt.date) else (dt.date.fromisoformat(today) if today else dt.date.today())
    out_dir.mkdir(parents=True, exist_ok=True)
    notes = prepare(out_dir, force, today)
    if leak_list == "auto":
        leak_list = forbidden_list()
    db_path = out_dir / DB_NAME
    archive_path = out_dir / "mail-archive" / ARCHIVE_NAME
    # One build: the output depends on the seed and the date only, never on
    # whether a forbidden list happens to be present on this machine.
    build(out_dir, seed, today)
    # the offline team site (site/) reads one file: the documents OppList would push for this database
    snapshot = site_snapshot.write(out_dir, db_path, archive_path, today, seed)
    if leak_list:
        leak_guard(out_dir, archive_path, leak_list)
    if site_data:
        Path(site_data).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(snapshot, Path(site_data) / site_snapshot.SNAPSHOT_NAME)
    result = counts(db_path, archive_path)
    result["notes"] = notes
    result["site_snapshot"] = str(snapshot)
    result["site_docs"] = site_snapshot.count(snapshot)
    return result


def build(out_dir: Path, seed: int, today: dt.date, salt: int = 0) -> None:
    """One generation into out_dir (prepare() has cleared the way)."""
    db_path = out_dir / DB_NAME
    archive_path = out_dir / "mail-archive" / ARCHIVE_NAME
    conn = db.open_db(db_path)
    repo = DemoRepository(conn)
    world = World(seed, today, salt)
    db.set_setting(conn, "next_opp_number", FIRST_OPP)
    db.set_setting(conn, SEED_SETTING, seed)
    db.set_setting(conn, DATE_SETTING, today.isoformat())
    db.set_setting(conn, "company_short", branding.COMPANY_SHORT)
    # the demo shows the shipments pages (off in a new database), pushes to
    # the two local stores and reads quotes from the recordings
    for key, value in (("shipments", "1"), ("team_site_store", "on"), ("ratecard_store", "on"),
                       ("read_quote_on", "1"), ("read_quote_recorded", "1")):
        db.set_setting(conn, key, value)
    seed_reference(world, repo)
    seed_parties(world, repo)
    recent = plan_recent(world)
    ships = plan_shipments(world)
    history = plan_history(world, TOTAL_CASES - len(recent) - len(ships))
    plans = sorted(history + ships + recent, key=lambda p: p.received)
    ex = Executor(world, repo)
    previous = None
    for plan in plans:
        # a sibling lane must follow its first lane
        if plan.group_with_previous and (previous is None or previous["plan"].requester != plan.requester):
            plan.group_with_previous = False
        previous = ex.run(plan, previous)
    build_shipments(ex)
    add_case_notes(ex)
    extra_accepted(ex)
    pending_requote_and_start(ex)
    # after every other case, so the rest of the world is as it was before these modes existed
    for plan in plan_later_modes(world):
        ex.run(plan, None)
    site_docs = demo_showcase.run(ex)
    repo.commit()
    _finish(conn)
    write_archive(world, archive_path, today)
    site_snapshot.write_site_docs(out_dir, site_docs)


def counts(db_path, archive_path=None) -> dict:
    """What the demo holds, for the command's report and the tests."""
    conn = sqlite3.connect("file:{}?mode=ro".format(Path(db_path).as_posix()), uri=True)
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    out = {
        "parties": one("SELECT COUNT(*) FROM party"),
        "contacts": one("SELECT COUNT(*) FROM contact"),
        "party_domains": one("SELECT COUNT(*) FROM party_domain"),
        "locations": one("SELECT COUNT(*) FROM master_location"),
        "located": one("SELECT COUNT(*) FROM master_location WHERE lat IS NOT NULL AND lon IS NOT NULL"),
        "countries": one("SELECT COUNT(*) FROM country"),
        "enquiries": one("SELECT COUNT(*) FROM enquiry"),
        "by_status": dict(conn.execute("SELECT status, COUNT(*) FROM enquiry GROUP BY status ORDER BY status").fetchall()),
        "vendor_requests": one("SELECT COUNT(*) FROM vendor_request"),
        "followups": one("SELECT COUNT(*) FROM status_event WHERE kind = 'followup'"),
        "requotes": one("SELECT COUNT(*) FROM status_event WHERE kind = 'requote'"),
        "suggestions_accepted": one("SELECT COUNT(*) FROM suggestion WHERE state = 'accepted'"),
        "suggestions_pending": one("SELECT COUNT(*) FROM suggestion WHERE state = 'pending'"),
        "shipments": one("SELECT COUNT(*) FROM shipment"),
        "shipment_events": one("SELECT COUNT(*) FROM shipment_event"),
        "change_log": one("SELECT COUNT(*) FROM change_log"),
    }
    conn.close()
    if archive_path and Path(archive_path).exists():
        conn = sqlite3.connect("file:{}?mode=ro".format(Path(archive_path).as_posix()), uri=True)
        out["archive_messages"] = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        conn.close()
    return out


def report_lines(result: dict, out_dir) -> list:
    status = ", ".join("{} {}".format(k, v) for k, v in result["by_status"].items())
    lines = list(result.get("notes") or [])
    lines += [
        "demo data in {}".format(Path(out_dir).resolve()),
        "  parties          : {} ({} contacts, {} mail domains)".format(result["parties"], result["contacts"],
                                                                        result["party_domains"]),
        "  locations        : {} ({} with coordinates), {} countries".format(result["locations"], result["located"],
                                                                         result["countries"]),
        "  enquiries        : {} ({})".format(result["enquiries"], status),
        "  vendor requests  : {}".format(result["vendor_requests"]),
        "  follow-ups       : {}, re-quotes {}".format(result["followups"], result["requotes"]),
        "  suggestions      : {} accepted, {} pending".format(result["suggestions_accepted"],
                                                            result["suggestions_pending"]),
        "  shipments        : {} with {} events".format(result["shipments"], result["shipment_events"]),
        "  mail archive     : {} messages".format(result.get("archive_messages", 0)),
        "  team site        : {} documents in {}".format(result.get("site_docs", 0), result.get("site_snapshot", "")),
    ]
    return lines
