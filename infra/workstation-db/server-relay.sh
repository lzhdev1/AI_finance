#!/bin/sh
# Install as a root-owned forced command for this workstation's restricted SSH key.
set -eu
address=$(/usr/bin/docker inspect --type container \
    --format '{{with index .NetworkSettings.Networks "shared-db"}}{{.IPAddress}}{{end}}' \
    shared-postgres)
case "$address" in
    ''|*[!0-9.]*) exit 1 ;;
esac
exec /usr/bin/socat STDIO "TCP4:${address}:5432,connect-timeout=6"
