# RST WYSIWYG editor — public release image.
#
# Base: hardened Debian 13 "dev" variant, pulled from the registry supplied
# via the ARTIFACTORY_URL build arg. This repo intentionally contains no
# internal registry address — pass it on the build command line. The base
# provides root + apt during the build; the runtime user is a dedicated
# non-root account, so the app never runs as root.
#
#   docker build --build-arg ARTIFACTORY_URL=<your-registry> -t rst-wysiwyg-editor .
#   # (or from an env var: ARTIFACTORY_URL=<your-registry> docker build --build-arg ARTIFACTORY_URL ...)
#   docker run --rm -p 8000:8000 -v rst-work:/work rst-wysiwyg-editor
#
# Need a different base (e.g. the public python:3.13-slim)? Pass
# --build-arg BASE=<image> — it takes precedence over ARTIFACTORY_URL.

ARG ARTIFACTORY_URL
ARG BASE=${ARTIFACTORY_URL}/dhi.io/debian-base:trixie-debian13-dev
FROM ${BASE}
# (If ARTIFACTORY_URL was not passed, the build fails here with "invalid
#  reference format" — that is the intended signal: pass --build-arg
#  ARTIFACTORY_URL=<your-registry>, or override the whole base with
#  --build-arg BASE=<image>.)

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RST_WORK_ROOT=/work

# System dependencies (Debian trixie: python3 = 3.13, git, pandoc).
# emf2svg provides the `emf2svg-conv` tool used to preview EMF metafiles
# (rendered to SVG) in the browser — Linux-native, no Windows/WSL needed.
# --no-install-recommends keeps the hardened image minimal.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
        python3-venv \
        python3-pip \
        git \
        openssh-client \
        pandoc \
        emf2svg && \
    rm -rf /var/lib/apt/lists/*

# Application code (no venv — Flask + deps are installed system-wide,
# which is what the entrypoint expects).
COPY src/ /opt/rst-editor/
COPY run.sh /opt/rst-editor/run.sh

# Repo clones land here. The hardened base image ships no useradd/passwd,
# so create the non-root user directly in /etc/passwd + /etc/group
# (uid 1000, matching the host convention), then hand over ownership.
RUN mkdir -p /work /home/appuser && \
    printf 'appuser:x:1000:1000::/home/appuser:/bin/sh\n' >> /etc/passwd && \
    printf 'appuser:x:1000:\n' >> /etc/group && \
    chown -R appuser:appuser /opt/rst-editor /work /home/appuser && \
    chmod +x /opt/rst-editor/run.sh

# Flask deps (Flask>=3 is required on Python 3.13 — see doc/README.md).
RUN pip install --no-cache-dir --break-system-packages -r /opt/rst-editor/requirements.txt

USER appuser
WORKDIR /opt/rst-editor
EXPOSE 8000
VOLUME ["/work"]
ENTRYPOINT ["/opt/rst-editor/run.sh"]
