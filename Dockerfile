# readerboard in a container.
#
# The service needs no changes to run here. Every setting it reads is available
# as a READERBOARD_ environment variable, it already runs uvicorn in the
# foreground, and GET /health is unauthenticated by design, so the health check
# below needs no API key.
#
# Two stages. The first builds a virtual environment, the second copies the
# finished environment across, so pip and its cache are in nothing anybody
# pulls.
#
# There is no compiler in either stage and nothing needs one. Every pinned
# dependency has a wheel for all three published platforms, which is why
# pyproject.toml takes plain uvicorn rather than the "standard" extra: that
# extra's uvloop, httptools and PyYAML publish no 32-bit arm wheel, and building
# them from source under emulation took eight minutes of a thirteen minute
# build for a speedup this service has no use for.

# The base image is pinned by digest, the way the workflows pin their actions by
# SHA, so that a rebuild of a released tag produces what it produced the first
# time. The digest names a manifest list covering amd64, arm64 and arm/v7, and
# the daemon picks the right one out of it.
#
# A patch tag rather than the rolling 3.14-slim, and that is not fussiness.
# The rolling tags are rebuilt every few days, so whatever digest they point
# at is always a few days old, and nothing that young may be used here.
#
# A patch tag is quieter, but it is not still: while it is the newest of its
# line it is rebuilt too. So the newest patch is exactly the one whose digest
# keeps moving, which is how 3.14.7-slim was taken here on a digest a day old.
# Take the patch behind the newest, which has stopped.
#
# Check the age rather than assuming it, and do not lean on Dependabot's
# cooldown for this: the cooldown reads the version, and a rebuilt digest is
# the same version it was yesterday. This one was 23 days old when it was
# pinned, and carries the same tzdata the note below relies on.
FROM python:3.14.6-slim@sha256:7bec7ddcddeff7975d6ba9b4be7dd6f6b2f55e7491539145e2978f7f97ce9144 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# The same path scripts/install.sh uses on a machine running systemd, so that a
# person who has seen one deployment recognises the other.
RUN python -m venv /opt/readerboard/venv
ENV PATH="/opt/readerboard/venv/bin:$PATH"

# The dependencies first and on their own, so that editing the source does not
# invalidate the layer that took the time to build.
#
# --require-hashes is the point of the lock file carrying hashes: a version pin
# says what to install, and the hashes say what the bytes must be. pip refuses
# the whole install if any file it selects is not one of the ones listed.
COPY requirements.lock requirements-build.lock ./
RUN pip install --require-hashes --requirement requirements.lock \
    && pip install --require-hashes --requirement requirements-build.lock

# Then the project, with its dependencies already in place.
#
# --no-deps so that the ranges in pyproject.toml cannot quietly pull a version
# the lock file did not choose. --no-build-isolation so that pip builds the
# wheel with the setuptools installed above rather than fetching an unpinned one
# from the index, which would be the only unverified thing in the whole install.
#
# setuptools then comes back out. It was needed to build the wheel and is needed
# by nothing at run time, and the environment this stage produces is copied
# wholesale into the image below.
COPY pyproject.toml README.md LICENSE ./
COPY readerboard ./readerboard
RUN pip install --no-deps --no-build-isolation . \
    && pip uninstall --yes setuptools


FROM python:3.14.6-slim@sha256:7bec7ddcddeff7975d6ba9b4be7dd6f6b2f55e7491539145e2978f7f97ce9144

LABEL org.opencontainers.image.title="readerboard" \
      org.opencontainers.image.description="An HTTP service for BetaBrite and Alpha protocol LED signs: several sources share one sign, with alerts, expiring messages and clock sync" \
      org.opencontainers.image.source="https://github.com/mjaksn/readerboard" \
      org.opencontainers.image.documentation="https://github.com/mjaksn/readerboard/blob/main/README.md" \
      org.opencontainers.image.licenses="MIT"

# No tzdata layer here, deliberately, and it is worth saying why rather than
# leaving the next reader to wonder. pyproject.toml installs the tzdata wheel on
# Windows only, on the reasoning that a Linux machine carries a system zone
# database, and a slim image looked like the obvious exception to that. It is
# not: this base image has the Debian tzdata package in it, 443 zone files, and
# `timezone` validates against them. Checked rather than assumed, and the digest
# pin above is what keeps it true from one build to the next.

# A fixed UID and GID, not an arbitrary one, because the state directory is
# normally a bind mount and its owner on the host has to match this. 10001 is
# above the range Debian hands out to system accounts, so it will not collide
# with a user the base image created.
RUN groupadd --gid 10001 readerboard \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin readerboard

# Where the registry is persisted. This is already the state_path default in
# readerboard/config.py, so nothing has to override it. The systemd unit gets
# this directory from StateDirectory=; here it is a volume.
RUN mkdir -p /var/lib/readerboard && chown readerboard:readerboard /var/lib/readerboard
VOLUME ["/var/lib/readerboard"]

COPY --from=builder /opt/readerboard/venv /opt/readerboard/venv

ENV PATH="/opt/readerboard/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    READERBOARD_CONFIG_FILE=/etc/readerboard/config.toml

# The same path packaging/readerboard.service points at. Mounting a TOML file
# there works exactly as it does under systemd; mounting nothing is the ordinary
# container arrangement, where the environment carries the settings and the file
# being absent is not an error.

EXPOSE 5001

# GET /health is deliberately unauthenticated, so that a monitor can watch the
# sign without holding a key that could write to it. urllib rather than curl
# because the interpreter is already here and curl is not.
#
# Note what this does and does not prove. It says the service is up and
# answering. It does not say the sign is reachable: the body reports that in
# `status`, and against loop:// or a live adapter alike the link reads as open.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/health', timeout=4)"]

USER readerboard

# The console script pyproject.toml declares. It runs uvicorn in the foreground
# and reaps nothing, so no init shim is needed. `Restart=always` in the systemd
# unit has the container restart policy as its counterpart.
ENTRYPOINT ["readerboard"]
