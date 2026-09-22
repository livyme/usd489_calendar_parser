#!/usr/bin/env python3
"""HTTP service for the Hays USD 489 closures calendar.

Routes
------
Public:
  GET  /                UI shell (renders client-side from /api/calendar)
  GET  /api/calendar    parsed calendar + provenance, as JSON
  GET  /calendar.ics    iCalendar feed for phone/desktop calendar subscriptions
  GET  /healthz         liveness: process is up
  GET  /readyz          readiness: calendar data is loaded and servable

Everything that can reach out to usd489.com lives under /admin/, so a single
Cloudflare Access policy on /admin/* covers it:
  GET  /admin           status page; where Access lands you after login
  GET  /admin/whoami    reports the Cloudflare Access identity, if any
  POST /admin/refresh   re-crawl usd489.com now, update cache, return new data

The app deliberately does not authenticate /admin/ itself -- enforcement is at
the edge. It does throttle refreshes (see MIN_REFRESH_INTERVAL) so that even an
exposed endpoint cannot be used to hammer the district's website.

Caching
-------
The district publishes the calendar once and rarely changes it, so there is no
background poller. Data is loaded at startup from, in order of preference:

  1. $CACHE_DIR/calendar.json   -- written by a previous refresh
  2. $SEED_FILE                 -- baked into the image at build time

Startup therefore does no network I/O at all: a cold pod is ready immediately
and serves usable data even if usd489.com is down. Refresh is explicit, via
POST /admin/refresh (the Refresh button, shown only to an authenticated admin).
"""

from __future__ import annotations

import html
import json
import os
import signal
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import calendar_source
import ical_feed

ROOT = Path(__file__).resolve().parent

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8489"))
CACHE_DIR = Path(os.environ.get("CACHE_DIR", ROOT / "cache"))
SEED_FILE = Path(os.environ.get("SEED_FILE", ROOT / "seed" / "calendar.json"))
STATIC_DIR = Path(os.environ.get("STATIC_DIR", ROOT / "static"))
# Off by default: a restarted pod should serve its cache instantly rather than
# waiting on someone else's website. Turn on if you want startup to re-crawl.
REFRESH_ON_START = os.environ.get("REFRESH_ON_START", "").lower() in ("1", "true", "yes")

# Floor on how often a refresh may actually crawl usd489.com. This is not the
# access control -- Cloudflare Access is -- but it bounds the damage if /admin/
# is ever reachable without it, and stops an impatient double-click turning into
# two crawls. Set to 0 to disable.
MIN_REFRESH_INTERVAL = int(os.environ.get("MIN_REFRESH_INTERVAL", "300"))

# Header Cloudflare Access adds to requests it has authenticated. Admin routes
# refuse to act without it, so the service fails CLOSED if the Access policy is
# missing or misconfigured rather than exposing a crawl trigger to the internet.
#
# This is a presence check, not cryptographic verification: anything able to
# reach the pod directly could set the header itself. The real boundary is the
# Access policy plus the fact that the tunnel is the only ingress. Validating
# Cf-Access-Jwt-Assertion against the team JWKS would close that too.
ACCESS_EMAIL_HEADER = "Cf-Access-Authenticated-User-Email"

# Escape hatch for local development, where there is no Access in front. Never
# set this on a deployment reachable from the internet.
ALLOW_INSECURE_ADMIN = os.environ.get("ALLOW_INSECURE_ADMIN", "").lower() in (
    "1",
    "true",
    "yes",
)

