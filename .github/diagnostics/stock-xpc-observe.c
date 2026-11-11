#include <bsm/libbsm.h>
#include <dispatch/dispatch.h>
#include <errno.h>
#include <fcntl.h>
#include <mach/mach.h>
#include <mach/task_special_ports.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <xpc/xpc.h>

enum {
    canary_id = -2047655148,
    rendezvous_id = -2047655147,
    control_id = -2047655146,
    transfer_id = -2047655145,
    header_transfer_id = -2047655144,
    registered_slot_limit = 3,
    stock_request_size = 200,
    startup_timeout = 4000,
    observation_window = 3000,
    send_timeout = 500,
    sink_timeout = 15000,
    source_watchdog_seconds = 20,
};

struct registry_readback {
    kern_return_t status;
    mach_msg_type_number_t count;
    char slots[registered_slot_limit + 1];
};

struct pre_exec_readback {
    kern_return_t clear;
    struct registry_readback registry;
};

struct shape {
    unsigned canary;
    unsigned matched;
    unsigned first_size;
    bool first_complex;
    mach_msg_type_name_t disposition;
    mach_port_t reply;
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

static bool inspect_registry(struct registry_readback *readback, mach_port_t replacement) {
    mach_port_array_t registered = NULL;
    readback->status = mach_ports_lookup(mach_task_self(), &registered, &readback->count);
    if (readback->status == KERN_SUCCESS && registered != NULL) {
        for (mach_msg_type_number_t i = 0; i < readback->count; ++i) {
            mach_port_t name = registered[i];
            if (i < registered_slot_limit) {
                if (name == MACH_PORT_NULL) {
                    readback->slots[i] = 'N';
                } else if (name == MACH_PORT_DEAD) {
                    readback->slots[i] = 'D';
                } else {
                    mach_port_type_t type = 0;
                    kern_return_t type_status = mach_port_type(mach_task_self(), name, &type);
                    if (type_status != KERN_SUCCESS) {
                        readback->slots[i] = '?';
                    } else if (type & MACH_PORT_TYPE_DEAD_NAME) {
                        readback->slots[i] = 'D';
                    } else if (name == replacement && (type & MACH_PORT_TYPE_SEND)) {
                        readback->slots[i] = 'A';
                    } else {
                        readback->slots[i] = 'L';
                    }
                }
            }
            if (MACH_PORT_VALID(name)) {
                mach_port_deallocate(mach_task_self(), name);
            }
        }
        if (readback->count != 0) {
            vm_deallocate(mach_task_self(), (vm_address_t)registered,
                          readback->count * sizeof(*registered));
        }
    }
    return readback->status == KERN_SUCCESS && readback->count == registered_slot_limit &&
           memcmp(readback->slots, "NNN", registered_slot_limit) == 0;
}

static void print_registry(const char *phase, const struct registry_readback *readback) {
    fprintf(stderr, "registered %s status=%d slots=%u values=%s\n",
            phase, readback->status, readback->count, readback->slots);
}

static mach_port_t check_replacement(void) {
    mach_port_t port = MACH_PORT_NULL;
    require(task_get_bootstrap_port(mach_task_self(), &port) == KERN_SUCCESS
            && MACH_PORT_VALID(port), "post-exec task slot");
    struct registry_readback registry = {0};
    bool clean = inspect_registry(&registry, port);
    print_registry("post-exec", &registry);
    require(clean, "post-exec three null registered slots");
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


struct port_message {
    mach_msg_header_t header;
    mach_msg_body_t body;
    mach_msg_port_descriptor_t port;
};

_Static_assert(offsetof(struct port_message, port) ==
               sizeof(mach_msg_header_t) + sizeof(mach_msg_body_t),
               "single port descriptor layout");
_Static_assert(sizeof(struct port_message) ==
               sizeof(mach_msg_header_t) + sizeof(mach_msg_body_t) +
                   sizeof(mach_msg_port_descriptor_t),
               "single port descriptor message size");

union received_message {
    mach_msg_header_t header;
    mach_msg_audit_trailer_t alignment;
    struct port_message port;
    unsigned char bytes[8192];
};

struct fixture {
    pid_t pid;
    int report;
    int gate;
    int pre_exec;
};

static unsigned long long milliseconds(void) {
    struct timespec now;
    require(clock_gettime(CLOCK_MONOTONIC, &now) == 0, "clock");
    return (unsigned long long)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static mach_port_t make_receiver(void) {
    mach_port_t receive = MACH_PORT_NULL;
    require(mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &receive) ==
                KERN_SUCCESS, "fixture receive port");
    return receive;
}

static mach_msg_return_t send_message(mach_msg_header_t *header) {
    return mach_msg(header, MACH_SEND_MSG | MACH_SEND_TIMEOUT | MACH_SEND_INTERRUPT,
                    header->msgh_size, 0, MACH_PORT_NULL, send_timeout, MACH_PORT_NULL);
}

static mach_msg_return_t send_port(mach_port_t destination, mach_msg_id_t id,
                                  mach_port_t right, mach_msg_type_name_t disposition) {
    struct port_message message = {
        .header = {
            .msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, 0) | MACH_MSGH_BITS_COMPLEX,
            .msgh_size = sizeof(message),
            .msgh_remote_port = destination,
            .msgh_id = id,
        },
        .body = {.msgh_descriptor_count = 1},
        .port = {.name = right, .disposition = disposition, .type = MACH_MSG_PORT_DESCRIPTOR},
    };
    mach_msg_return_t status = send_message(&message.header);
    if (status != MACH_MSG_SUCCESS && disposition == MACH_MSG_TYPE_MOVE_SEND_ONCE) {
        // A failed send can return the right under a new name in this buffer.
        mach_msg_destroy(&message.header);
    }
    return status;
}

static mach_msg_return_t send_reply_header(mach_port_t destination, mach_port_t reply) {
    mach_msg_header_t message = {
        .msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, MACH_MSG_TYPE_MOVE_SEND_ONCE),
        .msgh_size = sizeof(message),
        .msgh_remote_port = destination,
        .msgh_local_port = reply,
        .msgh_id = header_transfer_id,
    };
    mach_msg_return_t status = send_message(&message);
    fprintf(stderr, "ordinary transfer_status=%#x\n", (unsigned)status);
    if (status != MACH_MSG_SUCCESS) {
        // A failed send can return the reply right under a new local name.
        bool returned_reply = MACH_MSGH_BITS_LOCAL(message.msgh_bits) == MACH_MSG_TYPE_MOVE_SEND_ONCE &&
                              MACH_PORT_VALID(message.msgh_local_port);
        if (returned_reply) {
            mach_port_type_t type = 0;
            kern_return_t type_status = mach_port_type(mach_task_self(), message.msgh_local_port, &type);
            bool release = type_status == KERN_SUCCESS &&
                           (type & (MACH_PORT_TYPE_SEND_ONCE | MACH_PORT_TYPE_DEAD_NAME)) != 0;
            fprintf(stderr, "ordinary failed_header_reply_type_status=%d right_type=%#x drop_attempted=%d\n",
                    type_status, type, release);
            if (release) {
                kern_return_t dropped = mach_port_deallocate(mach_task_self(), message.msgh_local_port);
                fprintf(stderr, "ordinary failed_header_reply_drop_status=%d\n", dropped);
            }
            message.msgh_local_port = MACH_PORT_NULL;
            message.msgh_bits &= ~MACH_MSGH_BITS_LOCAL_MASK;
        } else {
            fprintf(stderr, "ordinary failed_header_reply_drop_attempted=0\n");
        }
        mach_msg_destroy(&message);
    }
    return status;
}

