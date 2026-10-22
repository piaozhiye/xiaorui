#include "ws_transport.h"

#include <assert.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

int main(void) {
    int pair[2];
    assert(socketpair(AF_UNIX, SOCK_STREAM, 0, pair) == 0);
    assert(xr_ws_set_nonblocking(pair[0]) == 0);
    xr_ws_t ws;
    xr_ws_init(&ws, pair[0]);

    /* 服务端 text 帧分两次到达：首字节可读时不能阻塞等待余下帧体。 */
    const unsigned char frame[] = {0x81, 0x05, 'h', 'e', 'l', 'l', 'o'};
    assert(write(pair[1], frame, 1) == 1);
    unsigned char out[32]; size_t len = 0; int opcode = 0;
    assert(xr_ws_recv_frame(&ws, 0, out, sizeof(out), &len, &opcode) == 0);
    assert(write(pair[1], frame + 1, sizeof(frame) - 1) == (ssize_t)(sizeof(frame) - 1));
    assert(xr_ws_recv_frame(&ws, 0, out, sizeof(out), &len, &opcode) == 1);
    assert(opcode == 1 && len == 5 && !memcmp(out, "hello", 5));

    /* 两帧合包时，一次取一帧，第二帧保留给下一轮。 */
    const unsigned char two[] = {0x81, 0x01, 'a', 0x82, 0x02, 1, 2};
    assert(write(pair[1], two, sizeof(two)) == (ssize_t)sizeof(two));
    assert(xr_ws_recv_frame(&ws, 0, out, sizeof(out), &len, &opcode) == 1);
    assert(opcode == 1 && len == 1 && out[0] == 'a');
    assert(xr_ws_recv_frame(&ws, 0, out, sizeof(out), &len, &opcode) == 1);
    assert(opcode == 2 && len == 2 && out[0] == 1 && out[1] == 2);

    /* 客户端帧必须带 mask；服务端解开后应拿到原始内容。 */
    assert(xr_ws_send(&ws, 1, "ok", 2) == 0);
    unsigned char sent[8];
    assert(read(pair[1], sent, sizeof(sent)) == (ssize_t)sizeof(sent));
    assert(sent[0] == 0x81 && sent[1] == 0x82);
    assert((sent[6] ^ sent[2]) == 'o' && (sent[7] ^ sent[3]) == 'k');

    /* 对端发完最后一帧立即关闭，也必须先交付完整帧。 */
    assert(write(pair[1], frame, sizeof(frame)) == (ssize_t)sizeof(frame));
    close(pair[1]);
    assert(xr_ws_recv_frame(&ws, 0, out, sizeof(out), &len, &opcode) == 1);
    assert(opcode == 1 && len == 5 && !memcmp(out, "hello", 5));
    assert(xr_ws_recv_frame(&ws, 0, out, sizeof(out), &len, &opcode) == -1);
    close(pair[0]);
    return 0;
}
