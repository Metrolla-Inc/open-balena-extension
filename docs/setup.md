# Setup

Two paths: **Ansible** (recommended) or **manual**. Either way, decide your TLDs first
([dns-tld-split.md](dns-tld-split.md)) and point a wildcard `*.${PUBLIC_TLD}` at the host.

## Prerequisites
- Ubuntu 22.04/24.04 host with Docker + compose v2, `git`, `make`, `pigz`.
- DNS: `*.${PUBLIC_TLD}` → host IP (covers `api.`, `vpn.`, `cloudlink.`, `registry2.`, `delta.`, `builder.`).
- Ports 80/443 reachable.

## Ansible (recommended)
See [../ansible/README.md](../ansible/README.md):
```bash
cd ansible
cp inventory.example.ini inventory.ini
cp group_vars/all.example.yml group_vars/all.yml   # set TLDs + secrets; vault-encrypt
ansible-playbook -i inventory.ini site.yml
```

## Manual
1. **Core** — clone + bring up upstream open-balena:
   ```bash
   git clone -b v4.1.752 https://github.com/balena-io/open-balena.git /opt/openbalena/open-balena
   cd /opt/openbalena/open-balena
   printf 'DNS_TLD=%s\nSUPERUSER_EMAIL=%s\nPRODUCTION_MODE=true\n' "$DNS_TLD" "$EMAIL" > .env
   make up && make showpass
   ```
2. **Custom haproxy** — [components/haproxy](../components/haproxy/): drop in `haproxy.cfg`, mount via override, validate, reload.
3. **Builder** — [components/builder](../components/builder/): `docker build -t open-balena-builder:patched`, add the override, start `builder` + `builder-dind`.
4. **Imagemaker** — [components/imagemaker](../components/imagemaker/): copy files, edit the systemd unit, `systemctl enable --now imagemaker`. Log its user's balena CLI into the backend once.
5. **Admin / Delta** — [components/admin](../components/admin/), [components/delta](../components/delta/): bring up each project.

## Verify
```bash
curl -k https://api.${PUBLIC_TLD}/ping                                   # OK
curl -k -o /dev/null -w '%{http_code}\n' https://builder.${PUBLIC_TLD}/  # 404 = builder route alive
balena login --credentials   # against ${PUBLIC_TLD}
balena push <fleet>          # streams a remote build → final release
```

## Backups (do this before you rely on it)
The PKI + DB are the irreplaceable state — without them, restored instances can't re-auth devices.
Back up: `.env`, the compose overrides, `haproxy.cfg`, and the volumes `db-data`, `pki-data`,
`certs-data`, `s3-data`. A `pg_dump` of `resin` + a tar of those volumes to off-box storage is enough.