static mach_msg_return_t receive_message(union received_message *message, mach_port_t port,
                                         mach_msg_timeout_t timeout) {
    return mach_msg(&message->header,
                    MACH_RCV_MSG | MACH_RCV_TIMEOUT | MACH_RCV_LARGE |
                        MACH_RCV_TRAILER_TYPE(MACH_MSG_TRAILER_FORMAT_0) |
                        MACH_RCV_TRAILER_ELEMENTS(MACH_RCV_TRAILER_AUDIT),
                    0, sizeof(*message), port, timeout, MACH_PORT_NULL);
}

static bool has_sender(const union received_message *message, pid_t expected) {
    const size_t size = message->header.msgh_size;
    const size_t aligned_size = (size + 3u) & ~(size_t)3u;
    if (size < sizeof(message->header) ||
        aligned_size > sizeof(*message) - sizeof(mach_msg_audit_trailer_t)) {
        return false;
    }
    mach_msg_audit_trailer_t trailer;
    memcpy(&trailer, message->bytes + aligned_size, sizeof(trailer));
    return trailer.msgh_trailer_type == MACH_MSG_TRAILER_FORMAT_0 &&
           trailer.msgh_trailer_size >= sizeof(trailer) &&
           audit_token_to_pid(trailer.msgh_audit) == expected;
}

