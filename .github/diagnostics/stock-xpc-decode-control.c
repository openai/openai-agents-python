#include <bsm/libbsm.h>
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <mach/mach.h>
#include <mach/task_special_ports.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <xpc/xpc.h>

/* Private declarations from Chromium sandbox/mac/xpc.h. */
typedef struct _xpc_pipe_s *xpc_pipe_t;
void xpc_dictionary_set_mach_send(xpc_object_t, const char *, mach_port_t);
void xpc_dictionary_get_audit_token(xpc_object_t, audit_token_t *);
mach_port_t xpc_mach_send_get_right(xpc_object_t);
xpc_pipe_t xpc_pipe_create_from_port(mach_port_t, int);
int xpc_pipe_routine(xpc_pipe_t, xpc_object_t, xpc_object_t *);
int xpc_pipe_receive(mach_port_t, xpc_object_t *);
int xpc_pipe_routine_reply(xpc_object_t);

static const char allowed[] = "org.example.nov9.typed-canary.allowed";
enum { ping_id = -2047655148, slots = 3, deadline_ms = 3000, child_alarm = 15 };
enum { lookup_subsystem = 5, lookup_routine = 207 };

static bool failure(const char *stage, int64_t status) {
    fprintf(stderr, "%s status=%" PRId64 "\n", stage, status);
    return false;
}

static bool drop_send(mach_port_t port, const char *stage) {
    kern_return_t kr = mach_port_deallocate(mach_task_self(), port);
    return kr == KERN_SUCCESS || failure(stage, kr);
}

static bool check_registry(const char *stage) {
    mach_port_array_t ports = NULL;
    mach_msg_type_number_t count = 0;
    kern_return_t kr = mach_ports_lookup(mach_task_self(), &ports, &count);
    if (kr != KERN_SUCCESS) {
        return failure(stage, kr);
    }
    bool clean = ports != NULL && count == slots;
    if (ports != NULL) {
        for (mach_msg_type_number_t i = 0; i < count; ++i) {
            clean &= ports[i] == MACH_PORT_NULL;
            if (MACH_PORT_VALID(ports[i])) {
                drop_send(ports[i], "registry-drop");
            }
        }
        if (count != 0) {
            kr = vm_deallocate(mach_task_self(), (vm_address_t)ports, count * sizeof(*ports));
            if (kr != KERN_SUCCESS) {
                return failure("registry-free", kr);
            }
        }
    }
    return clean || failure(stage, count);
}

static bool check_right(mach_port_t port, mach_port_type_t expected, const char *stage) {
    mach_port_type_t type = 0;
    kern_return_t kr = mach_port_type(mach_task_self(), port, &type);
    const mach_port_type_t rights = MACH_PORT_TYPE_SEND | MACH_PORT_TYPE_RECEIVE |
                                   MACH_PORT_TYPE_SEND_ONCE | MACH_PORT_TYPE_DEAD_NAME;
    return (kr == KERN_SUCCESS && (type & rights) == expected) ||
           failure(stage, kr == KERN_SUCCESS ? (int64_t)type : kr);
}

static bool read_bootstrap(mach_port_t *port, const char *stage) {
    kern_return_t kr = task_get_bootstrap_port(mach_task_self(), port);
    return (kr == KERN_SUCCESS && MACH_PORT_VALID(*port)) || failure(stage, kr);
}

static bool make_port(mach_port_t *port, const char *stage) {
    kern_return_t kr = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, port);
    if (kr == KERN_SUCCESS) {
        kr = mach_port_insert_right(mach_task_self(), *port, *port, MACH_MSG_TYPE_MAKE_SEND);
    }
    return kr == KERN_SUCCESS || failure(stage, kr);
}

