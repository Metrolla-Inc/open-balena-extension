# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Fleet Control API** (`components/fleet-api`, Relay plan F11 / M2c): a Flask + flask-restx
  service serving `/fleet/v1` on the openBalena host. It builds releases from a git ref through the
  remote builder and finalizes them, pins fleets and devices, rolls releases out in waves (canary
  devices, cumulative percent, soak, `halt_if`, re-pin on halt), reads and patches env vars with
  secret masking, restarts services and reboots devices through the supervisor proxy, builds
  flashable images through the imagemaker, reports drift (devices off target, target pins and env
  vars changed outside the API), checks server health and runs server upkeep (`registry_prune`,
  `builder_prune`, `haproxy_restart`). Full reference: [docs/fleet-api.md](docs/fleet-api.md).
  - Fleet change sets (`POST /fleet/v1/changes`) with the items `release.build`, `env.patch`,
    `release.deploy` and `site.change`, a plan hash, approvals bound to that hash, RRULE release
    windows, freezes, emergencies, Ed25519 signatures, webhooks (`fleet.change.<state>`) and an
    audit row per transition. `POST /fleet/v1/fleets/{fleet}/site-changes` fans a site change set
    out to device EdgeAPIs (`/api/v1/changes`) in waves.
  - Sign-in, roles, tokens and audit from `metrolla_auth` (EdgeAPI #101/#105, pinned to an
    EdgeAPI `main` commit), mounted at `/fleet/v1/auth`, `/fleet/v1/access`, `/fleet/v1/audit` and
    `/fleet/auth` (browser sign-in). OpenAPI at `/fleet/v1/swagger.json`, UI at `/fleet/v1/docs`.
  - Device reach through openBalena's VPN proxy (`CONNECT <uuid>.balena:80` to `vpn:3128`), with a
    connectivity-only health fallback. **Not yet proven on a real openBalena (.183).**
  - State in SQLite at `/data/fleet-api.db` on the `fleet-api-data` volume.
- **Shared change-control engine:** the generic change-set rules (canonical JSON and plan hash,
  compact-JWS EdDSA signature checks matching metrolla-cli, the `change_control` config with
  JSON-Pointer errors, RRULE windows and freezes, the state machine, approval rules, webhook HMAC)
  come from `metrolla_auth.change_control` (EdgeAPI PR #105), the same code EdgeAPI's change
  control runs. The interim `components/fleet-api/metrolla_change_control/` copy and its tests
  are gone; the tests live in EdgeAPI's `tests/test_change_control_engine.py`.
  - `change_control.webhooks` (`[{url, events, secret | secret_env}]`) are delivered alongside the
    top-level `webhooks` (which also accept `secret_env` now). Every hook uses EdgeAPI's wire
    format: canonical JSON body, `X-Metrolla-Event`, `X-Metrolla-Delivery` and
    `X-Metrolla-Signature: sha256=<HMAC-SHA256 of the body>` from the shared `signature()` helper.
  - `change_control.verify.settle_s` is honoured: verification waits the longer of it and
    `rollout.verify_settle_s`. `verify.checks` is parsed but unused (verification here is device
    release and health).
  - The image build checks `import metrolla_auth.change_control`; the Ansible role vendors
    metrolla-auth at `fleet_api_metrolla_auth_ref`.
  - **Authority comes from approvals, not the author.** Each fleet item maps to the permission a
    direct write needs (`release.build` → fleet:release, `release.deploy` → fleet:deploy,
    `env.patch` → fleet:env, `site.change` → change:author; `op.device.*` → fleet:device,
    `op.server.*` → system:admin) and the plan records it (`plan.required_permissions`). An
    approval counts toward an item only if the approver holds its permission (kept on the
    approval as `permissions`); others are refused 403 `approver-lacks-permission`. An item whose
    policy needs no approval needs the author's own permission (kept on the change set from
    creation) or one qualifying approval, emergencies likewise, so a `change_pipeline` token can't
    self-apply a deploy. `GET /fleet/v1/changes/{id}` shows `approval_status` and `waiting_for`.
    New nullable columns `fleet_change_sets.author_permissions` and
    `fleet_change_approvals.permissions`, added on start to existing databases.
- **OpenAPI with sign-in on:** `/fleet/v1/swagger.json` and the `/fleet/v1/docs` page answer any signed-in
  caller. They were refused as undeclared routes, because metrolla_auth only exempts the OpenAPI views
  under a blueprint named `api`.
- **Image and compose project:** `components/fleet-api/Dockerfile` builds `metrolla-fleet-api:local`
  (Python 3.12, gunicorn with **one** worker and 8 threads because the change scheduler runs in the
  process, static docker CLI for server operations, healthcheck on `/fleet/v1/ping`).
  `components/fleet-api/docker-compose.yml` runs it as compose project **`openbalena-fleet`** on
  the external `open-balena_default` network (alias `fleet-api`), with the `fleet-api-data` volume,
  `open-balena_certs-data` mounted read-only at `/certs`, the docker socket, and
  `host.docker.internal` mapped to the host gateway.
