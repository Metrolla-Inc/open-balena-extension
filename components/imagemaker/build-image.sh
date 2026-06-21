#!/bin/bash
# Build a ready-to-flash balenaOS image, preconfigured for an openBalena fleet.
# Internal/public TLDs are parameterized via env (DNS_TLD / PUBLIC_TLD).
#
# Usage: build-image.sh <deviceType> <osVersion> <fleet> <net> <ssid> <wifiKey> <conn> <out.img.gz> <log>
#   conn = lan      -> keep ${DNS_TLD} endpoints (on-LAN devices with internal DNS)
#   conn = internet -> rewrite endpoints to ${PUBLIC_TLD} (remote devices)
#
# Required env (see .env.example): DNS_TLD, PUBLIC_TLD
set -euo pipefail
DT="${1:?device type}"; VER="${2:?os version}"; FLEET="${3:?fleet}"; NET="${4:-ethernet}"
SSID="${5:-}"; KEY="${6:-}"; CONN="${7:-lan}"; OUT="${8:?output path}"; LOG="${9:-/dev/stderr}"

: "${DNS_TLD:?set DNS_TLD (internal openBalena TLD, e.g. example.local)}"
: "${PUBLIC_TLD:?set PUBLIC_TLD (public hostname base, e.g. ob.example.com)}"
ROOT_CA="${OPENBALENA_ROOT_CA:-/usr/local/share/ca-certificates/openbalena-root-ca.crt}"
DB_CONTAINER="${OPENBALENA_DB_CONTAINER:-open-balena-db-1}"
CACHE="${IMAGEMAKER_CACHE:-/var/lib/imagemaker/cache}"
CACHE_BUDGET_GB="${CACHE_BUDGET_GB:-8}"

exec >>"$LOG" 2>&1
export BALENARC_NO_ANALYTICS=1
export NODE_EXTRA_CA_CERTS="$ROOT_CA"

WORK="${IMAGEMAKER_WORK:-/var/lib/imagemaker/work}/$$"
mkdir -p "$CACHE" "$WORK"
LOOP=""
cleanup() {
  mountpoint -q "$WORK/mnt" 2>/dev/null && sudo -n umount "$WORK/mnt" 2>/dev/null || true
  [ -n "$LOOP" ] && sudo -n losetup -d "$LOOP" 2>/dev/null || true
  rm -rf "$WORK" 2>/dev/null || true
  [ -n "${OUT:-}" ] && rm -f "$OUT.partial" 2>/dev/null || true
}
trap cleanup EXIT

BASE="$CACHE/${DT}-${VER}.img"; LOCK="$CACHE/${DT}-${VER}.lock"
enforce_cache() {
  local keep="$1"
  while :; do
    local used; used=$(du -sBG "$CACHE" 2>/dev/null | grep -oE '^[0-9]+')
    [ "${used:-0}" -le "$CACHE_BUDGET_GB" ] && break
    local victim; victim=$(ls -1t "$CACHE"/*.img 2>/dev/null | grep -v "/${keep}$" | tail -1)
    [ -z "$victim" ] && break
    echo "[build] cache ${used}G > ${CACHE_BUDGET_GB}G; evicting $(basename "$victim")"
    rm -f "$victim" "${victim%.img}.lock"
  done
}

echo "[$(date)] START dt=$DT ver=$VER fleet=$FLEET net=$NET conn=$CONN"
enforce_cache "$(basename "$BASE")"
if [ ! -f "$BASE" ]; then
  exec 9>"$LOCK"; flock 9
  if [ ! -f "$BASE" ]; then
    echo "[build] downloading base OS (one-time per device-type/version, ~4GB)..."
    # balenaOS images are hosted by balena; download from balena-cloud.com regardless of your backend.
    BALENARC_BALENA_URL=balena-cloud.com balena os download "$DT" --version "$VER" -o "$BASE.partial"
    mv "$BASE.partial" "$BASE"
  fi
  flock -u 9 || true
fi
enforce_cache "$(basename "$BASE")"

echo "[build] copying base image..."
cp --reflink=auto "$BASE" "$WORK/dev.img"

echo "[build] generating fleet config (against your openBalena backend)..."
gen_config() {
  if [ "$NET" = "wifi" ]; then
    balena config generate --fleet "$FLEET" --version "$VER" --network wifi \
      --wifiSsid "$SSID" --wifiKey "$KEY" --appUpdatePollInterval 10 --output "$WORK/config.json"
  else
    balena config generate --fleet "$FLEET" --version "$VER" --network ethernet \
      --appUpdatePollInterval 10 --output "$WORK/config.json"
  fi
}
cfg_ok=0
for i in $(seq 1 12); do
  if gen_config; then cfg_ok=1; break; fi
  echo "[build] config generate retry $i/12 (transient CLI timeout)..."; sleep 5
done
[ "$cfg_ok" = 1 ] || { echo "[build] ERROR: config generate failed"; exit 1; }

if [ "$CONN" = "internet" ]; then
  echo "[build] rewriting endpoints ${DNS_TLD} -> ${PUBLIC_TLD} for internet connectivity..."
  sed -i "s/${DNS_TLD//./\\.}/${PUBLIC_TLD}/g" "$WORK/config.json"
fi

echo "[build] injecting config.json into boot partition..."
LOOP=$(sudo -n losetup -fP --show "$WORK/dev.img")
sudo -n partprobe "$LOOP" 2>/dev/null || true; sleep 1
BOOT=""
for p in $(seq 1 16); do
  dev="${LOOP}p${p}"; [ -e "$dev" ] || continue
  lbl=$(sudo -n blkid -o value -s LABEL "$dev" 2>/dev/null || true)
  case "$lbl" in resin-boot|flash-boot) BOOT="$dev"; break;; esac
done
if [ -z "$BOOT" ]; then            # fallback: first vfat partition
  for p in $(seq 1 16); do
    dev="${LOOP}p${p}"; [ -e "$dev" ] || continue
    [ "$(sudo -n blkid -o value -s TYPE "$dev" 2>/dev/null)" = "vfat" ] && { BOOT="$dev"; break; }
  done
fi
[ -n "$BOOT" ] || { echo "[build] ERROR no vfat boot partition"; exit 1; }
mkdir -p "$WORK/mnt"
sudo -n mount "$BOOT" "$WORK/mnt"
sudo -n cp "$WORK/config.json" "$WORK/mnt/config.json"
sync; sudo -n umount "$WORK/mnt"
sudo -n losetup -d "$LOOP"; LOOP=""

echo "[build] compressing (pigz)..."
pigz -c "$WORK/dev.img" > "$OUT.partial"
mv "$OUT.partial" "$OUT"
echo "[build] DONE $OUT ($(du -h "$OUT" | cut -f1))"
