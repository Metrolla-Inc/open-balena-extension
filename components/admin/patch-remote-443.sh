#!/usr/bin/env bash
# Patch dcnext/open-balena-remote so the dashboard web terminal works over a single 443
# endpoint (behind a reverse proxy / Cloudflare tunnel) instead of per-session high ports,
# and authenticates to devices with a FIXED, device-trusted key instead of an ephemeral one.
#
# Why: the stock service redirects the browser to https://<host>:<proxyPort> where proxyPort is
# 10000-10009. Those ports are blocked by most networks (only 443 is universal) and Cloudflare
# can't proxy them, so the terminal shows a blank/"can't connect" window. It also generates an
# ephemeral SSH key per session, which balenaOS host SSH (port 22222) won't trust — it only honours
# keys baked into config.json `os.sshKeys` at provision (see ../imagemaker/inject-sshkeys.sh).
#
# This patch makes a single terminal session run entirely over the same host:443 the browser
# arrived on (so a Cloudflare tunnel / haproxy 443 front carries it), and SSHes as `root` with the
# key at /certs/device_key (mount a key whose PUBLIC half is in the devices' os.sshKeys).
#
# Usage: patch-remote-443.sh /path/to/open-balena-remote.js   (extract it once with:
#        docker cp openbalena-admin-remote-1:/usr/src/app/open-balena-remote.js ./)
# Then mount the patched file + the key into the container (see docker-compose.yml) and restart it.
# Idempotent.
set -euo pipefail
F="${1:?path to open-balena-remote.js}"
python3 - "$F" <<'PY'
import sys
f=sys.argv[1]; s=open(f).read()

# 1. Single-port: drop the explicit :proxyPort for the base session so the redirect stays on
#    whatever port the request arrived on (443 via the reverse proxy / tunnel).
old1='var redirect = req.protocol + "://" + req.headers.host.split(":")[0] + ":" + sessionData.proxyPort;'
new1='var redirect = req.protocol + "://" + req.headers.host.split(":")[0] + (sessionData.proxyPort === PORT ? "" : ":" + sessionData.proxyPort);'
if old1 in s:
    s=s.replace(old1,new1)
    # 2. balenaOS host SSH user is root (not the balena account name).
    s=s.replace(new1, new1+'\n          if (req.query.service === "ssh") req.query.username = "root";')
elif 'sessionData.proxyPort === PORT' not in s:
    raise SystemExit("redirect line not found and not already patched")

# 3. Always use the fixed device-trusted key instead of the ephemeral per-session key.
old3='''            // save private key to session directory if provided
            if (req.query.privateKey) {
              fs.writeFileSync(`${sessionData.sessionDir}/privateKey`, req.query.privateKey);
              fs.chmodSync(`${sessionData.sessionDir}/privateKey`, "0600");
            }'''
new3='''            // use the fixed device-trusted key (baked into os.sshKeys); balenaOS host won't trust ephemeral keys
            fs.copyFileSync("/certs/device_key", `${sessionData.sessionDir}/privateKey`);
            fs.chmodSync(`${sessionData.sessionDir}/privateKey`, "0600");'''
if old3 in s:
    s=s.replace(old3,new3)
elif '/certs/device_key' not in s:
    raise SystemExit("privateKey block not found and not already patched")

# 4. ALWAYS reuse the base port (10000) so every terminal rides 443. The stock allocator marks a
#    port "used" from the browser's own session cookies, so reopening a terminal (before the prior
#    session expires) pushes the new one to a high port (10001-10009), which Cloudflare/most networks
#    block -> blank/white terminal. Pinning to the base port keeps it on 443. We do NOT kill the prior
#    session: each session bakes its own VPN tunnel port + ttyd args into its URL, and for SSH the
#    proxy route is always ttyd regardless of which session the (shared) base-port cookie maps to —
#    so multiple concurrent SSH terminals coexist over the single 443 endpoint.
old4='''  // always use base port if available, otherwise remove it from the list
  if (!cookiesArr.find(x => x.port == PORT)) {
    return PORT;
  } else {
    cookiesArr = cookiesArr.filter(x => x.port != PORT);
  }'''
new4='''  // multi-session over 443: every session rides the base port (10000) so it is always reachable.
  // Do NOT kill prior sessions - each has its own VPN tunnel + ttyd args baked into its URL, so
  // concurrent SSH terminals coexist over the single 443 endpoint.
  return PORT;'''
if old4 in s:
    s=s.replace(old4,new4)
elif 'multi-session over 443' not in s:
    raise SystemExit("port-allocation block not found and not already patched")

open(f,"w").write(s)
print("patched:", f)
PY
