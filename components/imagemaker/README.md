# Imagemaker

A small, bespoke web service that builds **ready-to-flash, fleet-preconfigured balenaOS images** for your openBalena instance — including the internal→public endpoint rewrite that makes remote devices actually connect (see [../../docs/dns-tld-split.md](../../docs/dns-tld-split.md)).

## What it does
1. Lists your fleets + device types (queried from the openBalena DB).
2. Downloads the matching balenaOS base (cached; from balena's public OS servers).
3. `balena config generate` against **your** backend → injects `config.json`.
4. For the **`internet`** variant, rewrites every `${DNS_TLD}` endpoint to `${PUBLIC_TLD}`.
5. Injects `config.json` into the image's boot partition and serves the compressed `.img.gz`.

## Files
| File | Role |
|---|---|
| `server.js` | Node web service (localhost:8090) + JSON API + download endpoint |
| `index.html` | Single-page UI |
| `build-image.sh` | Does one build (download → config → rewrite → inject → compress) |
| `prebuild.sh` | Batch-builds `lan`+`internet` variants for a manifest of fleets |
| `imagemaker.service` | systemd unit |

## Requirements
- A balena CLI on the host, **logged into your openBalena backend** in the service user's `~/.balena`.
- The openBalena **root CA** at `OPENBALENA_ROOT_CA` (so the CLI trusts your self-signed certs).
- Passwordless `sudo` for the service user (`losetup`, `mount`, `partprobe`, `blkid`, `docker exec`).
- `pigz` installed.

## Install (manual)
```bash
sudo mkdir -p /opt/openbalena/imagemaker /var/lib/imagemaker/{cache,builds,dist}
sudo cp server.js index.html build-image.sh prebuild.sh /opt/openbalena/imagemaker/
sudo chmod +x /opt/openbalena/imagemaker/*.sh
sudo cp imagemaker.service /etc/systemd/system/
# edit the Environment= lines (DNS_TLD, PUBLIC_TLD, user, CA path)
sudo systemctl daemon-reload && sudo systemctl enable --now imagemaker
```
Or use the Ansible role: [`ansible/roles/imagemaker`](../../ansible/roles/imagemaker).

## Access
The service binds to `127.0.0.1:8090` on purpose. Reach it via SSH tunnel
(`ssh -L 8090:127.0.0.1:8090 user@host`) or put it behind your reverse proxy with auth.

## Variants
- **`internet`** — public endpoints; use for any device not on the LAN.
- **`lan`** — internal endpoints; only for devices on the network where `${DNS_TLD}` resolves.

> If you deploy with `DNS_TLD == PUBLIC_TLD`, the rewrite is a no-op and both variants are identical — simplest if you don't need an internal-only path.
