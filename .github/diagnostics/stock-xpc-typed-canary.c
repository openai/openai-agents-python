#include <bsm/libbsm.h>
#include <errno.h>
#include <inttypes.h>
#include <mach/mach.h>
#include <mach/task_special_ports.h>
#include <servers/bootstrap.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <xpc/xpc.h>

/* Private declarations from Chromium sandbox/mac/xpc.h. */
void xpc_dictionary_set_mach_send(xpc_object_t, const char *, mach_port_t);
void xpc_dictionary_get_audit_token(xpc_object_t, audit_token_t *);
int xpc_pipe_receive(mach_port_t, xpc_object_t *);
int xpc_pipe_routine_reply(xpc_object_t);

static const char allowed[] = "org.example.nov9.typed-canary.allowed";
static const char denied[] = "org.example.nov9.typed-canary.unknown";
enum { ping_id = -2047655148, slots = 3, deadline_ms = 3000, child_alarm = 15 };

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
                                    sizeof(msg), 0, MACH_PORT_NULL, 500, MACH_PORT_NULL);
    return kr == MACH_MSG_SUCCESS || failure("child-ping-send", kr);
}

static bool run_child(void) {
    mach_port_t replacement = MACH_PORT_NULL;
    mach_port_t service = MACH_PORT_NULL;
    if (!read_bootstrap(&replacement, "post-exec-bootstrap")) {
        return false;
    }
    bool ok = false;
    if (!check_registry("post-exec-registry") ||
        !check_right(replacement, MACH_PORT_TYPE_SEND, "post-exec-bootstrap-right")) {
        goto cleanup;
    }
    fputs("child-allow-lookup entered\n", stderr);
    kern_return_t kr = bootstrap_look_up(replacement, allowed, &service);
    if (kr != KERN_SUCCESS || !MACH_PORT_VALID(service)) {
        failure("child-allow-result", kr);
        goto cleanup;
    }
    if (!check_right(service, MACH_PORT_TYPE_SEND, "child-allow-right") || !send_ping(service)) {
        goto cleanup;
    }
    bool dropped = drop_send(service, "child-service-drop");
    service = MACH_PORT_NULL;
    if (!dropped) {
        goto cleanup;
    }
    fputs("child-deny-lookup entered\n", stderr);
    kr = bootstrap_look_up(replacement, denied, &service);
    ok = (kr == BOOTSTRAP_UNKNOWN_SERVICE && service == MACH_PORT_NULL) ||
         failure(service == MACH_PORT_NULL ? "child-deny-result" : "child-deny-right", kr);

cleanup:
    if (MACH_PORT_VALID(service)) {
        drop_send(service, "child-unexpected-right-drop");
    }
    return drop_send(replacement, "child-bootstrap-drop") && ok;
}

