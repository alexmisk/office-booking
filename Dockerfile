# syntax=docker/dockerfile:1

# Office desk booking app.
#
# Three stages: Node builds the stylesheet, Python builds a virtualenv, and a
# clean slim image gets only the two results. Neither npm nor a compiler ends
# up in the image that runs in production.


# --------------------------------------------------------------------------
# Stage 1 -- build static/app.css with Tailwind + daisyUI
# --------------------------------------------------------------------------
FROM node:24-bookworm-slim AS css

WORKDIR /build

# package*.json first: this layer is cached until the dependencies change.
COPY package.json package-lock.json ./

# ci, not install: installs exactly the locked versions and fails if
# package-lock.json disagrees with package.json.
# --ignore-scripts blocks lifecycle scripts (postinstall & co.), the usual
# vehicle for a compromised npm package to run code at build time.
RUN npm ci --ignore-scripts

# The Tailwind scanner reads the templates for class names (@source in
# src/app.css), so they are inputs to the CSS build.
COPY src/ ./src/
COPY templates/ ./templates/

RUN npm run css


# --------------------------------------------------------------------------
# Stage 2 -- build the Python virtualenv
# --------------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS deps

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# requirements-prod.txt pins every package, direct and transitive, to an exact
# version and its wheel hashes. --require-hashes makes pip refuse anything that
# does not match, so a compromised or swapped-out package on PyPI fails the
# build instead of shipping. Regenerate it after editing requirements-prod.in:
#
#   uv pip compile requirements-prod.in --generate-hashes \
#       --python-version 3.13 --python-platform linux \
#       --output-file requirements-prod.txt
COPY requirements-prod.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-prod.txt


# --------------------------------------------------------------------------
# Stage 3 -- runtime
# --------------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS runtime

# Pick up whatever the base image has not yet been rebuilt with. Base images
# lag behind Debian security updates by days; this closes that window.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# A system account with no home, no password and no login shell. Fixed uid/gid
# so a bind-mounted /data has predictable ownership on the host.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app \
       --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin app

# Strip setuid/setgid bits. Nothing here needs them, and they are the standard
# stepping stone from code execution to root inside the container.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + || true

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    BOOKING_DB=/data/bookings.db

COPY --from=deps /opt/venv /opt/venv

WORKDIR /srv/app

# Everything below is owned by root and only readable by the app user, so a
# bug in the app cannot rewrite the app's own code or templates.
COPY --chown=root:root app.py ./
COPY --chown=root:root templates/ ./templates/
COPY --chown=root:root static/ ./static/
COPY --from=css --chown=root:root /build/static/app.css ./static/app.css
COPY --chown=root:root docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# a=rX -> readable by everyone, executable only where it is a directory. Done in
# a RUN rather than COPY --chmod so the build does not require BuildKit.
RUN chmod -R a=rX /srv/app \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh

# The one writable path: the SQLite database and its -wal/-shm siblings.
# Mount a volume here (see the run command in README) -- everything else can
# and should run with --read-only.
RUN install -d -o app -g app -m 750 /data

USER 10001:10001

EXPOSE 5001

# Hit the real month view rather than a synthetic endpoint, so the check
# exercises template rendering and the database, not just the socket.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5001/', timeout=4).status == 200 else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]

# Two processes, threads for concurrency: SQLite serialises writers, so many
# worker processes would just trade throughput for "database is locked".
# --worker-tmp-dir /dev/shm keeps gunicorn's heartbeat file off the (read-only,
# or disk-backed) root filesystem.
# --no-control-socket: gunicorn >=26 opens a unix control socket under $HOME by
# default. The app user has no home, so it would error on every start, and a
# live control channel into the server is attack surface this app never uses.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5001", \
     "--no-control-socket", \
     "--worker-class", "gthread", \
     "--workers", "2", \
     "--threads", "4", \
     "--worker-tmp-dir", "/dev/shm", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