static bool send_ping(mach_port_t service) {
    mach_msg_header_t msg = {
        .msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, 0),
        .msgh_size = sizeof(msg),
        .msgh_remote_port = service,
        .msgh_id = ping_id,
    };
    mach_msg_return_t kr = mach_msg(&msg, MACH_SEND_MSG | MACH_SEND_TIMEOUT,
                                    sizeof(msg), 0, MACH_PORT_NULL, deadline_ms, MACH_PORT_NULL);
    return kr == MACH_MSG_SUCCESS || failure("child-ping-send", kr);
}

static bool is_type(xpc_object_t value, xpc_type_t type) {
    return value != NULL && xpc_get_type(value) == type;
}

static bool send_gate(int fd, char byte, const char *stage) {
    ssize_t n = write(fd, &byte, 1);
    return n == 1 || failure(stage, n < 0 ? errno : EIO);
}

static bool receive_gate(int fd, char expected, const char *stage) {
    struct pollfd gate = {.fd = fd, .events = POLLIN};
    int rc = poll(&gate, 1, deadline_ms);
    if (rc <= 0) {
        return failure(stage, rc == 0 ? ETIMEDOUT : errno);
    }
    if (!(gate.revents & POLLIN)) {
        return failure(stage, EPIPE);
    }
    char byte;
    ssize_t n = read(fd, &byte, 1);
    return (n == 1 && byte == expected) || failure(stage, n < 0 ? errno : EPROTO);
}

static bool child_typed(mach_port_t replacement) {
    xpc_pipe_t pipe = xpc_pipe_create_from_port(replacement, 0);
    if (pipe == NULL) {
        return failure("child-pipe-create", 0);
    }
    xpc_object_t request = xpc_dictionary_create(NULL, NULL, 0);
    if (request == NULL) {
        xpc_release((xpc_object_t)pipe);
        return failure("child-request-create", 0);
    }
    xpc_dictionary_set_string(request, "name", allowed);
    xpc_dictionary_set_uint64(request, "subsystem", lookup_subsystem);
    xpc_dictionary_set_uint64(request, "routine", lookup_routine);

    xpc_object_t reply = NULL;
    fputs("child-typed-routine entered\n", stderr);
    int rc = xpc_pipe_routine(pipe, request, &reply);
    bool ok = false;
    if (rc != 0) {
        failure("child-typed-reply-status", rc);
    } else if (!is_type(reply, XPC_TYPE_DICTIONARY)) {
        failure("child-typed-reply-dictionary", 0);
    } else {
        xpc_object_t field = xpc_dictionary_get_value(reply, "port");
        mach_port_t service = field == NULL ? MACH_PORT_NULL : xpc_mach_send_get_right(field);
        // The reply owns this right until after the ping.
        if (!MACH_PORT_VALID(service)) {
            failure("child-typed-reply-port", 0);
        } else {
            ok = check_right(service, MACH_PORT_TYPE_SEND, "child-typed-reply-right") &&
                 send_ping(service);
        }
    }

    if (reply != NULL) {
        xpc_release(reply);
    }
    xpc_release(request);
    xpc_release((xpc_object_t)pipe);
    return ok;
}

static bool child_named(int fd) {
    fputs("child-named-create entered\n", stderr);
    xpc_connection_t connection = xpc_connection_create_mach_service(allowed, NULL, 0);
    if (connection == NULL) {
        return failure("child-named-create", 0);
    }
    xpc_connection_set_event_handler(connection, ^(xpc_object_t event) {
        (void)event;
    });
    xpc_connection_resume(connection);
    fputs("child-named-resumed\n", stderr);

    // The observer holds its endpoint open until it has joined the child.
    char byte;
    ssize_t n = read(fd, &byte, 1);
    xpc_connection_cancel(connection);
    xpc_release(connection);
    return n == 0 || failure("child-named-settlement", n < 0 ? errno : EPROTO);
}

