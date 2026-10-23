# Fleet Control API

`/fleet/v1` on the openBalena host: releases built and finalized from a git ref, wave rollouts
under change control, env vars, device operations, flashable images, drift and server upkeep,
with metrolla_auth sign-in, roles and audit. Full documentation: [docs/fleet-api.md](../../docs/fleet-api.md).

| Path | What |
|---|---|
| `fleet_api/` | The Flask + flask-restx service (`app.py` routes; `balena.py` pine client; `builds.py`, `rollout.py`, `fanout.py`, `changes.py`, `reach.py`, `server.py`, `images.py`) |
| `metrolla_change_control/` | Change-set rules with no balena in them: plan hash, signatures, windows, freezes, lifecycle. Meant to move into metrolla_auth for EdgeAPI's change control |
| `tests/` | pytest against fake pine API, builder, VPN proxy, device EdgeAPI and imagemaker |
| `Dockerfile`, `docker-compose.yml` | Image and compose project (`openbalena-fleet` on `open-balena_default`) |
| `fleet-api.example.yaml`, `auth.example.yaml` | `/etc/fleet-api/` config |

```bash
pip install -r requirements.txt -r requirements-auth.txt -r requirements-test.txt
python -m pytest
```

`requirements-auth.txt` installs metrolla-auth from the private EdgeAPI repo (branch
`feat/relay-auth` until PR #101 merges); for image builds put the package in `vendor/metrolla_auth`
or pass a token as the `github_token` build secret.
