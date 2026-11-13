#define _GNU_SOURCE
#include <errno.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include "ui_json.h"
#include "ui_server.h"

struct xr_ui_server {
    int descriptor;
    char path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    ino_t inode;
    dev_t device;
    bool bound;
    xr_ui_message_t status;
};

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

void xr_ui_server_begin_turn(xr_ui_server_t *server) {
    if (!server) return;
    server->status.question[0] = 0;
    server->status.answer[0] = 0;
}

void xr_ui_server_state(xr_ui_server_t *server, enum xr_ui_state state, unsigned int volume) {
    if (!server || state > XR_UI_SPEAKING || volume < 10 || volume > 100) return;
    server->status.state = (uint32_t)state;
    server->status.volume = volume;
}

void xr_ui_server_text(xr_ui_server_t *server, bool question, const char *json) {
    if (!server || !json) return;
    char text[sizeof(server->status.answer)];
    if (xr_ui_json_string(json, "text", text, sizeof(text)) != 1 || !text[0]) return;
    if (question)
    {
        if (xr_ui_json_string(json, "text", server->status.question,
                              sizeof(server->status.question)) != 1)
            server->status.question[0] = 0;
    }
    else
    {
        char state[32];
        if (xr_ui_json_string(json, "state", state, sizeof(state)) != 1 ||
            strcmp(state, "sentence_start")) return;
        size_t used = strlen(server->status.answer);
        size_t available = sizeof(server->status.answer) - used;
        char segment[sizeof(server->status.answer)];
        if (xr_ui_json_string(json, "text", segment, available) != 1) return;
        memcpy(server->status.answer + used, segment, strlen(segment) + 1);
    }
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
