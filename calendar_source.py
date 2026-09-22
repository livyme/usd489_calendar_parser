#!/usr/bin/env python3
"""Crawl usd489.com for the district calendar PDF and parse it into structured data.

Importable by the web app, and runnable to refresh the baked-in seed cache:

    uv run calendar_source.py --out seed/calendar.json
    uv run calendar_source.py --out seed/calendar.json --save-pdf seed/district-calendar.pdf

The parser reads the legend column on the right side of the calendar PDF (the
"17-NO SCHOOL Teacher Inservice" list), not the month grids -- the legend is
plain text in reading order, so it survives layout changes far better. The grids
are bare day numbers whose meaning is carried only by colour fill.
"""

from __future__ import annotations

import argparse
import calendar as calmod
import datetime as dt
import io
import json
import re
import sys
import urllib.request
from pathlib import Path

HOME_URL = "https://www.usd489.com/"

# usd489.com sits behind a Fastly bot challenge that fires on any User-Agent
# claiming to be a browser ("Mozilla/5.0 ..."), which gets served a JS challenge
# page instead of content. An honest, self-identifying agent is served the real
# page -- and robots.txt allows "/" for "*" (only /api/ is disallowed), so
# identifying ourselves is the well-behaved path, not a workaround.
USER_AGENT = "usd489-calendar-bot/1.0 (+personal school-closure calendar)"

MONTH_NUM = {
    name: i + 1
    for i, name in enumerate(
        "JANUARY FEBRUARY MARCH APRIL MAY JUNE JULY AUGUST "
        "SEPTEMBER OCTOBER NOVEMBER DECEMBER".split()
    )
}

# "17-NO SCHOOL Teacher Inservice", "29-30-NO SCHOOL P/T Conf",
# "15-19 -NO SCHOOL Spring Break", "5-Students return to school"
DAYSPEC_RE = re.compile(r"^(\d{1,2})(?:\s*[-\u2013]\s*(\d{1,2}))?\s*[-\u2013]\s*(\S.*)$")

# Right-hand legend column starts here; the third month grid ends around x=400.
SIDEBAR_X = 405.0

HTTP_TIMEOUT = 45


class CalendarError(RuntimeError):
    """Raised when the site or PDF no longer looks the way we expect."""


