#ifndef XR_UI_CLIENT_H
#define XR_UI_CLIENT_H

#include <stdbool.h>
#include <stdint.h>
#include "../board/ui_protocol.h"

int xr_ui_client_open(const char *path);
void xr_ui_client_tick(uint64_t now_ms);
bool xr_ui_client_connected(uint64_t now_ms);
const xr_ui_message_t *xr_ui_client_status(void);
int xr_ui_client_command(enum xr_ui_message_type type, unsigned int value);
/* UI 启动时调用: 通知前端清掉上一轮字幕(新会话)。 */
static inline int xr_ui_client_reset(void) { return xr_ui_client_command(XR_UI_RESET, 0); }
bool xr_ui_message_valid(const xr_ui_message_t *message, unsigned int length);
void xr_ui_client_close(void);

#endif