static bool fixture_port_message(const union received_message *message, pid_t sender,
                                 mach_msg_id_t id, mach_msg_type_name_t disposition) {
    const struct port_message *port = &message->port;
    return has_sender(message, sender) && port->header.msgh_size == sizeof(*port) &&
           port->header.msgh_id == id && (port->header.msgh_bits & MACH_MSGH_BITS_COMPLEX) &&
           port->header.msgh_remote_port == MACH_PORT_NULL &&
           MACH_MSGH_BITS_REMOTE(port->header.msgh_bits) == 0 &&
           port->header.msgh_voucher_port == MACH_PORT_NULL &&
           MACH_MSGH_BITS_VOUCHER(port->header.msgh_bits) == 0 &&
           port->body.msgh_descriptor_count == 1 && port->port.type == MACH_MSG_PORT_DESCRIPTOR &&
           port->port.disposition == disposition && MACH_PORT_VALID(port->port.name);
}

static bool live_right(const char *phase, mach_port_t port, mach_port_type_t expected) {
    mach_port_type_t type = 0;
    kern_return_t status = mach_port_type(mach_task_self(), port, &type);
    fprintf(stderr, "%s right_type_status=%d right_type=%#x right_dead=%d\n",
            phase, status, type, (type & MACH_PORT_TYPE_DEAD_NAME) != 0);
    const mach_port_type_t rights = MACH_PORT_TYPE_SEND | MACH_PORT_TYPE_SEND_ONCE |
                                    MACH_PORT_TYPE_RECEIVE | MACH_PORT_TYPE_DEAD_NAME;
    return status == KERN_SUCCESS && (type & rights) == expected;
}

static bool drop_reply_header(union received_message *message, mach_port_t receive, pid_t observer) {
    mach_msg_header_t *header = &message->header;
    if (!has_sender(message, observer) || header->msgh_size != sizeof(*header) ||
        header->msgh_id != header_transfer_id || (header->msgh_bits & MACH_MSGH_BITS_COMPLEX) ||
        header->msgh_local_port != receive ||
        MACH_MSGH_BITS_LOCAL(header->msgh_bits) != MACH_MSG_TYPE_PORT_SEND ||
        MACH_MSGH_BITS_REMOTE(header->msgh_bits) != MACH_MSG_TYPE_PORT_SEND_ONCE ||
        header->msgh_voucher_port != MACH_PORT_NULL || MACH_MSGH_BITS_VOUCHER(header->msgh_bits) != 0 ||
        !MACH_PORT_VALID(header->msgh_remote_port) ||
        !live_right("header-sink", header->msgh_remote_port, MACH_PORT_TYPE_SEND_ONCE)) {
        return false;
    }

    kern_return_t dropped = mach_port_deallocate(mach_task_self(), header->msgh_remote_port);
    fprintf(stderr, "header-sink reply_drop_status=%d\n", dropped);
    header->msgh_remote_port = MACH_PORT_NULL;
    header->msgh_bits &= ~MACH_MSGH_BITS_REMOTE_MASK;
    return dropped == KERN_SUCCESS;
}

static void inert_sink(int report, pid_t observer, bool header_transfer) {
    mach_port_t bootstrap = check_replacement();
    mach_port_t receive = make_receiver();
    require(send_port(bootstrap, rendezvous_id, receive, MACH_MSG_TYPE_MAKE_SEND) ==
                MACH_MSG_SUCCESS, "sink rendezvous send");

    union received_message message;
    mach_msg_return_t status = receive_message(&message, receive, sink_timeout);
    fprintf(stderr, "%s receive_status=%#x\n", header_transfer ? "header-sink" : "sink",
            (unsigned)status);
    bool received = status == MACH_MSG_SUCCESS;
    bool verified = received &&
                    (header_transfer
                         ? drop_reply_header(&message, receive, observer)
                         : fixture_port_message(&message, observer, transfer_id, MACH_MSG_TYPE_PORT_SEND_ONCE) &&
                               live_right("sink", message.port.port.name, MACH_PORT_TYPE_SEND_ONCE));
    if (received) {
        mach_msg_destroy(&message.header);
    }
    kern_return_t destroyed = mach_port_destroy(mach_task_self(), receive);
    if (header_transfer) {
        fprintf(stderr, "header-sink receive_port_destroy_status=%d\n", destroyed);
        verified &= destroyed == KERN_SUCCESS;
    }
    require(verified, "sink receipt");
    signal_phase(report, 'D');
}

