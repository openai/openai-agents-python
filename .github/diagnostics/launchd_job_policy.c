#include <dlfcn.h>
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

#if __has_include(<sandbox/private.h>)
#include <sandbox/private.h>
#else
// Match the stock-SDK fallback in WebKit's Darwin SandboxSPI.h.
enum sandbox_filter_type {
    SANDBOX_FILTER_NONE,
    SANDBOX_FILTER_PATH,
};
#endif

typedef int (*SandboxCheck)(pid_t, const char *, enum sandbox_filter_type, ...);

static void print_check(SandboxCheck check, pid_t pid, const char *key,
                        const char *operation, enum sandbox_filter_type filter, const char *path) {
    errno = 0;
    int rc = path ? check(pid, operation, filter, path) : check(pid, operation, filter);
    int error = errno;
    printf("\"%s\":{\"rc\":%d,\"errno\":%d}", key, rc, error);
}

int main(int argc, char **argv) {
    if (argc != 6) {
        puts("JOB_POLICY_HELPER {\"helper_error\":\"invalid_arguments\"}");
        return 2;
    }

    pid_t pid = getpid();
    char pid_text[32];
    char cwd[PATH_MAX];
    const char *token = getenv("NOV9_JOB_POLICY_TOKEN");
    snprintf(pid_text, sizeof(pid_text), "%jd", (intmax_t)pid);
    printf("JOB_POLICY_HELPER {\"context\":{\"pid_match\":%d,\"cwd_match\":%d,"
           "\"uid\":%jd,\"euid\":%jd,\"token_match\":%d}",
           strcmp(pid_text, argv[1]) == 0,
           getcwd(cwd, sizeof(cwd)) != NULL && strcmp(cwd, argv[2]) == 0,
           (intmax_t)getuid(), (intmax_t)geteuid(), token != NULL && strcmp(token, argv[3]) == 0);

    SandboxCheck check = (SandboxCheck)dlsym(RTLD_DEFAULT, "sandbox_check");
    if (check == NULL) {
        puts(",\"helper_error\":\"sandbox_check_unavailable\"}");
        return 3;
    }

    // These are self-policy queries. No launchd request or file open is needed.
    fputs(",\"queries\":{", stdout);
    print_check(check, pid, "sandbox_status", NULL, SANDBOX_FILTER_NONE, NULL);
    putchar(',');
    print_check(check, pid, "job_creation_none", "job-creation", SANDBOX_FILTER_NONE, NULL);
    putchar(',');
    print_check(check, pid, "job_creation_path", "job-creation", SANDBOX_FILTER_PATH, "/usr/bin/touch");
    putchar(',');
    print_check(check, pid, "inside_write", "file-write-data", SANDBOX_FILTER_PATH, argv[4]);
    putchar(',');
    print_check(check, pid, "outside_write", "file-write-data", SANDBOX_FILTER_PATH, argv[5]);
    puts("}}");
    return 0;
}
