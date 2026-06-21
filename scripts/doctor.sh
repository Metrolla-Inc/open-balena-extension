#!/usr/bin/env bash
# Preflight + health check for an open-balena-extension deployment.
# Reads .env and checks the things that actually break installs, in order:
#   tooling -> config -> DNS wildcard -> live endpoints.
# Run it before deploying (catches DNS/tooling gaps) and after (probes the endpoints).
#
# Usage: ./scripts/doctor.sh            # uses ./.env
#        PUBLIC_TLD=ob.example.com ./scripts/doctor.sh   # override without .env
set -uo pipefail   # deliberately not -e: run every check, then tally

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0; WARN=0; FAIL=0
if [ -t 1 ]; then G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; Z=$'\e[0m'; else G=; Y=; R=; Z=; fi
pass() { printf '  %sPASS%s %s\n' "$G" "$Z" "$1"; PASS=$((PASS+1)); }
warn() { printf '  %sWARN%s %s\n' "$Y" "$Z" "$1"; WARN=$((WARN+1)); }
fail() { printf '  %sFAIL%s %s\n' "$R" "$Z" "$1"; FAIL=$((FAIL+1)); }
have() { command -v "$1" >/dev/null 2>&1; }

# load .env (without exporting junk) if present
if [ -f "$ROOT/.env" ]; then
  set -a; . "$ROOT/.env"; set +a
fi
PUBLIC_TLD="${PUBLIC_TLD:-}"; DNS_TLD="${DNS_TLD:-}"; DASHBOARD_HOST="${DASHBOARD_HOST:-}"

resolve() { # print an IP for $1, or nothing
  if have getent;   then getent hosts "$1" | awk '{print $1; exit}'; return; fi
  if have dig;      then dig +short "$1" | grep -E '^[0-9]' | head -1; return; fi
  if have host;     then host "$1" 2>/dev/null | awk '/has address/{print $NF; exit}'; return; fi
  if have python3;  then python3 -c "import socket,sys; print(socket.gethostbyname(sys.argv[1]))" "$1" 2>/dev/null; return; fi
}

echo "== tooling =="
for t in docker git make pigz openssl curl; do
  if have "$t"; then pass "$t present"; else fail "$t missing"; fi
done
if docker compose version >/dev/null 2>&1; then pass "docker compose v2 present"; else fail "docker compose v2 missing"; fi
have balena && pass "balena CLI present" || warn "balena CLI not on PATH (needed by imagemaker host + 'balena push')"
have ansible-playbook && pass "ansible present" || warn "ansible not present (only needed for the Ansible deploy path)"

echo "== config =="
if [ -f "$ROOT/.env" ]; then pass ".env present"; else warn ".env missing — run ./scripts/bootstrap.sh"; fi
if [ -z "$PUBLIC_TLD" ] || [ "$PUBLIC_TLD" = "ob.example.com" ]; then
  warn "PUBLIC_TLD not set (still the example) — set it in .env before deploying"
else
  pass "PUBLIC_TLD=$PUBLIC_TLD"
fi
[ -n "$DNS_TLD" ] && [ "$DNS_TLD" != "$PUBLIC_TLD" ] && warn "DNS_TLD ($DNS_TLD) differs from PUBLIC_TLD — internal/public split is active (docs/dns-tld-split.md)"
CA=/usr/local/share/ca-certificates/openbalena-root-ca.crt
[ -f "$CA" ] && pass "root CA installed ($CA)" || warn "root CA not at $CA (imagemaker/builder need it to trust the backend)"

echo "== DNS =="
if [ -n "$PUBLIC_TLD" ] && [ "$PUBLIC_TLD" != "ob.example.com" ]; then
  api_ip="$(resolve "api.$PUBLIC_TLD")"
  if [ -n "$api_ip" ]; then pass "api.$PUBLIC_TLD -> $api_ip"; else fail "api.$PUBLIC_TLD does not resolve"; fi
  # wildcard probe: a random label must resolve to the SAME ip as api.
  rand="wildcard-probe-$(openssl rand -hex 3 2>/dev/null || echo x123).$PUBLIC_TLD"
  w_ip="$(resolve "$rand")"
  if [ -z "$w_ip" ]; then
    fail "wildcard *.$PUBLIC_TLD does NOT resolve (random label failed) — devices' registry2./vpn./builder. endpoints will fail. This is the #1 cause of broken installs."
  elif [ -n "$api_ip" ] && [ "$w_ip" != "$api_ip" ]; then
    warn "wildcard resolves ($w_ip) but differs from api. ($api_ip) — check your DNS"
  else
    pass "wildcard *.$PUBLIC_TLD resolves -> $w_ip"
  fi
else
  warn "skipping DNS checks (set PUBLIC_TLD first)"
fi

echo "== live endpoints (skip cleanly if core isn't up yet) =="
if [ -n "$PUBLIC_TLD" ] && [ "$PUBLIC_TLD" != "ob.example.com" ] && have curl; then
  ping="$(curl -fsk --max-time 8 "https://api.$PUBLIC_TLD/ping" 2>/dev/null || true)"
  if [ "$ping" = "OK" ]; then pass "api.$PUBLIC_TLD/ping = OK"
  elif [ -n "$ping" ]; then warn "api.$PUBLIC_TLD/ping returned: $ping"
  else warn "api.$PUBLIC_TLD/ping unreachable (core not up / DNS / ports 80,443?)"; fi

  code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 "https://builder.$PUBLIC_TLD/" 2>/dev/null || echo 000)"
  case "$code" in
    404) pass "builder.$PUBLIC_TLD route alive (404 from the builder = expected)";;
    000) warn "builder.$PUBLIC_TLD unreachable (builder/haproxy route not up yet)";;
    *)   warn "builder.$PUBLIC_TLD returned HTTP $code (expected 404 when idle)";;
  esac

  if [ -n "$DASHBOARD_HOST" ]; then
    code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 "https://$DASHBOARD_HOST/" 2>/dev/null || echo 000)"
    [ "$code" = 000 ] && warn "dashboard $DASHBOARD_HOST unreachable" || pass "dashboard $DASHBOARD_HOST responds (HTTP $code)"
  fi
else
  warn "skipping endpoint checks (set PUBLIC_TLD and install curl)"
fi

echo
printf 'summary: %s%d pass%s, %s%d warn%s, %s%d fail%s\n' "$G" "$PASS" "$Z" "$Y" "$WARN" "$Z" "$R" "$FAIL" "$Z"
[ "$FAIL" -gt 0 ] && exit 1 || exit 0
