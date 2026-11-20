#define _GNU_SOURCE
#include <errno.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "ui_server.h"

struct xr_ui_server {
    int descriptor;
    char path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    ino_t inode;
    dev_t device;
    bool bound;
    bool client_seen;
    /* 最近一次客户端的来源地址: UI 进程重启会换抽象地址(@xiaorui-ui.<pid>),
     * 用它区分"同一个客户端继续问"和"新客户端接入"(后者要清上一轮文本)。 */
    struct sockaddr_un client_peer;
    socklen_t client_peer_len;
    bool truncated;        /* 字幕缓冲曾溢出: 界面要标出"已截断" */
    uint64_t last_client_ms;
    xr_ui_message_t status;
};

static uint64_t monotonic_ms(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
        return 0;
    return (uint64_t)now.tv_sec * 1000 + (uint64_t)now.tv_nsec / 1000000;
}

/* 拷贝文本, 容量不够时回退到 UTF-8 字符边界, 不截半个汉字 */
static void copy_text(char *destination, size_t capacity, const char *source) {
    if (capacity == 0) return;
    size_t length = strlen(source);
    if (length >= capacity) {
        length = capacity - 1;
        while (length > 0 && ((unsigned char)source[length] & 0xc0) == 0x80)
            length--;
    }
    memcpy(destination, source, length);
    destination[length] = 0;
}

static int bind_endpoint(int descriptor, const struct sockaddr_un *address) {
    if (bind(descriptor, (const struct sockaddr *)address, sizeof(*address)) == 0)
        return 0;
    if (errno != EADDRINUSE) return -1;
    struct stat before;
    if (lstat(address->sun_path, &before) != 0 || !S_ISSOCK(before.st_mode) ||
        before.st_uid != geteuid()) return -1;
    int probe = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (probe < 0) return -1;
    int connected = connect(probe, (const struct sockaddr *)address, sizeof(*address));
    int connect_error = errno;
    close(probe);
    if (connected == 0)
    {
        errno = EADDRINUSE;
        return -1;
    }
    if (connect_error != ECONNREFUSED) { errno = connect_error; return -1; }
    struct stat current;
    if (lstat(address->sun_path, &current) != 0 || before.st_ino != current.st_ino ||
        before.st_dev != current.st_dev || unlink(address->sun_path) != 0) return -1;
    return bind(descriptor, (const struct sockaddr *)address, sizeof(*address));
}

xr_ui_server_t *xr_ui_server_open(const char *path) {
    if (!path || path[0] != '/' || strlen(path) >= sizeof(((struct sockaddr_un *)0)->sun_path))
        return NULL;
    char parent[sizeof(((struct sockaddr_un *)0)->sun_path)];
    memcpy(parent, path, strlen(path) + 1);
    char *slash = strrchr(parent, '/');
    if (!slash || slash == parent) return NULL;
    *slash = 0;
    if (mkdir(parent, 0700) != 0 && errno != EEXIST) return NULL;
    struct stat directory;
    if (lstat(parent, &directory) != 0 || !S_ISDIR(directory.st_mode) ||
        directory.st_uid != geteuid() || (directory.st_mode & 0077)) return NULL;
    xr_ui_server_t *server = calloc(1, sizeof(*server));
    if (!server) return NULL;
    server->descriptor = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (server->descriptor < 0) goto error;
    int enabled = 1;
    if (setsockopt(server->descriptor, SOL_SOCKET, SO_PASSCRED, &enabled, sizeof(enabled)) != 0)
        goto error;
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    memcpy(address.sun_path, path, strlen(path) + 1);
    if (bind_endpoint(server->descriptor, &address) != 0) goto error;
    memcpy(server->path, path, strlen(path) + 1);
    struct stat endpoint;
    if (lstat(path, &endpoint) != 0) goto error;
    server->inode = endpoint.st_ino;
    server->device = endpoint.st_dev;
    server->bound = true;
    if (chmod(path, 0600) != 0) goto error;
    server->status = (xr_ui_message_t){.magic = XR_UI_MAGIC, .version = XR_UI_VERSION,
                                      .type = XR_UI_STATUS, .volume = 100};
    return server;
error:
    xr_ui_server_close(server);
    return NULL;
}

void xr_ui_server_close(xr_ui_server_t *server) {
    if (!server) return;
    if (server->descriptor >= 0) close(server->descriptor);
    struct stat endpoint;
    if (server->bound && lstat(server->path, &endpoint) == 0 &&
        endpoint.st_ino == server->inode && endpoint.st_dev == server->device)
        unlink(server->path);
    free(server);
}

void xr_ui_server_turn_begin(xr_ui_server_t *server) {
    if (!server) return;
    server->status.question[0] = 0;
    server->status.answer[0] = 0;
    server->truncated = 0;   /* 新一轮: 截断标记复位 */
}

void xr_ui_server_state(xr_ui_server_t *server, enum xr_ui_state state, unsigned int volume) {
    if (!server || state > XR_UI_PREPARING || volume < 10 || volume > 100) return;
    server->status.state = (uint32_t)state;
    server->status.volume = volume;
}

void xr_ui_server_level(xr_ui_server_t *server, unsigned int level) {
    if (!server) return;
    server->status.level = level > 100 ? 100 : level;
}