static bool run_child(bool named, int fd) {
    mach_port_t replacement = MACH_PORT_NULL;
    if (!read_bootstrap(&replacement, "post-exec-bootstrap")) {
        return false;
    }
    bool ok = check_registry("post-exec-registry") &&
              check_right(replacement, MACH_PORT_TYPE_SEND, "post-exec-bootstrap-right") &&
              send_gate(fd, 'R', "child-ready-send") &&
              receive_gate(fd, 'G', "child-go-receive") &&
              (named ? child_named(fd) : child_typed(replacement));
    return drop_send(replacement, "child-bootstrap-drop") && ok;
}

static _Noreturn void exec_child(const char *binary, bool named, int fd) {
    alarm(child_alarm);
    kern_return_t kr = mach_ports_register(mach_task_self(), NULL, 0);
    if (kr != KERN_SUCCESS) {
        failure("pre-exec-registry-clear", kr);
        _exit(1);
    }
    if (!check_registry("pre-exec-registry")) {
        _exit(1);
    }
    char fd_arg[32];
    snprintf(fd_arg, sizeof(fd_arg), "%d", fd);
    execl(binary, binary, named ? "--child-named" : "--child-typed", fd_arg, (char *)NULL);
    failure("child-exec", errno);
    _exit(1);
}

static int64_t monotonic_ms(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        failure("clock", errno);
        return -1;
    }
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static bool wait_step(int64_t start, const char *stage) {
    int64_t now = monotonic_ms();
    if (start < 0 || now < 0) {
        return false;
    }
    if (now - start >= deadline_ms) {
        return failure(stage, ETIMEDOUT);
    }
    const struct timespec pause = {.tv_nsec = 10000000};
    nanosleep(&pause, NULL);
    return true;
}

static bool queued(mach_port_t port, mach_port_msgcount_t *messages, const char *stage) {
    mach_port_status_t status;
    mach_msg_type_number_t count = MACH_PORT_RECEIVE_STATUS_COUNT;
    kern_return_t kr = mach_port_get_attributes(mach_task_self(), port, MACH_PORT_RECEIVE_STATUS,
                                                (mach_port_info_t)&status, &count);
    if (kr != KERN_SUCCESS || count != MACH_PORT_RECEIVE_STATUS_COUNT) {
        return failure(stage, kr == KERN_SUCCESS ? (int64_t)count : kr);
    }
    *messages = status.mps_msgcount;
    return true;
}

static bool from_child(audit_token_t token, pid_t child, uid_t uid) {
    return audit_token_to_pid(token) == child && audit_token_to_ruid(token) == uid &&
           audit_token_to_euid(token) == uid;
}

static bool wait_for_request(mach_port_t bootstrap) {
    const int64_t start = monotonic_ms();
    mach_port_msgcount_t messages;
    do {
        if (!queued(bootstrap, &messages, "request-queue-status")) {
            return false;
        }
    } while (messages == 0 && wait_step(start, "request-queue-timeout"));
    return messages != 0;
}

static bool reply_lookup(xpc_object_t request, mach_port_t service) {
    xpc_object_t subsystem = xpc_dictionary_get_value(request, "subsystem");
    xpc_object_t routine = xpc_dictionary_get_value(request, "routine");
    xpc_object_t name = xpc_dictionary_get_value(request, "name");
    if (!is_type(subsystem, XPC_TYPE_UINT64) || !is_type(routine, XPC_TYPE_UINT64) ||
        !is_type(name, XPC_TYPE_STRING)) {
        return failure("xpc-lookup-schema", 0);
    }
    if (xpc_uint64_get_value(subsystem) != lookup_subsystem ||
        xpc_uint64_get_value(routine) != lookup_routine) {
        return failure("xpc-lookup-operation", 0);
    }
    if (xpc_string_get_length(name) != sizeof(allowed) - 1 ||
        memcmp(xpc_string_get_string_ptr(name), allowed, sizeof(allowed) - 1) != 0) {
        return failure("xpc-lookup-name", 0);
    }
    xpc_object_t reply = xpc_dictionary_create_reply(request);
    if (reply == NULL) {
        return failure("xpc-reply-create", 0);
    }
    xpc_dictionary_set_mach_send(reply, "port", service);
    fputs("xpc-reply entered\n", stderr);
    int rc = xpc_pipe_routine_reply(reply);
    xpc_release(reply);
    return rc == 0 || failure("xpc-reply-send", rc);
}

