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

## Security notes (inherent to the community builder — mitigate at the network layer)
These follow from the upstream builder's design; don't "fix" them blindly, as the build/push path
depends on them. Mitigate by keeping the openBalena network trusted (don't co-locate untrusted
workloads on `open-balena_default`):
- **`builder-dind` exposes a plaintext, unauthenticated Docker API on `tcp://0.0.0.0:2375` and is
  `privileged`.** Anything that can reach that port gets root on the host. It is **not** published
  to host ports (only on the compose network) — keep it that way; never map `2375` to the host, and
  treat every container on `open-balena_default` as trusted. The community builder CLI speaks only
  plaintext, so TLS isn't an option here.
- **`NODE_TLS_REJECT_UNAUTHORIZED=0`** disables TLS verification for the self-signed backend. It's
  scoped to the builder + its CLI wrapper. The cleaner alternative (trust the CA via
  `NODE_EXTRA_CA_CERTS`, as the imagemaker does) needs the CA mounted into the builder image and
  build-testing before flipping — left as a follow-up so a wrong move doesn't break every build.

## Operating rules
- **One `balena push` at a time.** Concurrent builds race on release/service creation and fail with `"application" and "service_name" must be unique`.
- **Rebuild the image when the CLI expires** (~150 days for v22.x): bump `BALENA_CLI_VERSION`, `docker build`, `docker compose up -d builder`.

## Verify
```bash
curl -k -o /dev/null -w '%{http_code}\n' https://builder.${PUBLIC_TLD}/   # 404 from Express = route alive
balena push <fleet> --registry-secrets ./docker-credentials.yml          # streams a remote build
```

## Registry → S3 internal TLS (push hangs in `running`)
If a build reaches **"Creating release / Pushing images"** but the release stays `running` forever,
check the registry logs for:
```
s3aws: ... Put "https://s3.<TLD>/...": tls: failed to verify certificate: x509: certificate signed by unknown authority
```
The registry's Go S3 client doesn't trust the self-signed internal CA (common after a PKI regen).
Fix by adding to the `registry` service env:
```yaml
services:
  registry:
    environment:
      REGISTRY_STORAGE_S3_SKIPVERIFY: "true"   # internal registry→s3 hop only
```
(Or install the CA into the registry's system trust store.) For offloading blobs to a fast edge
instead of local S3, see [../../docs/registry-edge-cache.md](../../docs/registry-edge-cache.md).
