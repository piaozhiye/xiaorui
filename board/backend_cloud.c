/* backend_cloud.c -- 云端 LLM 后端: 小智 v1 协议 detect+text 模式(端侧 ASR 文本
 * 上行, 云端/网关服务端负责 LLM+TTS). 与 backend_local 同骨架, 差异:
 *   - 端点/鉴权来自环境变量, 支持 hostname(DNS), 5s 连接超时(网关失联不拖死前端)
 *   - hello 声明 24k(网关按声明采样率合成下行 opus; 解码器固定 24k 输出兼容任意率)
 *   - seen_stt 在 stt/llm/tts 任一文本事件置位: 开源网关在 detect 模式下不一定回显
 *     stt, 只认 stt 会导致真 stop 被当迟到丢弃、本轮永不收尾
 * v1 仅 ws://(LAN 网关部署); wss(官方云)需 TLS 栈, 后续接 openssl 再开.
 *
 * 环境变量:
 *   XR_CLOUD_URL    ws://host[:port][/path]   (必填, 默认路径 /xiaorui/v1/)
 *   XR_CLOUD_TOKEN  Bearer 鉴权(可选, 注入 Authorization 头)
 *   XR_DEVICE_ID    device-id 头与 hello.device_id(默认 xiaorui-board)
 */
#define _GNU_SOURCE   /* -std=c11 严格模式下 netdb.h 的 getaddrinfo 需要 */
#include "backend.h"
#include "backend_turn.h"
#include "announce_protocol.h"
#include "runtime_config.h"
#include "json_escape.h"
#include "opus.h"
#include "ws_transport.h"
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

typedef struct {
    backend_t base;           /* ops 指针必须首成员 */
    int ws_fd;
    xr_ws_t ws;
    xr_turn_t turn;
    char announce_pending[33];
    char pending_msg[1400];
    size_t pending_len;       /* 发送失败时, 仅在重连后重试一次 */
    char host[128];
    char path[128];
    int port;
    char auth_hdr[560];       /* "Authorization: Bearer ...\r\n" 或空 */
    char device_id[64];
} cloud_be_t;

static int read_all_blocking(int fd, void *b, size_t l) {
    unsigned char *p = b;
    while (l) { ssize_t n = recv(fd, p, l, 0);
        if (n <= 0) { if (n < 0 && errno == EINTR) continue; return -1; }
        p += n; l -= (size_t)n; }
    return 0;
}

/* 5s(默认, XR_CLOUD_CONN_MS 可调 500..15000) 非阻塞连接:
 * 网关失联/外网不可达时快速失败, 由前端 restart 的 10s 退避兜住.
 * hybrid 的随轮探测也走这里, 超时越短恢复探测那一轮的额外延迟越小. */
static int tcp_connect_timeout(const char *host, int port) {
    char port_s[8];
    snprintf(port_s, sizeof(port_s), "%d", port);
    struct addrinfo hints = {0}, *res = NULL;
    hints.ai_family = AF_UNSPEC;          /* v4/v6 都收(主机名或 v4 字面量;
                                             * URL 里的裸 v6 字面量未支持, 解析在首个':'断开) */
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host, port_s, &hints, &res) || !res) return -1;
    int fd = -1;
    for (struct addrinfo *ai = res; ai; ai = ai->ai_next) {
        fd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (fd < 0) continue;
        /* 非阻塞 connect + poll */
        int fl = fcntl(fd, F_GETFL, 0);
        if (fl < 0 || fcntl(fd, F_SETFL, fl | O_NONBLOCK) < 0) { close(fd); fd = -1; continue; }
        int rc = connect(fd, ai->ai_addr, ai->ai_addrlen);
        if (rc == 0) break;
        if (errno == EINPROGRESS) {
            struct pollfd pfd = {.fd = fd, .events = POLLOUT};
            int pr = poll(&pfd, 1, xr_runtime_config()->cloud_conn_ms);
            int soerr = 0; socklen_t slen = sizeof(soerr);
            if (pr > 0 && (pfd.revents & POLLOUT) &&
                !getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &slen) && !soerr)
                break;
            close(fd); fd = -1; continue;
        }
        close(fd); fd = -1;
    }
    freeaddrinfo(res);
    if (fd < 0) return -1;
    int fl = fcntl(fd, F_GETFL, 0);
    if (fl < 0 || fcntl(fd, F_SETFL, fl & ~O_NONBLOCK) < 0) { close(fd); return -1; }
    return fd;
}