static bool receive_typed(mach_port_t bootstrap, pid_t child, uid_t uid, mach_port_t service) {
    // The observer is the only reader of this receive right.
    xpc_object_t request = NULL;
    fputs("xpc-receive entered\n", stderr);
    int rc = xpc_pipe_receive(bootstrap, &request);
    bool ok = false;
    if (rc != 0) {
        failure("xpc-receive-status", rc);
    } else if (!is_type(request, XPC_TYPE_DICTIONARY)) {
        failure("xpc-dictionary-type", 0);
    } else {
        audit_token_t token = {0};
        xpc_dictionary_get_audit_token(request, &token);
        if (!from_child(token, child, uid)) {
            failure("xpc-audit", 0);
        } else {
            fputs("xpc-dictionary authenticated\n", stderr);
            ok = service == MACH_PORT_NULL || reply_lookup(request, service);
        }
    }
    if (request != NULL) {
        xpc_release(request);
    }
    return ok;
}

static bool receive_raw(mach_port_t bootstrap, pid_t child, uid_t uid) {
    union {
        mach_msg_header_t header;
        mach_msg_audit_trailer_t alignment;
        unsigned char bytes[8192];
    } msg = {0};
    mach_msg_header_t *header = &msg.header;
    mach_msg_return_t kr = mach_msg(header, MACH_RCV_MSG | MACH_RCV_TIMEOUT | MACH_RCV_LARGE |
                                    MACH_RCV_TRAILER_TYPE(MACH_MSG_TRAILER_FORMAT_0) |
                                    MACH_RCV_TRAILER_ELEMENTS(MACH_RCV_TRAILER_AUDIT),
                                    0, sizeof(msg), bootstrap, deadline_ms, MACH_PORT_NULL);
    if (kr != MACH_MSG_SUCCESS) {
        return failure("raw-receive", kr);
    }

    bool ok = false;
    size_t size = header->msgh_size;
    if (size < sizeof(*header) || size > sizeof(msg) || header->msgh_local_port != bootstrap) {
        failure("raw-header", 0);
        goto cleanup;
    }

    size_t aligned_size = (size + 3u) & ~(size_t)3u;
    mach_msg_audit_trailer_t trailer;
    if (aligned_size > sizeof(msg) - sizeof(trailer)) {
        failure("raw-audit-bounds", 0);
        goto cleanup;
    }
    memcpy(&trailer, msg.bytes + aligned_size, sizeof(trailer));
    if (trailer.msgh_trailer_type != MACH_MSG_TRAILER_FORMAT_0 ||
        trailer.msgh_trailer_size < sizeof(trailer) ||
        trailer.msgh_trailer_size > sizeof(msg) - aligned_size ||
        !from_child(trailer.msgh_audit, child, uid)) {
        failure("raw-audit", 0);
        goto cleanup;
    }

    bool complex = (header->msgh_bits & MACH_MSGH_BITS_COMPLEX) != 0;
    mach_msg_type_number_t descriptors = 0;
    if (complex) {
        mach_msg_body_t body;
        if (size - sizeof(*header) < sizeof(body)) {
            failure("raw-body", 0);
            goto cleanup;
        }
        memcpy(&body, msg.bytes + sizeof(*header), sizeof(body));
        descriptors = body.msgh_descriptor_count;
    }
    printf("raw class=%s id=%" PRId32 " descriptors=%" PRIu32 "\n",
           complex ? "complex" : "simple", (int32_t)header->msgh_id, (uint32_t)descriptors);
    ok = true;

cleanup:
    mach_msg_destroy(header);
    return ok;
}

