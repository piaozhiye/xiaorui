#ifndef XR_FIFO_PCM_H
#define XR_FIFO_PCM_H

#include <stddef.h>
#include <stdint.h>

/* Read one 16-bit PCM frame from a FIFO. EINTR is retried. If the writer
 * closes, preserve bytes already read, zero-pad the remainder, and return a
 * complete silent-capable frame so the caller need not restart its audio
 * stack just because an external capture process is being replaced.
 * eof_seen is set while no writer is connected and cleared after data returns.
 */
long xr_fifo_read_s16(int fd, int16_t *out, size_t frames, int *eof_seen);

#endif
