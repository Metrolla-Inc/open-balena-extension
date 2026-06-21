# The internal vs public TLD split

This is the single most important concept for operating openBalena behind a public reverse proxy, and the root cause of the most common failure ("device online but VPN never connects").

## The setup
openBalena is deployed under one `DNS_TLD` — say `example.local`. The core API then advertises **every** device-facing endpoint under that TLD:

```
api.example.local          ← device API / heartbeat
cloudlink.example.local     ← VPN endpoint
registry2.example.local     ← image registry
delta.example.local         ← delta updates
```

But the instance is reached from the outside world at a **public** name via a reverse proxy / public DNS — say a wildcard `*.ob.example.com`. So there are two names for the same server:

| | internal (`DNS_TLD`) | public (`PUBLIC_TLD`) |
|---|---|---|
| value | `example.local` | `ob.example.com` |
| resolvable from | the LAN only | anywhere |
| used by | on-LAN devices, internal services | remote devices, engineers' CLIs |

## Why it breaks devices
A device flashed with a **stock** config gets `*.example.local` endpoints. A device on the LAN (with split-horizon DNS resolving `example.local`) is fine. A **remote** device cannot resolve `example.local`, so:
- If only `apiEndpoint` is hand-fixed to public, the device's **heartbeat works** (`api_heartbeat_state: online`) but...
- `vpnEndpoint` / `registryEndpoint` / `deltaEndpoint` still point at `example.local` → **VPN never connects** (`is_connected_to_vpn: false`, `last_connectivity_event: null`) and it **can't pull releases**.

That partial-fix state is the classic trap: the dashboard shows the device "online," but it's unreachable and not updating.

## How this toolkit handles it
**Build two image variants** (the imagemaker does this automatically):
- `__lan` — keep `${DNS_TLD}` endpoints. For devices on the local network with internal DNS.
- `__internet` — rewrite **all** endpoints to `${PUBLIC_TLD}`:
  ```sh
  sed -i "s/${DNS_TLD}/${PUBLIC_TLD}/g" config.json
  ```
  This fixes `api`, `vpn/cloudlink`, `registry2`, and `delta` together. Use this variant for any device not on the LAN.

The **builder** is configured with the internal TLD (it runs inside the docker network and reaches `api.${DNS_TLD}` directly), while it's *reached* externally at `builder.${PUBLIC_TLD}` through haproxy. The **haproxy** routes are keyed on the public subdomains.

## Fixing an already-deployed device
If a device is stuck (online heartbeat, no VPN), its `config.json` endpoints are internal. Options, in order of reliability:
1. **Reflash** with an `__internet` image (cleanest — also restores registry/delta).
2. **Console** (`os_variant: dev` only — prod images have no local login): `sed -i 's/${DNS_TLD}/${PUBLIC_TLD}/g' /mnt/boot/config.json && reboot`.
3. **Edit the boot media** on another computer: mount the `flash-boot`/`resin-boot` FAT partition, edit `config.json`, replace internal→public, reinsert, boot.

> You generally **cannot** SSH in to fix it remotely while the VPN is down: `balena device ssh` and the dashboard terminal both ride the VPN, and host SSH (`:22222`) only trusts keys baked in at provision time. See AGENTS.md → "Device access reality."

## The permanent fix
If you don't actually need the internal name, **deploy with `DNS_TLD` set to the public name** (`PUBLIC_TLD == DNS_TLD`). Then the API advertises public endpoints natively, the imagemaker rewrite becomes a no-op, and there is no `__lan`/`__internet` split to get wrong. The internal-TLD split is worth keeping only if you genuinely run devices on a trusted LAN that should bypass the public proxy.
