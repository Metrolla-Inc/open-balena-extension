#!/bin/bash
# Batch-build flashable images for a set of fleets (both lan + internet variants),
# so the imagemaker UI can serve them instantly. Run on a schedule or after fleet changes.
#
# Required env: DNS_TLD, PUBLIC_TLD  (passed through to build-image.sh)
set -euo pipefail
export BALENARC_NO_ANALYTICS=1
export NODE_EXTRA_CA_CERTS="${OPENBALENA_ROOT_CA:-/usr/local/share/ca-certificates/openbalena-root-ca.crt}"
export PATH=/usr/local/bin:/usr/bin:/bin
: "${DNS_TLD:?}"; : "${PUBLIC_TLD:?}"

DIR="$(cd "$(dirname "$0")" && pwd)"
DIST="${IMAGEMAKER_DIST:-/var/lib/imagemaker/dist}"
DB_CONTAINER="${OPENBALENA_DB_CONTAINER:-open-balena-db-1}"
export CACHE_BUDGET_GB="${CACHE_BUDGET_GB:-20}"
FORCE="${FORCE:-0}"
mkdir -p "$DIST"

# fleet|version  — leave version blank to resolve the latest balenaOS for the device type.
# Edit this list for your fleets.
MANIFEST=(
  "my-fleet|"
  # "another-fleet|7.0.5"
)

# fleet -> device type, read from the openBalena DB (same query the UI uses)
declare -A DTMAP
while IFS='|' read -r slug dtype; do
  [ -n "$slug" ] && DTMAP["$slug"]="$dtype"
done < <(sudo -n docker exec "$DB_CONTAINER" psql -U docker -d resin -tAF '|' -c \
  'select a.slug, dt.slug from application a join "device type" dt on a."is for-device type"=dt.id order by a.id;')

resolve_ver(){ BALENARC_BALENA_URL=balena-cloud.com balena os versions "$1" 2>/dev/null | sed 's/^v//' | grep -E '^[0-9]' | head -1 || true; }
san(){ echo "$1" | sed 's/[^a-zA-Z0-9._-]/-/g'; }

rc=0
for entry in "${MANIFEST[@]}"; do
  fleet="${entry%%|*}"; ver="${entry#*|}"
  dt="${DTMAP[$fleet]:-}"
  [ -z "$dt" ]  && { echo "[prebuild] SKIP $fleet (no device type in DB)"; rc=1; continue; }
  [ -z "$ver" ] && ver="$(resolve_ver "$dt")"
  [ -z "$ver" ] && { echo "[prebuild] SKIP $fleet/$dt (no version)"; rc=1; continue; }
  fsan="$(san "$fleet")"
  for conn in lan internet; do
    out="$DIST/${fsan}__${dt}-${ver}__${conn}.img.gz"
    log="$DIST/${fsan}__${dt}-${ver}__${conn}.log"
    if [ -f "$out" ] && [ "$FORCE" != "1" ]; then echo "[prebuild] have ${out##*/}"; continue; fi
    echo "[prebuild] building $fleet | $dt | $ver | $conn ..."
    if "$DIR/build-image.sh" "$dt" "$ver" "$fleet" ethernet "" "" "$conn" "$out" "$log"; then
      echo "[prebuild] OK ${out##*/} ($(du -h "$out" | cut -f1))"
    else
      echo "[prebuild] FAIL $fleet $dt $ver $conn:"; tail -4 "$log" 2>/dev/null || true; rc=1
    fi
  done
done
echo "[prebuild] dist usage: $(du -sh "$DIST" 2>/dev/null | cut -f1)"
exit $rc
