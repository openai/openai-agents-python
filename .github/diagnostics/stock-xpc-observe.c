#include <bsm/libbsm.h>
#include <dispatch/dispatch.h>
#include <fcntl.h>
#include <mach/mach.h>
#include <mach/task_special_ports.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <xpc/xpc.h>

enum { canary_id = -2047655148, registered_slot_limit = 3 };

struct shape {
    unsigned canary;
    unsigned matched;
    unsigned first_size;
    bool first_complex;
    bool first_reply;
    bool invalid;
};

struct progress {
    bool ready;
    bool entered;
    bool resumed;
    bool unavailable;
};

static void require(bool condition, const char *phase) {
    if (!condition) {
        fprintf(stderr, "probe control failed at %s\n", phase);
        exit(1);
    }
}

static void signal_phase(int fd, char phase) {
    if (write(fd, &phase, 1) != 1) {
        _exit(2);
    }
}

static bool registry_clean(const char *phase, mach_port_t replacement) {
    mach_port_array_t registered = NULL;
    mach_msg_type_number_t count = 0;
    kern_return_t status = mach_ports_lookup(mach_task_self(), &registered, &count);
    bool clean = status == KERN_SUCCESS && count <= registered_slot_limit &&
                 (count == 0 || registered != NULL);
    char slots[registered_slot_limit + 1] = "";
    if (clean) {
        for (mach_msg_type_number_t i = 0; i < count; ++i) {
            mach_port_t name = registered[i];
            if (name == MACH_PORT_NULL) {
                slots[i] = 'N';
            } else if (name == MACH_PORT_DEAD) {
                slots[i] = 'D';
            } else {
                mach_port_type_t type = 0;
                kern_return_t type_status = mach_port_type(mach_task_self(), name, &type);
                if (type_status != KERN_SUCCESS) {
                    slots[i] = '?';
                } else if (type & MACH_PORT_TYPE_DEAD_NAME) {
                    slots[i] = 'D';
                } else if (name == replacement && (type & MACH_PORT_TYPE_SEND)) {
                    slots[i] = 'A';
                } else {
                    slots[i] = 'L';
                }
                mach_port_deallocate(mach_task_self(), name);
            }
            clean &= slots[i] == 'N' || slots[i] == 'D' || slots[i] == 'A';
        }
        if (count != 0) {
            vm_deallocate(mach_task_self(), (vm_address_t)registered,
                          count * sizeof(*registered));
        }
    }
    fprintf(stderr, "registered %s status=%d slots=%u values=%s\n",
            phase, status, count, slots);
    return clean;
}

static mach_port_t check_replacement(void) {
    mach_port_t port = MACH_PORT_NULL;
    require(task_get_bootstrap_port(mach_task_self(), &port) == KERN_SUCCESS
            && MACH_PORT_VALID(port), "post-exec task slot");
    require(registry_clean("post-exec", port), "no extra live registered rights");
    return port;
}

static void send_canary(mach_port_t replacement) {
    mach_msg_header_t message = {
        .msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, 0),
        .msgh_size = sizeof(message),
        .msgh_remote_port = replacement,
        .msgh_id = canary_id,
    };
    require(mach_msg(&message, MACH_SEND_MSG | MACH_SEND_TIMEOUT,
                     sizeof(message), 0, MACH_PORT_NULL, 500,
                     MACH_PORT_NULL) == MACH_MSG_SUCCESS, "canary send");
}

static void stock_child(const char *mode, int report, int gate) {
    const bool baseline = strncmp(mode, "baseline-", 9) == 0;
    const bool canary = strcmp(mode, "canary") == 0;
    mach_port_t replacement = baseline ? MACH_PORT_NULL : check_replacement();
    if (!baseline && !canary) {
        send_canary(replacement);
    }
    signal_phase(report, 'R');
    char go;
    if (read(gate, &go, 1) != 1) {
        _exit(2);
    }
    signal_phase(report, 'E');
    if (canary) {
        send_canary(replacement);
        _exit(0);
    }
    if (strcmp(mode, "idle") != 0) {
        char name[96];
        snprintf(name, sizeof(name), "org.example.stock-xpc-observation.%08x.%08x",
                 arc4random(), arc4random());
        const size_t length = strlen(mode);
        const bool privileged = length >= 10 && strcmp(mode + length - 10, "privileged") == 0;
        xpc_connection_t connection = xpc_connection_create_mach_service(
            name, NULL, privileged ? XPC_CONNECTION_MACH_SERVICE_PRIVILEGED : 0);
        require(connection != NULL, "public named XPC connection");
        xpc_connection_set_event_handler(connection, ^(xpc_object_t event) {
            if (event == XPC_ERROR_CONNECTION_INVALID) {
                signal_phase(report, 'I');
            }
        });
        xpc_connection_resume(connection);
        signal_phase(report, 'A');
    }
    for (;;) {
        pause();
    }
}

