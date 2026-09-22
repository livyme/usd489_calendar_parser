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
| `GET /admin` | status page — where Access lands you after signing in |
| `GET /admin/whoami` | reports the Cloudflare Access identity, if any |
| `POST /admin/refresh` | re-crawl usd489.com now, update the cache, return the new data |

## Keeping refresh private

The site is public, so anyone who could reach `POST /admin/refresh` could make
this service crawl usd489.com on demand. Two things prevent that.

**The admin routes fail closed.** `POST /admin/refresh` returns `403` and does
not crawl unless the request carries a `Cf-Access-Authenticated-User-Email`
header, which only Cloudflare Access adds. So if the Access policy is missing
or misconfigured, refresh is disabled rather than sitting on the internet as an
open crawl trigger. `GET /admin/whoami` reports that verdict as
`authenticated`, which is the quickest way to check a policy is really in
front: reaching it at all and seeing `false` means it is not.

Note this is a presence check, not cryptographic verification — anything able
to reach the pod directly could set the header. The real boundary is the Access
policy plus the tunnel being the only ingress. Validating
`Cf-Access-Jwt-Assertion` against the team JWKS would close that gap too.

For local development there is no Access in front, so set
`ALLOW_INSECURE_ADMIN=1` to permit admin actions (`docker-compose.yml` already
does). Never set it on anything reachable from the internet.

### Closing the in-cluster path

Cloudflare guards the admin routes from the internet, but a presence check on a
header is no defence from *inside* the cluster: any pod that can reach the
Service can set `Cf-Access-Authenticated-User-Email` itself and trigger a crawl.
Verified by posting to `/admin/refresh` with the header set by hand — it returns
`200` and crawls, with no Access anywhere in the picture. The throttle bounds
that to one crawl per `MIN_REFRESH_INTERVAL`, but it does not prevent it.

[`k8s/networkpolicy.yaml`](k8s/networkpolicy.yaml) restricts ingress to the
tunnel pod itself — `app=tunnel-talos` in namespace `k-cloudflare` — so the
tunnel really is the only way in. Both selectors sit in one `from` element, which
means AND; as two elements it would mean "anything in `k-cloudflare`, *or*
anything labelled `app=tunnel-talos` anywhere", which is an easy mistake to make
and much weaker. The namespace is matched on
`kubernetes.io/metadata.name`, which Kubernetes sets itself, so `k-cloudflare`
needs no hand-applied label.

It denies by default, and that cuts both ways: if the tunnel is renamed,
relabelled or moved out of `k-cloudflare`, the site goes dark rather than
warning. After the first sync,
confirm the pod is still ready and the site still loads through the tunnel.

Two things to check on your cluster, because both fail quietly:

- **The CNI must enforce NetworkPolicy.** Flannel alone ignores these objects
  entirely, so the manifest applies cleanly, reports no error, and protects
  nothing. Calico and Cilium enforce it. This matters here: Talos ships Flannel
  by default, so unless this cluster had a policy-capable CNI installed
  deliberately, treat the manifest as documentation of intent and not as a
  control. `kubectl get pods -n kube-system` settles it.
- **Liveness and readiness probes come from the kubelet, not a pod**, so no
  `podSelector` can match them. Most CNIs permit node-to-pod traffic regardless;
  if yours does not, the probes start failing and the pod restart-loops. The fix
  is an extra `from: [ipBlock: {cidr: <node CIDR>}]` rule, which is why step 3
  is checking readiness rather than assuming it.

**Cloudflare Access supplies the identity.** In Cloudflare Zero Trust go to
Access controls → Applications → Create new application → Self-hosted, and add
**two** path entries with the same policy:

```
Public hostname:  <your-host>   path: admin      (the status page)
Public hostname:  <your-host>   path: admin/*    (whoami + refresh)
Policy:           Allow — Emails — <your email>
```