static void request_child(bool stock, int report, int gate) {
    mach_port_t replacement = check_replacement();
    if (stock) {
        send_canary(replacement);
    }
    signal_phase(report, 'R');
    char go;
    require(read(gate, &go, 1) == 1 && go == 'G', "request gate");
    signal_phase(report, 'E');
    mach_port_t control_reply = MACH_PORT_NULL;
    if (stock) {
        char name[96];
        snprintf(name, sizeof(name), "org.example.stock-xpc-observation.%08x.%08x",
                 arc4random(), arc4random());
        xpc_connection_t connection = xpc_connection_create_mach_service(name, NULL, 0);
        require(connection != NULL, "public named XPC connection");
        xpc_connection_set_event_handler(connection, ^(xpc_object_t event) {
            if (event == XPC_ERROR_CONNECTION_INVALID) {
                signal_phase(report, 'I');
            }
        });
        xpc_connection_resume(connection);
    } else {
        control_reply = make_receiver();
        mach_msg_header_t message = {
            .msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, MACH_MSG_TYPE_MAKE_SEND_ONCE),
            .msgh_size = sizeof(message),
            .msgh_remote_port = replacement,
            .msgh_local_port = control_reply,
            .msgh_id = control_id,
        };
        require(send_message(&message) == MACH_MSG_SUCCESS, "generic Mach request send");
    }
    signal_phase(report, 'A');

    // The observer holds its gate open until settlement. EOF also handles observer exit.
    require(read(gate, &go, 1) == 0, "request settlement");
    if (MACH_PORT_VALID(control_reply)) {
        mach_port_destroy(mach_task_self(), control_reply);
    }
}

static struct fixture spawn_fixture(const char *binary, const char *role) {
    int report[2], gate[2], pre_exec[2];
    require(pipe(report) == 0 && pipe(gate) == 0 && pipe(pre_exec) == 0 &&
            fcntl(report[0], F_SETFL, O_NONBLOCK) == 0 &&
            fcntl(report[0], F_SETFD, FD_CLOEXEC) == 0 &&
            fcntl(gate[1], F_SETFD, FD_CLOEXEC) == 0 &&
            fcntl(pre_exec[0], F_SETFD, FD_CLOEXEC) == 0, "fixture control pipes");
    char report_arg[16], gate_arg[16], observer_arg[16];
    snprintf(report_arg, sizeof(report_arg), "%d", report[1]);
    snprintf(gate_arg, sizeof(gate_arg), "%d", gate[0]);
    snprintf(observer_arg, sizeof(observer_arg), "%d", getpid());

    pid_t child = fork();
    require(child >= 0, "fixture fork");
    if (child == 0) {
        close(report[0]);
        close(gate[1]);
        close(pre_exec[0]);
        struct pre_exec_readback readback = {
            .clear = mach_ports_register(mach_task_self(), NULL, 0),
        };
        bool clean = readback.clear == KERN_SUCCESS &&
                     inspect_registry(&readback.registry, MACH_PORT_NULL);
        if (write(pre_exec[1], &readback, sizeof(readback)) != (ssize_t)sizeof(readback) ||
            !clean) {
            _exit(3);
        }
        close(pre_exec[1]);
        execl(binary, binary, role, report_arg, gate_arg, observer_arg, (char *)NULL);
        _exit(4);
    }
    close(report[1]);
    close(gate[0]);
    close(pre_exec[1]);
    return (struct fixture){.pid = child, .report = report[0], .gate = gate[1], .pre_exec = pre_exec[0]};
}

