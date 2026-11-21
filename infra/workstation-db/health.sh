#!/bin/sh
# PostgreSQL SSLRequest. The server replies with one byte, S or N.
reply=$(printf '\000\000\000\010\004\322\026\057' |
    socat -T 5 -t 5 STDIO TCP4:127.0.0.1:5432,connect-timeout=2 |
    head -c 1)
case "$reply" in
    S|N) exit 0 ;;
    *) exit 1 ;;
esac
