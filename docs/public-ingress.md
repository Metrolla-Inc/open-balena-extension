# Public ingress (and why Cloudflare Tunnel alone won't work)

If your openBalena host sits on a private network (home/office LAN, behind NAT) but your devices are
**in the wild on the internet**, you need a public ingress. The critical, non-obvious requirement:

> The ingress must carry **both** HTTPS (API, registry) **and raw TCP** (the openVPN/cloudlink VPN).
> Anything that only proxies HTTP/HTTPS will give you "device online but VPN never connects."

## Why HTTP-only fronts break the VPN
openBalena's device VPN is **openVPN over TCP** on port 443, demultiplexed from HTTPS by haproxy at
the L4 layer. An HTTP(S)-only front — **Cloudflare Tunnel (`cloudflared`)**, an nginx `http` proxy,
most "reverse proxy" SaaS — terminates HTTP and cannot carry the raw openVPN stream. Result:
- API heartbeat works (it's HTTPS) → device shows **online**.
- VPN never connects (`is_connected_to_vpn: false`, `last_connectivity_event: null`).
- The dashboard terminal and `balena device ssh` (which ride the VPN) don't work either.

This is a very common trap. The API working fools you into thinking the ingress is fine.

## Working pattern: a tiny relay VM + WireGuard + raw L4 forward
Put a small public VM (a $4–6 cloud droplet is plenty) in front, joined to the host by WireGuard,
and **raw-TCP forward** ports 80/443 to the host's haproxy:

```
device ──443──► relay VM (public IP) ──WireGuard──► host haproxy :443 ──► api / registry / openVPN
```

On the relay (Ubuntu example), this is pure L3/L4 — no HTTP proxy:
```bash
# WireGuard peer to the host already up (wg0). Then:
sysctl -w net.ipv4.ip_forward=1
iptables -t nat -A PREROUTING  -i eth0 -p tcp --dport 443 -j DNAT --to-destination <HOST_WG_IP>:443
iptables -t nat -A PREROUTING  -i eth0 -p tcp --dport 80  -j DNAT --to-destination <HOST_WG_IP>:80
iptables -t nat -A POSTROUTING -d <HOST_WG_IP> -o wg0 -j MASQUERADE
```
Because it's `DNAT` (not an HTTP proxy), the openVPN stream traverses it intact and haproxy demuxes
HTTPS vs VPN as usual.

## Where Cloudflare fits (use it for what it's good at)
- **DNS:** point a wildcard `*.${PUBLIC_TLD}` at the relay's public IP (covers `api.`, `registry2.`,
  `vpn/cloudlink.`, `delta.`, `builder.`). This is the "the DNS is already there" win.
- **R2:** registry blob storage / edge cache — see [registry-edge-cache.md](registry-edge-cache.md).
- **NOT** the Tunnel for the openBalena service ports — it can't carry the VPN. (A Tunnel is fine for
  a separate web dashboard that's pure HTTP.)

## Split-horizon for host-local tools
Tools that run **on the host** (e.g. the imagemaker's `balena config generate`) will resolve
`api.${PUBLIC_TLD}` to the relay and hairpin out-and-back — flaky and slow. Add `/etc/hosts` entries
on the host so those names resolve to the **local** haproxy:
```
127.0.0.1  api.${PUBLIC_TLD} registry2.${PUBLIC_TLD} delta.${PUBLIC_TLD} s3.${PUBLIC_TLD} \
           ca.${PUBLIC_TLD} builder.${PUBLIC_TLD} cloudlink.${PUBLIC_TLD} ${PUBLIC_TLD}
```
Devices and external clients are unaffected (they still use the relay).

## Simplest of all: deploy with `DNS_TLD == PUBLIC_TLD`
If you don't need an internal-only LAN path, set the instance's `DNS_TLD` to your public hostname.
Then the API advertises public endpoints natively (config.json **and** release image paths), there's
no `__lan`/`__internet` image split to get wrong, and field devices "just work." See
[dns-tld-split.md](dns-tld-split.md).
