#ifndef XR_UI_PROTOCOL_H
#define XR_UI_PROTOCOL_H

#include <stdint.h>

#define XR_UI_MAGIC UINT32_C(0x58525549)
#define XR_UI_VERSION 1
#define XR_UI_ANSWER_CAPACITY 4096

enum xr_ui_message_type { XR_UI_QUERY = 1, XR_UI_STATUS, XR_UI_VOLUME, XR_UI_STOP };
enum xr_ui_state { XR_UI_READY, XR_UI_LISTENING, XR_UI_THINKING, XR_UI_SPEAKING };

typedef struct {
    uint32_t magic;
    uint32_t version;
    uint32_t type;
    uint32_t state;
    uint32_t volume;
    uint32_t level;      /* 麦克风电平 0..100, 给界面做反馈 */
    char question[256];
    char answer[XR_UI_ANSWER_CAPACITY];
} xr_ui_message_t;

_Static_assert(sizeof(xr_ui_message_t) == 4376, "Unexpected UI wire layout");

#endif
