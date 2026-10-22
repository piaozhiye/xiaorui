/* backend_local.c -- local 后端实现: 板上全本地网关(XR_LOCAL_PORT, 默认 8002),
 * 小智 v1 协议的 detect+text 模式. 行为与 Step1 前的 xiaorui_live 完全一致. */
#define _POSIX_C_SOURCE 200809L
#include "backend.h"
#include "backend_turn.h"
#include "announce_protocol.h"
#include "json_escape.h"
#include "ws_transport.h"
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
#include <time.h>

#define WS_PORT 8000

typedef struct {
    backend_t base;
    int ws_fd;
    xr_ws_t ws;
    xr_turn_t turn;
    char announce_pending[33];
    char pending_msg[1400];
    size_t pending_len;      /* 发送失败时, 仅在重连后重试一次 */
} local_be_t;

static long long lb_now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}
static int read_all_blocking(int fd, void *b, size_t l, long long deadline) {
    unsigned char *p = b;
    while (l) {
        long long remaining = deadline - lb_now_ms();
        if (remaining <= 0) return -1;
        struct pollfd pfd = {.fd = fd, .events = POLLIN};
        int ready = poll(&pfd, 1, (int)remaining);
        if (ready < 0 && errno == EINTR) continue;
        if (ready <= 0) return -1;
        ssize_t n = recv(fd, p, l, MSG_DONTWAIT);
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) continue;
        if (n <= 0) { if (n < 0 && errno == EINTR) continue; return -1; }
        p += n; l -= (size_t)n; }
    return 0;
}

