#ifndef XR_UI_SERVER_H
#define XR_UI_SERVER_H

#include <stdbool.h>
#include "ui_protocol.h"

typedef struct xr_ui_server xr_ui_server_t;

xr_ui_server_t *xr_ui_server_open(const char *path);
void xr_ui_server_close(xr_ui_server_t *server);
void xr_ui_server_begin_turn(xr_ui_server_t *server);
void xr_ui_server_state(xr_ui_server_t *server, enum xr_ui_state state, unsigned int volume);
void xr_ui_server_text(xr_ui_server_t *server, bool question, const char *json);
int xr_ui_server_poll(xr_ui_server_t *server, unsigned int *volume);

#endif