static bool read_within(int fd, void *buffer, size_t size, int timeout) {
    struct pollfd input = {.fd = fd, .events = POLLIN};
    return poll(&input, 1, timeout) == 1 && (input.revents & POLLIN) &&
           read(fd, buffer, size) == (ssize_t)size;
}

static bool check_pre_exec(struct fixture *fixture, const char *role) {
    struct pre_exec_readback readback;
    bool reported = read_within(fixture->pre_exec, &readback, sizeof(readback), startup_timeout);
    close(fixture->pre_exec);
    if (!reported) {
        fprintf(stderr, "%s registered pre-exec readback missing\n", role);
        return false;
    }
    fprintf(stderr, "%s registered pre-exec clear status=%d\n", role, readback.clear);
    if (readback.clear != KERN_SUCCESS) {
        return false;
    }
    print_registry("pre-exec", &readback.registry);
    return readback.registry.status == KERN_SUCCESS &&
           readback.registry.count == registered_slot_limit &&
           memcmp(readback.registry.slots, "NNN", registered_slot_limit) == 0;
}

static bool finish_fixture(struct fixture fixture, const char *role, unsigned timeout) {
    close(fixture.gate);
    int status;
    unsigned long long deadline = milliseconds() + timeout;
    for (;;) {
        pid_t result = waitpid(fixture.pid, &status, WNOHANG);
        if (result == fixture.pid) {
            break;
        }
        require(result == 0 || errno == EINTR, "wait for fixture");
        if (milliseconds() >= deadline) {
            kill(fixture.pid, SIGKILL);
            do {
                result = waitpid(fixture.pid, &status, 0);
            } while (result < 0 && errno == EINTR);
            require(result == fixture.pid, "join fixture");
            break;
        }
        struct timespec delay = {.tv_nsec = 10000000};
        nanosleep(&delay, NULL);
    }
    close(fixture.report);
    fprintf(stderr, "%s exited=%d status=%d signal=%d\n", role, WIFEXITED(status),
            WIFEXITED(status) ? WEXITSTATUS(status) : -1,
            WIFSIGNALED(status) ? WTERMSIG(status) : 0);
    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

static void observe(mach_port_t port, pid_t sender, bool stock, bool collect_reply,
                    struct shape *shape, mach_msg_timeout_t timeout) {
    union received_message message;
    mach_msg_return_t status = receive_message(&message, port, timeout);
    if (status == MACH_RCV_TIMED_OUT) {
        return;
    }
    if (status != MACH_MSG_SUCCESS) {
        fprintf(stderr, "observer receive_status=%#x\n", (unsigned)status);
        shape->invalid = true;
        return;
    }

    mach_msg_header_t *header = &message.header;
    bool complex = (header->msgh_bits & MACH_MSGH_BITS_COMPLEX) != 0;
    if (!has_sender(&message, sender)) {
        shape->invalid = true;
    } else if (header->msgh_id == canary_id) {
        if (stock && !collect_reply && header->msgh_size == sizeof(*header) && !complex &&
            header->msgh_remote_port == MACH_PORT_NULL && MACH_MSGH_BITS_REMOTE(header->msgh_bits) == 0 &&
            header->msgh_voucher_port == MACH_PORT_NULL && MACH_MSGH_BITS_VOUCHER(header->msgh_bits) == 0) {
            ++shape->canary;
        } else {
            shape->invalid = true;
        }
    } else if (shape->matched++ == 0) {
        shape->first_size = header->msgh_size;
        shape->first_complex = complex;
        shape->disposition = MACH_MSGH_BITS_REMOTE(header->msgh_bits);
        bool expected = stock ? header->msgh_size == stock_request_size
                              : header->msgh_id == control_id && header->msgh_size == sizeof(*header);
        if (collect_reply && expected && !complex &&
            header->msgh_voucher_port == MACH_PORT_NULL &&
            MACH_MSGH_BITS_VOUCHER(header->msgh_bits) == 0 &&
            shape->disposition == MACH_MSG_TYPE_PORT_SEND_ONCE &&
            MACH_PORT_VALID(header->msgh_remote_port)) {
            shape->reply = header->msgh_remote_port;
            header->msgh_remote_port = MACH_PORT_NULL;
            header->msgh_bits &= ~MACH_MSGH_BITS_REMOTE_MASK;
        }
    }
    mach_msg_destroy(header);
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

static void receive_for(mach_port_t port, struct fixture source, bool stock, bool collect_reply,
                        struct progress *progress, struct shape *shape,
                        unsigned duration, bool until_ready) {
    unsigned long long end = milliseconds() + duration;
    while (!shape->invalid && milliseconds() < end) {
        read_progress(source.report, progress);
        if (until_ready && progress->ready) {
            break;
        }
        observe(port, source.pid, stock, collect_reply, shape, 25);
    }
    read_progress(source.report, progress);
}

static mach_port_t receive_rendezvous(mach_port_t port, pid_t sink) {
    union received_message message;
    mach_msg_return_t status = receive_message(&message, port, startup_timeout);
    fprintf(stderr, "rendezvous receive_status=%#x\n", (unsigned)status);
    if (status != MACH_MSG_SUCCESS) {
        return MACH_PORT_NULL;
    }
    mach_port_t destination = MACH_PORT_NULL;
    if (fixture_port_message(&message, sink, rendezvous_id, MACH_MSG_TYPE_PORT_SEND) &&
        live_right("rendezvous", message.port.port.name, MACH_PORT_TYPE_SEND)) {
        destination = message.port.port.name;
        message.port.port.name = MACH_PORT_NULL;
    }
    mach_msg_destroy(&message.header);
    return destination;
}

static int observe_request(const char *binary, const char *role, bool stock, mach_port_t port,
                           struct fixture sink, mach_port_t destination) {
    struct fixture source = spawn_fixture(binary, role);
    struct progress progress = {0};
    struct shape startup = {0}, lookup = {0}, settlement = {0};
    if (check_pre_exec(&source, role)) {
        receive_for(port, source, stock, false, &progress, &startup, startup_timeout, true);
        if (progress.ready && !startup.invalid) {
            receive_for(port, source, stock, false, &progress, &startup, 200, false);
            if (!startup.invalid && startup.canary == (stock ? 1u : 0u) &&
                startup.matched == 0 && write(source.gate, "G", 1) == 1) {
                receive_for(port, source, stock, true, &progress, &lookup, observation_window, false);
            }
        }
    }

    fprintf(stderr,
            "%s ready=%d entered=%d resumed=%d unavailable=%d startup_canary=%u startup_other=%u "
            "lookup_count=%u lookup_canary=%u lookup_size=%u complex=%d disposition=%u invalid=%d\n",
            role, progress.ready, progress.entered, progress.resumed, progress.unavailable,
            startup.canary, startup.matched, lookup.matched, lookup.canary, lookup.first_size,
            lookup.first_complex, lookup.disposition, startup.invalid || lookup.invalid);
    int outcome = 1;
    if (progress.ready && progress.entered && (stock || progress.resumed) && !startup.invalid &&
        !lookup.invalid && lookup.matched == 1 && lookup.canary == 0 && MACH_PORT_VALID(lookup.reply) &&
        live_right("observer", lookup.reply, MACH_PORT_TYPE_SEND_ONCE)) {
        fprintf(stderr, "%s transfer_attempt=1\n", role);
        mach_msg_return_t status = stock
                                       ? send_reply_header(destination, lookup.reply)
                                       : send_port(destination, transfer_id, lookup.reply,
                                                   MACH_MSG_TYPE_MOVE_SEND_ONCE);
        lookup.reply = MACH_PORT_NULL;
        if (!stock) {
            fprintf(stderr, "%s transfer_status=%#x\n", role, (unsigned)status);
        }
        bool dropped = false;
        if (status == MACH_MSG_SUCCESS) {
            char ack;
            dropped = read_within(sink.report, &ack, 1, 1500) && ack == 'D';
        }
        if (status == MACH_MSG_SUCCESS || stock) {
            receive_for(port, source, stock, false, &progress, &settlement, stock ? 1000 : 200, false);
            fprintf(stderr, "%s sink_drop_ack=%d settlement_resumed=%d settlement_unavailable=%d "
                            "settlement_extra=%u invalid=%d\n",
                    role, dropped, progress.resumed, progress.unavailable,
                    settlement.canary + settlement.matched, settlement.invalid);
        }
        if (status == MACH_MSG_SUCCESS) {
            outcome = dropped && !settlement.invalid &&
                      settlement.canary == 0 && settlement.matched == 0 &&
                      (!stock || (progress.resumed && progress.unavailable)) ? 0 : 1;
        } else if (status == MACH_SEND_INVALID_RIGHT) {
            fprintf(stderr, "%s result=kernel_invalid_right\n", role);
            outcome = 10;
        } else if (status == MACH_SEND_TIMED_OUT) {
            fprintf(stderr, "%s result=send_timeout\n", role);
        }
    }
    if (MACH_PORT_VALID(lookup.reply)) {
        kern_return_t released = mach_port_deallocate(mach_task_self(), lookup.reply);
        fprintf(stderr, "%s retained_reply_release_status=%d\n", role, released);
    }
    bool source_exited = finish_fixture(source, role, 1000);
    return outcome == 0 && !source_exited ? 1 : outcome;
}

static int run_arm(const char *binary, const char *role, bool stock) {
    mach_port_t port = make_receiver();
    require(mach_port_insert_right(mach_task_self(), port, port, MACH_MSG_TYPE_MAKE_SEND) ==
                KERN_SUCCESS, "fixture bootstrap send right");
    require(task_set_bootstrap_port(mach_task_self(), port) == KERN_SUCCESS,
            "assign inert task bootstrap port");
    const char *sink_role = stock ? "header-sink" : "sink";
    struct fixture sink = spawn_fixture(binary, sink_role);
    mach_port_t destination = MACH_PORT_NULL;
    if (check_pre_exec(&sink, sink_role)) {
        destination = receive_rendezvous(port, sink.pid);
    }
    int outcome = MACH_PORT_VALID(destination)
                      ? observe_request(binary, role, stock, port, sink, destination)
                      : 1;
    if (MACH_PORT_VALID(destination)) {
        mach_port_deallocate(mach_task_self(), destination);
    }
    bool sink_exited = finish_fixture(sink, sink_role, stock ? sink_timeout + 1000 : 1000);
    mach_port_destroy(mach_task_self(), port);
    return outcome == 0 && !sink_exited ? 1 : outcome;
}

static int supervise(const char *binary, const char *role, bool stock) {
    pid_t observer = fork();
    require(observer >= 0, "observer fork");
    if (observer == 0) {
        _exit(run_arm(binary, role, stock));
    }
    int status;
    pid_t result;
    do {
        result = waitpid(observer, &status, 0);
    } while (result < 0 && errno == EINTR);
    require(result == observer, "join observer");
    fprintf(stderr, "%s observer_exited=%d status=%d signal=%d\n", role, WIFEXITED(status),
            WIFEXITED(status) ? WEXITSTATUS(status) : -1,
            WIFSIGNALED(status) ? WTERMSIG(status) : 0);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}

int main(int argc, char *argv[]) {
    require(argv[0][0] == '/' && getuid() != 0, "unprivileged launcher");
    if (argc == 5) {
        int report = atoi(argv[2]), gate = atoi(argv[3]);
        pid_t observer = atoi(argv[4]);
        require(observer > 1 && observer == getppid(), "fixture observer");
        bool header_sink = strcmp(argv[1], "header-sink") == 0;
        if (header_sink || strcmp(argv[1], "sink") == 0) {
            inert_sink(report, observer, header_sink);
        } else {
            bool stock = strcmp(argv[1], "ordinary") == 0;
            require(stock || strcmp(argv[1], "generic") == 0, "fixture role");
            if (stock) {
                // A blocked XPC resume may never reach the observer gate.
                sigset_t watchdog;
                require(sigemptyset(&watchdog) == 0 && sigaddset(&watchdog, SIGALRM) == 0 &&
                        signal(SIGALRM, SIG_DFL) != SIG_ERR &&
                        sigprocmask(SIG_UNBLOCK, &watchdog, NULL) == 0, "ordinary source watchdog");
                alarm(source_watchdog_seconds);
            }
            request_child(stock, report, gate);
        }
        return 0;
    }
    require(argc == 1, "launcher arguments");
    signal(SIGPIPE, SIG_IGN);
    int control = supervise(argv[0], "generic", false);
    if (control != 0) {
        fprintf(stderr, "ordinary skipped: generic receipt gate failed\n");
        return 1;
    }
    return supervise(argv[0], "ordinary", true);
}
