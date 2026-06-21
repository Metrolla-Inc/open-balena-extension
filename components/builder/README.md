# Remote builder

Enables `balena push <fleet>` against openBalena. Core openBalena ships **no** builder (balenaCloud's is closed-source), so `balena push` returns `Remote builder responded with HTTP error: 404`. This adds the community [`open-balena-builder`](https://github.com/ob-community/open-balena-builder) (patched) plus a dedicated build daemon.

## Pieces
- **`Dockerfile`** → `open-balena-builder:patched`. Replaces the community image's **expired** bundled balena CLI with a current standalone one and wraps it (sets `HOME` + `NODE_TLS_REJECT_UNAUTHORIZED=0`).
- **`docker-compose.override.yml`** — adds `builder` + `builder-dind` to the core project, mounts the custom haproxy config, and adds the `builder` sidecar alias.
- A **`builder.` haproxy route** with `timeout server 3600s` (see [../haproxy](../haproxy/)).

## Why each workaround exists
| Workaround | Reason |
|---|---|
| Patched CLI image | Bundled CLI hard-expires and silently fails all builds |
| `HOME` + TLS in a **wrapper** | The builder spawns the CLI with a stripped env; container env vars don't reach it |
| Dedicated **plaintext** `builder-dind` | The builder CLI speaks only `tcp://:2375` (no TLS); can't use the TLS-only test dind |
| dind trusts the internal CA | So it can push built images to the self-signed registry |
| `BALENA_TLD = ${DNS_TLD}` (internal) | Builder runs inside the network; token issuer/registry are internal |
| `timeout server 3600s` on the route | Builds exceed haproxy's default 63s and get cut otherwise |

## Install
```bash
# 1. Build the patched image (rebuild when BALENA_CLI_VERSION needs bumping)
docker build -t open-balena-builder:patched components/builder

# 2. Get the API's builder token (generated into the node process env)
docker exec open-balena-api-1 sh -c \
  'cat /proc/$(pgrep -f node | head -1)/environ | tr "\0" "\n" | grep TOKEN_AUTH_BUILDER_TOKEN'

# 3. Put the override in the core project and bring it up
cp components/builder/docker-compose.override.yml <open-balena>/docker-compose.override.yml
cd <open-balena> && docker compose up -d builder-dind builder haproxy-sidecar
docker compose restart haproxy     # after adding the builder. route to haproxy.cfg
```
Or use the Ansible role: [`ansible/roles/builder`](../../ansible/roles/builder).

## Operating rules
- **One `balena push` at a time.** Concurrent builds race on release/service creation and fail with `"application" and "service_name" must be unique`.
- **Rebuild the image when the CLI expires** (~150 days for v22.x): bump `BALENA_CLI_VERSION`, `docker build`, `docker compose up -d builder`.

## Verify
```bash
curl -k -o /dev/null -w '%{http_code}\n' https://builder.${PUBLIC_TLD}/   # 404 from Express = route alive
balena push <fleet> --registry-secrets ./docker-credentials.yml          # streams a remote build
```
