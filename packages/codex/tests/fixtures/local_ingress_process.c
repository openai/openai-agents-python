#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <stdlib.h>
#include <unistd.h>

#ifdef INGRESS_SECCOMP
// Loaded in a dedicated CPython subprocess after Python startup, before the
// first native import. This denies the real syscall, not a native SDK method.
#include <stddef.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sys/prctl.h>
#include <sys/syscall.h>

int deny_getrandom(int error) {
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_getrandom, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | (unsigned int)error),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog program = { .len = sizeof(filter) / sizeof(filter[0]), .filter = filter };
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0))
        return -1;
    return prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program);
}
#else
#include <assert.h>
#include <pthread.h>
#include <sys/wait.h>
#include <signal.h>

// Keep the fixture executable's own environment and DNS untouched. The actual
// source's getters are exercised explicitly, with its actual lock and list.
#define getenv sdk_test_getenv
#define getaddrinfo sdk_test_getaddrinfo
#include INGRESS_SOURCE
#undef getenv
#undef getaddrinfo

// Fork/entropy cases do not parse any proxy variables. Abort if the test
// accidentally starts relying on a mock of the Rust classifier.
int sdk_ingress_no_proxy_kind(const char *value) { (void)value; abort(); }
int sdk_ingress_no_proxy_all(const char *value) { (void)value; abort(); }

static int held[2], handoff[2];
static int forking;
static int deny_entropy;
static pthread_t fork_thread;
static int init_window;
static int deny_registration;
static int registered[2], finish_init[2];
static pid_t initializer_parent;

int __real_pthread_mutex_lock(pthread_mutex_t *);
ssize_t __real_getrandom(void *, size_t, unsigned int);
int __real_pthread_atfork(void (*)(void), void (*)(void), void (*)(void));

ssize_t __wrap_getrandom(void *buffer, size_t size, unsigned int flags) {
    if (deny_entropy) {
        errno = EPERM;
        return -1;
    }
    return __real_getrandom(buffer, size, flags);
}

static void send_byte(int fd, char value) { assert(write(fd, &value, 1) == 1); }
static char read_byte(int fd) {
    char value;
    assert(read(fd, &value, 1) == 1);
    return value;
}

int __wrap_pthread_atfork(void (*prepare)(void), void (*parent)(void), void (*child)(void)) {
    if (deny_registration)
        return ENOMEM;
    int result = __real_pthread_atfork(prepare, parent, child);
    if (init_window && !result) {
        dprintf(1, "registered in %ld\n", (long)getpid());
        if (getpid() == initializer_parent) {
            // Registration is visible to fork but resolve_os/real_once has
            // not returned. The child must retry that abandoned initializer.
            send_byte(registered[1], 'R');
            assert(read_byte(finish_init[0]) == 'F');
        }
    }
    return result;
}

int __wrap_pthread_mutex_lock(pthread_mutex_t *lock) {
    if (forking && lock == &leases_lock && pthread_equal(pthread_self(), fork_thread))
        send_byte(handoff[1], 'L');
    return __real_pthread_mutex_lock(lock);
}

static void parent_forked(void) {
    // Old source has no prepare lock: release holder only AFTER fork copied it.
    // With corrected source the holder already got L from prepare.
    send_byte(handoff[1], 'P');
    forking = 0;
}
static void child_forked(void) { forking = 0; }

static void *holder(void *unused) {
    (void)unused;
    pthread_mutex_lock(&leases_lock);
    send_byte(held[1], 'H');
    char when = read_byte(handoff[0]);
    assert(when == 'L' || when == 'P');
    pthread_mutex_unlock(&leases_lock);
    return NULL;
}

static void resolves(const char *name, int expected) {
    struct addrinfo *info = NULL;
    assert(sdk_test_getaddrinfo(name, "80", NULL, &info) == expected);
    if (info)
        freeaddrinfo(info);
}

static void *first_initialization(void *unused) {
    (void)unused;
    char name[NAME_SIZE];
    void *lease = sdk_ingress_acquire(name, sizeof(name));
    assert(lease);
    resolves(name, 0);
    sdk_ingress_release(lease);
    return NULL;
}

