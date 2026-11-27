#define _POSIX_C_SOURCE 200809L
#include "xr_fifo_pcm.h"

#include <assert.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/stat.h>
#include <unistd.h>

static void write_all(int fd, const void *data, size_t size)
{
    const char *p = data;
    while (size) {
        ssize_t n = write(fd, p, size);
        assert(n > 0);
        p += n;
        size -= (size_t)n;
    }
}

typedef struct {
    int fd;
    long result;
    int16_t sample;
    int eof_seen;
} blocked_read_t;

static void ignore_signal(int signal_number) { (void)signal_number; }

static void *blocked_read(void *arg)
{
    blocked_read_t *read = arg;
    read->result = xr_fifo_read_s16(read->fd, &read->sample, 1, &read->eof_seen);
    return NULL;
}

int main(void)
{
    char path[] = "/tmp/xr-fifo-pcm-test-XXXXXX";
    int tmp = mkstemp(path);
    assert(tmp >= 0);
    close(tmp);
    unlink(path);
    assert(mkfifo(path, 0600) == 0);

    int reader = open(path, O_RDONLY | O_NONBLOCK);
    assert(reader >= 0);
    int flags = fcntl(reader, F_GETFL, 0);
    assert(flags >= 0 && fcntl(reader, F_SETFL, flags & ~O_NONBLOCK) == 0);

    const int16_t first[] = {123, -456};
    int writer = open(path, O_WRONLY | O_NONBLOCK);
    assert(writer >= 0);
    write_all(writer, first, sizeof(first));
    close(writer);

    int16_t frame[4] = {9, 9, 9, 9};
    int eof_seen = 0;
    assert(xr_fifo_read_s16(reader, frame, 4, &eof_seen) == 4);
    assert(frame[0] == 123 && frame[1] == -456);
    assert(frame[2] == 0 && frame[3] == 0 && eof_seen);

    memset(frame, 7, sizeof(frame));
    assert(xr_fifo_read_s16(reader, frame, 4, &eof_seen) == 4);
    assert(frame[0] == 0 && frame[1] == 0 && frame[2] == 0 && frame[3] == 0);
    assert(eof_seen);

    const int16_t second[] = {321, -654, 777, -888};
    writer = open(path, O_WRONLY | O_NONBLOCK);
    assert(writer >= 0);
    write_all(writer, second, sizeof(second));
    close(writer);
    memset(frame, 0, sizeof(frame));
    assert(xr_fifo_read_s16(reader, frame, 4, &eof_seen) == 4);
    assert(memcmp(frame, second, sizeof(second)) == 0);
    assert(!eof_seen);

    close(reader);
    unlink(path);

    /* EINTR on a blocked PCM read is transient and must be retried. */
    int pipefd[2];
    assert(pipe(pipefd) == 0);
    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = ignore_signal;
    sigemptyset(&action.sa_mask);
    assert(sigaction(SIGUSR1, &action, NULL) == 0);
    blocked_read_t read = {.fd = pipefd[0], .result = -99, .sample = 0, .eof_seen = 0};
    pthread_t thread;
    assert(pthread_create(&thread, NULL, blocked_read, &read) == 0);
    struct timespec pause = {.tv_sec = 0, .tv_nsec = 20000000L};
    for (int i = 0; i < 5; ++i) {
        nanosleep(&pause, NULL);
        assert(pthread_kill(thread, SIGUSR1) == 0);
    }
    const int16_t after_interrupt = -1234;
    write_all(pipefd[1], &after_interrupt, sizeof(after_interrupt));
    assert(pthread_join(thread, NULL) == 0);
    assert(read.result == 1 && read.sample == after_interrupt && !read.eof_seen);
    close(pipefd[0]);
    close(pipefd[1]);

    puts("fifo PCM EOF recovery PASS");
    return 0;
}
