# engrix-router container image.
#
# Build from the repository root:  docker build -f deploy/Dockerfile -t engrix-router .
#
# The image is the ONE place where binding 0.0.0.0 is acceptable -- see the ENV block
# below. On a workstation the default is 127.0.0.1 and it should stay that way.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Only the files the build needs. `data/`, `knowledge/`, `credentials/` and `.env` are
# NOT copied on purpose: the first holds real prompts, traces and a database containing
# upstream tokens, the others are the gitignored private material (ADR-0002). There is
# no .dockerignore in this repo, so this explicit copy list is the exclusion mechanism.
# If you add a file the build needs, name it here -- do not switch to `COPY . .`.
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# Non-root, and the writable directory is owned by that user.
RUN groupadd --system router \
    && useradd --system --gid router --home-dir /data --shell /usr/sbin/nologin router \
    && mkdir -p /data \
    && chown -R router:router /data /app

USER router

# Everything the gateway writes (SQLite database + WAL sidecars, rotating logs) goes to
# /data, which is a bind mount in deploy/docker-compose.yml.
ENV EROUTER_DATA_DIR=/data

# ─────────────────────────────────────────────────────────────────────────────
# LOUD COMMENT: 0.0.0.0 here is a container decision, not an invitation.
#
# Inside a container the loopback interface belongs to the container namespace, so
# binding 127.0.0.1 would make the gateway unreachable even to its own reverse proxy.
# 0.0.0.0 is therefore correct *in the image* -- and it also means this process is
# listening on every interface the host publishes to it.
#
# This gateway stores upstream vendor tokens (plaintext, in the local SQLite file) and
# spends real money. Run it ONLY behind a reverse proxy that terminates TLS and
# authenticates, or on a private network you control. Never expose 8450 straight at
# the public internet, and set EROUTER_ADMIN_TOKEN plus keep
# EROUTER_REQUIRE_CLIENT_KEY=true before you publish anything. See deploy/README.md.
# ─────────────────────────────────────────────────────────────────────────────
ENV EROUTER_HOST=0.0.0.0
ENV EROUTER_PORT=8450

EXPOSE 8450

# Liveness probe against the public /health route. Uses stdlib urllib instead of curl so
# the image needs no extra package: a healthcheck that cannot run is worse than none.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; p=os.environ.get('EROUTER_PORT','8450'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + p + '/health', timeout=4).getcode() == 200 else 1)"]

# Console script from pyproject.toml: engrix_router.web.server:main
CMD ["engrix-router"]
