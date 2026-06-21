# Custom haproxy

openBalena auto-generates its haproxy config from a template. To express extra routes (the
`builder.` subdomain, longer build timeouts, dashboard routing) we mount a **hand-written
static `haproxy.cfg`** over it via the compose override:

```yaml
services:
  haproxy:
    volumes:
      - ${INSTALL_ROOT}/haproxy/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro
```

## What this file changes vs stock
- Adds an `acl host-builder-backend hdr_beg(host) -i "builder."` + `use_backend` in **both** the
  `http` (:80) and `https` (:444) frontends.
- Adds `backend builder-backend` with **`timeout server 3600s`** — builds stream for minutes and
  would otherwise be cut by the default `timeout server 63s`.

haproxy is the **single ingress** for the whole instance. Treat edits with care:

## Validate before reloading (no downtime)
The config is mounted read-only, so the running container sees your edit immediately but keeps
its old config loaded. Validate the new file *inside* the running container (it has the certs +
runtime env) **before** restarting:

```bash
docker exec open-balena-haproxy-1 sh -c '
  for p in $(ls /proc | grep -E "^[0-9]+$"); do
    grep -qaE "CERT_CHAIN_PATH=" /proc/$p/environ 2>/dev/null && {
      eval "$(tr "\0" "\n" </proc/$p/environ | grep -E "^(LOGLEVEL|CERT_CHAIN_PATH|BALENA_DEVICE_UUID)=" | sed -E "s/^/export /")"; break; }; done
  haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg'
# exit 0 = valid (rule-ordering "Warnings" are pre-existing and fine)
docker compose restart haproxy
```
Always keep a timestamped backup (`cp haproxy.cfg haproxy.cfg.bak-$(date +%s)`); on any failure,
restore it and `docker compose restart haproxy`.

> This file tracks a specific openBalena version's routing. If you upgrade core and the generated
> config changes, re-derive from the new template and re-apply the two `builder` additions.
