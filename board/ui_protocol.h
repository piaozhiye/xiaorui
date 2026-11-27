#ifndef XR_UI_PROTOCOL_H
#define XR_UI_PROTOCOL_H

#include <stdint.h>

#define XR_UI_MAGIC UINT32_C(0x58525549)
#define XR_UI_VERSION 1
#define XR_UI_ANSWER_CAPACITY 4096

/* XR_UI_RESET: UI 进程启动时主动发一条, 告诉前端"新会话开始, 清掉上一轮
 * 字幕"。不依赖时间阈值(UI 快速重启 <3s 时旧文本会残留)。 */
enum xr_ui_message_type { XR_UI_QUERY = 1, XR_UI_STATUS, XR_UI_VOLUME, XR_UI_STOP,
                           XR_UI_RESET, XR_UI_TTS_GAIN, XR_UI_TTS_QUERY,
                           XR_UI_TTS_STATUS };
/* XR_UI_PREPARING(2026-11-20): 语音栈刚起、模型还在加载时, 界面要说实话
 * —— 显示"小瑞正在准备语音…", 而不是"我在听，请说"(说了没反应, 用户
 * 只会以为坏了)。加在末尾, 不改已有取值。 */
enum xr_ui_state { XR_UI_READY, XR_UI_LISTENING, XR_UI_THINKING, XR_UI_SPEAKING,
                    XR_UI_PREPARING, XR_UI_VOICE_ERROR };

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
