#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t interrupted;

static void record_interrupt(int sig)
{
    (void)sig;
    interrupted = 1;
}

static int mark(const char *path)
{
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0) {
        perror("create marker");
        return 1;
    }
    return close(fd) != 0;
}

static int terminal(int fd)
{
    int tty = open("/dev/tty", O_RDONLY);
    if (!isatty(0) || !isatty(1) || !isatty(2) || tty < 0
        || tcgetpgrp(tty) != getpgrp() || getsid(0) != getpid()) {
        fprintf(stderr, "fixture did not get a controlling terminal\n");
        return 2;
    }
    close(tty);
    if (fd >= 0) {
        char data[7];
        if (read(fd, data, sizeof(data)) != sizeof(data)
            || memcmp(data, "permit\n", sizeof(data)) != 0) {
            fprintf(stderr, "fixture did not get its explicit descriptor\n");
            return 3;
        }
        puts("FD READY");
    }
    puts("TERMINAL READY");
    fflush(stdout);
    char line[80];
    if (!fgets(line, sizeof(line), stdin)) {
        return 4;
    }
    printf("ACK %s", line);
    fflush(stdout);
    return 17;
}

static int detach(const char *gate, const char *marker, int wait_for_interrupt)
{
    const struct timespec delay = {.tv_nsec = 10 * 1000 * 1000};
    if (wait_for_interrupt) {
        struct sigaction action = {.sa_handler = record_interrupt};
        sigemptyset(&action.sa_mask);
        if (sigaction(SIGINT, &action, NULL) != 0) {
            return 5;
        }
    }
    int ready[2];
    if (pipe(ready) != 0) {
        return 6;
    }
    pid_t child = fork();
    if (child == 0) {
        alarm(20);
        close(ready[0]);
        if (setsid() < 0 || write(ready[1], "r", 1) != 1) {
            _exit(7);
        }
        close(ready[1]);
        while (access(gate, F_OK) != 0) {
            nanosleep(&delay, NULL);
        }
        _exit(mark(marker));
    }
    close(ready[1]);
    char byte;
    if (child < 0 || read(ready[0], &byte, 1) != 1 || byte != 'r') {
        return 8;
    }
    close(ready[0]);
    printf("DETACHED %d\n", (int)child);
    fflush(stdout);
    if (!wait_for_interrupt) {
        return 23;
    }
    while (!interrupted) {
        nanosleep(&delay, NULL);
    }
    return 128 + SIGINT;
}

int main(int argc, char **argv)
{
    alarm(20);
    if (argc == 3 && strcmp(argv[1], "mark") == 0) {
        return mark(argv[2]);
    }
    if ((argc == 2 || argc == 3) && strcmp(argv[1], "terminal") == 0) {
        return terminal(argc == 3 ? atoi(argv[2]) : -1);
    }
    if (argc == 4 && strcmp(argv[1], "orphan") == 0) {
        return detach(argv[2], argv[3], 0);
    }
    if (argc == 4 && strcmp(argv[1], "interrupt") == 0) {
        return detach(argv[2], argv[3], 1);
    }
    return 9;
}
