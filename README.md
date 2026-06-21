# open-balena-extension

[![CI](https://github.com/Metrolla-Inc/open-balena-extension/actions/workflows/ci.yml/badge.svg)](https://github.com/Metrolla-Inc/open-balena-extension/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![openBalena](https://img.shields.io/badge/openBalena-extension-3a6df0.svg)](https://github.com/balena-io/open-balena)
[![Ansible](https://img.shields.io/badge/deploy-Ansible-EE0000.svg?logo=ansible&logoColor=white)](ansible/)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

> **The production pieces openBalena's open-source core leaves out** — a remote builder, an
> image-build UI, a custom ingress, and an admin dashboard — packaged so anyone can bolt them
> onto their own self-hosted deployment.

openBalena core gives you an API, registry, VPN, and PKI — but **no remote builder** (so
`balena push` 404s), **no image-build UI**, and **no admin dashboard**. This repo fills those
gaps, hardens the rough edges, and documents the one configuration nuance (a split
internal/public DNS) that ties everything together.

---

## Why we built this

We run fleets of edge devices **in production on self-hosted openBalena** — not balenaCloud —
for cost, data-control, and on-prem/air-gapped reasons. openBalena's open source is excellent at
the genuinely hard parts (device identity, the VPN, the registry, a full PKI), but it
deliberately ships only the *backend*. Out of the box you **cannot**:

- **`balena push` a fleet** — core ships no builder at all (balenaCloud's is closed-source), so
  the command just returns `404 Remote builder`.
- **Hand someone a flashable image** for a specific fleet — there's no UI; you're on your own with
  `balena config generate`, partition mounting, and endpoint rewriting.
- **See or shell into a device** from a browser — no dashboard, no web terminal.

balenaCloud does all of this; it's just not open. So every serious self-hoster ends up rebuilding
the *same* missing pieces by trial and error — and rediscovering the *same* sharp edges:

- the community builder image bundles a **balena CLI that hard-expires** and silently breaks every build,
- the builder needs a **dedicated plaintext Docker daemon** that the TLS test-dind can't provide,
- and an **internal-vs-public DNS split** that, if you get it wrong, leaves devices showing
  "online" while their VPN never connects.

This repo is those pieces — **assembled, hardened, and documented, exactly as we run them** — so
you don't have to learn it the hard way.

## What's in here

| Component | What it adds | Status |
|---|---|---|
| **[Remote builder](components/builder/)** | `balena push <fleet>` against openBalena (core ships none — balenaCloud's is closed-source) | homemade |
| **[Imagemaker](components/imagemaker/)** | Web UI + scripts to build ready-to-flash, fleet-preconfigured device images | homemade |
| **[Custom haproxy](components/haproxy/)** | Hand-written static ingress config (routes the builder + extras) | homemade |
| **[Admin dashboard](components/admin/)** | Web UI / device terminal (the community [open-balena-ui](https://github.com/ob-community/open-balena-ui) stack) | third-party |

Plus **[Ansible playbooks](ansible/)** to deploy core + all extensions from scratch, helper
scripts (`make bootstrap` / `make doctor`), and **[docs](docs/)**:
[architecture map](docs/architecture.md) · [DNS-split explainer](docs/dns-tld-split.md) ·
[public ingress & Cloudflare](docs/public-ingress.md) · [registry edge cache (R2)](docs/registry-edge-cache.md).

## How it fits together

Three independent `docker compose` projects share one network, behind a single haproxy ingress:

```
   devices / CLI ──443──►  haproxy (custom static cfg, single ingress)
                              │ routes api. registry2. vpn. cloudlink. delta. s3. ca. builder.
        ┌─────────────────────┼─────────────────────────┐
        ▼                     ▼                          ▼
   open-balena CORE       builder + builder-dind     admin dashboard
   (upstream, unmodified) (remote builds)            (ui · remote · postgrest)
        ▲
        │ builds flashable, fleet-preconfigured images
   imagemaker (systemd service + web UI on :8090)
```

Core openBalena is **unmodified upstream** — everything here sits *around* it (a compose override
plus a replacement haproxy config). Full picture: **[docs/architecture.md](docs/architecture.md)**.

## Requirements

- An **Ubuntu 22.04/24.04** host with Docker + compose v2, plus `git`, `make`, `pigz`.
- **DNS:** a wildcard `*.${PUBLIC_TLD}` → the host IP (covers `api.`, `vpn.`, `cloudlink.`,
  `registry2.`, `delta.`, `builder.`, …), and ports **80/443** reachable.
- `ansible` on your workstation for the recommended deploy path.

`make doctor` verifies all of the above for you (see below).

## Quick start

```bash
git clone https://github.com/Metrolla-Inc/open-balena-extension
cd open-balena-extension

DNS_TLD=ob.example.com PUBLIC_TLD=ob.example.com make bootstrap   # scaffold .env + generate secrets
$EDITOR .env                  # confirm DNS_TLD / PUBLIC_TLD; point *.${PUBLIC_TLD} at the host
make doctor                   # check tooling, DNS wildcard, (later) live endpoints
make deploy                   # full Ansible deploy of core + all extensions
```

`make` with no target lists every command. Prefer to do it by hand, or add a single component to
an existing openBalena? Each component's README has standalone steps.

## Setup, step by step

The fast path above is four `make` targets. Here's what each one actually does, and how to
recover if a step complains.

### 1. Point DNS at the host
Decide your TLDs first ([dns-tld-split.md](docs/dns-tld-split.md)) and create a **wildcard**
`*.${PUBLIC_TLD}` A-record to the host. This is the single most common thing to get wrong —
`make doctor` resolves a random `*.${PUBLIC_TLD}` label specifically to catch it.

### 2. `make bootstrap` — scaffold config
Copies `.env.example` → `.env`, generates every secret (and sets `PGRST_JWT_SECRET ==
OPEN_BALENA_JWT_SECRET`, which the admin stack requires), generates the `IMAGEMAKER_TOKEN`, and
auto-reads the builder token from a running core if it finds one. It's **idempotent** — re-running
never clobbers values you've already set. Pass `DNS_TLD=… PUBLIC_TLD=…` to fill those in too, or
edit `.env` afterwards. For the Ansible path, `make ansible-config` also scaffolds (and you should
`ansible-vault encrypt`) `ansible/group_vars/all.yml`.

### 3. `make doctor` — preflight
Checks tooling, the DNS wildcard, and — once core is up — the live endpoints (`api/ping`, the
`builder.` route). Run it **before** deploying (catches DNS/tooling gaps) and **after** (confirms
the stack is reachable). It exits non-zero on any hard failure.

### 4. `make deploy` — bring it up
Runs the Ansible playbook: clones + starts upstream core, lays down the custom haproxy config,
builds and starts the patched builder + its plaintext dind, installs the imagemaker service, and
brings up the admin/delta projects. Deploy a single piece with `make deploy-builder`,
`make deploy-imagemaker`, etc. (any role tag).

### 5. Verify
```bash
curl -k https://api.${PUBLIC_TLD}/ping                                   # OK
curl -k -o /dev/null -w '%{http_code}\n' https://builder.${PUBLIC_TLD}/  # 404 = builder route alive
balena login --credentials                                               # against ${PUBLIC_TLD}
balena push <fleet>                                                      # streams a remote build
```

Full prose walkthrough, the **manual (non-Ansible) path**, and a backup checklist live in
**[docs/setup.md](docs/setup.md)**.

## The core idea: internal vs public TLD

openBalena issues every device endpoint (`api.`, `vpn/cloudlink.`, `registry2.`, `delta.`) under one `DNS_TLD`. You can run an **internal** `DNS_TLD` (e.g. `example.local`) reached **publicly** via a proxy (e.g. `ob.example.com`) and rewrite device endpoints to the public name (the imagemaker's `__internet` variant) — but for **devices in the wild, the simplest and most robust choice is to deploy with `DNS_TLD` set to your public hostname** so the API advertises public endpoints natively. **[Read the full explainer →](docs/dns-tld-split.md)** — getting this wrong is the #1 cause of "device online but VPN won't connect."

## Deploying for field devices (read these)
If devices live on the internet rather than your LAN, three things matter — all learned the hard way:
1. **Public ingress must carry raw TCP, not just HTTP.** The VPN is openVPN-over-TCP; an HTTP-only front (Cloudflare Tunnel, nginx `http`) makes devices show *online* but never connect the VPN. Use a tiny relay VM + WireGuard + raw L4 forward. → **[public-ingress.md](docs/public-ingress.md)**
2. **Use Cloudflare for DNS + R2, not the Tunnel.** Wildcard DNS to the relay; R2 (free egress) as the registry's blob store so devices pull from the edge instead of your uplink. → **[registry-edge-cache.md](docs/registry-edge-cache.md)**
3. **Deploy `DNS_TLD` = your public hostname.** Avoids the `__lan`/`__internet` split entirely.

## Security

The imagemaker builds images that **embed fleet-provisioning credentials**, so it must not be
reachable unauthenticated: set `IMAGEMAKER_TOKEN` (generated for you by `make bootstrap`) and/or
keep it loopback-only behind an SSH tunnel or authenticated proxy. The builder's `builder-dind`
runs a **plaintext, privileged Docker daemon** on the compose network — never publish `:2375` to
the host, and treat everything on `open-balena_default` as trusted. See
[components/imagemaker](components/imagemaker/) and [components/builder](components/builder/) for
the details and the rationale.

## Using with AI assistants

This repo ships [`AGENTS.md`](AGENTS.md) (and [`CLAUDE.md`](CLAUDE.md)) describing the architecture,
invariants, and gotchas so AI coding assistants can work on it safely. Point your assistant at them.

## Contributing

PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). CI runs shell/Node lint, a secret scan, and
an Ansible syntax check on every PR; `make lint` runs the same checks locally.

## License

[MIT](LICENSE). Not affiliated with or endorsed by Balena. openBalena, balenaOS, balenaCloud, and
Balena are trademarks of Balena Ltd.
