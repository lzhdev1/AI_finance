#!/bin/sh
set -eu
: "${SSH_TARGET:?Set SSH_TARGET to user@server}"
umask 077
mkdir -p /run/workstation-db
cp /config/id_ed25519 /run/workstation-db/id_ed25519
chmod 600 /run/workstation-db/id_ed25519
test -s /config/known_hosts

# Keep one SSH transport for all clients, including the health probe.
(
    while :; do
        ssh -F /etc/ssh/workstation_config -M -N -- "$SSH_TARGET" || true
        sleep 2
    done
) &
exec socat TCP4-LISTEN:5432,bind=0.0.0.0,reuseaddr,fork EXEC:/usr/local/bin/connect.sh,nofork
