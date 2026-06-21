# open-balena-extension

A toolkit of **production extensions for [openBalena](https://github.com/balena-io/open-balena)** — the pieces the open-source core doesn't ship, packaged so anyone can add them to their own deployment.

openBalena core gives you an API, registry, VPN, and PKI — but **no remote builder** (so `balena push` 404s), **no image-build UI**, and **no admin dashboard**. This repo fills those gaps, and documents the one configuration nuance (a split internal/public DNS) that ties them together.

## What's in here

| Component | What it adds | Status |
|---|---|---|
| **[Remote builder](components/builder/)** | `balena push <fleet>` against openBalena (core ships none — balenaCloud's is closed-source) | homemade |
| **[Imagemaker](components/imagemaker/)** | Web UI + scripts to build ready-to-flash, fleet-preconfigured device images | homemade |
| **[Custom haproxy](components/haproxy/)** | Hand-written static ingress config (routes the builder + extras) | homemade |
| **[Admin dashboard](components/admin/)** | Web UI / device terminal (the community [open-balena-ui](https://github.com/ob-community/open-balena-ui) stack) | third-party |

Plus **[Ansible playbooks](ansible/)** to deploy core + all extensions from scratch, and **[docs](docs/)** including an [architecture map](docs/architecture.md) and the [DNS-split explainer](docs/dns-tld-split.md).

## Quick start

```bash
git clone https://github.com/<you>/open-balena-extension
cd open-balena-extension
cp .env.example .env          # set DNS_TLD, PUBLIC_TLD, and generate secrets
# then either:
cd ansible && ansible-playbook -i inventory.ini site.yml      # full deploy
# or add a single component to an existing openBalena (see each component's README)
```

## The core idea: internal vs public TLD

openBalena issues every device endpoint (`api.`, `vpn/cloudlink.`, `registry2.`, `delta.`) under one `DNS_TLD`. Many self-hosters run an **internal** `DNS_TLD` (e.g. `example.local`) but reach the instance **publicly** via a reverse proxy (e.g. `ob.example.com`). Remote devices can't resolve `*.example.local`, so their endpoints must be rewritten to the public name. The imagemaker does this automatically (its `__internet` image variant); the builder and haproxy are configured around the same split. **[Read the full explainer →](docs/dns-tld-split.md)** — getting this wrong is the #1 cause of "device online but VPN won't connect."

## Using with AI assistants

This repo ships [`AGENTS.md`](AGENTS.md) (and [`CLAUDE.md`](CLAUDE.md)) describing the architecture, invariants, and gotchas so AI coding assistants can work on it safely. Point your assistant at them.

## License

[Apache-2.0](LICENSE). Not affiliated with or endorsed by Balena.
