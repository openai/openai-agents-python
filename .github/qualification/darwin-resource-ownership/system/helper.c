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

int main(int argc, char **argv)
{
    if (argc != 3 || (strcmp(argv[2], "h") && strcmp(argv[2], "c"))) {
        return 2;
    }
    sigset_t unblock;
    sigemptyset(&unblock);
    sigaddset(&unblock, SIGALRM);
    struct sigaction reap = {.sa_handler = SIG_DFL};
    sigemptyset(&reap.sa_mask);
    if (signal(SIGALRM, SIG_DFL) == SIG_ERR ||
        sigprocmask(SIG_UNBLOCK, &unblock, NULL) != 0 ||
        sigaction(SIGCHLD, &reap, NULL) != 0) {
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
    pid_t child = fork();
    if (child < 0) {
        return 1;
    }
    if (child == 0) {
        alarm(12);
        close(fd);
        if (setsid() >= 0) {
            execl("/bin/sleep", "/bin/sleep", "10", (char *)NULL);
        }
        _exit(1);
    }
    uint32_t pid = (uint32_t)child;
    if (write(fd, &pid, sizeof(pid)) != sizeof(pid)) {
        return 1;
    }
    int status;
    if (waitpid(child, &status, 0) != child) {
        return 1;
    }
    char result = WIFSIGNALED(status) && WTERMSIG(status) == SIGKILL ? 'k' : 'x';
    if (write(fd, &result, 1) != 1) {
        return 1;
    }
    return park(fd);
}