CACHE_FILE = CACHE_DIR / "calendar.json"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class Throttled(Exception):
    """Raised when a refresh is attempted inside MIN_REFRESH_INTERVAL."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Refreshed too recently; retry in {retry_after}s.")
        self.retry_after = retry_after


class CalendarStore:
    """Holds the parsed calendar in memory, with a disk cache behind it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict | None = None
        self._source: str = "none"
        self._refresh_lock = threading.Lock()
        self._last_refresh: float = 0.0

    # -- loading -----------------------------------------------------------
    def load(self) -> None:
        for path, origin in ((CACHE_FILE, "cache"), (SEED_FILE, "seed")):
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                log(f"[warn] ignoring unreadable {origin} at {path}: {exc}")
                continue
            if not data.get("events"):
                log(f"[warn] ignoring {origin} at {path}: no events in it")
                continue
            with self._lock:
                self._data = data
                self._source = origin
            log(
                f"[ok] loaded {len(data['events'])} entries for "
                f"{data.get('schoolYear', '?')} from {origin} ({path})"
            )
            return
        log(f"[warn] no usable calendar data (looked in {CACHE_FILE}, {SEED_FILE})")

    # -- accessors ---------------------------------------------------------
    @property
    def data(self) -> dict | None:
        with self._lock:
            return self._data

    @property
    def source(self) -> str:
        with self._lock:
            return self._source

    @property
    def ready(self) -> bool:
        return bool(self.data)

    # -- refreshing --------------------------------------------------------
    def retry_after(self) -> int:
        """Seconds until another crawl is permitted, 0 if one is allowed now."""
        if MIN_REFRESH_INTERVAL <= 0 or not self._last_refresh:
            return 0
        elapsed = time.monotonic() - self._last_refresh
        return max(0, int(MIN_REFRESH_INTERVAL - elapsed) + 1) if elapsed < MIN_REFRESH_INTERVAL else 0

    def refresh(self) -> dict:
        """Re-crawl and persist. Raises CalendarError, leaving old data intact."""
        # One refresh at a time; concurrent clicks shouldn't stampede the site.
        if not self._refresh_lock.acquire(blocking=False):
            raise calendar_source.CalendarError(
                "A refresh is already running -- try again in a moment."
            )
        try:
            wait = self.retry_after()
            if wait:
                raise Throttled(wait)
            data, _pdf = calendar_source.fetch_calendar(log=log)
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = CACHE_FILE.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(data, indent=2) + "\n")
                tmp.replace(CACHE_FILE)  # atomic, so a crash can't truncate the cache
                log(f"[ok] cached to {CACHE_FILE}")
                origin = "cache"
            except OSError as exc:
                # A read-only mount is survivable: serve from memory this run.
                log(f"[warn] could not write cache to {CACHE_FILE}: {exc}")
                origin = "memory"
            with self._lock:
                self._data = data
                self._source = origin
            self._last_refresh = time.monotonic()
            return data
        finally:
            self._refresh_lock.release()


STORE = CalendarStore()


