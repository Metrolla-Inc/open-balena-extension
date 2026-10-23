# syntax=docker/dockerfile:1
# Fleet Control API (/fleet/v1) for self-hosted openBalena. See ../../docs/fleet-api.md.
#
# metrolla-auth comes from the private EdgeAPI repo. Either put its package directory in
# vendor/metrolla_auth before building (the Ansible role does), or build with a GitHub token:
#   docker build --secret id=github_token,env=GITHUB_TOKEN -t metrolla-fleet-api:local .
FROM python:3.12-slim

ARG DOCKER_CLI_VERSION=27.3.1
# git for release sources; the static docker CLI for server operations (via the mounted socket).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git openssh-client ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    arch="$(uname -m)"; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_CLI_VERSION}.tgz" \
      | tar -xz -C /usr/local/bin --strip-components=1 docker/docker; \
    docker --version

WORKDIR /app
COPY requirements.txt requirements-auth.txt ./
COPY vendor/ /tmp/vendor/
RUN --mount=type=secret,id=github_token,required=false \
    set -eu; \
    pip install --no-cache-dir -r requirements.txt; \
    if [ -f /tmp/vendor/metrolla_auth/pyproject.toml ]; then \
      pip install --no-cache-dir /tmp/vendor/metrolla_auth; \
    else \
      if [ -s /run/secrets/github_token ]; then \
        git config --global url."https://x-access-token:$(cat /run/secrets/github_token)@github.com/".insteadOf "https://github.com/"; \
      fi; \
      pip install --no-cache-dir -r requirements-auth.txt; \
      rm -f /root/.gitconfig; \
    fi; \
    rm -rf /tmp/vendor; \
    python -c "import metrolla_auth.change_control"

COPY fleet_api fleet_api
COPY wsgi.py .

ENV FLEET_API_CONFIG=/etc/fleet-api/fleet-api.yaml \
    METROLLA_AUTH_CONFIG=/etc/fleet-api/auth.yaml \
    FLEET_API_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1/fleet/v1/ping', timeout=3)"
# One process: the change scheduler runs inside it.
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "300", "--bind", "0.0.0.0:80", "wsgi:app"]
