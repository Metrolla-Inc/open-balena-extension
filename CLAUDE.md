# CLAUDE.md

Guidance for Claude Code (and other AI assistants) lives in **[AGENTS.md](AGENTS.md)** — read it first.

Quick reminders specific to this repo:
- Internal `DNS_TLD` vs public `PUBLIC_TLD` is the central concept — see [docs/dns-tld-split.md](docs/dns-tld-split.md).
- Never commit secrets; templatize to `${ENV}` (the `.gitignore` enforces this).
- One `balena push` at a time; validate `haproxy.cfg` with `haproxy -c` before reloading.
- Device VPN/SSH access has real limits — see the "Device access reality" section in AGENTS.md before attempting to "remote in and fix" a device.
- Fleet Control API (`components/fleet-api`, docs in [docs/fleet-api.md](docs/fleet-api.md)) — see AGENTS.md invariants 13–16 and its "Fleet Control API" section:
  - it must never return the balena API key (responses, logs, errors, audit, OpenAPI);
  - `metrolla_change_control/` stays free of balena specifics (stdlib, `dateutil`, `cryptography` only) because it is moving into `metrolla_auth`;
  - the VPN proxy path (`vpn:3128` CONNECT) is unproven on .183 — verify it before relying on device-level actions over the VPN;
  - tests: `cd components/fleet-api && pip install -r requirements.txt -r requirements-auth.txt -r requirements-test.txt && python -m pytest -q` (or `make test-fleet-api`).
- Record every change in `CHANGELOG.md` under `[Unreleased]`.