static bool receive_ping(mach_port_t service, pid_t child, uid_t uid) {
    union {
        mach_msg_header_t header;
        mach_msg_audit_trailer_t alignment;
        unsigned char bytes[1024];
    } msg = {0};
    mach_msg_header_t *header = &msg.header;
    mach_msg_return_t kr = mach_msg(header, MACH_RCV_MSG | MACH_RCV_TIMEOUT | MACH_RCV_LARGE |
                                    MACH_RCV_TRAILER_TYPE(MACH_MSG_TRAILER_FORMAT_0) |
                                    MACH_RCV_TRAILER_ELEMENTS(MACH_RCV_TRAILER_AUDIT),
                                    0, sizeof(msg), service, deadline_ms, MACH_PORT_NULL);
    if (kr != MACH_MSG_SUCCESS) {
        return failure("ping-receive", kr);
    }
    bool ok = false;
    if (header->msgh_size != sizeof(*header) || header->msgh_id != ping_id ||
        (header->msgh_bits & MACH_MSGH_BITS_COMPLEX) ||
        header->msgh_local_port != service ||
        MACH_MSGH_BITS_LOCAL(header->msgh_bits) != MACH_MSG_TYPE_PORT_SEND ||
        header->msgh_remote_port != MACH_PORT_NULL || MACH_MSGH_BITS_REMOTE(header->msgh_bits) != 0 ||
        header->msgh_voucher_port != MACH_PORT_NULL || MACH_MSGH_BITS_VOUCHER(header->msgh_bits) != 0) {
        failure("ping-shape", header->msgh_id);
    } else {
        mach_msg_audit_trailer_t trailer;
        memcpy(&trailer, msg.bytes + sizeof(*header), sizeof(trailer));
        ok = (trailer.msgh_trailer_type == MACH_MSG_TRAILER_FORMAT_0 &&
              trailer.msgh_trailer_size == sizeof(trailer) &&
              from_child(trailer.msgh_audit, child, uid)) || failure("ping-audit", 0);
    }
    mach_msg_destroy(header);
    if (ok) {
        fputs("child-ping authenticated\n", stderr);
    }
    return ok;
}

static bool finish_child(pid_t *child, bool require_clean_exit) {
    const int64_t start = monotonic_ms();
    do {
        int status;
        pid_t result = waitpid(*child, &status, WNOHANG);
        if (result == *child) {
            *child = -1;
            return !require_clean_exit || (WIFEXITED(status) && WEXITSTATUS(status) == 0) ||
                   failure("child-exit", status);
        }
        if (result < 0 && errno != EINTR) {
            int error = errno;
            if (error == ECHILD) {
                *child = -1;
            }
            return failure("child-wait", error);
        }
    } while (wait_step(start, require_clean_exit ? "child-exit-timeout" : "child-reap-timeout"));
    return false;
}

static bool empty(mach_port_t port, const char *stage) {
    mach_port_msgcount_t messages;
    return queued(port, &messages, stage) && (messages == 0 || failure(stage, messages));
}

static bool destroy(mach_port_t port, const char *stage) {
    if (!MACH_PORT_VALID(port)) {
        return true;
    }
    kern_return_t kr = mach_port_destroy(mach_task_self(), port);
    return kr == KERN_SUCCESS || failure(stage, kr);
}

