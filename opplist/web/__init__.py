"""The local web app: the case list, the case page, parties, tracking.

One user, one machine, no login, bound to 127.0.0.1. Flask and Jinja2 with a
little plain JavaScript; there is no build step and no package manager.

Every write goes through db.Repository so change_log stays complete, and every
derived value is read from v_enquiry / v_party_stats rather than recomputed.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import sqlite3
import threading
from pathlib import Path

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.exceptions import RequestEntityTooLarge
from markupsafe import Markup, escape

from .. import (assign_list, autoexport, branding, backup, cargo, cargo_checks, case_mail, checks, dates, routes, db, dedupe, enquiry_search, export_xlsx, intake, suggestions,
                firebase_pull, logs, mail_archive, matching, monthly, monthly_export, outlook_bridge,
                packing, party_dupes, paths, places, report, site_quotes, status as status_mod, tracking, parsing)
from . import forms
from .text import TEXT


#: FX4: the channels a follow-up is logged by on the case page - the ones
#: the team site's follow-up action writes (firebase_pull.CHANNELS)
FU_CHANNELS = (("mail", "Log as sent"), ("call", "Log a call"), ("whatsapp", "WhatsApp"),
               ("teams", "Teams"), ("other", "Other"))
#: FX4: Waiting on us and Answered on the case page, as on the team site
WAITING_WHAT_MAX = 1000
ANSWER_NOTE_MAX = 500
#: who Answered names when the case has no Handled by
ANSWERED_BY_DEFAULT = branding.HOME_OFFICE

# H2: the register form carries the mail's text in hidden fields - the body's
# cargo text and every line of a spreadsheet attachment - and Werkzeug 3.1
# refuses a form field over 500,000 bytes or a multipart form over 1,000
# parts by default. The server listens on 127.0.0.1 only, so limits no mail
# reaches are safe; MAX_CONTENT_LENGTH stays unset.
FORM_MEMORY_LIMIT = 64 * 1024 * 1024
FORM_PARTS_LIMIT = 20000


class _LargeFormRequest(Flask.request_class):
    """Before Flask 3.1 the limits are only the request class's attributes."""
    max_form_memory_size = FORM_MEMORY_LIMIT
    max_form_parts = FORM_PARTS_LIMIT