static void observe(mach_port_t sink, pid_t child, struct shape *shape) {
    union {
        mach_msg_header_t header;
        mach_msg_audit_trailer_t alignment;
        unsigned char bytes[8192];
    } message;
    mach_msg_return_t result = mach_msg(
        &message.header,
        MACH_RCV_MSG | MACH_RCV_TIMEOUT | MACH_RCV_LARGE |
            MACH_RCV_TRAILER_TYPE(MACH_MSG_TRAILER_FORMAT_0) |
            MACH_RCV_TRAILER_ELEMENTS(MACH_RCV_TRAILER_AUDIT),
        0, sizeof(message), sink, 25, MACH_PORT_NULL);
    if (result == MACH_RCV_TIMED_OUT) {
        return;
    }
    if (result != MACH_MSG_SUCCESS) {
        shape->invalid = true;
        return;
    }

    const size_t size = message.header.msgh_size;
    const size_t aligned_size = (size + 3u) & ~(size_t)3u;
    if (size < sizeof(message.header) ||
        aligned_size > sizeof(message) - sizeof(mach_msg_audit_trailer_t)) {
        shape->invalid = true;
    } else {
        mach_msg_audit_trailer_t trailer;
        memcpy(&trailer, message.bytes + aligned_size, sizeof(trailer));
        if (trailer.msgh_trailer_size < sizeof(trailer) ||
            audit_token_to_pid(trailer.msgh_audit) != child) {
            shape->invalid = true;
        } else if (message.header.msgh_id == canary_id && size == sizeof(message.header) &&
                   !(message.header.msgh_bits & MACH_MSGH_BITS_COMPLEX) &&
                   !MACH_PORT_VALID(message.header.msgh_remote_port)) {
            ++shape->canary;
        } else if (shape->matched++ == 0) {
            shape->first_size = message.header.msgh_size;
            shape->first_complex = (message.header.msgh_bits & MACH_MSGH_BITS_COMPLEX) != 0;
            shape->first_reply = MACH_PORT_VALID(message.header.msgh_remote_port);
        }
    }
    mach_msg_destroy(&message.header);
}

static void read_progress(int fd, struct progress *progress) {
    char phases[32];
    ssize_t count;
    while ((count = read(fd, phases, sizeof(phases))) > 0) {
        for (ssize_t i = 0; i < count; ++i) {
            progress->ready |= phases[i] == 'R';
            progress->entered |= phases[i] == 'E';
            progress->resumed |= phases[i] == 'A';
            progress->unavailable |= phases[i] == 'I';
        }
    }
}

