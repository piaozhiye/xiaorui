/* ws_transport.h -- 单连接、增量解析的 WebSocket 基础设施。
 *
 * 后端只关心完整帧；transport 自己保留半帧，绝不因 poll() 只读到帧头而
 * 继续阻塞 read。当前协议只使用未分片的 text/binary/ping/pong 帧。
 */
#ifndef XR_WS_TRANSPORT_H
#define XR_WS_TRANSPORT_H

#include <stddef.h>

enum { XR_WS_RX_CAP = 131072 };

typedef struct {
    int fd;
    unsigned char rx[XR_WS_RX_CAP];
    size_t rx_len;
} xr_ws_t;

void xr_ws_init(xr_ws_t *ws, int fd);
int xr_ws_set_nonblocking(int fd);
int xr_ws_write_all(int fd, const void *buf, size_t len, int timeout_ms);
int xr_ws_send(xr_ws_t *ws, int opcode, const void *data, size_t len);

/* 返回 1=完整帧, 0=超时/半帧, -1=断开/协议/IO 错误。
 * opcode 为 WebSocket opcode；payload 已去 mask，写入 buf。 */
int xr_ws_recv_frame(xr_ws_t *ws, int timeout_ms, unsigned char *buf, size_t cap,
                     size_t *out_len, int *opcode);

/* 握手期等待完整帧，半帧沿用同一总截止时间，不重置超时。 */
int xr_ws_wait_frame(xr_ws_t *ws, int timeout_ms, unsigned char *buf, size_t cap,
                    size_t *out_len, int *opcode);

#endif