static _Noreturn void exec_child(const char *binary) {
    alarm(child_alarm);
    kern_return_t kr = mach_ports_register(mach_task_self(), NULL, 0);
    if (kr != KERN_SUCCESS) {
        failure("pre-exec-registry-clear", kr);
        _exit(1);
    }
    if (!check_registry("pre-exec-registry")) {
        _exit(1);
    }
    execl(binary, binary, "--child", (char *)NULL);
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

static bool is_type(xpc_object_t value, xpc_type_t type) {
    return value != NULL && xpc_get_type(value) == type;
}

static bool lookup_failure(bool allow, const char *stage, int status) {
    fprintf(stderr, "%s-%s status=%d\n", allow ? "allow" : "deny", stage, status);
    return false;
}

static bool reply_lookup(mach_port_t bootstrap, pid_t child, uid_t uid, mach_port_t service,
                         bool allow) {
    const char *stage = allow ? "allow-queue" : "deny-queue";
    const int64_t start = monotonic_ms();
    mach_port_msgcount_t messages;
    do {
        if (!queued(bootstrap, &messages, stage)) {
            return false;
        }
    } while (messages == 0 && wait_step(start, stage));
    if (messages == 0) {
        return false;
    }

    // The observer is the only reader of this receive right.
    xpc_object_t request = NULL;
    fprintf(stderr, "%s-xpc-receive entered\n", allow ? "allow" : "deny");
    int rc = xpc_pipe_receive(bootstrap, &request);
    bool ok = false;
    if (rc != 0 || request == NULL) {
        lookup_failure(allow, "xpc-receive", rc);
        goto cleanup;
    }
    if (!is_type(request, XPC_TYPE_DICTIONARY)) {
        lookup_failure(allow, "xpc-dictionary-type", 0);
        goto cleanup;
    }
    audit_token_t token = {0};
    xpc_dictionary_get_audit_token(request, &token);
    if (!from_child(token, child, uid)) {
        lookup_failure(allow, "xpc-audit", 0);
        goto cleanup;
    }
    xpc_object_t subsystem_field = xpc_dictionary_get_value(request, "subsystem");
    xpc_object_t routine_field = xpc_dictionary_get_value(request, "routine");
    xpc_object_t name_field = xpc_dictionary_get_value(request, "name");
    if (!is_type(subsystem_field, XPC_TYPE_UINT64)) {
        lookup_failure(allow, "xpc-subsystem-type", 0);
        goto cleanup;
    }
    if (!is_type(routine_field, XPC_TYPE_UINT64)) {
        lookup_failure(allow, "xpc-routine-type", 0);
        goto cleanup;
    }
    if (!is_type(name_field, XPC_TYPE_STRING)) {
        lookup_failure(allow, "xpc-name-type", 0);
        goto cleanup;
    }
    uint64_t subsystem = xpc_uint64_get_value(subsystem_field);
    uint64_t routine = xpc_uint64_get_value(routine_field);
    if (subsystem != 5 || routine != 207) {
        fprintf(stderr, "%s-xpc-lookup-op subsystem=%" PRIu64 " routine=%" PRIu64 "\n",
                allow ? "allow" : "deny", subsystem, routine);
        goto cleanup;
    }
    const char *expected = allow ? allowed : denied;
    size_t expected_length = strlen(expected);
    if (xpc_string_get_length(name_field) != expected_length ||
        memcmp(xpc_string_get_string_ptr(name_field), expected, expected_length) != 0) {
        lookup_failure(allow, "xpc-name", 0);
        goto cleanup;
    }
    xpc_object_t reply = xpc_dictionary_create_reply(request);
    if (reply == NULL) {
        lookup_failure(allow, "xpc-create-reply", 0);
        goto cleanup;
    }
    if (allow) {
        xpc_dictionary_set_mach_send(reply, "port", service);
    } else {
        xpc_dictionary_set_int64(reply, "error", BOOTSTRAP_UNKNOWN_SERVICE);
    }
    fprintf(stderr, "%s-xpc-reply entered\n", allow ? "allow" : "deny");
    rc = xpc_pipe_routine_reply(reply);
    xpc_release(reply);
    ok = rc == 0 || lookup_failure(allow, "xpc-send-reply", rc);

cleanup:
    if (request != NULL) {
        xpc_release(request);
    }
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

static bool finish_child(pid_t *child) {
    const int64_t start = monotonic_ms();
    do {
        int status;
        pid_t result = waitpid(*child, &status, WNOHANG);
        if (result == *child) {
            *child = -1;
            return (WIFEXITED(status) && WEXITSTATUS(status) == 0) || failure("child-exit", status);
        }
        if (result < 0 && errno != EINTR) {
            int error = errno;
            if (error == ECHILD) {
                *child = -1;
            }
            return failure("child-wait", error);
        }
    } while (wait_step(start, "child-exit-timeout"));
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

static bool observe(const char *binary, uid_t uid) {
    mach_port_t bootstrap = MACH_PORT_NULL, service = MACH_PORT_NULL, readback = MACH_PORT_NULL;
    pid_t child = -1;
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
    child = fork();
    if (child == 0) {
        exec_child(binary);
    }
    if (child < 0) {
        failure("child-fork", errno);
        goto cleanup;
    }
    ok = make_port(&service, "service-port") &&
         reply_lookup(bootstrap, child, uid, service, true) &&
         receive_ping(service, child, uid) &&
         reply_lookup(bootstrap, child, uid, service, false) &&
         finish_child(&child) && empty(bootstrap, "extra-lookup") && empty(service, "extra-ping");

cleanup:
    if (child > 0) {
        if (kill(child, SIGKILL) != 0 && errno != ESRCH) {
            failure("child-kill", errno);
        }
        while (waitpid(child, NULL, 0) < 0) {
            if (errno != EINTR) {
                failure("child-reap", errno);
                break;
            }
        }
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
    if (argc == 2 && strcmp(argv[1], "--child") == 0) {
        return run_child() ? 0 : 1;
    }
    if (argc != 1) {
        failure("arguments", argc);
        return 1;
    }
    if (!observe(argv[0], uid)) {
        return 1;
    }
    puts("typed-canary pass");
    return 0;
}
