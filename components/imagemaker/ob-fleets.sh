#!/bin/sh
# Root helper for the imagemaker: run ONLY the fixed, read-only fleet/device-type query
# against the openBalena DB container.
#
# It exists so the imagemaker service user can be granted passwordless sudo for *this one
# command* instead of `/usr/bin/docker` — blanket `sudo docker` is equivalent to host root
# (e.g. `docker run -v /:/host`). Installed root-owned at /usr/local/bin/ob-fleets and wired
# into /etc/sudoers.d/imagemaker. Takes no SQL from the caller, so there is nothing to inject.
set -eu
C="${1:-open-balena-db-1}"
# only a container name is accepted, and only [a-zA-Z0-9_.-]
case "$C" in
  ''|*[!a-zA-Z0-9_.-]*) echo "ob-fleets: bad container name" >&2; exit 2 ;;
esac
exec docker exec "$C" psql -U docker -d resin -tAF '|' -c \
  'select a.slug, dt.slug from application a join "device type" dt on a."is for-device type"=dt.id order by a.id;'