static int lb_connect(backend_t *b) {
    local_be_t *lb = (local_be_t *)b;
    /* Reconnect owns and replaces the old socket, including CLOSE_WAIT. */
    if (lb->ws_fd >= 0) { close(lb->ws_fd); lb->ws_fd = -1; }
    lb->announce_pending[0] = 0;
    xr_turn_reset(&lb->turn);
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    struct timeval handshake_tv = {.tv_sec = 5, .tv_usec = 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &handshake_tv, sizeof(handshake_tv));
    int one = 1; setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    /* XR_LOCAL_PORT: 本地腿服务端端口(2026-10-16 迁移: 8002=网关第二实例,
     * 本地 RKLLM+matcha+全意图族; 8000=老服务端, 保留为应急兜底)。 */
    int local_port = WS_PORT;
    const char *lp = getenv("XR_LOCAL_PORT");
    if (lp && atoi(lp) > 0) local_port = atoi(lp);
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons(local_port)};
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr))) { close(fd); return -1; }
    char req[512];
    int n = snprintf(req, sizeof(req),
        "GET /xiaorui/v1/ HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Sec-WebSocket-Version: 13\r\ndevice-id: xiaorui-board\r\n"
        "client-id: xiaorui-board-c\r\n\r\n", local_port);
    if (xr_ws_write_all(fd, req, (size_t)n, 5000)) { close(fd); return -1; }
    char reply[2048]; size_t used = 0;
    long long deadline = lb_now_ms() + 5000;
    while (used + 1 < sizeof(reply)) {
        if (read_all_blocking(fd, reply + used, 1, deadline)) { close(fd); return -1; }
        used++;
        if (used >= 4 && !memcmp(reply + used - 4, "\r\n\r\n", 4)) break;
    }
    reply[used] = 0;
    if (used < 4 || memcmp(reply + used - 4, "\r\n\r\n", 4)) { close(fd); return -1; }
    if (strncmp(reply, "HTTP/1.1 101 ", 13)) { fprintf(stderr, "[be] upgrade: %.80s\n", reply); close(fd); return -1; }
    if (xr_ws_set_nonblocking(fd)) { close(fd); return -1; }
    lb->ws_fd = fd;
    xr_ws_init(&lb->ws, fd);
    /* hello + listen start (local 协议序列) */
    static const char *hello_json =
        "{\"type\":\"hello\",\"version\":1,\"transport\":\"websocket\","
        "\"features\":{\"mcp\":false,\"aec\":false,\"announce_ack\":true},\"audio_params\":"
        "{\"format\":\"opus\",\"sample_rate\":16000,\"channels\":1,\"frame_duration\":60}}";
    if (xr_ws_send(&lb->ws, 1, hello_json, strlen(hello_json))) { close(fd); lb->ws_fd = -1; return -1; }
    {
        unsigned char buf[2048]; size_t l = 0;
        int op = 0;
        if (xr_ws_wait_frame(&lb->ws, 5000, buf, sizeof(buf), &l, &op) != 1) op = -1;
        if (op != 1) { fprintf(stderr, "[be] server hello 缺失\n"); close(fd); lb->ws_fd = -1; return -1; }
    }
    static const char *listen_start =
        "{\"type\":\"listen\",\"state\":\"start\",\"mode\":\"auto\"}";
    if (xr_ws_send(&lb->ws, 1, listen_start, strlen(listen_start))) { close(fd); lb->ws_fd = -1; return -1; }
    /* 重连补发上一轮指令(local 语义: 至多重试一次) */
    if (lb->pending_len) {
        size_t retry = lb->pending_len;
        lb->pending_len = 0;
        if (xr_ws_send(&lb->ws, 1, lb->pending_msg, retry)) {
            fprintf(stderr, "[be] 重连后补发失败, 当前轮已丢弃\n");
        } else {
            fprintf(stderr, "[be] 重连后已补发当前轮指令\n");
            lb->pending_msg[0] = 0;
            lb->turn.state = XR_TURN_ACCEPTED; /* 兼容原补发即受理语义，保留 id */
            return 2;           /* 告知调用方: 本轮已重发, 应直接进 RESPOND */
        }
    }
    return 0;
}
static void lb_disconnect(backend_t *b) {
    local_be_t *lb = (local_be_t *)b;
    lb->announce_pending[0] = 0;
    if (lb->ws_fd >= 0) { close(lb->ws_fd); lb->ws_fd = -1; }
}
static int lb_abort(backend_t *b) {
    local_be_t *lb = (local_be_t *)b;
    static const char message[] = "{\"type\":\"abort\"}";
    lb->announce_pending[0] = 0;
    lb->pending_len = 0;
    lb->pending_msg[0] = 0;
    lb->turn.state = XR_TURN_FAILED;
    lb->turn.audio_packets = 0;
    if (lb->ws_fd < 0) return -1;
    return xr_ws_send(&lb->ws, 1, message, sizeof(message) - 1);
}
static int lb_submit_text(backend_t *b, const char *text) {
    local_be_t *lb = (local_be_t *)b;
    lb->announce_pending[0] = 0;
    xr_turn_begin(&lb->turn);
    lb->pending_len = 0;
    /* 转义交给共用助手: ASR 文本原样来自模型 JSON, 必含换行/制表时
       旧的内联转义会拼出非法 JSON, 服务端拒收 -> 整轮静默丢失。 */
    char esc[1024];
    xr_json_escape(text, esc, sizeof esc);
    char msg[1400];
    int mn = snprintf(msg, sizeof(msg),
        "{\"type\":\"listen\",\"state\":\"detect\",\"text\":\"%s\"}", esc);
    if (xr_ws_send(&lb->ws, 1, msg, (size_t)mn)) {
        lb->turn.state = XR_TURN_FAILED;
        /* 缓存待重连补发 */
        if ((size_t)mn <= sizeof(lb->pending_msg)) {
            memcpy(lb->pending_msg, msg, (size_t)mn);
            lb->pending_len = (size_t)mn;
        }
        return -1;
    }
    fprintf(stderr, "[be] turn=%llu submitted\n", (unsigned long long)lb->turn.id);
    return 0;
}
static backend_ev_t lb_poll_event(backend_t *b, unsigned char *buf, size_t cap,
                                  size_t *out_len, char *ev_text, size_t text_cap,
                                  int timeout_ms) {
    local_be_t *lb = (local_be_t *)b;
    size_t l = 0;
    int op = 0;
    int rc = xr_ws_recv_frame(&lb->ws, timeout_ms, buf, cap, &l, &op);
    if (rc == 0) return EV_TIMEOUT;
    if (rc < 0 || op == 8) { lb->turn.state = XR_TURN_FAILED; return EV_ERROR; }
    if (op == 9) { (void)xr_ws_send(&lb->ws, 10, buf, l); return EV_NONE; }
    if (op != 1) {
        if (op == 2 && xr_turn_accepted(&lb->turn)) {
            xr_turn_audio(&lb->turn); *out_len = l; return EV_TTS_AUDIO;
        }
        return EV_NONE;   /* 未受理期的音频/未知帧: 丢弃 */
    }
    size_t m = l < cap - 1 ? l : cap - 1;
    buf[m] = 0;
    int is_stop = strstr((char *)buf, "\"state\":\"stop\"") ||
                  strstr((char *)buf, "\"state\": \"stop\"");
    if (is_stop) {
        if (!xr_turn_accepted(&lb->turn)) {
            fprintf(stderr, "[be] 忽略迟到 stop(本轮尚无 stt)\n"); return EV_NONE;
        }
        lb->turn.state = XR_TURN_FINISHED;
        return EV_TTS_STOP;
    }
    if (strstr((char *)buf, "\"type\":\"reboot\"")
        || strstr((char *)buf, "\"type\": \"reboot\"")) {
        /* 服务端语音重启(音频直传轮本地无文本, 意图在服务端判)。 */
        return EV_REBOOT;
    }
    if (strstr((char *)buf, "\"type\":\"announce\"")
        || strstr((char *)buf, "\"type\": \"announce\"")) {
        if (strstr((char *)buf, "\"request_id\"") || strstr((char *)buf, "\"state\"")) {
            char id[33];
            int control = xr_announce_control((char *)buf, lb->announce_pending, &lb->turn);
            if (control) return control == 1 ? EV_TTS_STOP : EV_NONE;
            if (!strstr((char *)buf, "\"state\":\"offer\"") &&
                !strstr((char *)buf, "\"state\": \"offer\"")) return EV_NONE;
            if (!xr_announce_id((char *)buf, id) || !ev_text || text_cap < 33) return EV_NONE;
            memcpy(ev_text, id, sizeof(id));
            return EV_ANNOUNCE_REQUEST;
        }
        /* 服务端主动播报(定时提醒到点): announce=服务端开轮。本地 turn 从
           IDLE 起步, accept 单独调用是空操作 -> 先 begin 再 accept, 否则后续
           start/audio/stop 全被"未受理期"守卫丢弃。 */
        xr_turn_begin(&lb->turn);
        xr_turn_accept(&lb->turn);
        return EV_ANNOUNCE;
    }
    if (strstr((char *)buf, "\"type\":\"volume\"")
        || strstr((char *)buf, "\"type\": \"volume\"")) {
        /* 服务端音量指令(音频直传轮的意图判定在服务端): 客户端执行软件增益。
           ev_text 约定 "up|down|max|min|set:NN", 主循环映射后本地收轮。 */
        if (!xr_turn_accept(&lb->turn)) return EV_NONE;
        lb->turn.state = XR_TURN_FINISHED;
        if (ev_text && text_cap) {
            const char *op = strstr((char *)buf, "\"op\":");
            const char *val = strstr((char *)buf, "\"value\":");
            char kind[8] = {0};
            int v = 0;
            if (op) {
                op += 5;
                while (*op == ' ') op++;      /* 服务端 json.dumps 冒号后带空格 */
                if (*op == '"') op++;
                for (size_t ki = 0; ki < sizeof(kind) - 1 && op[ki] && op[ki] != '"'; ki++)
                    kind[ki] = op[ki];
            }
            if (val) v = atoi(val + 8);
            if (!strcmp(kind, "set")) snprintf(ev_text, text_cap, "set:%d", v);
            else snprintf(ev_text, text_cap, "%s", kind);
        }
        return EV_VOLUME;
    }
    if (strstr((char *)buf, "\"type\":\"silent\"") || strstr((char *)buf, "\"type\": \"silent\"")) {
        /* 服务端静默丢弃(纯唤醒/噪声)。reason 决定前端收轮后是否开跟说窗。 */
        if (ev_text && text_cap) {
            const char *k = strstr((char *)buf, "\"reason\":");
            ev_text[0] = 0;
            if (k) {
                k += 9; while (*k == ' ') k++;
                if (*k == '"') k++;
                size_t ti = 0;
                while (*k && *k != '"' && ti < text_cap - 1) ev_text[ti++] = *k++;
                ev_text[ti] = 0;
            }
        }
        return EV_SILENT;
    }
    if (strstr((char *)buf, "\"type\":\"stt\"") || strstr((char *)buf, "\"type\": \"stt\"")) {
        if (!xr_turn_accept(&lb->turn)) return EV_NONE;
        if (ev_text && text_cap) {
            const char *k = strstr((char *)buf, "\"text\":");
            if (k) {
                k += 7; while (*k == ' ') k++;
                if (*k == '"') k++;
                size_t ti = 0;
                while (*k && *k != '"' && ti < text_cap - 1) ev_text[ti++] = *k++;
                ev_text[ti] = 0;
            }
        }
        return EV_STT;
    }
    if (strstr((char *)buf, "\"text\"")) {
        if (!xr_turn_active(&lb->turn)) return EV_NONE;
        if (ev_text && text_cap) snprintf(ev_text, text_cap, "%s", (char *)buf);
        return EV_TTS_START;
    }
    return EV_NONE;
}
static int lb_ping(backend_t *b) {
    local_be_t *lb = (local_be_t *)b;
    static const char ping[] = "{\"type\":\"ping\"}";
    return xr_ws_send(&lb->ws, 1, ping, sizeof(ping) - 1);
}
static int lb_has_audio(backend_t *b) { return xr_turn_has_audio(&((local_be_t *)b)->turn); }
static int lb_seen_stt(backend_t *b) { return xr_turn_accepted(&((local_be_t *)b)->turn); }
static int lb_drop_turn(backend_t *b) {
    /* 前端 8s/20s 兜底弃轮时清标志; 不动 pending——那属于断线补发链路 */
    local_be_t *lb = (local_be_t *)b;
    lb->announce_pending[0] = 0;
    lb->turn.state = XR_TURN_FAILED;
    lb->turn.audio_packets = 0;
    return 0;
}

