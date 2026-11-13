#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>

#include "wayland_port.h"
#include "xdg-shell-client-protocol.h"

typedef struct {
    struct wl_buffer *buffer;
    uint32_t *pixels;
    bool busy;
    bool presented;
    lv_area_t pending;
    bool has_pending;
} frame_t;

typedef struct {
    lv_point_t point;
    bool pressed;
} touch_event_t;

static struct {
    struct wl_display *display;
    struct wl_registry *registry;
    struct wl_compositor *compositor;
    struct wl_shm *shm;
    struct wl_seat *seat;
    struct wl_touch *touch;
    struct xdg_wm_base *shell;
    struct wl_surface *surface;
    struct xdg_surface *xdg_surface;
    struct xdg_toplevel *toplevel;
    frame_t frames[2];
    uint32_t *pixels;
    lv_color_t *draw_pixels;
    bool configured;
    bool closed;
    bool pending;
    bool pressed;
    int touch_id;
    lv_point_t point;
    touch_event_t touch_events[16];
    unsigned int touch_head;
    unsigned int touch_count;
    lv_area_t dirty;
    bool dirty_valid;
} port;

static const size_t frame_bytes = XR_UI_WIDTH * XR_UI_HEIGHT * sizeof(uint32_t);

static void rect_union(lv_area_t *target, bool *valid, const lv_area_t *area) {
    if (!*valid) {
        *target = *area;
        *valid = true;
        return;
    }
    if (area->x1 < target->x1) target->x1 = area->x1;
    if (area->y1 < target->y1) target->y1 = area->y1;
    if (area->x2 > target->x2) target->x2 = area->x2;
    if (area->y2 > target->y2) target->y2 = area->y2;
}

static void copy_rect(uint32_t *destination, const lv_area_t *area) {
    int width = area->x2 - area->x1 + 1;
    for (int row = area->y1; row <= area->y2; row++)
        memcpy(destination + (size_t)row * XR_UI_WIDTH + area->x1,
               port.pixels + (size_t)row * XR_UI_WIDTH + area->x1,
               (size_t)width * 4);
}

static void buffer_release(void *data, struct wl_buffer *buffer) {
    (void)buffer;
    frame_t *frame = (frame_t *)data;
    frame->busy = false;
    if (frame->has_pending && frame->presented) {
        copy_rect(frame->pixels, &frame->pending);
        frame->has_pending = false;
    }
}

static const struct wl_buffer_listener buffer_listener = {.release = buffer_release};

static int create_frame(frame_t *frame) {
    int descriptor = memfd_create("xiaorui-ui-frame", MFD_CLOEXEC);
    if (descriptor < 0)
        return -1;
    if (ftruncate(descriptor, (off_t)frame_bytes) != 0)
    {
        close(descriptor);
        return -1;
    }
    frame->pixels = mmap(NULL, frame_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, 0);
    if (frame->pixels == MAP_FAILED)
    {
        frame->pixels = NULL;
        close(descriptor);
        return -1;
    }
    struct wl_shm_pool *pool = wl_shm_create_pool(port.shm, descriptor, (int)frame_bytes);
    close(descriptor);
    if (!pool)
        return -1;
    frame->buffer = wl_shm_pool_create_buffer(pool, 0, XR_UI_WIDTH, XR_UI_HEIGHT,
                                             XR_UI_WIDTH * 4, WL_SHM_FORMAT_XRGB8888);
    wl_shm_pool_destroy(pool);
    if (!frame->buffer)
        return -1;
    return wl_buffer_add_listener(frame->buffer, &buffer_listener, frame);
}

static void present(void) {
    if (!port.pending || !port.configured || port.closed || !port.dirty_valid)
        return;
    frame_t *target = NULL;
    for (size_t index = 0; index < 2; index++)
        if (port.frames[index].buffer && !port.frames[index].busy) {
            target = &port.frames[index];
            break;
        }
    if (!target)
        return;
    lv_area_t area = port.dirty;
    if (!target->presented) {
        memcpy(target->pixels, port.pixels, frame_bytes);
        area.x1 = 0;
        area.y1 = 0;
        area.x2 = XR_UI_WIDTH - 1;
        area.y2 = XR_UI_HEIGHT - 1;
    } else {
        copy_rect(target->pixels, &area);
    }
    frame_t *other = (target == &port.frames[0]) ? &port.frames[1] : &port.frames[0];
    if (other->buffer) {
        if (other->busy)
            rect_union(&other->pending, &other->has_pending, &area);
        else if (other->presented)
            copy_rect(other->pixels, &area);
    }
    target->presented = true;
    wl_surface_attach(port.surface, target->buffer, 0, 0);
    wl_surface_damage(port.surface, area.x1, area.y1,
                      area.x2 - area.x1 + 1, area.y2 - area.y1 + 1);
    wl_surface_commit(port.surface);
    target->busy = true;
    target->has_pending = false;
    port.pending = false;
    port.dirty_valid = false;
}

