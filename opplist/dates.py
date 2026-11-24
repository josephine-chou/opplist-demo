"""Dates as the pages show them, month first (2026-09-25, 11c): 'Aug 25',
'Tue Aug 25', 'Sep 20, 2026'. The one server-side helper; app.js has its
twin (dayMonth). ISO dates, relative ages, date inputs, the Outlook drafts
and the data exports do not use it."""

from __future__ import annotations

import datetime as dt


def as_date(value):
    """A date from a date, a datetime or an ISO string ('2026-09-24',
    '2026-09-24 10:12'); None when it is not one."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def month_day(value, weekday: bool = False, year: bool = False) -> str:
    """'Aug 25'; with weekday 'Tue Aug 25'; with year 'Sep 20, 2026';
    '' when the value is not a date."""
    d = as_date(value)
    if d is None:
        return ""
    text = "{} {}".format(d.strftime("%b"), d.day)
    if weekday:
        text = "{} {}".format(d.strftime("%a"), text)
    if year:
        text = "{}, {}".format(text, d.year)
    return text
