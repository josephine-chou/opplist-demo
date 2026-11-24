"""The documents of the site snapshot that OppList does not push: the ones
the team site writes itself. On a real installation people make them by
working on the site; the demo starts with a few so that the panels have
something to show. All of it is invented and derived from the pushed
documents and the date only, so the same demo gives the same snapshot.

- users: the three people the site can be viewed as;
- notes: two notes of a colleague, one on a case with a reminder;
- one Won that a colleague sent from the site and that waits for the
  administrator, with the text they pasted (actions + evidence): it is
  dated after OppList's last pull, so the site lays it over the lists;
- demo/readAnswer: the reading OppList made from a recorded answer (see
  site_snapshot.demo_reading), kept apart so the site's stand-in can answer
  a Read quote request with it.
"""

from __future__ import annotations

import datetime as dt

from . import branding

#: The three people the site can be viewed as. `owner` is the person's name
#: in OppList's Handled by.
PEOPLE = (
    {"name": "Iris Chen", "owner": "Iris", "role": "admin", "report": True, "costs": True},
    {"name": "Victor Lang", "owner": "Victor", "role": "member", "report": True, "costs": True},
    {"name": "Ava Tran", "owner": "Ava", "role": "member", "report": False, "costs": False},
)
APP = "demo"
EVIDENCE_TEXT = ("Hello Victor,\n\nMy customer accepts the offer as quoted. Kindly go ahead with the booking for next "
                 "week's sailing and send me the booking confirmation once you have it.\n\nBest regards")


def address(name: str) -> str:
    return "{}@{}".format(name.lower().replace(" ", "."), branding.INTERNAL_DOMAIN)


def users(today: dt.date) -> dict:
    made = dt.datetime.combine(today - dt.timedelta(days=40), dt.time(10, 0))
    out = {}
    for person in PEOPLE:
        email = address(person["name"])
        out["users/" + email] = dict(person, email=email, active=True, createdAt=made, updatedAt=made,
                                     updatedBy=address(PEOPLE[0]["name"]))
    return out


def _rows(pushed: dict) -> list:
    return list((pushed.get("lists/inquiries") or {}).get("rows") or [])


def notes(pushed: dict, today: dt.date) -> dict:
    """Two notes by the member with Costs: one free, one on the newest quoted
    case with a reminder for today (it shows on that case and on the board)."""
    who = PEOPLE[1]
    quoted = sorted((r for r in _rows(pushed) if r.get("st") == "Quoted"), key=lambda r: r["opp"])
    at = dt.datetime.combine(today - dt.timedelta(days=1), dt.time(16, 10))
    base = {"done": False, "by": address(who["name"]), "name": who["name"]}
    out = {"notes/demo-note-1": dict(base, text="Ask the trucker in Savannah for next month's chassis rates",
                                     opp="", remind="", at=at, updatedAt=at)}
    if quoted:
        out["notes/demo-note-2"] = dict(base, text="Call them about the quote before the validity runs out",
                                        opp=quoted[-1]["opp"], remind=today.isoformat(), at=at, updatedAt=at)
    return out


def pending_won(pushed: dict, today: dt.date, after: dt.datetime) -> dict:
    """A Won sent from the site two minutes after OppList's last pull, on the
    oldest quoted case that is nobody's question right now."""
    who = PEOPLE[1]
    free = sorted((r for r in _rows(pushed) if r.get("st") == "Quoted" and not r.get("pend") and not r.get("pendingClose")
                   and r.get("fu") in ("FU1", "FU2")), key=lambda r: r["opp"])
    if not free:
        return {}
    opp, email = free[0]["opp"], address(who["name"])
    at = after + dt.timedelta(minutes=2)
    return {
        "actions/demo-won-1": {"type": "close", "opp": opp, "by": email, "name": who["name"], "owner": who["owner"],
                               "app": APP, "date": today.isoformat(), "result": "Won", "reason": "", "rateFrom": "",
                               "evidence": "pasted", "at": at},
        "evidence/demo-won-1": {"images": [], "text": EVIDENCE_TEXT, "by": email, "name": who["name"], "at": at},
    }


def read_answer(pushed: dict) -> dict:
    """demo/readAnswer: the recorded reading as a draft the stand-in can copy."""
    drafts = sorted(p for p in pushed if p.startswith("rateDrafts/"))
    if not drafts:
        return {}
    draft = {k: v for k, v in pushed[drafts[0]].items() if k not in ("opp", "vendor", "readAt", "status", "savedAs", "taken")}
    return {"demo/readAnswer": {"draft": draft, "from": drafts[0]}}


def docs(pushed: dict, today: dt.date, seed: int, pushed_at: dt.datetime) -> dict:
    """{path: fields} to add to the pushed documents."""
    out = {}
    out.update(users(today))
    out.update(notes(pushed, today))
    out.update(pending_won(pushed, today, pushed_at))
    out.update(read_answer(pushed))
    return out
