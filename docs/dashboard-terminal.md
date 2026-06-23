# Dashboard web terminal over 443 (balenaCloud-like)

The dcnext `open-balena-remote` service powers the dashboard's in-browser device terminal. Out of
the box it has two properties that break it behind a public reverse proxy / Cloudflare:

1. **Per-session high ports.** It redirects the browser to `https://<host>:<proxyPort>` where
   `proxyPort` is **10000–10009**. Most networks (corporate, guest Wi‑Fi, mobile) only allow
   outbound **443**, and Cloudflare's proxy can't carry those ports either. Symptom: the terminal
   window is blank / "this site can't be reached" / "took too long to respond", even though the
   dashboard itself loads fine. The VPN tunnel actually opens server-side — only the browser→port
   hop fails.
2. **Ephemeral SSH key per session.** It generates a new keypair per session and SSHes to the
   device with it. balenaOS host SSH (`:22222`) only trusts keys **baked into `config.json`
   `os.sshKeys` at provision** (see [../components/imagemaker/inject-sshkeys.sh](../components/imagemaker/inject-sshkeys.sh));
   it won't trust a runtime ephemeral key. Symptom: the terminal loads but the shell shows
   `Permission denied (publickey)`, and the dashboard's "Enable SSH" toggle errors.

This doc describes making it behave like balenaCloud — **one 443 endpoint, no port worries, a real
shell** — using [`patch-remote-443.sh`](../components/admin/patch-remote-443.sh).

## How it works after the patch
The service's base port (10000) already handles both session creation *and* proxying for the first
session. The patch:

- **Drops the explicit `:proxyPort`** from the redirect for the base session, so it stays on the
  same host:port the request arrived on. Front the service with a single **443** endpoint (a
  Cloudflare tunnel `ingress` rule, or an haproxy `bind :443` with SNI) → `localhost:10000`, and the
  whole flow — initial request, ttyd page, and the terminal **websocket** — rides 443.
- **Forces the SSH user to `root`** (balenaOS host user; the dashboard sends the account name).
- **Uses a fixed device-trusted key** at `/certs/device_key` instead of the ephemeral one. Mount a
  key whose **public half is in the devices' `os.sshKeys`** (e.g. the key the imagemaker bakes).

- **Pins every session to the base port (10000)** so it always rides 443. The stock allocator
  derives "used" ports from the browser's own session cookies, so reopening a terminal before the
  prior session expired pushed the new one to a high port (10001–10009) — blocked by Cloudflare/most
  networks → the terminal goes **blank/white again** even though the server is healthy. Pinning to
  10000 keeps it on 443. The patch does **not** kill the prior session, so multiple terminals coexist
  (see below).

> **Multiple concurrent terminals over 443.** Each session bakes its own VPN tunnel port + ttyd args
> into its URL, and for SSH the proxy route is always ttyd regardless of which session the shared
> base-port cookie maps to — so concurrent SSH terminals all run on port 10000/443 without killing
> each other. (Sessions are reaped on their 6 h expiry; for VNC, which needs per-session server-port
> routing, stick to one at a time.) If a terminal ever goes blank, a
> `docker restart openbalena-admin-remote-1` clears any stuck session state.
>
> **After editing the patched JS, you must restart the container** — Node reads
> `open-balena-remote.js` only at startup, so an edit (or re-running this script) doesn't take effect
> until `docker restart openbalena-admin-remote-1`.

## Setup
1. **Cert.** The service serves HTTPS itself from `/certs/certificate.crt` + `/certs/private_key.key`.
   If you terminate TLS at Cloudflare/haproxy (recommended), the browser gets the trusted edge cert
   and the origin cert can be your internal `*.${PUBLIC_TLD}` wildcard (the proxy ignores origin
   trust). Either way make sure the served cert matches the hostname the browser uses.
2. **`HOST_MODE: secure`.** Required so the `SameSite=None` session cookie gets the `Secure` flag;
   without it browsers drop the cookie over HTTPS and the ttyd request has no session.
3. **Device key.** Drop a passphrase-less private key at `${INSTALL_ROOT}/admin/certs/device_key`
   (`chmod 600`) whose public key is baked into your devices. Prefer a **dedicated** terminal key
   you register (`balena key add`) *before* building images, over reusing an existing one.
4. **Patch + mount.** Run `patch-remote-443.sh` against an extracted `open-balena-remote.js`, mount
   the patched file at `/usr/src/app/open-balena-remote.js`, and **restart** the container (node
   reads the file at startup; a no-op `up -d` won't reload it).
5. **Point the dashboard at the 443 host.** Set `REACT_APP_OPEN_BALENA_REMOTE_URL` on
   `open-balena-ui` to the 443 hostname (no port), e.g. `https://remote.${PUBLIC_TLD}`.

See [components/admin/docker-compose.yml](../components/admin/docker-compose.yml) for the mounts/env.

## Verify
```bash
# page over 443 (expect 200), then the same session's ttyd page (200) and the ws upgrade (101)
curl -sk -c cj "https://remote.<PUBLIC_TLD>/?service=ssh&uuid=<uuid>&jwt=<jwt>&container=<svc>&username=admin" -o /dev/null
curl -sk -b cj "https://remote.<PUBLIC_TLD>/ttyd/" -o /dev/null -w '%{http_code}\n'
curl -sk -b cj --http1.1 -o /dev/null -w '%{http_code}\n' \
  -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Sec-WebSocket-Protocol: tty" \
  "https://remote.<PUBLIC_TLD>/ttyd/ws"
```
The redirect logged by the service (`Redirecting to path: ...`) should have **no port** and the
ttyd args should end in `&arg=root&arg=/tmp/...`.

## Security notes
- `/certs/device_key` grants host-root SSH to every device that trusts its public key — treat the
  remote container as privileged (it already holds admin creds and tunnels to devices). Use a
  dedicated key so it can be rotated/revoked independently.
- The base 443 endpoint still requires a valid `jwt`/`apiKey` + signed session cookie to do anything.
- If you fronted it with the relay instead of Cloudflare, don't leave 10000–10009 open publicly once
  443 works — close them.
