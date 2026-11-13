#ifndef XR_WAYLAND_PORT_H
#define XR_WAYLAND_PORT_H

#include <stdbool.h>
#include <lvgl/lvgl.h>

#define XR_UI_WIDTH 1080
#define XR_UI_HEIGHT 1920

int xr_display_open(bool headless);
/* 阻塞等 wayland 事件, 最多 timeout_ms; 返回 0=正常, -1=断了 */
int xr_display_pump(int timeout_ms);
int xr_display_dump(const char *path);
void xr_display_close(void);

#endif
