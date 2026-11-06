#!/bin/bash
# This process already has private net, mount, PID, UTS and IPC namespaces.
set -euo pipefail
root=$1 helper=$2 fixture=$3 uid=$4 gid=$5 port=$6
image="$root/image"
test "$uid" != 0
ip link set lo up
test "$(ip -o link show | cut -d: -f2 | tr -d ' ')" = lo
test -z "$(ip route list default)$(ip -6 route list default)"
mount -t tmpfs -o nosuid,nodev tmpfs "$image"
mkdir -p "$image"/{usr,proc,dev,run,tmp,job,outside,etc,home/job,old-root}
chmod 1777 "$image/tmp"
chown "$uid:$gid" "$image/home/job"
# Every exposed device and system file is explicit. Host /run, sockets and homes stay outside.
ro_file() {
    mkdir -p "$(dirname "$image$2")"
    touch "$image$2"
    mount --bind "$1" "$image$2"
    mount -o remount,bind,ro,nosuid,nodev "$image$2"
}
mount --bind /usr "$image/usr"
mount -o remount,bind,ro,nosuid,nodev "$image/usr"
for name in bin sbin lib lib64; do
    if [[ -L "/$name" ]]; then cp -P "/$name" "$image/$name"; fi
done
for name in passwd group hosts nsswitch.conf resolv.conf ld.so.cache; do
    ro_file "/etc/$name" "/etc/$name"
done
ro_file "$fixture" /executor
ro_file "$helper" "$helper"
mount -t proc -o nosuid,nodev,noexec proc "$image/proc"
mount -t tmpfs -o nosuid tmpfs "$image/dev"
for device in null zero random urandom full tty; do
    touch "$image/dev/$device"
    mount --bind "/dev/$device" "$image/dev/$device"
done
ln -s /proc/self/fd "$image/dev/fd"
mkdir "$image/dev/pts"
mount -t devpts -o newinstance,ptmxmode=0666,mode=0620 devpts "$image/dev/pts"
ln -s pts/ptmx "$image/dev/ptmx"
for pair in workspace:job outside:outside; do
    dest="$image/${pair#*:}"
    mount --bind "$root/${pair%:*}" "$dest"
    mount -o remount,bind,nosuid,nodev "$dest"
done
cd "$image"
pivot_root . old-root
cd /job
umount -l /old-root
rmdir /old-root
exec /usr/bin/setpriv --reuid="$uid" --regid="$gid" --clear-groups --no-new-privs \
    --bounding-set=-all --inh-caps=-all --ambient-caps=-all /usr/bin/env -i \
    PATH=/usr/bin:/bin HOME=/home/job CODEX_HOME=/home/job/.codex LANG=C.UTF-8 \
    HTTP_PROXY="http://127.0.0.1:$port" HTTPS_PROXY="http://127.0.0.1:$port" \
    /bin/sh -ec '
        test "$(id -u)" != 0
        for check in /job/.git/.positive /tmp/.positive /outside/.positive; do
            : > "$check"; rm "$check"
        done
        exec /executor stdio "$1"
    ' sh "$helper"
