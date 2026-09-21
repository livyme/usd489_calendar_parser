# Hays USD 489 closures

A small containerized web app that crawls [usd489.com](https://www.usd489.com/) for the
district calendar PDF, parses the closure dates out of it, and serves them as a
searchable page, a JSON API, and an iCalendar feed you can subscribe to from a phone.

Unofficial personal project; not affiliated with Hays USD 489.

## Endpoints

Public:

| Route | Purpose |
| --- | --- |
| `GET /` | the closures page (renders client-side from the API) |
| `GET /api/calendar` | parsed calendar + provenance, as JSON |
| `GET /calendar.ics` | iCalendar feed for calendar subscriptions |
| `GET /healthz` | liveness — the process is up |
| `GET /readyz` | readiness — calendar data is loaded and servable |

Admin — everything that can reach out to usd489.com:

| Route | Purpose |
| --- | --- |
| `GET /admin/whoami` | reports the Cloudflare Access identity, if any |
| `POST /admin/refresh` | re-crawl usd489.com now, update the cache, return the new data |

## Keeping refresh private

The site is public, so anyone who could reach `POST /admin/refresh` could make
this service crawl usd489.com on demand. Two things prevent that.

**Cloudflare Access protects the path.** The app does *not* authenticate
`/admin/` itself — enforcement is at the edge. In Cloudflare Zero Trust, add a
self-hosted application covering the **whole prefix**:

```
Application domain:  <your-host>/admin
Policy:              Allow — Emails — <your email>
```

Protect `/admin`, not just `/admin/refresh`. `/admin/whoami` is what the page
probes to decide whether to show its Refresh button; if that probe is left
public while refresh is protected, every visitor sees a button that fails with
a login redirect.

Once you have signed in through Access, the `CF_Authorization` cookie rides
along on the page's same-origin `fetch`, so the in-page button just works. An
unauthenticated visitor's probe is redirected to the login page, which the page
reads as "not an admin" and leaves the button hidden.

**Refreshes are throttled regardless.** `MIN_REFRESH_INTERVAL` (default 300s)
caps how often a crawl can actually happen, and a single-flight lock means
concurrent clicks cannot stampede. Repeat attempts get `429` with `Retry-After`
rather than hitting the district's site. That is deliberately independent of
who is asking, so a misconfigured policy cannot turn into a crawl loop.

## Subscribe from an iPhone

The page shows the URL to use. On iOS: **Calendar → Add Calendar → Add Subscription
Calendar**, then paste the `webcal://` form of your deployed URL:

```
webcal://your-host/calendar.ics
```

macOS Calendar, Google Calendar and Outlook take the same URL over `https://`.

The feed is built to behave properly for something people subscribe to rather than
import once:

- **Stable UIDs**, derived from (start date, title), so polling never duplicates events.
- **DTSTAMP pinned to the district's own `Last-Modified`**, not to our crawl time, so
  re-crawling an unchanged PDF produces a byte-identical feed and nothing shows as
  modified.
- **Multi-day closures are one event**, not fifteen. Per-day entries are collapsed back
  into contiguous runs, so Winter Recess appears once as Dec 21 – Jan 4.
- `TRANSP:TRANSPARENT`, so subscribed closures don't make you look busy.

## Running it

### Locally

```sh
uv run app.py                     # http://localhost:8489/
```

### Local container loop

```sh
docker compose up                 # http://localhost:8489/
docker compose restart calendar   # pick up Python edits
```

Source is bind-mounted into a stock uv image, so there's no image to rebuild while
iterating. Edits to `static/index.html` need no restart at all — it's read per request.

To smoke-test the actual production image instead:

```sh
docker compose --profile image up --build calendar-image   # http://localhost:8490/
```

### Kubernetes

### Releasing

Images are cut from **release tags only** — nothing is built on a push to `main` or on a
pull request:

```sh
git tag v1.0.0
git push origin v1.0.0
```

[`.github/workflows/build-image.yml`](.github/workflows/build-image.yml) then:

1. builds the image for amd64 and **runs it**, asserting the container becomes ready from
   its baked-in seed, that the API returns events, and that the iCal feed parses with
   unique UIDs — nothing is published if any of that fails;
2. publishes multi-arch (amd64 + arm64) to `ghcr.io/livyme/usd489_calendar_parser`, tagged
   `1.0.0`, `1.0`, `sha-<commit>` and `latest`;
3. commits the new version into `newTag:` in
   [`k8s/kustomization.yaml`](k8s/kustomization.yaml) on `main`.

So after the workflow finishes, deploying is just:

```sh
git pull
kubectl apply -k k8s/
```

The bump commit is made with `GITHUB_TOKEN`, which by design does not trigger workflows,
so it cannot loop back into another build. It does need `main` to accept a push from
`github-actions[bot]` — if `main` is protected, either allow the bot or drop the
`bump-manifests` job and edit `newTag:` by hand.

To build and push by hand instead:

```sh
docker build -t ghcr.io/livyme/usd489_calendar_parser:1.0.0 .
docker push ghcr.io/livyme/usd489_calendar_parser:1.0.0
```

`k8s/` has a Deployment and a ClusterIP Service in namespace `l-usd489-calendar`, and no
Ingress — point `cloudflared` at:

```
http://usd489-calendar.l-usd489-calendar.svc.cluster.local
```

The container runs as uid 10001 with `readOnlyRootFilesystem: true` and all capabilities
dropped; `/data` (an `emptyDir`) is the only path it writes. Measured footprint is ~18Mi
idle and ~43Mi while parsing, hence the 64Mi request / 192Mi limit.

**`replicas: 1` is deliberate.** Refresh is manual and per-pod: the parsed calendar lives
in each pod's memory and its own `/data`. With two replicas, pressing Refresh would only
update whichever pod served that request, and the page would appear to flip between fresh
and stale depending on routing. Since the district publishes the calendar once a year, one
replica is the right trade rather than adding shared state to work around it.

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | bind address |
| `PORT` | `8489` | bind port |
| `CACHE_DIR` | `./cache` (`/data` in the image) | where a refresh writes its cache |
| `SEED_FILE` | `./seed/calendar.json` | fallback data baked into the image |
| `STATIC_DIR` | `./static` | UI shell location |
| `REFRESH_ON_START` | unset | set to `1` to crawl at startup instead of trusting the cache |
| `MIN_REFRESH_INTERVAL` | `300` | seconds between permitted crawls; `0` disables the throttle |

## How the data is cached

The district publishes the calendar once and rarely touches it, so there's no background
poller. At startup the app loads, in order of preference:

1. `$CACHE_DIR/calendar.json` — written by a previous refresh
2. `$SEED_FILE` — baked into the image at build time

**Startup does no network I/O at all.** A cold pod is ready immediately and serves usable
data even if usd489.com is down. usd489.com is contacted only when someone calls
`POST /admin/refresh` (the Refresh button) — not on page load, not on a timer, not per
visitor.

To refresh the seed baked into the image:

```sh
uv run calendar_source.py --out seed/calendar.json
```

## How the calendar is found and parsed

1. **Fetch the homepage.** The site's nav ships inside the page as an escaped JSON blob;
   the script unescapes it, collects every nav entry whose name mentions a calendar, and
   scores them — preferring `district`, preferring the later school year, rejecting the
   BOE meeting calendar. Today that picks *"2026-2027 District Calendar"* →
   `https://aptg.co/LkvZmJ`.
2. **Follow the shortlink** to the PDF on `files-backend.assets.thrillshare.com` and
   verify it really is a PDF before parsing.
3. **Parse the legend column, not the month grids.** The right-hand side of the PDF is a
   plain list — `AUGUST`, `17-NO SCHOOL Teacher Inservice`, `29-30-NO SCHOOL P/T Conf` —
   which extracts as text in reading order. The month grids are bare day numbers whose
   meaning is carried only by colour fill, so they're far more fragile. The parser takes
   words right of x≈405, groups them into lines, tracks the current month heading, and
   expands day ranges into one entry per day. Aug–Dec map to the first year of the span,
   Jan–Jul to the second.

### On the User-Agent

usd489.com sits behind a Fastly bot challenge that fires on any User-Agent claiming to be
a browser (`Mozilla/5.0 ...`) — those get a JS challenge page instead of content. An
honest self-identifying agent (`usd489-calendar-bot/1.0`) is served the real page, which
is also what the site's `robots.txt` permits (`Allow: /`, only `/api/` disallowed). So
the crawler identifies itself rather than impersonating a browser, fetches exactly two
URLs per refresh (the homepage, then the PDF), serializes refreshes behind a lock, and
doesn't mirror the district's PDF. The page itself explains this to visitors in its
"How this page is built" section.

## If it breaks

Refresh fails loudly with an actionable message and **keeps serving the last good data**:

- *"Could not find a calendar link"* — the nav changed; check `discover_calendar_link()`.
- *"Expected a PDF ... but got Content-Type"* — the district moved off a PDF.
- *"found no dated entries"* — the PDF layout changed; check `SIDEBAR_X` and `DAYSPEC_RE`.

A failed refresh surfaces the error in the UI and returns `502` from
`POST /admin/refresh`; readiness is unaffected, so k8s won't restart the pod over it.

## Layout

```
app.py                 HTTP service: routes, in-memory store, disk cache
calendar_source.py     crawl + parse (importable; also a CLI for seeding)
ical_feed.py           RFC 5545 feed rendering
static/index.html      UI shell (inline CSS/JS, renders from /api/calendar)
seed/calendar.json     seed cache baked into the image
k8s/                   Deployment + ClusterIP Service (ns l-usd489-calendar)
.github/workflows/     on a v* tag: smoke-test, publish to ghcr.io, pin the tag in k8s/
```

## Known quirk in the source PDF

The 2026–2027 calendar lists Jan 4 twice — once inside `1-4-NO SCHOOL Winter Recess` and
again as `4-NO SCHOOL Teacher Inservice`. Both are kept, because that's what the district
published; it isn't a parsing artifact.
