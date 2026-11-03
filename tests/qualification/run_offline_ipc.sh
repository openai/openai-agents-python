#!/bin/bash
# Called inside private CI mount and network namespaces. Only loopback has a route.
set -euo pipefail
test "$(id -u)" = 0
test "$(readlink /proc/self/ns/net)" != "$1"
test "$(readlink /proc/self/ns/mnt)" != "$2"
uid=$3 gid=$4 stage=$5 python=$6 fixture=$7
ip link set lo up
resolver=$(readlink -f /etc/resolv.conf)
mount -t tmpfs -o nosuid,nodev tmpfs /run
# Ubuntu can symlink resolv.conf into /run, which is private in this namespace.
if [[ "$resolver" == /run/* ]]; then mkdir -p "$(dirname "$resolver")"; touch "$resolver"; fi
printf 'nameserver 127.0.0.2\noptions attempts:1 timeout:1\n' > "$stage/resolv.conf"
printf 'passwd: files\ngroup: files\nhosts: files dns\n' > "$stage/nsswitch.conf"
mount --bind "$stage/resolv.conf" /etc/resolv.conf
mount --bind "$stage/nsswitch.conf" /etc/nsswitch.conf
dnsmasq --keep-in-foreground --conf-file=/dev/null --no-resolv --no-hosts --no-poll \
    --bind-interfaces --listen-address=127.0.0.2 --address=/#/127.0.0.1 \
    --log-queries --log-facility=- --pid-file= > "$stage/dns.log" 2>&1 &
dns=$!
trap 'kill "$dns" 2>/dev/null || true; wait "$dns" 2>/dev/null || true' EXIT
for _ in {1..50}; do
    if getent ahostsv4 startup.ipc.test >/dev/null; then break; fi
    kill -0 "$dns"
    sleep 0.1
done
cd "$stage"
# Run the installed wheel outside the checkout. The only added import root contains test fixtures.
setpriv --reuid="$uid" --regid="$gid" --init-groups env -i \
    HOME="$stage/home" PATH=/usr/bin:/bin LANG=C.UTF-8 \
    "$python" -I -B -c 'import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module("tests.qualification.offline_ipc",run_name="__main__")' \
    "$stage" "$fixture" "$stage/dns.log"
