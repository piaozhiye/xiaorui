#ifndef XR_ANNOUNCE_PROTOCOL_H
#define XR_ANNOUNCE_PROTOCOL_H
#include <string.h>
#include <stdio.h>
#include "ws_transport.h"
#include "backend_turn.h"

/* Request ids are UUID hex, never user text. */
static inline int xr_announce_id(const char *json, char id[33]) {
    const char *p = strstr(json, "\"request_id\"");
    if (!p) return 0;
    p += 12;
    while (*p == ' ') ++p;
    if (*p++ != ':') return 0;
    while (*p == ' ') ++p;
    if (*p++ != '"') return 0;
    for (int i = 0; i < 32; ++i) {
        if (!((p[i] >= '0' && p[i] <= '9') || (p[i] >= 'a' && p[i] <= 'f'))) return 0;
        id[i] = p[i];
    }
    if (p[32] != '"') return 0;
    id[32] = 0;
    return 1;
}

static inline int xr_announce_reply(xr_ws_t *ws, char pending[33],
                                    const char *id, int accepted) {
    if (!id || strlen(id) != 32) return -1;
    for (int i = 0; i < 32; ++i)
        if (!((id[i] >= '0' && id[i] <= '9') || (id[i] >= 'a' && id[i] <= 'f'))) return -1;
    char message[160];
    int n = snprintf(message, sizeof(message),
        "{\"type\":\"listen\",\"state\":\"announce_ack\",\"request_id\":\"%s\",\"accepted\":%s}",
        id, accepted ? "true" : "false");
    if (xr_ws_send(ws, 1, message, (size_t)n)) return -1;
    if (accepted) memcpy(pending, id, 33);
    return 0;
}

/* Grant/cancel only the matching reservation, never a later user turn. */
static inline int xr_announce_control(const char *json, char pending[33], xr_turn_t *turn) {
    char id[33];
    if (!pending[0] || !xr_announce_id(json, id) || strcmp(id, pending)) return 0;
    if (strstr(json, "\"state\":\"grant\"") || strstr(json, "\"state\": \"grant\"")) {
        pending[0] = 0;
        xr_turn_begin(turn); xr_turn_accept(turn);
        return 2;
    }
    if (strstr(json, "\"state\":\"cancel\"") || strstr(json, "\"state\": \"cancel\"")) {
        pending[0] = 0;
        turn->state = XR_TURN_FINISHED;
        return 1;
    }
    return 0;
}
#endif
