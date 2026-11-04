#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <mach/mach.h>
#include <mach/task_info.h>
#include <mach/task_special_ports.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static volatile sig_atomic_t received;

static void note_signal(int signo)
{
    (void)signo;
    received = 1;
}

static int control(void)
{
    struct sigaction handler = {.sa_handler = note_signal, .sa_flags = SA_RESTART};
    sigemptyset(&handler.sa_mask);
    if (sigaction(SIGUSR1, &handler, NULL) != 0) {
        return 1;
    }
    alarm(45);
    audit_token_t token;
    mach_msg_type_number_t count = TASK_AUDIT_TOKEN_COUNT;
    if (task_info(mach_task_self(), TASK_AUDIT_TOKEN, (task_info_t)&token, &count) != KERN_SUCCESS ||
        count != TASK_AUDIT_TOKEN_COUNT ||
        write(STDOUT_FILENO, &token, sizeof(token)) != sizeof(token)) {
        return 1;
    }
    char command;
    ssize_t size;
    while ((size = read(STDIN_FILENO, &command, 1)) == 1) {
        if (command == 'c') {
            received = 0;
        } else if (command != 'p') {
            return 1;
        }
        const char answer = received ? '1' : '0';
        if (write(STDOUT_FILENO, &answer, 1) != 1) {
            return 1;
        }
    }
    return size == 0 ? 0 : 1;
}

static int try_signal(char **words)
{
    audit_token_t token;
    for (size_t index = 0; index < sizeof(token.val) / sizeof(token.val[0]); ++index) {
        char *end;
        errno = 0;
        unsigned long value = strtoul(words[index], &end, 10);
        if (errno || *end || end == words[index] || value > UINT32_MAX) {
            return EINVAL;
        }
        token.val[index] = (uint32_t)value;
    }
    void *lib = dlopen("/usr/lib/libproc.dylib", RTLD_NOW | RTLD_LOCAL);
    if (lib == NULL) {
        return ENOSYS;
    }
    int (*send)(audit_token_t *, int) = dlsym(lib, "proc_signal_with_audittoken");
    int result = send != NULL ? send(&token, SIGUSR1) : ENOSYS;
    dlclose(lib);
    return result;
}

static int try_read(const char *path)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        return errno;
    }
    char byte;
    int result = read(fd, &byte, 1) < 0 ? errno : 0;
    close(fd);
    return result;
}

static int try_append(const char *path)
{
    int fd = open(path, O_WRONLY | O_APPEND | O_CLOEXEC);
    if (fd < 0) {
        return errno;
    }
    int result = write(fd, "!", 1) < 0 ? errno : 0;
    close(fd);
    return result;
}

static int prepare_launchctl(const char *witness)
{
    mach_port_t bootstrap = MACH_PORT_NULL;
    kern_return_t kr = task_get_special_port(mach_task_self(), TASK_BOOTSTRAP_PORT, &bootstrap);
    if (kr != KERN_SUCCESS || !MACH_PORT_VALID(bootstrap)) {
        fprintf(stderr, "no inherited bootstrap port (Mach return=%d)\n", kr);
        return 125;
    }
    puts(witness);
    fflush(stdout);
    // Keep the task's inherited special port. Drop only our extra reference.
    mach_port_deallocate(mach_task_self(), bootstrap);
    alarm(4);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc == 2 && strcmp(argv[1], "control") == 0) {
        return control();
    }
    if (argc == 10 && strcmp(argv[1], "signal") == 0) {
        int result = try_signal(&argv[2]);
        printf("audit_signal_errno=%d\n", result);
        return result == 0 ? 0 : 1;
    }
    if (argc == 3 && strcmp(argv[1], "kickstart") == 0) {
        if (prepare_launchctl("kickstart_inherited_bootstrap=present") != 0) {
            return 125;
        }
        execl("/bin/launchctl", "/bin/launchctl", "kickstart", argv[2], (char *)NULL);
        fprintf(stderr, "launchctl kickstart exec failed: %s\n", strerror(errno));
        return 125;
    }
    if (argc != 13 || strcmp(argv[1], "probe") != 0) {
        return 125;
    }
    int plist_read = try_read(argv[3]);
    int proof = open("workspace-proof", O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (proof < 0 || write(proof, "ok\n", 3) != 3 || close(proof) != 0 || plist_read != 0) {
        fprintf(stderr, "workspace or plist precondition failed (plist errno=%d)\n", plist_read);
        return 125;
    }
    int read_error = try_read(argv[4]);
    int write_error = try_append(argv[4]);
    int signal_error = try_signal(&argv[5]);
    printf("nonce_read_errno=%d\nnonce_write_errno=%d\n"
           "audit_signal_errno=%d\n", read_error, write_error, signal_error);
    if (prepare_launchctl("inherited_bootstrap=present") != 0) {
        return 125;
    }
    execl("/bin/launchctl", "/bin/launchctl", "bootstrap", argv[2], argv[3], (char *)NULL);
    fprintf(stderr, "launchctl exec failed: %s\n", strerror(errno));
    return 125;
}