class Handler(BaseHTTPRequestHandler):
    server_version = "usd489-calendar/1.0"
    protocol_version = "HTTP/1.1"

    # -- helpers -----------------------------------------------------------
    def _send(
        self,
        status: HTTPStatus | int,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: HTTPStatus | int, payload: dict, extra=None) -> None:
        body = (json.dumps(payload, indent=2) + "\n").encode()
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _no_store(self) -> dict[str, str]:
        return {"Cache-Control": "no-store"}

    def _wants_html(self) -> bool:
        """True for a browser form post, false for a scripted JSON caller.

        A form submission sends Accept: text/html,...; anything driving the
        endpoint as an API asks for application/json explicitly. Defaulting to
        JSON keeps curl and the old fetch contract unchanged.
        """
        accept = self.headers.get("Accept", "")
        return "text/html" in accept and "application/json" not in accept

    def _redirect(self, location: str) -> None:
        """See-other, so a browser reload cannot re-submit a crawl."""
        self._send(
            HTTPStatus.SEE_OTHER,
            b"",
            "text/plain; charset=utf-8",
            {"Location": location, **self._no_store()},
        )

    # -- admin gate --------------------------------------------------------
    def _admin_identity(self) -> str | None:
        return self.headers.get(ACCESS_EMAIL_HEADER)

    def _admin_allowed(self) -> bool:
        """True when this request may perform an admin action."""
        return bool(self._admin_identity()) or ALLOW_INSECURE_ADMIN

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0].rstrip("/") or "/"

        if route == "/":
            return self._serve_shell()
        if route == "/api/calendar":
            return self._serve_json_calendar()
        if route == "/calendar.ics":
            return self._serve_ics()
        if route == "/healthz":
            return self._json(HTTPStatus.OK, {"status": "ok"}, self._no_store())
        if route == "/readyz":
            if STORE.ready:
                return self._json(
                    HTTPStatus.OK,
                    {"status": "ready", "source": STORE.source},
                    self._no_store(),
                )
            return self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"status": "no calendar data loaded"},
                self._no_store(),
            )
        if route == "/admin":
            return self._serve_admin_page()
        if route == "/admin/whoami":
            return self._serve_whoami()
        if route == "/admin/refresh":
            return self._json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "use POST /admin/refresh"},
                {"Allow": "POST", **self._no_store()},
            )
        self._json(HTTPStatus.NOT_FOUND, {"error": f"no route for {route}"})

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if route != "/admin/refresh":
            return self._json(HTTPStatus.NOT_FOUND, {"error": f"no route for {route}"})

        # Fail closed: without an Access identity we refuse to crawl, so a
        # missing or misconfigured edge policy cannot leave a public trigger
        # for hitting the district's website.
        if not self._admin_allowed():
            log("[warn] refused refresh: no Cloudflare Access identity on request")
            if self._wants_html():
                return self._redirect("/admin?failed=forbidden")
            return self._json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": (
                        "Refusing to refresh: no Cloudflare Access identity on this "
                        "request. Protect /admin/ with an Access policy, or set "
                        "ALLOW_INSECURE_ADMIN=1 for local development."
                    )
                },
                self._no_store(),
            )
        try:
            data = STORE.refresh()
        except Throttled as exc:
            log(f"[warn] refresh throttled ({exc.retry_after}s remaining)")
            if self._wants_html():
                return self._redirect(f"/admin?throttled={exc.retry_after}")
            return self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": str(exc), "retryAfter": exc.retry_after},
                {"Retry-After": str(exc.retry_after), **self._no_store()},
            )
        except calendar_source.CalendarError as exc:
            log(f"[error] refresh failed: {exc}")
            if self._wants_html():
                return self._redirect("/admin?failed=source")
            return self._json(
                HTTPStatus.BAD_GATEWAY,
                {"error": str(exc), "stillServing": (STORE.data or {}).get("meta")},
                self._no_store(),
            )
        except Exception as exc:  # noqa: BLE001 - never kill the server on refresh
            log(f"[error] refresh crashed: {type(exc).__name__}: {exc}")
            if self._wants_html():
                return self._redirect("/admin?failed=error")
            return self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"{type(exc).__name__}: {exc}"},
                self._no_store(),
            )
        if self._wants_html():
            return self._redirect("/admin?refreshed=1")
        self._json(HTTPStatus.OK, data, self._no_store())

    # -- handlers ----------------------------------------------------------
    def _serve_shell(self) -> None:
        index = STATIC_DIR / "index.html"
        try:
            body = index.read_bytes()
        except OSError:
            return self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"UI shell missing at {index}"},
            )
        # The shell is immutable per image; the data it fetches is what changes.
        self._send(HTTPStatus.OK, body, "text/html; charset=utf-8",
                   {"Cache-Control": "no-cache"})

    def _serve_json_calendar(self) -> None:
        data = STORE.data
        if not data:
            return self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "no calendar data loaded yet; POST /admin/refresh"},
                self._no_store(),
            )
        payload = dict(data)
        payload["cacheSource"] = STORE.source
        self._json(HTTPStatus.OK, payload, self._no_store())

    def _refresh_outcome(self) -> str:
        """Render the banner for a POST that redirected back here.

        Only fixed codes are accepted, and the retry count is re-parsed as an
        int, so nothing from the query string is ever echoed into the page.
        The reason a refresh failed goes to the container log, which is the
        only place with enough detail to be worth reading.
        """
        query = parse_qs(urlsplit(self.path).query)
        if query.get("refreshed"):
            return ('<p class="banner ok">Refresh complete &mdash; the entry count '
                    "and timestamp above are from that crawl.</p>")
        raw = (query.get("throttled") or [""])[0]
        if raw.isdigit():
            return (f'<p class="banner no">Not refreshed: usd489.com was crawled '
                    f"recently, so the throttle is holding for another "
                    f"{int(raw)}s.</p>")
        reason = {
            "forbidden": ("Not refreshed: this request carried no Cloudflare Access "
                          "identity, so crawling was refused."),
            "source": ("The refresh failed while reading the district's site. The "
                       "previously cached calendar is still being served &mdash; see "
                       "the container log for the reason."),
            "error": ("The refresh crashed. The previously cached calendar is still "
                      "being served &mdash; see the container log for the traceback."),
        }.get((query.get("failed") or [""])[0])
        return f'<p class="banner no">{reason}</p>' if reason else ""

    def _serve_admin_page(self) -> None:
        """Human-readable landing page for /admin.

        Cloudflare Access sends you here after login, so it needs to be
        readable rather than a JSON 404. Doubles as a way to confirm the Access
        policy is working: if it says no identity, the policy is not in front.
        """
        email = self._admin_identity()
        allowed = self._admin_allowed()
        wait = STORE.retry_after()
        outcome = self._refresh_outcome()

        if email:
            identity = f"Signed in as <strong>{html.escape(email)}</strong>."
        elif ALLOW_INSECURE_ADMIN:
            identity = (
                "No Cloudflare Access identity, but <code>ALLOW_INSECURE_ADMIN</code> "
                "is set, so admin actions are permitted. Expected locally; a mistake "
                "anywhere public."
            )
        else:
            identity = (
                "No Cloudflare Access identity on this request, so refreshing is "
                "disabled. Protect <code>/admin</code> with an Access policy."
            )

        disabled = "" if allowed else " disabled"
        crawl_note = (
            "Crawls usd489.com twice: the homepage, then the calendar PDF. "
            "Nothing else is fetched, and only one refresh runs at a time."
            if allowed else
            "Refreshing is disabled for this request, so the button does nothing."
        )
        state = "permitted" if allowed else "disabled"
        throttle = (
            f"Throttled for another {wait}s." if wait
            else f"Ready (minimum {MIN_REFRESH_INTERVAL}s between crawls)."
        )
        meta = (STORE.data or {}).get("meta") or {}
        retrieved = html.escape(str(meta.get("retrievedAt", "unknown")))
        count = len((STORE.data or {}).get("events") or [])

        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin — USD 489 Closures</title>
