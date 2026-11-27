#define _POSIX_C_SOURCE 200809L
#include "xr_fifo_pcm.h"

#include <errno.h>
#include <limits.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

long xr_fifo_read_s16(int fd, int16_t *out, size_t frames, int *eof_seen)
{
    if (fd < 0 || !out || !frames || !eof_seen || frames > (size_t)(LONG_MAX / 2)) {
        errno = EINVAL;
        return -1;
    }

    const size_t wanted = frames * sizeof(*out);
    size_t received = 0;
    while (received < wanted) {
        ssize_t n = read(fd, (char *)out + received, wanted - received);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (n == 0) {
            /* PCM is 16-bit: never leave an odd trailing byte as a sample. */
            size_t valid = received & ~(size_t)1;
            memset((char *)out + valid, 0, wanted - valid);
            *eof_seen = 1;

            /* A blocking FIFO fd returns EOF immediately until a new writer
             * appears. Keep the caller at the requested audio cadence rather
             * than spinning and burning a CPU core during that interval.
             */
            struct timespec delay = {
                .tv_sec = (time_t)(frames / 16000),
                .tv_nsec = (long)(frames % 16000) * 1000000000L / 16000,
            };
            while (nanosleep(&delay, &delay) < 0 && errno == EINTR) {}
            return (long)frames;
        }
        received += (size_t)n;
        *eof_seen = 0;
    }
    return (long)frames;
}