static int cb_connect(backend_t *b) {
    cloud_be_t *cb = (cloud_be_t *)b;
    xr_turn_reset(&cb->turn);
    int fd = tcp_connect_timeout(cb->host, cb->port);
    if (fd < 0) { fprintf(stderr, "[be-cloud] 连接 %s:%d 失败\n", cb->host, cb->port); return -1; }
    struct timeval handshake_tv = {.tv_sec = 5, .tv_usec = 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &handshake_tv, sizeof(handshake_tv));
    int one = 1; setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    char req[1024];
    int n = snprintf(req, sizeof(req),
        "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Sec-WebSocket-Version: 13\r\ndevice-id: %s\r\n"
        "client-id: %s-c\r\n%s\r\n",
        cb->path, cb->host, cb->port, cb->device_id, cb->device_id, cb->auth_hdr);
    if (xr_ws_write_all(fd, req, (size_t)n, 5000)) { close(fd); return -1; }
    char reply[2048]; size_t used = 0;
    while (used + 1 < sizeof(reply)) {
        if (read_all_blocking(fd, reply + used, 1)) { close(fd); return -1; }
        used++;
        if (used >= 4 && !memcmp(reply + used - 4, "\r\n\r\n", 4)) break;
    }
    reply[used] = 0;
    if (strncmp(reply, "HTTP/1.1 101 ", 13) && strncmp(reply, "HTTP/1.0 101 ", 13)) {
        /* 401/403 = 鉴权失败, 打印状态行便于排查 token */
        fprintf(stderr, "[be-cloud] upgrade 失败: %.80s\n", reply);
        close(fd); return -1;
    }
    if (xr_ws_set_nonblocking(fd)) { close(fd); return -1; }
    cb->ws_fd = fd;
    xr_ws_init(&cb->ws, fd);
    char hello_json[512];
    int hn = snprintf(hello_json, sizeof(hello_json),
        "{\"type\":\"hello\",\"version\":1,\"transport\":\"websocket\","
        "\"device_id\":\"%s\",\"features\":{\"mcp\":false,\"aec\":false,\"announce_ack\":true},"
        "\"audio_params\":{\"format\":\"opus\",\"sample_rate\":%d,"
        "\"channels\":1,\"frame_duration\":60}}", cb->device_id,
        xr_runtime_config()->cloud_asr ? 16000 : 24000);
    if (hn < 0 || (size_t)hn >= sizeof(hello_json)) { close(fd); cb->ws_fd = -1; return -1; }
    if (xr_ws_send(&cb->ws, 1, hello_json, (size_t)hn)) { close(fd); cb->ws_fd = -1; return -1; }
    {
        unsigned char buf[2048]; size_t l = 0;
        int op = 0;
        if (xr_ws_wait_frame(&cb->ws, xr_runtime_config()->cloud_conn_ms,
                             buf, sizeof(buf), &l, &op) != 1) op = -1;
        if (op != 1) { fprintf(stderr, "[be-cloud] server hello 缺失\n"); close(fd); cb->ws_fd = -1; return -1; }
    }
    static const char listen_start[] =
        "{\"type\":\"listen\",\"state\":\"start\",\"mode\":\"auto\"}";
    if (xr_ws_send(&cb->ws, 1, listen_start, sizeof(listen_start) - 1)) { close(fd); cb->ws_fd = -1; return -1; }
    fprintf(stderr, "[be-cloud] 已连接 ws://%s:%d%s\n", cb->host, cb->port, cb->path);
    /* 重连补发上一轮指令(与 local 同语义: 至多重试一次) */
    if (cb->pending_len) {
        size_t retry = cb->pending_len;
        cb->pending_len = 0;
        if (xr_ws_send(&cb->ws, 1, cb->pending_msg, retry)) {
            fprintf(stderr, "[be-cloud] 重连后补发失败, 当前轮已丢弃\n");
        } else {
            fprintf(stderr, "[be-cloud] 重连后已补发当前轮指令\n");
            cb->pending_msg[0] = 0;
            cb->turn.state = XR_TURN_ACCEPTED; /* 重试沿用原轮次 id */
            return 2;
        }
    }
    return 0;
}
static void cb_disconnect(backend_t *b) {
    cloud_be_t *cb = (cloud_be_t *)b;
    cb->announce_pending[0] = 0;
    if (cb->ws_fd >= 0) { close(cb->ws_fd); cb->ws_fd = -1; }
}
/* 打断当前轮但不断连: 服务端 {"type":"abort"} 会停 LLM/TTS 并置 client_abort,
   音乐流的 cancelled() 立刻收敛(音乐停)。以前"停止音乐"只能 disconnect
   整条 WS -> 前端重连 + 重新标定, 用户观感是"播放时设备被切了一刀"。 */