static void flush(lv_disp_drv_t *driver, const lv_area_t *area, lv_color_t *colors) {
    int width = area->x2 - area->x1 + 1;
    for (int row = area->y1; row <= area->y2; row++)
    {
        memcpy(port.pixels + (size_t)row * XR_UI_WIDTH + area->x1,
               colors + (size_t)(row - area->y1) * width, (size_t)width * 4);
    }
    rect_union(&port.dirty, &port.dirty_valid, area);
    if (lv_disp_flush_is_last(driver))
    {
        port.pending = true;
        present();
    }
    lv_disp_flush_ready(driver);
}

static void read_touch(lv_indev_drv_t *driver, lv_indev_data_t *data) {
    (void)driver;
    data->point = port.point;
    data->state = port.pressed ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
    if (port.touch_count)
    {
        const touch_event_t *event = &port.touch_events[port.touch_head];
        data->point = event->point;
        data->state = event->pressed ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
        port.touch_head = (port.touch_head + 1) % 16;
        port.touch_count--;
    }
    data->continue_reading = port.touch_count > 0;
}

static void queue_touch(void) {
    if (port.touch_count == 16)
    {
        port.touch_head = 0;
        port.touch_count = 0;
        port.pressed = false;
    }
    unsigned int tail = (port.touch_head + port.touch_count) % 16;
    port.touch_events[tail] = (touch_event_t){.point = port.point, .pressed = port.pressed};
    port.touch_count++;
}

static void set_point(wl_fixed_t position_x, wl_fixed_t position_y) {
    int coordinate_x = wl_fixed_to_int(position_x);
    int coordinate_y = wl_fixed_to_int(position_y);
    port.point.x = coordinate_x < 0 ? 0 :
                   coordinate_x >= XR_UI_WIDTH ? XR_UI_WIDTH - 1 : coordinate_x;
    port.point.y = coordinate_y < 0 ? 0 :
                   coordinate_y >= XR_UI_HEIGHT ? XR_UI_HEIGHT - 1 : coordinate_y;
}

static void touch_down(void *data, struct wl_touch *touch, uint32_t serial, uint32_t time,
                       struct wl_surface *surface, int32_t id, wl_fixed_t position_x,
                       wl_fixed_t position_y) {
    (void)data;
    (void)touch;
    (void)serial;
    (void)time;
    if (surface != port.surface || port.pressed)
        return;
    port.touch_id = id;
    set_point(position_x, position_y);
    port.pressed = true;
    queue_touch();
    fprintf(stderr, "[ui] touch down %d,%d\n", port.point.x, port.point.y);
}

static void touch_up(void *data, struct wl_touch *touch, uint32_t serial, uint32_t time,
                     int32_t id) {
    (void)data;
    (void)touch;
    (void)serial;
    (void)time;
    if (id == port.touch_id)
    {
        port.pressed = false;
        queue_touch();
    }
}

static void touch_motion(void *data, struct wl_touch *touch, uint32_t time, int32_t id,
                         wl_fixed_t position_x, wl_fixed_t position_y) {
    (void)data;
    (void)touch;
    (void)time;
    if (port.pressed && id == port.touch_id)
        set_point(position_x, position_y);
}

static void touch_frame(void *data, struct wl_touch *touch) {
    (void)data;
    (void)touch;
}

static void touch_cancel(void *data, struct wl_touch *touch) {
    (void)data;
    (void)touch;
    port.pressed = false;
    port.touch_head = 0;
    port.touch_count = 0;
    queue_touch();
}

static const struct wl_touch_listener touch_listener = {
    .down = touch_down, .up = touch_up, .motion = touch_motion,
    .frame = touch_frame, .cancel = touch_cancel,
};

