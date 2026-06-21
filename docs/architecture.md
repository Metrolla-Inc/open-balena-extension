# Architecture

A self-hosted openBalena deployment using this toolkit has three layers.

```
                          ┌─────────────────────────────────────────────┐
   devices / CLI  ──443──►│  haproxy (custom static cfg)  [homemade]     │
                          │   routes api. registry2. vpn. cloudlink.     │
                          │   delta. s3. ca. builder. by Host header     │
                          └───┬───────────────┬──────────────┬──────────┘
                              │               │              │
        ┌─────────────────────▼──┐   ┌────────▼───────┐  ┌───▼──────────────┐
        │  open-balena CORE       │   │  builder        │  │  admin dashboard │
        │  (upstream, unmodified) │   │  [homemade]      │  │  [third-party]   │
        │  api registry vpn db    │   │  builder +       │  │  ui remote       │
        │  redis s3 ca pki delta  │   │  builder-dind    │  │  postgrest       │
        └─────────────────────────┘   └──────────────────┘  └──────────────────┘
                              ▲
                              │ build flashable images
                   ┌──────────┴───────────┐
                   │  imagemaker [homemade]│  systemd svc, web UI :8090
                   └───────────────────────┘
```

## 1. Stock open-source openBalena (unmodified)
The base stack from [balena-io/open-balena](https://github.com/balena-io/open-balena): **api, registry, vpn, db, redis, s3, cert-manager, balena-ca**, and the haproxy/sidecar scaffolding. Brought up with the project's own `make up`; PKI/cert generation and the `DNS_TLD`-based config come from the project itself. **This toolkit does not modify core** — it adds services alongside it (via a compose override) and replaces only the haproxy config file.

## 2. Homemade extensions (this repo)
| Component | What / why |
|---|---|
| **[Remote builder](../components/builder/)** | openBalena core ships **no** builder (balenaCloud's is closed-source), so `balena push` returns `404 Remote builder`. Adds `builder` (patched community image) + a dedicated plaintext `builder-dind`, wired through a `builder.` haproxy route. |
| **[Imagemaker](../components/imagemaker/)** | A bespoke Node web service + shell scripts that produce ready-to-flash, fleet-preconfigured balenaOS images — including the **internal→public endpoint rewrite** that makes remote devices actually connect. |
| **[Custom haproxy](../components/haproxy/)** | A hand-written static `haproxy.cfg` (mounted via override) replacing openBalena's auto-generated one, so extra routes (`builder.`, dashboard subdomains) and longer build timeouts can be expressed. |

## 3. Third-party community add-ons
| Component | Source |
|---|---|
| **[Admin dashboard](../components/admin/)** | [ob-community/open-balena-ui](https://github.com/ob-community/open-balena-ui) + `open-balena-remote` + `open-balena-postgrest` — a web console and device terminal. |
| **Delta** | `open-balena-delta` — efficient delta image updates. |

## Compose projects on the host
Three independent `docker compose` projects share the `open-balena_default` network:
- `open-balena` — core + the builder/builder-dind override.
- `openbalena-admin` — ui / remote / postgrest.
- `openbalena-delta` — delta.

## The cross-cutting nuance
None of the above is unusual on its own. What shapes the whole deployment is the **internal `DNS_TLD` vs public `PUBLIC_TLD` split** — it's *why* the imagemaker rewrite, the builder's internal-TLD config, and the public haproxy routes all exist. See **[dns-tld-split.md](dns-tld-split.md)**.
