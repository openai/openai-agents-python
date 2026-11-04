#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

enum { lifetime = 30 };

static int connect_role(const char *path, char role)
{
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    if (strlen(path) >= sizeof(addr.sun_path)) {
        return -1;
    }
    strcpy(addr.sun_path, path);
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0 || connect(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0 ||
        write(fd, &role, 1) != 1) {
        perror("connect helper");
        return -1;
    }
    return fd;
}

static int park(int fd)
{
    char command;
    ssize_t count;
    while ((count = read(fd, &command, 1)) == 1) {
        if (command != 'p' || write(fd, &command, 1) != 1) {
            return 1;
        }
    }
    return count == 0 ? 0 : 1;
}

static _Noreturn void command_parent(const char *path, int fd, int gate[2])
{
    alarm(lifetime);
    close(fd);
    close(gate[1]);
    pid_t child = fork();
    if (child == 0) {
        alarm(lifetime);
        close(gate[0]);
        if (setsid() < 0) {
            _exit(1);
        }
        int detached = connect_role(path, 'd');
        _exit(detached < 0 ? 1 : park(detached));
    }
    // The tracked parent opens no socket and waits for the helper to release it.
    char unused;
    _exit(child > 0 && read(gate[0], &unused, 1) == 0 ? 0 : 1);
}

int main(int argc, char **argv)
{
    if (argc != 3 || (strcmp(argv[2], "h") && strcmp(argv[2], "c"))) {
        return 2;
    }
    sigset_t unblock;
    sigemptyset(&unblock);
    sigaddset(&unblock, SIGALRM);
    if (signal(SIGALRM, SIG_DFL) == SIG_ERR ||
        sigprocmask(SIG_UNBLOCK, &unblock, NULL) != 0) {
        return 1;
    }
    alarm(lifetime);
    char role = argv[2][0];
    int fd = connect_role(argv[1], role);
    if (fd < 0 || role == 'c') {
        return fd < 0 ? 1 : park(fd);
    }

    char command;
    ssize_t count = read(fd, &command, 1);
    if (count != 1 || command != 'f') {
        return count == 0 ? 0 : 1;
    }
    int gate[2];
    if (pipe(gate) != 0) {
        return 1;
    }
    pid_t parent = fork();
    if (parent < 0) {
        return 1;
    }
    if (parent == 0) {
        command_parent(argv[1], fd, gate);
    }
    close(gate[0]);
    uint32_t pid = (uint32_t)parent;
    if (write(fd, &pid, sizeof(pid)) != sizeof(pid)) {
        return 1;
    }
    count = read(fd, &command, 1);
    close(gate[1]);
    int status;
    if (waitpid(parent, &status, 0) != parent || !WIFEXITED(status) ||
        WEXITSTATUS(status) != 0) {
        return 1;
    }
    if (count != 1 || command != 'e') {
        return count == 0 ? 0 : 1;
    }
    if (write(fd, "r", 1) != 1) {
        return 1;
    }
    return park(fd);
}
