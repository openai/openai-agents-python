#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <unistd.h>

int main(int argc, char **argv) {
    int ready[2];
    if ((argc != 3 && argc != 4) || pipe(ready) == -1 || signal(SIGHUP, SIG_IGN) == SIG_ERR) {
        return 1;
    }
    pid_t child = fork();
    if (child == -1) {
        return 1;
    }
    if (child > 0) {
        // The tracked command exits successfully only after the child is ready.
        close(ready[1]);
        char started;
        return read(ready[0], &started, 1) == 1 ? 0 : 1;
    }
    close(ready[0]);
    close(STDIN_FILENO);
    close(STDOUT_FILENO);
    close(STDERR_FILENO);
    int detach_error = setsid() == -1 ? errno : 0;
    if (detach_error != 0 && (argc == 3 || (detach_error != EPERM && detach_error != EACCES))) {
        return 1;
    }
    if (argc == 4) {
        FILE *status = fopen(argv[3], "w");
        if (status == NULL || fprintf(status, "%ld %d\n", (long)getpid(), detach_error) < 0 ||
            fclose(status) != 0) {
            return 1;
        }
    }
    if (write(ready[1], "1", 1) != 1) {
        return 1;
    }
    close(ready[1]);

    // Exit within 30 seconds even if the test never releases the child.
    for (int attempt = 0; attempt < 300; ++attempt) {
        if (access(argv[1], F_OK) == 0) {
            FILE *witness = fopen(argv[2], "w");
            if (witness == NULL || fputs("old grant", witness) == EOF) {
                return 1;
            }
            return fclose(witness) == 0 ? 0 : 1;
        }
        usleep(100000);
    }
    return 1;
}