static bool observe(const char *binary, uid_t uid, bool named, bool raw) {
    mach_port_t bootstrap = MACH_PORT_NULL, service = MACH_PORT_NULL, readback = MACH_PORT_NULL;
    pid_t child = -1;
    int gate[2] = {-1, -1};
    bool ok = false;
    if (!make_port(&bootstrap, "bootstrap-port")) {
        goto cleanup;
    }
    kern_return_t kr = task_set_bootstrap_port(mach_task_self(), bootstrap);
    if (kr != KERN_SUCCESS) {
        failure("bootstrap-install", kr);
        goto cleanup;
    }
    if (!read_bootstrap(&readback, "pre-fork-bootstrap")) {
        goto cleanup;
    }
    bool same_port = readback == bootstrap;
    bool dropped = drop_send(readback, "pre-fork-bootstrap-drop");
    if (!same_port || !dropped ||
        !check_right(bootstrap, MACH_PORT_TYPE_RECEIVE | MACH_PORT_TYPE_SEND, "pre-fork-bootstrap-right")) {
        if (!same_port) {
            failure("pre-fork-bootstrap-identity", 0);
        }
        goto cleanup;
    }
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, gate) != 0) {
        failure("gate-socketpair", errno);
        goto cleanup;
    }
    child = fork();
    if (child == 0) {
        close(gate[0]);
        exec_child(binary, named, gate[1]);
    }
    if (child < 0) {
        failure("child-fork", errno);
        goto cleanup;
    }
    close(gate[1]);
    gate[1] = -1;

    if (!receive_gate(gate[0], 'R', "parent-ready-receive") ||
        !empty(bootstrap, "pre-go-queue") ||
        !send_gate(gate[0], 'G', "parent-go-send") ||
        !wait_for_request(bootstrap)) {
        goto cleanup;
    }

    if (raw) {
        ok = receive_raw(bootstrap, child, uid);
    } else if (named) {
        ok = receive_typed(bootstrap, child, uid, MACH_PORT_NULL);
    } else {
        ok = make_port(&service, "service-port") &&
             receive_typed(bootstrap, child, uid, service) &&
             receive_ping(service, child, uid) &&
             finish_child(&child, true) &&
             empty(bootstrap, "extra-lookup") && empty(service, "extra-ping");
    }

cleanup:
    if (child > 0) {
        if (kill(child, SIGKILL) != 0 && errno != ESRCH) {
            failure("child-kill", errno);
            ok = false;
        }
        ok = finish_child(&child, false) && ok;
    }
    if (gate[0] >= 0) {
        close(gate[0]);
    }
    if (gate[1] >= 0) {
        close(gate[1]);
    }
    bool service_destroyed = destroy(service, "service-destroy");
    bool bootstrap_destroyed = destroy(bootstrap, "bootstrap-destroy");
    return ok && service_destroyed && bootstrap_destroyed;
}

int main(int argc, char **argv) {
    uid_t uid = getuid();
    if (uid == 0 || geteuid() != uid || argc < 1 || argv[0][0] != '/') {
        failure("unprivileged-absolute-launch", 0);
        return 1;
    }
    if (argc == 3 && (strcmp(argv[1], "--child-typed") == 0 ||
                      strcmp(argv[1], "--child-named") == 0)) {
        char *end;
        errno = 0;
        long fd = strtol(argv[2], &end, 10);
        if (errno != 0 || *end != '\0' || fd < 3 || fd > INT_MAX) {
            failure("child-gate-fd", 0);
            return 1;
        }
        return run_child(strcmp(argv[1], "--child-named") == 0, (int)fd) ? 0 : 1;
    }
    if (argc != 2) {
        failure("arguments", argc);
        return 1;
    }
    bool named = strcmp(argv[1], "named") == 0 || strcmp(argv[1], "named-raw") == 0;
    bool raw = strcmp(argv[1], "typed-raw") == 0 || strcmp(argv[1], "named-raw") == 0;
    if (!named && !raw && strcmp(argv[1], "typed") != 0) {
        failure("mode", 0);
        return 1;
    }
    if (signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
        failure("sigpipe-ignore", errno);
        return 1;
    }
    if (!observe(argv[0], uid, named, raw)) {
        return 1;
    }
    if (!raw) {
        printf("%s pass\n", argv[1]);
    }
    return 0;
}
