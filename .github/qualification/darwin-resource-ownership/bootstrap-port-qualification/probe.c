#include <dlfcn.h>
#include <mach/host_special_ports.h>
#include <mach/mach.h>
#include <mach/task_special_ports.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int inspect(const char *tag, int tty)
{
    mach_port_t task = mach_task_self();
    mach_port_t bootstrap = MACH_PORT_NULL;
    mach_port_t parent = MACH_PORT_NULL;
    mach_port_t launchctl = MACH_PORT_NULL;
    mach_port_array_t registered = NULL;
    mach_msg_type_number_t count = 0;
    kern_return_t bootstrap_rc = task_get_bootstrap_port(task, &bootstrap);
    kern_return_t registered_rc = mach_ports_lookup(task, &registered, &count);
    unsigned int rights = 0;
    if (registered_rc == KERN_SUCCESS && registered != NULL) {
        for (mach_msg_type_number_t i = 0; i < count; i++) {
            if (MACH_PORT_VALID(registered[i])) {
                rights++;
                mach_port_deallocate(task, registered[i]);
            }
        }
        vm_deallocate(task, (vm_address_t)registered, count * sizeof(*registered));
    }

    // XNU declares HOST_LAUNCHCTL_PORT, but the host MIG call expects a
    // privileged host right. Check the actual ordinary host right without
    // trying to acquire privileges or sending messages to a returned right.
    mach_port_t host = mach_host_self();
    kern_return_t host_rc = host_get_launchctl_port(host, &launchctl);
    mach_port_deallocate(task, host);
    kern_return_t (*bootstrap_parent)(mach_port_t, mach_port_t *) =
        dlsym(RTLD_DEFAULT, "bootstrap_parent");
    kern_return_t parent_rc = -1;
    if (bootstrap_parent != NULL && !MACH_PORT_VALID(bootstrap)) {
        parent_rc = bootstrap_parent(bootstrap, &parent);
    }
    printf("%s bootstrap_rc=%d bootstrap=%s registered_rc=%d registered=%u "
           "host_launchctl_rc=%d host_launchctl=%s parent_rc=%d parent=%s\n",
           tag, bootstrap_rc, MACH_PORT_VALID(bootstrap) ? "present" : "absent",
           registered_rc, rights, host_rc, MACH_PORT_VALID(launchctl) ? "present" : "absent",
           parent_rc, MACH_PORT_VALID(parent) ? "present" : "absent");
    fflush(stdout);
    if (MACH_PORT_VALID(bootstrap)) {
        mach_port_deallocate(task, bootstrap);
    }
    if (MACH_PORT_VALID(parent)) {
        mach_port_deallocate(task, parent);
    }
    if (MACH_PORT_VALID(launchctl)) {
        mach_port_deallocate(task, launchctl);
    }
    if (tty) {
        pid_t group = tcgetpgrp(STDIN_FILENO);
        int is_terminal = isatty(0) && isatty(1) && isatty(2);
        printf("pty stdio=%d foreground_group=%ld process_group=%ld\n",
               is_terminal, (long)group, (long)getpgrp());
        if (!is_terminal) {
            return 2;
        }
        if (group == getpgrp()) {
            puts("controlling-tty-ok");
        }
    }
    return bootstrap_rc == KERN_SUCCESS && registered_rc == KERN_SUCCESS ? 0 : 1;
}

int main(int argc, char **argv)
{
    if ((argc == 2 || argc == 3) && strcmp(argv[1], "inspect") == 0) {
        alarm(4);
        return inspect("after-exec", argc == 3 && strcmp(argv[2], "tty") == 0);
    }
    if (argc > 2 && strcmp(argv[1], "run-launchctl") == 0) {
        alarm(4);
        argv[1] = "/bin/launchctl";
        execv(argv[1], &argv[1]);
        perror("exec launchctl");
        return 125;
    }
    if (argc < 3 || strcmp(argv[1], "clear-all")) {
        return 125;
    }
    alarm(4);
    if (inspect("before-clear", 0) != 0) {
        return 125;
    }
    mach_port_t task = mach_task_self();
    mach_port_t slots[] = {MACH_PORT_NULL, MACH_PORT_NULL, MACH_PORT_NULL};
    kern_return_t registered_rc = mach_ports_register(task, slots, 3);
    kern_return_t bootstrap_rc = task_set_bootstrap_port(task, MACH_PORT_NULL);
    printf("clear bootstrap_rc=%d registered_rc=%d\n", bootstrap_rc, registered_rc);
    fflush(stdout);
    if (bootstrap_rc != KERN_SUCCESS || registered_rc != KERN_SUCCESS) {
        return 125;
    }
    alarm(0);
    execv(argv[2], &argv[2]);
    perror("exec cleared payload");
    return 125;
}