static int cb_abort(backend_t *b) {
    cloud_be_t *cb = (cloud_be_t *)b;
    static const char abort_msg[] = "{\"type\":\"abort\"}";
    cb->announce_pending[0] = 0;
    cb->pending_len = 0; cb->pending_msg[0] = 0;
    cb->turn.state = XR_TURN_FAILED;
    cb->turn.audio_packets = 0;
    if (cb->ws_fd < 0) return -1;
    if (xr_ws_send(&cb->ws, 1, abort_msg, sizeof(abort_msg) - 1)) {
        cb->turn.state = XR_TURN_FAILED;
        return -1;
    }
    fprintf(stderr, "[be-cloud] 已发 abort(打断当前轮, 保持连接)\n");
    return 0;
}
static int cb_submit_text(backend_t *b, const char *text) {
    cloud_be_t *cb = (cloud_be_t *)b;
    cb->announce_pending[0] = 0;
    xr_turn_begin(&cb->turn);
    cb->pending_len = 0;
    /* 转义交给共用助手: ASR 文本原样来自模型 JSON, 必含换行/制表时
       旧的内联转义会拼出非法 JSON, 服务端拒收 -> 整轮静默丢失。 */
    char esc[1024];
    xr_json_escape(text, esc, sizeof esc);
    char msg[1400];
    int mn = snprintf(msg, sizeof(msg),
        "{\"type\":\"listen\",\"state\":\"detect\",\"text\":\"%s\"}", esc);
    if (xr_ws_send(&cb->ws, 1, msg, (size_t)mn)) {
        cb->turn.state = XR_TURN_FAILED;
        if ((size_t)mn <= sizeof(cb->pending_msg)) {
            memcpy(cb->pending_msg, msg, (size_t)mn);
            cb->pending_len = (size_t)mn;
        }
        return -1;
    }
    fprintf(stderr, "[be-cloud] turn=%llu submitted\n", (unsigned long long)cb->turn.id);
    return 0;
}
static backend_ev_t cb_poll_event(backend_t *b, unsigned char *buf, size_t cap,
                                  size_t *out_len, char *ev_text, size_t text_cap,
                                  int timeout_ms) {
    cloud_be_t *cb = (cloud_be_t *)b;
    size_t l = 0;
    int op = 0;
    int rc = xr_ws_recv_frame(&cb->ws, timeout_ms, buf, cap, &l, &op);
    if (rc == 0) return EV_TIMEOUT;
    if (rc < 0 || op == 8) { cb->turn.state = XR_TURN_FAILED; return EV_ERROR; }
    if (op == 9) { (void)xr_ws_send(&cb->ws, 10, buf, l); return EV_NONE; }
    if (op != 1) {
        if (op == 2 && xr_turn_accepted(&cb->turn)) {
            xr_turn_audio(&cb->turn); *out_len = l; return EV_TTS_AUDIO;
        }
        return EV_NONE;
    }
    size_t m = l < cap - 1 ? l : cap - 1;
    buf[m] = 0;
    int is_stop = strstr((char *)buf, "\"state\":\"stop\"") ||
                  strstr((char *)buf, "\"state\": \"stop\"");
    if (is_stop) {
        if (!xr_turn_accepted(&cb->turn)) {
            fprintf(stderr, "[be-cloud] 忽略迟到 stop(本轮尚无受理事件)\n"); return EV_NONE;
        }
        cb->turn.state = XR_TURN_FINISHED;
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
            int control = xr_announce_control((char *)buf, cb->announce_pending, &cb->turn);
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
        xr_turn_begin(&cb->turn);
        xr_turn_accept(&cb->turn);
        return EV_ANNOUNCE;
    }
    if (strstr((char *)buf, "\"type\":\"volume\"")
        || strstr((char *)buf, "\"type\": \"volume\"")) {
        /* 服务端音量指令(音频直传轮的意图判定在服务端): 客户端执行软件增益。
           ev_text 约定 "up|down|max|min|set:NN", 主循环映射后本地收轮。 */
        if (!xr_turn_accept(&cb->turn)) return EV_NONE;
        cb->turn.state = XR_TURN_FINISHED;
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
        /* 服务端静默丢弃(纯唤醒/噪声)。reason 决定前端收轮后是否开跟说窗:
           没有这条回执, 前端会等满 response_timeout 才收轮, 期间麦克风聋着,
           紧随其后的指令段整段丢失。 */
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
        if (!xr_turn_accept(&cb->turn)) return EV_NONE;
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
        /* llm 情绪/文本事件与 tts sentence_start 都走这里; detect 模式下网关
           不一定回显 stt, 任一文本事件都视为本轮已受理(stop 不再被当迟到) */
        if (!xr_turn_accept(&cb->turn)) return EV_NONE;
        if (ev_text && text_cap) snprintf(ev_text, text_cap, "%s", (char *)buf);
        return EV_TTS_START;
    }
    return EV_NONE;
}
static int cb_ping(backend_t *b) {
    cloud_be_t *cb = (cloud_be_t *)b;
    static const char ping[] = "{\"type\":\"ping\"}";
    return xr_ws_send(&cb->ws, 1, ping, sizeof(ping) - 1);
}
static int cb_wants_audio(backend_t *b) {
    return xr_runtime_config()->cloud_asr && ((cloud_be_t *)b)->ws_fd >= 0;
}
static int cb_submit_audio(backend_t *b, const backend_audio_request_t *r) {
    cloud_be_t *cb = (cloud_be_t *)b;
    if (!r || !r->pcm || !r->samples || r->samples > 16000u * 15 || !cb_wants_audio(b))
        return -1;
    int error;
    OpusEncoder *encoder = opus_encoder_create(16000, 1, OPUS_APPLICATION_VOIP, &error);
    if (!encoder) return -1;
    if (opus_encoder_ctl(encoder, OPUS_SET_BITRATE(24000)) != OPUS_OK) {
        opus_encoder_destroy(encoder); return -1;
    }
    cb->announce_pending[0] = 0;
    xr_turn_begin(&cb->turn); cb->pending_len = 0;
    const char start[] = "{\"type\":\"listen\",\"state\":\"start\",\"mode\":\"manual\"}";
    const char start_wake[] =
        "{\"type\":\"listen\",\"state\":\"start\",\"mode\":\"manual\",\"wake\":\"required\"}";
    int rc = xr_ws_send(&cb->ws, 1,
                        r->wake_required ? start_wake : start,
                        r->wake_required ? sizeof(start_wake) - 1 : sizeof(start) - 1);
    for (size_t off = 0; rc == 0 && off < r->samples; off += 960) {
        int16_t pcm[960] = {0}; unsigned char packet[1500];
        size_t n = r->samples - off < 960 ? r->samples - off : 960;
        memcpy(pcm, r->pcm + off, n * sizeof(*pcm));
        int bytes = opus_encode(encoder, pcm, 960, packet, sizeof(packet));
        rc = bytes > 0 ? xr_ws_send(&cb->ws, 2, packet, (size_t)bytes) : -1;
    }
    opus_encoder_destroy(encoder);
    const char stop[] = "{\"type\":\"listen\",\"state\":\"stop\"}";
    if (rc == 0) rc = xr_ws_send(&cb->ws, 1, stop, sizeof(stop) - 1);
    if (rc < 0) cb->turn.state = XR_TURN_FAILED;
    else fprintf(stderr, "[be-cloud] turn=%llu audio submitted: %zu samples\n",
                 (unsigned long long)cb->turn.id, r->samples);
    return rc;
}
static int cb_has_audio(backend_t *b) { return xr_turn_has_audio(&((cloud_be_t *)b)->turn); }
static int cb_seen_stt(backend_t *b) { return xr_turn_accepted(&((cloud_be_t *)b)->turn); }
static int cb_drop_turn(backend_t *b) {
    /* hybrid 交叉兜底用: 放弃本轮, 防止重连后补发旧指令 */
    cloud_be_t *cb = (cloud_be_t *)b;
    cb->announce_pending[0] = 0;
    cb->pending_len = 0; cb->pending_msg[0] = 0;
    cb->turn.state = XR_TURN_FAILED;
    cb->turn.audio_packets = 0;
    return 0;
}

