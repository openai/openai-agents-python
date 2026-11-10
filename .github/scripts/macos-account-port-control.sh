#!/bin/bash
set -euo pipefail

if [[ $(uname -s) != Darwin ]]; then
    echo "This account lookup control requires macOS." >&2
    exit 2
fi

probe_dir=$(mktemp -d "${TMPDIR:-/tmp}/sdk-account-port.XXXXXX")
trap 'rm -rf -- "$probe_dir"' EXIT

cat > "$probe_dir/control.c" <<'C'
#include <mach/mach.h>
#include <mach/task_special_ports.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char *argv[]) {
    if (argc != 3) {
        return 2;
    }
    mach_port_t task = mach_task_self();
    if (strcmp(argv[1], "clear") == 0) {
        mach_port_t registered[3] = {MACH_PORT_NULL, MACH_PORT_NULL, MACH_PORT_NULL};
        if (mach_ports_register(task, registered, 3) != KERN_SUCCESS ||
            task_set_special_port(task, TASK_BOOTSTRAP_PORT, MACH_PORT_NULL) != KERN_SUCCESS) {
            fputs("Could not reproduce the worker's task-port clear.\n", stderr);
            return 2;
        }
    } else if (strcmp(argv[1], "keep") != 0) {
        return 2;
    }
    mach_port_t port = MACH_PORT_NULL;
    kern_return_t status = task_get_special_port(task, TASK_BOOTSTRAP_PORT, &port);
    int present = status == KERN_SUCCESS && MACH_PORT_VALID(port);
    fprintf(stderr, "%s %s bootstrap query=%d present=%d\n", argv[1], argv[2], status, present);
    if (present) {
        mach_port_deallocate(task, port);
    }

    if (strcmp(argv[2], "id") == 0) {
        char *const args[] = {"/usr/bin/id", "-F", NULL};
        execv(args[0], args);
    } else if (strcmp(argv[2], "ssh") == 0) {
        char *const args[] = {
            "/usr/bin/ssh", "-G", "-T", "-F", "/dev/null", "credential.example.invalid", NULL,
        };
        execv(args[0], args);
    } else {
        return 2;
    }
    perror("execv");
    return 127;
}
C

/usr/bin/xcrun --sdk macosx clang -std=c17 -Wall -Wextra \
    -o "$probe_dir/control" "$probe_dir/control.c"
for mode in keep clear; do
    for probe in id ssh; do
        status=0
        "$probe_dir/control" "$mode" "$probe" >/dev/null || status=$?
        printf '%s %s exit=%s\n' "$mode" "$probe" "$status"
    done
done
