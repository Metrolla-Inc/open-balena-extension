# Fleet Control API

`components/fleet-api` is an HTTP API at `/fleet/v1` that runs on the openBalena host, next to
core. It does over HTTP what we otherwise do with the balena CLI, `make open-deploy` or the admin
dashboard: build and finalize releases, roll them out in waves, set env vars, restart services,
reboot devices, build flashable images, and look after the server. Sign-in, roles, audit and
change control are the same as EdgeAPI's, because both use `metrolla_auth`.

```
 mtl fleet / CI ──► haproxy fleet.<tld> ──► fleet-api ──► api.<tld>/v6 (pine)     releases, devices, env, pins
                                               │      ──► builder:80/v3/build      release builds
                                               │      ──► vpn:3128 CONNECT          device EdgeAPI /api/v1/*
                                               │      ──► imagemaker :8090          flashable images
                                               └──────► docker.sock                 prune / restart upkeep
```

It holds a balena API key server-side. Callers sign in to the Fleet API and never see that key.
Direct openBalena access stays for Metrolla admins, and anything changed that way shows up as
drift.

It is opt-in: the Ansible role is off (`deploy_fleet_api: false`), sign-in is off, and
`change_control.mode` is `open`, so deploying it changes nothing until you turn those on.

**Contents:** [Deploy](#deploy) · [Configuration](#configuration) ·
[Sign-in and roles](#sign-in-and-roles) · [Endpoints](#endpoints) ·
[Endpoint reference](#endpoint-reference) · [Releases](#releases) ·
[Fleet change sets](#fleet-change-sets) · [Device reach (VPN proxy caveat)](#device-reach) ·
[Drift](#drift) · [Server health and upkeep](#server-health-and-upkeep) · [Images](#images) ·
[State](#state) · [Tests](#tests) · [Troubleshooting](#troubleshooting) ·
[Lab checklist](#lab-checklist-183)

## Deploy

### Ansible (recommended)

The role `fleet_api` (tag `fleet_api`) runs only when `deploy_fleet_api` is true:

```yaml
# group_vars/all.yml (vault it)
deploy_fleet_api: true
fleet_api_balena_api_key: "…"         # balena api-key generate fleet-api  (as an admin user; required)
fleet_api_github_token: "…"           # read EdgeAPI for metrolla-auth (or set fleet_api_metrolla_auth_src)
fleet_api_git_token: "…"              # read-only token for the fleet repos it builds from
imagemaker_host: 172.17.0.1           # let the container reach the imagemaker
```

`make deploy-fleet_api` then:

1. fails early if `fleet_api_balena_api_key` is empty;
2. vendors metrolla-auth into the build context, from `fleet_api_metrolla_auth_src` on the
   control node, or from a shallow checkout of `fleet_api_metrolla_auth_repo` at
   `fleet_api_metrolla_auth_ref` (default `feat/relay-auth`) using `fleet_api_github_token`;
3. builds `metrolla-fleet-api:local`;
4. writes `<install_root>/fleet-api/config/{fleet-api,auth}.yaml` from the role templates
   (`fleet_api_manage_auth_config: false` leaves `auth.yaml` alone), the secrets in
   `<install_root>/fleet-api/secrets/` (mode 0600, `no_log`), and the compose `.env`
   (`DNS_TLD`, `INSTALL_ROOT`);
5. starts the `openbalena-fleet` compose project on `open-balena_default`.

The `fleet.` haproxy route lives in `components/haproxy/haproxy.cfg`; run the haproxy role (and
`haproxy -c` in the running container, see AGENTS.md invariant 5) to install it. Until the
fleet-api project is up, `fleet.<tld>` answers 503. Set `imagemaker_host` and re-run the
imagemaker role too, or image builds can't reach it. All role variables, with defaults, are in
`ansible/roles/fleet_api/defaults/main.yml`.

### By hand

```bash
cp -r ~/src/EdgeAPI/packages/metrolla_auth components/fleet-api/vendor/
docker build -t metrolla-fleet-api:local components/fleet-api
#   or, without vendoring:  docker build --secret id=github_token,env=GITHUB_TOKEN -t metrolla-fleet-api:local components/fleet-api
mkdir -p /opt/openbalena/fleet-api/{config,secrets}
cp components/fleet-api/fleet-api.example.yaml /opt/openbalena/fleet-api/config/fleet-api.yaml
cp components/fleet-api/auth.example.yaml /opt/openbalena/fleet-api/config/auth.yaml
install -m600 /dev/stdin /opt/openbalena/fleet-api/secrets/balena_api_key <<<"$KEY"
cd /opt/openbalena/fleet-api && cp …/components/fleet-api/docker-compose.yml . \
  && DNS_TLD=… docker compose -p openbalena-fleet up -d
```

Check it: `curl -k https://fleet.<tld>/fleet/v1/ping` → `{"ok": true}`, then
`GET /fleet/v1/server/health`.

The container mounts the docker socket (server operations) and holds the balena admin key:
like everything on `open-balena_default`, it is trusted and must not be published.

## Configuration

### Environment variables

| Variable | Default | Used for |
|---|---|---|
| `DNS_TLD` | (required in compose) | Expanded wherever `${DNS_TLD}` appears in `fleet-api.yaml` (e.g. `balena.api_url`) |
| `FLEET_API_CONFIG` | `/etc/fleet-api/fleet-api.yaml` | The service config file |
| `METROLLA_AUTH_CONFIG` | `/etc/fleet-api/auth.yaml` | metrolla_auth's `auth`, `access`, `site` sections |
| `FLEET_API_DATA_DIR` | `/data` | SQLite DB, metrolla_auth data (`<dir>/auth`), the CLI builder's `balenarc` |
| `FLEET_API_DB` | `sqlite:///<data dir>/fleet-api.db` | SQLAlchemy URL |
| `FLEET_API_SCHEDULER` | `1` | `0` disables the in-process change scheduler (tests and tools) |
| `LOG_LEVEL` | `INFO` | Python log level; compose sets it from `FLEET_API_LOG_LEVEL` |
| `BALENA_API_KEY` | unset | Optional alternative to `balena.api_key_file` (named by `balena.api_key_env`) |
| `NODE_EXTRA_CA_CERTS`, `NODE_TLS_REJECT_UNAUTHORIZED` | unset / `0` | Passed through to `balena push` in `builder.mode: cli` only |

The compose file itself reads `DNS_TLD`, `INSTALL_ROOT` (default `/opt/openbalena`) and
`FLEET_API_LOG_LEVEL` from its `.env`.

### Ports and network

- The container listens on **`:80`** (gunicorn, one worker, 8 threads, 300 s timeout). It is not
  published on the host; haproxy reaches it as `fleet-api:80` and serves it at `fleet.<tld>`
  through both the `http` and `https` frontends.
- Outbound, inside `open-balena_default`: `https://api.<DNS_TLD>` (pine v6), `builder:80`,
  `vpn:3128`, `registry:80/v2/`, `haproxy:80/health`; on the host: the imagemaker at
  `host.docker.internal:8090` (mapped to the host gateway).

### Files

| Container path | Host path (Ansible) | What |
|---|---|---|
| `/etc/fleet-api/fleet-api.yaml` | `<install_root>/fleet-api/config/` (ro) | Service config ([example](../components/fleet-api/fleet-api.example.yaml)) |
| `/etc/fleet-api/auth.yaml` | same | Sign-in config ([example](../components/fleet-api/auth.example.yaml)) |
| `/run/secrets/*` | `<install_root>/fleet-api/secrets/` (ro, 0700/0600) | `balena_api_key`, `git_token`, `imagemaker_token`, `device_token`, webhook secrets |
| `/data` | volume `fleet-api-data` | SQLite DB and auth data |
| `/certs` | volume `open-balena_certs-data` (ro) | `root-ca.pem` for TLS verification |
| `/var/run/docker.sock` | host docker socket | Server operations |

Secrets are never in `fleet-api.yaml`: each `*_file` key names a file under `/run/secrets`.

### `fleet-api.yaml`

Unset keys take the defaults in `fleet_api/settings.py`. The file is re-read when its mtime
changes, so windows and freezes can be edited without a restart. An invalid edit is logged and
the previous settings stay; an invalid file at start-up refuses to start.

| Section | Keys (default) |
|---|---|
| `balena` | `api_url` (`https://api.${DNS_TLD}`), `api_key_file` (`/run/secrets/balena_api_key`), `api_key_env` (`BALENA_API_KEY`), `verify` (`/certs/root-ca.pem`; `true`/`false`/path), `timeout_s` (30) |
| `builder` | `mode` (`http` \| `cli`), `url` (`http://builder:80`), `timeout_s` (3600), `cli` (`balena`), `release_wait_s` (300) |
| `git` | `repo_base` (`https://github.com/Metrolla-Inc/`), `allowed_repos` (empty = any under `repo_base`), `allow_urls` (false), `token_file`, `ssh_key_file`, `timeout_s` (300) |
| `reach` | `mode` (`vpn_proxy` \| `connectivity`), `proxy_host` (`vpn`), `proxy_port` (3128), `proxy_tls` (false), `proxy_user` (`root`), `device_port` (80), `timeout_s` (10), `health_path` (`/api/v1/health`), `device_token_file`, `device_user` |
| `imagemaker` | `url` (`http://host.docker.internal:8090`), `token_file` (`/run/secrets/imagemaker_token`), `timeout_s` (60) |
| `server` | `docker`, `containers.{builder_dind,haproxy,registry}` (`open-balena-*-1`), `registry_url`, `builder_url`, `haproxy_health_url`, `haproxy_stats_url`, `haproxy_stats_auth_file`, `registry_config` (`/etc/docker/registry/config.yml`), `builder_disk_warn_pct` (80), `builder_disk_fail_pct` (90), `command_timeout_s` (900) |
| `env` | `secret_patterns` (`TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `KEY`, `CREDENTIAL`, `PRIVATE`, `DSN`): names containing one are masked as `***` |
| `rollout` | `update_timeout_min` (30), `site_change_timeout_min` (1440), `tick_s` (15), `verify_settle_s` (60) |
| `webhooks` | `[{url, events: [glob], secret_file}]` |
| `change_control` | `mode` (`open` \| `gated`), `allowed_callers`, `require_signature`, `trusted_keys`, `timezone` (UTC), `windows`, `freezes`, `verify.timeout_s` (120), `emergency.approvals` (2), `policy` (each class defaults to `{approvals: 0, window: false}`) |

Policy lookups fall back to the parent class (`op.device.reboot` → `op.device` → `op`) and then
to `default`. Resource classes: `release.build`, `release.deploy`, `env`, `site.change`,
`op.device.restart_service`, `op.device.reboot`, `op.server.<type>`.

## Sign-in and roles

`/etc/fleet-api/auth.yaml` holds metrolla_auth's `auth`, `access` and `site` sections. Off by
default: every caller is anonymous with `anonymous_role: site_admin`, limited to
`auth.allow_networks` (haproxy sets `X-Real-IP` from the client address). **Don't route
`fleet.<tld>` to the internet while sign-in is off**; if a relay source-NATs public traffic to a
private address, `allow_networks` can't tell the difference.

When it's on, the switches are EdgeAPI's: SSO, local users, API tokens (`POST /fleet/v1/auth/token`),
static tokens (`Authorization: Bearer mtl_st_…` + `X-Metrolla-User`). Roles: `fleet_viewer`
(fleet:view), `fleet_operator` (+ fleet:device), `fleet_release_manager` (+ fleet:release,
fleet:deploy, fleet:env), `site_admin` (everything, including system:admin). Change sets use the
`change:*` permissions (`change_pipeline` for CI, machine callers only: change:view/author/apply;
`change_approver` for approvers: change:view/approve). Every write is audited (actor, kind,
redacted body, outcome) and readable at `/fleet/v1/audit` (audit:view); env values are masked in
audit rows.

## Endpoints

All under `/fleet/v1`. Fleet slugs go in one path segment (`admin%2Fbellagio-retail`); a bare
fleet name or numeric id also works. "Gated" means the route is refused in `change_control.mode:
gated` unless that class's policy needs no approval and no window (see
[Gated mode](#fleet-change-sets)).

| Method | Path | Permission | What |
|---|---|---|---|
| GET | `/ping` | public | Liveness |
| GET | `/fleets` | fleet:view | Fleets with target release, device counts and online state |
| GET | `/fleets/{fleet}/releases` | fleet:view | Releases (status, commit, images, `finalized`) plus this API's recent builds (`kind: build`) |
| POST | `/fleets/{fleet}/releases` | fleet:release | `{git: {repo, ref, path}}` → 202 build job: clone, remote build, finalize (gated) |
| GET | `/builds/{id}` | fleet:view | One build job |
| GET | `/builds/{id}/log` | fleet:view | Build output, `text/plain` |
| PUT | `/fleets/{fleet}/target-release` | fleet:deploy | `{release: id or commit}` pins the fleet (gated) |
| GET | `/fleets/{fleet}/devices` | fleet:view | Devices: release, target, online, last seen, supervisor, health (`?health=false` skips the health probe) |
| PUT | `/devices/{uuid}/target-release` | fleet:deploy | Pin one device (canary); `{release: null}` unpins (gated) |
| GET | `/fleets/{fleet}/env` | fleet:view | Fleet and service env vars, secrets masked |
| PATCH | `/fleets/{fleet}/env` | fleet:env | `{set, unset, service}` (gated) |
| PATCH | `/devices/{uuid}/env` | fleet:env | Device (or device-service) overrides (gated) |
| POST | `/devices/{uuid}/operations` | fleet:device | `{type: restart_service, service}` or `{type: reboot, force}` (gated per policy) |
| POST | `/images` | fleet:release | `{fleet, device_type, os_version, config: {network, connectivity, wifi_ssid, wifi_key}}` |
| GET | `/images/{id}` | fleet:view | Image build status; `download_url` once ready |
| GET | `/images/{id}/download` | fleet:release | The `.img.gz` (it embeds provisioning credentials) |
| GET | `/changes` | change:view | Change sets, newest first (`?state=`, `?limit=` 1–500, default 50) |
| POST | `/changes` | change:author | Fleet change set (below) |
| GET | `/changes/{id}` | change:view | Plan, approvals, schedule, rollout progress per wave and device, history |
| POST | `/changes/{id}/approve` | change:approve | `{plan_hash, comment}` |
| POST | `/changes/{id}/reject` | change:approve | `{reason}` |
| POST | `/changes/{id}/plan` | change:author | Re-plan against current state (new hash) |
| POST | `/changes/{id}/apply` | change:apply | Apply now (open window or approved emergency) |
| POST | `/changes/{id}/cancel` | change:author | `{reason}`, before it applies |
| POST | `/changes/{id}/rollback` | change:apply | `{reason}`, while applying, verifying or applied |
| GET | `/change-control` | change:view | Mode, policy, windows, freezes, and the schedule now |
| POST | `/fleets/{fleet}/site-changes` | change:author | Send a site change set to the fleet's devices in waves |
| GET | `/drift` | fleet:view | Devices off target; target pins and env vars changed outside the API (`?fleet=`) |
| GET | `/server/health` | fleet:view | API, registry, VPN, haproxy (backlog when stats are configured), builder + builder-dind disk |
| POST | `/server/operations` | system:admin | `registry_prune` (`{params: {dry_run}}`), `builder_prune`, `haproxy_restart` (gated, async) |
| GET | `/server/operations/{id}` | fleet:view | Operation state and output |

Added beyond the plan's catalog, because the catalog rows need them: `GET /builds/{id}` and
`/builds/{id}/log`, `GET /images/{id}/download`, `GET /server/operations/{id}`, the change-set
actions mirrored from EdgeAPI's catalog (`GET /changes`, `POST
/changes/{id}/approve|reject|plan|apply|cancel|rollback`, `GET /change-control`) that
metrolla-cli's `approve` and `status` use, and `GET /ping`.

metrolla_auth's routes are mounted too: `/fleet/v1/auth/{check,me,methods,token,tokens}`,
`/fleet/v1/access/{permissions,roles,bindings,sessions,users,tokens}` (access:admin),
`/fleet/v1/audit` (audit:view), and the browser sign-in at `/fleet/auth/*` with a minimal
sign-in page at `/fleet/signin`. OpenAPI: `/fleet/v1/swagger.json`, UI at `/fleet/v1/docs`.

Errors are `application/problem+json` (`type`, `title`, `status`, `detail`, `errors[{pointer,
message}]`), the same shape as EdgeAPI. openBalena API failures come back as 502 (`openbalena`),
or 404 when pine says 404.

## Endpoint reference

Request and response bodies, as the code builds them. Times are ISO 8601 UTC; `…` elides fields.

### Fleets, releases and builds

```http
GET /fleet/v1/fleets
→ 200 {"fleets": [{"id": 3, "slug": "admin/bellagio-retail", "name": "bellagio-retail",
        "target_release_id": 812, "target_release": "9f1c…", "tracks_latest": false,
        "device_count": 6, "online_count": 5}]}

GET /fleet/v1/fleets/admin%2Fbellagio-retail/releases
→ 200 {"fleet": "admin/bellagio-retail", "releases": [
        {"kind": "build", "id": "bld_…", "status": "success", "release_id": 812, …},
        {"kind": "release", "id": 812, "commit": "9f1c…", "status": "success", "finalized": true,
         "version": "0.0.0+rev3", "created_at": "…", "finished_at": "…", "is_target": true,
         "build_id": "bld_…",
         "images": [{"id": 1, "service": "edgeapi", "status": "success", "size": 1234, "content_hash": "sha256:…"}]}]}

POST /fleet/v1/fleets/admin%2Fbellagio-retail/releases
{"git": {"repo": "balena-fleets-metrollaprod", "ref": "v2.2.0", "path": "bellagio-retail"}}
→ 202 {"kind": "build", "id": "bld_…", "build_id": "bld_…", "fleet": "admin/bellagio-retail",
        "git": {…}, "status": "queued", "release_id": null, "commit": null, "source_commit": null,
        "finalized": false, "error": null, "created_by": "…", "change_id": null,
        "created_at": "…", "started_at": null, "finished_at": null,
        "build_log_url": "/fleet/v1/builds/bld_…/log"}
```

Build `status` goes `queued → cloning → building → finalizing → success`, or `failed` (with
`error`). `GET /builds/{id}` returns the same object; `/builds/{id}/log` the output as text.

### Pinning

```http
PUT /fleet/v1/fleets/{fleet}/target-release      {"release": 812}        # or a commit
→ 200 {"fleet": "…", "previous_release_id": 799, "target_release_id": 812, "target_release": "9f1c…"}

PUT /fleet/v1/devices/{uuid}/target-release      {"release": "9f1c…"}    # null unpins
→ 200 {"uuid": "…", "fleet": "…", "previous_pinned_release_id": null,
        "pinned_release_id": 812, "target_release": "9f1c…"}
```

The release must be `success` and final, or 409. Unknown fleet, device or release: 404.

### Devices and device operations

```http
GET /fleet/v1/fleets/{fleet}/devices[?health=false]
→ 200 {"fleet": "…", "devices": [{"id": 41, "uuid": "…", "name": "wandering-mountain",
        "online": true, "last_seen": "…", "api_heartbeat_state": "online", "status": "Idle",
        "release_id": 812, "release": "9f1c…", "target_release_id": 812, "target_release": "9f1c…",
        "pinned_release_id": null, "supervisor_version": "…", "os_version": "…", "fleet_id": 3,
        "health": {"status": "ok", "source": "edgeapi", "detail": {"site_status": "ok", …}}}]}

POST /fleet/v1/devices/{uuid}/operations   {"type": "restart_service", "service": "edgeapi"}
→ 202 {"uuid": "…", "type": "restart_service", "service": "edgeapi", "state": "requested"}

POST /fleet/v1/devices/{uuid}/operations   {"type": "reboot", "force": false}
→ 202 {"uuid": "…", "type": "reboot", "force": false, "state": "requested"}
```

`health.status` is `ok`, `degraded`, `failed`, `unreachable` or `unknown`; `health.source` is `edgeapi` (the
device answered through the VPN proxy) or `connectivity` (fallback, with the reason in
`detail`). Operations go through the supervisor proxy
(`/supervisor/v2/applications/<app>/restart-service`, `/supervisor/v1/reboot`).

### Env vars

```http
GET /fleet/v1/fleets/{fleet}/env
→ 200 {"fleet": "…", "env": [{"name": "API_TOKEN", "value": "***", "service": "", "level": "fleet", "secret": true},
                             {"name": "LOG_LEVEL", "value": "info", "service": "edgeapi", "level": "service", "secret": false}]}

PATCH /fleet/v1/fleets/{fleet}/env     {"set": {"LOG_LEVEL": "debug"}, "unset": ["OLD_VAR"], "service": "edgeapi"}
PATCH /fleet/v1/devices/{uuid}/env     {"set": {"LOG_LEVEL": "debug"}}
→ 200 {"fleet": "…", "device": null, "service": "edgeapi", "set": ["LOG_LEVEL"], "unset": ["OLD_VAR"],
        "changed": [{"name": "LOG_LEVEL", "from": "info", "to": "debug"}]}
```

Names must start with a letter or `_` and contain only letters, digits and `_`; a name can't be
in both `set` and `unset`; at least one is required (422 otherwise). Booleans are sent as
`true`/`false` strings. `service` empty = fleet (or device) level. Secret values are masked in
every response and audit row.

### Images

```http
POST /fleet/v1/images
{"fleet": "admin/bellagio-retail", "device_type": "jetson-orin-nx-xavier-nx-devkit", "os_version": "6.0.13",
 "config": {"network": "wifi", "connectivity": "internet", "wifi_ssid": "site", "wifi_key": "…"}}
→ 202 {"id": "img_…", "fleet": "…", "status": "building", "params": {"device_type": "…", "os_version": "…",
        "network": "wifi", "connectivity": "internet", "wifi_ssid": "site"}, "name": null, "error": null,
        "created_by": "…", "created_at": "…", "download_url": null}

GET /fleet/v1/images/img_…            → 200 same shape; status building | ready | failed
GET /fleet/v1/images/img_…/download   → 200 application/gzip stream (fleet:release)
```

`device_type` and `os_version` default to the fleet's type and the newest version the imagemaker
lists. `network` is `wifi` or `ethernet` (default), `connectivity` is `internet` (default) or
`lan`. The Wi-Fi key is passed through and never stored.

### Change sets

```http
POST /fleet/v1/changes            (Idempotency-Key: mtl-… optional)
→ 201 created, or 200 with the existing change for a repeated Idempotency-Key
  {"id": "fchg_…", "title": "…", "ticket": "…", "state": "planned", "fleets": ["admin/bellagio-retail"],
   "plan_hash": "sha256:…", "author": "…", "emergency": false, "scheduled_for": null, "window_end": null,
   "error": null, "created_at": "…", "updated_at": "…",
   "items": [...], "plan": {"items": [...], "policy": {...}, "required_approvals": 1, "secret_names": [...]},
   "policy": {...}, "reason": null, "window": "next", "not_before": null, "signature_kid": "ci-2026",
   "applied_at": null, "finished_at": null, "verify_result": null,
   "approvals": [{"approver": "…", "plan_hash": "…", "comment": "…", "approved_at": "…", "counts": true}],
   "progress": {...}, "history": [...]}

GET  /fleet/v1/changes?state=scheduled&limit=20   → 200 {"changes": [<brief change>]}
GET  /fleet/v1/changes/fchg_…                     → 200 <full change>
POST /fleet/v1/changes/fchg_…/approve  {"plan_hash": "sha256:…", "comment": "ok"}   → 200 <full change>
POST /fleet/v1/changes/fchg_…/reject   {"reason": "…"}                              → 200 <full change>
POST /fleet/v1/changes/fchg_…/plan                                                   → 200 <full change>
POST /fleet/v1/changes/fchg_…/apply                                                  → 200 <full change>
POST /fleet/v1/changes/fchg_…/cancel   {"reason": "…"}                              → 200 <full change>
POST /fleet/v1/changes/fchg_…/rollback {"reason": "…"}                              → 200 <full change>

GET /fleet/v1/change-control
→ 200 {"mode": "open", …, "policy": {…}, "default": {"approvals": 0, "window": false},
        "schedule": {"now": "…", "open_now": true, "frozen": null, "next_window": {"start": "…", "end": "…"}}}

POST /fleet/v1/fleets/{fleet}/site-changes
{"title": "…", "ticket": "…", "change": <site change set for device EdgeAPI>, "waves": [...],
 "soak_min": 10, "timeout_min": 60, "window": "next", "signature": {…}}
→ 201 / 200 <full change> (one site.change item)
```

Secret env values in `items` are masked; `site.change` items show only the inner change's title,
ticket, item count and whether it is signed. Change set body and items: see
[Fleet change sets](#fleet-change-sets).

### Drift, server health and operations

```http
GET /fleet/v1/drift[?fleet=…]
→ 200 {"count": 2, "drift": [
   {"kind": "release", "fleet": "…", "device": "<uuid>", "name": "…", "online": true, "expected": "9f1c…", "actual": "77ab…"},
   {"kind": "env", "fleet": "…", "device": null, "service": null, "name": "LOG_LEVEL",
    "expected": "debug", "actual": "info", "detail": "changed outside the API"}]}

GET /fleet/v1/server/health
→ 200 {"status": "ok", "components": {
   "api": {"status": "ok", "detail": "https://api.…/ping"}, "registry": {…}, "vpn": {…},
   "haproxy": {"status": "ok", "detail": {"health": 200, …}},
   "builder": {"status": "ok", "detail": {"http": "HTTP 404", "disk_used_pct": 41}}}}

POST /fleet/v1/server/operations   {"type": "registry_prune", "params": {"dry_run": true}}
→ 202 {"id": "sop_…", "type": "registry_prune", "params": {"dry_run": true}, "state": "running",
        "result": null, "created_by": "…", "created_at": "…", "finished_at": null}
GET /fleet/v1/server/operations/sop_…   → 200 same shape; state running | succeeded | failed, result holds the output
```

Overall `status` is the worst component status (`ok` < `degraded` < `failed`).

## Releases

`POST /fleets/{fleet}/releases {git: {repo, ref, path}}` returns a build job and runs it in the
background, one build at a time on the host (concurrent `balena push` races on release creation):

1. Shallow clone. `repo` is a name under `git.repo_base` and must be in `git.allowed_repos`;
   full URLs only with `git.allow_urls`. A commit sha is fetched directly. The git token goes in
   as an `http.extraHeader` env setting, never argv or the URL.
2. Build through the remote builder the way `balena push` does (balena-cli
   `src/utils/remote-build.ts`): `POST http://builder:80/v3/build?slug=<fleet>&headless=false&isdraft=false…`
   with the API key as bearer and a gzipped tar of the source (`.git` and `.dockerignore`
   matches left out). The streamed JSON messages go to the build log; the
   `buildLogId` metadata message names the release.
3. Wait for the release to leave `running`, then **finalize it**: `PATCH /v6/release(<id>)
   {is_final: true}`, which is what `balena release finalize` (balena-sdk `release.finalize`)
   sends. This replaces the manual finalize after the builder's auto-finalize 404s. A failed
   build is not finalized.

`builder.mode: cli` instead runs `balena push <fleet> --source <dir>` with
`BALENARC_DATA_DIRECTORY` in the service's data dir (the API key is written there as the CLI's
`token` file, never argv). Trade-off: the CLI hard-expires (see components/builder), adds ~150 MB
to the image, and only gives text output to parse for the release id, so `http` is the default
and `cli` is the fallback if the builder's HTTP contract changes. The image doesn't ship the CLI;
mount or install one before switching.

Through the public relay the CLI's heavy calls time out; the Fleet API avoids that by running
on the host and talking to `builder:80` and `api.<DNS_TLD>` inside the openBalena network.

## Fleet change sets

```http
POST /fleet/v1/changes
Idempotency-Key: mtl-…

{ "title": "datablade v2.2.0 to bellagio-retail", "ticket": "CHG0012400",
  "items": [
    { "op": "release.build", "fleet": "admin/bellagio-retail",
      "git": { "repo": "balena-fleets-metrollaprod", "ref": "v2.2.0", "path": "bellagio-retail" } },
    { "op": "env.patch", "fleet": "admin/bellagio-retail", "set": { "METROLLA_AUTH_ENABLED": "true" } },
    { "op": "release.deploy", "fleet": "admin/bellagio-retail", "release": "@built",
      "waves": [ { "devices": ["wandering-mountain"] }, { "percent": 25 }, { "percent": 100 } ],
      "soak_min": 30, "halt_if": { "health": "worse", "offline_devices": 1 } } ],
  "window": "next",
  "signature": { "kid": "ci-2026", "jws": "eyJhbGciOiJFZERTQSJ9…" } }
```

Items: `release.build {git}`, `env.patch {set, unset, service, device}`, `release.deploy
{release: id | commit | "@built", waves, soak_min, halt_if}`, `site.change {change, waves,
soak_min, timeout_min}`. `window` is `"next"` or an RFC 3339 time to wait for. `emergency: true`
needs a `reason`. `draft: true` stops at `draft`.

Lifecycle, as F10: `draft → planned → approved → scheduled → applying → verifying → applied |
rolled_back`, plus `stale`, `cancelled`, `expired`, `rejected` and `failed` (a build failed before
anything changed).

### The flow, step by step

1. **Author** (`change:author`, e.g. `mtl fleet deploy` from CI with a `change_pipeline` token):
   `POST /changes`. The caller kind is checked against `allowed_callers` and the signature
   against `trusted_keys` (403 on either). Items are validated (422 with JSON pointers), the base
   state is read, the plan and plan hash are computed, and the change is `planned` (or stays
   `draft`).
2. **Approve** (`change:approve`): each approver sends `POST …/approve {plan_hash}` with the hash
   they reviewed. When the required count of distinct approvers (not the author) is met on the
   current hash, the change is `approved`. With zero required approvals this happens at once.
3. **Schedule:** the scheduler (every `rollout.tick_s`) decides apply / wait / expire. Outside a
   window, or behind another change on the same fleet, it is `scheduled` with `scheduled_for`
   and `window_end`. `POST …/apply` applies now if a window is open (or it is an approved
   emergency).
4. **Apply:** just before applying, the base state is re-checked; if it moved the change is
   `stale` and needs `POST …/plan` and fresh approvals. Otherwise it is `applying` and runs the
   items in order (build → env → rollout waves / site fan-out).
5. **Verify:** `verifying` until devices run their target and health holds for
   `rollout.verify_settle_s`; then `applied`. A halt, worse health or a verify timeout rolls back
   (`rolled_back`). `POST …/rollback` undoes an applied change.
6. **Notify and audit:** every transition writes an audit row and an event in `history`, and
   fires matching webhooks.

### Rules

- **Plan hash.** `sha256` over the canonical JSON of `{base, items}`. Base is the state the plan
  was made against: the fleet's target release (when a deploy item rolls it), the env snapshot
  of every scope an env.patch touches, and the release a deploy names. If any of it moves before
  applying, the change goes `stale` and `POST …/plan` makes a new plan (new hash).
- **Approvals** bind to the hash (`POST …/approve {plan_hash}`); only approvals of the current
  hash count. The author can't approve. Required approvals: the strictest policy across the
  items' resource classes (`release.build`, `release.deploy`, `env`, `site.change`), or
  `emergency.approvals` for an emergency. With sign-in off everyone is `anonymous`, so a policy
  that needs approvals can't be met until sign-in is on.
- **Windows and freezes** come from `change_control` in `fleet-api.yaml` (re-read when the file
  changes): RRULE windows in `change_control.timezone`, freezes that beat windows. Policies with
  `window: false` apply at once, even inside a freeze; emergencies skip windows and freezes.
  With no windows configured, any time outside a freeze counts as open. A change that misses the
  window it was scheduled for (service down, or the fleet busy all window) `expire`s.
- **Signatures.** `require_signature: true` refuses unsigned change sets. Verification is
  exactly metrolla-cli's: compact JWS, `alg: EdDSA`, header kid = `signature.kid`, payload =
  canonical JSON of the body without `signature` (byte for byte), Ed25519 key from
  `trusted_keys[kid].ed25519` (base64 DER SPKI). For `site-changes`, the signature covers the
  request body as sent.
- **Callers.** `allowed_callers: [machine]` keeps browser sessions from authoring or applying.
- **Gated mode.** `mode: gated` refuses direct writes (`PUT target-release`, `PATCH env`,
  `POST releases`, device and server operations) with 409 `change-control-required` unless
  that class's policy needs no approval and no window. Reads are never gated. A direct pin or env
  write while a change is rolling out on the fleet gets 409 `rollout-in-progress`.
- **One rollout per fleet.** Others wait (`approved`/`scheduled`) in creation order.
- **Webhooks** (`webhooks: [{url, events, secret_file}]`) get `fleet.change.<state>` and
  `fleet.change.emergency` with the change id, ticket, fleets and state;
  `X-Metrolla-Signature: sha256=<hmac>` when a secret is set.

The generic half of these rules (canonical JSON, plan hash, signatures, config, windows,
freezes, state machine, approval rules) lives in `components/fleet-api/metrolla_change_control/`,
which has no balena, Flask or SQLAlchemy in it so it can move into `metrolla_auth` for EdgeAPI's
change control. The fleet-specific half (base state, items, rollouts) is `fleet_api/changes.py`.

### Rollouts

`{devices: [...]}` waves name canaries (device name, uuid or a unique uuid prefix);
`{percent: N}` takes the fleet up to N% of its eligible devices **in total** (cumulative, in name
order). Devices pinned to another release before the rollout are left alone unless a wave names
them. Default waves: `[{percent: 100}]`; default `halt_if`: `{health: worse}`.

Per wave: record each device's health and pin, pin it to the new release, wait until every
online device in the wave runs it (`rollout.update_timeout_min`, default 30), then soak
`soak_min` while checking `halt_if`. `health: worse` halts when a device that already runs the
new release ranks below its pre-rollout health (ok < degraded < failed); `offline_devices: N`
halts when N rolled-out devices that were online drop off. A halt puts every touched device back
on its previous pin, so it follows the fleet's unchanged previous release, and rolls back the
change's earlier items (env values restored). Only when the waves reach 100% does the fleet
itself move to the new release (per-device pins are then dropped).

After the last item the change is `verifying`: devices must run their target and health must
hold, no worse than before, for `rollout.verify_settle_s` (60 s); anything worse rolls back,
and devices still not on target after `change_control.verify.timeout_s` roll it back too.
`POST /changes/{id}/rollback` undoes an applied change the same way. The scheduler ticks every
`rollout.tick_s` (15 s); all state lives in the database, so a restart picks up where it was.

### Site change fan-out

`POST /fleets/{fleet}/site-changes {title, ticket, change, waves, soak_min, timeout_min}` wraps
a `site.change` item in a fleet change set. Each wave POSTs `change` to every online device's
EdgeAPI `/api/v1/changes` through the VPN proxy (`Idempotency-Key: fsc-<hash of fleet change
and device>`), then polls `GET /api/v1/changes/{id}` until all report `applied`, soaks, and
moves on. A device that refuses, can't be reached while online, or ends `rolled_back`,
`failed`, `expired`, `cancelled` or `rejected` stops the fan-out (`failed`); devices offline
when their wave goes out are skipped. Each site applies under its own change control and rolls
back on its own checks. **This depends on the VPN proxy path below.**

## Device reach

`reach.mode: vpn_proxy` (default) reaches a device's EdgeAPI through openBalena's VPN proxy, the
path `balena device tunnel` uses (balena-cli `src/utils/tunnel.ts`): connect to the proxy, send
`CONNECT <uuid>.balena:80 HTTP/1.0` with `Proxy-Authorization: Basic base64(root:<api key>)`,
and speak HTTP through the tunnel. The CLI reaches the proxy as TLS to `tunnel.<tld>:443`
(haproxy SNI → `vpn:3128`); inside the network the Fleet API dials `vpn:3128` directly
(`reach.proxy_tls: true` + `proxy_host: tunnel.<tld>`, `proxy_port: 443` for the TLS path).
Device health is `GET /api/v1/health` → `site_status` (ok / degraded / down). When device
sign-in is on, `reach.device_token_file` + `device_user` send a static token.

When the tunnel fails (or with `reach.mode: connectivity`) health falls back to what the API
knows: offline = failed, API heartbeat timeout = degraded, otherwise ok.

> **Caveat: not yet proven on .183. Verify it before relying on anything device-level that goes
> through the VPN** (EdgeAPI health in rollouts and `halt_if: {health: worse}`, site-change
> fan-out, device EdgeAPI token use). Check, on the real host:
> - that the vpn container's proxy listens on 3128 inside `open-balena_default`
>   (`GET /server/health` → `components.vpn`);
> - that it accepts the API key as the Basic password with user `root` (the CLI sends the
>   username for user API keys);
> - that device EdgeAPI answers on port 80 over the VPN (`GET /fleets/{fleet}/devices` shows
>   `health.source: edgeapi`).
>
> Until then a rollout's health checks silently degrade to connectivity-only health, which
> cannot see an unhealthy-but-online site, and site-change fan-out fails on the first online
> device it can't reach. Supervisor operations (restart, reboot) do **not** use the CONNECT proxy:
> they go through the openBalena API's `/supervisor` proxy (which still needs the device's VPN
> connected).

## Drift

`GET /drift` lists:
- `release`: a device whose running release isn't its target (pin, else the fleet's).
- `target_release`: the fleet target or a device pin differs from what this API last set.
- `env`: a variable this API set (or removed) whose live value differs. The API records each
  applied value (only its sha256 for secrets), so a change made with the CLI or dashboard shows
  up here. Variables the API never touched are not tracked.

## Server health and upkeep

`GET /server/health`: API `/ping`, registry `/v2/` (200/401 = up), the VPN proxy port, haproxy
`/health` (503 when api, registry or vpn has no server) and its per-backend queue when
`server.haproxy_stats_url` + `haproxy_stats_auth_file` are set, the builder answering HTTP, and
builder-dind disk use (warn 80%, fail 90%: run `builder_prune`).

`POST /server/operations` runs the runbook commands through the mounted docker socket:

| type | command |
|---|---|
| `builder_prune` | `docker exec open-balena-builder-dind-1 docker image prune -af` |
| `haproxy_restart` | `docker restart open-balena-haproxy-1` |
| `registry_prune` | `docker exec open-balena-registry-1 registry garbage-collect [--dry-run] /etc/docker/registry/config.yml` |

`registry_prune` defaults to `--dry-run`; pass `{params: {dry_run: false}}` to delete. Its
container name and config path are settings to verify on .183 before the first real run.
Operations are async: poll `GET /server/operations/{id}`.

## Images

`POST /images` calls the imagemaker's `/api/build` (device type and OS version default to the
fleet's and the newest listed). `GET /images/{id}` maps building/done/error to
building/ready/failed. The Wi-Fi key is passed through and never stored. The imagemaker binds
127.0.0.1 by default; set `imagemaker_host: 172.17.0.1` (role var) so the container can reach
it at `host.docker.internal`. The imagemaker keeps jobs in memory: after it restarts, earlier
images report failed.

## State

SQLite at `/data/fleet-api.db` on the `fleet-api-data` volume: builds, images, change sets,
approvals and events, applied env values and targets, server operations, and metrolla_auth's
sessions, tokens, users and audit log. One process with a low write rate on one host doesn't need
a database server, and the volume survives rebuilds. Back it up with the other openBalena state.
Env values in pending change sets (and the previous values kept to roll them back) are stored as
given, secrets included; the API never returns them.

Run exactly one process: the scheduler lives in it, and builds and rollouts are serialized
in-process. Don't raise gunicorn's `-w`.

## Tests

```bash
cd components/fleet-api
pip install -r requirements.txt -r requirements-auth.txt -r requirements-test.txt
python -m pytest            # or, from the repo root: make test-fleet-api
```

`requirements-auth.txt` installs metrolla-auth from the private EdgeAPI repo (branch
`feat/relay-auth`); with a local EdgeAPI checkout, `pip install <EdgeAPI>/packages/metrolla_auth`
instead. CI runs the same tests in the `fleet-api tests` job when the `EDGEAPI_READ_TOKEN` repo
secret is set.

They run against a fake pine API (with the supervisor proxy and the builder's `/v3/build`), a
fake VPN proxy with device EdgeAPIs behind it, and a fake imagemaker, all real HTTP servers on
127.0.0.1, with an injectable clock. Docker commands are recorded, not run. Nothing touches a
real openBalena.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `fleet.<tld>` answers 503 | The `openbalena-fleet` project isn't running or is unhealthy: `docker compose -p openbalena-fleet ps`, `docker logs openbalena-fleet-fleet-api-1`. Or haproxy wasn't reloaded with the new route. |
| Container exits at start | The `change_control` section of `fleet-api.yaml` is invalid (the error names the JSON pointer), or the YAML doesn't parse. A bad image build fails earlier: the Dockerfile checks `import metrolla_auth`. |
| 403 "This network is not allowed to reach this host" | The client address isn't in `auth.allow_networks`. Check what haproxy puts in `X-Real-IP`; a relay that source-NATs changes it. |
| 502 `openbalena` with `no balena API key configured` | `secrets/balena_api_key` is empty or not mounted at `/run/secrets`. |
| 502 `openbalena` with a TLS error | `balena.verify` points at a CA that doesn't match. Confirm `/certs/root-ca.pem` exists in `open-balena_certs-data`; set `fleet_api_tls_verify: false` only while proving the path. |
| Build `failed` | Read `GET /builds/{id}/log`. Builder-dind disk full → `builder_prune`. Git refused (403/422 `git`) → repo not in `git.allowed_repos` or the token can't read it. A failed build is never finalized. |
| Build stays `queued` | Another build is running; builds are one at a time. |
| Device `health.source: connectivity` with "EdgeAPI not reachable" | The VPN proxy path isn't working (see [Device reach](#device-reach)). Check `components.vpn` in `/server/health`, the Basic auth user `root`, and that EdgeAPI listens on :80 on the device. |
| 409 `change-control-required` | `change_control.mode: gated` and this class's policy needs approval or a window: send a change set. |
| 409 `rollout-in-progress` / `fleet-busy` | A change is applying on the fleet. Wait, or cancel/roll it back. |
| 409 `stale` on approve or apply | The fleet's target, env or named release moved since planning: `POST /changes/{id}/plan` and approve the new hash. |
| 409 `plan-hash-mismatch` / 403 `author-cannot-approve` | Approve with the current `plan_hash`; a different user from the author must approve. |
| 403 `signature-required` / `unknown-key` / `signature-mismatch` | `require_signature` is on, the `kid` isn't in `trusted_keys`, or the body changed after signing (canonical JSON without `signature`). |
| 403 `caller-not-allowed` | `allowed_callers` excludes this caller kind (e.g. a browser session when only `machine` is allowed). |
| Change sits in `planned` | Not enough approvals. With sign-in off every caller is `anonymous`, so a policy that needs approvals can't be met. |
| Change sits in `scheduled`, then `expired` | No window open: `GET /change-control` shows `open_now`, `frozen` and `next_window`. |
| Image `failed` "no longer knows this build" | The imagemaker restarted (it keeps jobs in memory). Rebuild. |
| Image build refused / imagemaker unreachable | `imagemaker_host` not set to the bridge gateway, or `imagemaker_token` doesn't match `IMAGEMAKER_TOKEN`. |
| `registry_prune` fails | The registry container name or config path differs on this host: set `server.containers.registry` / `server.registry_config`. |

## Lab checklist (.183)

1. `GET /server/health` all ok; `vpn` up confirms the proxy port.
2. `GET /fleets/{fleet}/devices`: health `source: edgeapi` proves the CONNECT path (else it says
   why it fell back).
3. `POST /fleets/{fleet}/releases` from a tag → `success`, `finalized: true`, and `balena
   releases <fleet>` shows it final.
4. A change set with a canary wave on a labtestbed edge, `soak_min: 5`; break the canary's
   health and watch it halt and re-pin.
5. `PATCH env` through the API, change the same var with the balena CLI, see it in `/drift`.
6. `registry_prune` with the default dry run; check the container name and config path.