static unsigned long long milliseconds(void) {
    struct timespec now;
    require(clock_gettime(CLOCK_MONOTONIC, &now) == 0, "clock");
    return (unsigned long long)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static void receive_for(mach_port_t sink, pid_t child, int report,
                        struct progress *progress, struct shape *shape,
                        unsigned duration, bool until_ready) {
    unsigned long long end = milliseconds() + duration;
    while (!shape->invalid && milliseconds() < end) {
        read_progress(report, progress);
        if (until_ready && progress->ready) {
            break;
        }
        observe(sink, child, shape);
    }
    read_progress(report, progress);
}

static void print_shape(const char *phase, struct shape shape) {
    printf(" %s=%u/%u/%d/%d", phase, shape.matched, shape.first_size,
           shape.first_complex, shape.first_reply);
}

static bool run(const char *binary, const char *mode) {
    mach_port_t sink = MACH_PORT_NULL;
    require(mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &sink)
                == KERN_SUCCESS &&
            mach_port_insert_right(mach_task_self(), sink, sink, MACH_MSG_TYPE_MAKE_SEND)
                == KERN_SUCCESS, "inert receive port");

    int report[2], gate[2];
    require(pipe(report) == 0 && pipe(gate) == 0 &&
            fcntl(report[0], F_SETFL, O_NONBLOCK) == 0, "control pipes");
    char report_arg[16], gate_arg[16];
    snprintf(report_arg, sizeof(report_arg), "%d", report[1]);
    snprintf(gate_arg, sizeof(gate_arg), "%d", gate[0]);

    const bool baseline = strncmp(mode, "baseline-", 9) == 0;
    const bool canary = strcmp(mode, "canary") == 0;
    const bool idle = strcmp(mode, "idle") == 0;
    // XNU inherits the bootstrap slot, but not an arbitrary parent port name.
    if (!baseline) {
        kern_return_t clear = mach_ports_register(mach_task_self(), NULL, 0);
        fprintf(stderr, "registered clear status=%d\n", clear);
        require(clear == KERN_SUCCESS, "clear inherited registered ports");
        require(task_set_bootstrap_port(mach_task_self(), sink) == KERN_SUCCESS,
                "assign inert task bootstrap port");
        require(registry_clean("pre-fork", sink), "pre-fork registered rights");
    }
    pid_t child = fork();
    require(child >= 0, "fork");
    if (child == 0) {
        close(report[0]);
        close(gate[1]);
        execl(binary, binary, mode, report_arg, gate_arg, (char *)NULL);
        _exit(4);
    }
    close(report[1]);
    close(gate[0]);

    struct progress progress = {0};
    struct shape startup = {0}, lookup = {0};
    receive_for(sink, child, report[0], &progress, &startup, 4000, true);
    if (progress.ready && !startup.invalid) {
        receive_for(sink, child, report[0], &progress, &startup, 200, false);
        unsigned expected_canary = baseline || canary ? 0 : 1;
        if (!startup.invalid && startup.canary == expected_canary &&
            write(gate[1], "G", 1) == 1) {
            receive_for(sink, child, report[0], &progress, &lookup,
                        canary || idle ? 900 : 3000, false);
        }
    }
    close(gate[1]);
    kill(child, SIGKILL);
    int status;
    require(waitpid(child, &status, 0) == child, "join probe child");
    read_progress(report[0], &progress);
    close(report[0]);
    mach_port_destroy(mach_task_self(), sink);

    printf("%s ready=%d entered=%d resumed=%d unavailable=%d invalid_shape=%d", mode,
           progress.ready, progress.entered, progress.resumed, progress.unavailable,
           startup.invalid || lookup.invalid);
    printf(" canary=%u/%u", startup.canary, lookup.canary);
    print_shape("startup", startup);
    print_shape("lookup", lookup);
    putchar('\n');
    fflush(stdout);

    if (!progress.ready || !progress.entered || startup.invalid || lookup.invalid) {
        return false;
    }
    if (baseline) {
        return progress.resumed && progress.unavailable && !startup.matched && !lookup.matched;
    }
    if (canary) {
        return lookup.canary == 1 && lookup.matched == 0 &&
               WIFEXITED(status) && WEXITSTATUS(status) == 0;
    }
    return idle ? lookup.matched == 0 : progress.resumed;
}

int main(int argc, char *argv[]) {
    if (argc == 4) {
        stock_child(argv[1], atoi(argv[2]), atoi(argv[3]));
    }
    require(argc == 1 && argv[0][0] == '/' && getuid() != 0, "unprivileged launcher");
    signal(SIGPIPE, SIG_IGN);
    puts("shape=message_count/first_size/first_complex/first_reply");
    puts("registered=N:null D:dead A:task-port-alias L:distinct-live ?:unclassified");
    const char *modes[] = {"baseline-ordinary", "baseline-privileged", "canary",
                           "idle", "ordinary", "privileged"};
    for (size_t i = 0; i < sizeof(modes) / sizeof(modes[0]); ++i) {
        if (!run(argv[0], modes[i])) {
            return 1;
        }
    }
    return 0;
}
