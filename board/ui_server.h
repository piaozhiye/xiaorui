#ifndef XR_UI_SERVER_H
#define XR_UI_SERVER_H

#include <stdbool.h>
#include "ui_protocol.h"

typedef struct xr_ui_server xr_ui_server_t;

xr_ui_server_t *xr_ui_server_open(const char *path);
void xr_ui_server_close(xr_ui_server_t *server);
/* 一轮的第一段内容(问句或回答首句)到达时调用: 清掉上一轮的字幕。
   注意不能在"进入 RESPOND"时清 —— 那种轮次可能被网关当噪声丢掉,
   清了就把用户刚问的和刚答的全擦了。 */
void xr_ui_server_turn_begin(xr_ui_server_t *server);
void xr_ui_server_state(xr_ui_server_t *server, enum xr_ui_state state, unsigned int volume);
/* text 为已解码的 UTF-8 明文(问句/回答分句), 不再传 JSON */
void xr_ui_server_text(xr_ui_server_t *server, bool question, const char *text);
int xr_ui_server_poll(xr_ui_server_t *server, unsigned int *volume);
/* 最近 window_ms 内有 UI 客户端问过状态 -> 前端可以缩短轮询间隔 */
int xr_ui_server_client_active(const xr_ui_server_t *server, unsigned int window_ms);

#endif
