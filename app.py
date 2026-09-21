#!/usr/bin/env python3
"""HTTP service for the Hays USD 489 closures calendar.

Routes
------
GET  /                  UI shell (renders client-side from /api/calendar)
GET  /api/calendar      parsed calendar + provenance, as JSON
POST /api/refresh       re-crawl usd489.com now, update cache, return new data
GET  /calendar.ics      iCalendar feed for phone/desktop calendar subscriptions
GET  /healthz           liveness: process is up
GET  /readyz            readiness: calendar data is loaded and servable

Caching
-------
The district publishes the calendar once and rarely changes it, so there is no
background poller. Data is loaded at startup from, in order of preference:

  1. $CACHE_DIR/calendar.json   -- written by a previous /api/refresh
  2. $SEED_FILE                 -- baked into the image at build time

Startup therefore does no network I/O at all: a cold pod is ready immediately
and serves usable data even if usd489.com is down. Refresh is explicit, via
POST /api/refresh (the Refresh button in the UI).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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

CACHE_FILE = CACHE_DIR / "calendar.json"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class CalendarStore:
    """Holds the parsed calendar in memory, with a disk cache behind it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict | None = None
        self._source: str = "none"
        self._refresh_lock = threading.Lock()

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
    def refresh(self) -> dict:
        """Re-crawl and persist. Raises CalendarError, leaving old data intact."""
        # One refresh at a time; concurrent clicks shouldn't stampede the site.
        if not self._refresh_lock.acquire(blocking=False):
            raise calendar_source.CalendarError(
                "A refresh is already running -- try again in a moment."
            )
        try:
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
        if route == "/api/refresh":
            return self._json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "use POST /api/refresh"},
                {"Allow": "POST", **self._no_store()},
            )
        self._json(HTTPStatus.NOT_FOUND, {"error": f"no route for {route}"})

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if route != "/api/refresh":
            return self._json(HTTPStatus.NOT_FOUND, {"error": f"no route for {route}"})
        try:
            data = STORE.refresh()
        except calendar_source.CalendarError as exc:
            log(f"[error] refresh failed: {exc}")
            return self._json(
                HTTPStatus.BAD_GATEWAY,
                {"error": str(exc), "stillServing": (STORE.data or {}).get("meta")},
                self._no_store(),
            )
        except Exception as exc:  # noqa: BLE001 - never kill the server on refresh
            log(f"[error] refresh crashed: {type(exc).__name__}: {exc}")
            return self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"{type(exc).__name__}: {exc}"},
                self._no_store(),
            )
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
                {"error": "no calendar data loaded yet; POST /api/refresh"},
                self._no_store(),
            )
        payload = dict(data)
        payload["cacheSource"] = STORE.source
        self._json(HTTPStatus.OK, payload, self._no_store())

    def _serve_ics(self) -> None:
        data = STORE.data
        if not data:
            return self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "no calendar data loaded yet; POST /api/refresh"},
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
