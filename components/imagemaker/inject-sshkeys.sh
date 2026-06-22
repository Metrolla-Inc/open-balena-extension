#!/bin/bash
# Inject all openBalena account SSH public keys into config.json's os.sshKeys.
#
# WHY: openBalena devices (current balenaOS) do NOT dynamically sync account SSH keys
# to the host — they only trust keys baked into config.json `os.sshKeys` at provision
# time. Without this, `balena ssh <device>` is rejected ("Permission denied (publickey)")
# on every device. Baking the account keys in here makes host SSH work for every device.
#
# Env: OPENBALENA_DB_CONTAINER (default open-balena-db-1)
set -euo pipefail
CFG="${1:?config.json path}"
DB="${OPENBALENA_DB_CONTAINER:-open-balena-db-1}"

sudo -n docker exec "$DB" psql -U docker -d resin -tAc 'select "public key" from "user-has-public key";' 2>/dev/null \
 | python3 -c '
import json, sys
cfg = sys.argv[1]
keys = [l.strip() for l in sys.stdin if l.strip()]
c = json.load(open(cfg))
sk = c.setdefault("os", {}).setdefault("sshKeys", [])
for k in keys:
    if k and k not in sk:
        sk.append(k)
json.dump(c, open(cfg, "w"))
print("[build] injected %d account ssh key(s) into os.sshKeys" % len(keys))
' "$CFG"