static int cb_announce_reply(backend_t *b, const char *id, int accepted) {
    cloud_be_t *cb = (cloud_be_t *)b;
    return xr_announce_reply(&cb->ws, cb->announce_pending, id, accepted);
}
static const backend_ops_t CLOUD_OPS = {
    .create = NULL, .connect = cb_connect, .disconnect = cb_disconnect,
    .abort = cb_abort,
    .submit_text = cb_submit_text, .poll_event = cb_poll_event,
    .ping = cb_ping, .has_audio = cb_has_audio,
    .seen_stt = cb_seen_stt, .drop_turn = cb_drop_turn,
    .wants_audio = cb_wants_audio, .submit_audio = cb_submit_audio,
    .announce_reply = cb_announce_reply,
};
backend_t *be_open_cloud(void) {
    const char *url = getenv("XR_CLOUD_URL");
    if (!url || !*url) {
        fprintf(stderr, "[be-cloud] 缺少 XR_CLOUD_URL(形如 ws://192.168.1.10:8000/xiaorui/v1/)\n");
        return NULL;
    }
    if (!strncmp(url, "wss://", 6)) {
        fprintf(stderr, "[be-cloud] 暂不支持 wss(TLS), v1 面向 LAN 网关 ws://\n");
        return NULL;
    }
    if (strncmp(url, "ws://", 5)) {
        fprintf(stderr, "[be-cloud] XR_CLOUD_URL 须以 ws:// 开头\n");
        return NULL;
    }
    cloud_be_t *cb = calloc(1, sizeof(cloud_be_t));
    if (!cb) return NULL;
    cb->base.ops = &CLOUD_OPS;
    cb->ws_fd = -1;
    /* ws://host[:port][/path] */
    const char *p = url + 5, *slash = strchr(p, '/');
    size_t hl = slash ? (size_t)(slash - p) : strlen(p);
    if (hl == 0 || hl >= sizeof(cb->host)) { free(cb); fprintf(stderr, "[be-cloud] URL 主机段非法\n"); return NULL; }
    memcpy(cb->host, p, hl); cb->host[hl] = 0;
    snprintf(cb->path, sizeof(cb->path), "%s", slash ? slash : "/xiaorui/v1/");
    char *colon = strchr(cb->host, ':');
    cb->port = 80;
    if (colon) {
        long v = strtol(colon + 1, NULL, 10);
        if (v > 0 && v < 65536) { cb->port = (int)v; *colon = 0; }
        else { free(cb); fprintf(stderr, "[be-cloud] URL 端口非法\n"); return NULL; }
    }
    const char *tok = getenv("XR_CLOUD_TOKEN");
    if (tok && *tok)
        snprintf(cb->auth_hdr, sizeof(cb->auth_hdr), "Authorization: Bearer %s\r\n", tok);
    (void)xr_runtime_config();
    const char *did = getenv("XR_DEVICE_ID");
    snprintf(cb->device_id, sizeof(cb->device_id), "%s",
             (did && *did) ? did : "xiaorui-board");
    fprintf(stderr, "[be-cloud] 端点 ws://%s:%d%s%s\n", cb->host, cb->port, cb->path,
            cb->auth_hdr[0] ? " (带鉴权)" : "");
    return (backend_t *)cb;
}
