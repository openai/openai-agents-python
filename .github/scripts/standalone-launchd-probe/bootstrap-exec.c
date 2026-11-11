#include <mach/mach.h>
#include <mach/task_special_ports.h>
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
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
    int recovery = argc >= 2 && (!strcmp(argv[1], "recover-parent") ||
                                !strcmp(argv[1], "recover-root"));
    if (argc < 3 || (strcmp(argv[1], "inherited") && strcmp(argv[1], "registered") &&
                     strcmp(argv[1], "null-bootstrap") && strcmp(argv[1], "subset") && !recovery)) {
        fprintf(stderr, "usage: bootstrap-exec inherited|registered|null-bootstrap|subset|recover-parent|recover-root /absolute/program [args]\n");
        return 64;
    }
    report_port("before");
    kern_return_t rc = KERN_SUCCESS;
    if (!recovery && strcmp(argv[1], "inherited")) {
        mach_port_t ports[3] = {MACH_PORT_NULL, MACH_PORT_NULL, MACH_PORT_NULL};
        rc = mach_ports_register(mach_task_self(), ports, 3);
    }
    if (rc == KERN_SUCCESS && !strcmp(argv[1], "null-bootstrap")) {
        rc = task_set_special_port(mach_task_self(), TASK_BOOTSTRAP_PORT, MACH_PORT_NULL);
    }
    if (rc == KERN_SUCCESS && recovery) {
        typedef kern_return_t (*getter_fn)(mach_port_t, mach_port_t *);
        const char *symbol = !strcmp(argv[1], "recover-parent") ? "bootstrap_parent" : "bootstrap_get_root";
        getter_fn getter = (getter_fn)dlsym(RTLD_DEFAULT, symbol);
        fprintf(stderr, "probe_symbol name=%s available=%d\n", symbol, getter != NULL);
        if (getter == NULL) {
            return 69;
        }
        mach_port_t current = MACH_PORT_NULL, recovered = MACH_PORT_NULL;
        rc = task_get_special_port(mach_task_self(), TASK_BOOTSTRAP_PORT, &current);
        fprintf(stderr, "probe_step=recovery_current rc=%d\n", rc);
        if (rc == KERN_SUCCESS) {
            rc = getter(current, &recovered);
            fprintf(stderr, "probe_step=%s rc=%d port_valid=%d\n", symbol, rc, MACH_PORT_VALID(recovered));
        }
        if (rc == KERN_SUCCESS && MACH_PORT_VALID(recovered)) {
            rc = task_set_special_port(mach_task_self(), TASK_BOOTSTRAP_PORT, recovered);
            fprintf(stderr, "probe_step=recovery_install rc=%d\n", rc);
        } else {
            return 65;
        }
    }
    if (rc == KERN_SUCCESS && !strcmp(argv[1], "subset")) {
        typedef kern_return_t (*subset_fn)(mach_port_t, mach_port_t, mach_port_t *);
        subset_fn subset = (subset_fn)dlsym(RTLD_DEFAULT, "bootstrap_subset");
        fprintf(stderr, "probe_symbol name=bootstrap_subset available=%d\n", subset != NULL);
        if (subset == NULL) {
            return 69;
        }
        mach_port_t current = MACH_PORT_NULL, requestor = MACH_PORT_NULL, child_port = MACH_PORT_NULL;
        rc = task_get_special_port(mach_task_self(), TASK_BOOTSTRAP_PORT, &current);
        fprintf(stderr, "probe_step=subset_current rc=%d\n", rc);
        if (rc == KERN_SUCCESS) {
            rc = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &requestor);
            fprintf(stderr, "probe_step=requestor_allocate rc=%d\n", rc);
        }
        if (rc == KERN_SUCCESS) {
            rc = mach_port_insert_right(mach_task_self(), requestor, requestor, MACH_MSG_TYPE_MAKE_SEND);
            fprintf(stderr, "probe_step=requestor_send rc=%d\n", rc);
        }
        if (rc == KERN_SUCCESS) {
            rc = subset(current, requestor, &child_port);
            fprintf(stderr, "probe_step=bootstrap_subset rc=%d port_valid=%d\n", rc, MACH_PORT_VALID(child_port));
        }
        if (rc == KERN_SUCCESS && MACH_PORT_VALID(child_port)) {
            rc = task_set_special_port(mach_task_self(), TASK_BOOTSTRAP_PORT, child_port);
            fprintf(stderr, "probe_step=subset_install rc=%d\n", rc);
        } else {
            return 65;
        }
        if (rc != KERN_SUCCESS) {
            return 65;
        }
        fflush(stderr);
        pid_t child = fork();
        if (child < 0) {
            perror("fork");
            return 66;
        }
        if (child > 0) {
            int status;
            pid_t joined;
            do {
                joined = waitpid(child, &status, 0);
            } while (joined < 0 && errno == EINTR);
            if (joined < 0) {
                perror("waitpid");
                return 66;
            }
            // This parent has been retaining the requester receive right until the payload joins.
            kern_return_t destroyed = mach_port_destroy(mach_task_self(), requestor);
            fprintf(stderr, "probe_step=requestor_destroy rc=%d child_joined=1\n", destroyed);
            if (WIFEXITED(status)) {
                return WEXITSTATUS(status);
            }
            if (WIFSIGNALED(status)) {
                fprintf(stderr, "probe_child_signal=%d\n", WTERMSIG(status));
                return 128 + WTERMSIG(status);
            }
            return 66;
        }
        report_port("subset-child-inherited");
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
