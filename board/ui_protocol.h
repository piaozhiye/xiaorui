#ifndef XR_UI_PROTOCOL_H
#define XR_UI_PROTOCOL_H

#include <stdint.h>

#define XR_UI_MAGIC UINT32_C(0x58525549)
#define XR_UI_VERSION 1

enum xr_ui_message_type { XR_UI_QUERY = 1, XR_UI_STATUS, XR_UI_VOLUME, XR_UI_STOP };
enum xr_ui_state { XR_UI_READY, XR_UI_LISTENING, XR_UI_THINKING, XR_UI_SPEAKING };

typedef struct {
    uint32_t magic;
    uint32_t version;
    uint32_t type;
    uint32_t state;
    uint32_t volume;
    char question[256];
    char answer[4096];
} xr_ui_message_t;

_Static_assert(sizeof(xr_ui_message_t) == 4372, "Unexpected UI wire layout");

#endif