def create_app(db_path=None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = str(db_path or paths.DB_PATH)
    app.config["SECRET_KEY"] = "opplist-local"     # only signs the flash cookie
    if "MAX_FORM_MEMORY_SIZE" in app.config:       # Flask 3.1 and later
        app.config["MAX_FORM_MEMORY_SIZE"] = FORM_MEMORY_LIMIT
        app.config["MAX_FORM_PARTS"] = FORM_PARTS_LIMIT
    else:
        app.request_class = _LargeFormRequest
    # an app over any database but the live one - a test, a probe server on
    # 8799 over a copy - logs next to that database, never into the live
    # data\opplist.log (13c, 2026-09-30)
    if Path(app.config["DB_PATH"]).resolve() != Path(paths.LIVE_DB_PATH).resolve():
        logs.redirect(Path(app.config["DB_PATH"]).resolve().parent / "opplist.log")
    app.jinja_env.globals["t"] = TEXT
    app.jinja_env.globals["forms"] = forms
    app.jinja_env.globals["party_types"] = db.PARTY_TYPES        # Q2b: one list for every type menu
    from .. import parts as parts_mod
    app.jinja_env.globals["vendor_parts"] = parts_mod.PARTS          # Q2b: what a vendor is asked to quote
    app.jinja_env.globals["place_picks"] = _place_picks
    app.auto_exporter = autoexport.AutoExporter(app.config["DB_PATH"])
    from .. import firebase_sync
    # W1a: the team site; only over the live database, never in the tests
    app.firebase_syncer = firebase_sync.Syncer(app.config["DB_PATH"])
    app.started_at = dt.datetime.now()

    @app.context_processor
    def _build_line():
        """'v9 · started 12:20 · code 11:55' at the foot of every page, so
        a stale server is spotted at a glance, and ?v= on the assets so a
        browser never keeps an old stylesheet after a change."""
        conn = g.get("conn")
        badge = 0
        if conn is not None:
            try:
                badge = tracking.focus_count(conn)
            except Exception:                  # a half-migrated db: no badge
                logs.get("web").exception("tracking badge")
        # OP1: a skipped or failed morning step, or no run for more than a
        # working day - one red line on every page
        from .. import scheduled
        try:
            run_banner = scheduled.banner()
        except Exception:
            logs.get("web").exception("morning run banner")
            run_banner = None
        # OP2: 8765 not running the last commit - one red line on every page
        from .. import running
        try:
            code_banner = running.info()["red"]
        except Exception:
            logs.get("web").exception("code banner")
            code_banner = ""
        # FX3: shipments switched off hide everything of them
        from .. import shipments_switch
        return {"build": build_info(app, conn),
                "static_v": static_version,
                "tracking_badge": badge,
                "run_banner": run_banner,
                "code_banner": code_banner,
                "shipments_on": shipments_switch.enabled(conn) if conn is not None else False,
                "home_office": branding.HOME_OFFICE,
                "company_short": branding.company_short(conn)}

    @app.after_request
    def _schedule_auto_export(response):
        """A write starts the sixty second debounce.

        Only while export_target is 'publish'; a preview nobody reads is not
        worth rewriting, and the app should not be quietly rewriting files on
        a timer before the exports are meant to be live.
        """
        if request.method == "POST" and response.status_code < 400:
            try:
                app.auto_exporter.schedule()
            except Exception:                    # never break a save over this
                logs.get("web").exception("could not schedule the auto export")
            # W1a: about thirty seconds later, the team site
            try:
                app.firebase_syncer.schedule()
            except Exception:
                logs.get("web").exception("could not schedule the team site push")
        return response

    @app.after_request
    def _place_notes(response):
        """RD1b: what the master's naming rules did to a new place ('AIR'
        taken off, ', IL' put in) is said, never done quietly."""
        repo = g.get("repo")
        for note in (getattr(repo, "place_notes", None) or []) if response.status_code < 400 else []:
            flash(note.capitalize() if note[:1].islower() else note, "message")
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(exc):
        """H2: a form over the limits still says what happened, in the app."""
        size = request.content_length or 0
        logs.get("web").warning("form too large: %s %s, %s bytes - nothing saved",
                                request.method, request.path, size)
        return render_template("too_large.html", size_mb="{:.1f}".format(size / (1024 * 1024))), 413

    # ------------------------------------------------------- connection --
    @app.before_request
    def _open_db():
        g.conn = db.open_db(app.config["DB_PATH"])
        g.repo = db.Repository(g.conn, source="ui")
        # E2: one request is one action in change_log, however many commits
        g.repo.pin_action()

    #: FX3: the shipment pages and actions, closed while shipments are off
    SHIPMENT_ENDPOINTS = ("shipments_page", "shipment_page", "shipment_update_from_outlook", "shipment_start",
                          "shipment_milestone", "shipment_issue", "shipment_note", "shipment_event",
                          "shipment_edit", "shipment_close")
    SUGGESTION_ENDPOINTS = ("suggestion_accept", "suggestion_record", "suggestion_edit", "suggestion_put_back")

    @app.before_request
    def _shipments_switched_off():
        """FX3: while setting shipments is off, a shipment page or action
        says so, and a shipment suggestion cannot be accepted or put back."""
        from .. import shipments_switch, suggestions
        endpoint = request.endpoint or ""
        if endpoint not in SHIPMENT_ENDPOINTS and endpoint not in SUGGESTION_ENDPOINTS:
            return None
        if shipments_switch.enabled(g.conn):
            return None
        if endpoint in SHIPMENT_ENDPOINTS:
            return render_template("shipments_off.html", message=shipments_switch.OFF_PAGE), 404
        row = g.conn.execute("SELECT kind FROM suggestion WHERE id = ?",
                             ((request.view_args or {}).get("suggestion_id"),)).fetchone()
        if row is not None and row["kind"] in suggestions.SHIPMENT_KINDS:
            warn(shipments_switch.OFF_PAGE)
            return redirect(_safe_next(request.form.get("next")) if request.form.get("next")
                            else url_for("tracking_page"))
        return None

    @app.teardown_request
    def _close_db(exc):
        conn = g.pop("conn", None)
        if conn is not None:
            if exc is None:
                conn.commit()
            else:
                conn.rollback()
            conn.close()

    @app.template_filter("nice_date")
    def nice_date(value):
        return (value or "")[:10]

    @app.template_filter("md")
    def month_day(value):
        """'2026-09-20' -> '9/20', for the small FU marker on the list."""
        try:
            day = dt.date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return ""
        return "{}/{}".format(day.month, day.day)

    @app.template_filter("cut")
    def cut_filter(value, limit=30):
        return cut(value, limit)

    @app.template_filter("hide_mid")
    def hide_mid_filter(value):
        return hide_mid(value)

    @app.template_filter("hide_quote")
    def hide_quote_filter(value):
        """Q3: an event's note without its [quote:<id>] mark (the title)."""
        return site_quotes.hide_marks(value)

    @app.template_filter("quote_note")
    def quote_note_filter(value, known=None):
        """Q3: [mid:] hidden, [quote:<id>] a link Quote n when the quote is in
        OppList and hidden when it is not; the note is stored as it was."""
        return site_quotes.note_html(value, known)

    @app.template_filter("cargo_value")
    def cargo_value_filter(amount, currency=None):
        """D7a: a line's value of the goods, 'USD 12,500'; a value typed and
        refused comes back as typed."""
        if isinstance(amount, str):
            return amount
        return cargo.value_text(amount, currency)
    @app.template_filter("day")
    def day_filter(value, weekday=False, year=False):
        """'2026-09-07' -> 'Sep 7' (dates.month_day, month first, 11c);
        |day(weekday=True, year=True) -> 'Mon Sep 7, 2026'."""
        return dates.month_day(value, weekday=weekday, year=year)

    @app.template_filter("closed_why")
    def closed_why_filter(row):
        """FX7: why a follow-up suggestion was closed without writing anything."""
        return suggestions.closed_why(row) if row is not None and row["kind"] == "followup" else ""

    @app.template_filter("weekday")
    def weekday_filter(value):
        try:
            return dt.date.fromisoformat(str(value)[:10]).strftime("%a")
        except (TypeError, ValueError):
            return ""

    @app.template_filter("direction")
    def direction(value):
        # batch 18: a direction as she reads it - Triangle for the stored 'Cross-trade'
        from .. import words
        return words.direction_label(value)

    @app.template_filter("lane")
    def lane(row):
        origin = row["origin_loc"] or row["origin_country"] or "?"
        dest = row["dest_loc"] or row["dest_country"] or "?"
        return "{} - {}".format(origin, dest)

    @app.template_filter("next_touch")
    def next_touch(last_activity, cadence_days):
        """Last activity plus the cadence for this party."""
        try:
            day = dt.date.fromisoformat(str(last_activity)[:10])
        except (TypeError, ValueError):
            return None
        return (day + dt.timedelta(days=int(cadence_days or 45))).isoformat()

    @app.template_filter("packing")
    def packing_filter(line):
        """'1 pallet', '25 big bags', '3 crates': a cargo line's pieces and
        its Packing (v39), else its word as written."""
        from .. import cargo_card
        return cargo_card.what_of(dict(line))

    @app.template_filter("qty")
    def qty(row):
        if row["container_qty"] and row["container_type"]:
            return "{} x {}".format(row["container_qty"], row["container_type"])
        if row["air_weight_kg"]:
            return "{:g} kg".format(row["air_weight_kg"])
        if row["lcl_cbm"]:
            return "{:g} cbm".format(row["lcl_cbm"])
        return row["qty_raw"] or ""

    register_routes(app)
    return app


# ------------------------------------------------------------- helpers ----
_WEB_FILE = Path(__file__).resolve()


def static_version(filename: str) -> int:
    """The file's mtime, the cache key on every asset link."""
    try:
        return int((_WEB_FILE.parent / "static" / filename).stat().st_mtime)
    except OSError:
        return 0


def build_info(app, conn=None) -> dict:
    version = ""
    if conn is not None:
        try:
            version = db.get_setting(conn, "schema_version", "")
        except sqlite3.Error:
            version = ""
    code = dt.datetime.fromtimestamp(_WEB_FILE.stat().st_mtime)
    started = getattr(app, "started_at", None) or dt.datetime.now()
    from .. import running
    try:
        code_part = running.footer_code(code.strftime("%H:%M"))     # OP2: the commit from opplist-serve
    except Exception:
        code_part = "code {}".format(code.strftime("%H:%M"))
    return {
        "version": "v{}".format(version) if version else "",
        "started": started.strftime("%H:%M"),
        "code": code.strftime("%H:%M"),
        "line": "{} \u00b7 started {} \u00b7 {}".format(
            "v{}".format(version) if version else "v?",
            started.strftime("%H:%M"), code_part),
    }


def _opp_no(conn, enquiry_id) -> str:
    row = conn.execute("SELECT opp_no FROM enquiry WHERE id = ?",
                       (enquiry_id,)).fetchone()
    return row["opp_no"] if row else "?"


#: F1: the flash kinds. 'message' (Flask's default) is something done -
#: green, fades; WARN is a refusal or nothing done - amber, stays.
WARN = "warning"
#: Words that say nothing was done, for a message made elsewhere (say()).
_NOTHING_DONE = re.compile(r"\bnothing\b|\bnot (?:saved|logged|changed)\b", re.I)


def warn(message) -> None:
    """flash() for a refusal or for nothing saved, logged or changed (F1)."""
    flash(message, WARN)


def say(message, done=None) -> None:
    """flash() with its kind: `done` when given, else read off the words."""
    if done is None:
        done = not _NOTHING_DONE.search(message or "")
    flash(message, "message" if done else WARN)


def _status_said(conn, repo, enquiry_id, status, at, followed=True, written=None) -> str:
    """What a status form wrote, in words (13d): a confirm-only outcome or
    a Re-quoted double click never reads like a change. `written` is the
    message for a plain status change."""
    opp = _opp_no(conn, enquiry_id)
    day = dates.month_day(at) or at
    outcome = repo.last_status_outcome
    if outcome == "repeat":
        return ("{} was already re-quoted on {} with the same reason and note a moment ago; "
                "nothing new was recorded.".format(opp, day))
    if outcome == "confirmed":
        text = "Already {} on {}; nothing new was recorded.".format(status, day)
        return text + " Use Re-quoted for a revised quote." if status == "Quoted" else text
    last = conn.execute("SELECT kind FROM status_event WHERE enquiry_id = ? ORDER BY id DESC LIMIT 1",
                        (enquiry_id,)).fetchone()
    if status == "Quoted" and last is not None and last["kind"] == "requote":
        rounds = conn.execute("SELECT quote_rounds FROM v_enquiry WHERE id = ?",
                              (enquiry_id,)).fetchone()
        return "{} was already Quoted: recorded as a re-quote on {} (round {}).".format(
            opp, day, rounds["quote_rounds"] if rounds else "?")
    if not followed:
        now = conn.execute("SELECT status FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        return "{} on {} added to the timeline; {} stays {}.".format(
            status, day, opp, now["status"] if now else "?")
    return written or "Status set to {}".format(status)


def _place_picks(v, side) -> list:
    """The chips of one place picker for a template (batch 23): a route's
    own ('picks', worked out from its places), else the field's value -
    keys joined with '|', or the text a side holds - read against the
    master."""
    if v is None or not hasattr(v, "get"):
        return []
    own = v.get("picks")
    if own and side in own:
        return own[side]
    text = v.get("pod" if side == "pod" else side + "_loc")
    if not text:
        return []
    conn = g.get("conn")
    if conn is None:
        return [places.pick(None, key) for key in places.split(text)]
    return places.picks_from_text(conn, text)


def settings_lists(conn) -> dict:
    return {
        "owners": db.get_setting_list(conn, "owners"),
        "container_types": db.get_setting_list(conn, "container_types"),
        # v39 (batch 23): the cargo line's Packing dropdown
        "packing_units": packing.units_of(conn),
        "special_handling": db.get_setting_list(conn, "special_handling"),
        # two vocabularies since 2026-09-18; `closed_reasons` is
        # the pair of them, for a form that has no status to go on
        "closed_reasons": status_mod.closed_reasons(conn),
        "closed_reasons_lost": status_mod.closed_reasons(conn, "Lost"),
        "closed_reasons_cancelled": status_mod.closed_reasons(conn, "Cancelled"),
        "closed_reasons_dropped": status_mod.closed_reasons(conn, "Dropped"),
        # why a quote was done again (v22)
        "requote_reasons": status_mod.requote_reasons(conn),
    }


def reference(conn) -> dict:
    """The lists the enquiry form needs. Party and location are pick-only.
    FX4: the prospects too, after the active parties ('prospect' = 1; the
    Requester pickers mark them, the vendor picker leaves them out)."""
    party_sql = ("SELECT id, key, type, country, placeholder, (stage = 'prospect') AS prospect FROM party "
                 "WHERE merged_into IS NULL")
    # Placeholders last among the active: still there to pick, never in the way.
    party_sql += " ORDER BY (stage = 'prospect'), placeholder, key COLLATE NOCASE"
    return {
        "parties": conn.execute(party_sql).fetchall(),
        "countries": conn.execute(
            "SELECT name, region FROM country ORDER BY (name = 'Unknown') DESC, name"
        ).fetchall(),
        "locations": location_options(conn),
        "areas": conn.execute("SELECT key, name FROM master_area ORDER BY key").fetchall(),
        # v31: the Handled by select (the home office is the empty choice)
        "office_codes": db.office_codes(conn),
    }


def location_options(conn) -> list:
    """The origin / dest dropdown: every live
    master place with what the search box matches on (key, name, aliases,
    area) and what the option shows (key . name . type). On a database
    still on the old table: the keys, as before."""
    from . import db as db_mod

    if not db_mod.master_active(conn):
        return [{"key": r["key"], "country": r["country"], "status": r["status"],
                 "name": None, "type": None, "area": "", "search": r["key"].casefold()}
                for r in conn.execute("SELECT key, country, status FROM location ORDER BY key")]
    out = []
    for r in conn.execute(
            "SELECT l.key, l.name, l.type, l.country, l.status, a.key AS area, "
            "(SELECT group_concat(alias, ' ') FROM master_location_alias x "
            " WHERE x.location_id = l.id) AS aliases "
            "FROM master_location l LEFT JOIN master_area a ON a.id = l.area_id "
            "WHERE l.status <> 'retired' ORDER BY l.key"):
        out.append({
            "key": r["key"], "name": r["name"], "type": r["type"], "country": r["country"],
            "status": "To Confirm" if r["status"] == "to_confirm" else "OK",
            "area": r["area"] or "",
            "search": " ".join(x for x in (r["key"], r["name"], r["aliases"], r["area"]) if x).casefold(),
        })
    return out


def _today() -> str:
    return dt.date.today().isoformat()


def _safe_next(target) -> str:
    """Only a path on this app; anything else goes to the tracking page."""
    target = (target or "").strip()
    if target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("tracking_page")


def _case_of_thread(conn, mail):
    """(the case this mail's thread already is, how) or (None, '')."""
    link = _thread_of_mail(conn, mail)
    return link["case"], link["how"]


def _thread_of_mail(conn, mail, declined=()):
    from .. import case_numbers
    return _thread_link(conn, getattr(mail, "subject", "") or "", getattr(mail, "conversation_id", "") or "",
                        parsing.subject_thread_key(getattr(mail, "subject", "") or ""),
                        day=case_numbers.mail_day(mail), declined=declined)


def _existing_case(conn, subject, conversation, key, day=""):
    """(the thread's case, how) or (None, ''): _thread_link's case."""
    link = _thread_link(conn, subject, conversation, key, day)
    return link["case"], link["how"]


def _thread_link(conn, subject, conversation, key, day="", declined=()) -> dict:
    """The case this mail's thread is and the ones it was before.
    {'case': row or None, 'how', 'seen': [{'row', 'how', 'why'}]}.

    AL1: a case number in the subject names its case first, open or closed;
    then the case's own ids or a thread attached by hand. DD1:
    the same conversation or subject is the same case only while that case
    is open and came in at most dedupe_same_subject_days before the mail
    (dedupe.link_holds); the others are seen before - one line, nothing
    asked. `declined`: the case ids she said no to on this page (not asked
    again)."""
    from .. import case_numbers
    declined = {int(d) for d in declined if str(d).isdigit()}
    out = {"case": None, "how": "", "seen": []}
    named = case_numbers.valid_cases(conn, subject or "")
    if named:
        how = ("case number in the subject" if len(named) == 1 else
               "case numbers {} in the subject".format(", ".join(r["opp_no"] for r in named)))
        if named[0]["id"] not in declined:
            out.update(case=_case_line(conn, named[0]["id"]), how=how)
        return out
    days = dedupe.same_subject_days(conn)
    day = (day or _today())[:10]
    seen_ids = set(declined)
    for value, how in ((conversation, "same Outlook conversation"), (key, "same subject")):
        if not value:
            continue
        rows = db.thread_cases(conn, conversation=value, legacy=False) if how.endswith("conversation") \
            else db.thread_cases(conn, key=value, legacy=False)
        held = []
        for r in rows:
            line = _case_line(conn, r["id"])
            holds, why = dedupe.link_holds(line, day, days)
            if holds:
                held.append(r)
            elif r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                out["seen"].append({"row": line, "how": how, "why": why})
        rows = [r for r in held if r["id"] not in declined]
        if not rows or out["case"] is not None:
            continue
        if len(rows) > 1:
            # N1: a thread several cases share - the mail's date says whose
            owners = case_numbers.owners_of(conn, [r["id"] for r in rows])
            chosen = case_numbers.split_owners(owners, (day or "")[:10])
            pick = next(o for o in owners if o["id"] in chosen)
            others = case_numbers.shared_with(pick["id"], owners)
            if all(o["same_day"] for o in others):
                how += "; {} registered the same day".format(", ".join(o["opp_no"] for o in others))
            else:
                how += "; " + "; ".join("{} has it {}".format(o["opp_no"], o["up_to"]) for o in others)
            out.update(case=_case_line(conn, pick["id"]), how=how)
        else:
            out.update(case=_case_line(conn, rows[0]["id"]), how=how)
        seen_ids.add(out["case"]["id"])
    out["seen"] = [s for s in out["seen"] if not out["case"] or s["row"]["id"] != out["case"]["id"]]
    return out


def _case_line(conn, enquiry_id):
    """The case as the register page's lines show it (v_enquiry's row)."""
    return conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()


def _is_open(row) -> bool:
    return row is not None and (row["status"] or "") in dedupe.OPEN_STATUSES and not row["legacy"]


#: DD1: at most this many cases in "Already in OppList?"
ALREADY_MAX = 5


def _already(conn, link, matches, mail_day="") -> dict:
    """DD1 item 2: the one block "Already in OppList?" - the thread's case
    first, then the duplicate matches, one row per case (reasons joined),
    at most five - and the seen-before lines above the form.
    {'rows': [...], 'new_primary': bool, 'seen': [...]}"""
    rows, by_id = [], {}
    case = link.get("case")
    if case is not None:
        row = {"id": case["id"], "opp": case["opp_no"], "row": case, "why": [link.get("how") or ""],
               "band": "", "thread": True, "same_mail": False, "when": "",
               "action": "update" if _is_open(case) else "open"}
        rows.append(row)
        by_id[case["id"]] = row
    for m in matches or []:
        if m.enquiry_id in by_id:
            row = by_id[m.enquiry_id]
            row["why"] += [r for r in m.reasons if r not in row["why"]]
            row["band"] = row["band"] or m.band
            if m.same_mail and not row["same_mail"]:
                # the very mail this case was registered from: It is OPP-xxxx, not Update
                row.update(same_mail=True, action="duplicate", when=_ago(m.row["created_at"]))
            continue
        row = {"id": m.enquiry_id, "opp": m.opp_no, "row": m.row, "why": list(m.reasons), "band": m.band,
               "thread": False, "same_mail": m.same_mail, "action": "duplicate",
               "when": _ago(m.row["created_at"]) if m.same_mail else ""}
        rows.append(row)
        by_id[m.enquiry_id] = row
    rows = rows[:ALREADY_MAX]
    new_primary = not any(r["same_mail"] or (r["thread"] and r["action"] == "update") for r in rows)
    seen, taken = [], {r["id"] for r in rows}
    day = (mail_day or _today())[:10]
    for s in list(link.get("seen") or []) + [
            {"row": m.row, "how": "same subject" if "subject" in " ".join(m.reasons) else "same Outlook conversation",
             "why": ""} for m in getattr(matches, "seen_before", []) or []]:
        r = s["row"]
        if r["id"] in taken:
            continue
        taken.add(r["id"])
        apart = dedupe._days_apart(r["received_on"], day)
        seen.append({"id": r["id"], "opp": r["opp_no"], "row": r, "open": _is_open(r),
                     "how": "Same subject" if s["how"].endswith("subject") else "Same Outlook conversation",
                     "ago": "" if apart is None else ("today" if apart == 0 else
                                                      "{} day{} ago".format(apart, "" if apart == 1 else "s"))})
    return {"rows": rows, "new_primary": new_primary, "seen": seen}


def _deliver_to_of(conn, row) -> str:
    """Where a case's cargo goes, for the request group's list: its
    live cargo lines' deliver_to, else its dest alt."""
    places = []
    for line in cargo.lines_of(conn, row["id"]):
        if line.get("deliver_to") and line["deliver_to"] not in places:
            places.append(line["deliver_to"])
    return "; ".join(places) if places else (row["dest_alt"] or "")


def _archive_matches(subject: str, key: str) -> int:
    """How many archived mails carry this thread key (0 when there is no
    archive, or none yet: the mails may come later)."""
    search, _ = case_mail.open_search()
    if search is None:
        return 0
    try:
        clean = parsing.clean_subject(subject)
        return sum(1 for m in (search.by_subject(clean, limit=300) if clean else [])
                   if parsing.subject_thread_key(m.subject) == key)
    finally:
        search.__exit__(None, None, None)


def _tags(form) -> list:
    return [t for t in form.getlist("special_handling") if t]


#: Who asked?'s Add party: every type but one of our offices (Q2b: with
#: Coloader and Trucker)
WHO_ASKED_TYPES = tuple(t for t in db.PARTY_TYPES if t != "Office")


#: The roles a party can carry, in the
#: order the page shows them and the column stores them.
PARTY_ROLES = ("agent", "carrier_air", "carrier_ocean", "coloader", "trucker", "broker",   # Q2c: coloader
               "warehouse", "direct_customer", "own_office")


def _resolve_inline(repo, form, values, errors=None, locations=True) -> None:
    """'Add party...' and 'Add location...' create the row, then the case
    points at it. Free text never becomes a foreign key.
    A two-letter 'location' is refused and lands in `errors`.
    RD1c: the register page passes `locations=False` - its places are
    pending until Register (_pending_inline_places)."""
    new_party = (form.get("new_party_key") or "").strip()
    if new_party:
        values["requester_party_id"] = repo.add_party(
            new_party,
            type=form.get("new_party_type") or "Agent",
            country=form.get("new_party_country") or "Unknown",
            notes="Added from the inquiry form.", **_new_party_extra(form))
    for side in ("origin", "dest"):
        key = (form.get("new_{}_loc".format(side)) or "").strip()
        if key and locations:
            try:
                values["{}_loc".format(side)] = repo.add_location(
                    key, country=values.get("{}_country".format(side)) or "Unknown",
                    notes="Added from the inquiry form.")
            except ValueError as exc:
                if errors is None:
                    raise
                errors["{}_loc".format(side)] = str(exc)
                values["{}_loc".format(side)] = None


def _new_party_extra(form) -> dict:
    """RG2b (WC2): the new party's city and MFN no. as the form posts them
    (the MFN directory's office, or what she typed); a MFN no. makes it a
    MFN member."""
    out = {}
    city = " ".join((form.get("new_party_city") or "").split())[:80]
    number = "".join((form.get("new_party_mfn_no") or "").split())[:20]
    if city:
        out["city"] = city
    if number:
        out["mfn_no"], out["mfn_member"] = number, 1
    return out


def _pending_inline_places(conn, form, picked, values, errors) -> None:
    """RD1c: an 'Add location...' key (new_<side>_loc) on the register page
    is a pending place, like one added in a picker's small form: checked by
    the master's naming rules now (a refusal shows before Register), written
    with the case at Register. The side's key is the rules' spelling; a
    spelling that turns out to be on file is that place, nothing added."""
    for side in ("origin", "dest"):
        legacy = " ".join((form.get("new_{}_loc".format(side)) or "").split()).upper()
        if not legacy:
            continue
        field, text = side + "_loc", legacy
        if db.master_active(conn) and not db.TWO_LETTER_CODE.match(legacy):
            rules = db.place_rules(conn, legacy, values.get(side + "_country"))
            if rules["error"]:
                errors[field] = "; ".join(rules["notes"] + [rules["error"]])
                continue
            picked[side]["notes"] = picked[side].get("notes", []) + rules["notes"]
            if rules["found"] is not None:
                picked[side]["keys"] = [rules["found"]["key"]]
                continue
            text = rules["text"].upper()
        key = db.location_key(text)
        picked[side]["keys"] = [key]
        if not any(e["key"] == key for e in picked[side]["new"]):
            picked[side]["new"].append({"key": key, "written": text[:80], "name": "", "country": "",
                                        "port": 0, "airport": 0})


def _two_letter_errors(conn, values, lanes, errors) -> None:
    """A lane side that is a bare state / country code cannot be saved as
    a place: it would be created 'to confirm' with a guess behind it."""
    if not db.master_active(conn):
        return
    for side, index in (("origin", 0), ("dest", 1)):
        candidates = [values.get("{}_loc".format(side)) or ""] + [lane[index] for lane in lanes]
        for text in candidates:
            text = (text or "").strip().upper()
            if db.TWO_LETTER_CODE.match(text) and db.find_master_location(conn, text) is None:
                errors["{}_loc".format(side)] = (
                    "'{}' is a two-letter code - a state or a country. Pick the country and "
                    "leave the location empty, or type the place's name.".format(text))
                break


def _place_rule_errors(conn, items, errors) -> None:
    """RD1b: a place the save would create is checked by the master's
    naming rules first (db.place_rules): a country name, or a US / Canadian
    city with no state, stops the save with the reason in the form. items:
    (field, text, country). A text that names a place is left alone; a
    two-letter code is the two-letter guard's."""
    if not db.master_active(conn):
        return
    for field, text, country in items:
        text = (text or "").strip()
        if not text or field in errors or db.TWO_LETTER_CODE.match(text.upper()):
            continue
        rules = db.place_rules(conn, text, country)
        if rules["error"]:
            errors[field] = "; ".join(rules["notes"] + [rules["error"]])


def _dedupe_matches(conn, values, exclude_id=None, declined=()):
    """dedupe.find for the form; DD1: the cases she said no to on this page
    (`declined`) are not asked about again (their seen-before line stays)."""
    probe = dict(values)
    probe["id"] = exclude_id
    found = dedupe.find(conn, probe)
    skip = {int(d) for d in declined if str(d).isdigit()}
    if not skip:
        return found
    kept = dedupe.Found(m for m in found if m.enquiry_id not in skip)
    kept.seen_before = found.seen_before
    return kept


def _declined(form) -> list:
    """DD1: the case ids she answered Register a new case to, at load."""
    return [v for v in (form.get("declined_case") or "").split(",") if v.strip().isdigit()]


def _log_register(outcome: str, form) -> None:
    """One line per registration attempt, so the next question has an answer.

    Until now nothing about a registration reached the log, which made 'it did
    not save' impossible to look into after the fact.
    """
    logs.get("web").info(
        "register: %s | requester=%s received=%s lane=%s>%s mode=%s "
        "commodity=%r decision=%s from=%s",
        outcome,
        form.get("requester_party_id") or "-",
        form.get("received_on") or "-",
        form.get("origin_loc") or "-", form.get("dest_loc") or "-",
        form.get("service_mode") or "-",
        (form.get("commodity") or "")[:40],
        form.get("decision") or "-",
        form.get("created_by") or "manual")


#: 15b: what the case page never posts any more - the cargo lines give them
#: (Repository.sync_cargo_totals); qty_raw stays the text as received.
CARGO_FIELDS_OF_LINES = ("gross_kg", "gross_unit", "volume_cbm", "volume_unit", "air_weight_kg", "lcl_cbm",
                         "container_type", "container_qty", "commodity", "qty_raw")


#: 13g: one register form, one case. Every render of the form carries a
#: one-time token; the first save with it creates the case and is kept here
#: (token -> (enquiry id, opp no, saved at)), so the same form sent again
#: opens that case instead of making a second one (OPP-1495 / 0496). One
#: lock around the whole save, so two sends cannot both get past the check.
_REGISTER_LOCK = threading.Lock()
_SAVED_FORMS: dict = {}
_SAVED_FORMS_KEEP = 500


#: 13e: an OPP number typed into the search box - one to four digits,
#: with or without 'OPP', a hyphen or a space, and any leading zeros.
OPP_JUMP = re.compile(r"^(?:opp[\s\-]*)?0*(\d{1,4})$", re.IGNORECASE)


def _new_form_token() -> str:
    return secrets.token_hex(12)


def _remember_form(token, enquiry_id, opp_no) -> None:
    if not token:
        return
    _SAVED_FORMS[token] = (enquiry_id, opp_no, dt.datetime.now())
    while len(_SAVED_FORMS) > _SAVED_FORMS_KEEP:
        _SAVED_FORMS.pop(next(iter(_SAVED_FORMS)))


#: The Outlook context a registration from a mail carries (13g): it must
#: survive a refused save, or the case is stored as manual and the domain
#: is not learned.
MAIL_CONTEXT_FIELDS = ("created_by", "sender_address", "sender_internal",
                       "recipient_addresses", "learn_domain", "learn_for", "forward_note", "ack_offer",
                       "message_id",
                       # RG2a: what the reader filled (layer 1) and the official places it offered (F)
                       "reader_read", "official_new")


def _where(conn, row, entry_id=None) -> dict:
    """M2: what Outlook needs to find a mail - {'entry_id', 'message_id',
    'folders'} - for the registered mail (entry_id None, through
    case_mail.registered_mail) or an archive mail; without an archive, the
    ids the case has."""
    from .. import case_mail
    search, _info = case_mail.open_search()
    if search is None:
        return {"entry_id": entry_id or row["entry_id"] or "",
                "message_id": "" if entry_id else case_mail.registered_message_id(conn, row["id"]), "folders": ()}
    try:
        return case_mail.where(conn, row, search, entry_id)
    finally:
        search.__exit__(None, None, None)


def _title_where(conn, row):
    """M2: what the title card's Open in Outlook (and, MF1, the
    acknowledgment) opens - the registered mail, else the agent thread's
    newest mail; None with neither. MF1: a case registered from a mail
    always opens that mail - the archive's copy, else its own entry id and
    Message-ID through Outlook - never a newer mail of the thread."""
    from .. import case_mail
    search, _info = case_mail.open_search()
    if search is None:
        return _where(conn, row) if case_mail.has_registered(conn, row) else None
    try:
        mail = case_mail.registered_mail(conn, row, search)
        if mail is None and case_mail.has_registered(conn, row):
            return case_mail.where(conn, row, search)
        if mail is None:
            mail = case_mail.newest_mail(case_mail.case_mails(conn, search, row))
        if mail is None:
            return None
        return case_mail.where(conn, row, search, mail.entry_id)
    finally:
        search.__exit__(None, None, None)



def _can_acknowledge(conn, row, acknowledged_on=None) -> bool:
    """FX5: Acknowledge is offered on a New case registered from a mail that
    the scan has not seen acknowledged."""
    from .. import ack
    if (row["status"] or "") != "New" or not case_mail.has_registered(conn, row):
        return False
    return not (ack.acknowledged_on(conn, row["id"]) if acknowledged_on is None else acknowledged_on)


def _registered_mail(conn, row, where):
    """The registered mail: the archive's copy, else Outlook's; None with
    the warning said."""
    search, _info = case_mail.open_search()
    if search is not None:
        try:
            mail = case_mail.registered_mail(conn, row, search)
        finally:
            search.__exit__(None, None, None)
        if mail is not None:
            return mail
    try:
        return outlook_bridge.read_item(**where)
    except outlook_bridge.OutlookError as exc:
        warn(exc.message)
        return None


def _open_blank_ack(conn, row) -> bool:
    """FX5 (her answer of 13 Nov): what the old Acknowledge opened, without
    its text - a Reply All of the registered mail (found as Open in Outlook
    finds it) with the case number in the subject (ack.numbered_subject);
    a colleague's forward a new mail to the original sender, the colleague
    on Cc (ack.forward_target). Nothing in the body but what Outlook puts
    there; displayed, never sent; nothing written on the case (the scan
    records Acknowledged from the subject's number once she sends it)."""
    from .. import ack
    where = _title_where(conn, row)
    if where is None:
        warn("No mail of this case to reply to.")
        return False
    mail = _registered_mail(conn, row, where)
    if mail is None:
        return False
    try:
        if ack.is_colleague_forward(mail):
            target = ack.forward_target(conn, mail)
            outlook_bridge.blank_new(target["address"], target["cc"],
                                     ack.numbered_subject(target["subject"], row["opp_no"]))
            if not target["address"]:
                warn("The original sender's address was not found in the forward: fill in To.")
        else:
            outlook_bridge.blank_reply_all(where["entry_id"], lambda subject: ack.numbered_subject(subject, row["opp_no"]),
                                           message_id=where["message_id"], folders=where["folders"])
    except outlook_bridge.OutlookError as exc:
        warn(exc.message)
        return False
    flash("Acknowledgment opened in Outlook.")
    return True


def _mail_context(form) -> dict:
    if (form.get("created_by") or "") != "outlook":
        return {}
    return {name: form.get(name) or "" for name in MAIL_CONTEXT_FIELDS}


def _ago(stamp, now=None) -> str:
    """'Sep 30 16:17 (1 minute ago)' for a created_at stamp."""
    try:
        when = dt.datetime.fromisoformat(str(stamp)[:19])
    except (TypeError, ValueError):
        return str(stamp or "")
    minutes = int(((now or dt.datetime.now()) - when).total_seconds() // 60)
    if minutes < 1:
        rel = "just now"
    elif minutes < 60:
        rel = "{} minute{} ago".format(minutes, "" if minutes == 1 else "s")
    elif minutes < 48 * 60:
        hours = minutes // 60
        rel = "{} hour{} ago".format(hours, "" if hours == 1 else "s")
    else:
        rel = "{} days ago".format(minutes // (24 * 60))
    return "{} {} ({})".format(dates.month_day(when.date().isoformat()), when.strftime("%H:%M"), rel)


#: What the form posts that is not an enquiry column, and must survive a
#: rejected save so she does not type it twice.
INLINE_FIELDS = ("new_party_key", "new_party_type", "new_party_country",
                 "new_party_domain", "new_party_domain_shown", "new_origin_loc", "new_dest_loc",
                 # RG2b (WC2): the new party's city and MFN no., from the directory
                 "new_party_city", "new_party_mfn_no",
                 # batch 23: places added in a picker's small form, ports marked
                 "origin_loc_new", "dest_loc_new", "pod_new", "pod_mark")


_MAIL_NUMBER = re.compile(r"\d[\d.,]*\d|\d")


def _mail_html(text):
    """A line of mail text with its numbers highlighted (the CargoCheck sample)."""
    out, last = [], 0
    for found in _MAIL_NUMBER.finditer(text or ""):
        out.append(escape(text[last:found.start()]))
        out.append(Markup('<span class="num">{}</span>').format(found.group(0)))
        last = found.end()
    out.append(escape((text or "")[last:]))
    return Markup("").join(out)


def _cargo_checks_view(conn, values, lines, stated, totals) -> dict:
    """The Cargo card's checks, their count and its mail text (v34)."""
    case = {k: values.get(k) for k in ("service_mode", "qty_raw", "cargo_decided", "gross_kg", "volume_cbm")}
    found = cargo_checks.for_case(case, lines, stated, totals, cargo_checks.limits(conn))
    text = []
    for marker, block in cargo_checks.mail_text(lines, stated, values.get("qty_raw")):
        for n, one in enumerate((block or "").split("\n")):
            if one.strip():
                text.append((marker if n == 0 else ("total" if marker == "total" else ""), _mail_html(one)))
    return {"cargo_checks": found, "cargo_asks": cargo_checks.asks(found), "cargo_mail_text": text}


def _cargo_card(conn, enquiry_id, values, live, lines, t, archived=None) -> dict:
    """The case page's Cargo card (15b): the rows by mode, the totals strip,
    the mail text, the checks (13b's and 15b's) and the rail's words."""
    from .. import cargo_card, case_mail
    stated = cargo_checks.stated_of(conn, enquiry_id)
    if archived is None and not cargo_checks.mail_text(lines, stated, None) and values.get("entry_id"):
        # registered before 13b: the registered mail, from the archive
        search, _info = case_mail.open_search()
        if search is not None:
            try:
                row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
                archived = cargo_card.archived_text(search, row, conn)
            except Exception:                                # the card is a convenience
                logs.get("web").exception("cargo mail text")
                archived = None
            finally:
                search.__exit__(None, None, None)
    text = cargo_card.source_text(lines, stated, values.get("qty_raw"), archived)
    mode = values.get("service_mode")
    case = {k: values.get(k) for k in ("service_mode", "qty_raw", "cargo_decided", "gross_kg", "volume_cbm")}
    lim = cargo_checks.limits(conn)
    us_max = float(db.get_setting(conn, "us_chassis_max_kg", "17200") or 17200)
    found = cargo_checks.more_checks(lines, mode, live, text["text"], us_max, lim) + \
        cargo_checks.for_case(case, lines, stated, t, lim)
    return {"cargo_checks": found, "cargo_asks": cargo_checks.asks(found),
            "cargo_rows": cargo_card.rows(lines, mode, live), "cargo_strip": cargo_card.strip(t, lines, mode, live),
            "cargo_fcl": cargo_card.is_fcl(mode), "cargo_lines": lines,
            "cargo_text": dict(text, lines=[(marker, _mail_html(one)) for marker, one in text["lines"]]),
            "cargo_rail": cargo_checks.rail_words(found), "cargo_archived": archived,
            # the register page's card still reads these (until 15c)
            "cargo_mail_text": [(marker, _mail_html(one)) for marker, one in text["lines"]]}


#: 15c: the register page is the case page's cards, all open for input,
#: with one Register at the bottom. Its cargo lines are 15b's line editor
#: with every name prefixed cargo_<i>_ (a removed line leaves a gap in the
#: numbers); its routes after A are route_<i>_<field>. What the reader read
#: rides along with a line unseen and is saved with it.
REG_CARRIED = ("label", "source", "source_mail", "source_text", "weight_written", "weight_reading",
               "cbm_written", "decided", "dims_text", "stated_ref", "deliver_to",
               # batch 14: 'UN1993, Class 3, PG III (from SDS.pdf)' rides with its line
               "notes")
REG_ROUTE_FIELDS = ("origin_country", "origin_loc", "new_origin_loc", "origin_alt", "dest_country", "dest_loc",
                    "new_dest_loc", "dest_alt", "service_scope", "pod", "service_mode", "incoterm",
                    # batch 23: what the place pickers carry besides their keys
                    "origin_loc_new", "dest_loc_new", "pod_new", "pod_mark")
_CARGO_ROW = re.compile(r"^cargo_(\d+)_")
_ROUTE_ROW = re.compile(r"^route_(\d+)_")
#: the checks a register-page line shows inside its own editor (15c item 3)
LINE_ASK_CODES = ("unit", "size_unit")


def _row_indices(form, pattern) -> list:
    return sorted({int(found.group(1)) for found in (pattern.match(name) for name in form) if found})


def _num_text(value) -> str:
    return "{:g}".format(value) if isinstance(value, (int, float)) else str(value or "")


def _line_notes(line, typed_from="") -> dict:
    """Where a register-page line's values came from (15c item 2), shown
    under each field: 'from the mail: 1300 lbs', 'from the mail: 48 x 48 x
    48, no unit'. A line made from the mail's totals (no packing list)
    names the text it was read from; a line typed by hand has none."""
    if line.get("source") != "mail":
        return {}
    said = "from the mail: {}".format
    if line.get("attachment"):
        # batch 14: a line read off an attachment says which file
        said = ("from the attachment " + line["attachment"].replace("{", "{{").replace("}", "}}") + ": {}").format
    if typed_from:
        keys = [k for k, f in (("weight", "weight_value"), ("volume", "cbm_value"), ("containers", "container_qty"))
                if line.get(f) not in (None, "")]
        return dict({k: said(typed_from) for k in keys}, **({"commodity": "from the mail"} if line.get("commodity") else {}))
    notes = {}
    pieces = " ".join(p for p in (_num_text(line.get("pieces")) if line.get("pieces") is not None else "",
                                  line.get("pack_word") or "") if p)
    if pieces:
        notes["pieces"] = said(pieces)
    if line.get("dims_text"):
        notes["size"] = said(line["dims_text"] + ("" if line.get("dims_unit") else ", no unit"))
    if line.get("weight_written") or line.get("weight_value") is not None:
        notes["weight"] = said(line.get("weight_written") or "{} {}".format(
            _num_text(line["weight_value"]), line.get("weight_unit") or "kg"))
    if line.get("cbm_written") or line.get("cbm_value") is not None:
        notes["volume"] = said(line.get("cbm_written") or "{} cbm".format(_num_text(line["cbm_value"])))
    if line.get("container_qty"):
        notes["containers"] = said("{} x {}".format(line["container_qty"], line.get("container_type") or "?"))
    if line.get("commodity"):
        notes["commodity"] = "from the mail"
    return notes


def _draft_rows(draft_lines, values, source="mail", commodity_from="from the mail", extra=None) -> list:
    """The register page's lines from the reader (15c): its lines, or one
    made from the numbers the mail states when it has no packing list (the
    way 15b makes a first line), or one empty editor. The case's commodity
    goes to the first line when no line names one. D7a: with no line from
    the reader, the one line made takes the mail's HS code and goods value
    (`extra`, intake's draft.line_extra)."""
    from .. import cargo_lines_migration
    rows = [dict(l, _notes=_line_notes(l)) for l in draft_lines]
    if not rows:
        typed = dict(values)
        typed.setdefault("gross_kg", values.get("air_weight_kg"))
        typed.setdefault("volume_cbm", values.get("lcl_cbm"))
        line = cargo_lines_migration.line_from_typed(typed, source=source)
        if line:
            line = dict(line, weight_unit=line.get("weight_unit") or ("kg" if line.get("weight_value") is not None else None))
            rows = [dict(line, _notes=_line_notes(line, typed_from=values.get("qty_raw") or "the totals"))]
    if rows and (values.get("commodity") or "").strip() and not any((r.get("commodity") or "").strip() for r in rows):
        rows[0] = dict(rows[0], commodity=values["commodity"].strip())
        rows[0]["_notes"] = dict(rows[0].get("_notes") or {}, commodity=commodity_from)
    if not rows:
        rows = [{"source": source, "commodity": (values.get("commodity") or "").strip() or None}]
    if extra and not draft_lines and len(rows) == 1:
        rows[0] = dict(rows[0], **{k: v for k, v in extra.items() if rows[0].get(k) in (None, "")})
        rows[0]["_notes"] = dict(rows[0].get("_notes") or {},
                                 **{k: "from the mail" for k in ("hs_code", "cargo_value") if k in extra})
    for n, row in enumerate(rows):
        row["_idx"], row["_n"], row["_blank"] = n, n + 1, cargo.is_blank(row)
        row["_routes"] = None
    return rows


def _reg_routes(form) -> list:
    """The register page's routes after A, as posted (15c): one dict per
    route_<i>_ block that names a destination, with its key (the index) for
    the cargo lines' route ticks."""
    out = []
    for index in _row_indices(form, _ROUTE_ROW):
        prefix = "route_{}_".format(index)
        row = {k: (form.get(prefix + k) or "").strip() for k in REG_ROUTE_FIELDS}
        if not any(row[k] for k in ("dest_loc", "new_dest_loc", "dest_loc_new", "dest_country", "dest_alt")):
            continue
        for side in ("origin", "dest"):
            row[side + "_loc"] = row[side + "_loc"].upper()
        out.append(dict(row, key=str(index)))
    for n, row in enumerate(out):
        row["letter"] = routes.letter(n + 2)
    return out


def _draft_routes(conn, draft, values) -> tuple:
    """(routes B, C... , other readings) from the mail's lanes (15c item 1;
    13 item 17): the first lane the reader trusts is route A (the form's
    own fields), every other one a route of the same OPP; a lane it only
    offers as another reading is listed for Add as route."""
    extra, other = [], []
    primaries = [l for l in draft.lanes if l.get("primary")]
    for n, lane in enumerate(primaries[1:]):
        row = {k: "" for k in REG_ROUTE_FIELDS}
        row.update(origin_loc=lane.get("origin") or values.get("origin_loc") or "",
                   origin_country=lane.get("origin_country") or (values.get("origin_country") if not lane.get("origin") else "") or "",
                   # batch 23: 'X or Y' read from the mail is one route with both places
                   dest_loc=places.join([lane.get("dest") or ""] + list(lane.get("dest_more") or [])) if lane.get("dest") else "",
                   dest_country=lane.get("dest_country") or "",
                   service_mode=lane.get("service_mode") or values.get("service_mode") or "",
                   incoterm=lane.get("incoterm") or values.get("incoterm") or "",
                   service_scope=lane.get("service_scope") or "", pod=lane.get("pod") or "",
                   origin_alt=values.get("origin_alt") or "" if not lane.get("origin") else "")
        for side in ("origin", "dest"):
            first = places.split(row[side + "_loc"])[:1]
            if first and not row[side + "_country"]:
                found = conn.execute("SELECT country FROM location WHERE key = ?", (first[0],)).fetchone()
                row[side + "_country"] = found["country"] if found and found["country"] != "Unknown" else ""
        extra.append(dict(row, key=str(n + 1), letter=routes.letter(n + 2)))
    for lane in draft.lanes:
        if not lane.get("primary"):
            other.append({"origin": lane.get("origin") or "", "dest": lane.get("dest") or "",
                          "origin_country": lane.get("origin_country") or "", "dest_country": lane.get("dest_country") or ""})
    return extra, other


def _route_list(values, extra) -> list:
    """Route A and the routes after it, the way the checks and the totals
    read routes (letter, mode, scope, countries). Batch 23: a destination
    of several places ('A|B' on the form) reads 'A or B'."""
    def dest(text):
        keys = places.split(text)
        return {"dest_loc": keys[0] if keys else None, "dest_places": [{"id": None, "key": k} for k in keys]}
    first = dict({"id": "a", "letter": "A", "service_mode": values.get("service_mode"),
                  "service_scope": values.get("service_scope"), "origin_country": values.get("origin_country"),
                  "dest_country": values.get("dest_country"), "origin_loc": values.get("origin_loc")},
                 **dest(values.get("dest_loc")))
    out = [first]
    for r in extra:
        out.append(dict({"id": r["key"], "letter": r["letter"], "service_mode": r.get("service_mode") or first["service_mode"],
                         "service_scope": r.get("service_scope"), "origin_country": r.get("origin_country"),
                         "dest_country": r.get("dest_country"), "origin_loc": r.get("origin_loc") or r.get("new_origin_loc")},
                        **dest(r.get("dest_loc") or r.get("new_dest_loc"))))
    return out


def _register_cargo(conn, rows, stated, values, text, route_list) -> dict:
    """The register page's Cargo card (15c): the totals strip and the checks
    as the case will have them once registered. Every line editor counts
    for the numbering ('Line 2'); an empty one is left out of the totals
    and its checks are not shown. A size with no unit or a doubtful one
    is asked inside its own line; the rest are under the totals."""
    from .. import cargo_card
    lines = []
    for row in rows:
        line = cargo.normalise({k: v for k, v in row.items() if not k.startswith("_")})
        line["id"] = row["_idx"]
        line.setdefault("counted", 1)
        ref = row.get("stated_ref")
        line["stated_id"] = int(ref) + 1 if ref not in (None, "") and str(ref).isdigit() else None
        lines.append(line)
    blank = {row["_idx"] for row in rows if row.get("_blank")}
    live = [l for l in lines if l["id"] not in blank]
    mode = values.get("service_mode")
    case = {"service_mode": mode, "qty_raw": values.get("qty_raw"), "cargo_decided": values.get("cargo_decided"),
            "gross_kg": None, "volume_cbm": None}
    t = routes.totals(conn, None, lines=live, live_routes=route_list, case=case)
    lim = cargo_checks.limits(conn)
    us_max = float(db.get_setting(conn, "us_chassis_max_kg", "17200") or 17200)
    stated_view = [dict(s, id=j + 1) for j, s in enumerate(stated)]
    found = [c for c in cargo_checks.more_checks(lines, mode, route_list, text or "", us_max, lim) +
             cargo_checks.for_case(case, lines, stated_view, t, lim) if c.line_id not in blank]
    line_asks = {}
    for c in found:
        if c.code in LINE_ASK_CODES:
            line_asks.setdefault(c.line_id, []).append(c)
    unit_missing = any(c.code == "unit" and c.kind == "ask" for c in found)
    return {"cargo_strip": cargo_card.strip(t, live, mode, route_list), "cargo_fcl": cargo_card.is_fcl(mode),
            "cargo_checks": [c for c in found if c.code not in LINE_ASK_CODES], "line_asks": line_asks,
            "cargo_asks": cargo_checks.asks(found), "unit_missing": unit_missing,
            "cargo_rail": "unit to choose" if unit_missing else cargo_checks.rail_words(found),
            "cargo_totals": t, "cargo_counted": len([l for l in live if int(l.get("counted", 1) or 0)])}


def _reg_lines(rows) -> list:
    """The line editors of the register page: [{idx, n, line, carried,
    notes, routes}] - `routes` the route keys the line is ticked for (None:
    every route)."""
    out = []
    for row in rows:
        line = dict({k: v for k, v in row.items() if not k.startswith("_")}, **(row.get("_typed") or {}))
        carried = {k: (_num_text(line.get(k)) if k == "stated_ref" else (line.get(k) or "")) for k in REG_CARRIED}
        carried["source"] = carried["source"] or "manual"
        out.append({"idx": row["_idx"], "n": row["_n"], "line": line, "carried": carried,
                    "notes": row.get("_notes") or {}, "routes": row.get("_routes")})
    return out


def _register_view(conn, rows, stated, values, text, extra, other=(), status_at="") -> dict:
    """Everything the register page shows besides the request (15c)."""
    route_list = _route_list(values, extra)
    card = _register_cargo(conn, rows, stated, values, text, route_list)
    title = routes.title([r for r in route_list if r.get("dest_loc") or r.get("dest_country")]) \
        if (values.get("dest_loc") or values.get("dest_country")) else ""
    return {**card, "reg_lines": _reg_lines(rows), "reg_routes": extra, "reg_other_lanes": list(other),
            "reg_route_list": route_list, "reg_stated": stated, "reg_mail_raw": text or "",
            **_mail_parts(text),
            "reg_title": title, "reg_status": values.get("status") or "New",
            "reg_status_at": status_at or values.get("status_at") or values.get("received_on") or _today()}


def _register_lists(conn) -> dict:
    """What the register page's route editors offer (15c); batch 23: the
    place pickers' list."""
    return {"service_scopes": db.get_setting_list(conn, "service_scopes") or routes.DEFAULT_SCOPES,
            "place_data": places.picker_data(conn)}


#: batch 14: how the attachments ride in the register page's mail text (the
#: hidden mail_cargo_text keeps them on a refused save): a file's heading,
#: its lines, and at the bottom the files not read with the reason.
ATT_FILE, ATT_UNREAD, ATT_NOTHING = "[[file]] ", "[[unread]] ", "[[nothing]]"


def _attachment_text(draft) -> str:
    """The attachments' part of Show the mail text: under each file read,
    the lines the readers took something from; then the files not read."""
    out = []
    for f in getattr(draft, "attachment_files", []) or []:
        if f.read:
            out.append(ATT_FILE + f.name)
            out.extend(draft.attachment_used.get(f.name) or [ATT_NOTHING])
    for f in getattr(draft, "attachment_files", []) or []:
        if not f.read:
            out.append("{}{}: {}".format(ATT_UNREAD, f.name, f.reason))
    return "\n".join(out)


def _mail_parts(text) -> dict:
    """The register page's Show the mail text out of the mail text: the
    body's lines, then one block per attachment, then the files not read."""
    body, files, unread = [], [], []
    for one in (text or "").split("\n"):
        if not one.strip():
            continue
        if one.startswith(ATT_FILE):
            files.append({"name": one[len(ATT_FILE):], "lines": []})
        elif one.startswith(ATT_UNREAD):
            name, _, why = one[len(ATT_UNREAD):].rpartition(": ")
            unread.append({"name": name, "reason": why})
        elif files:
            files[-1]["lines"].append(None if one == ATT_NOTHING else _mail_html(one))
        else:
            body.append(_mail_html(one))
    for f in files:
        f["lines"] = [l for l in f["lines"] if l is not None]
    return {"reg_mail_lines": body, "reg_mail_files": files, "reg_mail_unread": unread}


def _read_mail_attachments(conn, draft, mail) -> None:
    """Register from Outlook reads the attachments too (batch 14): only a
    mail that asks us; Outlook saves copies into a folder of the app's own,
    they are read and deleted; nothing in the mailbox changes."""
    names = list(getattr(mail, "attachment_names", None) or [])
    if not names:
        return
    from .. import attachments as att
    if not intake.attachments_wanted(draft):
        shown = [n for n in names if not att.is_inline_image(n)]
        if shown:
            draft.notes.append("Attachments not read: this is our own mail, not a request ({}).".format(
                ", ".join(shown)))
        return
    try:
        files = outlook_bridge.read_attachments(mail.entry_id)
    except outlook_bridge.OutlookError as exc:
        logs.get("web").info("attachments refused: %s", exc.detail)
        draft.notes.append("The attachments could not be read: {}".format(exc.message))
        return
    try:
        intake.add_attachments(conn, draft, files)
    except Exception:                                    # never a reason not to register
        logs.get("web").exception("reading the attachments into the draft")
        draft.notes.append("The attachments could not be read into the form.")


#: batch 14 item 7: the mail Update from Outlook last read for a case, while
#: its attachments are offered on the case page (the Flask session cookie)
ATT_SESSION = "att_mail"


def _remember_attachments(enquiry_id, mail) -> None:
    """After Update from Outlook: the mail's attachments (inline pictures
    left out), so the case page can list them with 'Read attachments'."""
    from .. import attachments as att
    names = [n for n in (getattr(mail, "attachment_names", None) or []) if n and not att.is_inline_image(n)]
    if names and getattr(mail, "entry_id", ""):
        session[ATT_SESSION] = {"case": enquiry_id, "entry_id": mail.entry_id, "names": names[:20],
                                "subject": (getattr(mail, "subject", "") or "")[:120]}
    else:
        session.pop(ATT_SESSION, None)


def _attachment_panel(conn, enquiry_id, values) -> dict | None:
    """The case page's Attachments block: the names, and with ?att=1 what
    the readers find in them as suggestions (Accept, Dismiss); ?skip= the
    ones already accepted or dismissed. Nothing is written here."""
    from .. import attachments as att
    held = session.get(ATT_SESSION)
    if not held or held.get("case") != enquiry_id:
        return None
    panel = dict(held, read=False, findings=[], unread=[], error="", skip="")
    if request.args.get("att") != "1":
        return panel
    panel["read"] = True
    try:
        files = outlook_bridge.read_attachments(held["entry_id"])
    except outlook_bridge.OutlookError as exc:
        logs.get("web").info("attachments refused: %s", exc.detail)
        panel["error"] = exc.message
        return panel
    lines = [dict(r) for r in conn.execute(
        "SELECT * FROM enquiry_cargo WHERE enquiry_id = ? AND voided = 0 ORDER BY seq, id", (enquiry_id,))]
    try:
        found = intake.attachment_findings(conn, values, lines, files)
    except Exception:
        logs.get("web").exception("attachment findings")
        panel["error"] = "The attachments could not be read."
        return panel
    skip = [k for k in (request.args.get("skip") or "").split(",") if k]
    panel["findings"] = [f for f in found if f["key"] not in skip]
    panel["unread"] = [{"name": f.name, "reason": f.reason} for f in att.every_file(files) if not f.read]
    panel["skip"] = ",".join(skip)
    return panel


def _mail_cargo_text(mail) -> str:
    """The cargo lines of a mail being registered (Show the mail text, 15c)."""
    from .. import cargo_card
    try:
        return cargo_card.cargo_text_of(getattr(mail, "body_excerpt", "") or "")
    except Exception:                                    # the text is a convenience
        logs.get("web").exception("register mail text")
        return ""


def _reg_header(form=None, mail=None) -> dict:
    """The register page's header line: the mail's subject, sender and date
    (kept on the form, so a refused save shows them again)."""
    if mail is not None:
        return {"subject": mail.subject or "", "sender": mail.sender_name or mail.sender_address or "",
                "date": mail.received_on or "", "outlook": True}
    form = form or {}
    return {"subject": form.get("source_subject") or "", "sender": form.get("mail_sender") or "",
            "date": form.get("mail_date") or "", "outlook": (form.get("created_by") or "") == "outlook"}


def _scheduled_run(db_path=None):
    """The last scheduled run for the Tracking line (13f): its time, and
    'done' or 'skipped' with the reason; None when there has been none.
    OP1: 'offsite', the off-PC copy (scheduled.offsite_status)."""
    from .. import scheduled
    run = scheduled.last_run()
    offsite = scheduled.offsite_status(db_path or paths.DB_PATH)
    if (not run or not run.get("at")) and not offsite:
        return None
    run = run if run and run.get("at") else {}
    return {"at": run.get("at") or "", "state": run.get("state") or "", "reason": run.get("reason") or "",
            "command": run.get("command") or "", "ok": run.get("state") == "done",
            "commit": run.get("commit") or "", "offsite": offsite}


def _scheduled_offsite(db_path):
    """OP1: the off-PC copy for Settings and the Export page."""
    from .. import scheduled
    try:
        return scheduled.offsite_status(db_path)
    except Exception:
        logs.get("web").exception("off-PC copy status")
        return None


def _num_or_none(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _posted_stated(form) -> list:
    """The totals a mail states, carried by the register form as
    'stated_<j>_<field>' (v34)."""
    out, index = [], 0
    while any(name.startswith("stated_{}_".format(index)) for name in form):
        row = {}
        for key in cargo.STATED_FIELDS:
            value = (form.get("stated_{}_{}".format(index, key)) or "").strip()
            if key in ("pieces",):
                row[key] = int(float(value)) if value else None
            elif key in ("weight_value", "weight_kg", "cbm", "chargeable_kg"):
                row[key] = _num_or_none(value)
            else:
                row[key] = value or None
        out.append(row)
        index += 1
    return out


def _inline_values(form) -> dict:
    return {name: (form.get(name) or "").strip() for name in INLINE_FIELDS}


def _posted_checks(form) -> list:
    """The attachments' amber checks a refused register page shows again."""
    try:
        found = json.loads(form.get("attachment_checks") or "[]")
    except ValueError:
        return []
    return [str(c) for c in found if c] if isinstance(found, list) else []


def _posted_cargo(form, keep_blank=False) -> tuple:
    """The cargo rows of the register form, typed (cargo.clean): rows
    'cargo_<i>_<field>' in index order (15c: a removed line leaves a gap),
    blank rows skipped unless `keep_blank`. Each row carries _idx (its
    index), _n (its place among the line editors: 'line 2'), _blank,
    _routes (the route keys ticked, None for every route) and _notes (where
    its values came from). (rows, errors)."""
    rows, errors = [], {}
    for n, index in enumerate(_row_indices(form, _CARGO_ROW), start=1):
        prefix = "cargo_{}_".format(index)
        raw = {key: form.get(prefix + key) for key in
               cargo.FIELDS + ("source", "stated_ref", "counted_present", "dims_unit_present", "cbm_unit")
               if prefix + key in form}
        blank = cargo.is_blank(raw)
        if blank and not keep_blank:
            continue
        if raw.get("cbm_unit") == "cft" and (raw.get("cbm_value") or "").strip():
            raw.pop("cbm_written", None)          # 15c: a volume typed in cft is written as typed, not as read
        values, bad = cargo.clean(raw, packing.units_of(g.conn) if g.get("conn") is not None else None)
        values["source"] = raw.get("source") if raw.get("source") in ("mail", "manual") else "manual"
        ref = (raw.get("stated_ref") or "").strip()
        values["stated_ref"] = int(ref) if ref.isdigit() else None
        if not blank:
            for key, why in bad.items():
                errors["cargo line {} {}".format(n, key)] = why
        try:
            notes = json.loads(form.get(prefix + "whence") or "{}")
        except ValueError:
            notes = {}
        # 13g: the kg and cm columns from what was written (416 lbs is
        # 188.7 kg), as the card and the stored line have them - without
        # this a line in lbs weighed nothing and an Air case could not be saved
        row = cargo.normalise(values)
        # 13c: a box refused for its number shows what was typed again
        row["_typed"] = {key: raw.get(key) for key in bad if raw.get(key)}
        row.update(_idx=index, _n=n, _blank=blank, _notes=notes if isinstance(notes, dict) else {},
                   _routes=form.getlist(prefix + "route") if prefix + "routes_present" in form else None)
        rows.append(row)
    return rows, errors


def _missing_quantity(cargo_state, what) -> tuple:
    """(error key, message) when an Air case has no weight or an LCL case
    no volume on the register form (13g). It names what is on the page:
    with no lines, the typed box under Quantity; with lines (the typed boxes
    are not shown then), the line to open and fill in, or its Counted tick."""
    lines = cargo_state["lines"] if cargo_state else []
    if what == "weight":
        need, box = "Air needs a weight", ("gross_kg", "the Gross weight box (any unit)")
        fill = "open it with Edit on the Cargo card and give it a weight (kg or lbs) or a size with its unit"
        has = lambda l: l.get("weight_n") is not None or l.get("volumetric_kg") is not None
    else:
        need, box = "LCL needs a volume", ("volume_cbm", "the Volume box (cbm or cft)")
        fill = "open it with Edit on the Cargo card and give it its cbm, or a size with its unit"
        has = lambda l: l.get("volume_cbm") is not None
    if not lines:
        return box[0], "{}: add a cargo line, or fill in {}".format(need, box[1])
    counted = [n for n, l in enumerate(lines, start=1) if l.get("counted", 1)]
    if not counted:
        return "cargo", "{}: no cargo line is counted - tick Counted on the line that carries it".format(need)
    empty = next((n for n in counted if not has(lines[n - 1])), counted[0])
    return "cargo line {}".format(empty), "{}: {}".format(need, fill)


def _as_typed(form, values) -> dict:
    """The parsed values, with her own text back in whatever failed to parse.

    forms.read() turns '12 boxes' into None because it is not a number. Giving
    that None back to the form would blank the field she has to correct, so
    the raw text goes back instead.
    """
    typed = dict(values)
    for name in forms.ENQUIRY_FIELDS:
        if typed.get(name) in (None, "") and (form.get(name) or "").strip():
            typed[name] = form.get(name).strip()
    return typed


def _posted_vendors(form) -> list:
    """The vendor rows as they were posted, so a round trip keeps the ticks.

    The dedupe panel re-renders the form without the Outlook draft, and the
    ticks she made must survive that.
    """
    ticked = set(form.getlist("vendor"))
    rows = []
    for name in form:
        if not name.startswith("vendor_key_"):
            continue
        domain = name[len("vendor_key_"):]
        party_id = form.get("vendor_party_{}".format(domain), type=int)
        rows.append({
            "domain": domain,
            "key": (form.get(name) or "").strip(),
            "party_id": party_id,
            "known": bool(party_id),
            "checked": domain in ticked,
            "addresses": [a.strip() for a in
                          (form.get("vendor_addr_{}".format(domain)) or "").split(",")
                          if a.strip()],
            "note": "",
        })
    return sorted(rows, key=lambda r: r["domain"])


_DOMAIN_SHAPE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")


def _new_party_domain_refusal(conn, domain) -> str:
    """D1: the New party's Domain box as she typed it - '' when it may be
    remembered, else why not (shape, ours, a mailbox provider, another
    party's)."""
    domain = (domain or "").strip().lower()
    if not _DOMAIN_SHAPE.match(domain):
        return "'{}' is not a mail domain (like quillagents.com: no @, no spaces, one dot at least).".format(domain)
    if db.is_internal_domain(conn, domain):
        return ("{} is our own domain: it never belongs to a party. One of our offices is "
                "known by its prefix (JJK-OPS is JoJo JJK) - pick that office, or clear "
                "the domain.".format(domain))
    if matching.is_free_mail(domain):
        return "{} is a public mailbox, so the company behind it is not guessed - clear the domain.".format(domain)
    owner = conn.execute("SELECT p.id, p.key FROM party_domain d JOIN party p ON p.id = d.party_id "
                         "WHERE d.domain = ?", (domain,)).fetchone()
    if owner is not None:
        return "{} already belongs to {} (/parties/{}) - pick {} as the requester, or clear the domain.".format(
            domain, owner["key"], owner["id"], owner["key"])
    return ""


def _vendor_party(repo, domain, key, party_id):
    """The vendor's party id, created from the key with its domain
    remembered when it is not on file yet. An office vendor
    (domain 'office:<CODE>', 2026-09-25) is created as a Office
    and no domain is remembered: theirs is our own."""
    if party_id:
        return party_id
    if not key:
        return None
    if (domain or "").startswith(intake.OFFICE_DOMAIN):
        found = intake.office_party(repo.conn, domain[len(intake.OFFICE_DOMAIN):])
        if found:
            return found["id"]
        return repo.add_party(key, type="Office", country="Unknown", source="outlook",
                              notes="Added from a rate request we sent to this office.")
    party_id = repo.add_party(
        key, type="Unknown", country="Unknown", source="outlook",
        notes="Added from a mail we sent out.")
    repo.attach_domain(party_id, domain)          # never an internal one (13g)
    return party_id


def _log_vendor_requests(repo, form, enquiry_ids, asked_on) -> list:
    """Record who we asked, one vendor_request per case per vendor.

    A vendor that is not on file yet is created with the key on the form and
    its domain remembered, so the next mail to them matches by itself.
    Nothing here is written unless the box was ticked.
    """
    logged = []
    seen = set()
    for domain in form.getlist("vendor"):
        key = (form.get("vendor_key_{}".format(domain)) or "").strip()
        party_id = _vendor_party(
            repo, domain, key, form.get("vendor_party_{}".format(domain), type=int))
        if not party_id:
            continue
        if party_id in seen:
            # Two domains of one company (bqr-usa.example and bqrflorida.example are
            # both 'BQR'): the second only adds its domain to the party.
            continue
        seen.add(party_id)
        for enquiry_id in enquiry_ids:
            repo.insert("vendor_request", {
                "enquiry_id": enquiry_id, "vendor_party_id": party_id,
                "asked_on": asked_on, "source": "manual",
                "notes": "Asked in the mail this case was registered from.",
            })
        logged.append(key or domain)
    return logged


# ------------------------------------------------------------- sorting ----
#: What each list may be sorted by, and the SQL (or item attribute) behind it.
#: A whitelist, because the value arrives in the URL: nothing else reaches the
#: ORDER BY.
SORTS = {
    "enquiries": {
        "opp_no": "CAST(substr(opp_no, 5) AS INTEGER)",
        "received_on": "received_on",
        "requester": "requester_key COLLATE NOCASE",
        "end_customer": "end_customer COLLATE NOCASE",
        "service_mode": "service_mode COLLATE NOCASE",
        # two expressions: origin, then destination
        "lane": ["COALESCE(origin_loc, origin_country) COLLATE NOCASE",
                 "COALESCE(dest_loc, dest_country) COLLATE NOCASE"],
        "qty": "COALESCE(container_qty, air_weight_kg, lcl_cbm)",
        "commodity": "commodity COLLATE NOCASE",
        "scope": "scope",
        "hold": ["hold_until", "hold_reason COLLATE NOCASE"],
        "status": "status COLLATE NOCASE",
        "last_event_on": "last_activity_on",
        "owner": "owner COLLATE NOCASE",
        "request_group": "request_group",
    },
    "parties": {
        "key": "key COLLATE NOCASE",
        "type": "type COLLATE NOCASE",
        "country": "country COLLATE NOCASE",
        "region": "region COLLATE NOCASE",
        "stage": "stage COLLATE NOCASE",
        "mfn_member": "mfn_member",
        "total_enquiries": "total_enquiries",
        "last_activity_on": "last_activity_on",
    },
    "tracking": {
        "label": "label",
        "detail": "detail",
        "days": "days",
        "since": "since",
    },
}

#: The enquiry list's columns: id, label key, sort key (None = not
#: sortable), shown by default. The order here is the default order; what
#: she drags and ticks lives in setting list_columns and never touches the
#: sort, the filters or the exports.
LIST_COLUMNS = (
    ("opp_no", "col_opp", "opp_no", True),
    ("received_on", "col_received", "received_on", True),
    ("requester", "col_requester", "requester", True),
    ("service_mode", "col_mode", "service_mode", True),
    ("lane", "col_lane", "lane", True),
    ("incoterm", "col_incoterm", "incoterm", True),         # 2026-09-23
    ("qty", "col_qty", "qty", True),
    ("commodity", "col_commodity", "commodity", True),
    ("status", "col_status", "status", True),
    ("hold", "col_hold", "hold", True),
    ("last_event_on", "col_last_event", "last_event_on", True),
    ("end_customer", "col_end_customer", "end_customer", False),
    ("owner", "col_owner", "owner", False),
    ("request_group", "col_group", "request_group", False),
    ("scope", "col_scope", "scope", False),
)
COLUMN_IDS = tuple(c[0] for c in LIST_COLUMNS)
#: Where a column added later lands in a layout saved before it existed.
_INSERT_AFTER = {"incoterm": "lane"}
#: Longer commodities are cut on the list; the full text sits in the title.
COMMODITY_CUT = 30


def column_layout(conn) -> list:
    """[(id, label_key, sort_key, visible)] in her order; a column added to
    the code since she saved appears at the end, shown if it is a default."""
    by_id = {c[0]: c for c in LIST_COLUMNS}
    try:
        saved = json.loads(db.get_setting(conn, "list_columns", "") or "{}")
    except ValueError:
        saved = {}
    order = [c for c in (saved.get("order") or []) if c in by_id]
    # A column added after she saved goes at the end, unless the code says
    # where it belongs (incoterm after lane, 2026-09-23).
    for c in COLUMN_IDS:
        if c not in order:
            after = _INSERT_AFTER.get(c)
            order.insert(order.index(after) + 1 if after in order else len(order), c)
    if "visible" in saved:
        visible = {c for c in saved["visible"] if c in by_id}
        visible |= {c for c in COLUMN_IDS if c not in (saved.get("order") or [])
                    and by_id[c][3]}
    else:
        visible = {c for c in COLUMN_IDS if by_id[c][3]}
    return [(c, by_id[c][1], by_id[c][2], c in visible) for c in order]


#: The Message-ID a detected reply leaves in its note ('[mid: <...>]') is
#: for finding the mail again, not for reading, and it cannot wrap: left on
#: the page it pushed the timeline wider than its panel (2026-09-21). The
#: note in the database keeps it; the page shows it in the title only.
def hide_mid(text) -> str:
    return parsing.hide_mid(text)


def cut(text, limit=COMMODITY_CUT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"


#: How each list is sorted until she says otherwise.
DEFAULT_SORT = {
    "enquiries": ("received_on", "desc"),
    "parties": ("total_enquiries", "desc"),
    "tracking": ("days", "desc"),
}


def _sort_key(value):
    """Sort mixed values without tripping over None."""
    if value is None:
        return (1, "")
    if isinstance(value, (int, float)):
        return (0, value)
    return (0, str(value).casefold())


#: The Enquiries views: the rows each one renders. Scope and
#: the dropped rows are the page's filters, applied in the browser.
VIEW_WHERE = {
    # v31: Open is the home office's own work; a case another office handles is
    # listed under Other offices, whatever its status. FX1: a legacy row is
    # Legacy only while it is Dropped; with any other status it is listed
    # by that status (Open or Closed)
    "open": "status IN ('New','Quoted') AND handled_by IS NULL",
    "all": "",
    "closed": "status IN ('Won','Lost','Cancelled','Dropped') AND NOT (legacy = 1 AND status = 'Dropped')",
    "legacy": "legacy = 1 AND status = 'Dropped'",
    "unconfirmed": "status_confirmed = 0",
    "others": "handled_by IS NOT NULL",
}
#: A mode's outline label tone on the list (the tones are app.css's).
MODE_TONES = {"Air": "navy", "Sea-Air": "navy", "FCL": "accent", "LCL": "accent", "RORO": "accent",
              "Break Bulk": "accent", "Truck": "amber", "Cross-border Truck": "amber"}


def qty_text(row) -> str:
    """The list's Qty cell, as the qty filter writes it."""
    if row["container_qty"] and row["container_type"]:
        return "{} x {}".format(row["container_qty"], row["container_type"])
    if row["air_weight_kg"]:
        return "{:g} kg".format(row["air_weight_kg"])
    if row["lcl_cbm"]:
        return "{:g} cbm".format(row["lcl_cbm"])
    return row["qty_raw"] or ""


def sort_choice(conn, page: str) -> tuple:
    """(column, direction) from the URL, or the last one she chose.

    Remembered in `setting` rather than in the browser: she opens this app on
    one machine, and a sort she picked yesterday should still be there today.
    """
    column = request.args.get("sort")
    direction = request.args.get("dir")
    if column in SORTS[page]:
        direction = "asc" if direction == "asc" else "desc"
        remembered = db.get_setting(conn, "sort_" + page, "")
        if remembered != "{}:{}".format(column, direction):
            db.set_setting(conn, "sort_" + page,
                           "{}:{}".format(column, direction))
        return column, direction
    stored = str(db.get_setting(conn, "sort_" + page, "") or "")
    if ":" in stored:
        column, direction = stored.split(":", 1)
        if column in SORTS[page] and direction in ("asc", "desc"):
            return column, direction
    return DEFAULT_SORT[page]


def order_by(page: str, column: str, direction: str, tie: str = "") -> str:
    """The ORDER BY for a whitelisted column, nulls always last.

    An entry is one expression or a list of them (a lane sorts by origin,
    then destination). They are never split on commas: CAST(substr(x, 5)
    AS INTEGER) has one of its own, and splitting it broke every sort but
    the plain columns.
    """
    expression = SORTS[page][column]
    expressions = list(expression) if isinstance(expression, (list, tuple)) \
        else [expression]
    way = "ASC" if direction == "asc" else "DESC"
    parts = ["({}) IS NULL".format(expressions[0])]
    parts.extend("{} {}".format(part, way) for part in expressions)
    if tie:
        parts.append(tie)
    return " ORDER BY " + ", ".join(parts)


def _posted_lanes(form) -> list:
    """The lanes ticked on the form, as (origin, dest) pairs."""
    lanes = []
    for raw in form.getlist("lane"):
        if ">" not in raw:
            continue
        origin, dest = raw.split(">", 1)
        pair = (origin.strip().upper(), dest.strip().upper())
        if pair not in lanes:
            lanes.append(pair)
    return lanes


def _ensure_location(repo, key, country=None) -> str:
    """A lane endpoint the subject named but the location list has not.

    An unknown code stays a candidate and saving is what creates it,
    as 'To Confirm' so the consistency check and rule R8 both pick it up.
    """
    return repo.add_location(
        key, country=country or "Unknown",
        notes="Added when registering a case from a mail subject.")


def _official_new_places(conn, repo, form, keys) -> list:
    """RG2a (F): the places the reader found in the official code lists
    (the form's official_new), created now that she registers - active,
    source intake, built from the official row read again from the files
    Settings names (the form is not trusted for it) - when the case still
    holds them. The keys created."""
    from .. import official_places
    try:
        offered = json.loads(form.get("official_new") or "{}")
    except ValueError:
        return []
    if not isinstance(offered, dict):
        return []
    wanted = {db.location_key(k) for k in keys if k}
    mode = (form.get("service_mode") or "").strip() or None
    made = []
    for key, spec in offered.items():
        if not isinstance(spec, dict) or db.location_key(key) not in wanted:
            continue
        if places.resolve_one(conn, key) is not None:
            continue
        code = str(spec.get("iata") or spec.get("unlocode") or "")
        found = official_places.place_for(conn, code, spec.get("country"), mode) if code else None
        if found is None or (found.get("iata") or None) != (spec.get("iata") or None) \
                or (found.get("unlocode") or None) != (spec.get("unlocode") or None):
            continue
        rules = db.place_rules(conn, found["text"], found["country"], found["type"])
        if rules["error"] or rules["found"] is not None or db.location_key(rules["text"]) != db.location_key(key):
            continue
        columns = {k: found[k] for k in ("iata", "unlocode", "is_airport", "is_seaport") if found.get(k) is not None}
        if found["type"] == "airport" and found.get("town"):
            city = db.find_master_location(conn, found["town"])
            if city is not None and city["type"] == "city" and city["country"] == found["country"]:
                columns["parent_id"] = city["id"]
        row = repo.add_master_location(rules["text"], found["country"], found["type"], name=found["name"],
                                       status="active", source="intake",
                                       note="Added at Register from the official list ({}).".format(found["official"]),
                                       **columns)
        made.append(row["key"])
    return made


def _write_register_read(conn, repo, form, enquiry_id) -> int:
    """RG2a (layer 1): one register_read row per field the reader filled on
    a registration from a mail, beside the value the case was saved with.
    Written here, at Register, and nowhere else. The rows written."""
    if (form.get("created_by") or "") != "outlook" or not db._has_table(conn, "register_read"):
        return 0
    try:
        read = json.loads(form.get("reader_read") or "{}")
    except ValueError:
        return 0
    if not isinstance(read, dict):
        return 0
    case = conn.execute("SELECT * FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    written = 0
    for name in intake.READ_FIELDS:
        entry = read.get(name)
        if not isinstance(entry, dict) or not str(entry.get("value") or "").strip():
            continue
        saved = case[name]
        if name == "requester_party_id" and saved is not None:
            party = conn.execute("SELECT key FROM party WHERE id = ?", (saved,)).fetchone()
            saved = party["key"] if party else saved
        where = entry.get("from") if entry.get("from") in intake.READ_FROM else "other"
        words = " ".join(str(entry.get("words") or "").upper().split())[:80] or None
        repo.insert("register_read", {
            "enquiry_id": enquiry_id, "field": name, "value_read": str(entry["value"])[:200],
            "read_from": where, "value_saved": None if saved in (None, "") else str(saved)[:200],
            "words": words if name in ("origin_loc", "dest_loc") else None})
        written += 1
    return written


# -------------------------------------------------------------- routes ----
def register_routes(app: Flask) -> None:

    @app.route("/favicon.ico")
    def favicon_ico():
        """I1: the request a browser makes at the root by itself."""
        return app.send_static_file("favicon.ico")

    @app.route("/")
    def home():
        return redirect(url_for("enquiries"))

    @app.route("/site")
    def team_site_root():
        return redirect("/site/")

    @app.route("/site/")
    @app.route("/site/<path:name>")
    def team_site(name="index.html"):
        """The offline copy of the team site (site/ in the repository): its
        files as they are, and its one data file, the snapshot the demo
        command wrote into the data folder. Read only; no page of OppList
        depends on it."""
        from flask import send_from_directory
        from .. import site_snapshot
        if name == "data/" + site_snapshot.SNAPSHOT_NAME:
            folder, name = paths.DATA_DIR / site_snapshot.FOLDER, site_snapshot.SNAPSHOT_NAME
        else:
            folder = paths.PROJECT_DIR / "site"
        if not folder.is_dir():
            abort(404)
        return send_from_directory(str(folder), name, max_age=0)

    # ---------------------------------------------------- 4.1 the list --
    @app.route("/enquiries")
    def enquiries():
        """Enquiries v2 (2026-09-25): the rows of the chosen view are rendered
        once, with what the page needs in data attributes (the Last event is
        our own last action, tracking.our_last_actions); grouping,
        sorting, the filter chips, the live search box and the board all run
        in the browser (app.js, enquiries v2). Every parameter the list took
        before still works: view on the server, q as the deep search on the
        server (vendors, notes, people), status / requester / latam /
        direction / mode / month / scope / owner / dropped / sort / dir read
        by the page's script on load, group and board as well."""
        conn = g.conn
        view = request.args.get("view", "open")
        if view not in VIEW_WHERE and view != "silent":
            view = "open"
        from .. import silent
        silent_ids = silent.silent_ids(conn)                    # L1: the same rule as Tracking's R11
        if view == "silent":
            where = ["e.id IN ({})".format(",".join(str(int(i)) for i in silent_ids) or "NULL")]
        else:
            where = [VIEW_WHERE[view]] if VIEW_WHERE[view] else []
        args = []
        query = (request.args.get("q") or "").strip()
        select_args = []
        activity = tracking.activity_on_sql("e")
        if query:
            core_sql, core_args = enquiry_search.core_clause(query)
            select = "SELECT e.*, {} AS q_core, {} AS last_activity_on FROM v_enquiry e".format(core_sql, activity)
            select_args = core_args
            found_sql, found_args = enquiry_search.clause(query)
            where.append(found_sql)
            args.extend(found_args)
        else:
            select = "SELECT e.*, 1 AS q_core, {} AS last_activity_on FROM v_enquiry e".format(activity)
        sql = select + (" WHERE " + " AND ".join(where) if where else "") + \
            " ORDER BY e.received_on DESC, CAST(substr(e.opp_no, 5) AS INTEGER) DESC"
        rows = [dict(r) for r in conn.execute(sql, select_args + args).fetchall()]
        if query:
            for row in rows:
                row["q_hit"] = "" if row["q_core"] else enquiry_search.explain(
                    conn, row["id"], row["requester_party_id"], query)

        # our own last action, in one pass (2026-09-25: the list no longer
        # asks Tracking what to do next - that lives on Tracking)
        last = tracking.our_last_actions(conn)
        group_sizes = {r["request_group"]: r["n"] for r in conn.execute(
            "SELECT request_group, COUNT(*) AS n FROM v_enquiry GROUP BY request_group HAVING n > 1")}
        # v33: every live route of a row, for the '+N' chip and filters
        # that match any route (LATAM, mode, direction, the search box)
        by_case = {}
        if db._has_table(conn, "enquiry_route"):
            for route in conn.execute("SELECT enquiry_id, seq, service_mode, latam, direction, origin_loc, "
                                      "dest_loc, origin_country, dest_country, dest_alt FROM v_route "
                                      "ORDER BY enquiry_id, seq"):
                by_case.setdefault(route["enquiry_id"], []).append(route)
        for row in rows:
            case_routes = by_case.get(row["id"], [])
            row["route_more"] = max(len(case_routes) - 1, 0)
            row["modes"] = "|" + "|".join(sorted({r["service_mode"] for r in case_routes if r["service_mode"]}
                                                 | ({row["service_mode"]} if row["service_mode"] else set()))) + "|"
            row["directions"] = "|" + "|".join(sorted({r["direction"] for r in case_routes if r["direction"]}
                                                      | ({row["direction"]} if row["direction"] else set()))) + "|"
            row["latam_any"] = "Y" if row["latam"] == "Y" or any(r["latam"] == "Y" for r in case_routes) else "N"
            row["route_places"] = " ".join(" ".join(filter(None, (r["origin_loc"], r["dest_loc"], r["origin_country"],
                                                                   r["dest_country"], r["dest_alt"])))
                                           for r in case_routes[1:])
            row["last"] = last.get(row["id"])
            row["lane_from"] = row["origin_loc"] or row["origin_country"] or "?"
            row["lane_to"] = row["dest_loc"] or row["dest_country"] or "?"
            row["mode_tone"] = MODE_TONES.get(row["service_mode"] or "", "grey")
            row["qty_text"] = qty_text(row)
            row["round"] = row["quote_rounds"] if (row["quote_rounds"] or 0) >= 2 else (
                (row["quote_rounds"] or 0) + 1 if row["in_requote"] else 0)

        # the five views, counted the way each tab lists them by default
        # (scope In, dropped hidden but on Closed) - the same numbers the
        # old tabs showed; SC1: Legacy lists its rows (shown as Legacy)
        counts = {}
        for key, clause in VIEW_WHERE.items():
            parts = [c for c in (clause, "scope = 'In'") if c]
            if key not in ("closed", "others", "legacy"):
                parts.append("status <> 'Dropped'")
            counts[key] = conn.execute("SELECT COUNT(*) FROM v_enquiry WHERE " + " AND ".join(parts)).fetchone()[0]
        counts["silent"] = len(silent_ids)
        countries = {r[0]: (r[1] or "") for r in conn.execute("SELECT id, country FROM party")}
        mfn = db.mfn_party_ids(conn)                            # FX4: the MFN label
        for row in rows:
            row["mfn"] = row["requester_party_id"] in mfn
            country = countries.get(row["requester_party_id"], "")
            row["requester_country"] = country if country and country != "Unknown" else "Country unknown"
        requesters = {}
        for row in rows:
            key = (row["requester_party_id"], row["requester_key"] or "?")
            requesters[key] = requesters.get(key, 0) + 1
        return render_template(
            "enquiries.html", rows=rows, view=view, query=query, counts=counts, silent_days=silent.days_setting(conn),
            group_sizes=group_sizes, filters=request.args,
            columns=column_layout(conn), cut=cut,
            requesters=sorted(requesters.items(), key=lambda kv: kv[0][1].casefold()),
            months=sorted({r["month"] for r in rows if r["month"]}, reverse=True),
            on_file=conn.execute("SELECT COUNT(*) FROM enquiry").fetchone()[0],
            **reference(conn), **settings_lists(conn))

    @app.route("/enquiries/columns", methods=["POST"])
    def enquiries_columns():
        """Remember the list's column order and which ones are shown."""
        conn = g.conn
        if request.form.get("action") == "reset":
            db.set_setting(conn, "list_columns", "")
            flash("Columns back to the default")
        else:
            order = [c for c in (request.form.get("order") or "").split(",")
                     if c in COLUMN_IDS]
            order += [c for c in COLUMN_IDS if c not in order]
            current = {c: v for c, _, _, v in column_layout(conn)}
            if "visible" in request.form or request.form.get("action") == "show":
                visible = [c for c in order if c in request.form.getlist("visible")]
            else:
                visible = [c for c in order if current[c]]
            db.set_setting(conn, "list_columns",
                           json.dumps({"order": order, "visible": visible}))
        back = request.form.get("back") or url_for("enquiries")
        return redirect(back if back.startswith("/") else url_for("enquiries"))

    # ----------------------------------------------------- 13e search --
    @app.route("/search")
    def search():
        """The top bar's search box. In the two case scopes an OPP number
        that exists opens the case ('487', '0487', 'OPP-1487', 'opp 487',
        'OPP0487'); anything else goes to the list with q."""
        query = (request.args.get("q") or "").strip()
        scope = request.args.get("scope") or "all"
        if scope not in ("all", "open", "parties"):
            scope = "all"
        if not query:
            return redirect(url_for("enquiries"))
        if scope == "parties":
            return redirect(url_for("parties", q=query))
        found = OPP_JUMP.match(query)
        if found:
            row = g.conn.execute("SELECT id FROM enquiry WHERE opp_no = ?",
                                 ("OPP-{:04d}".format(int(found.group(1))),)).fetchone()
            if row is not None:
                return redirect(url_for("enquiry", enquiry_id=row["id"]))
        return redirect(url_for("enquiries", view="all" if scope == "all" else "open", q=query))

    # ----------------------------------------------------- 4.3 register --
    @app.route("/enquiries/new", methods=["GET", "POST"])
    def enquiry_new():
        conn, repo = g.conn, g.repo
        group_id = request.args.get("group", type=int)

        if request.method == "GET":
            # AL1: a new case starts with owner empty (the team site's Assign fills it)
            prefill = {"received_on": _today()}
            if group_id:
                source = conn.execute(
                    "SELECT * FROM v_enquiry WHERE id = ?", (group_id,)).fetchone()
                if source is None:
                    abort(404)
                prefill = dict(source)
                prefill["id"] = None
            return render_template(
                "enquiry_form.html", values=prefill, errors={}, matches=[],
                form_token=_new_form_token(),
                group_id=group_id, draft=None, lane_options=[], vendors=[],
                tags=_split_tags(prefill.get("special_handling")),
                **_register_view(conn, _draft_rows([], prefill, source="manual"), [], prefill, "", []),
                **reference(conn), **settings_lists(conn), **_register_lists(conn))

        token = (request.form.get("form_token") or "").strip()
        with _REGISTER_LOCK:
            saved = _SAVED_FORMS.get(token) if token else None
            if saved is not None and conn.execute("SELECT 1 FROM enquiry WHERE id = ?",
                                                  (saved[0],)).fetchone():
                # the same form again (a second click, a resend): nothing new
                _log_register("form sent again after {} was saved - nothing created".format(saved[1]),
                              request.form)
                warn("Already saved as {} at {}; nothing new was created.".format(
                    saved[1], saved[2].strftime("%H:%M")))
                return redirect(url_for("enquiry", enquiry_id=saved[0]))
            response = _register_post(conn, repo, group_id, token)
        return response

    def _register_post(conn, repo, group_id, token):
        """The save itself, under _REGISTER_LOCK."""
        values, errors = forms.read(request.form, _tags(request.form))
        typed = _as_typed(request.form, values)
        # 13g: our own domain never belongs to a party - not a new one either
        new_domain = (request.form.get("new_party_domain") or "").strip().lower()
        if (request.form.get("new_party_key") or "").strip() and new_domain:
            refusal = _new_party_domain_refusal(conn, new_domain)
            if refusal:
                errors["new_party_domain"] = refusal
        # RD1c: nothing typed or parsed here is written before Register
        # - a place added inline is pending like a picker's
        _resolve_inline(repo, request.form, values, errors, locations=False)
        # batch 23: route A's place pickers - checked now, written when the
        # case is created; origin_loc / dest_loc are the first places for
        # the checks (the other destinations ride in picked_a)
        picked_a = _parse_pickers(conn, request.form, "", errors)
        _pending_inline_places(conn, request.form, picked_a, values, errors)
        for side in ("origin", "dest"):
            values[side + "_loc"] = picked_a[side]["keys"][0] if picked_a[side]["keys"] else None
        # a refused save shows the pickers as she left them (with an 'Add
        # location...' key typed the old way), and route A's Service
        for side, name in (("origin", "origin_loc"), ("dest", "dest_loc"), ("pod", "pod")):
            typed[name] = places.join(picked_a[side]["keys"])
        typed["service_scope"] = (request.form.get("service_scope") or "").strip()
        # the cargo lines: with any on the form, the three
        # quantity fields are their totals, whatever the (read-only) inputs say
        all_rows, cargo_errors = _posted_cargo(request.form, keep_blank=True)
        cargo_rows = [r for r in all_rows if not r["_blank"]]
        cargo_stated = _posted_stated(request.form)
        errors.update(cargo_errors)
        cargo_state = cargo.summarise(cargo_rows, values.get("service_mode"))
        for name, value in cargo.enquiry_fields(cargo_state).items():
            values[name] = value
            typed[name] = value
        # 15c: a size is never saved without its unit - the line asks cm or in
        for row in cargo_rows:
            dims = [row.get(k) for k in ("dim_l", "dim_w", "dim_h")]
            if all(v is not None for v in dims) and not row.get("dims_unit"):
                errors["cargo line {} size".format(row["_n"])] = (
                    "the size {} x {} x {} has no unit: choose Use in or Use cm on the line".format(
                        *(_num_text(v) for v in dims)))
        # 15c: the routes after A, and FCL's containers from the lines
        reg_routes = _reg_routes(request.form)
        if cargo_rows and "container_type" not in request.form and "container_qty" not in request.form:
            fcl = routes.totals(conn, None, lines=[dict(r, counted=r.get("counted", 1)) for r in cargo_rows],
                                live_routes=[], case={"service_mode": values.get("service_mode")})
            values["container_type"], values["container_qty"] = fcl.get("container_type"), fcl.get("container_qty")
        # 13c: no counted line - the typed gross weight and volume (any
        # unit) are the case's; on Air the weight is the chargeable one
        if not cargo_state["totals"]["count"] and ("gross_kg" in request.form or "volume_cbm" in request.form):
            gross, volume = values.get("gross_kg"), values.get("volume_cbm")
            if "air_weight_kg" not in request.form:
                values["air_weight_kg"] = routes.chargeable(gross, volume) if values.get("service_mode") in forms.AIR_MODES else gross
                values["air_weight_kg"] = round(values["air_weight_kg"], 3) if values["air_weight_kg"] is not None else None
            if "lcl_cbm" not in request.form:
                values["lcl_cbm"] = volume
            for name in ("gross_kg", "volume_cbm", "gross_unit", "volume_unit"):
                typed[name] = request.form.get(name) if name in request.form else typed.get(name)
        # 'BKK / CNX - ATL' is one request over two lanes. The first
        # ticked lane fills the form so the usual validation applies to it.
        lanes = _posted_lanes(request.form)
        if lanes:
            # A lane side the draft could not settle (several places share
            # that spelling) is empty here; the dropdown's pick fills it.
            lanes = [(o or values.get("origin_loc") or "", d or values.get("dest_loc") or "")
                     for o, d in lanes]
            values["origin_loc"] = lanes[0][0]
            values["dest_loc"] = lanes[0][1]
            for side, key in (("origin", lanes[0][0]), ("dest", lanes[0][1])):
                picked_a[side]["keys"] = [key] if key else []
        # S1: route A's Service decides its places (it is the route's, not the case's)
        forms.validate(dict(values, service_scope=typed.get("service_scope") or None), errors)
        # 13c: the register form has no Air weight / LCL volume boxes: the
        # rule is met by a counted line or the typed boxes, and it is named there
        if "air_weight_kg" in errors and "air_weight_kg" not in request.form:
            key, why = _missing_quantity(cargo_state, "weight")
            errors.setdefault(key, why)
            errors.pop("air_weight_kg")
        if "lcl_cbm" in errors and "lcl_cbm" not in request.form:
            key, why = _missing_quantity(cargo_state, "volume")
            errors.setdefault(key, why)
            errors.pop("lcl_cbm")
        # 15c: FCL's containers and a free-text mode's description are the lines' too
        for field in ("container_type", "container_qty"):
            if field in errors and field not in request.form and errors[field].startswith("required"):
                errors.pop(field)
                counted = [r for r in cargo_rows if int(r.get("counted", 1) or 0)]
                where = "cargo line {}".format(counted[0]["_n"]) if counted else "cargo"
                errors.setdefault(where, "FCL needs its containers: give the line how many and which type "
                                         "(2 x 40HQ)" if counted else "FCL needs its containers: add a cargo line "
                                         "with how many and which type (2 x 40HQ)")
        if "qty_raw" in errors and "qty_raw" not in request.form:
            errors.pop("qty_raw")
            if not cargo_rows:
                errors["cargo"] = "{} needs a cargo line: describe the pieces or the size".format(
                    values.get("service_mode"))
        for n, r in enumerate(reg_routes, start=2):
            for field, allowed in (("service_mode", forms.SERVICE_MODES), ("incoterm", forms.INCOTERMS)):
                if r[field] and r[field] not in allowed:
                    errors["route {} {}".format(r["letter"], field)] = "not one of the list"
        # batch 23: the later routes' pickers; every destination place is checked
        for r in reg_routes:
            r["_picked"] = _parse_pickers(conn, request.form, "route_{}_".format(r["key"]), errors)
        for prefix, picked in [("", picked_a)] + [("route_{}_".format(r["key"]), r["_picked"]) for r in reg_routes]:
            ports, added = picked["pod"], {e["key"] for e in picked["pod"]["new"]}
            stray = [k for k in ports["keys"] if len(ports["keys"]) > 1 and db.location_key(k) not in added
                     and places.resolve_one(conn, k, retired=True) is None]
            if stray:
                errors[prefix + "pod"] = "{} is not in the location list: pick it from the list or add it".format(
                    ", ".join(stray))
        _two_letter_errors(conn, values, lanes + [("", d) for d in picked_a["dest"]["keys"][1:]] +
                           [(r["_picked"]["origin"]["keys"][0] if r["_picked"]["origin"]["keys"] else "", d)
                            for r in reg_routes for d in (r["_picked"]["dest"]["keys"] or [""])], errors)
        # RD1b: what the save would create as a place obeys the master's naming rules
        oc, dc = values.get("origin_country"), values.get("dest_country")
        _place_rule_errors(conn, [("origin_loc", values.get("origin_loc"), oc), ("dest_loc", values.get("dest_loc"), dc)]
                           + [("origin_loc", o, oc) for o, _ in lanes] + [("dest_loc", d, dc) for _, d in lanes]
                           + [("origin_loc", k, oc) for k in picked_a["origin"]["keys"]]
                           + [("dest_loc", k, dc) for k in picked_a["dest"]["keys"]]
                           + [("route_{}_{}_loc".format(r["key"], side), k, r.get(side + "_country") or None)
                              for r in reg_routes for side in ("origin", "dest")
                              for k in r["_picked"][side]["keys"]], errors)
        # 15c: Register as Quoted, on a date (the mail's by default); never before it came in
        status_at = (request.form.get("status_at") or "").strip()
        if (values.get("status") or "New") != "New" and status_at:
            try:
                status_at = dt.date.fromisoformat(status_at).isoformat()
                if values.get("received_on") and status_at < values["received_on"]:
                    errors["status_at"] = "the quote date is before the date it came in"
            except ValueError:
                errors["status_at"] = "not a date"

        decision = request.form.get("decision") or ""
        target = request.form.get("decision_target", type=int)
        matches = []
        if not errors:
            if decision == "duplicate" and target:
                conn.rollback()                    # RD1c: nothing of this form is kept
                _log_register("opened the existing case instead", request.form)
                return redirect(url_for("enquiry", enquiry_id=target))
            if not decision:
                # the mail's own ids reach the check too (13g): a case
                # registered from this same mail is named as such
                probe = dict(values)
                for name in ("thread_key", "conversation_id", "entry_id"):
                    probe[name] = probe.get(name) or (request.form.get(name) or "").strip() or None
                matches = _dedupe_matches(conn, probe, declined=_declined(request.form))

        if errors or matches:
            # RD1c: a refused save or the duplicate panel writes nothing - a
            # party added inline goes too; the form keeps them for Register
            conn.rollback()
            if errors:
                _log_register("rejected: {}".format(
                    "; ".join("{} {}".format(k, v) for k, v in
                              sorted(errors.items()))), request.form)
            else:
                _log_register("possible duplicate panel shown ({} match(es): "
                              "{}) - nothing saved".format(
                                  len(matches),
                                  ", ".join(m.opp_no for m in matches)),
                              request.form)
            mail_context = _mail_context(request.form)
            link = {"case": None, "how": "", "seen": []}
            if mail_context:
                # the thread's case runs again on a refused page (13g), by the DD1 rule
                link = _thread_link(
                    conn, request.form.get("source_subject") or "",
                    values.get("conversation_id") or request.form.get("conversation_id") or "",
                    request.form.get("thread_key") or "",
                    day=(request.form.get("received_on") or "").strip(), declined=_declined(request.form))
            already = _already(conn, link, matches, (request.form.get("received_on") or "").strip())
            return render_template(
                "enquiry_form.html", values=typed, errors=errors,
                matches=matches, group_id=group_id, tags=_tags(request.form),
                form_token=token or _new_form_token(), mail_context=mail_context,
                already=already, declined_case=",".join(_declined(request.form)),
                draft=None, inline=_inline_values(request.form),
                lane_options=[{"origin": o, "dest": d, "primary": True}
                              for o, d in lanes],
                vendors=_posted_vendors(request.form),
                vendor_note=(request.form.get("vendor_note") or "").strip(),
                reg_header=_reg_header(request.form), attachment_checks=_posted_checks(request.form),
                **_register_view(conn, all_rows or _draft_rows([], {}), cargo_stated,
                                 dict(typed, cargo_decided=request.form.get("cargo_decided") or "",
                                      service_scope=request.form.get("service_scope") or "",
                                      pod=request.form.get("pod") or ""),
                                 request.form.get("mail_cargo_text") or "", reg_routes,
                                 status_at=request.form.get("status_at") or ""),
                **reference(conn), **settings_lists(conn), **_register_lists(conn)), (400 if errors else 200)

        # v33: 'Same request, add as lane' is gone - one mail is one OPP with
        # its routes; a clash is still offered as a duplicate
        values["created_by"] = request.form.get("created_by") or "manual"
        if values["created_by"] not in ("manual", "outlook", "migration"):
            values["created_by"] = "manual"
        for name in ("thread_key", "conversation_id", "entry_id"):
            value = (request.form.get(name) or "").strip()
            if value:
                values[name] = value
        # AL1: a new case starts with owner empty (AL2 fills it from the assign
        # list); a subject carrying another case's number gives the new case
        # no thread of its own (item 7). RG2b (1): from a mail, the Owner she
        # left on the form (the colleague whose mail it is, or her choice),
        # when it is one of Settings' owners
        owner = (request.form.get("owner") or "").strip()
        values.pop("owner", None)
        if values["created_by"] == "outlook" and owner and owner in assign_list.owners(conn):
            values["owner"] = owner
        from .. import case_numbers
        own_thread = not case_numbers.valid_cases(conn, values.get("source_subject") or "")

        # v33: one mail is one OPP. The first ticked lane is
        # route A (the case's own lane); every other lane is another route
        # of the same OPP, not another case.
        all_lanes = lanes or [(values.get("origin_loc"), values.get("dest_loc"))]
        case_values = dict(values)
        # RG2a (F): a place the reader found in the official lists is created
        # now, active, instead of To confirm
        _official_new_places(conn, repo, request.form,
                             [k for side in ("origin", "dest") for k in picked_a[side]["keys"]]
                             + [k for lane in all_lanes for k in lane if k]
                             + [k for r in reg_routes for side in ("origin", "dest") for k in r["_picked"][side]["keys"]])
        # batch 23: route A's pickers written - places added in their small
        # forms (To confirm), ports marked, a key not on file added To
        # confirm as a lane from a mail always was; the first destination
        # is the case's, the others and the ports go on route A below
        got_a = _apply_pickers(conn, repo, picked_a, values, create_unknown=True)
        case_values["origin_loc"] = got_a.get("origin_loc")
        dest_ids = got_a.get("dest_places") or []
        case_values["dest_loc"] = conn.execute("SELECT key FROM master_location WHERE id = ?", (dest_ids[0],)
                                               ).fetchone()["key"] if dest_ids else got_a.get("dest_loc")
        if group_id:
            case_values["group_id"] = group_id
        # A mail of ours that gave the rate: Quoted on the day it went
        # out, not on the day the enquiry came in (intake.mail_direction).
        if len(status_at) != 10 or status_at[4] != "-" or status_at[7] != "-" or \
                (case_values.get("status") or "New") == "New":
            status_at = case_values["received_on"]
        enquiry_id = repo.create_enquiry(case_values, status_at=status_at, own_thread=own_thread)
        created = [enquiry_id]
        route_ids = {}
        keyed = {}                      # 15c: route key on the page -> route id
        first_route = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? ORDER BY seq LIMIT 1",
                                   (enquiry_id,)).fetchone()
        if first_route is not None:
            route_ids[(case_values["dest_loc"] or "").upper()] = first_route["id"]
            keyed["a"] = first_route["id"]
            extra_a = {"service_scope": (request.form.get("service_scope") or "").strip() or None}
            if len(dest_ids) > 1:
                extra_a["dest_places"] = dest_ids
            if got_a.get("pod_places"):
                extra_a["pod_places"] = got_a["pod_places"]
            elif got_a.get("pod"):
                extra_a["pod"] = got_a["pod"]
            if any(extra_a.values()):
                repo.edit_route(first_route["id"], extra_a)
        for origin, dest in all_lanes[1:]:
            lane = {k: values.get(k) for k in ("origin_country", "origin_alt", "dest_country", "dest_alt",
                                               "service_mode", "incoterm")}
            lane["origin_loc"] = _ensure_location(repo, origin, values.get("origin_country")) if origin else None
            lane["dest_loc"] = _ensure_location(repo, dest, values.get("dest_country")) if dest else None
            for side in ("origin", "dest"):
                known = conn.execute("SELECT country FROM location WHERE key = ?",
                                     (lane[side + "_loc"],)).fetchone() if lane[side + "_loc"] else None
                if known is not None and known["country"] and known["country"] != "Unknown":
                    lane[side + "_country"] = known["country"]
            if lane["dest_loc"] != case_values["dest_loc"]:
                lane["dest_alt"] = None
            route_ids[(lane["dest_loc"] or "").upper()] = repo.add_route(
                enquiry_id, lane, source="mail" if values.get("created_by") == "outlook" else "manual")

        # 15c: the routes after A, as the page has them; a blank mode or
        # incoterm is route A's
        for r in reg_routes:
            lane = {k: r[k] or None for k in ("origin_country", "origin_alt", "dest_country", "dest_alt",
                                              "service_scope")}
            lane["service_mode"] = r["service_mode"] or values.get("service_mode")
            lane["incoterm"] = r["incoterm"] or values.get("incoterm")
            # batch 23: its pickers, written as route A's are
            lane.update(_apply_pickers(conn, repo, r["_picked"], lane, create_unknown=True))
            keyed[r["key"]] = repo.add_route(
                enquiry_id, lane, source="mail" if values.get("created_by") == "outlook" else "manual")

        # the totals the mail states (v34), then the cargo lines: a line
        # delivering to one route's destination is tagged with it; the rest
        # apply to every route
        stated_ids = [repo.add_cargo_stated(enquiry_id, s) for s in cargo_stated]
        # 15b: cargo is lines only. The typed weight, volume, containers and
        # commodity of a registration without lines become its first line;
        # lines that name no commodity give the first one the typed one
        from .. import cargo_lines_migration
        if not cargo_rows:
            typed_line = cargo_lines_migration.line_from_typed(
                case_values, source="mail" if values.get("created_by") == "outlook" else "manual")
            cargo_rows = [typed_line] if typed_line else []
        elif (case_values.get("commodity") or "").strip() and not any(
                (r.get("commodity") or "").strip() for r in cargo_rows):
            cargo_rows[0] = dict(cargo_rows[0], commodity=case_values["commodity"])
        for row in cargo_rows:
            deliver = (row.get("deliver_to") or "").upper()
            tagged = [rid for key, rid in route_ids.items() if key and key in deliver]
            ticked = row.get("_routes")
            row = {k: v for k, v in row.items() if not k.startswith("_")}
            ref = row.pop("stated_ref", None)
            row["stated_id"] = stated_ids[ref] if ref is not None and ref < len(stated_ids) else None
            if len(route_ids) > 1 and len(tagged) == 1:
                row["route_ids"] = str(tagged[0])
            # 15c: the routes ticked on the line; all of them (or none) is every route
            chosen = [keyed[k] for k in (ticked or []) if k in keyed]
            if len(keyed) > 1 and chosen and len(chosen) < len(keyed):
                row["route_ids"] = routes.format_ids(chosen)
            repo.add_cargo_line(enquiry_id, row)
        decided = (request.form.get("cargo_decided") or "").strip()
        if decided:
            repo.update("enquiry", enquiry_id, {"cargo_decided": decided})
        # RG2a (layer 1): what the reader filled, beside what was saved
        _write_register_read(conn, repo, request.form, enquiry_id)

        new_party = (request.form.get("new_party_key") or "").strip()
        # The domain the draft read the requester off (a quoted original, a
        # name match, or the one a New party was pre-filled from): remembered
        # on the party the case was saved with.
        # D1: only for the party it was read for - the New party (the Domain
        # box as she left it, emptied means none) or the party named like it
        learn, learn_for = "", (request.form.get("learn_for") or "").strip()
        if new_party and "new_party_domain" in request.form:
            learn = (request.form.get("new_party_domain") or "").strip().lower()
        elif not new_party and learn_for and learn_for != "new":
            if str(values.get("requester_party_id") or "") in learn_for.split(","):
                learn = (request.form.get("learn_domain") or "").strip().lower()
        elif not learn_for and "new_party_domain" not in request.form:
            # a form rendered before D1 carries no learn_for: as it was then
            learn = (request.form.get("learn_domain") or request.form.get("new_party_domain") or "").strip().lower()
        if learn and values.get("requester_party_id"):
            learned = matching.remember_domain(repo, values["requester_party_id"], learn)
            if learned:
                flash("Remembered {} for {}.".format(
                    learned, conn.execute("SELECT key FROM party WHERE id = ?",
                                          (values["requester_party_id"],)).fetchone()["key"]))
        elif new_party and request.form.get("sender_address") and "new_party_domain" not in request.form:
            sender = matching.SenderInfo(
                address=request.form.get("sender_address") or "",
                internal=request.form.get("sender_internal") == "1",
                recipients=[a for a in
                            (request.form.get("recipient_addresses") or "").split(",")
                            if a.strip()])
            learned = matching.learn_domain(repo, values["requester_party_id"],
                                            sender)
            if learned:
                flash("Remembered {} for {}.".format(learned, new_party))

        asked = _log_vendor_requests(repo, request.form, created,
                                     values.get("received_on"))
        numbers = [conn.execute("SELECT opp_no FROM enquiry WHERE id = ?",
                                (i,)).fetchone()["opp_no"] for i in created]
        if asked:
            flash("Logged what we asked: {}.".format(", ".join(asked)))
        _log_register("created {}".format(", ".join(numbers)), request.form)
        # M2: the registered mail's Message-ID, kept on the case
        message_id = (request.form.get("message_id") or "").strip()
        if message_id and values.get("created_by") == "outlook" and values.get("entry_id"):
            from .. import case_mail
            case_mail.remember_registered(repo, created[0], message_id, _today())
        # commit before the lock is let go, so a second send finds the case
        conn.commit()
        _remember_form(token, created[0], numbers[0])
        # The case page says 'Saved OPP-xxxx' in green and clears the browser
        # draft, so that a save is unmistakable.
        return redirect(url_for("enquiry", enquiry_id=created[0],
                                saved=",".join(numbers)))

    # ------------------------------------------- 4.3 register from Outlook --
    @app.route("/enquiries/from-outlook", methods=["GET", "POST"])
    def enquiry_from_outlook():
        conn = g.conn
        try:
            mail = outlook_bridge.read_selection()
        except outlook_bridge.OutlookError as exc:
            logs.get("web").info("outlook selection refused: %s", exc.detail)
            warn(exc.message)
            return redirect(url_for("enquiries"))

        return _render_outlook_draft(conn, mail)

    def _render_outlook_draft(conn, mail):
        """The register page from one mail: the rule-based draft and the
        attachments (14)."""
        draft = intake.build(conn, mail)
        _read_mail_attachments(conn, draft, mail)     # batch 14
        values = dict(draft.values)
        # AL1: a new case starts with owner empty; RG2b (1): unless the mail is
        # a JJC colleague's (intake._owner_from_colleague)
        if values.get("dest_loc") and draft.dest_more:
            # batch 23: 'San Antonio or Valparaiso' - route A holds both places
            values["dest_loc"] = places.join([values["dest_loc"]] + list(draft.dest_more))
        link = _thread_of_mail(conn, mail)
        from .. import case_numbers
        if case_numbers.valid_cases(conn, mail.subject or ""):
            # AL1 item 7: another case's number - registering anyway keeps the
            # mail but neither its thread key nor its conversation
            values["thread_key"], values["conversation_id"] = "", ""
        inline = {}
        if draft.new_party:
            inline = {"new_party_key": draft.new_party["key"],
                      "new_party_type": draft.new_party["type"],
                      "new_party_country": draft.new_party.get("country", ""),
                      "new_party_domain": draft.new_party.get("domain", ""),
                      "new_party_domain_shown": "1" if draft.new_party.get("domain") else "",
                      "new_party_city": draft.new_party.get("city", ""),
                      "new_party_mfn_no": draft.new_party.get("mfn_no", "")}
        return render_template(
            "enquiry_form.html", values=values, errors={}, matches=[],
            form_token=_new_form_token(), reader_read=intake.reader_values(conn, draft),
            group_id=None, draft=draft, tags=draft.special_tags,
            lane_options=draft.lanes, vendors=draft.vendors, inline=inline,
            new_party=draft.new_party,
            vendor_note=draft.vendor_note,
            already=_already(conn, link, [], case_numbers.mail_day(mail) or ""), declined_case="",
            reg_header=_reg_header(mail=mail), attachment_checks=draft.attachment_checks,
            **_register_view(conn, _draft_rows(draft.cargo_lines, values,
                                               commodity_from=draft.sources.get("commodity") or "from the mail",
                                               extra=draft.line_extra),
                             draft.cargo_stated, values,
                             "\n".join(p for p in (_mail_cargo_text(mail), _attachment_text(draft)) if p),
                             *_draft_routes(conn, draft, values),
                             status_at=values.get("status_at") or (mail.received_on or "")),
            **reference(conn), **settings_lists(conn), **_register_lists(conn))

    @app.route("/enquiries/new/cargo/preview", methods=["POST"])
    def enquiry_new_cargo_preview():
        """15c: the register page's totals, checks and rail words as the
        lines stand while they are typed; nothing is written."""
        conn = g.conn
        values, _errors = forms.read(request.form, [])
        values.update(cargo_decided=request.form.get("cargo_decided") or "",
                      qty_raw=request.form.get("qty_raw") or values.get("qty_raw"),
                      service_scope=request.form.get("service_scope") or "")
        rows, _bad = _posted_cargo(request.form, keep_blank=True)
        card = _register_cargo(conn, rows, _posted_stated(request.form), values,
                               request.form.get("mail_cargo_text") or "",
                               _route_list(values, _reg_routes(request.form)))
        html = render_template("_cargo_live.html", reg=True, **card)
        asks = {str(r["_idx"]): render_template("_reg_asks.html", checks=card["line_asks"].get(r["_idx"], []))
                for r in rows}
        return jsonify(ok=True, html=html, asks=asks, count=card["cargo_asks"], rail=card["cargo_rail"],
                       blocking=card["unit_missing"])

    @app.route("/api/outlook/selection", methods=["GET", "POST"])
    def api_outlook_selection():
        try:
            mail = outlook_bridge.read_selection()
        except outlook_bridge.OutlookError as exc:
            return json.dumps({"error": exc.message}), 409, \
                {"Content-Type": "application/json"}
        draft = intake.build(g.conn, mail)
        return json.dumps({
            "values": draft.values,
            "sources": draft.sources,
            "lanes": draft.lanes,
            "special_tags": draft.special_tags,
            "references": draft.references,
            "notes": draft.notes,
            "parties": [{"id": m.party_id, "key": m.key, "how": m.how,
                         "detail": m.detail} for m in draft.party_candidates],
            "new_party": draft.new_party,
            "quoted": [{"name": q.name, "address": q.address, "sent_at": q.sent_at,
                        "subject": q.subject} for q in draft.quoted],
            "mail": {"subject": mail.subject, "sender": mail.sender_address,
                     "internal": mail.sender_internal,
                     "to": mail.to_addresses, "cc": mail.cc_addresses},
        }), 200, {"Content-Type": "application/json"}

    @app.route("/enquiries/<int:enquiry_id>/acknowledge", methods=["POST"])
    def enquiry_acknowledge(enquiry_id):
        """FX5: Acknowledge, blank (_open_blank_ack). Only on a New case
        registered from a mail and not acknowledged yet; nothing is written."""
        conn = g.conn
        row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if row is None:
            abort(404)
        if not _can_acknowledge(conn, row):
            warn("Acknowledge is for a New case registered from a mail and not acknowledged yet.")
        else:
            _open_blank_ack(conn, row)
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    @app.route("/enquiries/<int:enquiry_id>/rates/<int:rate_id>", methods=["POST"])
    def enquiry_rates_strike(enquiry_id, rate_id):
        """Q2c: Strike out a version of a vendor's rates typed by mistake, or
        Put it back (vendor_rate.voided, through the repository)."""
        from .. import site_rates
        if g.conn.execute("SELECT 1 FROM vendor_rate WHERE id = ? AND enquiry_id = ?",
                          (rate_id, enquiry_id)).fetchone() is None:
            abort(404)
        action = request.form.get("action") or ""
        if action not in ("void", "unvoid"):
            abort(400)
        say(site_rates.strike(g.repo, rate_id, voided=action == "void"))
        return redirect(url_for("enquiry", enquiry_id=enquiry_id) + "#vendors")

    @app.route("/enquiries/<int:enquiry_id>/read-quote/<int:request_id>", methods=["POST"])
    def enquiry_read_quote(enquiry_id, request_id):
        """Q2d: Read quote - the picked mail (or a pasted message) read with
        the Anthropic API; the drafts go to the team site in the sync run
        started right after. Only on the live app (8765), behind the gate."""
        from .. import case_mail, read_quote
        conn = g.conn
        case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        req = read_quote._request(conn, request_id)
        if case is None or req is None or req["enquiry_id"] != enquiry_id:
            abort(404)
        back = url_for("enquiry", enquiry_id=enquiry_id, read=request_id)
        state = read_quote.gate(conn, case, req, app.config["DB_PATH"])
        if not state["enabled"]:
            warn("Read quote: {}.".format(state["why"].rstrip(".") or "not available"))
            return redirect(back + "#read-quote")
        search = None
        dropped = request.files.get("mail_file")
        try:
            if dropped is not None and (dropped.filename or "").strip():
                # RQ2: a mail file dropped into the panel, read as one sent from the team site
                from .. import site_read
                source = site_read.mail_source(_keep_dropped(dropped))
            elif (request.form.get("paste") or "").strip():
                source = read_quote.from_paste(request.form.get("paste"), request.form.get("paste_day"), _today())
            else:
                search, _info = case_mail.open_search()
                wanted = (request.form.get("mail") or "").strip()
                mail = next((m["mail"] for m in read_quote.panel_mails(conn, search, req) if m["id"] == wanted), None)
                if mail is None:
                    raise read_quote.ReadQuoteError("Pick a mail, or paste a message instead.")
                source = read_quote.from_mail(mail, search)
            reading = read_quote.read(conn, request_id, source, today=_today(), db_path=app.config["DB_PATH"],
                                      repo=g.repo)
        except read_quote.ReadQuoteError as exc:
            read_quote.note_error(conn, str(exc))
            warn("Read quote: {}".format(exc))
            return redirect(back + "#read-quote")
        finally:
            if search is not None:
                search.__exit__(None, None, None)
        if reading.status == "failed":
            warn("Read quote: {}".format(reading.error))
        elif reading.status == "no_prices":
            say("Read quote: no prices in it{}.".format(" ({})".format(reading.why_not) if reading.why_not else ""),
                done=False)
        else:
            say("Read quote: {} draft{} for the team site (USD {:.2f}).".format(
                len(reading.drafts), "" if len(reading.drafts) == 1 else "s", reading.cost), done=True)
            try:
                app.firebase_syncer.schedule(delay=1)       # the drafts go at once
            except Exception:
                logs.get("web").exception("could not start the team site push after a reading")
        return redirect(url_for("enquiry", enquiry_id=enquiry_id, read=request_id, qr=reading.id) + "#read-quote")

    @app.route("/enquiries/<int:enquiry_id>/open-in-outlook", methods=["POST"])
    def enquiry_open_in_outlook(enquiry_id):
        conn = g.conn
        row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if row is None:
            abort(404)
        # the Mail card's button names the mail to open (2026-09-25); the
        # header's opens the registered one, else the thread's newest
        entry_id = (request.form.get("entry_id") or "").strip()
        where = _where(conn, row, entry_id) if entry_id else _title_where(conn, row)
        if where is None:
            warn("No mail of this case to open.")
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        try:
            outlook_bridge.open_item(**where)
        except outlook_bridge.OutlookError as exc:
            warn(exc.message)
        else:
            flash("Opened in Outlook.")
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    # --------------------------------------------------- 4.2 case page --
    @app.route("/enquiries/<int:enquiry_id>", methods=["GET", "POST"])
    def enquiry(enquiry_id):
        conn, repo = g.conn, g.repo
        row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?",
                           (enquiry_id,)).fetchone()
        if row is None:
            abort(404)

        errors = {}
        values = dict(row)
        route_values, route_errors = None, {}
        posting = request.method == "POST"
        # 24b-fix: a refused route save comes back here by redirect - its
        # form and errors stashed by enquiry_routes, shown once (?refused=1)
        route_stash = app.extensions.setdefault("route_stash", {}).pop(enquiry_id, None) if not posting else None
        if route_stash is not None and request.args.get("refused"):
            route_values, route_errors = route_stash["form"], dict(route_stash["errors"])
            errors.update(route_errors)
        if posting and request.form.get("form") == "route":
            # a refused route save re-renders the page with the editor open
            route_values, route_errors = request.form, dict(g.get("route_errors") or {})
            errors.update(route_errors)
        elif posting and any(name in request.form for name in CARGO_FIELDS_OF_LINES):
            # 15b: the quantities and the commodity are the cargo lines'
            message = ("The quantities and the commodity come from the cargo lines now: "
                       "change them in a line of the Cargo card.")
            field = next(name for name in CARGO_FIELDS_OF_LINES if name in request.form)
            if _save_first():
                return jsonify(ok=False, errors=[{"field": field, "label": "Cargo", "anchor": "cargo",
                                                  "message": message}]), 422
            warn(message)
            return redirect(url_for("enquiry", enquiry_id=enquiry_id) + "#cargo")
        elif posting:
            # 2026-09-28: a gap (no delivery place for DAP, no cbm for LCL,
            # no received_on) no longer blocks the save - it is saved and
            # named. A value that cannot be saved (does not parse, qty below
            # 1, a refused location) still blocks: nothing is written and
            # the Not saved banner lists each field.
            # 15a: each card posts only its own fields; nothing else is touched
            posted, errors = forms.read(request.form, _tags(request.form), only_posted=True)
            if "owner" in request.form and posted.get("owner") and posted["owner"] != (row["owner"] or "") \
                    and posted["owner"] not in assign_list.owners(conn):
                # DD1: the person is one of Settings' owners (or Not assigned)
                errors["owner"] = "'{}' is not one of the owners in Settings".format(posted["owner"])
            _resolve_inline(repo, request.form, posted, errors)
            merged = dict(row)
            merged.update(posted)
            forms.validate(merged, errors, complete=False)
            _two_letter_errors(conn, posted, [], errors)
            if not errors:
                extra = {}
                if "requester_party_id" in posted and not posted.get("requester_party_id"):
                    posted.pop("requester_party_id", None)     # never blank the requester
                    extra["requester_party_id"] = "pick who is asking"
                repo.save_enquiry(enquiry_id, posted)
                if posted.get("owner"):
                    db.set_setting(conn, "last_owner", posted["owner"])
                # SC1: a requester changing from (TBC) leaves scope alone
                message = _saved_message(conn, enquiry_id, extra=extra)
                flash(message)
                if _save_first():
                    return jsonify(ok=True, message=message)
                return redirect(url_for("enquiry", enquiry_id=enquiry_id))
            conn.rollback()                  # a location or party added inline goes too
            if _save_first():
                return jsonify(ok=False, errors=forms.error_items(errors)), 422
            values.update(posted)

        # Follow-ups: the ones logged, and the next one's text.
        fu_rows = conn.execute(
            "SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        fu_count = int((fu_rows["fu_count"] if fu_rows is not None else 0) or 0)
        fu_total = int((fu_rows["fu_count_total"] if fu_rows is not None else 0) or 0)
        from .. import wait as wait_mod
        fu_wait = wait_mod.live(conn, enquiry_id)           # WR1: a New case waiting for the requester
        if fu_wait is not None:
            fu_count = len(wait_mod.followups(conn, fu_wait))
        # FX4: no draft any more (she has her own template tool); the next
        # follow-up logged is numbered as the next FU
        fu_step = fu_count + 1
        # FX4: Waiting on us, logged by hand on a Quoted case
        waiting_on_us = tracking.replied_note(conn, enquiry_id) \
            if fu_rows is not None and fu_rows["status"] == "Quoted" else None
        card = _mail_card(conn, fu_rows, request.args.get("mail") == "all")
        shipment = conn.execute("SELECT id FROM shipment WHERE enquiry_id = ?", (enquiry_id,)).fetchone() \
            if db._has_table(conn, "shipment") else None
        # the request group, each member with where its cargo goes
        group = conn.execute(
            "SELECT * FROM v_enquiry WHERE request_group = "
            "(SELECT request_group FROM v_enquiry WHERE id = ?) AND id <> ? "
            "ORDER BY CAST(substr(opp_no, 5) AS INTEGER)",
            (enquiry_id, enquiry_id)).fetchall()
        group_lines = {r["id"]: _deliver_to_of(conn, r) for r in group}
        # a pasted subject that turned out to be another case's (?clash=<id>)
        clash = conn.execute("SELECT id, opp_no, status FROM v_enquiry WHERE id = ?",
                             (request.args.get("clash", type=int),)).fetchone() \
            if request.args.get("clash", type=int) else None
        clash_kind = (request.args.get("clash_kind") or "") if clash else ""
        route_ctx = _route_context(conn, enquiry_id, values, request.args.get("route"), route_values, route_errors)
        case_ctx = _case_context(conn, values, fu_rows, _today(),
                                 mail_count=sum(len(t["mails"]) for t in card[0]) if card[0] else None)
        if not case_ctx.get("closed") and route_ctx.get("cargo_rail"):
            case_ctx["states"]["cargo"] = route_ctx["cargo_rail"]           # 15b
        return render_template(
            "enquiry.html", values=values, errors=errors, enquiry_id=enquiry_id,
            shipment_id=shipment["id"] if shipment else None,
            cargo_state=cargo.summary(conn, enquiry_id, values.get("service_mode")),
            group_lines=group_lines, clash=clash, clash_kind=clash_kind,
            clash_subject=(request.args.get("subject") or "") if clash else "",
            today=_today(), tags=_split_tags(values.get("special_handling")),
            tomorrow=(dt.date.today() + dt.timedelta(days=1)).isoformat(),
            case_ctx=case_ctx,
            mfn=db.is_mfn_party(conn, values.get("requester_party_id")),
            fu_days=tracking.fu_settings(conn),
            mail_card=card,
            ack_state=_ack_state(conn, fu_rows, card),
            who_asked=_who_asked(conn, fu_rows, card),
            **_who_outlook_context(conn, fu_rows),
            schema_version=db.get_setting(conn, "schema_version", "?"),
            followups=conn.execute(
                "SELECT * FROM status_event WHERE enquiry_id = ? AND kind = 'followup' "
                "ORDER BY voided, at, id", (enquiry_id,)).fetchall(),
            fu_step=fu_step, fu_count=fu_count, fu_count_total=fu_total, fu_wait=fu_wait,
            wait_fu_ids=set(wait_mod.of_waits(conn, enquiry_id)),         # FX7: 'FU<n> · our question',
            fu_channels=FU_CHANNELS, waiting_on_us=waiting_on_us,
            waiting_gist=tracking._reply_gist(waiting_on_us[1], 200) if waiting_on_us else "",
            what_max=WAITING_WHAT_MAX, answer_max=ANSWER_NOTE_MAX,
            wait_prefill=wait_mod.prefill(conn, fu_rows) if fu_rows is not None and fu_rows["status"] == "New" else "",
            wait_max=wait_mod.WHAT_MAX,
            # threads attached by hand (v20), struck-out ones last
            threads=conn.execute(
                "SELECT * FROM enquiry_thread WHERE enquiry_id = ? AND source <> 'register' "
                "ORDER BY voided, added_on, id", (enquiry_id,)).fetchall(),
            subject_lines=_subject_lines(conn, enquiry_id),
            registered_shared=_shared_of(conn, enquiry_id, values.get("thread_key")),
            error_items=forms.error_items(errors),
            **route_ctx,
            attach_offer=request.args.get("attach") == "1",
            attach_subject=request.args.get("subject") or "",
            events=conn.execute(
                "SELECT * FROM status_event WHERE enquiry_id = ? "
                "ORDER BY at DESC, id DESC", (enquiry_id,)).fetchall(),
            # D7b: the changes made on the team site, beside the events
            timeline_items=_timeline_items(conn, enquiry_id),
            # Q2a: each vendor's newest rates typed on the team site
            vendor_rates=_vendor_rates(conn, enquiry_id),
            # Q3: the quote card's quotes, the timeline's links, the Won form's start
            quotes=site_quotes.view(conn, enquiry_id),
            quote_marks=site_quotes.marks(conn, enquiry_id),
            won_prefill=_won_prefill(conn, enquiry_id),
            # RG2a (layer 2): 'Remember <words> as <place>' under the Routes card
            remember_places=places.remember_offers(conn, enquiry_id),
            # Q2d: Read quote on each live row, and its panel (?read=<request id>)
            read_quote=_read_quote_ctx(conn, fu_rows, app.config["DB_PATH"]),
            vendors=conn.execute(
                "SELECT v.*, p.key AS vendor_key FROM vendor_request v "
                "JOIN party p ON p.id = v.vendor_party_id "
                "WHERE v.enquiry_id = ? ORDER BY v.voided, v.asked_on, v.id",
                (enquiry_id,)).fetchall(),
            vendor_statuses=VENDOR_STATUSES,
            rate_used_choices=status_mod.RATE_USED,
            rate_used_labels=status_mod.RATE_USED_LABELS,
            awarded_party_key=(conn.execute(
                "SELECT key FROM party WHERE id = ?",
                (values.get("awarded_vendor_id"),)).fetchone() or {"key": ""})["key"],
            all_parties=conn.execute(
                "SELECT id, key FROM party WHERE merged_into IS NULL AND placeholder = 0 "
                "ORDER BY key COLLATE NOCASE").fetchall(),
            offices=conn.execute(
                "SELECT id, key FROM party WHERE type = 'Office' "
                "AND merged_into IS NULL ORDER BY key COLLATE NOCASE").fetchall(),
            # Q2b: Carrier direct lists the coloaders and truckers too
            carriers=conn.execute(
                "SELECT id, key FROM party WHERE type IN ({}) AND merged_into IS NULL ORDER BY key COLLATE NOCASE".format(
                    ",".join("?" for _ in db.CARRIER_TYPES)), db.CARRIER_TYPES).fetchall(),
            group=group,
            suggestions=suggestions.pending(conn, enquiry_id),
            sugg_items=suggestions.view(conn, suggestions.pending(conn, enquiry_id), _today()),
            sugg_dismissed=suggestions.dismissed(conn, enquiry_id),
            edited_events=suggestions.edited_events(conn, "status_event", [r[0] for r in conn.execute(
                "SELECT id FROM status_event WHERE enquiry_id = ?", (enquiry_id,))]),
            lost_event_id=g.repo.lost_closing_event(enquiry_id),
            site_by=firebase_pull.site_by(conn),
            att_panel=_attachment_panel(conn, enquiry_id, values),
            **reference(conn), **settings_lists(conn))

    # ------------------------------------------ 4.2.4 cargo lines (v28) --
    @app.route("/enquiries/<int:enquiry_id>/cargo/preview", methods=["POST"])
    def enquiry_cargo_preview(enquiry_id):
        """15b: the totals and the checks as they would be with the open line
        editor's values (a line changed, or a new one); nothing is written."""
        conn = g.conn
        values = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if values is None:
            abort(404)
        values = dict(values)
        typed, _bad = cargo.clean(request.form)
        typed = cargo.normalise(typed)
        line_id = request.form.get("line_id", type=int)
        lines = cargo.lines_of(conn, enquiry_id)
        if line_id:
            lines = [dict(l, **typed) if l["id"] == line_id else l for l in lines]
        else:
            lines = lines + [dict(typed, id=None, counted=typed.get("counted", 1))]
        live = routes.routes_of(conn, enquiry_id)
        case = conn.execute("SELECT * FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        t = routes.totals(conn, enquiry_id, lines=lines, live_routes=live, case=case)
        card = _cargo_card(conn, enquiry_id, values, live, lines, t)
        conn.rollback()
        html = render_template("_cargo_live.html", values=values, enquiry_id=enquiry_id, live_routes=live, **card)
        return jsonify(ok=True, html=html)

    @app.route("/enquiries/<int:enquiry_id>/cargo/read", methods=["POST"])
    def enquiry_cargo_read(enquiry_id):
        """15b: Read into lines - the registration reader run on the text
        'Show the mail text' shows; the lines it finds come back as open
        editors. Nothing is written until each line's Save line."""
        from .. import cargo_card
        conn = g.conn
        values = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if values is None:
            abort(404)
        values = dict(values)
        live = routes.routes_of(conn, enquiry_id)
        card = _cargo_card(conn, enquiry_id, values, live, cargo.lines_of(conn, enquiry_id), routes.totals(conn, enquiry_id))
        found = cargo_card.read_lines(card["cargo_text"]["text"] if "text" in card["cargo_text"] else "")
        units = packing.units_of(conn)
        for line in found:
            # v39: the Packing each word means on her list (the word kept)
            line["pack_unit"] = packing.unit_for(line.get("pack_word"), units) if line.get("pack_word") else None
        html = render_template("_cargo_new_lines.html", values=values, enquiry_id=enquiry_id, live_routes=live,
                               found=found, cargo_fcl=card["cargo_fcl"], **settings_lists(conn))
        return jsonify(ok=True, n=len(found), html=html,
                       message="" if found else "The reader found no cargo lines in this text.")

    @app.route("/enquiries/<int:enquiry_id>/cargo", methods=["POST"])
    def enquiry_cargo(enquiry_id):
        """Add, edit, remove (strike out), put back, or split a cargo line.
        Every write goes through the repository, which rewrites the case's
        totals."""
        conn, repo = g.conn, g.repo
        if conn.execute("SELECT 1 FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone() is None:
            abort(404)
        action = request.form.get("action") or "add"
        line_id = request.form.get("line_id", type=int)
        back = redirect(url_for("enquiry", enquiry_id=enquiry_id) + "#cargo")
        if action == "answer":
            # a cargo check's answer (v34): 'count|12|1', 'reading|12|1940', 'keep_case||air'
            what, _, rest = (request.form.get("answer") or "").partition("|")
            line, _, value = rest.partition("|")
            message = repo.cargo_answer(enquiry_id, what, value, int(line) if line.isdigit() else None)
            if not message:
                abort(400)
            say(message)
            return back
        if action != "add":
            line = conn.execute("SELECT * FROM enquiry_cargo WHERE id = ? AND enquiry_id = ?",
                                (line_id, enquiry_id)).fetchone() if line_id else None
            if line is None:
                abort(404)
        if action in ("add", "save"):
            values, bad = cargo.clean(request.form, packing.units_of(conn))
            if bad:
                if _save_first():
                    return jsonify(ok=False, errors=[
                        {"field": k, "label": "Cargo line " + k.replace("_", " "), "anchor": "cargo",
                         "message": v} for k, v in bad.items()]), 422
                # without the page's script: the old message (13c: the script keeps the typing)
                warn("Cargo line not saved: " + "; ".join(
                    "{} {}".format(k.replace("_", " "), v) for k, v in bad.items()))
                return back
            if action == "add":
                if cargo.is_blank(request.form):
                    return _refuse("Nothing to add: give the line pieces, a size, a weight or a cbm.", back,
                                   "pieces", "Cargo line", "cargo")
                values["source"] = "manual"
                repo.add_cargo_line(enquiry_id, values)
                flash(TEXT["cargo_added"])
            else:
                changed = repo.edit_cargo_line(line_id, values)
                say(TEXT["cargo_saved"] if changed else "Nothing to change", done=bool(changed))
        elif action in ("void", "unvoid"):
            repo.void_cargo_line(line_id, voided=(action == "void"))
            flash(TEXT["cargo_removed"] if action == "void" else TEXT["cargo_put_back"])
        else:                  # 'split' went with v33: a destination is a route, not a case
            abort(400)
        if _save_first():
            return jsonify(ok=True)
        return back

    # ------------------------------------ 4.2.4 subjects and same request --
    @app.route("/enquiries/<int:enquiry_id>/subjects", methods=["POST"])
    def enquiry_subjects(enquiry_id):
        """'Add another subject': the pasted subject's thread key joins the
        case (enquiry_thread, kind thread_key) unless it is already another
        case's - then that case is shown instead. The archive is counted,
        not required: the mails may come later."""
        conn, repo = g.conn, g.repo
        case = conn.execute("SELECT id, opp_no, thread_key FROM enquiry WHERE id = ?",
                            (enquiry_id,)).fetchone()
        if case is None:
            abort(404)
        from .. import case_numbers
        subject = " ".join((request.form.get("subject") or "").split())
        key = parsing.subject_thread_key(subject)
        back = redirect(url_for("enquiry", enquiry_id=enquiry_id) + "#subjects")
        if not key:
            return _refuse(TEXT["subj_empty"], back, "subject", "Subject", "subjects")
        # N1: a case number in the subject decides first (5.0)
        named = case_numbers.valid_cases(conn, subject)
        if len(named) >= 2:
            warn(TEXT["subj_two_numbers"].format(", ".join(r["opp_no"] for r in named)))
            return back
        if len(named) == 1 and named[0]["id"] == enquiry_id:
            flash(TEXT["subj_own_number"].format(opp=case["opp_no"], n=_numbered_count(conn, case)))
            return back
        if len(named) == 1:
            warn(TEXT["subj_number_clash"].format(opp=named[0]["opp_no"]))
            return redirect(url_for("enquiry", enquiry_id=enquiry_id, clash=named[0]["id"], clash_kind="number",
                                    subject=subject[:300]) + "#subjects")
        owners = case_numbers.case_owners(conn, enquiry_id, key=key)
        others = [o for o in owners if o["id"] != enquiry_id]
        matched = _archive_matches(subject, key)
        if enquiry_id in {o["id"] for o in owners}:
            warn(TEXT["subj_already"].format(n=matched) + _split_sentence(case_numbers.shared_with(enquiry_id, owners)))
            return back
        if others and request.form.get("anyway") != "1":
            warn(TEXT["subj_clash"].format(opp=others[0]["opp_no"]))
            return redirect(url_for("enquiry", enquiry_id=enquiry_id, clash=others[0]["id"],
                                    subject=subject[:300]) + "#subjects")
        if repo.attach_thread(enquiry_id, "thread_key", key, _today(), note=subject, source="manual") is None:
            warn(TEXT["subj_already"].format(n=matched))
            return back
        owners = case_numbers.case_owners(conn, enquiry_id, key=key)
        flash(TEXT["subj_added"].format(n=matched) + _split_sentence(case_numbers.shared_with(enquiry_id, owners)))
        return back

    # ------------------------------------------ 4.8 update from Outlook --
    @app.route("/enquiries/<int:enquiry_id>/update-from-outlook", methods=["POST"])
    def enquiry_update_from_outlook(enquiry_id):
        """Read the mail selected in Outlook, and what followed it in its
        conversation, into pending suggestions for this case. Nothing is
        written to the case until a suggestion is accepted."""
        conn, repo = g.conn, g.repo
        case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if case is None:
            abort(404)
        try:
            mail = outlook_bridge.read_selection()
        except outlook_bridge.OutlookError as exc:
            logs.get("web").info("outlook selection refused: %s", exc.detail)
            warn(exc.message)
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        found = suggestions.membership(conn, case, mail)
        if not found.belongs:
            text = "{} ({}).".format(TEXT["not_this_case"], found.how)
            if found.others:
                text += " {} {}.".format(TEXT["maybe_cases"], ", ".join(
                    opp for _, opp in found.others))
            warn(text)
            # ?attach=1 puts the rescue button under the message: attach
            # this mail's thread to the case by hand (v20)
            return redirect(url_for("enquiry", enquiry_id=enquiry_id, attach=1,
                                    subject=(mail.subject or "")[:120]))
        return _suggest_from_mail(repo, case, mail, found.how)

    @app.route("/enquiries/<int:enquiry_id>/attach-thread", methods=["POST"])
    def enquiry_attach_thread(enquiry_id):
        """The rescue: the selected mail's conversation id and thread key
        become the case's too (enquiry_thread), then Update from
        Outlook runs as if the mail had matched. The subject may have been
        changed by the other side or the conversation header lost; a person
        says it is the same thread, and from then on the app agrees."""
        conn, repo = g.conn, g.repo
        case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if case is None:
            abort(404)
        from_card = bool((request.form.get("conversation_id") or request.form.get("subject") or "").strip())
        if from_card:
            # the Mail card's 'Use as this case's thread' names the mail
            # itself (2026-09-25); the header button reads Outlook. The
            # archived row is used when it is still there, so the update
            # can run on it; otherwise only the thread is attached.
            mail = _archived_mail(request.form.get("message_id")) if request.form.get("message_id") else None
            if mail is None:
                mail = _ThreadOnly(request.form.get("conversation_id"), request.form.get("subject"))
        else:
            try:
                mail = outlook_bridge.read_selection()
            except outlook_bridge.OutlookError as exc:
                logs.get("web").info("outlook selection refused: %s", exc.detail)
                warn(exc.message)
                return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        subject = (mail.subject or "").strip()
        key = parsing.subject_thread_key(subject)
        added = []
        for kind, value in (("conversation", mail.conversation_id or ""), ("thread_key", key)):
            if value and repo.attach_thread(enquiry_id, kind, value, _today(), note=subject):
                added.append(kind.replace("_", " "))
        repo.commit()
        # no thread key on the page, in a flash either (12b)
        say("{} attached to {}.".format("Thread" if added else "Thread already", case["opp_no"]), done=bool(added))
        if isinstance(mail, _ThreadOnly):
            return redirect(url_for("enquiry", enquiry_id=enquiry_id) + "#mail")
        found = suggestions.membership(conn, case, mail)
        if not found.belongs:
            warn("{} ({}).".format(TEXT["not_this_case"], found.how))
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        return _suggest_from_mail(repo, case, mail, found.how)

    # ------------------------------------- RG2a (layer 2): remember a place --
    @app.route("/enquiries/<int:enquiry_id>/remember-place", methods=["POST"])
    def enquiry_remember_place(enquiry_id):
        """The mail's words for a side become a manual alias of the place
        she gave that side instead of the reader's, once; refused with why
        when the words already name another place."""
        conn, repo = g.conn, g.repo
        if conn.execute("SELECT 1 FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone() is None:
            abort(404)
        side = request.form.get("side") or ""
        offer = next((o for o in places.remember_offers(conn, enquiry_id) if o["side"] == side), None)
        if offer is None:
            warn("Nothing to remember for that place.")
            return redirect(url_for("enquiry", enquiry_id=enquiry_id) + "#routes")
        try:
            places.remember(repo, offer)
        except ValueError as exc:
            conn.rollback()
            warn(str(exc))
            return redirect(url_for("enquiry", enquiry_id=enquiry_id) + "#routes")
        conn.commit()
        flash("Remembered '{}' as {}: the next mail with these words reads {}.".format(
            offer["words"], offer["key"], offer["key"]))
        return redirect(url_for("enquiry", enquiry_id=enquiry_id) + "#routes")

    # ------------------------------------------------ 4.2.7 routes (v33) --
    @app.route("/enquiries/<int:enquiry_id>/routes", methods=["POST"])
    def enquiry_routes(enquiry_id):
        """Save one route (new or edited), strike one out, or put it back
. A value that cannot be saved writes nothing and the
        page comes back with the editor open and the Not saved banner; a
        gap (DAP without a delivery place) is saved and named."""
        conn, repo = g.conn, g.repo
        if conn.execute("SELECT 1 FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone() is None:
            abort(404)
        action = request.form.get("action") or "save"
        route_id = request.form.get("route_id", type=int)
        back = redirect(url_for("enquiry", enquiry_id=enquiry_id) + "#routes")
        if route_id and conn.execute("SELECT 1 FROM enquiry_route WHERE id = ? AND enquiry_id = ?",
                                     (route_id, enquiry_id)).fetchone() is None:
            abort(404)
        if action in ("void", "unvoid"):
            try:
                repo.void_route(route_id, voided=action == "void")
            except ValueError as exc:
                return _refuse(str(exc), back, "", "Route", "routes")
            letter = routes.letter(conn.execute("SELECT seq FROM enquiry_route WHERE id = ?",
                                                (route_id,)).fetchone()["seq"])
            flash(TEXT["route_struck"].format(letter) if action == "void" else TEXT["route_put_back"].format(letter))
            return back
        values, errors = _read_route(conn, repo, request.form, route_id=route_id)
        if not errors and route_id:
            # C1 (6b): a result that would close the case before its latest event
            live = routes.routes_of(conn, enquiry_id)
            old = next((r for r in live if r["id"] == route_id), None)
            if old is not None and (values.get("result") or None) != (old.get("result") or None):
                after = [dict(r, result=values.get("result")) if r["id"] == route_id else r for r in live]
                target = routes.status_target(after)
                current = conn.execute("SELECT status FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()["status"]
                if target and target != current:
                    refusal = repo.closing_refusal(enquiry_id, target, values.get("result_on")
                                                   or old.get("result_on") or _today())
                    if refusal:
                        errors["result"] = refusal
        if errors:
            conn.rollback()
            if _save_first():
                return jsonify(ok=False, errors=forms.error_items(errors)), 422
            # 24b-fix: post, redirect, get - never a page at this address
            app.extensions.setdefault("route_stash", {})[enquiry_id] = {"form": request.form.copy(),
                                                                         "errors": dict(errors)}
            return redirect(url_for("enquiry", enquiry_id=enquiry_id, refused=1) + "#routes")
        if route_id:
            repo.edit_route(route_id, values, at=_today())
        else:
            route_id = repo.add_route(enquiry_id, values)
        message = _saved_message(conn, enquiry_id, prefix=TEXT["route_saved"].format(
            routes.letter(conn.execute("SELECT seq FROM enquiry_route WHERE id = ?", (route_id,)).fetchone()["seq"])))
        flash(message)
        if _save_first():
            return jsonify(ok=True, message=message)
        return back

    @app.route("/enquiries/<int:enquiry_id>/subjects/strike", methods=["POST"])
    def enquiry_subject_strike(enquiry_id):
        """One subject line (2026-09-28): every attached row that shares its
        text - the Outlook conversation and the subject key - is struck out,
        or put back, together. Never deleted."""
        ids = [int(i) for i in (request.form.get("ids") or "").split(",") if i.strip().isdigit()]
        rows = [g.conn.execute("SELECT id FROM enquiry_thread WHERE id = ? AND enquiry_id = ?",
                               (i, enquiry_id)).fetchone() for i in ids]
        if not ids or any(r is None for r in rows):
            abort(404)
        back = request.form.get("action") == "unvoid"
        for thread_id in ids:
            g.repo.void_thread(thread_id, voided=not back)
        flash(TEXT["subj_put_back"] if back else TEXT["subj_struck"])
        return redirect(url_for("enquiry", enquiry_id=enquiry_id) + "#subjects")

    @app.route("/enquiries/<int:enquiry_id>/threads/<int:thread_id>", methods=["POST"])
    def enquiry_thread_void(enquiry_id, thread_id):
        """Strike an attached thread out, or put it back. Never deleted."""
        row = g.conn.execute("SELECT voided FROM enquiry_thread WHERE id = ? AND enquiry_id = ?",
                             (thread_id, enquiry_id)).fetchone()
        if row is None:
            abort(404)
        g.repo.void_thread(thread_id, voided=not row["voided"])
        flash("Attached thread {}.".format("put back" if row["voided"] else "struck out"))
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    def _suggest_from_mail(repo, case, mail, how):
        """The second half of Update from Outlook: read the rest of the
        thread and turn it into pending suggestions."""
        enquiry_id = case["id"]
        _remember_attachments(enquiry_id, mail)          # batch 14 item 7
        # The archive first: the rest of the thread is usually there, and
        # reading it costs Outlook nothing. Only a thread the archive has
        # not seen (a new one, or one synced before the reply came) goes
        # to Outlook's own conversation view.
        made, where = None, "mail archive"
        blocked = []                    # FX6: what kept a suggestion from being made again
        if mail_archive.usable()[0]:
            try:
                with mail_archive.ArchiveSearch() as search:
                    if search.later_in_conversation(mail):
                        made = suggestions.from_mail(repo, case, mail, search, blocked=blocked)
            except mail_archive.ArchiveError as exc:
                logs.get("web").info("mail archive refused: %s", exc)
        if made is None:
            where = "Outlook"
            try:
                with outlook_bridge.Search() as search:
                    made = suggestions.from_mail(repo, case, mail, search, blocked=blocked)
            except outlook_bridge.OutlookError as exc:
                warn(exc.message)
                return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        flash("{} suggestion(s) from the mail ({}, thread read from {}); accept or "
              "dismiss below.".format(len(made), how, where) if made else suggestions.nothing_new(blocked))
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    @app.route("/enquiries/<int:enquiry_id>/attachments/accept", methods=["POST"])
    def enquiry_attachment_accept(enquiry_id):
        """Accept one thing the mail's attachments say (batch 14 item 7):
        written through the repository, then back to the list without it."""
        if g.conn.execute("SELECT 1 FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone() is None:
            abort(404)
        try:
            finding = json.loads(request.form.get("finding") or "{}")
            say(intake.apply_finding(g.repo, enquiry_id, finding, _today()))
        except (ValueError, KeyError, TypeError) as exc:
            warn(str(exc) or "That could not be accepted.")
            finding = {}
        skip = ",".join(k for k in ((request.form.get("skip") or "").split(",") + [finding.get("key") or ""]) if k)
        return redirect(url_for("enquiry", enquiry_id=enquiry_id, att=1, skip=skip) + "#attachments")

    @app.route("/suggestions/<int:suggestion_id>/accept", methods=["POST"])
    def suggestion_accept(suggestion_id):
        at = (request.form.get("at") or "").strip() or None
        try:
            say(suggestions.accept(g.repo, suggestion_id, at=at, today=_today(),
                                     case=request.form.get("case")))
        except ValueError as exc:
            warn(str(exc))
        return redirect(_safe_next(request.form.get("next")))

    @app.route("/suggestions/<int:suggestion_id>/record", methods=["POST"])
    def suggestion_record(suggestion_id):
        """SG1: one of a status or note suggestion's buttons - what Edit
        writes with that Record as, the suggestion's date and text. Refused
        (Lost... without a reason), it stays pending."""
        action = (request.form.get("record_as") or "").strip()
        try:
            say(suggestions.record_as(g.repo, suggestion_id, action,
                                      reason=(request.form.get("reason_" + action) or "").strip() or None,
                                      today=_today()))
        except ValueError as exc:
            g.conn.rollback()
            warn(str(exc))
        return redirect(_safe_next(request.form.get("next")))

    @app.route("/suggestions/<int:suggestion_id>/edit", methods=["POST"])
    def suggestion_edit(suggestion_id):
        """E1: a suggestion recorded as she changed it in its Edit form,
        through the repository calls the case page's forms use, source mail.
        Refused (a reason missing, a closing dated before the latest event),
        it stays pending."""
        conn, repo = g.conn, g.repo
        back = _safe_next(request.form.get("next"))
        row = conn.execute("SELECT * FROM suggestion WHERE id = ?", (suggestion_id,)).fetchone()
        if row is None or row["state"] != "pending":
            warn("That suggestion is not pending.")
            return redirect(back)
        form = request.form
        try:
            if row["kind"] in ("status", "note"):
                action = (form.get("record_as") or "").strip()
                offered = [o[0] for o in suggestions.options(conn, row, _today())]
                if action not in offered:
                    raise ValueError("Record as: not one this case offers.")
                decision = {"action": action, "at": form.get("at"), "note": form.get("note"),
                            "reason": form.get("requote_reason") if action in ("requote", "history_requote")
                            else form.get("closed_reason"),
                            "lost_their_price": form.get("lost_their_price"), "lost_our_price": form.get("lost_our_price")}
                if action == "won":
                    decision["rate"] = _rate_used_from(form, row["enquiry_id"])
                    if "routes_present" in form:
                        decision["routes"] = _won_routes_from(conn, form, row["enquiry_id"])
                say(suggestions.decide(repo, row, decision, _today()))
            elif row["kind"] == "followup":
                # FX7: the date and the note
                say(suggestions.decide_followup(repo, row, {"at": form.get("date"), "note": form.get("note")}))
            elif row["kind"] in suggestions.SHIPMENT_KINDS:
                item = suggestions.view(conn, [row], _today())[0]
                name = (form.get("milestone") or "").strip()
                if row["kind"] == "shipment_milestone" and name not in item["milestones"]:
                    raise ValueError("'{}' is not a milestone of this shipment.".format(name))
                if not name:
                    raise ValueError("Say what it is.")
                day = (form.get("date") or "").strip()
                try:
                    dt.date.fromisoformat(day)
                except ValueError:
                    raise ValueError("Give the date.")
                time = (form.get("time") or "").strip() or "00:00"
                say(suggestions.decide_shipment(repo, row, {"milestone": name, "at": "{} {}".format(day, time),
                                                            "note": (form.get("note") or "").strip(),
                                                            "planned": bool(form.get("planned"))}))
            else:
                raise ValueError("This suggestion has no Edit.")
        except ValueError as exc:
            conn.rollback()
            warn(str(exc))
        return redirect(back)

    @app.route("/site-evidence/<action_id>/<name>")
    def site_evidence(action_id, name):
        """W2b: a picture of the mail a colleague pasted with a Won or Lost on
        the team site, from data\\site-evidence\\<id>\\ (only 1.jpg ... 9.jpg)."""
        from flask import send_from_directory
        from .. import site_close
        if not site_close.IMAGE_NAME.match(name or ""):
            abort(404)
        try:
            folder = site_close.evidence_dir(action_id)
        except ValueError:
            abort(404)
        if not (folder / name).is_file():
            abort(404)
        return send_from_directory(str(folder), name, mimetype="image/jpeg")

    @app.route("/suggestions/<int:suggestion_id>/put-back", methods=["POST"])
    def suggestion_put_back(suggestion_id):
        try:
            flash(suggestions.put_back(g.repo, suggestion_id))
        except ValueError as exc:
            warn(str(exc))
        return redirect(_safe_next(request.form.get("next")))

    @app.route("/suggestions/<int:suggestion_id>/dismiss", methods=["POST"])
    def suggestion_dismiss(suggestion_id):
        try:
            flash(suggestions.dismiss(g.repo, suggestion_id))
        except ValueError as exc:
            warn(str(exc))
        return redirect(_safe_next(request.form.get("next")))

    @app.route("/enquiries/<int:enquiry_id>/confirm", methods=["POST"])
    def enquiry_confirm(enquiry_id):
        """The 'unconfirmed' badge's one click: the status stands."""
        if g.conn.execute("SELECT 1 FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone() is None:
            abort(404)
        g.repo.confirm_status(enquiry_id)
        flash("Status confirmed.")
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    @app.route("/enquiries/<int:enquiry_id>/status", methods=["POST"])
    def enquiry_status(enquiry_id):
        repo = g.repo
        status = request.form.get("status")
        at = request.form.get("at") or _today()
        if status == "note":
            # 'Note only': a dated line on the timeline, the status untouched.
            note = (request.form.get("note") or "").strip()
            if not note:
                return _refuse("A note needs some text.", redirect(url_for("enquiry", enquiry_id=enquiry_id)), "note", "Note")
            repo.add_note(enquiry_id, at, note)
            flash("Note added")
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        if status in ("waiting", "info_received"):
            # WR1: a note on the timeline; the status stays New
            from .. import wait
            if status == "waiting":
                event_id, why = wait.start(repo, enquiry_id, request.form.get("what") or "", at)
                if event_id is None:
                    return _refuse(why, redirect(url_for("enquiry", enquiry_id=enquiry_id)), "what", "What we need")
                flash("Waiting for requester since {}".format(at))
            else:
                event_id, why = wait.info_received(repo, enquiry_id, _today())
                if event_id is None:
                    warn(why)
                    return redirect(url_for("enquiry", enquiry_id=enquiry_id))
                flash("Info received: the case is back to work")
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        if status == "hold":
            # 'On hold': why and until when; the status stays (v11).
            reason = (request.form.get("hold_reason") or "").strip()
            if not reason:
                return _refuse("On hold needs a reason.", redirect(url_for("enquiry", enquiry_id=enquiry_id)), "hold_reason", "Reason")
            until = (request.form.get("hold_until") or "").strip() or None
            # 15a: a hold says until when, and that is after today
            if not until:
                return _refuse("On hold needs an Until date.", redirect(url_for("enquiry", enquiry_id=enquiry_id)),
                               "hold_until", "Until")
            if until <= _today():
                return _refuse("Until must be after today.", redirect(url_for("enquiry", enquiry_id=enquiry_id)),
                               "hold_until", "Until")
            repo.set_hold(enquiry_id, reason, at, until=until)
            flash("On hold: {}{}".format(reason, " until {}".format(until) if until else ""))
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        if status not in status_mod.ALL_STATUSES:
            abort(400)
        reason = (request.form.get("closed_reason") or "").strip() or None
        if status in status_mod.REASON_STATUSES and not reason:
            return _refuse("{} needs a reason.".format(status), redirect(url_for("enquiry", enquiry_id=enquiry_id)), "closed_reason", "Reason")
        # Lost, Cancelled and Dropped have their own lists: a
        # reason from another list is a mis-click, not a decision.
        if not status_mod.reason_belongs(g.conn, status, reason):
            return _refuse("'{}' is not a {} reason.".format(reason, status), redirect(url_for("enquiry", enquiry_id=enquiry_id)), "closed_reason", "Reason")
        # C1 (6b): a closing dated before the case's latest event is refused
        refusal = repo.closing_refusal(enquiry_id, status, at)
        if refusal:
            return _refuse(refusal, redirect(url_for("enquiry", enquiry_id=enquiry_id)), "at", "Date")

        rate = None
        if status == "Won":
            try:
                rate = _rate_used_from(request.form, enquiry_id)
            except ValueError as exc:
                return _refuse(str(exc), redirect(url_for("enquiry", enquiry_id=enquiry_id)), "rate_used", "Rate used")
        # Why the new quote (v22): only on a Quoted, only from the list,
        # optional. Anything sent with another status is ignored.
        requote_reason = (request.form.get("requote_reason") or "").strip() or None
        if status != "Quoted":
            requote_reason = None
        elif not status_mod.requote_reason_belongs(g.conn, requote_reason):
            return _refuse("'{}' is not a re-quote reason.".format(requote_reason), redirect(url_for("enquiry", enquiry_id=enquiry_id)),
                           "requote_reason", "Re-quote reason")

        note = (request.form.get("note") or "").strip() or None
        if status == "Won" and "routes_present" in request.form:
            # the Won picker: the ticked routes won; an unticked
            # route with a reason lost, without one it keeps no result
            try:
                won_ids, lost, cancelled = _won_routes_from(g.conn, request.form, enquiry_id)
            except _RouteRefusal as exc:
                return _refuse(str(exc), redirect(url_for("enquiry", enquiry_id=enquiry_id)), exc.field, exc.label)
            repo.last_status_outcome = None
            repo.record_results(enquiry_id, at, won_ids, lost, note=note, cancelled=cancelled)
            followed = True
        elif status == "Quoted" and repo.conn.execute(
                "SELECT 1 FROM enquiry WHERE id = ? AND status = 'Quoted'", (enquiry_id,)).fetchone():
            # Quoted on a Quoted case is a re-quote (13d, v22): written with
            # its reason and note, whatever the date
            repo.quote_again(enquiry_id, at, note=note, reason=requote_reason)
            followed = True
        else:
            followed = repo.set_status(enquiry_id, status, at, note=note, closed_reason=reason, reason=requote_reason)
        if rate is not None:
            repo.set_rate_used(enquiry_id, *rate)
        cut = []
        if status == "Lost":
            # Their price / Our price (v21): what report section 7 shows
            # beside the note. Blank boxes leave what is there.
            prices, cut = status_mod.lost_prices(request.form.get)
            if prices:
                repo.update("enquiry", enquiry_id, prices)
        said = _status_said(g.conn, repo, enquiry_id, status, at, followed)
        say(" ".join(filter(None, (said, status_mod.prices_cut_text(cut)))))
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    @app.route("/enquiries/<int:enquiry_id>/rate-used", methods=["POST"])
    def enquiry_rate_used(enquiry_id):
        """Change whose rate a won case used, after the fact."""
        try:
            rate = _rate_used_from(request.form, enquiry_id)
        except ValueError as exc:
            return _refuse(str(exc), redirect(url_for("enquiry", enquiry_id=enquiry_id)), "rate_used", "Rate used")
        g.repo.set_rate_used(enquiry_id, *rate)
        flash("Rate used: {}".format(status_mod.RATE_USED_LABELS[rate[0]]))
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    @app.route("/enquiries/<int:enquiry_id>/requote", methods=["POST"])
    def enquiry_requote(enquiry_id):
        """Re-quoted (v30): a revised quote went out - one requote
        event on Quoted with its date, reason and note.
        Only a quoted case has a round to reopen."""
        row = g.conn.execute("SELECT opp_no, status FROM enquiry WHERE id = ?",
                             (enquiry_id,)).fetchone()
        if row is None:
            abort(404)
        if row["status"] != "Quoted":
            return _refuse("Re-quote is for a quoted case; {} is {}.".format(row["opp_no"], row["status"]),
                           redirect(url_for("enquiry", enquiry_id=enquiry_id)), "", "Re-quoted")
        at = (request.form.get("at") or "").strip() or _today()
        reason = (request.form.get("requote_reason") or "").strip() or None
        if not status_mod.requote_reason_belongs(g.conn, reason):
            return _refuse("'{}' is not a re-quote reason.".format(reason),
                           redirect(url_for("enquiry", enquiry_id=enquiry_id)), "requote_reason", "Why")
        g.repo.requote(enquiry_id, at, note=request.form.get("note"), reason=reason)
        say(_status_said(g.conn, g.repo, enquiry_id, "Quoted", at)
              if g.repo.last_status_outcome == "repeat" else
              "{} re-quoted on {}.".format(row["opp_no"], at))
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    @app.route("/enquiries/<int:enquiry_id>/hold/release", methods=["POST"])
    def enquiry_release_hold(enquiry_id):
        if g.repo.release_hold(enquiry_id, _today()):
            flash("Hold released")
        else:
            warn("The case was not on hold.")
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    @app.route("/enquiries/<int:enquiry_id>/scope", methods=["POST"])
    def enquiry_scope(enquiry_id):
        """Flip a case between In and Out (v9); logged like any field."""
        scope = "Out" if request.form.get("scope") == "Out" else "In"
        g.repo.update("enquiry", enquiry_id, {"scope": scope})
        g.repo.touch("enquiry", enquiry_id)
        flash("Scope: {}".format(scope))
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    @app.route("/enquiries/<int:enquiry_id>/latam", methods=["POST"])
    def enquiry_latam(enquiry_id):
        """SC1: LATAM set by hand under More - automatic, yes or no
        (enquiry.latam_choice), through the repository like any field."""
        if g.conn.execute("SELECT 1 FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone() is None:
            abort(404)
        wanted = (request.form.get("latam") or "").strip().upper()
        choice = wanted if wanted in ("Y", "N") else None
        g.repo.update("enquiry", enquiry_id, {"latam_choice": choice})
        g.repo.touch("enquiry", enquiry_id)
        flash("LATAM: {}".format({"Y": "yes", "N": "no"}.get(choice or "", "automatic")))
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    @app.route("/enquiries/<int:enquiry_id>/note", methods=["POST"])
    def enquiry_note(enquiry_id):
        """A dated note on the timeline; it changes no status."""
        note = (request.form.get("note") or "").strip()
        if not note:
            warn("A note needs some text.")
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        g.repo.add_note(enquiry_id, request.form.get("at") or _today(), note)
        flash("Note added")
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    @app.route("/enquiries/<int:enquiry_id>/followups", methods=["POST"])
    def enquiry_followup(enquiry_id):
        """Log as sent / Log a call / WhatsApp / Teams / Other on the
        Follow-ups card (FX4): one followup event, numbered as the
        next FU (a case waiting for the requester: the next from the wait)."""
        from .. import wait as wait_mod
        if g.conn.execute("SELECT 1 FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone() is None:
            abort(404)
        at = (request.form.get("at") or "").strip() or _today()
        channel = request.form.get("channel") if request.form.get("channel") in dict(FU_CHANNELS) else "mail"
        waiting = wait_mod.live(g.conn, enquiry_id)
        seq = len(wait_mod.followups(g.conn, waiting)) + 1 if waiting is not None else None
        event_id = g.repo.add_followup(enquiry_id, at, channel=channel, seq=seq)
        seq = g.conn.execute("SELECT seq FROM status_event WHERE id = ?", (event_id,)).fetchone()["seq"]
        # FX7: the mail's follow-up suggestion for that day is closed - this one stands
        closed = suggestions.close_logged_followups(g.repo, enquiry_id)
        flash("FU{} logged ({}, {}).{}".format(seq, channel, at, " The follow-up found in the mail that day was "
                                               "closed: this one stands." if closed else ""))
        return redirect(url_for("enquiry", enquiry_id=enquiry_id) + "#followups")

    @app.route("/enquiries/<int:enquiry_id>/waiting-on-us", methods=["POST"])
    def enquiry_waiting_on_us(enquiry_id):
        """FX4: Waiting on us, by hand - what the site's waiting action
        writes (firebase_pull._waiting), source manual: on Tracking and on
        the site's Pending on us at once."""
        from .. import firebase_pull
        conn = g.conn
        row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if row is None:
            abort(404)
        back = redirect(url_for("enquiry", enquiry_id=enquiry_id) + "#followups")
        what = " ".join((request.form.get("what") or "").split())
        if not what or len(what) > WAITING_WHAT_MAX:
            warn("What they asked needs 1 to {:,} characters; nothing was logged.".format(WAITING_WHAT_MAX))
            return back
        action = {"date": (request.form.get("at") or "").strip(), "what": what,
                  "who": (request.form.get("who") or "").strip()}
        try:
            said = firebase_pull._waiting(g.repo, row, action, "", _today(), source="manual")
        except firebase_pull.Refused as exc:
            conn.rollback()
            warn(str(exc))
            return back
        flash(said)
        return back

    @app.route("/enquiries/<int:enquiry_id>/answered", methods=["POST"])
    def enquiry_answered(enquiry_id):
        """FX4: Answered, by hand - what the site's answered action writes
        (firebase_pull._answered), source manual; the case leaves Pending
        on us."""
        from .. import firebase_pull
        conn = g.conn
        row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if row is None:
            abort(404)
        action = {"date": (request.form.get("at") or "").strip(),
                  "note": " ".join((request.form.get("note") or "").split())[:ANSWER_NOTE_MAX]}
        who = (row["owner"] or "").strip() or ANSWERED_BY_DEFAULT
        try:
            flash(firebase_pull._answered(g.repo, row, action, who, _today(), source="manual"))
        except firebase_pull.Refused as exc:
            conn.rollback()
            warn(str(exc))
        return redirect(url_for("enquiry", enquiry_id=enquiry_id) + "#followups")

    @app.route("/enquiries/<int:enquiry_id>/events/<int:event_id>",
               methods=["POST"])
    def enquiry_event(enquiry_id, event_id):
        """Correct or strike out one event. Nothing is ever deleted."""
        conn, repo = g.conn, g.repo
        row = conn.execute(
            "SELECT id FROM status_event WHERE id = ? AND enquiry_id = ?",
            (event_id, enquiry_id)).fetchone()
        if row is None:
            abort(404)
        action = request.form.get("action") or "save"
        if action in ("void", "unvoid"):
            notes = repo.void_status_event(event_id, voided=action == "void")
            flash(" ".join(["Event {}.".format("struck out" if action == "void" else "put back")] + notes))
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))

        status = request.form.get("status") or ""
        current = conn.execute("SELECT status FROM status_event WHERE id = ?", (event_id,)).fetchone()["status"]
        # A past Sourcing event keeps its status when its date or note is
        # edited (history stays); nothing new can be set to Sourcing.
        if status not in status_mod.ALL_STATUSES and not (
                status in status_mod.RETIRED_STATUSES and status == current):
            warn("That is not a status.")
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        at = (request.form.get("at") or "").strip()
        if not at:
            warn("An event needs a date.")
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        # the reason (v22) from the vocabulary that fits the event's status
        reason = (request.form.get("reason") or "").strip() or None
        kind = conn.execute("SELECT kind FROM status_event WHERE id = ?", (event_id,)).fetchone()["kind"]
        allowed = status_mod.event_reason_list(conn, status, kind)
        if reason and not allowed:
            reason = None
        elif reason and reason.casefold() not in {
                status_mod.reason_shape(r).casefold() for r in allowed} \
                and status_mod.reason_shape(reason).casefold() not in {
                    status_mod.reason_shape(r).casefold() for r in allowed}:
            warn("'{}' is not a {} reason.".format(reason, status))
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        closing_lost = repo.lost_closing_event(enquiry_id) == event_id
        changed = repo.edit_status_event(event_id, {
            "at": at, "status": status,
            "note": (request.form.get("note") or "").strip() or None,
            "reason": reason, "approx": 0,
        })
        # FX1: the Edit of the event that closes a Lost case carries the
        # case's two prices, written in this same action (an emptied box
        # clears its price); a Save that takes the event away from Lost
        # leaves them alone
        cut = []
        if closing_lost and status == "Lost" and any(n in request.form for n, _ in status_mod.LOST_PRICES):
            prices, cut = status_mod.lost_prices(request.form.get, keep_blank=True)
            written = repo.update("enquiry", enquiry_id, prices)
            if written:
                changed = (changed or 0) + written
                repo.touch("enquiry", enquiry_id)
        said = "Event updated" if changed else "Nothing to change"
        say(" ".join(filter(None, (said, status_mod.prices_cut_text(cut)))), done=bool(changed))
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    @app.route("/enquiries/<int:enquiry_id>/duplicate", methods=["POST"])
    def enquiry_duplicate(enquiry_id):
        """Mark this case as the same request as another one."""
        conn, repo = g.conn, g.repo
        wanted = (request.form.get("of_opp_no") or "").strip().upper()
        at = (request.form.get("at") or "").strip() or _today()
        refusal = repo.closing_refusal(enquiry_id, "Dropped", at)
        if refusal:
            warn(refusal)
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        if request.form.get("action") == "error":
            repo.mark_registered_in_error(
                enquiry_id, at, note=(request.form.get("note") or "").strip()
                or None)
            flash("Marked as registered in error")
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))

        row = conn.execute("SELECT id FROM enquiry WHERE opp_no = ?",
                           (wanted,)).fetchone()
        if row is None:
            warn("No case {}.".format(wanted or "(none given)"))
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        if row["id"] == enquiry_id:
            warn("A case cannot be a duplicate of itself.")
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        repo.mark_duplicate(enquiry_id, row["id"], at,
                            note=(request.form.get("note") or "").strip() or None)
        flash("Marked as a duplicate of {}".format(wanted))
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    # ------------------------------------------- MF1: Who asked? from mail --
    def _who_back(enquiry_id):
        target = (request.form.get("back") or "").strip()
        return redirect(_safe_next(target) if target else url_for("enquiry", enquiry_id=enquiry_id) + "#request")

    def _who_case(enquiry_id):
        """The (TBC) case a Who asked? button names, or None with a warning."""
        from .. import direct_customers_out as dco
        row = g.conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if row is None:
            abort(404)
        if not dco.is_tbc(g.conn, row["requester_party_id"]):
            warn("{} already has its requester ({}).".format(row["opp_no"], row["requester_key"] or "?"))
            return None
        return row

    @app.route("/enquiries/<int:enquiry_id>/who-asked/use", methods=["POST"])
    def enquiry_who_asked_use(enquiry_id):
        """Use as requester: through the repository (source manual), the
        note 'Requester from mail: <key> (<evidence>)'; scope is not
        touched (SC1)."""
        conn, repo = g.conn, g.repo
        row = _who_case(enquiry_id)
        if row is None:
            return _who_back(enquiry_id)
        party = conn.execute("SELECT id, key, merged_into, placeholder FROM party WHERE id = ?",
                             (request.form.get("party_id", type=int),)).fetchone()
        if party is None or party["merged_into"] is not None or party["placeholder"]:
            warn("That party cannot be the requester.")
            return _who_back(enquiry_id)
        evidence = " ".join((request.form.get("evidence") or "").split())[:300]
        repo.save_enquiry(enquiry_id, {"requester_party_id": party["id"]})
        repo.add_note(enquiry_id, _today(), "Requester from mail: {}{}".format(
            party["key"], " ({})".format(evidence) if evidence else ""))
        attached = ""
        if request.form.get("attach") == "1":
            # RG2b (2): the thread of the mail From Outlook read, with it
            subject = (request.form.get("subject") or "").strip()[:300]
            added = []
            for kind, value in (("conversation", (request.form.get("conversation_id") or "").strip()),
                                ("thread_key", parsing.subject_thread_key(subject) if subject else "")):
                if value and repo.attach_thread(enquiry_id, kind, value, _today(), note=subject):
                    added.append(kind)
            attached = " The thread is attached." if added else " The thread was attached already."
        _who_outlook_forget(enquiry_id)
        flash("{}: the requester is now {}.{}".format(row["opp_no"], party["key"], attached))
        return _who_back(enquiry_id)

    @app.route("/enquiries/<int:enquiry_id>/who-asked/from-outlook", methods=["POST"])
    def enquiry_who_asked_outlook(enquiry_id):
        """RG2b (2): read the mail selected in Outlook with the register
        reader and offer what it finds; kept in the session for the page.
        Nothing is written to the case until she clicks."""
        conn = g.conn
        row = _who_case(enquiry_id)
        if row is None:
            return _who_back(enquiry_id)
        try:
            mail = outlook_bridge.read_selection()
        except outlook_bridge.OutlookError as exc:
            logs.get("web").info("who asked from outlook refused: %s", exc.detail)
            warn(exc.message)
            return _who_back(enquiry_id)
        from .. import who_asked
        found = who_asked.from_outlook(conn, row, mail)
        if not found:
            _who_outlook_forget(enquiry_id)
            warn("The mail selected in Outlook ({}) does not say who asked.".format(
                " ".join((mail.subject or "no subject").split())[:120]))
            return _who_back(enquiry_id)
        # one case's result at a time: the cookie stays small
        kept = {str(enquiry_id): {"cands": [who_asked.candidate_state(c) for c in found],
                                  "conversation_id": (mail.conversation_id or "")[:200],
                                  "subject": (mail.subject or "")[:300]}}
        session[WHO_OUTLOOK_KEY] = kept
        return _who_back(enquiry_id)

    @app.route("/enquiries/<int:enquiry_id>/who-asked/duplicate", methods=["POST"])
    def enquiry_who_asked_duplicate(enquiry_id):
        """Duplicate of OPP-nnnn: Dropped with that reason, as the Dropped
        reasons setting writes it (Repository.mark_duplicate)."""
        conn, repo = g.conn, g.repo
        row = _who_case(enquiry_id)
        if row is None:
            return _who_back(enquiry_id)
        other = conn.execute("SELECT id, opp_no FROM enquiry WHERE id = ?",
                             (request.form.get("of_id", type=int),)).fetchone()
        if other is None or other["id"] == enquiry_id:
            warn("No such case to be a duplicate of.")
            return _who_back(enquiry_id)
        at = _today()
        refusal = repo.closing_refusal(enquiry_id, "Dropped", at)
        if refusal:
            warn(refusal)
            return _who_back(enquiry_id)
        repo.mark_duplicate(enquiry_id, other["id"], at)
        flash("{}: marked as a duplicate of {}.".format(row["opp_no"], other["opp_no"]))
        return _who_back(enquiry_id)

    @app.route("/enquiries/<int:enquiry_id>/who-asked/add-party", methods=["POST"])
    def enquiry_who_asked_add_party(enquiry_id):
        """Add party for an address with none: the party, its domain (a
        mailbox provider's address becomes a contact instead). The
        requester is not set - Use as requester then does that."""
        conn, repo = g.conn, g.repo
        row = _who_case(enquiry_id)
        if row is None:
            return _who_back(enquiry_id)
        key = " ".join((request.form.get("key") or "").split())
        address = (request.form.get("address") or "").strip().lower()
        office = (request.form.get("office") or "").strip().upper()
        if office and re.fullmatch(r"[A-Z]{2,4}", office):
            # RG2b (3): one of our offices not on file yet - 13g's party, no domain
            key = branding.office_key(office, conn)
            repo.add_party(key, type="Office", country="Unknown",
                           notes="Added from Who asked? on {}.".format(row["opp_no"]))
            flash("Party {} added. Use as requester sets it on {}.".format(key, row["opp_no"]))
            return _who_back(enquiry_id)
        kind = request.form.get("type") or "Agent"
        if kind not in WHO_ASKED_TYPES:
            kind = "Agent"
        domain = matching.domain_of(address) or (request.form.get("domain") or "").strip().lower()
        if address.startswith("@"):
            address = ""
        if not key or not domain or db.is_internal_domain(conn, domain):
            warn("A name and an outside address are needed to add a party.")
            return _who_back(enquiry_id)
        country = (request.form.get("country") or "").strip()
        if not country or conn.execute("SELECT 1 FROM country WHERE name = ?", (country,)).fetchone() is None:
            country = "Unknown"
        party_id = repo.add_party(key, type=kind, country=country,
                                  notes="Added from Who asked? on {} ({}).".format(row["opp_no"], address))
        if matching.is_free_mail(domain):
            if conn.execute("SELECT 1 FROM contact WHERE party_id = ? AND lower(trim(email)) = ?",
                            (party_id, address)).fetchone() is None:
                repo.insert("contact", {"party_id": party_id, "email": address})
        else:
            matching.remember_domain(repo, party_id, domain)
        flash("Party {} added. Use as requester sets it on {}.".format(key, row["opp_no"]))
        return _who_back(enquiry_id)

    @app.route("/enquiries/<int:enquiry_id>/vendors", methods=["POST"])
    def enquiry_vendors(enquiry_id):
        conn, repo = g.conn, g.repo
        vendor_request_id = request.form.get("vendor_request_id", type=int)
        action = request.form.get("action") or "save"
        if vendor_request_id and conn.execute(
                "SELECT 1 FROM vendor_request WHERE id = ? AND enquiry_id = ?",
                (vendor_request_id, enquiry_id)).fetchone() is None:
            abort(404)
        if action == "award":
            # The awarded vendor is the agent whose rate was used.
            repo.set_rate_used(enquiry_id, "agent", vendor_request_id=vendor_request_id)
            flash("Awarded")
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        if action == "unaward":
            # OP1: the flag and the case's Rate used cleared, one action
            vendor = repo.remove_award(enquiry_id, vendor_request_id)
            if vendor is None:
                warn("That vendor request is not the awarded one; nothing changed.")
            else:
                flash("Award removed: {}. Rate used is not answered.".format(vendor))
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        if action in ("void", "unvoid"):
            notes = repo.void_vendor_request(vendor_request_id, voided=action == "void")
            flash(" ".join(["Vendor request {}.".format(
                "struck out" if action == "void" else "put back")] + notes))
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))

        # no 'quoted' here: the repository derives it from machine_status
        # (one vendor status, not two - 2026-09-25)
        values = {
            "asked_on": (request.form.get("asked_on") or "").strip() or None,
            "replied_on": (request.form.get("replied_on") or "").strip() or None,
            "their_ref": (request.form.get("their_ref") or "").strip() or None,
            "notes": (request.form.get("notes") or "").strip() or None,
        }
        if "machine_status" in request.form:
            values["machine_status"] = (request.form.get("machine_status") or "").strip() or None
        if "routes_present" in request.form:
            values["route_ids"] = _posted_route_ids(request.form, enquiry_id)
        if "parts_present" in request.form:
            # Q2b: what the vendor was asked to quote, in the fixed order
            from .. import parts as parts_mod
            values["parts"] = parts_mod.join(request.form.getlist("part"))
        if vendor_request_id:
            changed = repo.update("vendor_request", vendor_request_id, values)
            repo.touch("vendor_request", vendor_request_id)
            say("Vendor request updated" if changed else "Nothing to change", done=bool(changed))
            if _save_first():
                return jsonify(ok=True)
        else:
            party_id = request.form.get("vendor_party_id", type=int)
            new_key = (request.form.get("new_vendor_key") or "").strip()
            if new_key:
                party_id = repo.add_party(new_key, notes="Added as a vendor.")
            if not party_id:
                return _refuse("Pick a vendor first.", redirect(url_for("enquiry", enquiry_id=enquiry_id)), "vendor_party_id", "Vendor")
            repo.insert("vendor_request", dict(
                values, enquiry_id=enquiry_id, vendor_party_id=party_id,
                source="manual"))
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    # ------------------------------------------------------ 4.4 parties --
    @app.route("/enquiries/<int:enquiry_id>/vendors/from-outlook", methods=["POST"])
    def enquiry_vendors_from_outlook(enquiry_id):
        """Log the rate request selected in Outlook as this case's vendor
        requests: one per outside domain we wrote to."""
        conn, repo = g.conn, g.repo
        mail = None
        anyway = request.form.get("anyway") == "1"
        if (request.form.get("message_id") or "").strip():
            # the Mail card's 'Log vendor request': the mail is an archive
            # row, not the Outlook selection (2026-09-25)
            mail = _archived_mail(request.form.get("message_id"))
            if mail is None:
                warn("That mail is not in the archive any more.")
                return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        if mail is None:
            try:
                mail = outlook_bridge.read_selection()
            except outlook_bridge.OutlookError as exc:
                logs.get("web").info("outlook selection refused: %s", exc.detail)
                warn(exc.message)
                return redirect(url_for("enquiry", enquiry_id=enquiry_id))
            if anyway and (request.form.get("entry_id") or "") != (mail.entry_id or ""):
                # Log anyway is for the mail she was shown, not whatever is selected now
                warn("The selection in Outlook changed since; select that mail again and log it - nothing was logged.")
                return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        case = conn.execute("SELECT requester_party_id FROM enquiry WHERE id = ?",
                            (enquiry_id,)).fetchone()
        from .. import propose
        requester_domains = propose.requester_domains_of(
            conn, case["requester_party_id"]) if case is not None else set()
        # F1: Log anyway reads the mail as us asking the people outside on it
        # (the requester's domain still left out, the same duplicate checks)
        draft = intake.build(conn, mail, case_requester_domains=requester_domains, force_asking=anyway)
        if not draft.vendors:
            outside = intake._outside_recipients(mail, intake._internal_domains(conn, mail))
            offer = not anyway and (bool(draft.vendor_thread) or (
                not draft.vendor_note and (draft.direction == "quoting" or outside)))
            if draft.vendor_thread:
                # D1 (1b): the vendor's own reply in a thread we started
                text = "{}'s reply in a thread we started{} - nothing was logged.".format(
                    draft.vendor_thread["key"], " on " + dates.month_day(draft.vendor_thread["asked_on"])
                    if draft.vendor_thread.get("asked_on") else "")
            elif draft.vendor_note:
                text = "{} - nothing was logged.".format(draft.vendor_note)
            elif draft.direction == "quoting":
                text = "Reads as us giving rates ({}) - nothing was logged.".format(
                    draft.direction_words or "our own amounts")
            else:
                text = "The selected mail is not one we sent out asking a vendor for a rate - nothing was logged."
            if offer:
                fields = {"anyway": "1"}
                if (request.form.get("message_id") or "").strip():
                    fields["message_id"] = request.form.get("message_id").strip()
                else:
                    fields["entry_id"] = mail.entry_id or ""
                warn({"text": text, "action": {
                    "url": url_for("enquiry_vendors_from_outlook", enquiry_id=enquiry_id),
                    "label": TEXT["log_anyway"], "fields": fields}})
            else:
                warn(text)
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        asked_on = (mail.received_on or "")[:10] or dt.date.today().isoformat()
        if draft.vendor_thread and draft.vendor_thread.get("asked_on"):
            # D1 (1b): the request is our first mail in the thread
            asked_on = draft.vendor_thread["asked_on"]
        logged, already, left = [], [], []
        seen = set()
        for vendor in draft.vendors:
            if not vendor.get("checked", True):
                left.append(vendor["key"])            # wrote earlier in the thread
                continue
            party_id = _vendor_party(repo, vendor["domain"], vendor["key"],
                                     vendor.get("party_id"))
            if not party_id or party_id in seen:      # a second domain of one company
                continue
            seen.add(party_id)
            exists = conn.execute(
                "SELECT 1 FROM vendor_request WHERE enquiry_id = ? "
                "  AND vendor_party_id = ? AND asked_on = ? AND voided = 0",   # FX2: a struck-out one is not on the case
                (enquiry_id, party_id, asked_on)).fetchone()
            if exists:
                already.append(vendor["key"])
                continue
            repo.insert("vendor_request", {
                "enquiry_id": enquiry_id, "vendor_party_id": party_id,
                "asked_on": asked_on, "source": "manual",
                "notes": "Logged from Outlook: {}".format((mail.subject or "")[:120]),
                # v23: the mail's thread, so the Mail card can show it
                "conversation_id": (mail.conversation_id or "").strip() or None,
                "thread_key": parsing.subject_thread_key(mail.subject or "") or None,
            })
            logged.append(vendor["key"])
        repo.commit()
        parts = []
        if logged:
            parts.append("Logged {}: {}".format(
                "1 vendor request" if len(logged) == 1
                else "{} vendor requests".format(len(logged)), ", ".join(logged)))
        if already:
            parts.append("already on the case: {}".format(", ".join(already)))
        if left:
            parts.append("not logged, they wrote earlier in the thread: {}"
                         .format(", ".join(left)))
        if draft.vendor_note:
            parts.append(draft.vendor_note)
        say("; ".join(parts) if parts else "Nothing to log.", done=bool(logged))
        logs.get("web").info("vendor requests from outlook: case %s logged=%s "
                             "already=%s left=%s", enquiry_id, logged, already, left)
        return redirect(url_for("enquiry", enquiry_id=enquiry_id))

    @app.route("/parties")
    def parties():
        conn = g.conn
        where = []
        args = []
        for field, column in (("type", "type"), ("region", "region"),
                              ("stage", "stage")):
            value = request.args.get(field)
            if value:
                where.append("{} = ?".format(column))
                args.append(value)
        if request.args.get("mfn") == "1":
            where.append("mfn_member = 1")
        query = (request.args.get("q") or "").strip()
        if query:
            like = "%{}%".format(query)
            # aliases too (13e): a merged party's old name is one of them
            where.append(
                "(key LIKE ? OR full_name LIKE ? OR aliases LIKE ? OR id IN "
                "(SELECT party_id FROM party_domain WHERE domain LIKE ?) "
                "OR id IN (SELECT party_id FROM contact "
                "          WHERE email LIKE ? OR name LIKE ?))")
            args.extend([like] * 6)

        group_key = (request.args.get("group") or "").strip().upper()
        if group_key:
            where.append("group_id IN (SELECT id FROM party_group WHERE key = ?)")
            args.append(group_key)
        where.append("merged_into IS NULL")
        sql = ("SELECT v.*, (SELECT g.key FROM party_group g WHERE g.id = v.group_id) AS group_key "
               "FROM v_party_stats v")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sort, direction = sort_choice(conn, "parties")
        sql += order_by("parties", sort, direction, tie="key COLLATE NOCASE")
        rows = conn.execute(sql, args).fetchall()
        cadence_default = int(db.get_setting(conn, "cadence_days_default", "45"))
        return render_template(
            "parties.html", rows=rows, filters=request.args, query=query,
            sort=sort, dir=direction,
            cadence_default=cadence_default, today=_today(),
            groups=conn.execute("SELECT key, name FROM party_group ORDER BY key").fetchall(),
            regions=[r["region"] for r in conn.execute(
                "SELECT DISTINCT region FROM country WHERE region IS NOT NULL "
                "ORDER BY region")])

    @app.route("/parties/duplicates")
    def parties_duplicates():
        return render_template("parties_duplicates.html",
                               pairs=party_dupes.find_duplicates(g.conn))

    @app.route("/parties/merge", methods=["POST"])
    def parties_merge():
        """From the duplicates page (keep_id + drop_id), or from a party page's
        'Merge into' box (party_id + other_key + keep=this|other)."""
        keep_id = request.form.get("keep_id", type=int)
        drop_id = request.form.get("drop_id", type=int)
        party_id = request.form.get("party_id", type=int)
        back = url_for("parties_duplicates")
        if party_id and request.form.get("other_key") is not None:
            back = url_for("party", party_id=party_id)
            other_key = (request.form.get("other_key") or "").strip()
            other = g.conn.execute(
                "SELECT id FROM party WHERE lower(trim(key)) = ? AND merged_into IS NULL",
                (other_key.casefold(),)).fetchone() if other_key else None
            if other is None:
                warn("No party called '{}' - pick one from the list.".format(other_key))
                return redirect(back)
            if other["id"] == party_id:
                warn("That is this party; pick another one.")
                return redirect(back)
            if request.form.get("keep") == "other":
                keep_id, drop_id = other["id"], party_id
            else:
                keep_id, drop_id = party_id, other["id"]
        if not keep_id or not drop_id:
            abort(400)
        try:
            moved = g.repo.merge_party(keep_id, drop_id, today=_today())
        except ValueError as exc:
            warn(str(exc))
            return redirect(back)
        keep = g.conn.execute("SELECT key FROM party WHERE id = ?", (keep_id,)).fetchone()
        flash("Merged into {}: {}".format(keep["key"], ", ".join(
            "{} {}".format(n, k.split(".")[0]) for k, n in moved.items() if n) or "nothing to move"))
        logs.get("web").info("party merge: %s -> %s moved=%s", drop_id, keep_id, moved)
        return redirect(url_for("party", party_id=keep_id))

    @app.route("/parties/<int:party_id>", methods=["GET", "POST"])
    def party(party_id):
        conn, repo = g.conn, g.repo
        if request.method == "POST":
            values = {
                "key": (request.form.get("key") or "").strip(),
                "full_name": (request.form.get("full_name") or "").strip() or None,
                "type": request.form.get("type"),
                "country": request.form.get("country") or "Unknown",
                "city": (request.form.get("city") or "").strip() or None,
                "mfn_member": 1 if request.form.get("mfn_member") else None,
                "mfn_no": (request.form.get("mfn_no") or "").strip() or None,
                "stage": request.form.get("stage") or "active",
                "cadence_days": request.form.get("cadence_days", type=int),
                "notes": (request.form.get("notes") or "").strip() or None,
                "roles": "|".join(r for r in PARTY_ROLES if r in request.form.getlist("roles")),
            }
            # the group: an existing key, a new one typed in, or none
            new_group = (request.form.get("new_group") or "").strip()
            chosen = (request.form.get("group_key") or "").strip().upper()
            if new_group:
                values["group_id"] = repo.add_party_group(new_group)
            elif chosen:
                row = conn.execute("SELECT id FROM party_group WHERE key = ?", (chosen,)).fetchone()
                values["group_id"] = row["id"] if row else repo.add_party_group(chosen)
            else:
                values["group_id"] = None
            repo.update("party", party_id, values)
            repo.touch("party", party_id)
            flash(TEXT["saved"])
            return redirect(url_for("party", party_id=party_id))

        row = conn.execute("SELECT * FROM v_party_stats WHERE id = ?",
                           (party_id,)).fetchone()
        if row is None:
            abort(404)
        cadence_default = int(db.get_setting(conn, "cadence_days_default", "45"))
        merged_into = None
        if row["merged_into"]:
            merged_into = conn.execute("SELECT id, key FROM party WHERE id = ?",
                                       (row["merged_into"],)).fetchone()
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except ValueError:
            aliases = []
        suggested_name = ""
        if intake.looks_like_domain_stem(row["key"]):
            candidate = intake.name_from_stem(row["key"])
            if candidate and candidate != row["key"]:
                suggested_name = candidate
        group = None
        if row["group_id"]:
            group = conn.execute("SELECT * FROM party_group WHERE id = ?", (row["group_id"],)).fetchone()
        return render_template(
            "party.html", row=row, cadence_default=cadence_default,
            today=_today(), merged_into=merged_into, aliases=aliases,
            suggested_name=suggested_name,
            roles=PARTY_ROLES, party_roles=[r for r in (row["roles"] or "").split("|") if r],
            group=group,
            groups=conn.execute("SELECT key, name FROM party_group ORDER BY key").fetchall(),
            group_offices=conn.execute(
                "SELECT id, key, country, type FROM party WHERE group_id = ? AND id <> ? "
                "AND merged_into IS NULL ORDER BY key COLLATE NOCASE",
                (row["group_id"], party_id)).fetchall() if row["group_id"] else [],
            merge_keys=[r["key"] for r in conn.execute(
                "SELECT key FROM party WHERE merged_into IS NULL AND id <> ? "
                "ORDER BY key COLLATE NOCASE", (party_id,))],
            domains=conn.execute(
                "SELECT domain FROM party_domain WHERE party_id = ? ORDER BY domain",
                (party_id,)).fetchall(),
            contacts=conn.execute(
                "SELECT * FROM contact WHERE party_id = ? "
                "ORDER BY is_primary DESC, id", (party_id,)).fetchall(),
            enquiries=conn.execute(
                "SELECT * FROM v_enquiry WHERE requester_party_id = ? "
                "ORDER BY received_on DESC LIMIT 200", (party_id,)).fetchall(),
            vendor_requests=conn.execute(
                "SELECT v.*, e.opp_no FROM vendor_request v "
                "JOIN enquiry e ON e.id = v.enquiry_id "
                "WHERE v.vendor_party_id = ? ORDER BY v.asked_on DESC LIMIT 200",
                (party_id,)).fetchall(),
            timeline=_timeline(conn, party_id),
            **reference(conn))

    @app.route("/parties/<int:party_id>/use-suggested-name", methods=["POST"])
    def party_use_suggested_name(party_id):
        """Take the name offered on the page; the old key becomes an alias."""
        new_key = (request.form.get("key") or "").strip()
        if g.repo.rename_party(party_id, new_key):
            flash("Renamed to {}; the old key is kept as an alias.".format(new_key))
        else:
            warn("Could not rename: the name is empty, unchanged, or already in use.")
        return redirect(url_for("party", party_id=party_id))

    @app.route("/parties/<int:party_id>/events", methods=["POST"])
    def party_events(party_id):
        repo = g.repo
        repo.insert("party_event", {
            "party_id": party_id,
            "at": request.form.get("at") or _today(),
            "type": request.form.get("type") or "note",
            "summary": (request.form.get("summary") or "").strip() or None,
            "source": "manual",
        })
        flash("Event added")
        return redirect(url_for("party", party_id=party_id))

    @app.route("/parties/<int:party_id>/domains/move", methods=["POST"])
    def party_domain_move(party_id):
        """D1: a domain remembered on the wrong party goes to the right one -
        the row's party changes, nothing is deleted, change_log has it."""
        conn, repo = g.conn, g.repo
        domain = (request.form.get("domain") or "").strip().lower()
        other_key = (request.form.get("other_key") or "").strip()
        back = redirect(url_for("party", party_id=party_id))
        row = conn.execute("SELECT party_id FROM party_domain WHERE domain = ?", (domain,)).fetchone()
        if row is None or row["party_id"] != party_id:
            abort(404)
        other = conn.execute("SELECT id, key FROM party WHERE lower(trim(key)) = ? AND merged_into IS NULL",
                             (other_key.casefold(),)).fetchone() if other_key else None
        if other is None:
            warn("No party called '{}' - pick one from the list.".format(other_key))
            return back
        if other["id"] == party_id:
            warn("{} is already this party's.".format(domain))
            return back
        repo.move_domain(domain, other["id"])
        here = conn.execute("SELECT key FROM party WHERE id = ?", (party_id,)).fetchone()["key"]
        flash("Moved {} from {} to {}.".format(domain, here, other["key"]))
        return back

    @app.route("/parties/<int:party_id>/contacts", methods=["POST"])
    def party_contacts(party_id):
        repo = g.repo
        email = (request.form.get("email") or "").strip() or None
        repo.insert("contact", {
            "party_id": party_id,
            "name": (request.form.get("name") or "").strip() or None,
            "email": email,
            "title": (request.form.get("title") or "").strip() or None,
        })
        if email and "@" in email:
            repo.attach_domain(party_id, email.split("@", 1)[1])   # not ours (13g)
        flash("Contact added")
        return redirect(url_for("party", party_id=party_id))

    # ---------------------------------------------------------- the api --
    @app.route("/api/dedupe", methods=["POST"])
    def api_dedupe():
        """Used by the form to show the panel before anything is written."""
        values, _ = forms.read(request.form, _tags(request.form))
        matches = _dedupe_matches(g.conn, values,
                                  request.form.get("id", type=int))
        return json.dumps([{
            "id": m.enquiry_id, "opp_no": m.opp_no, "score": m.score,
            "band": m.band, "reasons": m.reasons,
            "received_on": m.row["received_on"],
            "requester": m.row["requester_key"],
            "commodity": m.row["commodity"] or "",
        } for m in matches]), 200, {"Content-Type": "application/json"}

    # ------------------------------------------- 4.7 locations (master) --
    LOCATION_TYPES = ("city", "airport", "seaport", "ramp")
    LOCATION_STATUSES = ("active", "to_confirm", "retired")

    def _location_form(form) -> dict:
        """The master_location columns as the form sends them."""
        def text(name):
            return (form.get(name) or "").strip() or None

        # lat / lon (5c): both or neither, within range; anything else is refused
        lat, lon = text("lat"), text("lon")
        if bool(lat) != bool(lon):
            raise ValueError(TEXT["location_latlon_both"])
        coords = {}
        if lat and lon:
            try:
                lat_v, lon_v = float(lat), float(lon)
            except ValueError:
                raise ValueError(TEXT["location_latlon_range"])
            if not (-90 <= lat_v <= 90 and -180 <= lon_v <= 180):
                raise ValueError(TEXT["location_latlon_range"])
            coords = {"lat": round(lat_v, 4), "lon": round(lon_v, 4)}
        elif "lat" in form:
            coords = {"lat": None, "lon": None}
        return {
            **coords,
            "name": text("name"),
            "type": form.get("type") if form.get("type") in LOCATION_TYPES else "city",
            "country": form.get("country") or "Unknown",
            "subdivision": (text("subdivision") or "").upper() or None,
            "unlocode": (text("unlocode") or "").upper() or None,
            "iata": (text("iata") or "").upper() or None,
            "is_seaport": 1 if form.get("is_seaport") else 0,
            "is_airport": 1 if form.get("is_airport") else 0,
            "status": form.get("status") if form.get("status") in LOCATION_STATUSES else "active",
            "note": text("note"),
            "no_parent": 1 if form.get("no_parent") else 0,
        }

    def _location_refs(conn, form) -> dict:
        """parent_id / area_id from the keys typed in the form; '' clears."""
        out = {}
        parent = (form.get("parent_key") or "").strip()
        if "parent_key" in form:
            row = db.find_master_location(conn, parent) if parent else None
            out["parent_id"] = row["id"] if row is not None else None
            if parent and row is None:
                out["parent_unknown"] = parent
        area = (form.get("area_key") or "").strip().upper()
        if "area_key" in form:
            row = conn.execute("SELECT id FROM master_area WHERE key = ?", (area,)).fetchone()                 if area else None
            out["area_id"] = row["id"] if row is not None else None
        return out

    def _location_parent_errors(values) -> dict:
        """RD1: a city has no parent and no box; a gateway names its parent
        city or says why it has none. Refused in the form, nothing saved."""
        unknown = values.pop("parent_unknown", None)
        if values.get("type") == "city":
            values["parent_id"], values["no_parent"] = None, 0
            return {}
        if unknown:
            return {"parent_key": TEXT["location_parent_unknown"].format(unknown.upper())}
        if values.get("parent_id"):
            values["no_parent"] = 0
        problem = db.gateway_parent_problem(values)
        return {"parent_key": problem} if problem else {}

    def _location_lists(conn) -> dict:
        return {
            "types": LOCATION_TYPES, "statuses": LOCATION_STATUSES,
            "countries": conn.execute(
                "SELECT name FROM country ORDER BY (name = 'Unknown') DESC, name").fetchall(),
            "areas": conn.execute("SELECT key, name FROM master_area ORDER BY key").fetchall(),
            "parent_keys": [r["key"] for r in conn.execute(
                "SELECT key FROM master_location WHERE type = 'city' AND status <> 'retired' "
                "ORDER BY key")],
        }

    @app.route("/locations")
    def locations():
        conn = g.conn
        if not db.master_active(conn):
            return render_template("locations.html", rows=None, query="")
        query = (request.args.get("q") or "").strip()
        missing = request.args.get("missing") == "1"
        where, args = [], []
        if missing:
            # the gaps fill-coordinates left (5c): live rows without lat / lon
            where.append("l.status <> 'retired' AND (l.lat IS NULL OR l.lon IS NULL)")
        if query:
            like = "%{}%".format(query)
            where.append("(l.key LIKE ? OR l.name LIKE ? OR l.id IN "
                         "(SELECT location_id FROM master_location_alias WHERE alias LIKE ?) "
                         "OR a.key LIKE ? OR a.name LIKE ?)")
            args.extend([like] * 5)
        sql = ("SELECT l.*, p.key AS parent_key, a.key AS area_key, "
               "(SELECT group_concat(alias, ', ') FROM (SELECT alias FROM master_location_alias x "
               " WHERE x.location_id = l.id ORDER BY alias)) AS aliases, "
               "(SELECT COUNT(*) FROM enquiry e WHERE e.origin_loc_id = l.id OR e.dest_loc_id = l.id) AS used "
               "FROM master_location l LEFT JOIN master_location p ON p.id = l.parent_id "
               "LEFT JOIN master_area a ON a.id = l.area_id")
        if where:
            sql += " WHERE " + " AND ".join(where)
        # to_confirm first, and among them the ones a person was asked to
        # decide (note 'DECIDE: ...') before the rest
        sql += (" ORDER BY (l.status = 'to_confirm') DESC, (l.status = 'retired'), "
                "(l.note LIKE 'DECIDE:%') DESC, l.key")
        missing_count = conn.execute(
            "SELECT COUNT(*) FROM master_location WHERE status <> 'retired' AND (lat IS NULL OR lon IS NULL)"
        ).fetchone()[0]
        return render_template("locations.html", rows=conn.execute(sql, args).fetchall(),
                               query=query, missing=missing, missing_count=missing_count)

    @app.route("/locations/new", methods=["GET", "POST"])
    def location_new():
        conn, repo = g.conn, g.repo
        if not db.master_active(conn):
            warn(TEXT["locations_not_active"])
            return redirect(url_for("locations"))
        if request.method == "POST":
            text = (request.form.get("key") or "").strip()
            if not text:
                warn("A location needs a name.")
                return redirect(url_for("location_new"))
            found = db.find_master_location(conn, text)
            if found is not None:
                warn(TEXT["location_exists"].format(found["key"]))
                return redirect(url_for("location", location_id=found["id"]))
            try:
                values = _location_form(request.form)
            except ValueError as exc:
                warn(str(exc))
                return redirect(url_for("location_new"))
            values.update(_location_refs(conn, request.form))
            errors = _location_parent_errors(values)
            rules = db.place_rules(conn, text, values.get("country"), values.get("type") or "city")
            if rules["error"]:
                errors = dict(errors, key="; ".join(rules["notes"] + [rules["error"]]))
            if errors:
                warn(next(iter(errors.values())))
                return render_template("location.html", row=None, aliases=[], used=[],
                                       draft=request.form, errors=errors, **_location_lists(conn))
            row = repo.add_master_location(text, source="manual", **values)
            flash(TEXT["location_saved"])
            return redirect(url_for("location", location_id=row["id"]))
        return render_template("location.html", row=None, aliases=[], used=[],
                               **_location_lists(conn))

    @app.route("/locations/<int:location_id>", methods=["GET", "POST"])
    def location(location_id):
        conn, repo = g.conn, g.repo
        row = conn.execute("SELECT * FROM master_location WHERE id = ?", (location_id,)).fetchone()
        if row is None:
            abort(404)
        if request.method == "POST":
            try:
                values = _location_form(request.form)
            except ValueError as exc:
                warn(str(exc))
                return redirect(url_for("location", location_id=location_id))
            values.update(_location_refs(conn, request.form))
            if values.get("parent_id") == location_id:
                values["parent_id"] = None
            errors = _location_parent_errors(values)
            if errors:
                warn(next(iter(errors.values())))
                return _location_page(conn, row, draft=request.form, errors=errors)
            key = db.location_key(request.form.get("key") or row["key"])
            if key and key != row["key"]:
                other = conn.execute("SELECT id FROM master_location WHERE key = ? AND id <> ?",
                                     (key, location_id)).fetchone()
                if other is not None:
                    warn(TEXT["location_key_taken"].format(key))
                    return redirect(url_for("location", location_id=location_id))
                values["key"] = key
            if repo.update("master_location", location_id, values):
                repo.touch("master_location", location_id)
                if "key" in values:
                    # the cases' text column follows the key (derived from the id)
                    for side in ("origin", "dest"):
                        for case in conn.execute(
                                "SELECT id FROM enquiry WHERE {}_loc_id = ?".format(side),
                                (location_id,)).fetchall():
                            repo.update("enquiry", case["id"], {"{}_loc_id".format(side): location_id})
                    # batch 23: and every route's (its places, the pod copy)
                    repo.routes_follow_location(location_id)
            flash(TEXT["location_saved"])
            return redirect(url_for("location", location_id=location_id))
        return _location_page(conn, row)

    def _location_page(conn, row, draft=None, errors=None):
        location_id = row["id"]
        parent = area = None
        if row["parent_id"]:
            parent = conn.execute("SELECT id, key FROM master_location WHERE id = ?",
                                  (row["parent_id"],)).fetchone()
        if row["area_id"]:
            area = conn.execute("SELECT id, key FROM master_area WHERE id = ?",
                                (row["area_id"],)).fetchone()
        return render_template(
            "location.html", row=row, parent=parent, area=area, draft=draft, errors=errors or {},
            aliases=conn.execute("SELECT alias, kind FROM master_location_alias "
                                 "WHERE location_id = ? ORDER BY alias", (location_id,)).fetchall(),
            used=conn.execute(
                "SELECT id, opp_no, origin_loc, dest_loc, status FROM enquiry "
                "WHERE origin_loc_id = ? OR dest_loc_id = ? "
                "ORDER BY CAST(substr(opp_no, 5) AS INTEGER) DESC LIMIT 100",
                (location_id, location_id)).fetchall(),
            children=conn.execute("SELECT id, key, type FROM master_location WHERE parent_id = ? "
                                  "ORDER BY key", (location_id,)).fetchall(),
            merge_keys=[r["key"] for r in conn.execute(
                "SELECT key FROM master_location WHERE status <> 'retired' AND id <> ? ORDER BY key",
                (location_id,))],
            **_location_lists(conn))

    @app.route("/locations/merge", methods=["POST"])
    def locations_merge():
        """Two rows for one place: the other one
        is named by key or alias; 'keep' says which row survives."""
        conn, repo = g.conn, g.repo
        this_id = request.form.get("location_id", type=int)
        other = db.find_master_location(conn, request.form.get("other_key") or "")
        if this_id is None or other is None or other["id"] == this_id:
            warn("Name another location to merge with.")
            return redirect(url_for("location", location_id=this_id) if this_id else url_for("locations"))
        keep_id, drop_id = (this_id, other["id"]) if request.form.get("keep") != "other" \
            else (other["id"], this_id)
        try:
            moved = repo.merge_master_location(keep_id, drop_id)
        except ValueError as exc:
            warn(str(exc))
            return redirect(url_for("location", location_id=this_id))
        flash("Merged: {} alias(es), {} case origin(s), {} case destination(s) moved; the other row is retired.".format(
            moved["aliases"], moved["enquiry.origin_loc_id"], moved["enquiry.dest_loc_id"]))
        return redirect(url_for("location", location_id=keep_id))

    @app.route("/locations/<int:location_id>/aliases", methods=["POST"])
    def location_aliases(location_id):
        conn, repo = g.conn, g.repo
        if conn.execute("SELECT 1 FROM master_location WHERE id = ?", (location_id,)).fetchone() is None:
            abort(404)
        alias = (request.form.get("alias") or "").strip()
        if request.form.get("remove"):
            removed = repo.remove_master_alias(request.form["remove"], location_id)
            say(TEXT["location_alias_removed"] if removed else "No such alias.", done=bool(removed))
        elif alias:
            other = db.find_master_location(conn, alias)
            if other is not None and other["id"] != location_id and other["key"] == db.location_key(alias):
                warn(TEXT["location_key_taken"].format(other["key"]))
            else:
                added = repo.add_master_alias(alias, location_id, "manual")
                say(TEXT["location_alias_added"] if added else "That alias is already on this location.",
                    done=bool(added))
        return redirect(url_for("location", location_id=location_id))

    @app.route("/api/locations")
    def api_locations():
        country = request.args.get("country")
        rows = [dict(r) for r in location_options(g.conn)
                if not country or r["country"] == country]
        rows.sort(key=lambda r: (r["status"] != "To Confirm", r["key"]))
        return json.dumps(rows), 200, {"Content-Type": "application/json"}

    @app.route("/api/place-rules")
    def api_place_rules():
        """RD1c: a place added in a picker's small form, asked of the
        master's naming rules at once (db.place_rules) so a refusal shows
        before Register. Reads only: {key, text, notes, error, found}, where
        found is the key of the place on file the text turned out to be."""
        conn = g.conn
        text = " ".join((request.args.get("text") or "").split())
        country = (request.args.get("country") or "").strip()
        out = {"key": db.location_key(text), "text": text.upper(), "notes": [], "error": None, "found": None}
        if not text or not db.master_active(conn):
            return jsonify(out)
        if db.TWO_LETTER_CODE.match(text.upper()) and db.find_master_location(conn, text) is None:
            out["error"] = ("'{}' is a two-letter code - a state or a country. Pick the country and leave "
                            "the location empty, or type the place's name.".format(text.upper()))
            return jsonify(out)
        rules = db.place_rules(conn, text, country if country not in ("", "Unknown") else None)
        found = rules["found"]
        out.update(notes=rules["notes"], error=rules["error"], text=rules["text"].upper(),
                   key=found["key"] if found is not None else db.location_key(rules["text"]),
                   found=found["key"] if found is not None else None)
        return jsonify(out)

    # ------------------------------------------------------ 4.5 tracking --
    @app.route("/tracking")
    def tracking_page():
        # v2 (2026-09-24): run() and the rules as they were; page_model
        # regroups the rows for the sample's layout. Sort, search and the
        # filters are the page's own script, so no ?sort / ?all here.
        conn = g.conn
        today = _today()
        sections = tracking.run(conn, today)
        model = tracking.page_model(conn, today, sections=sections)
        archive = mail_archive.status()
        view = request.args.get("view") if request.args.get("view") in ("today", "backlog", "all") else "today"
        return render_template(
            "tracking.html", model=model, sections=sections, today=today, view=view,
            open_card=(request.args.get("open") or "").strip(),
            limit=tracking.FOCUS_LIMIT, archive=archive,
            scheduled_run=_scheduled_run(app.config["DB_PATH"]),
            statuses=status_mod.ALL_STATUSES, snooze_choices=tracking.SNOOZE_CHOICES,
            **settings_lists(conn))

    @app.route("/tracking/decide-lost", methods=["POST"])
    def tracking_decide_lost():
        """The batch button under R3's Decide rows: every ticked case goes
        Lost, one set_status each, with the No response reason
        for its requester (L2: agent, other office / sales, customer). A
        reason is added to the closed reasons if missing."""
        conn, repo = g.conn, g.repo
        ids = [i for i in request.form.getlist("ids") if str(i).isdigit()]
        at = (request.form.get("at") or "").strip() or _today()
        if not ids:
            warn("Tick the cases to mark Lost first.")
            return redirect(url_for("tracking_page") + "#R3")
        done, skipped = {}, []
        for enquiry_id in ids:
            row = conn.execute("SELECT opp_no, status, requester_type FROM v_enquiry WHERE id = ?",
                               (int(enquiry_id),)).fetchone()
            if row is None or row["status"] != "Quoted":
                skipped.append(row["opp_no"] if row else enquiry_id)
                continue
            reason = status_mod.ensure_closed_reason(conn, status_mod.no_response_reason(row["requester_type"]))
            repo.set_status(int(enquiry_id), "Lost", at, closed_reason=reason)
            done.setdefault(reason, []).append(row["opp_no"])
        repo.commit()
        parts = []
        for reason, opps in done.items():
            parts.append("Marked Lost ({}): {}".format(reason, ", ".join(opps)))
        if skipped:
            parts.append("not Quoted any more, left alone: {}".format(", ".join(map(str, skipped))))
        flash("; ".join(parts))
        logs.get("web").info("decide lost: %s skipped=%s", done, skipped)
        return redirect(url_for("tracking_page") + "#R3")

    @app.route("/tracking/act", methods=["POST"])
    def tracking_act():
        """Every row action. Each one leaves the list and comes straight back.

        The work goes through the same repository calls the case and party
        pages use, so an item dealt with here is dealt with identically.
        """
        conn, repo = g.conn, g.repo
        do = request.form.get("do") or ""
        at = (request.form.get("at") or "").strip() or _today()
        note = (request.form.get("note") or "").strip() or None
        enquiry_id = request.form.get("enquiry_id", type=int)
        party_id = request.form.get("party_id", type=int)

        if do.startswith("snooze"):
            rule = request.form.get("rule") or ""
            kind = request.form.get("kind") or ""
            ref_id = request.form.get("ref_id", type=int)
            if kind not in ("enquiry", "party", "vendor_request", "shipment") or not ref_id:
                abort(400)
            if do == "snooze_until":
                until = (request.form.get("until") or "").strip()
                if not until:
                    warn("Pick a date to snooze until.")
                    return redirect(url_for("tracking_page"))
            else:
                until = tracking.snooze_until(do.rsplit("_", 1)[-1])
            tracking.snooze(repo, rule, kind, ref_id, until)
            flash("{} snoozed until {}".format(rule, until))

        elif do in ("quick:requote", "quick:Sourcing") and request.form.get("rule") == "R3":
            # The Replied row's Re-quoted button (quick:Sourcing is the value a
            # page loaded before Sourcing was retired still posts): the same
            # event the case page writes (v30), the case stays Quoted.
            if not enquiry_id:
                abort(400)
            repo.requote(enquiry_id, at, note=note)
            say(_status_said(conn, repo, enquiry_id, "Quoted", at)
                  if repo.last_status_outcome == "repeat" else
                  "{} re-quoted on {}".format(_opp_no(conn, enquiry_id), at))

        elif do == "status" or do.startswith("quick:"):
            # 'quick:Quoted' is the one-click button on a focus row: the
            # status is in the button, the date is the row's date field.
            status = do[len("quick:"):] if do.startswith("quick:") else \
                (request.form.get("status") or "")
            if status not in status_mod.ALL_STATUSES or not enquiry_id:
                warn("Pick a status first.")
                return redirect(url_for("tracking_page"))
            # A row can carry several reason lists (Lost... / Cancelled... /
            # Drop... and the one under More); the filled-in one from this
            # status's list counts, else the first filled in.
            filled = [r.strip() for r in request.form.getlist("closed_reason") if r and r.strip()]
            if status == "Lost":
                # L2: a Decide or R11 row offers its No response reason even
                # before it is on the list; choosing it adds it, as the batch does
                filled = [status_mod.ensure_closed_reason(conn, r)
                          if r.casefold() in {x.casefold() for x in status_mod.NO_RESPONSE_REASONS} else r
                          for r in filled]
            reason = next((r for r in filled if status_mod.reason_belongs(conn, status, r)),
                          filled[0] if filled else None)
            if status in status_mod.REASON_STATUSES and not reason:
                warn("{} needs a reason - pick one on the row.".format(status))
                return redirect(url_for("tracking_page"))
            if not status_mod.reason_belongs(conn, status, reason):
                warn("'{}' is not a {} reason.".format(reason, status))
                return redirect(url_for("tracking_page"))
            refusal = repo.closing_refusal(enquiry_id, status, at)
            if refusal:
                warn(refusal)
                return redirect(url_for("tracking_page"))
            if status == "Quoted" and conn.execute(
                    "SELECT 1 FROM enquiry WHERE id = ? AND status = 'Quoted'", (enquiry_id,)).fetchone():
                repo.quote_again(enquiry_id, at, note=note)       # a re-quote (13d)
                followed = True
            else:
                followed = repo.set_status(enquiry_id, status, at, note=note,
                                           closed_reason=reason)
            say(_status_said(conn, repo, enquiry_id, status, at, followed,
                               written="{} set to {}".format(_opp_no(conn, enquiry_id), status)))

        elif do == "keep":
            # L1: the month-end review's Keep - On hold for silent_days, so the
            # case leaves the card until then
            from .. import silent
            if not enquiry_id:
                abort(400)
            until = (dt.date.fromisoformat(at[:10]) + dt.timedelta(days=silent.days_setting(conn))).isoformat()
            repo.set_hold(enquiry_id, "Reviewed {}, keep".format(at[:10]), at, until=until)
            flash("{} kept: on hold until {}".format(_opp_no(conn, enquiry_id), until))

        elif do in ("wait", "info_received"):
            # WR1: Waiting for requester (an R1 row), Info received (an R12 row)
            from .. import wait
            if not enquiry_id:
                abort(400)
            if do == "wait":
                event_id, why = wait.start(repo, enquiry_id, request.form.get("what") or "", at)
                done = "{} waits for the requester since {}".format(_opp_no(conn, enquiry_id), at)
            else:
                event_id, why = wait.info_received(repo, enquiry_id, _today())
                done = "{}: info received, back to work".format(_opp_no(conn, enquiry_id))
            if event_id is None:
                warn("{}: {}".format(_opp_no(conn, enquiry_id), why))
                return redirect(url_for("tracking_page"))
            flash(done)

        elif do == "note":
            if not enquiry_id or not note:
                warn("Type the note first.")
                return redirect(url_for("tracking_page"))
            repo.add_note(enquiry_id, at, note)
            flash("Note added to {}".format(_opp_no(conn, enquiry_id)))

        elif do == "followup":
            # Log FU on an R3 row: the next number, the row's date, by mail
            # unless the form says call.
            if not enquiry_id:
                abort(400)
            channel = request.form.get("channel") or "mail"
            from .. import wait
            # WR1: a chaser on a waiting case is numbered from the wait note
            event_id = repo.add_followup(enquiry_id, at, channel=channel, note=note,
                                         seq=wait.next_seq(conn, enquiry_id))
            if event_id is None:
                abort(404)
            seq = conn.execute("SELECT seq FROM status_event WHERE id = ?", (event_id,)).fetchone()["seq"]
            suggestions.close_logged_followups(repo, enquiry_id)          # FX7: this one stands
            flash("FU{} logged on {} ({}, {})".format(seq, _opp_no(conn, enquiry_id), channel, at))

        elif do == "vendor_replied":
            vendor_request_id = request.form.get("ref_id", type=int)
            row = conn.execute("SELECT id FROM vendor_request WHERE id = ?",
                               (vendor_request_id,)).fetchone()
            if row is None:
                abort(404)
            # 'they quoted' on the tracking page sets the one status; the
            # repository derives the quoted flag from it (2026-09-25)
            values = {"replied_on": at, "notes": note}
            if request.form.get("quoted"):
                values["machine_status"] = "Quoted"
            repo.update("vendor_request", vendor_request_id, values)
            repo.touch("vendor_request", vendor_request_id)
            flash("Reply recorded")

        elif do == "touch":
            if not party_id:
                abort(400)
            repo.insert("party_event", {
                "party_id": party_id, "at": at,
                "type": request.form.get("type") or "outreach",
                "summary": note, "source": "manual"})
            flash("Touch logged")

        else:
            abort(400)
        return redirect(url_for("tracking_page"))

    # -------------------------------------------------------- 7.3 report --
    @app.route("/report")
    def report_page():
        """The month report, printable, with a summary box at the top.

        The same report.build() writes the export's Report sheet, so what
        is on the screen is what is in the file.
        """
        conn = g.conn
        month = _month_arg(request.args.get("month"))
        common = dict(month=month, months=_report_months(conn, month), previous=report.previous_month(month),
                      following=report.next_month(month))
        if request.args.get("view") == "cheat":
            # 17c: the cheat sheet, the second tab - same month, same base
            from .. import cheatsheet
            return render_template("report.html", view="cheat", report=None, sheet=cheatsheet.build(conn, month),
                                   css=monthly_export.css(".mreport") + "\n" + cheatsheet.CSS, **common)
        # 17j: outcomes on the month's last day; the running month's is today
        built = monthly.build(conn, month, dt.date.fromisoformat(_today()))
        from .. import inquiries_xlsx
        return render_template(
            "report.html", view="", report=built, sheet=None, inquiry_tbc=inquiries_xlsx.tbc_list(conn, month),
            frag=monthly_export.fragments(built), precheck=True,
            css=monthly_export.css(".mreport") + "\n" + _report_tabs_css(), **common)

    @app.route("/export/report", methods=["POST"])
    def export_report():
        """Write {MONTH} REPORT.html / .pdf / .xlsx to the reports folder."""
        month = _month_arg(request.form.get("month"))
        kind = request.form.get("kind") or "html"
        if kind not in ("html", "pdf", "xlsx"):
            abort(400)
        try:
            written = monthly_export.write(g.conn, month, kinds=(kind,), today=dt.date.fromisoformat(_today()))
        except monthly_export.EdgeNotFound as exc:
            warn(str(exc))
            return redirect(url_for("report_page", month=month))
        except Exception as exc:
            logs.get("web").exception("report export failed")
            warn("Report export failed: {}".format(exc))
            return redirect(url_for("report_page", month=month))
        flash("Report written: {}".format(written[kind]))
        return redirect(url_for("report_page", month=month))

    @app.route("/report/inquiries.xlsx")
    def report_inquiries_xlsx():
        """Batch 19: the month's inquiries for Derek, a browser download
        (OppList_Inquiries_<today>.xlsx); nothing is written to disk."""
        import io
        from flask import send_file
        from .. import inquiries_xlsx
        month = _month_arg(request.args.get("month"))
        today = dt.date.fromisoformat(_today())
        data = inquiries_xlsx.build(g.conn, month, today)
        return send_file(io.BytesIO(data), as_attachment=True, download_name=inquiries_xlsx.file_name(today),
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # ---------------------------------------------------- 4.11 shipments --
    def _shipment_or_404(conn, shipment_id):
        row = conn.execute("SELECT * FROM shipment WHERE id = ?", (shipment_id,)).fetchone()
        if row is None:
            abort(404)
        return row

    def _stamp(value) -> str:
        """'2026-09-25T14:30' (datetime-local) or a date -> 'YYYY-MM-DD HH:MM'."""
        from .. import shipments
        text = (value or "").strip().replace("T", " ")
        if len(text) == 10:
            text += " 00:00"
        return text[:16] or shipments.now_stamp()

    @app.route("/shipments")
    def shipments_page():
        from .. import shipments
        which = request.args.get("show") or "active"
        if which not in ("active", "delivered", "all"):
            which = "active"
        return render_template("shipments.html", rows=shipments.list_rows(g.conn, which, _today()),
                               which=which)

    @app.route("/shipments/<int:shipment_id>")
    def shipment_page(shipment_id):
        from .. import shipments
        conn = g.conn
        ship = _shipment_or_404(conn, shipment_id)
        state = shipments.summary(conn, shipment_id, _today())
        case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (ship["enquiry_id"],)).fetchone()
        all_events = shipments.events(conn, shipment_id)
        resolved = {e["resolves_event_id"] for e in all_events if e["resolves_event_id"] and not e["voided"]}
        shown, folded = all_events[:10], all_events[10:]
        # 5b: a Log form prefilled from one archived mail (?from_mail=&as=)
        prefill = None
        if (request.args.get("from_mail") or "").strip():
            mail = _archived_mail(request.args.get("from_mail"))
            if mail is None:
                warn("That mail is not in the archive any more.")
            else:
                prefill = _shipment_prefill(mail, ship, request.args.get("as") or "milestone")
        return render_template(
            "shipment.html", ship=ship, state=state, case=case, today=_today(),
            stepper=shipments.stepper(state), facts=shipments.facts(state),
            references=shipments.references(ship), milestones=state["milestones"],
            shown=shown, folded=folded, resolved=resolved,
            now=shipments.now_stamp().replace(" ", "T"), detail_fields=shipments.DETAIL_FIELDS,
            mode_label=shipments.MODE_LABELS[state["mode"]], prefill=prefill,
            close_reasons=shipments.CLOSE_REASONS,
            route=shipments.route_map(conn, ship, case, state) if case is not None else None,
            links=shipments.carrier_links(ship),
            suggestions=suggestions.pending(conn, shipment_id=shipment_id),
            sugg_items=suggestions.view(conn, suggestions.pending(conn, shipment_id=shipment_id), _today()),
            sugg_dismissed=suggestions.dismissed(conn, shipment_id=shipment_id),
            edited_events=suggestions.edited_events(conn, "shipment_event", [e["id"] for e in all_events]),
            mail_card=_mail_card(conn, case, request.args.get("mail") == "all") if case is not None else ([], {}, 0))

    def _shipment_prefill(mail, ship, as_kind) -> dict:
        """What the Log form opens with when a mail row asks for it: the
        detection's guess, the mail's time, its first line, the source."""
        from .. import shipments
        proposal = shipments.detect(mail, ship, dest=shipments.destination_names(g.conn, ship["enquiry_id"]))
        stamp = shipments._stamp_of(mail)
        lines = shipments._lines(mail)
        out = {"kind": "issue" if as_kind == "issue" else "milestone",
               "at": stamp.replace(" ", "T"), "detail": shipments._first_line(lines),
               "message_id": mail.message_id or "", "sender": mail.sender_name or mail.sender_address or "",
               "when": stamp, "milestone": "", "title": "", "eta": "", "quote": "", "confidence": ""}
        if proposal is not None:
            out.update({"at": proposal.at.replace(" ", "T"), "detail": proposal.detail or out["detail"],
                        "eta": proposal.eta or "", "quote": proposal.quote, "confidence": proposal.confidence})
            if proposal.kind == "milestone":
                out["milestone"] = proposal.name
                out["title"] = proposal.name
            else:
                out["title"] = proposal.name
        return out

    def _mail_source(form) -> dict:
        """The source fields a Log form carries when it was opened from a mail."""
        if (form.get("source") or "") == "mail" and (form.get("message_id") or "").strip():
            return {"source": "mail", "message_id": form.get("message_id").strip(),
                    "sender": (form.get("sender") or "").strip() or None}
        return {}

    @app.route("/shipments/<int:shipment_id>/update-from-outlook", methods=["POST"])
    def shipment_update_from_outlook(shipment_id):
        """Read the mail selected in Outlook, run the detection, and put
        the proposal in the status card for Accept / Dismiss (5b)."""
        from .. import shipments
        conn, repo = g.conn, g.repo
        ship = _shipment_or_404(conn, shipment_id)
        case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (ship["enquiry_id"],)).fetchone()
        try:
            mail = outlook_bridge.read_selection()
        except outlook_bridge.OutlookError as exc:
            logs.get("web").info("outlook selection refused: %s", exc.detail)
            warn(exc.message)
            return redirect(url_for("shipment_page", shipment_id=shipment_id))
        found = suggestions.membership(conn, case, mail)
        if not found.belongs:
            warn("{} ({}).".format(TEXT["ship_not_this_case"], found.how))
            return redirect(url_for("shipment_page", shipment_id=shipment_id))
        proposal = shipments.detect(mail, ship, dest=shipments.destination_names(conn, ship["enquiry_id"]))
        if proposal is None:
            warn(TEXT["ship_nothing_detected"])
            return redirect(url_for("shipment_page", shipment_id=shipment_id))
        values = shipments.suggestion_values(proposal, mail, source="mail")
        values["shipment_id"] = shipment_id
        made = suggestions.record(repo, ship["enquiry_id"], case["opp_no"], values)
        repo.commit()
        flash("Proposed from the mail: {} ({}); accept or dismiss below.".format(proposal.name, proposal.confidence)
              if made else "Nothing new to suggest from that mail (already suggested, or dismissed before).")
        return redirect(url_for("shipment_page", shipment_id=shipment_id) + "#suggested")

    @app.route("/enquiries/<int:enquiry_id>/shipment/start", methods=["POST"])
    def shipment_start(enquiry_id):
        """Start the one shipment of a won case, prefilled from the case."""
        from .. import shipments
        conn, repo = g.conn, g.repo
        row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if row is None:
            abort(404)
        route_id = request.form.get("route_id", type=int)
        route = shipments.route_of(conn, enquiry_id, route_id)
        existing = shipments.for_route(conn, route["id"]) if route is not None \
            else shipments.for_enquiry(conn, enquiry_id)
        if existing is not None:
            return redirect(url_for("shipment_page", shipment_id=existing["id"]))
        if row["status"] != "Won":
            warn("Only a won case starts a shipment.")
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        try:
            shipment_id = shipments.start(repo, enquiry_id, None, route_id)
        except ValueError as exc:
            warn(str(exc))
            return redirect(url_for("enquiry", enquiry_id=enquiry_id))
        repo.commit()
        flash("Shipment started from {}.".format(row["opp_no"]))
        return redirect(url_for("shipment_page", shipment_id=shipment_id))

    @app.route("/shipments/<int:shipment_id>/milestone", methods=["POST"])
    def shipment_milestone(shipment_id):
        from .. import shipments
        conn, repo = g.conn, g.repo
        _shipment_or_404(conn, shipment_id)
        try:
            shipments.log_milestone(repo, shipment_id, request.form.get("milestone") or "",
                                    _stamp(request.form.get("at")), request.form.get("detail"),
                                    (request.form.get("eta") or "").strip() or None, **_mail_source(request.form))
        except ValueError as exc:
            warn(str(exc))
            return redirect(url_for("shipment_page", shipment_id=shipment_id))
        repo.commit()
        flash("Milestone logged: {}".format(request.form.get("milestone")))
        return redirect(url_for("shipment_page", shipment_id=shipment_id))

    @app.route("/shipments/<int:shipment_id>/issue", methods=["POST"])
    def shipment_issue(shipment_id):
        from .. import shipments
        conn, repo = g.conn, g.repo
        _shipment_or_404(conn, shipment_id)
        title = (request.form.get("title") or "").strip()
        if not title:
            warn("An issue needs a title.")
            return redirect(url_for("shipment_page", shipment_id=shipment_id))
        shipments.log_issue(repo, shipment_id, title, _stamp(request.form.get("at")), request.form.get("detail"),
                            **_mail_source(request.form))
        if (request.form.get("eta") or "").strip():
            repo.update("shipment", shipment_id, {"eta": request.form.get("eta").strip()})
        repo.commit()
        flash("Issue logged: {}".format(title))
        return redirect(url_for("shipment_page", shipment_id=shipment_id))

    @app.route("/shipments/<int:shipment_id>/note", methods=["POST"])
    def shipment_note(shipment_id):
        from .. import shipments
        conn, repo = g.conn, g.repo
        _shipment_or_404(conn, shipment_id)
        title = (request.form.get("title") or "").strip()
        detail = (request.form.get("detail") or "").strip()
        if not title and not detail:
            warn("A note needs some words.")
            return redirect(url_for("shipment_page", shipment_id=shipment_id))
        shipments.add_note(repo, shipment_id, title or detail[:80], _stamp(request.form.get("at")),
                           detail if title else None, **_mail_source(request.form))
        repo.commit()
        flash("Note added.")
        return redirect(url_for("shipment_page", shipment_id=shipment_id))

    @app.route("/shipments/<int:shipment_id>/events/<int:event_id>", methods=["POST"])
    def shipment_event(shipment_id, event_id):
        """Resolve an issue, or strike an event out / put it back."""
        from .. import shipments
        conn, repo = g.conn, g.repo
        _shipment_or_404(conn, shipment_id)
        action = request.form.get("action") or ""
        try:
            if action == "resolve":
                shipments.resolve(repo, shipment_id, event_id, _stamp(request.form.get("at")),
                                  request.form.get("detail"))
                flash("Issue resolved.")
            elif action in ("void", "unvoid"):
                notes = shipments.void_event(repo, shipment_id, event_id, voided=action == "void")
                flash(" ".join(["Event {}.".format("struck out" if action == "void" else "put back")] + notes))
            else:
                abort(400)
        except KeyError:
            abort(404)
        repo.commit()
        return redirect(url_for("shipment_page", shipment_id=shipment_id))

    @app.route("/shipments/<int:shipment_id>/edit", methods=["POST"])
    def shipment_edit(shipment_id):
        from .. import shipments
        conn, repo = g.conn, g.repo
        _shipment_or_404(conn, shipment_id)
        values = {k: request.form.get(k) for k in shipments.DETAIL_FIELDS if k in request.form}
        changed = shipments.edit(repo, shipment_id, values)
        repo.commit()
        say("Details saved" if changed else "Nothing to change", done=bool(changed))
        return redirect(url_for("shipment_page", shipment_id=shipment_id))

    @app.route("/shipments/<int:shipment_id>/close", methods=["POST"])
    def shipment_close(shipment_id):
        from .. import shipments
        conn, repo = g.conn, g.repo
        _shipment_or_404(conn, shipment_id)
        reopen = request.form.get("action") == "reopen"
        try:
            shipments.close(repo, shipment_id, closed=not reopen,
                            reason=(request.form.get("reason") or "Delivered").strip())
        except ValueError as exc:
            warn(str(exc))
            return redirect(url_for("shipment_page", shipment_id=shipment_id))
        repo.commit()
        flash("Shipment {}".format("reopened" if reopen else "closed"))
        return redirect(url_for("shipment_page", shipment_id=shipment_id))

    # ---------------------------------------------------- 4.10 dashboard --
    @app.route("/dashboard")
    def dashboard_page():
        """The pipeline at a glance: inline SVG built in
        Python, read-only, no OPP number on it."""
        from .. import dashboard
        period = request.args.get("period") or "month"
        dash = dashboard.build(g.conn, _today(), period)
        return render_template("dashboard.html", dash=dash, frag=dashboard.fragments(dash),
                               as_of=dashboard.as_of(dash), css=dashboard.CSS.strip(),
                               periods=dashboard.PERIODS, standalone=False)

    @app.route("/export/dashboard", methods=["POST"])
    def export_dashboard():
        """Write DASHBOARD <yyyy-mm-dd>.html, self-contained, to the reports folder."""
        from .. import dashboard
        period = request.form.get("period") or "month"
        try:
            dash = dashboard.build(g.conn, _today(), period)
            written = dashboard.write_html(dash)
        except Exception as exc:
            logs.get("web").exception("dashboard export failed")
            warn("Dashboard export failed: {}".format(exc))
            return redirect(url_for("dashboard_page", period=period))
        flash("Dashboard written: {}".format(written))
        return redirect(url_for("dashboard_page", period=period))

    # ------------------------------------------------------- 4.6 export --
    @app.route("/export")
    def export_page():
        conn = g.conn
        mode = autoexport.target(conn)
        out_dir, data_dir = autoexport.directories(mode)
        files = [
            out_dir / paths.EXPORT_WORKBOOK_NAME,
            data_dir / paths.OPP_INDEX_NAME,
            data_dir / paths.AGENTS_CSV_NAME,
        ]
        report_month = monthly.default_month()
        return render_template(
            "export.html", mode=mode, files=[_file_info(p) for p in files],
            report_months=_report_months(conn, report_month),
            report_month=report_month,
            report_files=[_file_info(paths.REPORTS_DIR / "{}.{}".format(
                monthly.file_stem(report_month), kind)) for kind in ("html", "pdf", "xlsx")],
            auto_on=db.get_setting(conn, "auto_export", "1") == "1",
            auto_pending=app.auto_exporter.pending,
            auto_error=app.auto_exporter.last_error,
            offsite=_scheduled_offsite(app.config["DB_PATH"]),
            restore_points=_restore_points(backup.folder_for(app.config["DB_PATH"])),
            share_dir=db.get_setting(conn, "export_share_dir", ""))

    @app.route("/export/run", methods=["POST"])
    def export_run():
        mode = request.form.get("mode") or None
        try:
            written = autoexport.export_now(app.config["DB_PATH"], mode=mode)
        except Exception as exc:
            logs.get("web").exception("export failed")
            warn("Export failed: {}".format(exc))
            return redirect(url_for("export_page"))
        flash("Exported ({}): {}".format(
            written["mode"], ", ".join(str(written[k].name) for k in
                                       ("workbook", "opp_index", "agents"))))
        if written.get("share_error"):
            warn("Share copy failed, the export itself is fine: {}".format(
                written["share_error"]))
        return redirect(url_for("export_page"))

    @app.route("/export/backup", methods=["POST"])
    def export_backup():
        conn = g.conn
        keep = int(db.get_setting(conn, "backup_keep", "30"))
        try:
            made = backup.before_task(app.config["DB_PATH"], "manual")
            removed = backup.prune(keep, db_path=app.config["DB_PATH"])
        except OSError as exc:
            warn("Backup failed: {}".format(exc))
            return redirect(url_for("export_page"))
        flash("Restore point: {} (pruned {} old daily copies)".format(
            made.name, len(removed)))
        return redirect(url_for("export_page"))

    @app.route("/check")
    def check_page():
        return render_template("check.html", findings=checks.run(g.conn))

    # ----------------------------------------------------- 4.6 settings --
    LIST_SETTINGS = ("owners", "container_types", "packing_units", "special_handling",
                     "closed_reasons_lost", "closed_reasons_cancelled", "closed_reasons_dropped", "requote_reasons",
                     "latam_countries", "internal_domains", "service_scopes", "report_events")
    NUMBER_SETTINGS = ("fu1_days", "fu2_days", "fu_decide_days", "fu1_wait_days", "fu_max", "silent_days",
                       "cadence_days_default", "rule_new_days",
                       "rule_vendor_no_reply_days", "rule_quoted_no_answer_days",
                       "dedupe_window_days", "dedupe_same_subject_days", "suggestion_expire_days", "backup_keep",
                       "port")
    # FX4: the mail templates' settings (fu_template_*, ack_template,
    # lost_feedback_template) stay in the setting table, unused
    TEXT_SETTINGS = ("export_share_dir", "offsite_backup_dir")

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        conn = g.conn
        if request.method == "POST":
            # 17g: the report's events - a line it cannot read is refused,
            # naming it, and nothing is saved
            from .. import monthly_requests
            refused = monthly_requests.refused_event_lines((request.form.get("report_events") or "").splitlines())
            if refused:
                for number, line in refused:
                    warn('Report events, line {}: "{}" is not "YYYY-MM-DD label" or '
                         '"YYYY-MM-DD to YYYY-MM-DD label". Nothing was saved.'.format(number, line.strip()))
                return _settings_page(conn, posted_lists={key: [
                    line.strip() for line in (request.form.get(key) or "").splitlines() if line.strip()]
                    for key in ("report_events",)})
            for key in LIST_SETTINGS:
                # One value per line; blank lines dropped, order kept.
                values = [line.strip() for line in
                          (request.form.get(key) or "").splitlines()
                          if line.strip()]
                if values or (key == "report_events" and key in request.form):
                    db.set_setting(conn, key, values)       # this one may be emptied
            for key in NUMBER_SETTINGS:
                raw = (request.form.get(key) or "").strip()
                if raw.isdigit():
                    db.set_setting(conn, key, int(raw))
            # the cargo checks' limits (v34): a number each, a gap keeps the old one
            limits = cargo_checks.limits(conn)
            for key in cargo_checks.DEFAULT_LIMITS:
                raw = (request.form.get("cargo_limit_" + key) or "").strip().replace(",", "")
                try:
                    number = float(raw)
                except ValueError:
                    continue
                if number >= 0:
                    limits[key] = int(number) if number == int(number) else number
            db.set_setting(conn, "cargo_limits", json.dumps(limits, sort_keys=True))
            # 15b: a 40' over this on US roads needs a tri-axle chassis or a permit
            raw = (request.form.get("us_chassis_max_kg") or "").strip().replace(",", "")
            try:
                if float(raw) > 0:
                    db.set_setting(conn, "us_chassis_max_kg", str(int(float(raw))))
            except ValueError:
                pass
            for key in TEXT_SETTINGS:
                if key not in request.form:
                    continue
                value = (request.form.get(key) or "").strip()
                if key in paths.FOLDER_SETTINGS:
                    value = paths.folder_setting(value)   # FX1: Copy as path's quotes
                db.set_setting(conn, key, value)

            wanted = request.form.get("export_target")
            if wanted in ("preview", "publish"):
                previous = autoexport.target(conn)
                if wanted != previous:
                    logs.get("web").warning("export_target %s -> %s",
                                            previous, wanted)
                    flash("Export target is now '{}'.".format(wanted))
                db.set_setting(conn, "export_target", wanted)
            db.set_setting(conn, "auto_export",
                           "1" if request.form.get("auto_export") else "0")
            if request.form.get("tracking_hidden_shown"):
                # FX4: the Today's work cards Tracking hides ('' = none)
                known = [d[0] for d in tracking.TODAY_CARDS]
                ticked = set(request.form.getlist("tracking_hidden"))
                db.set_setting(conn, tracking.HIDDEN_SETTING, ",".join(k for k in known if k in ticked))
            from .. import shipments_switch
            # FX3: the shipments switch (off until she redesigns them)
            db.set_setting(conn, shipments_switch.SETTING, "1" if request.form.get("shipments") else "0")
            if "helper_folder" in request.form:
                # FX4: the Make Outlook drafts folder the team site shows ('' = none)
                from .. import firebase_sync
                db.set_setting(conn, firebase_sync.HELPER_SETTING,
                               paths.folder_setting((request.form.get("helper_folder") or "").strip()))
            if "rate_request_cc" in request.form:
                # W2a: the team address the Ask vendors card copies; '' = none
                db.set_setting(conn, "rate_request_cc", " ".join((request.form.get("rate_request_cc") or "").split()))
            if "team_site_form" in request.form:
                # this edition: the two local stores, off unless ticked
                from .. import firebase_sync, ratecard_sync
                db.set_setting(conn, firebase_sync.STORE_SETTING, "on" if request.form.get("team_site_store") else "")
                db.set_setting(conn, ratecard_sync.STORE_SETTING, "on" if request.form.get("ratecard_store") else "")
            if "firebase_daily_cap" in request.form:
                # FX3: the most documents the team site's pushes write a day
                from .. import firebase_sync
                raw = (request.form.get("firebase_daily_cap") or "").replace(",", "").strip()
                if raw.isdigit() and int(raw) > 0:
                    db.set_setting(conn, firebase_sync.CAP_SETTING, str(int(raw)))
                else:
                    warn("Team site: most writes a day needs a whole number above 0; it stays {:,}.".format(
                        firebase_sync.daily_cap(conn)))
            # W3: the quote card's markup and the margin it warns under
            from .. import firebase_sync
            for key, label in ((firebase_sync.MARKUP_SETTING, "Quote card: markup"),
                               (firebase_sync.FLOOR_SETTING, "Quote card: lowest margin")):
                if key not in request.form:
                    continue
                raw = (request.form.get(key) or "").replace("%", "").strip()
                try:
                    number = float(raw)
                except ValueError:
                    number = -1
                if 0 <= number <= 100:
                    db.set_setting(conn, key, "{:g}".format(number))
                else:
                    warn("{} needs a number from 0 to 100; it stays {}%.".format(
                        label, firebase_sync.quote_rules(conn)[
                            "markupPct" if key == firebase_sync.MARKUP_SETTING else "marginFloorPct"]))
            if "read_quote_present" in request.form:
                # Q2d: Read quote's switch, key file, model and monthly cap
                from .. import read_quote
                db.set_setting(conn, read_quote.ON_SETTING, "1" if request.form.get("read_quote_on") else "0")
                # this edition: answers from the recordings, no key and no call
                db.set_setting(conn, read_quote.RECORDED_SETTING,
                               "1" if request.form.get("read_quote_recorded") else "0")
                key_text = paths.folder_setting((request.form.get("read_quote_key") or "").strip())
                old_key = db.get_setting(conn, read_quote.KEY_SETTING, "") or ""
                db.set_setting(conn, read_quote.KEY_SETTING, key_text)
                try:
                    read_quote.key_path(conn)
                except read_quote.ReadQuoteError as exc:
                    db.set_setting(conn, read_quote.KEY_SETTING, old_key)
                    warn("Read quote: {}; the key file stays as it was.".format(exc))
                chosen = (request.form.get("read_quote_model") or "").strip()
                if chosen in read_quote.MODELS:
                    db.set_setting(conn, read_quote.MODEL_SETTING, chosen)
                raw = (request.form.get("read_quote_cap_usd") or "").replace(",", "").replace("$", "").strip()
                try:
                    amount = float(raw)
                except ValueError:
                    amount = -1
                if amount > 0:
                    db.set_setting(conn, read_quote.CAP_SETTING, "{:g}".format(amount))
                else:
                    warn("Read quote: the most a month needs a number above 0; it stays USD {:.2f}.".format(
                        read_quote.cap(conn)))
            flash(TEXT["saved"])
            return redirect(url_for("settings"))
        return _settings_page(conn)

    @app.route("/settings/read-quote-report")
    def read_quote_report():
        """Q2d: This week's report - written to ..\\data\\reports as the
        command writes it, and handed over as that same file."""
        from flask import send_file

        from .. import read_quote
        path = read_quote.write_report(g.conn, 7, _today())
        return send_file(path, mimetype="text/markdown", as_attachment=False, download_name=path.name)

    @app.route("/settings/team-site/sync", methods=["POST"])
    def team_site_sync_now():
        """W1a: Sync now - a push on a background thread; the page does not
        wait (its result is in the Team site box at the next look)."""
        import threading
        from .. import firebase_sync
        ok, why = firebase_sync.configured(g.conn)
        if not firebase_sync.is_live(app.config["DB_PATH"]):
            say("Team site: not pushed - this app is not over the live database.", done=False)
        elif not ok:
            say("Team site: not set up - {}.".format(why), done=False)
        else:
            threading.Thread(target=app.firebase_syncer.run, name="firebase-sync-now", daemon=True).start()
            say("Team site: pushing now; the box below shows the result in a minute.", done=True)
        return redirect(url_for("settings") + "#team-site")

    def _settings_page(conn, posted=None, posted_lists=None):
        from .. import silent
        texts = {key: db.get_setting(conn, key, "") for key in TEXT_SETTINGS}
        texts.update(posted or {})
        return render_template(
            "settings.html",
            lists=dict({key: db.get_setting_list(conn, key) or (
                routes.DEFAULT_SCOPES if key == "service_scopes" else []) for key in LIST_SETTINGS},
                **(posted_lists or {})),
            numbers={key: db.get_setting(conn, key, {"silent_days": "30", "fu1_wait_days": "10",
                                                     "dedupe_same_subject_days": str(dedupe.SAME_SUBJECT_DAYS),
                                                     "suggestion_expire_days": str(suggestions.EXPIRE_DAYS)}.get(key, ""))
                     for key in NUMBER_SETTINGS},
            cargo_limits=[(key, label, cargo_checks.limits(conn)[key])
                          for key, label in cargo_checks.LIMIT_LABELS.items()],
            us_chassis_max_kg=db.get_setting(conn, "us_chassis_max_kg", "17200"),
            texts=texts,
            export_target=autoexport.target(conn),
            auto_on=db.get_setting(conn, "auto_export", "1") == "1",
            preview_dir=paths.EXPORT_PREVIEW_DIR,
            live_dirs=(paths.WORKBOOKS_DIR, paths.DATA_DIR),
            db_path=app.config["DB_PATH"],
            team=_team_site_info(conn, app.config["DB_PATH"]),
            rq=_read_quote_info(conn),
            tracking_cards=[(d[0], d[3].format(days=silent.days_setting(conn)) if "{days}" in d[3] else d[3])
                            for d in tracking.TODAY_CARDS],
            tracking_hidden=tracking.hidden_cards(conn),
            offsite=_scheduled_offsite(app.config["DB_PATH"]),
            running_lines=_running_lines(app),
            reader_table=_reader_table(),
            schema_version=db.get_setting(conn, "schema_version", "?"))


def _reader_table() -> dict:
    """RG2b (layer 3): Settings' table of the reader's monthly checks."""
    from .. import reader_month
    try:
        return reader_month.table()
    except Exception:
        logs.get("web").exception("reader table")
        return {"months": [], "rows": []}


def _running_lines(app) -> list:
    """OP2: Settings' lines on the code this server runs (one git call)."""
    from .. import running
    try:
        return running.settings_lines(getattr(app, "started_at", None))
    except Exception:
        logs.get("web").exception("running lines")
        return []


def _team_site_info(conn, db_path) -> dict:
    """The Settings page's Team site box (W1a): set up or why not, where
    the local store is, the last run."""
    from .. import firebase_sync, ratecard_sync
    ok, why = firebase_sync.configured(conn)
    live = firebase_sync.is_live(db_path)
    last = firebase_sync.status()
    counts = last.get("counts") or {}
    return {"cap": firebase_sync.daily_cap(conn),
            "today": firebase_sync.writes_on(firebase_sync.load_state(), _today()),
            "ready": ok and live, "why": why if not ok else ("" if live else "this app is not over the live database"),
            "project": firebase_sync.project(conn), "account": firebase_sync.service_account(conn) if ok else "",
            "last": last, "cc": firebase_sync.team_cc(conn), "quote": firebase_sync.quote_rules(conn),
            "helper": firebase_sync.helper_folder(conn),
            "store_on": ok, "ratecard_on": ratecard_sync.configured(conn)[0],
            # Q2c: the Rate Card's Agent quotes - the last push, its error, or Off and why
            "ratecard": _ratecard_line(conn),
            "counts": "{} inquiries, {} waiting, {} pending on us, {} cases".format(
                counts.get("inquiries", 0), counts.get("waiting", 0), counts.get("pending", 0),
                counts.get("cases", 0)) if counts else ""}




def _read_quote_info(conn) -> dict:
    """Q2d: the Settings page's Read quote block."""
    from .. import read_quote
    try:
        return read_quote.info(conn)
    except Exception:                                     # the page shows without it
        logs.get("web").exception("read quote settings")
        return {"on": False, "recorded": False, "has_key": False, "key_line": "unknown", "key_path": "", "model": "", "models": (),
                "cap": 0, "spend_line": "unknown", "error": "", "configured_key": ""}


def _ratecard_line(conn) -> str:
    from .. import ratecard_sync
    try:
        return ratecard_sync.status_line(conn)
    except Exception as exc:                              # a line, never a crash
        return "unknown ({})".format(type(exc).__name__)


#: The reason the R3 batch button closes with for a customer;
#: L2: status.no_response_reason picks it per requester.
NO_RESPONSE_REASON = status_mod.NO_RESPONSE_CUSTOMER

#: What a vendor's answer can be, as the tracker scan names them.
VENDOR_STATUSES = ("Asked", "Acknowledged", "Quoted", "Declined",
                   "Waiting on us", "No reply")


class _RouteRefusal(ValueError):
    def __init__(self, message, field, label):
        super().__init__(message)
        self.field, self.label = field, label


def _won_routes_from(conn, form, enquiry_id) -> tuple:
    """(won route ids, lost {id: reason}, cancelled {id: reason}) from the
    Won form's route picker - the case page's Won and a suggestion's Edit
    (E1) read it here. Raises _RouteRefusal."""
    live = {r["id"]: r for r in routes.routes_of(conn, enquiry_id)}
    won_ids = [int(v) for v in form.getlist("won_route") if v.isdigit() and int(v) in live]
    if not won_ids:
        raise _RouteRefusal("Tick the routes that were won.", "won_route", "Routes")
    lost, cancelled = {}, {}
    for route_id in live:
        why = (form.get("lost_reason_{}".format(route_id)) or "").strip()
        if route_id not in won_ids and why:
            # C1: 'Cancelled|reason' is a cancelled route, a bare reason a lost one
            result, _, text = why.partition("|") if why.startswith("Cancelled|") else ("Lost", "", why)
            if not status_mod.reason_belongs(conn, result, text):
                raise _RouteRefusal("'{}' is not a {} reason.".format(text, result),
                                    "lost_reason_{}".format(route_id), "Reason")
            (cancelled if result == "Cancelled" else lost)[route_id] = text
    return won_ids, lost, cancelled


def _won_prefill(conn, enquiry_id) -> dict:
    """The Won form's starting values: Agent rate when a vendor was asked,
    else Own rate; Q3: with no request awarded, from the last quote not
    struck out (site_quotes.won_start). Only the start: what is saved is
    what the form sends."""
    asked = conn.execute("SELECT 1 FROM vendor_request WHERE enquiry_id = ? AND voided = 0 LIMIT 1",
                         (enquiry_id,)).fetchone()
    out = {"rate_used": "agent" if asked else "own"}
    out.update(site_quotes.won_start(conn, enquiry_id) or {})
    return out


def _rate_used_from(form, enquiry_id):
    """(rate_used, party_id, vendor_request_id, note) from the Won form."""
    rate = (form.get("rate_used") or "").strip()
    if rate not in status_mod.RATE_USED_LABELS:
        raise ValueError("Say whose rate was used.")
    note = (form.get("awarded_note") or "").strip() or None
    if rate == "agent":
        request_id = form.get("awarded_vendor_request", type=int)
        party_id = form.get("agent_party_id", type=int)
        if not request_id and not party_id:
            raise ValueError("Agent rate: pick the vendor request or the party.")
        return rate, party_id, request_id or None, note
    if rate == "office":
        party_id = form.get("office_party_id", type=int)
        if not party_id:
            raise ValueError("Office rate: pick the office.")
        return rate, party_id, None, note
    if rate == "carrier":
        # the carrier is named when it is on file; a courier asked once need not be
        return rate, form.get("carrier_party_id", type=int) or None, None, note
    return rate, None, None, note


def _month_arg(value) -> str:
    value = (value or "").strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        return monthly.default_month()
    return value


def _report_tabs_css() -> str:
    """The two tabs' own rules (17c), shared by both views."""
    from .. import cheatsheet
    return "\n".join(line for line in cheatsheet.CSS.splitlines() if line.startswith(".report-tabs"))


def _report_months(conn, month) -> list:
    months = [r["month"] for r in conn.execute(
        "SELECT DISTINCT month FROM v_enquiry WHERE month IS NOT NULL "
        "AND month >= ? ORDER BY month DESC", (report.TREND_FROM,))]
    if month not in months:
        months.insert(0, month)
    return months


def _file_info(path):
    try:
        stat = path.stat()
        return {"path": path, "size": stat.st_size,
                "when": dt.datetime.fromtimestamp(stat.st_mtime)
                .strftime("%Y-%m-%d %H:%M:%S")}
    except OSError:
        return {"path": path, "size": None, "when": None}


def _restore_points(folder, limit: int = 8) -> list:
    try:
        files = sorted(Path(folder).glob("opplist_*.db"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    return [_file_info(p) for p in files[:limit]]


def _archived_mail(message_id):
    """One mail out of the archive by its Message-ID, or None."""
    from .. import case_mail
    search, _ = case_mail.open_search()
    if search is None:
        return None
    try:
        return search.by_message_id(message_id)
    finally:
        search.__exit__(None, None, None)


class _ThreadOnly:
    """What the attach route needs of a mail when the Mail card names it:
    its conversation id and its subject (2026-09-25)."""

    def __init__(self, conversation_id, subject):
        self.conversation_id = (conversation_id or "").strip()
        self.subject = (subject or "").strip()


#: RG2b (2): the session's From Outlook result, {case id: {cands, conversation_id, subject}}
WHO_OUTLOOK_KEY = "who_outlook"


def _who_outlook_forget(enquiry_id) -> None:
    kept = dict(session.get(WHO_OUTLOOK_KEY) or {})
    if kept.pop(str(enquiry_id), None) is not None:
        session[WHO_OUTLOOK_KEY] = kept


def _who_outlook_context(conn, row) -> dict:
    """RG2b (2): the case page's From Outlook button (a (TBC) case) and the
    candidates the selected mail gave, their parties looked up again."""
    from .. import direct_customers_out as dco, who_asked
    if row is None or not dco.is_tbc(conn, row["requester_party_id"]):
        return {"who_outlook_on": False, "who_outlook": [], "who_outlook_thread": {}}
    kept = (session.get(WHO_OUTLOOK_KEY) or {}).get(str(row["id"])) or {}
    cands = []
    for state in kept.get("cands") or []:
        try:
            cands.append(who_asked.candidate_from_state(conn, state))
        except Exception:
            logs.get("web").exception("who asked from outlook")
    return {"who_outlook_on": True, "who_outlook": cands,
            "who_outlook_thread": {"conversation_id": kept.get("conversation_id", ""),
                                   "subject": kept.get("subject", "")}}


def _who_asked(conn, row, card) -> list:
    """MF1: the Who asked? box of a (TBC) case - who_asked.candidates on the
    Mail card's agent thread (no second archive read); [] otherwise."""
    from .. import who_asked
    mails = []
    for tab in (card[0] if card else []) or []:
        if tab.get("key") == "case":
            mails = tab.get("mails") or []
    return who_asked.candidates(conn, row, mails=mails)


def _ack_state(conn, row, card) -> dict:
    """The title card's Open in Outlook, and 'Acknowledged <date>' (the
    morning scan's Acknowledged event, AL1). FX4: no Ask for feedback. FX5:
    Acknowledge again, blank (_can_acknowledge)."""
    from .. import ack
    mails = []
    for tab in (card[0] if card else []) or []:
        if tab.get("key") == "case":
            mails = tab.get("mails") or []
    out = {"acknowledged_on": ack.acknowledged_on(conn, row["id"])}
    out["show"] = _can_acknowledge(conn, row, out["acknowledged_on"])
    # M2: the title card's Open in Outlook - the registered mail, else the
    # agent thread's newest; without an archive, the case's own id
    info = card[1] if card and len(card) > 1 else {}
    if (info or {}).get("searched"):
        out["open_title"] = bool(info.get("registered_found") or case_mail.has_registered(conn, row)
                                 or any(getattr(m, "entry_id", "") for m in mails))
    else:
        out["open_title"] = bool(row["entry_id"])
    return out


def _mail_card(conn, row, show_all) -> tuple:
    """(tabs, archive status, mails hidden) for the case page's Mail card.
    Never raises: the card says why when the archive is not there."""
    from .. import case_mail
    search, info = case_mail.open_search()
    if search is None:
        return [], info, 0
    hidden = 0
    try:
        tabs = case_mail.tabs(conn, search, row)
        registered = case_mail.registered_mail(conn, row, search)        # M2: one resolver
        info = dict(info, searched=True, registered_found=registered is not None)
        for tab in tabs:
            tags = case_mail.tag_mails(conn, row, tab["mails"], registered if tab["key"] == "case" else None,
                                       by_date=tab["key"] == "case")
            rows = [case_mail.row_view(m, tags.get(case_mail.mail_key(m), [])) for m in tab["mails"]]
            rows.reverse()                                  # newest first
            if not show_all and len(rows) > case_mail.MAIL_CAP:
                hidden += len(rows) - case_mail.MAIL_CAP
                rows = rows[:case_mail.MAIL_CAP]
            tab["rows"] = rows
    except Exception:                                       # the card is a convenience
        logs.get("web").exception("mail card")
        info = dict(info, reason="the mail archive could not be read for this case")
        return [], info, 0
    finally:
        search.__exit__(None, None, None)
    return tabs, info, hidden


#: Batch 23: a route form's three place pickers - (side, field, label).
PICKERS = (("origin", "origin_loc", "Origin location"), ("dest", "dest_loc", "Dest. location"),
           ("pod", "pod", "Port of discharge"))


def _parse_pickers(conn, form, prefix, errors) -> dict:
    """What a route's pickers posted (prefix '' or 'route_1_'), checked and
    not written: {side: places.parse()}. Origin location is one place;
    Dest. location and Port of discharge take four with the master layer.
    A legacy 'Add location...' key (new_<side>_loc) is the side's key."""
    most = places.MAX_PLACES if db.master_active(conn) else 1
    out = {}
    for side, name, label in PICKERS:
        country = (form.get(prefix + ("origin_country" if side == "origin" else "dest_country")) or "").strip()
        out[side] = places.parse(form, prefix + name, label, errors, 1 if side == "origin" else most,
                                 conn=conn, country=country or None)
        if side != "pod":
            # a place's key is upper case; the pod's text is kept as written
            out[side]["keys"] = [k.upper() for k in out[side]["keys"]]
        legacy = (form.get(prefix + "new_" + name) or "").strip() if side != "pod" else ""
        if legacy:
            out[side]["keys"] = [legacy.upper()]
    return out


def _apply_pickers(conn, repo, picked, values, errors=None, prefix="", create_unknown=False) -> dict:
    """Write what a route's pickers asked for (places added To confirm,
    ports marked) and turn them into the route's values: origin_loc (and
    its id), dest_places (dest_loc / dest_loc_id follow, written by the
    repository) or the destination's text, pod_places (pod is their
    readable copy) or the pod's text. A key that names no place: on the
    register page (`create_unknown`) it is added To confirm, as a lane from
    a mail always was; on the case page it is the text the route held, and
    beside other places it is refused."""
    country = {"origin": values.get("origin_country"), "dest": values.get("dest_country"),
               "pod": values.get("dest_country")}
    out = {}
    for side, name, label in PICKERS:
        repo.place_notes.extend(n for n in picked[side].get("notes", ()) if n not in repo.place_notes)
        got = places.apply(conn, repo, picked[side], country[side],
                           create_unknown=create_unknown and side != "pod")
        if got["unknown"] and got["text"] is None and errors is not None:
            errors[prefix + name] = "{} is not in the location list: pick it from the list or add it".format(
                ", ".join(got["unknown"]))
        if side == "origin":
            if got["ids"]:
                out["origin_loc_id"], out["origin_loc"] = got["ids"][0], got["keys"][0]
            else:
                out["origin_loc"] = (got["text"] or "").upper() or None
        elif side == "dest":
            if got["ids"]:
                out["dest_places"] = got["ids"]
            else:
                out["dest_places"] = []
                out["dest_loc"] = (got["text"] or "").upper() or None
        else:
            if got["ids"]:
                out["pod_places"] = got["ids"]
            else:
                out["pod_places"] = []
                out["pod"] = got["text"]
    return out


def _route_picks(conn, route) -> dict:
    """The chips of a route's pickers (batch 23): its places, or the text a
    side holds that names no place."""
    origin = conn.execute("SELECT * FROM master_location WHERE id = ?",
                          (route["origin_loc_id"],)).fetchone() if route.get("origin_loc_id") else None
    dests, ports = route.get("dest_places") or [], route.get("pod_places") or []
    return {"origin": places.picks_from_rows([origin] if origin is not None else [], route.get("origin_loc")),
            "dest": places.picks_from_rows(dests, None if dests else route.get("dest_loc")),
            "pod": places.picks_from_rows(ports, None if ports else route.get("pod"))}


def _read_route(conn, repo, form, route_id=None) -> tuple:
    """A route out of the route form: (values, errors). New locations
    typed inline are added; a two-letter place is refused. Batch 23: the
    three place pickers (places added in their small forms, ports marked);
    a retired place is never offered, but a route that already holds one
    (`route_id`) keeps it."""
    values, errors = {}, {}
    for key in ("origin_country", "origin_alt", "dest_country", "dest_alt",
                "service_scope", "notes", "lost_reason"):
        text = (form.get(key) or "").strip()
        values[key] = text or None
    mode = (form.get("service_mode") or "").strip() or None
    if mode and mode not in forms.SERVICE_MODES:
        errors["service_mode"] = "not a service mode"
    values["service_mode"] = mode
    incoterm = (form.get("incoterm") or "").strip() or None
    if incoterm and incoterm not in forms.INCOTERMS:
        errors["incoterm"] = "not an incoterm"
    values["incoterm"] = incoterm
    result = (form.get("result") or "").strip() or None
    if result and result not in routes.RESULTS:
        errors["result"] = "Won, Lost, Cancelled or nothing"
    values["result"] = result
    if result in routes.REASON_RESULTS and values.get("lost_reason") and \
            not status_mod.reason_belongs(conn, result, values["lost_reason"]):
        errors["lost_reason"] = "not a {} reason".format(result)
    if result not in routes.REASON_RESULTS:
        values["lost_reason"] = None
    _resolve_inline(repo, form, values, errors)
    picked = _parse_pickers(conn, form, "", errors)
    for side in ("origin", "dest"):
        if (form.get("new_{}_loc".format(side)) or "").strip():
            picked[side]["keys"] = [values.get(side + "_loc")] if values.get(side + "_loc") else []
    if route_id:
        held = conn.execute("SELECT * FROM enquiry_route WHERE id = ?", (route_id,)).fetchone()
        if held is not None:
            held = dict(held)
            routes.attach_places(conn, [held])
            picked["origin"]["kept"] = [held["origin_loc"] or ""]
            picked["dest"]["kept"] = [p["key"] for p in held["dest_places"]]
            picked["pod"]["kept"] = [p["key"] for p in held["pod_places"]]
    if not errors:
        values.update(_apply_pickers(conn, repo, picked, values, errors))
    _two_letter_errors(conn, values, [], errors)
    return values, errors


def _route_gaps(conn, enquiry_id) -> list:
    """'Route B: Dest. alt (delivery), DAP needs the delivery place' - the
    place gaps of each live route, by its Service or, with none, its
    incoterm (S1; the letter only when there are several)."""
    live = routes.routes_of(conn, enquiry_id)
    out = []
    for route in live:
        gaps = {k: v for k, v in forms.missing(dict(route, requester_party_id=1, received_on="x")).items()
                if k in ("origin_alt", "dest_alt")}
        if gaps:
            text = forms.missing_line(gaps)
            out.append("Route {}: {}".format(route["letter"], text) if len(live) > 1 else text)
    return out


def _saved_message(conn, enquiry_id, prefix=None, extra=None) -> str:
    """'Saved. Still missing: ...' from the case as it now stands: the
    case's own gaps and each route's; `extra` adds what the form left empty
    and the save kept (the requester)."""
    case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    gaps = dict(extra or {})
    gaps.update({k: v for k, v in forms.missing(dict(case)).items() if k not in ("origin_alt", "dest_alt")})
    parts = ([forms.missing_line(gaps)] if gaps else []) + _route_gaps(conn, enquiry_id)
    head = prefix or TEXT["saved"]
    return "{}. Still missing: {}.".format(head.rstrip("."), "; ".join(parts)) if parts else head


def _posted_route_ids(form, enquiry_id) -> str | None:
    """The ticked routes of a vendor request or a cargo line: NULL when
    every live route is ticked (or none is)."""
    chosen = [int(v) for v in form.getlist("route") if str(v).isdigit()]
    live = [r["id"] for r in routes.routes_of(g.conn, enquiry_id)]
    chosen = [i for i in chosen if i in live]
    if not chosen or set(chosen) >= set(live):
        return None
    return routes.format_ids(chosen)


def _route_context(conn, enquiry_id, values, editing, posted=None, route_errors=None) -> dict:
    """What the Routes card, the Cargo card, the header and the Won picker
    need."""
    from .. import shipments
    live = routes.routes_of(conn, enquiry_id)
    voided = routes.routes_of(conn, enquiry_id, voided=True)
    def name_of(loc_id, key):
        return _place_title(conn, loc_id, key)
    for route in live + voided:
        route["from"] = routes.place(route, "origin", name_of)
        route["to"] = routes.place(route, "dest", name_of)
        # batch 23: a destination of several places, each one, read with 'or'
        route["dest_titles"] = [name_of(p["id"], p["key"]) or p["key"] for p in route.get("dest_places") or []]
        route["pod_text"] = routes.pod_text(route, name_of)
        route["picks"] = _route_picks(conn, route)
    edit_values = None
    if editing == "new" and live:
        first = live[0]
        edit_values = {k: first.get(k) for k in ("origin_country", "origin_loc", "origin_loc_id", "origin_alt",
                                                "service_mode", "incoterm", "service_scope")}
        edit_values["id"] = None
        edit_values["letter"] = routes.letter((max(r["seq"] for r in live + voided)) + 1)
    elif editing and str(editing).isdigit():
        edit_values = next((dict(r) for r in live + voided if r["id"] == int(editing)), None)
    if posted is not None:
        edit_values = dict(edit_values or {"id": None, "letter": "?"})
        for key in routes.ROUTE_FIELDS:
            if key in posted:
                edit_values[key] = posted.get(key)
        rid = posted.get("route_id", type=int) if hasattr(posted, "get") else None
        edit_values["id"] = rid
    # 15a: '+ Add route' is a unit on the page: a new route, route A's origin copied in
    new_route = None
    if live:
        first = live[0]
        new_route = {k: first.get(k) for k in ("origin_country", "origin_loc", "origin_loc_id", "origin_alt",
                                               "service_mode", "incoterm", "service_scope")}
        new_route["id"] = None
        new_route["letter"] = routes.letter((max(r["seq"] for r in live + voided)) + 1)
        # batch 23 (item 7): on a closed case it starts with the case's result
        # and reason; she changes them here when this route is different
        if values.get("status") in routes.RESULTS:
            new_route["result"] = values.get("status")
            if values.get("status") in routes.REASON_RESULTS:
                new_route["lost_reason"] = values.get("closed_reason")
        new_route["picks"] = _route_picks(conn, new_route)
    t = routes.totals(conn, enquiry_id)
    summary = cargo.summary(conn, enquiry_id, values.get("service_mode"))
    for line in summary["lines"] + summary["voided"]:
        line["written"] = cargo.written(line)
        line["routes_label"] = routes.letters_of(line.get("route_ids"), live)
        line["route_list"] = routes.parse_ids(line.get("route_ids")) or []
    card = _cargo_card(conn, enquiry_id, values, live, summary["lines"], t)
    return {
        **card,
        "volume_fact": _volume_fact(t, summary["lines"]),
        "result_on": max((r["result_on"] for r in live if r.get("result") and r.get("result_on")), default=None),
        "live_routes": live, "voided_routes": voided, "route_edit": edit_values, "new_route": new_route,
        "route_errors": route_errors or {}, "route_ships": shipments.by_route(conn, enquiry_id)
        if db._has_table(conn, "shipment") and "route_id" in {r[1] for r in conn.execute("PRAGMA table_info(shipment)")} else {},
        "totals": t, "result_line": routes.result_line(live), "cargo_view": summary,
        "service_scopes": db.get_setting_list(conn, "service_scopes") or routes.DEFAULT_SCOPES,
        "place_data": places.picker_data(conn),
        "routes_title": routes.title(live, name_of) if len(live) > 1 else "",
        "any_project": any(l.get("per_month") or l.get("months") for l in summary["lines"]),
        "any_fcl_lines": any(l.get("container_qty") for l in summary["lines"]),
    }


def _routes_complete(conn, enquiry_id, values) -> bool:
    """The rail's tick on Routes: every live route has both countries and
    locations, and the pickup / delivery address its Service (or, with
    none, its incoterm) needs."""
    live = routes.routes_of(conn, enquiry_id) or [values]
    for r in live:
        state = forms.field_state(r.get("service_mode"), r.get("incoterm"), r.get("service_scope"))
        if not all(r.get(k) for k in ("origin_country", "dest_country", "origin_loc", "dest_loc")):
            return False
        if state["origin_alt"] == "required" and not r.get("origin_alt"):
            return False
        if state["dest_alt"] == "required" and not r.get("dest_alt"):
            return False
    return True


def _volume_fact(t, lines):
    """The header's Volume fact for a project: '6 x 40' / month',
    '24 months · 144 x 40' in all'; None without how often."""
    if not t.get("per_month"):
        return None

    def num(x):
        return str(int(x)) if float(x).is_integer() else "{:.2f}".format(x).rstrip("0").rstrip(".")
    kind = next((l.get("container_type") for l in lines if l.get("container_type") and l.get("per_month")), None)
    unit = " x {}".format(kind) if kind else " shipments"
    value = "{}{} / month".format(num(t["per_month"]), unit)
    sub = ""
    if t.get("project"):
        sub = "{} months · {}{} in all".format(num(t["months"]), num(t["project"]), unit)
    return {"value": value, "sub": sub}


def _save_first() -> bool:
    """The case page's script saving a form before the one she clicked
    (2026-09-28): it asks for JSON instead of a redirect. Since 13c
    (2026-09-30) the clicked small form itself is sent the same way."""
    return request.headers.get("X-Save-First") == "1"


def _refuse(message, back, field="", label="", anchor=""):
    """A small form refused (13c): JSON for the page's script - the form
    stays open with what was typed and the reason beside the field - or,
    without the script, the old flash and redirect."""
    if _save_first():
        return jsonify(ok=False, errors=[{"field": field, "label": label or field.replace("_", " "),
                                          "anchor": anchor, "message": message}]), 422
    warn(message)
    return back


def _numbered_count(conn, case) -> int:
    """N1: the archived mails carrying this case's number that are its own."""
    from .. import case_numbers
    search, _ = case_mail.open_search()
    if search is None:
        return 0
    try:
        return len(case_numbers.keep(conn, case["id"], case_numbers.numbered_mails(search, case["opp_no"]), search))
    except Exception:
        logs.get("web").exception("numbered mails")
        return 0
    finally:
        search.__exit__(None, None, None)


def _split_sentence(others) -> str:
    """' OPP-1280 has it too: its mails up to Oct 4, this case's from Oct 5.'"""
    parts = []
    for o in others:
        if o["same_day"]:
            parts.append("{} has it too, registered the same day.".format(o["opp_no"]))
        else:
            parts.append("{} has it too: {}, {}.".format(o["opp_no"], o["theirs"], o["mine"]))
    return (" " + " ".join(parts)) if parts else ""


def _shared_of(conn, enquiry_id, key) -> list:
    """N1 item 3: the other cases that have this key, with the split."""
    from .. import case_numbers
    if not key:
        return []
    return case_numbers.shared_with(enquiry_id, case_numbers.case_owners(conn, enquiry_id, key=key))


def _subject_lines(conn, enquiry_id) -> list:
    """The attached subjects, one line per subject text (2026-09-28):
    attaching a mail adds two rows with the same text (the conversation and
    the subject key). A line is live while any of its rows is; struck-out
    lines are offered in More to put back. [{text, ids, live}]"""
    lines, by_text = [], {}
    # v33: a subject attached with the registered subject's own text folds
    # into the registered line (its rows stay, so its mail still matches)
    registered = conn.execute("SELECT source_subject FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    registered = " ".join(((registered["source_subject"] if registered else "") or "").split()).casefold()
    for row in conn.execute("SELECT id, kind, value, note, voided FROM enquiry_thread WHERE enquiry_id = ? "
                            "AND source <> 'register' ORDER BY added_on, id", (enquiry_id,)):
        text = " ".join((row["note"] or "").split())
        key = text.casefold() if text else "#{}".format(row["id"])
        line = by_text.get(key)
        if line is None:
            line = by_text[key] = {"text": text, "ids": [], "live": False, "shared": []}
            lines.append(line)
        line["ids"].append(row["id"])
        line["live"] = line["live"] or not row["voided"]
        if row["kind"] == "thread_key" and not row["voided"] and not line["shared"]:
            line["shared"] = _shared_of(conn, enquiry_id, row["value"])
    for line in lines:
        line["registered"] = bool(registered) and line["text"].casefold() == registered
    return lines


def _place_title(conn, loc_id, key) -> str:
    """'Aspers, PA' for a master row; shipments.place_title, one rule."""
    from .. import shipments
    return shipments.place_title(conn, loc_id, key)


def _day(value) -> str:
    return dates.month_day(value)


def _case_context(conn, values, row, today, mail_count=None) -> dict:
    """Everything the case page's header, tiles and rail show that the
    form fields do not (2026-09-24; the facts row, the rail
    states and the next step depend on the status since v2b). Read only."""
    from .. import status as status_mod, tracking
    from . import forms

    origin = _place_title(conn, values.get("origin_loc_id"), values.get("origin_loc"))
    dest = _place_title(conn, values.get("dest_loc_id"), values.get("dest_loc"))
    first_route = routes.route_a(conn, values["id"]) if values.get("id") else None
    if first_route is not None and len(first_route.get("dest_places") or []) > 1:
        # batch 23: route A's destination is several places, any of them will do
        dest = routes.place(first_route, "dest", lambda loc_id, key: _place_title(conn, loc_id, key))
    if origin and dest:
        title = "{} to {}".format(origin, dest)
    elif origin or dest:
        title = origin or dest
    else:
        title = values.get("opp_no") or ""

    mode = values.get("service_mode") or ""
    qty, ctype = values.get("container_qty"), values.get("container_type")
    if mode and qty and ctype:
        mode_pill = "{} · {} x {}".format(mode, int(qty) if float(qty).is_integer() else qty, ctype)
    else:
        mode_pill = mode

    received = values.get("received_on") or ""
    quoted = values.get("quoted_on") or values.get("quoted_on_any")
    to_quote = tracking.business_days_between(received, quoted) if quoted else None
    last_fu = conn.execute(
        "SELECT seq, at FROM status_event WHERE enquiry_id = ? AND kind = 'followup' "
        "AND voided = 0 ORDER BY at DESC, id DESC LIMIT 1", (values["id"],)).fetchone()
    fu_ago = tracking.business_days_between(last_fu["at"], today) if last_fu else None
    rung = tracking.ladder(conn, row, today) if row is not None else {"steps": [], "next": ""}
    # batch 21: a direct customer is never chased - no ladder, no draft
    direct = tracking.no_followups(values.get("requester_type"))
    if direct:
        rung = {"steps": [], "next": ""}
    from .. import wait
    waiting = wait.live(conn, values["id"]) if values.get("status") == "New" else None
    if waiting is not None and not values.get("handled_by"):
        # WR1: she asked to chase the requester for details - whoever they are
        rung = wait.ladder(conn, waiting, today)
        direct = False

    vendor_quote = conn.execute(
        "SELECT v.their_ref, v.replied_on, v.asked_on, p.key AS vendor_key FROM vendor_request v "
        "JOIN party p ON p.id = v.vendor_party_id WHERE v.enquiry_id = ? AND v.voided = 0 "
        "AND (v.quoted = 1 OR v.machine_status = 'Quoted') "
        "ORDER BY COALESCE(v.replied_on, v.asked_on, '') DESC, v.id DESC LIMIT 1",
        (values["id"],)).fetchone()
    counts = conn.execute(
        "SELECT COUNT(*) AS asked, SUM(CASE WHEN replied_on IS NOT NULL THEN 1 ELSE 0 END) AS replied "
        "FROM vendor_request WHERE enquiry_id = ? AND voided = 0", (values["id"],)).fetchone()

    party_id = values.get("requester_party_id")
    stats = conn.execute("SELECT total_enquiries, won FROM v_party_stats WHERE id = ?",
                         (party_id,)).fetchone() if party_id else None
    contacts = [r["name"] for r in conn.execute(
        "SELECT name FROM contact WHERE party_id = ? AND name IS NOT NULL AND TRIM(name) <> '' "
        "ORDER BY is_primary DESC, id LIMIT 3", (party_id,))] if party_id else []
    earlier = conn.execute(
        "SELECT id, opp_no, received_on, service_mode, dest_loc, dest_country, status "
        "FROM v_enquiry WHERE requester_party_id = ? AND id <> ? "
        "ORDER BY received_on DESC, id DESC LIMIT 5", (party_id, values["id"])).fetchall() \
        if party_id else []
    latest_suggestion = conn.execute(
        "SELECT proposed_status, proposed_date, state, decided_at FROM suggestion "
        "WHERE enquiry_id = ? ORDER BY id DESC LIMIT 1", (values["id"],)).fetchone()

    # 'since': the latest live event that says what the case is - a status or
    # a requote - not last_event_on, which every note and follow-up moves
    since = conn.execute(
        "SELECT at FROM status_event WHERE enquiry_id = ? AND voided = 0 AND {} "
        "ORDER BY {} LIMIT 1".format(status_mod.STATUS_KIND_SQL, status_mod.LATEST_EVENT_ORDER),
        (values["id"],)).fetchone()
    closed = values.get("closed_on") or values.get("closed_on_any") \
        if values.get("status") in status_mod.CLOSED_STATUSES else None

    state = forms.field_state(values.get("service_mode"), values.get("incoterm"), values.get("service_scope"))
    ticks = {
        "request": bool(received and party_id),
        "cargo": bool(mode) and (mode != "FCL" or bool(ctype and qty)),
        # v33: every live route has its places, and the address its incoterm needs
        "routes": _routes_complete(conn, values["id"], values),
    }
    asked, replied = counts["asked"] or 0, counts["replied"] or 0
    st = values.get("status") or ""
    fus_total = int(values.get("fu_count_total") or 0)

    # the rail's short states: a tick says the data is complete; these say
    # where the case stands
    live_n = conn.execute("SELECT COUNT(*) FROM enquiry_route WHERE enquiry_id = ? AND voided = 0",
                          (values["id"],)).fetchone()[0] if db._has_table(conn, "enquiry_route") else 1
    states = {
        "routes": str(live_n) if live_n > 1 else "",
        "vendors": TEXT["state_asked"].format(n=asked),
        "status": st,
        "followups": TEXT["state_closed"] if closed
                     else TEXT["state_direct"] if direct
                     else (TEXT["state_fu_sent"].format(n=last_fu["seq"]) if last_fu else TEXT["state_none"]),
        "mail": TEXT["state_mails"].format(n=mail_count) if mail_count is not None else "",
    }

    # the facts row, by status
    def tile(label, value, sub="", mono=False):
        return {"label": label, "value": value, "sub": sub, "mono": mono}

    def working(n):
        return TEXT["working_days"].format(n=n) if n is not None else ""

    received_tile = tile(TEXT["fact_received"], _day(received) or "-",
                         dt.date.fromisoformat(received).strftime("%a") if _day(received) else "")
    quoted_tile = tile(TEXT["fact_quoted"], _day(quoted) if quoted else TEXT["fact_not_yet"],
                       working(to_quote) if quoted else "")
    if vendor_quote:
        vq_tile = tile(TEXT["fact_vendor_quote"], vendor_quote["their_ref"] or "-",
                       "{} · {}".format(vendor_quote["vendor_key"],
                                        _day(vendor_quote["replied_on"] or vendor_quote["asked_on"])), mono=True)
    else:
        vq_tile = tile(TEXT["fact_vendor_quote"], TEXT["asked_replied"].format(a=asked, r=replied))
    nudge = tracking.case_nudge(conn, row, today) if row is not None else {"text": "-", "hint": ""}
    if st == "Quoted":
        last_fu_tile = tile(TEXT["fact_last_fu"],
                            "FU{} {}".format(last_fu["seq"], _day(last_fu["at"])) if last_fu else TEXT["none_yet"],
                            TEXT["working_days_ago"].format(n=fu_ago) if last_fu and fu_ago is not None else "")
        if values.get("handled_by"):
            # another office quoted it (v31): no ladder to climb here
            next_tile = tile(TEXT["fact_next"], nudge["text"], nudge["hint"])
        elif direct:
            next_tile = tile(TEXT["fact_next"], TEXT["no_fu_short"], TEXT["direct_customer"])
        elif rung.get("next"):
            next_tile = tile(TEXT["fact_next"], "{} {}".format(rung["next"], _day(rung["next_due"])),
                             TEXT["in_working_days"].format(n=rung["days_left"]) if rung["days_left"] else TEXT["due_now"])
        else:
            next_tile = tile(TEXT["fact_next"], "-")
        facts = [received_tile, quoted_tile, last_fu_tile, next_tile, vq_tile]
    elif st == "Won":
        after = tracking.business_days_between(quoted, closed) if quoted and closed else None
        won_tile = tile(TEXT["fact_won"], _day(closed) or "-",
                        TEXT["after_quote"].format(n=after) if after is not None else "")
        label = status_mod.RATE_USED_LABELS.get(values.get("rate_used"))
        awarded = conn.execute("SELECT key FROM party WHERE id = ?",
                               (values.get("awarded_vendor_id"),)).fetchone() if values.get("awarded_vendor_id") else None
        rate_tile = tile(TEXT["fact_rate_used"], label or TEXT["fact_not_answered"],
                         awarded["key"] if awarded else "")
        facts = [received_tile, quoted_tile, won_tile, rate_tile, vq_tile]
    elif st in status_mod.REASON_STATUSES:
        quoted_tile = tile(TEXT["fact_quoted"], _day(quoted) if quoted else TEXT["fact_not_quoted"],
                           working(to_quote) if quoted else "")
        closed_tile = tile(st, _day(closed) or "-", values.get("closed_reason") or "")
        fus_tile = tile(TEXT["fact_fus_sent"], str(fus_total),
                        "FU{} {}".format(last_fu["seq"], _day(last_fu["at"])) if last_fu else "")
        facts = [received_tile, quoted_tile, closed_tile, fus_tile, vq_tile]
    else:                                                   # New, a re-quote round too
        age = tracking.business_days_between(received, today) if received else None
        age_tile = tile(TEXT["fact_age"], working(age) or "-", TEXT["since_day"].format(d=_day(received)) if received else "")
        asked_tile = tile(TEXT["fact_rate_requests"], TEXT["asked_replied"].format(a=asked, r=replied))
        if not vendor_quote:
            vq_tile = tile(TEXT["fact_vendor_quote"], TEXT["none_yet"])
        next_tile = tile(TEXT["fact_next"], nudge["text"], nudge["hint"])
        facts = [received_tile, age_tile, asked_tile, vq_tile, next_tile]

    # due now (or overdue): the ladder's next step with no working days
    # left - the Next tile, the rail's title and the strip's rung turn
    # --due (2026-09-25); a step still waiting keeps its colours
    due = bool(rung.get("next")) and not rung.get("days_left") and not values.get("handled_by")
    if st == "Quoted" and due:
        facts[3]["due"] = True

    # the condensed header's one line (v2e): requester · mode · incoterm ·
    # origin -> dest, missing parts left out, the arrow only with both
    lane = [c for c in (values.get("origin_country"), values.get("dest_country")) if c]
    condensed_line = " · ".join(part for part in (
        values.get("requester_key"), mode, values.get("incoterm"),
        " → ".join(lane) if len(lane) == 2 else (lane[0] if lane else "")) if part)
    # the same line in parts (2026-09-25): the requester in a box, the mode
    # as its outline label, the incoterm and the lane in dark text
    condensed = {"requester": values.get("requester_key") or "", "mode": mode or "",
                 "mode_tone": MODE_TONES.get(values.get("service_mode") or "", "grey"),
                 "incoterm": values.get("incoterm") or "",
                 "lane": " → ".join(lane) if len(lane) == 2 else (lane[0] if lane else "")}

    # the rail's Next step block
    if (st == "Quoted" or waiting is not None) and not values.get("handled_by") and not direct:
        next_step = {"kind": "ladder", "title": "", "sub": ""}
    elif st == "Quoted" and direct:
        next_step = {"kind": "open", "title": TEXT["dc_no_followups"], "sub": ""}
    elif closed:
        next_step = {"kind": "closed", "title": TEXT["closed_next"].format(s=st, d=_day(closed)),
                     "sub": values.get("closed_reason") or ""}
    else:
        next_step = {"kind": "open", "title": nudge["text"], "sub": nudge["hint"]}

    return {
        "title": title, "mode_pill": mode_pill, "to_quote": to_quote,
        "last_fu": last_fu, "fu_ago": fu_ago, "ladder": rung,
        "vendor_quote": vendor_quote, "asked": asked, "replied": replied,
        "stats": stats, "contacts": contacts, "earlier": earlier,
        "latest_suggestion": latest_suggestion, "ticks": ticks, "states": states,
        "facts": facts, "next_step": next_step, "due": due, "condensed_line": condensed_line,
        "condensed": condensed,
        "since": since["at"] if since else values.get("last_event_on"),
        "closed": closed, "direct": direct, "waiting": waiting,
    }




def _vendor_rates(conn, enquiry_id) -> dict:
    """Q2a: the newest rates for the Rate requests card. Q2b: per request -
    {vendor_request id: [the newest rates of each route it covers, and
    those that name no route]}."""
    from .. import routes as routes_mod, site_rates
    card = site_rates.card(conn, enquiry_id, _today())
    if not card:
        return {}
    live = routes_mod.routes_of(conn, enquiry_id)
    return {v["id"]: site_rates.for_request(card, v, live) for v in conn.execute(
        "SELECT id, vendor_party_id, route_ids FROM vendor_request WHERE enquiry_id = ? AND voided = 0", (enquiry_id,))}


def _keep_dropped(upload) -> str:
    """RQ2: a .msg or .eml dropped into the Read quote panel, kept as the
    site's are (data\\site-reads\\app-<time>\\<name>, never deleted);
    its path. Another kind of file is refused before anything is read."""
    from .. import read_quote, site_read
    name = Path(upload.filename or "").name
    if Path(name).suffix.lower() not in site_read.MAIL_FILES:
        raise read_quote.ReadQuoteError("Drop a mail file (.msg from Outlook, or .eml); {} is not one.".format(
            name or "that"))
    where = site_read.root() / "app-{}".format(dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    where.mkdir(parents=True, exist_ok=True)
    path = where / site_read._safe(name)
    upload.save(str(path))
    if path.stat().st_size == 0:
        raise read_quote.ReadQuoteError("{} is empty.".format(name))
    return str(path)


def _read_quote_ctx(conn, case, db_path) -> dict:
    """Q2d: {'gates': {request id: gate}, 'panel': the open panel or None,
    'reading': the reading just made (?qr=) or None}."""
    from .. import case_mail, read_quote
    out = {"gates": {}, "panel": None, "reading": None}
    if case is None:
        return out
    try:
        for req in conn.execute("SELECT v.*, p.key AS vendor_key FROM vendor_request v JOIN party p "
                                "ON p.id = v.vendor_party_id WHERE v.enquiry_id = ? AND v.voided = 0", (case["id"],)):
            out["gates"][req["id"]] = read_quote.gate(conn, case, req, db_path)
        wanted = request.args.get("read", type=int)
        if wanted and out["gates"].get(wanted, {}).get("show"):
            req = read_quote._request(conn, wanted)
            search, info = case_mail.open_search()
            try:
                mails = [dict(m, mail=None) for m in read_quote.panel_mails(conn, search, req)] if search else []
            finally:
                if search is not None:
                    search.__exit__(None, None, None)
            out["panel"] = {"request_id": wanted, "vendor": req["vendor_key"], "mails": mails,
                            "archive": search is not None, "today": _today(), "gate": out["gates"][wanted],
                            "paste_max": read_quote.PASTE_MAX}
        reading = request.args.get("qr", type=int)
        if reading:
            found = read_quote.view(conn, reading)
            if found is not None and conn.execute("SELECT 1 FROM quote_read WHERE id = ? AND enquiry_id = ?",
                                                  (reading, case["id"])).fetchone():
                out["reading"] = found
    except Exception:                                    # the row works without it
        logs.get("web").exception("read quote panel")
    return out


def _timeline_items(conn, enquiry_id) -> list:
    """D7b: the case page's Timeline - its live events and the changes made
    on the team site (site_edit.timeline, from change_log), newest first:
    [('event', row) | ('edit', item)]."""
    from .. import site_edit
    rows = [("event", e) for e in conn.execute(
        "SELECT * FROM status_event WHERE enquiry_id = ? AND voided = 0 ORDER BY at DESC, id DESC", (enquiry_id,))]
    edits = [("edit", dict(i, headline=site_edit.headline(i))) for i in site_edit.timeline(conn, enquiry_id)]
    if not edits:
        return rows
    merged = rows + edits
    # by day, newest first; on the same day a change from the site above the events
    merged.sort(key=lambda r: (r[1]["at"][:10], 1 if r[0] == "edit" else 0), reverse=True)
    return merged


def _split_tags(value):
    if not value:
        return []
    return [t for t in str(value).split(";") if t]


def _timeline(conn: sqlite3.Connection, party_id: int) -> list:
    """One thread of everything that happened with this party."""
    rows = []
    for row in conn.execute(
        "SELECT at, type, summary FROM party_event WHERE party_id = ?",
        (party_id,)
    ):
        rows.append({"at": row["at"], "kind": row["type"],
                     "text": row["summary"] or "", "link": None})
    for row in conn.execute(
        "SELECT id, opp_no, received_on, commodity FROM enquiry "
        "WHERE requester_party_id = ?", (party_id,)
    ):
        rows.append({"at": row["received_on"], "kind": "enquiry",
                     "text": "{} {}".format(row["opp_no"], row["commodity"] or ""),
                     "link": row["id"]})
    for row in conn.execute(
        "SELECT v.asked_on, v.replied_on, e.id, e.opp_no FROM vendor_request v "
        "JOIN enquiry e ON e.id = v.enquiry_id WHERE v.vendor_party_id = ?",
        (party_id,)
    ):
        if row["asked_on"]:
            rows.append({"at": row["asked_on"], "kind": "asked",
                         "text": "asked about {}".format(row["opp_no"]),
                         "link": row["id"]})
        if row["replied_on"]:
            rows.append({"at": row["replied_on"], "kind": "replied",
                         "text": "replied on {}".format(row["opp_no"]),
                         "link": row["id"]})
    rows = [r for r in rows if r["at"]]
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows[:200]
