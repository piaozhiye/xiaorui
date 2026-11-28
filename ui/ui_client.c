#define _GNU_SOURCE
#include <errno.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "ui_client.h"

static int descriptor = -1;
static struct sockaddr_un server;
static uint64_t last_query;
static uint64_t last_tts_query;
static uint64_t last_reply;
static int tts_gain = -1;
static bool received;
static xr_ui_message_t status;

bool xr_ui_message_valid(const xr_ui_message_t *message, unsigned int length) {
    return message && length == sizeof(*message) && message->magic == XR_UI_MAGIC &&
           message->version == XR_UI_VERSION && message->type == XR_UI_STATUS &&
           message->state <= XR_UI_VOICE_ERROR && message->volume >= 10 && message->volume <= 100 &&
           memchr(message->question, 0, sizeof(message->question)) &&
           memchr(message->answer, 0, sizeof(message->answer));
}

int xr_ui_client_open(const char *path) {
    if (!path || path[0] != '/' || strlen(path) >= sizeof(server.sun_path) || descriptor >= 0)
        return -1;
    descriptor = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (descriptor < 0)
        return -1;
    struct sockaddr_un local = {.sun_family = AF_UNIX};
    int length = snprintf(local.sun_path + 1, sizeof(local.sun_path) - 1,
                          "xiaorui-ui.%ld", (long)getpid());
    if (length < 0 || bind(descriptor, (struct sockaddr *)&local,
                          (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1 + length)) != 0)
    {
        xr_ui_client_close();
        return -1;
    }
    server.sun_family = AF_UNIX;
    memcpy(server.sun_path, path, strlen(path) + 1);
    return 0;
}

int xr_ui_client_command(enum xr_ui_message_type type, unsigned int value) {
    if (descriptor < 0 || (type != XR_UI_QUERY && type != XR_UI_TTS_QUERY &&
                           type != XR_UI_VOLUME && type != XR_UI_TTS_GAIN &&
                           type != XR_UI_STOP && type != XR_UI_RESET))
        return -1;
    if (type == XR_UI_VOLUME && (value < 10 || value > 100))
        return -1;
    if (type == XR_UI_TTS_GAIN && (value < 1 || value > 100))
        return -1;
    if (type == XR_UI_TTS_QUERY && value != 0)
        return -1;
    xr_ui_message_t message = {.magic = XR_UI_MAGIC, .version = XR_UI_VERSION,
                               .type = (uint32_t)type, .volume = value};
    ssize_t sent = sendto(descriptor, &message, sizeof(message), MSG_DONTWAIT | MSG_NOSIGNAL,
                          (struct sockaddr *)&server, sizeof(server));
    return sent == (ssize_t)sizeof(message) ? 0 : -1;
}

void xr_ui_client_tick(uint64_t now_ms) {
    if (descriptor < 0)
        return;
    /* 100ms 问一次: 麦克风电平要跟得上, 界面才有"在听"的动态 */
    if (now_ms - last_query >= 100)
    {
        last_query = now_ms;
        if (xr_ui_client_command(XR_UI_QUERY, 0) != 0 &&
            errno != ENOENT && errno != ECONNREFUSED && errno != EAGAIN)
            fprintf(stderr, "[ui] status query failed: %s\n", strerror(errno));
    }
    if (now_ms - last_tts_query >= 1000)
    {
        last_tts_query = now_ms;
        if (xr_ui_client_command(XR_UI_TTS_QUERY, 0) != 0 &&
            errno != ENOENT && errno != ECONNREFUSED && errno != EAGAIN)
            fprintf(stderr, "[ui] tts gain query failed: %s\n", strerror(errno));
    }
    for (int index = 0; index < 8; index++)
    {
        xr_ui_message_t message;
        struct sockaddr_un peer = {0};
        socklen_t peer_length = sizeof(peer);
        ssize_t size = recvfrom(descriptor, &message, sizeof(message), MSG_DONTWAIT | MSG_TRUNC,
                                (struct sockaddr *)&peer, &peer_length);
        if (size < 0)
            break;
        if (peer.sun_family == AF_UNIX && !memcmp(peer.sun_path, server.sun_path,
                                                sizeof(server.sun_path)) &&
            size == (ssize_t)sizeof(message) && message.magic == XR_UI_MAGIC &&
            message.version == XR_UI_VERSION && message.type == XR_UI_TTS_STATUS &&
            message.volume >= 10 && message.volume <= 100)
        {
            tts_gain = (int)message.volume;
            last_reply = now_ms;
        }
        else if (peer.sun_family == AF_UNIX && !memcmp(peer.sun_path, server.sun_path,
                                                sizeof(server.sun_path)) &&
            xr_ui_message_valid(&message, (unsigned int)size))
        {
            status = message;
            received = true;
            last_reply = now_ms;
        }
    }
}

bool xr_ui_client_connected(uint64_t now_ms) {
    return received && now_ms >= last_reply && now_ms - last_reply < 2500;
}

const xr_ui_message_t *xr_ui_client_status(void) {
    return &status;
}

int xr_ui_client_tts_gain(void) { return tts_gain; }

void xr_ui_client_close(void) {
    if (descriptor >= 0)
        close(descriptor);
    descriptor = -1;
    received = false;
    last_query = 0;
    last_tts_query = 0;
    last_reply = 0;
    tts_gain = -1;
    memset(&server, 0, sizeof(server));
    memset(&status, 0, sizeof(status));
}