<style>
  :root{{color-scheme:light dark}}
  body{{margin:0;padding:clamp(24px,5vw,48px);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;
    line-height:1.55;background:#fbfaf9;color:#1c1917}}
  main{{max-width:34rem;margin:0 auto}}
  h1{{font-size:1.25rem;margin:0 0 4px;letter-spacing:-.02em}}
  p{{margin:0 0 12px}}
  .muted{{color:#8a827c;font-size:.85rem}}
  dl{{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;
    margin:18px 0;font-size:.9rem}}
  dt{{color:#8a827c}}
  dd{{margin:0}}
  code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85em;
    background:#f4f2f0;border-radius:4px;padding:1px 5px}}
  a{{color:#8c1d1d}}
  .ok{{color:#1f6b38;font-weight:600}}
  .no{{color:#8c1d1d;font-weight:600}}
  .banner{{border-left:3px solid currentColor;padding:8px 12px;font-size:.9rem;
    background:#f4f2f0;border-radius:0 6px 6px 0}}
  .btn{{font:inherit;font-weight:600;cursor:pointer;color:#fff;background:#8c1d1d;
    border:0;border-radius:8px;padding:9px 16px}}
  .btn:disabled{{cursor:not-allowed;opacity:.45}}
  form{{margin:18px 0 8px}}
  @media (prefers-color-scheme:dark){{
    body{{background:#131314;color:#f2f0ee}}
    code{{background:#242426}} a{{color:#f0a8a7}}
    .banner{{background:#1f1f21}} .btn{{background:#a33232}}
    .muted,dt{{color:#8b847e}} .ok{{color:#8ed7a3}} .no{{color:#e8908f}}
  }}
</style></head><body><main>
<h1>USD 489 Closures — admin</h1>
<p class="muted">{identity}</p>
<dl>
  <dt>Refresh</dt><dd class="{'ok' if allowed else 'no'}">{state}</dd>
  <dt>Throttle</dt><dd>{throttle}</dd>
  <dt>Data source</dt><dd>{html.escape(STORE.source)}</dd>
  <dt>Retrieved</dt><dd>{retrieved}</dd>
  <dt>Entries</dt><dd>{count}</dd>
</dl>
{outcome}
<form method="post" action="/admin/refresh">
  <button class="btn" type="submit"{disabled}>Refresh now</button>
</form>
<p class="muted">{crawl_note}</p>
<p><a href="/">&larr; Back to the calendar</a></p>
</main></body></html>
"""
        self._send(
            HTTPStatus.OK,
            body.encode(),
            "text/html; charset=utf-8",
            self._no_store(),
        )

    def _serve_whoami(self) -> None:
        """Report the Cloudflare Access identity, for the UI to gate on.

        Reaching this at all means Access let the request through, so the UI
        treats a 200 as "show the Refresh button". Unauthenticated callers get
        a redirect to the Access login page and never see this handler.
        """
        email = self._admin_identity()
        self._json(
            HTTPStatus.OK,
            {
                # "may this caller refresh", which is what the UI gates on --
                # not merely "did this request arrive".
                "authenticated": self._admin_allowed(),
                "email": email,
                "insecureAdminAllowed": ALLOW_INSECURE_ADMIN,
                "retryAfter": STORE.retry_after(),
                "minRefreshInterval": MIN_REFRESH_INTERVAL,
            },
            self._no_store(),
        )

    def _serve_ics(self) -> None:
        data = STORE.data
        if not data:
            return self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "no calendar data loaded yet; POST /admin/refresh"},
                self._no_store(),
            )
        body = ical_feed.render_ics(data)
        self._send(
            HTTPStatus.OK,
            body,
            "text/calendar; charset=utf-8",
            {
                "Content-Disposition": 'inline; filename="usd489-closures.ics"',
                # Subscribers poll on their own schedule; don't let a CDN pin
                # a stale copy for long after a refresh.
                "Cache-Control": "public, max-age=3600",
            },
        )

    # Route request logs through stderr in one line, like the rest of our logs.
    def log_message(self, fmt: str, *args) -> None:
        log(f"[http] {self.address_string()} {fmt % args}")


def serve() -> None:
    STORE.load()

    if REFRESH_ON_START:
        log("[info] REFRESH_ON_START set -- crawling before serving")
        try:
            STORE.refresh()
        except Exception as exc:  # noqa: BLE001
            log(f"[warn] startup refresh failed, continuing with cached data: {exc}")

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True

    def shutdown(signum, _frame) -> None:
        # k8s sends SIGTERM on rollout/scale-down; exit cleanly rather than
        # letting connections die mid-response.
        log(f"[info] signal {signal.Signals(signum).name} -- shutting down")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log(f"[ok] listening on http://{HOST}:{PORT}/  (data source: {STORE.source})")
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        log("[info] stopped")


if __name__ == "__main__":
    serve()