- **haproxy route `fleet.<tld>`** in both the `http` and `https` frontends of
  `components/haproxy/haproxy.cfg`, to `backend fleet-backend` (`fleet-api:80`, `timeout server
  300s`, `X-Real-IP` set from the client address, overwriting any client value). It answers 503
  until the fleet-api project is deployed.
- **Ansible role `fleet_api`** (tag `fleet_api`), wired into `ansible/site.yml` behind
  **`deploy_fleet_api`**, which defaults to **`false`**. It vendors metrolla-auth, builds the image,
  writes `<install_root>/fleet-api/{config,secrets}` and the compose `.env`, and starts the project.
  Role variables are in `ansible/roles/fleet_api/defaults/main.yml`; the secrets
  `fleet_api_balena_api_key` (required), `fleet_api_git_token`, `fleet_api_github_token` and
  `fleet_api_device_token` are new entries in `ansible/group_vars/all.example.yml` (vault them).
- **`imagemaker_host`** variable: the imagemaker unit now sets `HOST={{ imagemaker_host |
  default('127.0.0.1') }}`. Set it to the docker bridge gateway (e.g. `172.17.0.1`) so the Fleet
  API container can reach the imagemaker.
- **`make test-fleet-api`** and a `fleet-api tests` CI job (Python 3.12). The job needs an
  `EDGEAPI_READ_TOKEN` repo secret to install metrolla-auth from the private EdgeAPI repo; without
  it the job is skipped with a warning.

### Fixed
- **Fleet API `registry_prune` on registry:3.** The registry binary and config path are now found
  inside the registry container (`registry` + `/etc/distribution/config.yml` on `registry:3`,
  `/etc/docker/registry/config.yml` on `registry:2`, `docker-registry` + `/etc/docker-registry.yml`
  on upstream `balena/open-balena-registry`, or a `.yml` in the container's command). The old
  default `/etc/docker/registry/config.yml` was wrong for both registry:3 and openBalena's image.
  `server.registry_config` (now empty by default) and the new `server.registry_bin` override it.
- **Fleet API server operations no longer leave processes behind.** Killing `docker exec` on
  timeout left the command (e.g. `registry garbage-collect`) running in the container. In-container
  commands now record their pid in `/tmp/fleet-api-<op id>.pid` and are stopped (TERM, then KILL)
  when they time out, and when the API finds an operation still `running` after a restart (marked
  `failed`, `interrupted`). Only one operation of a type runs at a time (409 `operation-running`).
  Time limits are per type: `server.operation_timeouts_s` (`registry_prune` 3600 s), else
  `command_timeout_s` (900 s). Operations stay async (202, then poll
  `GET /fleet/v1/server/operations/{id}`).

### Changed
- **metrolla-auth repinned to EdgeAPI `main`.** `requirements-auth.txt` and
  `fleet_api_metrolla_auth_ref` pin EdgeAPI `main` commit `71e546b` instead of the merged
  `feat/relay-changes` branch. The role's checkout is shallow only for a branch or tag ref.
- `make secret-scan` ignores a 40-hex commit pin in a pip `git+…@<sha>#` URL or an Ansible
  `*_ref:` var (the rest of such a line is still scanned).

### Configuration reference (new)
- **Container env vars:** `DNS_TLD` (expanded as `${DNS_TLD}` in the config file),
  `FLEET_API_CONFIG` (default `/etc/fleet-api/fleet-api.yaml`), `METROLLA_AUTH_CONFIG` (default
  `/etc/fleet-api/auth.yaml`), `FLEET_API_DATA_DIR` (default `/data`), `FLEET_API_DB` (default
  `sqlite:///<data dir>/fleet-api.db`), `FLEET_API_SCHEDULER` (`0` disables the change
  scheduler), `LOG_LEVEL` (compose sets it from `FLEET_API_LOG_LEVEL`, default `INFO`),
  `BALENA_API_KEY` (optional alternative to the key file; the file is the default path).
- **Compose env:** `DNS_TLD`, `INSTALL_ROOT` (default `/opt/openbalena`), `FLEET_API_LOG_LEVEL`.
- **Secret files** under `<install_root>/fleet-api/secrets` → `/run/secrets`: `balena_api_key`,
  `git_token`, `imagemaker_token`, `device_token`, plus any webhook `secret_file`.
- **Ports:** the container listens on `:80` and is not published on the host; it is reached only
  through haproxy (`fleet.<tld>`). Outbound it uses `https://api.<DNS_TLD>` (pine v6),
  `builder:80` (`/v3/build`), `vpn:3128` (device CONNECT proxy), `registry:80`, `haproxy:80/health`
  and the imagemaker at `host.docker.internal:8090`.
- **Defaults keep today's behaviour:** the role is off, sign-in is off (`auth.enabled: false`,
  anonymous `site_admin` limited to `auth.allow_networks`), and `change_control.mode: open`.