static void fork_during_initialization(void) {
    init_window = 1;
    initializer_parent = getpid();
    assert(pipe(registered) == 0 && pipe(finish_init) == 0);
    pthread_t initializer;
    assert(pthread_create(&initializer, NULL, first_initialization, NULL) == 0);
    assert(read_byte(registered[0]) == 'R');
    pid_t child = fork();
    assert(child >= 0);
    if (!child) {
        alarm(4); // Deadlock bound only. The pipes select the registration window.
        char name[NAME_SIZE];
        void *lease = sdk_ingress_acquire(name, sizeof(name));
        assert(lease);
        resolves(name, 0);
        dprintf(1, "child initialization complete; attempting next fork\n");
        pid_t grandchild = fork();
        assert(grandchild >= 0);
        if (!grandchild) {
            resolves(name, 0);
            sdk_ingress_release(lease);
            resolves(name, EAI_NONAME);
            _exit(0);
        }
        int status;
        assert(waitpid(grandchild, &status, 0) == grandchild);
        assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
        resolves(name, 0);
        sdk_ingress_release(lease);
        _exit(0);
    }
    send_byte(finish_init[1], 'F');
    assert(pthread_join(initializer, NULL) == 0);
    int status;
    assert(waitpid(child, &status, 0) == child);
    if (WIFSIGNALED(status))
        dprintf(1, "child signal %d\n", WTERMSIG(status));
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
}

int main(int argc, char **argv) {
    assert(argc == 2);
    char name[NAME_SIZE], sibling[NAME_SIZE], later[NAME_SIZE];
    if (strcmp(argv[1], "init-fork") == 0) {
        fork_during_initialization();
        return 0;
    }
    if (strcmp(argv[1], "registration-error") == 0) {
        deny_registration = 1;
        assert(setenv("SDK_INGRESS_TEST_KEY", "original", 1) == 0);
        errno = EBUSY;
        assert(strcmp(sdk_test_getenv("SDK_INGRESS_TEST_KEY"), "original") == 0);
        assert(errno == EBUSY);
        assert(setenv("NO_PROXY", "original.example", 1) == 0);
        errno = EDOM;
        assert(strcmp(sdk_test_getenv("NO_PROXY"), "original.example") == 0);
        assert(errno == EDOM);
        resolves("127.0.0.1", 0);
        assert(sdk_ingress_init() == ENOMEM);
        assert(sdk_ingress_acquire(name, sizeof(name)) == NULL);
        return 0;
    }
    if (strcmp(argv[1], "entropy") == 0) {
        deny_entropy = 1;
        sdk_ingress_init();
        assert(sdk_ingress_acquire(name, sizeof(name)) == NULL);
        // A failure never invents an empty suffix or hijacks unrelated DNS/env.
        assert(setenv("NO_PROXY", "original.example", 1) == 0);
        errno = EBUSY;
        assert(strcmp(sdk_test_getenv("NO_PROXY"), "original.example") == 0);
        assert(errno == EBUSY);
        resolves("127.0.0.1", 0);
        return 0;
    }
    assert(strcmp(argv[1], "fork") == 0);
    void *one = sdk_ingress_acquire(name, sizeof(name));
    void *two = sdk_ingress_acquire(sibling, sizeof(sibling));
    assert(one && two);
    resolves(name, 0);
    assert(pipe(held) == 0 && pipe(handoff) == 0);
    assert(pthread_atfork(NULL, parent_forked, child_forked) == 0);
    fork_thread = pthread_self();
    pthread_t worker;
    assert(pthread_create(&worker, NULL, holder, NULL) == 0);
    assert(read_byte(held[0]) == 'H');
    forking = 1;
    pid_t pid = fork();
    assert(pid >= 0);
    if (!pid) {
        // Alarm is only a deadlock bound; ordering is controlled above by pipes.
        alarm(4);
        resolves(name, 0);
        sdk_ingress_release(one);
        resolves(name, EAI_NONAME);
        resolves(sibling, 0);
        void *fresh = sdk_ingress_acquire(later, sizeof(later));
        assert(fresh && strcmp(later, name) && strcmp(later, sibling));
        resolves(later, 0);
        sdk_ingress_release(fresh);
        sdk_ingress_release(two);
        _exit(0);
    }
    assert(pthread_join(worker, NULL) == 0);
    int status;
    assert(waitpid(pid, &status, 0) == pid);
    // Child release cannot change parent's still-live nodes.
    resolves(name, 0);
    resolves(sibling, 0);
    sdk_ingress_release(one);
    resolves(name, EAI_NONAME);
    resolves(sibling, 0);
    sdk_ingress_release(two);
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
    return 0;
}
#endif
