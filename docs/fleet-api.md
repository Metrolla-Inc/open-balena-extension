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

## Endpoints

All under `/fleet/v1`. Fleet slugs go in one path segment (`admin%2Fbellagio-retail`); a bare
fleet name or numeric id also works.

| Method | Path | Permission | What |
|---|---|---|---|
| GET | `/fleets` | fleet:view | Fleets with target release, device counts and online state |
| GET | `/fleets/{fleet}/releases` | fleet:view | Releases (status, commit, images, `finalized`) plus this API's recent builds (`kind: build`) |
| POST | `/fleets/{fleet}/releases` | fleet:release | `{git: {repo, ref, path}}` → 202 build job: clone, remote build, finalize |
| PUT | `/fleets/{fleet}/target-release` | fleet:deploy | `{release: id or commit}` pins the fleet (gated) |
| GET | `/fleets/{fleet}/devices` | fleet:view | Devices: release, target, online, last seen, supervisor, health (`?health=false` skips the health probe) |
| PUT | `/devices/{uuid}/target-release` | fleet:deploy | Pin one device (canary); `{release: null}` unpins (gated) |
| GET | `/fleets/{fleet}/env` | fleet:view | Fleet and service env vars, secrets masked |
| PATCH | `/fleets/{fleet}/env` | fleet:env | `{set, unset, service}` (gated) |
| PATCH | `/devices/{uuid}/env` | fleet:env | Device (or device-service) overrides (gated) |
| POST | `/devices/{uuid}/operations` | fleet:device | `{type: restart_service, service}` or `{type: reboot, force}` (gated per policy) |
| POST | `/images` | fleet:release | `{fleet, device_type, os_version, config: {network, connectivity, wifi_ssid, wifi_key}}` |
| GET | `/images/{id}` | fleet:view | Image build status; `download_url` once ready |
| POST | `/changes` | change:author | Fleet change set (below) |
| GET | `/changes/{id}` | change:view | Plan, approvals, schedule, rollout progress per wave and device |
| POST | `/fleets/{fleet}/site-changes` | change:author | Send a site change set to the fleet's devices in waves |
| GET | `/drift` | fleet:view | Devices off target; target pins and env vars changed outside the API (`?fleet=`) |
| GET | `/server/health` | fleet:view | API, registry, VPN, haproxy (backlog when stats are configured), builder + builder-dind disk |
| POST | `/server/operations` | system:admin | `registry_prune` (`{params: {dry_run}}`), `builder_prune`, `haproxy_restart` (gated, async) |

Added beyond the plan's catalog, because the catalog rows need them: `GET /builds/{id}` and
`/builds/{id}/log`, `GET /images/{id}/download` (fleet:release: the image embeds provisioning
credentials), `GET /server/operations/{id}`, the change-set actions mirrored from EdgeAPI's
catalog (`GET /changes`, `POST /changes/{id}/approve|reject|plan|apply|cancel|rollback`,
`GET /change-control`), `GET /ping` (public liveness), and metrolla_auth's routes:
`/fleet/v1/auth/*`, `/fleet/v1/access/*`, `/fleet/v1/audit`, browser sign-in at `/fleet/auth/*`.
OpenAPI: `/fleet/v1/swagger.json`, UI at `/fleet/v1/docs`.

Errors are `application/problem+json` (`type`, `title`, `status`, `detail`, `errors[{pointer,
message}]`), the same shape as EdgeAPI.

## Sign-in and roles

`/etc/fleet-api/auth.yaml` holds metrolla_auth's `auth`, `access` and `site` sections. Off by
default: every caller is anonymous with `anonymous_role: site_admin`, limited to
`auth.allow_networks` (haproxy sets `X-Real-IP` from the client address). **Don't route
`fleet.<tld>` to the internet while sign-in is off**; if a relay source-NATs public traffic to a
private address, `allow_networks` can't tell the difference.

When it's on, the switches are EdgeAPI's: SSO, local users, API tokens (`POST /fleet/v1/auth/token`),
static tokens (`Authorization: Bearer mtl_st_…` + `X-Metrolla-User`). Roles: `fleet_viewer`
(fleet:view), `fleet_operator` (+ fleet:device), `fleet_release_manager` (+ fleet:release,
fleet:deploy, fleet:env), `site_admin` (everything). Change sets use the `change:*` permissions
(`change_pipeline` for CI, `change_approver` for approvers). Every write is audited (actor, kind,
redacted body, outcome) and readable at `/fleet/v1/audit`; env values are masked in audit rows.

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
   sends. This replaces the manual finalize after the builder's auto-finalize 404s.

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
  `window: false` apply at once; emergencies skip windows and freezes. A change that misses the
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
back on its own checks.

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

**Not yet proven on .183 — verify before relying on it:** that the vpn container's proxy listens
on 3128 inside `open-balena_default`; that it accepts the API key as the Basic password with user
`root` (the CLI sends the username for user API keys); and that device EdgeAPI answers on port 80
over the VPN.

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

## Deploy

Ansible, off by default:

```yaml
# group_vars/all.yml (vault it)
deploy_fleet_api: true
fleet_api_balena_api_key: "…"         # balena api-key generate fleet-api
fleet_api_github_token: "…"           # read EdgeAPI for metrolla-auth (or set fleet_api_metrolla_auth_src)
fleet_api_git_token: "…"              # read the fleet repos
imagemaker_host: 172.17.0.1
```

`make deploy-fleet_api` builds `metrolla-fleet-api:local`, writes
`<install_root>/fleet-api/{config,secrets}`, and starts the `openbalena-fleet` compose project on
`open-balena_default`. The `fleet.` haproxy route is in `components/haproxy/haproxy.cfg` (run the
haproxy role to install it). Role variables are in `ansible/roles/fleet_api/defaults/main.yml`.

By hand:

```bash
cp -r ~/src/EdgeAPI/packages/metrolla_auth components/fleet-api/vendor/
docker build -t metrolla-fleet-api:local components/fleet-api
mkdir -p /opt/openbalena/fleet-api/{config,secrets}
cp components/fleet-api/fleet-api.example.yaml /opt/openbalena/fleet-api/config/fleet-api.yaml
cp components/fleet-api/auth.example.yaml /opt/openbalena/fleet-api/config/auth.yaml
install -m600 /dev/stdin /opt/openbalena/fleet-api/secrets/balena_api_key <<<"$KEY"
cd /opt/openbalena/fleet-api && cp …/components/fleet-api/docker-compose.yml . \
  && DNS_TLD=… docker compose -p openbalena-fleet up -d
```

The container mounts the docker socket (server operations) and holds the balena admin key:
like everything on `open-balena_default`, it is trusted and must not be published.

## Tests

```bash
cd components/fleet-api
pip install -r requirements.txt -r requirements-auth.txt -r requirements-test.txt
python -m pytest
```

They run against a fake pine API (with the supervisor proxy and the builder's `/v3/build`), a
fake VPN proxy with device EdgeAPIs behind it, and a fake imagemaker, all real HTTP servers on
127.0.0.1, with an injectable clock. Docker commands are recorded, not run. Nothing touches a
real openBalena.

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
