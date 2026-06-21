#!/usr/bin/env bash
# Scaffold a ready-to-edit .env (and optionally ansible/group_vars/all.yml) for
# open-balena-extension: copies the example, generates every secret, and — if the
# openBalena core is already running on this host — reads the builder token for you.
#
# Safe to re-run: it never overwrites an existing file unless you pass --force, and
# it only fills secret fields that are still blank.
#
# Usage:
#   ./scripts/bootstrap.sh                          # scaffold .env in repo root
#   ./scripts/bootstrap.sh --ansible                # also scaffold ansible/group_vars/all.yml
#   DNS_TLD=ob.example.com PUBLIC_TLD=ob.example.com ./scripts/bootstrap.sh   # set TLDs too
#   ./scripts/bootstrap.sh --force                  # regenerate from scratch
#
# TLDs/host come from env vars if set (DNS_TLD, PUBLIC_TLD, DASHBOARD_HOST,
# INSTALL_ROOT, SERVICE_USER); otherwise the example defaults are kept for you to edit.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FORCE=0; DO_ANSIBLE=0
for a in "$@"; do case "$a" in
  --force)   FORCE=1 ;;
  --ansible) DO_ANSIBLE=1 ;;
  -h|--help) grep -E '^#( |$)' "$0" | sed 's/^#\{1,2\} \{0,1\}//'; exit 0 ;;
  *) echo "unknown arg: $a (try --help)" >&2; exit 2 ;;
esac; done

have()   { command -v "$1" >/dev/null 2>&1; }
gen()    { openssl rand -hex "${1:-16}"; }
have openssl || { echo "FATAL: openssl is required to generate secrets" >&2; exit 1; }

# set KEY=value in a dotenv file: replace an existing KEY= line, else append.
# (values here are hex/uuid/hostnames — no '=' — so a split/rejoin on '=' is lossless.)
set_kv() {
  local file="$1" key="$2" val="$3"
  if grep -qE "^${key}=" "$file"; then
    awk -v k="$key" -v v="$val" -F= 'BEGIN{OFS="="} $1==k{print k,v; next} {print}' "$file" > "$file.tmp"
    mv "$file.tmp" "$file"
  else
    printf '%s=%s\n' "$key" "$val" >> "$file"
  fi
}
# current value of KEY in a dotenv file ("" if blank/absent)
get_kv() { sed -nE "s/^$2=//p" "$1" | head -1; }

# best-effort: read the API-generated builder token from a running core
read_builder_token() {
  have docker || return 1
  docker exec open-balena-api-1 sh -c \
    'cat /proc/$(pgrep -f node | head -1)/environ | tr "\0" "\n" | sed -nE "s/^TOKEN_AUTH_BUILDER_TOKEN=//p"' \
    2>/dev/null | head -1
}