Both are required. A path of `admin` covers only `/admin` and does *not* cover
`/admin/refresh`, while `admin/*` covers everything beneath `/admin` but not
`/admin` itself — see Cloudflare's [application paths][paths] docs. Protecting
only `admin` leaves the real endpoints open; protecting only `admin/*` leaves
the status page reporting "no identity" to everyone. If the dashboard accepts
just one hostname per application, make two applications.

Do **not** add an application covering `/`, or the calendar page, the iCal feed
and the Kubernetes probes will all start demanding a login.

Visit `<your-host>/admin` to check the result: it shows the identity Access
passed through and whether refreshing is permitted. If it loads but reports no
identity, Access is not in front of that path.

[paths]: https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/

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

1. builds the image **once**, for amd64, and **runs it** — asserting the container becomes
   ready from its baked-in seed, that the API returns events, and that the iCal feed parses
   with unique UIDs;
2. pushes *that* image, the one that just passed, to
   `ghcr.io/livyme/usd489_calendar_parser`, tagged `1.0.0`, `1.0`, `sha-<commit>` and
   `latest`. The registry credential is only added to the job after the tests pass, so a
   failure cannot publish anything;
3. commits the new version into `newTag:` in
   [`k8s/kustomization.yaml`](k8s/kustomization.yaml) on `main`.

Published images are **amd64 only**. Buildx cannot `--load` a multi-platform manifest into
the local daemon, so smoke testing a multi-arch build would mean building twice and
publishing a rebuild of what was tested rather than the tested bytes themselves. The
cluster is amd64, so the single build wins on both counts. Local testing on an arm64 Mac
is unaffected — `docker compose --profile image up --build calendar-image` builds natively
from the Dockerfile rather than pulling from ghcr. Add `platforms:` back to the build step
if a second architecture ever needs to run this.

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
| `ALLOW_INSECURE_ADMIN` | unset | set to `1` to allow `/admin/` without a Cloudflare Access identity — local development only |

## How the data is cached

The district publishes the calendar once and rarely touches it, so there's no background
poller. At startup the app loads, in order of preference:

1. `$CACHE_DIR/calendar.json` — written by a previous refresh
2. `$SEED_FILE` — baked into the image at build time

**Startup does no network I/O at all.** A cold pod is ready immediately and serves usable
data even if usd489.com is down. usd489.com is contacted only when someone calls
`POST /admin/refresh` — via the Refresh button on `/admin`, or directly — not on page
load, not on a timer, not per visitor. The public calendar page ships no admin controls
and makes exactly two requests of its own: `GET /` and `GET /api/calendar`.

To refresh the seed baked into the image:

```sh
uv run calendar_source.py --out seed/calendar.json
```

### Refreshing across a school-year boundary

A refresh replaces the dataset wholesale — `STORE.refresh()` assigns the newly parsed
payload over the old one, and the PDF covers exactly one school year. So when the district
swaps in the 2027–2028 calendar, which it typically does in spring while 2026–27 is still
running, refreshing at that moment drops the rest of the current year from the feed.
Refresh in February and subscribers could lose that year's spring break from their
calendars.

Nothing guards against this, deliberately: refresh is manual, so the timing is a judgement
call rather than something the code should decide. If you hit it, the options are to hold
off refreshing until the current year has run out, or to merge the two years by hand into
`$CACHE_DIR/calendar.json`. Worth checking what the district actually publishes when it
happens — if they keep both years reachable, the nav-link picker may well pick the wrong
one, which is a separate thing to look at.

Note the feed's name is year-agnostic (`Hays USD 489 District Calendar`) precisely so a
subscription survives this transition; only its contents change.

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
k8s/                   Deployment, ClusterIP Service and a NetworkPolicy
                       restricting ingress to k-cloudflare (ns l-usd489-calendar)
.github/workflows/     on a v* tag: smoke-test, publish to ghcr.io, pin the tag in k8s/
```

## Known quirk in the source PDF

The 2026–2027 calendar lists Jan 4 twice — once inside `1-4-NO SCHOOL Winter Recess` and
again as `4-NO SCHOOL Teacher Inservice`. Both are kept, because that's what the district
published; it isn't a parsing artifact.
