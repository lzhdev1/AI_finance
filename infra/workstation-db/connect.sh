#!/bin/sh
# Long-lived API connections may exhaust sshd's default ten sessions per master.
# Hold the slot in this shell while ssh runs. Overflow clients use their own transport.
for slot in 1 2 3 4 5 6 7 8; do
    exec 9>"/run/workstation-db/slot-$slot"
    if flock -n 9; then
        ssh -F /etc/ssh/workstation_config -T -- "$SSH_TARGET"
        exit $?
    fi
done
exec ssh -F /etc/ssh/workstation_config -o ControlPath=none -T -- "$SSH_TARGET"
