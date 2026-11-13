#ifndef XR_WAYLAND_PORT_H
#define XR_WAYLAND_PORT_H

#include <stdbool.h>
#include <lvgl/lvgl.h>

#define XR_UI_WIDTH 1080
#define XR_UI_HEIGHT 1920

int xr_display_open(bool headless);
int xr_display_pump(void);
int xr_display_dump(const char *path);
void xr_display_close(void);

#endif
