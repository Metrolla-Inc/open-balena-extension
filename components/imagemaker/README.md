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
- **Scoped** passwordless `sudo` for the service user: `losetup`, `mount`, `umount`, `partprobe`,
  `blkid`, and `/usr/local/bin/ob-fleets` only. Note this list deliberately excludes `cp` and
  `docker` (each is effectively host-root) — see Security below.
- `pigz` installed.
- **`BALENARC_BALENA_URL` set** to your backend hostname (the systemd unit sets it). Without it,
  `balena config generate` uses the CLI's stored default and fails with `fetch failed`.
- **Split-horizon `/etc/hosts`** on the host if you front the instance with a relay/proxy — point the
  openBalena hostnames at the local haproxy so config generate doesn't hairpin out and back. See
  [../../docs/public-ingress.md](../../docs/public-ingress.md).

## Install (manual)
```bash
sudo mkdir -p /opt/openbalena/imagemaker /var/lib/imagemaker/{cache,builds,dist}
sudo cp server.js index.html build-image.sh prebuild.sh /opt/openbalena/imagemaker/
sudo chmod +x /opt/openbalena/imagemaker/*.sh
sudo install -o root -g root -m755 ob-fleets.sh /usr/local/bin/ob-fleets   # scoped DB-read helper
sudo cp imagemaker.service /etc/systemd/system/
# edit the Environment= lines (DNS_TLD, PUBLIC_TLD, user, CA path, IMAGEMAKER_TOKEN)
echo '<user> ALL=(root) NOPASSWD: /usr/sbin/losetup, /usr/bin/mount, /usr/bin/umount, /usr/sbin/partprobe, /usr/sbin/blkid, /usr/local/bin/ob-fleets' | sudo tee /etc/sudoers.d/imagemaker
sudo systemctl daemon-reload && sudo systemctl enable --now imagemaker
```
Or use the Ansible role: [`ansible/roles/imagemaker`](../../ansible/roles/imagemaker).

## Access & security
A built image embeds `config.json` with **fleet-provisioning credentials**, so the service must
not be reachable unauthenticated.
- **Auth.** Set `IMAGEMAKER_TOKEN` (a shared secret) and open the UI as
  `http://127.0.0.1:8090/?token=<value>`; the API and download links carry it automatically
  (header `x-imagemaker-token` / `?token=`). Binding to a non-loopback `HOST` **without** a token
  is refused unless you set `IMAGEMAKER_ALLOW_NO_AUTH=1`.
- **Network.** It still binds `127.0.0.1:8090` by default — reach it via SSH tunnel
  (`ssh -L 8090:127.0.0.1:8090 user@host`) or a reverse proxy. The token is defence-in-depth on top.
- **Least privilege.** The DB read runs through `ob-fleets` (one fixed read-only query) instead of
  `sudo docker`; the boot partition is mounted `uid=<service_user>` so the config copy needs no
  `sudo cp`. The Wi-Fi PSK is passed to `build-image.sh` via the environment, never argv (so it
  doesn't leak through `ps`).

## Variants
- **`internet`** — public endpoints; use for any device not on the LAN.
- **`lan`** — internal endpoints; only for devices on the network where `${DNS_TLD}` resolves.

> If you deploy with `DNS_TLD == PUBLIC_TLD`, the rewrite is a no-op and both variants are identical — simplest if you don't need an internal-only path.