static void seat_capabilities(void *data, struct wl_seat *seat, uint32_t capabilities) {
    (void)data;
    if ((capabilities & WL_SEAT_CAPABILITY_TOUCH) && !port.touch)
    {
        port.touch = wl_seat_get_touch(seat);
        if (!port.touch || wl_touch_add_listener(port.touch, &touch_listener, NULL) != 0)
            port.closed = true;
    }
    else if (!(capabilities & WL_SEAT_CAPABILITY_TOUCH) && port.touch)
    {
        wl_touch_destroy(port.touch);
        port.touch = NULL;
        port.pressed = false;
        queue_touch();
    }
}

static void seat_name(void *data, struct wl_seat *seat, const char *name) {
    (void)data;
    (void)seat;
    (void)name;
}

static const struct wl_seat_listener seat_listener = {
    .capabilities = seat_capabilities, .name = seat_name,
};

static void shell_ping(void *data, struct xdg_wm_base *shell, uint32_t serial) {
    (void)data;
    xdg_wm_base_pong(shell, serial);
}

static const struct xdg_wm_base_listener shell_listener = {.ping = shell_ping};

static void registry_add(void *data, struct wl_registry *registry, uint32_t name,
                         const char *interface, uint32_t version) {
    (void)data;
    if (!strcmp(interface, "wl_compositor"))
        port.compositor = wl_registry_bind(registry, name, &wl_compositor_interface, 1);
    else if (!strcmp(interface, "wl_shm"))
        port.shm = wl_registry_bind(registry, name, &wl_shm_interface, 1);
    else if (!strcmp(interface, "xdg_wm_base"))
    {
        port.shell = wl_registry_bind(registry, name, &xdg_wm_base_interface, 1);
        if (!port.shell || xdg_wm_base_add_listener(port.shell, &shell_listener, NULL) != 0)
            port.closed = true;
    }
    else if (!strcmp(interface, "wl_seat") && !port.seat)
    {
        port.seat = wl_registry_bind(registry, name, &wl_seat_interface, version < 5 ? version : 5);
        if (!port.seat || wl_seat_add_listener(port.seat, &seat_listener, NULL) != 0)
            port.closed = true;
    }
}

static void registry_remove(void *data, struct wl_registry *registry, uint32_t name) {
    (void)data;
    (void)registry;
    (void)name;
}

static const struct wl_registry_listener registry_listener = {
    .global = registry_add, .global_remove = registry_remove,
};

static void configure_surface(void *data, struct xdg_surface *surface, uint32_t serial) {
    (void)data;
    xdg_surface_ack_configure(surface, serial);
    port.configured = true;
}

static const struct xdg_surface_listener surface_listener = {.configure = configure_surface};

static void configure_toplevel(void *data, struct xdg_toplevel *toplevel, int32_t width,
                               int32_t height, struct wl_array *states) {
    (void)data;
    (void)toplevel;
    (void)states;
    if (width > 0 && height > 0 && (width != XR_UI_WIDTH || height != XR_UI_HEIGHT))
    {
        fprintf(stderr, "[ui] unsupported surface size: %dx%d\n", width, height);
        port.closed = true;
    }
}

static void close_toplevel(void *data, struct xdg_toplevel *toplevel) {
    (void)data;
    (void)toplevel;
    port.closed = true;
}

static const struct xdg_toplevel_listener toplevel_listener = {
    .configure = configure_toplevel, .close = close_toplevel,
};

