# syntax=docker/dockerfile:1

# ---- build: resolve dependencies into a self-contained venv ----------------
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve from the lockfile alone, so this layer stays cached
# across application code edits.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project


# ---- runtime ---------------------------------------------------------------
# Same base the uv image is built on, so the venv's interpreter matches.
FROM python:3.14-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8489 \
    CACHE_DIR=/data \
    SEED_FILE=/app/seed/calendar.json \
    STATIC_DIR=/app/static

WORKDIR /app

COPY --from=build /app/.venv /app/.venv
COPY app.py calendar_source.py ical_feed.py ./
COPY static/ ./static/
# Seed cache, so a cold pod serves the calendar immediately without reaching
# out to usd489.com. Refresh it at build time with:
#   uv run calendar_source.py --out seed/calendar.json
COPY seed/ ./seed/

# Unprivileged, and /data is the only writable path the app needs (a refresh
# writes its cache there). Works with readOnlyRootFilesystem + an emptyDir.
RUN useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data \
 && chown -R app:app /data /app
USER 10001

EXPOSE 8489
VOLUME ["/data"]

# No curl in slim; use the interpreter we already have.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,os,sys; \
sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8489')+'/readyz', timeout=4).status==200 else sys.exit(1)"]

ENTRYPOINT ["python", "app.py"]