# --------------------------------------------------------------------------
# crawling
# --------------------------------------------------------------------------
def fetch_text(url: str, *, timeout: int = HTTP_TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def discover_calendar_link(page_html: str) -> tuple[str, str]:
    """Find the district calendar link in the site nav.

    The nav ships inside the page as an escaped JSON blob, so unescape it and
    collect nav entries whose name mentions a calendar, then score them.
    """
    blob = page_html.replace('\\"', '"').replace("\\/", "/")
    candidates: dict[tuple[str, str], None] = {}

    for m in re.finditer(r'"name":"([^"]{0,160})"', blob):
        name = " ".join(m.group(1).split())
        if "calend" not in name.lower():
            continue
        tail = blob[m.end() : m.end() + 600]
        um = re.search(r'"url":"(https?://[^"]+)"', tail)
        if um:
            candidates[(name, um.group(1))] = None

    # Fall back to any direct PDF link that looks calendar-ish.
    for m in re.finditer(r'https?://[^\s"\'<>]+\.pdf[^\s"\'<>]*', blob, re.I):
        if "calend" in m.group(0).lower():
            candidates[("(direct PDF link)", m.group(0))] = None

    if not candidates:
        raise CalendarError(
            "Could not find a calendar link on the homepage. "
            "The site nav may have changed -- check discover_calendar_link()."
        )

    def score(item: tuple[str, str]) -> tuple:
        name, _url = item
        low = name.lower()
        s = 0
        if "district" in low:
            s += 10
        if re.search(r"\bboe\b|board", low):
            s -= 10  # board-of-education meeting calendar, not the school year
        if low.strip() in ("calendars", "calendar"):
            s -= 3  # a section header, not the item itself
        years = re.search(r"(20\d{2})\s*[-\u2013]\s*(20\d{2})", name)
        return (s, int(years.group(1)) if years else 0, -len(name))

    return max(candidates, key=score)


def download_pdf(url: str) -> tuple[bytes, str, str | None]:
    """Follow the shortlink to the PDF. Returns (bytes, final_url, last_modified)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
        final_url = resp.geturl()
        ctype = resp.headers.get("Content-Type", "")
        last_mod = resp.headers.get("Last-Modified")

    if "pdf" not in ctype.lower() and not body.startswith(b"%PDF"):
        raise CalendarError(
            f"Expected a PDF at {final_url} but got Content-Type {ctype!r}. "
            "The district may have switched the calendar to a non-PDF page."
        )
    return body, final_url, last_mod


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
def _sidebar_lines(pdf_bytes: bytes) -> tuple[list[str], str]:
    """Return the legend-column lines (top to bottom) and the full page text."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[0]
        words = page.extract_words()
        full_text = page.extract_text() or ""

    groups: dict[int, list[dict]] = {}
    for w in words:
        if w["x0"] <= SIDEBAR_X:
            continue
        groups.setdefault(round(w["top"] / 4), []).append(w)

    lines = []
    for key in sorted(groups):
        row = sorted(groups[key], key=lambda w: w["x0"])
        lines.append(" ".join(w["text"] for w in row).strip())
    return lines, full_text


def classify(title: str) -> str:
    t = title.lower()
    if "p/t" in t or "conf" in t:
        return "conf"
    if "inservice" in t or "workday" in t:
        return "inservice"
    if "recess" in t or "break" in t:
        return "recess"
    if any(k in t for k in ("first day", "last day", "graduation", "students return")):
        return "milestone"
    return "holiday"


# Tags that describe people being at school. A weekend day inside one of these
# spans is a transcription artefact of the legend, not a real entry.
WEEKDAY_ONLY_TAGS = {"conf", "inservice"}


def _contiguous(days: list[dt.date]) -> list[list[dt.date]]:
    """Split a sorted date list into runs of consecutive days."""
    runs: list[list[dt.date]] = []
    for day in days:
        if runs and (day - runs[-1][-1]).days == 1:
            runs[-1].append(day)
        else:
            runs.append([day])
    return runs


def clean_title(raw: str) -> tuple[str, bool]:
    """Split the explicit 'NO SCHOOL' prefix off the descriptive title."""
    text = " ".join(raw.split())
    text = text.replace("---", "\u2014").replace("--", "\u2013")
    m = re.match(r"^NO\s+SCHOOL\b[\s:\u2013\u2014-]*(.*)$", text, re.I)
    if m:
        return (m.group(1).strip() or "No School"), True
    return text, False


def parse_pdf(pdf_bytes: bytes) -> dict:
    """Parse the calendar PDF into {schoolYear, events, notes}."""
    lines, full_text = _sidebar_lines(pdf_bytes)

    ym = re.search(r"(20\d{2})\s*[-\u2013]\s*(20\d{2})", full_text)
    if not ym:
        raise CalendarError("Could not read the school year (e.g. '2026-2027') from the PDF.")
    year1, year2 = int(ym.group(1)), int(ym.group(2))

    events: list[dict] = []
    notes: list[str] = []
    current_month: int | None = None
    last_event: dict | None = None

    for line in lines:
        text = " ".join(line.split())
        if not text:
            continue

        upper = text.upper()
        if upper in MONTH_NUM and text.isupper():
            current_month = MONTH_NUM[upper]
            last_event = None
            continue

        m = DAYSPEC_RE.match(text)
        if m and current_month:
            d1 = int(m.group(1))
            d2 = int(m.group(2)) if m.group(2) else d1
            title, no_school = clean_title(m.group(3))

            # Aug-Dec belong to the first year of the span, Jan-Jul to the second.
            year = year1 if current_month >= 8 else year2
            last_dom = calmod.monthrange(year, current_month)[1]
            d1 = max(1, min(d1, last_dom))
            d2 = max(d1, min(d2, last_dom))

            tag = classify(title)
            dates = [dt.date(year, current_month, day) for day in range(d1, d2 + 1)]

            # The legend writes conferences and inservice days as one span --
            # "26-29 NO SCHOOL P/T Conf" -- even where the span runs over a
            # weekend the month grid leaves unmarked. Nobody holds conferences
            # on a Saturday, so those days are dropped rather than published as
            # conference days. Recesses and holidays keep their weekends: there
            # the closure really is continuous, and splitting Winter Recess at
            # every weekend would turn one break into several.
            # Spans only. A legend line naming a single date is the district
            # marking that day deliberately, even a Saturday one; only a span
            # written across a weekend is the transcription shortcut.
            if len(dates) > 1 and tag in WEEKDAY_ONLY_TAGS:
                dates = [day for day in dates if day.weekday() < 5]

            # Whatever survives may no longer be one span, so the range is
            # rebuilt per contiguous stretch instead of echoing the legend's.
            group = []
            for run in _contiguous(dates):
                for day in run:
                    ev = {
                        "date": day.isoformat(),
                        "title": title,
                        "tag": tag,
                        "noSchool": no_school,
                    }
                    if len(run) > 1:
                        ev["range"] = f"{run[0].isoformat()}/{run[-1].isoformat()}"
                    events.append(ev)
                    group.append(ev)
            last_event = group[0] if group else None
            continue

        # "(Full Day HMS/HHS---1/2 Day Elementary)" continues the line above it.
        if text.startswith("(") and last_event is not None:
            extra = text.replace("---", "\u2014").replace("--", "\u2013")
            for ev in events:
                if ev["date"] == last_event["date"] and ev["title"] == last_event["title"]:
                    ev["title"] = f"{ev['title']} {extra}"
            last_event = None
            continue

        # Free-standing prose such as "Floating Workday for teachers Dec 21-Jan 3".
        if re.search(r"[a-z]", text) and len(text.split()) >= 4 and not text.startswith("#"):
            notes.append(text)

    # The same day can be emitted twice where the PDF's own ranges overlap.
    seen = set()
    deduped = []
    for ev in sorted(events, key=lambda e: (e["date"], e["title"])):
        key = (ev["date"], ev["title"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)

    if not deduped:
        raise CalendarError(
            "Parsed the PDF but found no dated entries -- the layout likely changed. "
            "Check SIDEBAR_X and DAYSPEC_RE."
        )

    return {"schoolYear": f"{year1}\u2013{year2}", "events": deduped, "notes": notes}


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------
def fetch_calendar(*, log=print) -> tuple[dict, bytes]:
    """Crawl, download and parse. Returns (data_with_meta, pdf_bytes).

    Raises CalendarError with an actionable message if anything looks different
    from what we expect.
    """
    log(f"[info] fetching {HOME_URL}")
    page = fetch_text(HOME_URL)
    label, link = discover_calendar_link(page)
    log(f"[info] calendar link: {label!r} -> {link}")

    pdf_bytes, final_url, last_mod = download_pdf(link)
    log(f"[info] downloaded {len(pdf_bytes):,} bytes")

    data = parse_pdf(pdf_bytes)
    data["meta"] = {
        "retrievedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "linkLabel": label,
        "sourcePage": HOME_URL,
        "shortUrl": link,
        "pdfUrl": final_url,
        "pdfLastModified": last_mod,
        "pdfBytes": len(pdf_bytes),
        # Surfaced in the UI's "how this page is built" section, so the page
        # states the User-Agent actually sent rather than a hardcoded copy.
        "userAgent": USER_AGENT,
        "requestCount": 2,  # the homepage, then the PDF
    }

    no_school = sum(1 for e in data["events"] if e["noSchool"])
    log(
        f"[ok] {data['schoolYear']}: {len(data['events'])} dated entries "
        f"({no_school} no-school days), {len(data['notes'])} note(s)"
    )
    return data, pdf_bytes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Crawl and parse the USD 489 district calendar.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--out", type=Path, help="write parsed JSON here (default: stdout)")
    ap.add_argument("--save-pdf", type=Path, help="also save the downloaded PDF here")
    args = ap.parse_args(argv)

    try:
        data, pdf_bytes = fetch_calendar(log=lambda m: print(m, file=sys.stderr))
    except CalendarError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(data, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload)
        print(f"[ok] wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(payload)

    if args.save_pdf:
        args.save_pdf.parent.mkdir(parents=True, exist_ok=True)
        args.save_pdf.write_bytes(pdf_bytes)
        print(f"[ok] wrote {args.save_pdf}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