static int lb_announce_reply(backend_t *b, const char *id, int accepted) {
    local_be_t *lb = (local_be_t *)b;
    return xr_announce_reply(&lb->ws, lb->announce_pending, id, accepted);
}
static const backend_ops_t LOCAL_OPS = {
    .create = NULL, .connect = lb_connect, .disconnect = lb_disconnect,
    .abort = lb_abort,
    .submit_text = lb_submit_text, .poll_event = lb_poll_event,
    .ping = lb_ping, .has_audio = lb_has_audio,
    .seen_stt = lb_seen_stt, .drop_turn = lb_drop_turn,
    .announce_reply = lb_announce_reply,
};
backend_t *be_open_local(void) {
    local_be_t *lb = calloc(1, sizeof(local_be_t));
    if (!lb) return NULL;
    lb->base.ops = &LOCAL_OPS;
    lb->ws_fd = -1;
    return (backend_t *)lb;
}
/* 主循环需要 seen_stt 做空回答兜底判定(经 ops 表路由, local/cloud 各自实现) */
int be_seen_stt(backend_t *b) { return b->ops->seen_stt ? b->ops->seen_stt(b) : 0; }
int be_announce_reply(backend_t *b, const char *id, int accepted) {
    return b->ops->announce_reply ? b->ops->announce_reply(b, id, accepted) : -1;
}
int be_drop_turn(backend_t *b) { return b->ops->drop_turn ? b->ops->drop_turn(b) : 0; }
int be_wants_audio(backend_t *b) { return b->ops->wants_audio ? b->ops->wants_audio(b) : 0; }
int be_submit_audio(backend_t *b, const backend_audio_request_t *r) {
    return b->ops->submit_audio ? b->ops->submit_audio(b, r) : -1;
}

/* 薄封装实现 */
int  be_connect(backend_t *b) { return b->ops->connect ? b->ops->connect(b) : -1; }
void be_disconnect(backend_t *b) { if (b->ops->disconnect) b->ops->disconnect(b); }
/* Both gateway backends cancel playback without closing their connection. */
int  be_abort(backend_t *b) { return b->ops->abort ? b->ops->abort(b) : -1; }
int  be_submit_text(backend_t *b, const char *text) { return b->ops->submit_text ? b->ops->submit_text(b, text) : -1; }
backend_ev_t be_poll_event(backend_t *b, unsigned char *buf, size_t cap,
                           size_t *out_len, char *ev_text, size_t text_cap,
                           int timeout_ms) {
    return b->ops->poll_event ? b->ops->poll_event(b, buf, cap, out_len, ev_text, text_cap,
                                                  timeout_ms) : EV_ERROR;
}
int  be_ping(backend_t *b) { return b->ops->ping ? b->ops->ping(b) : -1; }
int  be_has_audio(backend_t *b) { return b->ops->has_audio ? b->ops->has_audio(b) : 0; }