scaffold_env() {
  local out="$ROOT/.env" ex="$ROOT/.env.example"
  [ -f "$ex" ] || { echo "FATAL: $ex missing" >&2; exit 1; }
  if [ -f "$out" ] && [ "$FORCE" != 1 ]; then
    echo "[bootstrap] .env exists — keeping it (only filling blank secrets). Use --force to reset."
  else
    cp "$ex" "$out"; echo "[bootstrap] created .env from .env.example"
  fi

  # TLDs / host / paths — only overridden when you export them
  for k in DNS_TLD PUBLIC_TLD DASHBOARD_HOST INSTALL_ROOT SERVICE_USER; do
    v="${!k:-}"; [ -n "$v" ] && { set_kv "$out" "$k" "$v"; echo "[bootstrap]   set $k=$v"; }
  done

  # Secrets — generate only if still blank
  local jwt; jwt="$(get_kv "$out" OPEN_BALENA_JWT_SECRET)"
  if [ -z "$jwt" ]; then jwt="$(gen 16)"; set_kv "$out" OPEN_BALENA_JWT_SECRET "$jwt"; echo "[bootstrap]   generated OPEN_BALENA_JWT_SECRET"; fi
  # PGRST secret MUST equal the JWT secret in the dcnext admin stack
  [ -z "$(get_kv "$out" PGRST_JWT_SECRET)" ] && { set_kv "$out" PGRST_JWT_SECRET "$jwt"; echo "[bootstrap]   set PGRST_JWT_SECRET = OPEN_BALENA_JWT_SECRET"; }
  [ -z "$(get_kv "$out" OPEN_BALENA_S3_ACCESS_KEY)" ] && { set_kv "$out" OPEN_BALENA_S3_ACCESS_KEY "$(gen 12)"; echo "[bootstrap]   generated OPEN_BALENA_S3_ACCESS_KEY"; }
  [ -z "$(get_kv "$out" OPEN_BALENA_S3_SECRET_KEY)" ] && { set_kv "$out" OPEN_BALENA_S3_SECRET_KEY "$(gen 20)"; echo "[bootstrap]   generated OPEN_BALENA_S3_SECRET_KEY"; }

  # Builder token — read from a running core if we can; otherwise leave blank (Ansible can auto-read it too)
  if [ -z "$(get_kv "$out" TOKEN_AUTH_BUILDER_TOKEN)" ]; then
    if tok="$(read_builder_token)" && [ -n "$tok" ]; then
      set_kv "$out" TOKEN_AUTH_BUILDER_TOKEN "$tok"; echo "[bootstrap]   read TOKEN_AUTH_BUILDER_TOKEN from running core"
    else
      echo "[bootstrap]   TOKEN_AUTH_BUILDER_TOKEN left blank (core not running here; the builder Ansible role auto-reads it, or set it once core is up)"
    fi
  fi
  chmod 600 "$out"
}

scaffold_ansible() {
  local out="$ROOT/ansible/group_vars/all.yml" ex="$ROOT/ansible/group_vars/all.example.yml"
  [ -f "$ex" ] || { echo "FATAL: $ex missing" >&2; exit 1; }
  if [ -f "$out" ] && [ "$FORCE" != 1 ]; then
    echo "[bootstrap] ansible/group_vars/all.yml exists — leaving it (use --force to reset)"; return
  fi
  cp "$ex" "$out"
  # mirror the .env secrets so both paths use the same values
  local jwt; jwt="$(get_kv "$ROOT/.env" OPEN_BALENA_JWT_SECRET)"
  sed -i.bak \
    -e "s|^open_balena_jwt_secret:.*|open_balena_jwt_secret: \"$jwt\"|" \
    -e "s|^pgrst_jwt_secret:.*|pgrst_jwt_secret: \"$jwt\"|" \
    -e "s|^open_balena_s3_access_key:.*|open_balena_s3_access_key: \"$(get_kv "$ROOT/.env" OPEN_BALENA_S3_ACCESS_KEY)\"|" \
    -e "s|^open_balena_s3_secret_key:.*|open_balena_s3_secret_key: \"$(get_kv "$ROOT/.env" OPEN_BALENA_S3_SECRET_KEY)\"|" \
    "$out" && rm -f "$out.bak"
  [ -n "${DNS_TLD:-}" ]    && sed -i.bak "s|^dns_tld:.*|dns_tld: ${DNS_TLD}|" "$out" && rm -f "$out.bak"
  [ -n "${PUBLIC_TLD:-}" ] && sed -i.bak "s|^public_tld:.*|public_tld: ${PUBLIC_TLD}|" "$out" && rm -f "$out.bak"
  chmod 600 "$out"
  echo "[bootstrap] created ansible/group_vars/all.yml (mirrored secrets from .env)"
  echo "[bootstrap] recommended: cd ansible && ansible-vault encrypt group_vars/all.yml"
}

scaffold_env
[ "$DO_ANSIBLE" = 1 ] && scaffold_ansible

cat <<EOF

[bootstrap] done. Next:
  1. Edit .env — set DNS_TLD / PUBLIC_TLD (and DASHBOARD_HOST if using the admin UI).
     Point a wildcard *.\${PUBLIC_TLD} at this host. See docs/dns-tld-split.md.
  2. ./scripts/doctor.sh        # verify tooling, DNS, and (later) live endpoints
  3. make deploy                # or: cd ansible && ansible-playbook -i inventory.ini site.yml
EOF