void xr_ui_server_text(xr_ui_server_t *server, bool question, const char *text) {
    if (!server || !text || !text[0]) return;
    if (question) {
        copy_text(server->status.question, sizeof(server->status.question), text);
        return;
    }
    size_t used = strlen(server->status.answer);
    size_t available = sizeof(server->status.answer) - used;
    /* 尾部固定留 5 字节("…\n" + NUL)放截断标记: 字幕满了的时候把末尾
     * 标成"...\n", 界面上看得出回答被截断, 而不是"回答到这就结束了"
     * (静默丢弃最坏: 截断冒充完整)。 */
    const size_t marker_room = 5;
    size_t payload = available > marker_room ? available - marker_room : 0;
    if (payload <= 2) {
        /* 放不下一句了: 标记截断(只标记一次, 之后都丢弃) */
        if (!server->truncated) {
            server->truncated = 1;
            fprintf(stderr, "[ui] 字幕缓冲已满(%zu 字节), 后续字幕将丢弃\n",
                    sizeof(server->status.answer) - 1);
            /* 回退到最后一个换行处再写标记, 保住已有内容; 一整句没有换行
             * 点就直接接在末尾(别把整段清掉)。 */
            size_t cut = used;
            while (cut > 0 && server->status.answer[cut - 1] != '\n') cut--;
            if (cut == 0) {
                if (available >= marker_room) {
                    memcpy(server->status.answer + used, "...\n", 4);
                    server->status.answer[used + 4] = 0;
                }
            } else {
                memcpy(server->status.answer + cut, "...\n", 4);
                server->status.answer[cut + 4] = 0;
            }
        }
        return;
    }
    if (used && server->status.answer[used - 1] != '\n')
    {
        server->status.answer[used++] = '\n';
        server->status.answer[used] = 0;
        payload--;
    }
    copy_text(server->status.answer + used, payload, text);
}

int xr_ui_server_client_active(const xr_ui_server_t *server, unsigned int window_ms) {
    if (!server || !server->client_seen) return 0;
    uint64_t now = monotonic_ms();
    if (now < server->last_client_ms) return 1;
    return now - server->last_client_ms < window_ms;
}

int xr_ui_server_poll(xr_ui_server_t *server, unsigned int *volume) {
    if (!server || !volume) return 0;
    for (int index = 0; index < 8; index++)
    {
        xr_ui_message_t request;
        struct sockaddr_un peer = {0};
        struct iovec vector = {.iov_base = &request, .iov_len = sizeof(request)};
        union { struct cmsghdr alignment; char bytes[CMSG_SPACE(sizeof(struct ucred))]; } control;
        struct msghdr message = {.msg_name = &peer, .msg_namelen = sizeof(peer),
                                  .msg_iov = &vector, .msg_iovlen = 1,
                                  .msg_control = control.bytes, .msg_controllen = sizeof(control)};
        ssize_t size = recvmsg(server->descriptor, &message, MSG_DONTWAIT | MSG_TRUNC);
        if (size < 0) break;
        if (size != (ssize_t)sizeof(request) || message.msg_flags & (MSG_TRUNC | MSG_CTRUNC) ||
            message.msg_namelen <= offsetof(struct sockaddr_un, sun_path) ||
            message.msg_namelen > sizeof(peer) || peer.sun_family != AF_UNIX ||
            request.magic != XR_UI_MAGIC || request.version != XR_UI_VERSION) continue;
        bool trusted = false;
        for (struct cmsghdr *header = CMSG_FIRSTHDR(&message); header;
             header = CMSG_NXTHDR(&message, header))
        {
            if (header->cmsg_level == SOL_SOCKET && header->cmsg_type == SCM_CREDENTIALS &&
                header->cmsg_len == CMSG_LEN(sizeof(struct ucred)))
            {
                struct ucred credential;
                memcpy(&credential, CMSG_DATA(header), sizeof(credential));
                trusted = credential.uid == geteuid();
            }
        }
        if (!trusted) continue;
        /* 新客户端判定: 没见过客户端, 或来源地址变了(UI 进程重启 -> 抽象
         * 地址里的 pid 变)。新客户端 = 新会话开始 -> 清掉上一轮问答文本,
         * 否则 UI 重启后会把旧对话原样显示出来(2026-11-20 用户反馈)。 */
        bool new_client = !server->client_seen ||
            server->client_peer_len != message.msg_namelen ||
            memcmp(&server->client_peer, &peer, message.msg_namelen) != 0;
        server->client_seen = true;
        server->last_client_ms = monotonic_ms();
        if (new_client) {
            fprintf(stderr, "[ui] 新客户端接入, 清空上一轮文本(peer_len=%u)\n",
                    (unsigned)message.msg_namelen);
            server->status.question[0] = 0;
            server->status.answer[0] = 0;
            server->client_peer = peer;
            server->client_peer_len = message.msg_namelen;
        }
        if (request.type == XR_UI_QUERY)
        {
            ssize_t sent = sendto(server->descriptor, &server->status, sizeof(server->status),
                                  MSG_DONTWAIT | MSG_NOSIGNAL, (struct sockaddr *)&peer,
                                  message.msg_namelen);
            if (sent < 0) continue;
        }
        else if (request.type == XR_UI_STOP)
            return XR_UI_STOP;
        else if (request.type == XR_UI_VOLUME && request.volume >= 10 && request.volume <= 100)
        {
            *volume = request.volume;
            return XR_UI_VOLUME;
        }
    }
    return 0;
}
