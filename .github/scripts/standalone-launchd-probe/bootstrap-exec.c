#include <mach/mach.h>
#include <mach/task_special_ports.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static void report_port(const char *phase) {
    mach_port_t port = MACH_PORT_NULL;
    kern_return_t rc = task_get_special_port(mach_task_self(), TASK_BOOTSTRAP_PORT, &port);
    fprintf(stderr, "probe_bootstrap phase=%s rc=%d present=%d\n", phase, rc,
            rc == KERN_SUCCESS && MACH_PORT_VALID(port));
    if (rc == KERN_SUCCESS && MACH_PORT_VALID(port)) {
        mach_port_deallocate(mach_task_self(), port);
    }
}

int main(int argc, char **argv) {
    if (argc < 3 || (strcmp(argv[1], "inherited") && strcmp(argv[1], "registered") &&
                     strcmp(argv[1], "null-bootstrap"))) {
        fprintf(stderr, "usage: bootstrap-exec inherited|registered|null-bootstrap /absolute/program [args]\n");
        return 64;
    }
    report_port("before");
    kern_return_t rc = KERN_SUCCESS;
    if (strcmp(argv[1], "inherited")) {
        mach_port_t ports[3] = {MACH_PORT_NULL, MACH_PORT_NULL, MACH_PORT_NULL};
        rc = mach_ports_register(mach_task_self(), ports, 3);
    }
    if (rc == KERN_SUCCESS && !strcmp(argv[1], "null-bootstrap")) {
        rc = task_set_special_port(mach_task_self(), TASK_BOOTSTRAP_PORT, MACH_PORT_NULL);
    }
    fprintf(stderr, "probe_mode=%s mutation_rc=%d\n", argv[1], rc);
    report_port("after");
    fflush(stderr);
    if (rc != KERN_SUCCESS || argv[2][0] != '/') {
        return 65;
    }
    execv(argv[2], &argv[2]);
    perror("execv");
    return 66;
}
