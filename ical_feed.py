#!/usr/bin/env python3
"""Render the parsed calendar as an RFC 5545 iCalendar feed.

Subscribable from iOS/macOS Calendar, Google Calendar, Outlook, etc. Two things
matter for a feed people subscribe to rather than import once:

* UIDs must be stable across refreshes, or every poll duplicates every event.
  They're derived from (start date, title), so re-crawling the same PDF yields
  byte-identical UIDs.
* DTSTAMP must only change when the data changes -- not per request -- or
  clients treat untouched events as modified. It's pinned to the district's own
  Last-Modified, so re-crawling an unchanged PDF yields a byte-identical feed.

Per-day entries are collapsed back into one multi-day event per contiguous run
of the same title, so a phone shows "Winter Recess, Dec 21 - Jan 4" once instead
of fifteen separate all-day entries.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from email.utils import parsedate_to_datetime

PRODID = "-//hays-usd-489-calendar//district closures//EN"
UID_NAMESPACE = "usd489-calendar"

TAG_LABELS = {
    "holiday": "No School",
    "inservice": "No School — Teacher Inservice",
    "conf": "No School — Parent/Teacher Conferences",
    "recess": "No School — Recess",
    "milestone": "School Milestone",
}


def _esc(text: str) -> str:
    """Escape a TEXT value per RFC 5545 section 3.3.11."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> list[bytes]:
    """Fold a content line to <=75 octets, continuations prefixed with a space."""
    limit = 75
    out: list[bytes] = []
    cur = b""
    for ch in line:
        enc = ch.encode("utf-8")
        if len(cur) + len(enc) > limit:
            out.append(cur)
            cur = b" " + enc
        else:
            cur += enc
    out.append(cur)
    return out


def _spans(events: list[dict]) -> list[dict]:
    """Collapse per-day events into contiguous runs sharing the same title.

    Groups by title so that overlapping different titles on one day (the PDF
    lists Jan 4 as both Winter Recess and Teacher Inservice) stay separate.
    """
    by_title: dict[str, list[dict]] = {}
    for ev in events:
        by_title.setdefault(ev["title"], []).append(ev)

    spans: list[dict] = []
    for title, group in by_title.items():
        group.sort(key=lambda e: e["date"])
        run_start = run_end = None
        run_ev = None
        for ev in group:
            day = dt.date.fromisoformat(ev["date"])
            if run_end is not None and day == run_end + dt.timedelta(days=1):
                run_end = day
                continue
            if run_ev is not None:
                spans.append({"start": run_start, "end": run_end, "event": run_ev})
            run_start = run_end = day
            run_ev = ev
        if run_ev is not None:
            spans.append({"start": run_start, "end": run_end, "event": run_ev})

    spans.sort(key=lambda s: (s["start"], s["event"]["title"]))
    return spans


def _uid(start: dt.date, title: str) -> str:
    digest = hashlib.sha1(f"{start.isoformat()}|{title}".encode()).hexdigest()[:16]
    return f"{digest}@{UID_NAMESPACE}"


def _dtstamp(meta: dict) -> str:
    """UTC timestamp in basic format, pinned to when the *data* last changed.

    Prefers the district's own Last-Modified over our crawl time, so re-crawling
    an unchanged PDF produces a byte-identical feed and subscribers see nothing
    as modified.
    """
    meta = meta or {}
    stamp = None

    http_date = meta.get("pdfLastModified")
    if http_date:
        try:
            stamp = parsedate_to_datetime(http_date)
        except (TypeError, ValueError):
            stamp = None

    if stamp is None and meta.get("retrievedAt"):
        try:
            stamp = dt.datetime.fromisoformat(meta["retrievedAt"])
        except ValueError:
            stamp = None

    if stamp is None:
        stamp = dt.datetime.now(dt.timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def render_ics(data: dict, *, calendar_name: str | None = None) -> bytes:
    """Build the iCalendar document for a parsed calendar payload."""
    meta = data.get("meta") or {}
    school_year = data.get("schoolYear", "")
    name = calendar_name or f"Hays USD 489 Closures {school_year}".strip()
    stamp = _dtstamp(meta)

    desc_bits = ["No-school days and milestones from the USD 489 district calendar."]
    if meta.get("pdfUrl"):
        desc_bits.append(f"Source PDF: {meta['pdfUrl']}")
    for note in data.get("notes", []):
        desc_bits.append(note)
    cal_desc = " ".join(desc_bits)

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_esc(name)}",
        f"X-WR-CALDESC:{_esc(cal_desc)}",
        # The district publishes the calendar once and rarely touches it, so
        # there is no value in subscribers polling aggressively.
        "REFRESH-INTERVAL;VALUE=DURATION:P1D",
        "X-PUBLISHED-TTL:P1D",
    ]

    for span in _spans(data.get("events", [])):
        ev = span["event"]
        start: dt.date = span["start"]
        end: dt.date = span["end"]
        tag = ev.get("tag") or ""
        label = TAG_LABELS.get(tag, "No School" if ev.get("noSchool") else "Note")

        summary = ev["title"]
        if ev.get("noSchool") and "no school" not in summary.lower():
            summary = f"No School — {summary}"

        lines += [
            "BEGIN:VEVENT",
            f"UID:{_uid(start, ev['title'])}",
            f"DTSTAMP:{stamp}",
            # All-day events use DATE values; DTEND is exclusive.
            f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(end + dt.timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_esc(summary)}",
            f"DESCRIPTION:{_esc(label)}",
            # School closures shouldn't make the subscriber look busy.
            "TRANSP:TRANSPARENT",
            "CLASS:PUBLIC",
        ]
        if tag:
            lines.append(f"CATEGORIES:{_esc(tag.upper())}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")

    folded: list[bytes] = []
    for line in lines:
        folded.extend(_fold(line))
    return b"\r\n".join(folded) + b"\r\n"