int xr_display_open(bool headless) {
    _Static_assert(sizeof(lv_color_t) == sizeof(uint32_t), "LVGL requires 32-bit color");
    port.pixels = calloc(1, frame_bytes);
    port.draw_pixels = calloc(XR_UI_WIDTH * 80, sizeof(lv_color_t));
    if (!port.pixels || !port.draw_pixels)
        goto error;
    if (!headless)
    {
        port.display = wl_display_connect(NULL);
        if (!port.display)
            goto error;
        port.registry = wl_display_get_registry(port.display);
        if (!port.registry ||
            wl_registry_add_listener(port.registry, &registry_listener, NULL) != 0)
            goto error;
        if (wl_display_roundtrip(port.display) < 0 || !port.compositor || !port.shm ||
            !port.shell || port.closed)
            goto error;
        port.surface = wl_compositor_create_surface(port.compositor);
        if (!port.surface)
            goto error;
        port.xdg_surface = xdg_wm_base_get_xdg_surface(port.shell, port.surface);
        if (!port.xdg_surface ||
            xdg_surface_add_listener(port.xdg_surface, &surface_listener, NULL) != 0)
            goto error;
        port.toplevel = xdg_surface_get_toplevel(port.xdg_surface);
        if (!port.toplevel ||
            xdg_toplevel_add_listener(port.toplevel, &toplevel_listener, NULL) != 0)
            goto error;
        xdg_toplevel_set_title(port.toplevel, "小瑞 / Xiaorui");
        xdg_toplevel_set_app_id(port.toplevel, "org.xiaorui.panel");
        xdg_toplevel_set_fullscreen(port.toplevel, NULL);
        wl_surface_commit(port.surface);
        if (wl_display_roundtrip(port.display) < 0 || port.closed)
            goto error;
        if (create_frame(&port.frames[0]) || create_frame(&port.frames[1]))
            goto error;
    }
    lv_init();
    static lv_disp_draw_buf_t draw_buffer;
    static lv_disp_drv_t driver;
    lv_disp_draw_buf_init(&draw_buffer, port.draw_pixels, NULL, XR_UI_WIDTH * 80);
    lv_disp_drv_init(&driver);
    driver.hor_res = XR_UI_WIDTH;
    driver.ver_res = XR_UI_HEIGHT;
    driver.draw_buf = &draw_buffer;
    driver.flush_cb = flush;
    if (!lv_disp_drv_register(&driver))
        goto error;
    static lv_indev_drv_t input;
    lv_indev_drv_init(&input);
    input.type = LV_INDEV_TYPE_POINTER;
    input.read_cb = read_touch;
    if (!lv_indev_drv_register(&input))
        goto error;
    return 0;
error:
    xr_display_close();
    return -1;
}

int xr_display_pump(int timeout_ms) {
    if (port.closed)
        return -1;
    if (!port.display) {
        if (timeout_ms > 0) {
            struct timespec delay = {.tv_sec = timeout_ms / 1000,
                                     .tv_nsec = (long)(timeout_ms % 1000) * 1000000};
            nanosleep(&delay, NULL);
        }
        return 0;
    }
    if (wl_display_dispatch_pending(port.display) < 0)
        return -1;
    struct pollfd descriptor = {.fd = wl_display_get_fd(port.display), .events = POLLIN};
    int result = poll(&descriptor, 1, timeout_ms);
    if (result < 0 && errno != EINTR)
        return -1;
    if (result > 0)
    {
        if (descriptor.revents & (POLLERR | POLLHUP | POLLNVAL))
            return -1;
        if ((descriptor.revents & POLLIN) && wl_display_dispatch(port.display) < 0)
            return -1;
    }
    present();
    if (wl_display_flush(port.display) < 0 && errno != EAGAIN)
        return -1;
    return port.closed ? -1 : 0;
}

int xr_display_dump(const char *path) {
    FILE *output = fopen(path, "wb");
    if (!output)
        return -1;
    bool failed = fprintf(output, "P6\n%d %d\n255\n", XR_UI_WIDTH, XR_UI_HEIGHT) < 0;
    for (size_t index = 0; index < XR_UI_WIDTH * XR_UI_HEIGHT && !failed; index++)
    {
        uint32_t pixel = port.pixels[index];
        unsigned char rgb[] = {(pixel >> 16) & 255, (pixel >> 8) & 255, pixel & 255};
        failed = fwrite(rgb, 1, 3, output) != 3;
    }
    if (fclose(output) != 0)
        failed = true;
    return failed ? -1 : 0;
}

void xr_display_close(void) {
    for (size_t index = 0; index < 2; index++)
    {
        if (port.frames[index].buffer)
            wl_buffer_destroy(port.frames[index].buffer);
        if (port.frames[index].pixels)
            munmap(port.frames[index].pixels, frame_bytes);
    }
    if (port.toplevel) xdg_toplevel_destroy(port.toplevel);
    if (port.xdg_surface) xdg_surface_destroy(port.xdg_surface);
    if (port.surface) wl_surface_destroy(port.surface);
    if (port.touch) wl_touch_destroy(port.touch);
    if (port.seat) wl_seat_destroy(port.seat);
    if (port.shell) xdg_wm_base_destroy(port.shell);
    if (port.shm) wl_shm_destroy(port.shm);
    if (port.compositor) wl_compositor_destroy(port.compositor);
    if (port.registry) wl_registry_destroy(port.registry);
    if (port.display) wl_display_disconnect(port.display);
    free(port.draw_pixels);
    free(port.pixels);
    memset(&port, 0, sizeof(port));
}
