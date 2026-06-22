# Admin dashboard (third-party)

A web console + device terminal for openBalena, using the community
[ob-community / dcnext](https://github.com/ob-community) images: `open-balena-ui`,
`open-balena-remote`, `open-balena-postgrest`. **Not part of this project** — included here
so a full deployment is reproducible. Runs as its own compose project (`openbalena-admin`).

## Bring up
```bash
cp components/admin/docker-compose.yml ${INSTALL_ROOT}/admin/docker-compose.yml
# provide its certs (TLS for the remote/postgrest services) under ${INSTALL_ROOT}/admin/certs
cd ${INSTALL_ROOT}/admin && docker compose up -d        # serves the dashboard host
```
Secrets come from the environment (`OPEN_BALENA_JWT_SECRET`, `OPEN_BALENA_S3_*`,
`PGRST_JWT_SECRET`) — never hardcode them.

## Important caveat: device terminal needs the VPN
The dashboard's web terminal connects to a device **through the openBalena VPN** and tunnels via
cloudlink. If a device's VPN is down (e.g. its `config.json` endpoints are still internal), the
terminal **cannot reach it** — the same limitation as `balena device ssh`. Fix such devices by
reflashing with a correct `internet` image, not via the dashboard. See
[../../docs/dns-tld-split.md](../../docs/dns-tld-split.md).

## Making the web terminal actually work (443 + a real shell)
Stock `open-balena-remote` redirects the browser to per-session **high ports (10000–10009)** and
authenticates with an **ephemeral SSH key** — so behind Cloudflare/a proxy the terminal window is
blank ("can't connect"), and even when it loads the shell gets `Permission denied` (balenaOS only
trusts keys baked into `os.sshKeys`). [`patch-remote-443.sh`](patch-remote-443.sh) makes it run over
a single **443** endpoint and SSH as `root` with a fixed **device-trusted** key, balenaCloud-style.
Full walkthrough + verification: [../../docs/dashboard-terminal.md](../../docs/dashboard-terminal.md).
The `HOST_MODE: secure` setting and the two extra mounts in `docker-compose.yml` are part of this.

## Delta service
The `openbalena-delta` project ([components/delta](../delta/)) runs `open-balena-delta` for
efficient delta image updates. It shares the builder token and the internal TLD.
