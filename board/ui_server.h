#ifndef XR_UI_SERVER_H
#define XR_UI_SERVER_H

#include <stdbool.h>
#include "ui_protocol.h"

typedef struct xr_ui_server xr_ui_server_t;

xr_ui_server_t *xr_ui_server_open(const char *path);
void xr_ui_server_close(xr_ui_server_t *server);
void xr_ui_server_begin_turn(xr_ui_server_t *server);
void xr_ui_server_state(xr_ui_server_t *server, enum xr_ui_state state, unsigned int volume);
/* text 为已解码的 UTF-8 明文(问句/回答分句), 不再传 JSON */
void xr_ui_server_text(xr_ui_server_t *server, bool question, const char *text);
int xr_ui_server_poll(xr_ui_server_t *server, unsigned int *volume);
/* 最近 window_ms 内有 UI 客户端问过状态 -> 前端可以缩短轮询间隔 */
int xr_ui_server_client_active(const xr_ui_server_t *server, unsigned int window_ms);

#endif
