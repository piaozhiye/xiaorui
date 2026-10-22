#define _POSIX_C_SOURCE 200809L
#include "ws_transport.h"

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

static const unsigned char WS_MASK[4] = {0x12, 0x34, 0x56, 0x78};

static long long monotonic_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

int xr_ws_wait_frame(xr_ws_t *ws, int timeout_ms, unsigned char *buf, size_t cap,
                    size_t *out_len, int *opcode) {
    long long deadline = monotonic_ms() + timeout_ms;
    for (;;) {
        long long remaining = deadline - monotonic_ms();
        int rc = xr_ws_recv_frame(ws, remaining > 0 ? (int)remaining : 0,
                                 buf, cap, out_len, opcode);
        if (rc != 0 || monotonic_ms() >= deadline) return rc;
    }
}

void xr_ws_init(xr_ws_t *ws, int fd) {
    memset(ws, 0, sizeof(*ws));
    ws->fd = fd;
}

int xr_ws_set_nonblocking(int fd) {
    int flags = fcntl(fd, F_GETFL, 0);
    return flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0 ? -1 : 0;
}

int xr_ws_write_all(int fd, const void *buf, size_t len, int timeout_ms) {
    const unsigned char *p = buf;
    while (len) {
        ssize_t n = send(fd, p, len, MSG_NOSIGNAL);
        if (n > 0) { p += n; len -= (size_t)n; continue; }
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            struct pollfd pfd = {.fd = fd, .events = POLLOUT};
            int ready;
            do { ready = poll(&pfd, 1, timeout_ms); } while (ready < 0 && errno == EINTR);
            if (ready > 0 && (pfd.revents & POLLOUT)) continue;
        }
        return -1;
    }
    return 0;
}

int xr_ws_send(xr_ws_t *ws, int opcode, const void *data, size_t len) {
    unsigned char header[14];
    size_t hn = 0;
    header[hn++] = 0x80 | (unsigned char)opcode;
    if (len < 126) header[hn++] = 0x80 | (unsigned char)len;
    else if (len < 65536) {
        header[hn++] = 0x80 | 126;
        header[hn++] = (unsigned char)(len >> 8);
        header[hn++] = (unsigned char)len;
    } else {
        header[hn++] = 0x80 | 127;
        uint64_t wire_len = len;
        for (int i = 7; i >= 0; --i)
            header[hn++] = (unsigned char)(wire_len >> (i * 8));
    }
    memcpy(header + hn, WS_MASK, sizeof(WS_MASK));
    hn += sizeof(WS_MASK);
    if (xr_ws_write_all(ws->fd, header, hn, 5000)) return -1;

    const unsigned char *p = data;
    unsigned char masked[1024];
    for (size_t off = 0; off < len;) {
        size_t n = len - off < sizeof(masked) ? len - off : sizeof(masked);
        for (size_t i = 0; i < n; ++i) masked[i] = p[off + i] ^ WS_MASK[(off + i) & 3];
        if (xr_ws_write_all(ws->fd, masked, n, 5000)) return -1;
        off += n;
    }
    return 0;
}

/* Parse at most one complete unfragmented frame already stored in ws->rx. */
static int parse_frame(xr_ws_t *ws, unsigned char *out, size_t cap,
                       size_t *out_len, int *opcode) {
    if (ws->rx_len < 2) return 0;
    const unsigned char *p = ws->rx;
    int fin = p[0] & 0x80;
    int op = p[0] & 0x0f;
    int masked = p[1] & 0x80;
    uint64_t len = p[1] & 0x7f;
    size_t pos = 2;
    if (!fin || op == 0) return -1; /* 协议暂不支持分片，安全重连而不是卡住。 */
    if (len == 126) {
        if (ws->rx_len < pos + 2) return 0;
        len = ((uint64_t)p[pos] << 8) | p[pos + 1]; pos += 2;
    } else if (len == 127) {
        if (ws->rx_len < pos + 8) return 0;
        len = 0;
        for (int i = 0; i < 8; ++i) len = (len << 8) | p[pos + i];
        pos += 8;
    }
    if (len > cap || len > XR_WS_RX_CAP) return -1;
    if (masked) {
        if (ws->rx_len < pos + 4) return 0;
        pos += 4;
    }
    if (len > SIZE_MAX - pos || ws->rx_len < pos + (size_t)len) return 0;
    if (len) memcpy(out, p + pos, (size_t)len);
    if (masked) {
        const unsigned char *mask = p + pos - 4;
        for (size_t i = 0; i < (size_t)len; ++i) out[i] ^= mask[i & 3];
    }
    size_t consumed = pos + (size_t)len;
    memmove(ws->rx, ws->rx + consumed, ws->rx_len - consumed);
    ws->rx_len -= consumed;
    *out_len = (size_t)len;
    *opcode = op;
    return 1;
}

int xr_ws_recv_frame(xr_ws_t *ws, int timeout_ms, unsigned char *buf, size_t cap,
                     size_t *out_len, int *opcode) {
    int parsed = parse_frame(ws, buf, cap, out_len, opcode);
    if (parsed) return parsed;

    struct pollfd pfd = {.fd = ws->fd, .events = POLLIN};
    int ready;
    do { ready = poll(&pfd, 1, timeout_ms); } while (ready < 0 && errno == EINTR);
    if (ready == 0) return 0;
    if (ready < 0 || (pfd.revents & (POLLERR | POLLNVAL))) return -1;
    /* POLLIN 与 POLLHUP 可同时出现：先消费对端关闭前发来的最后一帧。 */
    if ((pfd.revents & POLLHUP) && !(pfd.revents & POLLIN)) return -1;
    if (!(pfd.revents & POLLIN)) return 0;

    for (;;) {
        if (ws->rx_len == sizeof(ws->rx)) return -1;
        ssize_t n = recv(ws->fd, ws->rx + ws->rx_len, sizeof(ws->rx) - ws->rx_len, 0);
        if (n > 0) {
            ws->rx_len += (size_t)n;
            parsed = parse_frame(ws, buf, cap, out_len, opcode);
            if (parsed) return parsed;
            continue;
        }
        if (n == 0) return -1;
        if (errno == EINTR) continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK) break;
        return -1;
    }
    return parse_frame(ws, buf, cap, out_len, opcode);
}
