#define _POSIX_C_SOURCE 200809L
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <lvgl/src/extra/libs/freetype/lv_freetype.h>

#include "ui_client.h"
#include "wayland_port.h"

static volatile sig_atomic_t interrupted;
static volatile sig_atomic_t dump_requested;
static lv_ft_info_t body_font;
static lv_ft_info_t title_font;
static lv_ft_info_t large_font;
static lv_ft_info_t small_font;
static const lv_font_t *tag_font;
static lv_obj_t *conversation_page;
static lv_obj_t *settings_page;
static lv_obj_t *state_label;
static lv_obj_t *state_dot;
static lv_obj_t *state_chip;
static lv_obj_t *connection_label;
static lv_obj_t *question_label;
static lv_obj_t *answer_label;
static lv_obj_t *clock_label;
static lv_obj_t *volume_slider;
static lv_obj_t *volume_label;
static lv_obj_t *stop_button;
static lv_obj_t *answer_card;
static lv_obj_t *greeting_label;
static lv_obj_t *date_label;
static lv_obj_t *exit_caption;
static lv_obj_t *halo;
static lv_obj_t *service_label;
static lv_obj_t *detail_volume;
static lv_obj_t *memory_label;
static lv_obj_t *uptime_label;
static lv_obj_t *eyes[2];
static bool settings_visible;
static bool demo;
static bool connected;
static bool fonts_ready;
static bool answer_follow = true;
static bool exit_armed;
static uint32_t exit_armed_at;
static const lv_font_t *caption_font;
static const char *font_path_used;
static uint32_t displayed_state = UINT32_MAX;
static int displayed_hour = -1;
static int ui_level;
static uint32_t displayed_eye_color;
static uint32_t displayed_ring_color;
static uint64_t started_ms;
static uint64_t last_memory_ms;
static int displayed_volume = -1;
static int displayed_speaking = -1;
static char displayed_question[256];
static char displayed_answer[4096];

static const uint32_t state_palette[4] = {0x8ca5aa, 0x8fe3c3, 0xf0d38a, 0xa8d8f0};
static const char *state_text[4] = {
    "等你说一句「小瑞小瑞」", "我在听，请说", "让我想一想", "正在回答你",
};
static const char *question_placeholder = "说点什么，识别后的文字会显示在这里";
static const char *answer_placeholder = "小瑞的回答会显示在这里";
/* 静音时保留的待机微光(%)。纯电平为 0 时脸是全暗的, 观感偏死;
   15 与之前被整数除法钉住的亮度一致, 说话时在此之上叠加。 */
#define IDLE_GLOW 15

static uint64_t monotonic_ms(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
        return 0;
    return (uint64_t)now.tv_sec * 1000 + (uint64_t)now.tv_nsec / 1000000;
}

/* 两个颜色按百分比混 */
static uint32_t mix_color(uint32_t from, uint32_t to, int percent) {
    if (percent < 0) percent = 0;
    if (percent > 100) percent = 100;
    int red = (int)(((from >> 16) & 0xff) * (100 - percent) + ((to >> 16) & 0xff) * percent) / 100;
    int green = (int)(((from >> 8) & 0xff) * (100 - percent) + ((to >> 8) & 0xff) * percent) / 100;
    int blue = (int)((from & 0xff) * (100 - percent) + (to & 0xff) * percent) / 100;
    return ((uint32_t)red << 16) | ((uint32_t)green << 8) | (uint32_t)blue;
}

/* 界面自己占多少内存 */
static int read_rss_kb(void) {
    FILE *status = fopen("/proc/self/status", "r");
    if (!status) return 0;
    char line[256];
    int kilobytes = 0;
    while (fgets(line, sizeof(line), status))
        if (!strncmp(line, "VmRSS:", 6)) { kilobytes = atoi(line + 6); break; }
    fclose(status);
    return kilobytes;
}

static void on_signal(int number) {
    if (number == SIGUSR1)
        dump_requested = 1;
    else
        interrupted = 1;
}

static lv_obj_t *panel(lv_obj_t *parent, int position_x, int position_y,
                        int width, int height, uint32_t color, int radius) {
    lv_obj_t *object = lv_obj_create(parent);
    lv_obj_remove_style_all(object);
    lv_obj_set_pos(object, position_x, position_y);
    lv_obj_set_size(object, width, height);
    lv_obj_set_style_bg_color(object, lv_color_hex(color), 0);
    lv_obj_set_style_bg_opa(object, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(object, radius, 0);
    lv_obj_clear_flag(object, LV_OBJ_FLAG_SCROLLABLE);
    return object;
}

static lv_obj_t *label(lv_obj_t *parent, const char *text, int position_x, int position_y,
                        int width, const lv_font_t *font, uint32_t color) {
    lv_obj_t *object = lv_label_create(parent);
    lv_obj_set_pos(object, position_x, position_y);
    lv_obj_set_width(object, width);
    lv_obj_set_style_text_font(object, font, 0);
    lv_obj_set_style_text_color(object, lv_color_hex(color), 0);
    lv_label_set_long_mode(object, LV_LABEL_LONG_WRAP);
    lv_label_set_text(object, text);
    return object;
}

static void navigation(lv_event_t *event) {
    settings_visible = lv_event_get_user_data(event) != NULL;
    if (settings_visible)
    {
        lv_obj_add_flag(conversation_page, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(settings_page, LV_OBJ_FLAG_HIDDEN);
    }
    else
    {
        lv_obj_clear_flag(conversation_page, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(settings_page, LV_OBJ_FLAG_HIDDEN);
    }
    fprintf(stderr, "[ui] page=%s\n", settings_visible ? "settings" : "conversation");
}

static lv_obj_t *button(lv_obj_t *parent, const char *text, int position_x, int position_y,
                         int width, lv_event_cb_t callback, void *user_data) {
    lv_obj_t *object = lv_btn_create(parent);
    lv_obj_set_pos(object, position_x, position_y);
    lv_obj_set_size(object, width, 94);
    lv_obj_set_style_radius(object, 25, 0);
    lv_obj_set_style_bg_color(object, lv_color_hex(0x1c3034), 0);
    lv_obj_set_style_border_width(object, 1, 0);
    lv_obj_set_style_border_color(object, lv_color_hex(0x2c4a50), 0);
    lv_obj_set_style_border_color(object, lv_color_hex(0x8fe3c3), LV_STATE_PRESSED);
    lv_obj_set_style_bg_color(object, lv_color_hex(0x2f5a5f), LV_STATE_PRESSED);
    lv_obj_set_style_shadow_width(object, 0, 0);
    lv_obj_set_style_bg_opa(object, LV_OPA_40, LV_STATE_DISABLED);
    lv_obj_t *caption = label(object, text, 0, 0, width - 20, caption_font, 0xd9e9e7);
    lv_obj_set_style_text_color(caption, lv_color_hex(0xffffff), LV_STATE_PRESSED);
    lv_obj_set_style_text_align(caption, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_center(caption);
    lv_obj_add_event_cb(object, callback, LV_EVENT_CLICKED, user_data);
    return object;
}

static void stop_answer(lv_event_t *event) {
    (void)event;
    if (connected && !demo && xr_ui_client_command(XR_UI_STOP, 0) != 0)
        lv_label_set_text(connection_label, "停止请求未发送，请稍后重试");
}

static void volume_changed(lv_event_t *event) {
    (void)event;
    unsigned int value = (unsigned int)lv_slider_get_value(volume_slider);
    if (connected && !demo && xr_ui_client_command(XR_UI_VOLUME, value) != 0)
        lv_label_set_text(connection_label, "音量请求未发送，请稍后重试");
}

static void exit_ui(lv_event_t *event) {
    (void)event;
    if (!exit_armed || monotonic_ms() - exit_armed_at > 3000)
    {
        exit_armed = true;
        exit_armed_at = (uint32_t)monotonic_ms();
        if (exit_caption) lv_label_set_text(exit_caption, "确认退出？");
        fprintf(stderr, "[ui] exit requested (press again to confirm)\n");
        return;
    }
    interrupted = 1;
    fprintf(stderr, "[ui] exit confirmed\n");
}

/* 状态胶囊: 文字长度变了就重新量宽度并居中 */
static void layout_state_chip(const char *text, uint32_t color) {
    if (!state_chip) return;
    lv_point_t size;
    lv_txt_get_size(&size, text, title_font.font, 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
    int width = 48 + size.x + 24;
    lv_obj_set_width(state_chip, width);
    lv_obj_set_x(state_chip, (XR_UI_WIDTH - width) / 2);
    lv_obj_set_width(state_label, size.x);
    lv_obj_set_style_text_color(state_label, lv_color_hex(color), 0);
    lv_obj_set_style_bg_color(state_dot, lv_color_hex(color), 0);
}

static void scroll_answer_to_bottom(void) {
    if (!answer_card || !answer_follow) return;
    lv_obj_scroll_to_y(answer_card, LV_COORD_MAX, LV_ANIM_OFF);
}

/* 用户自己往上翻了就别再把他弹回底部, 回到底部才恢复跟随 */
static void answer_scrolled(lv_event_t *event) {
    lv_obj_t *card = lv_event_get_target(event);
    answer_follow = lv_obj_get_scroll_bottom(card) <= 4;
}

static const char *greeting_text(int hour) {
    if (hour < 5)  return "夜深了，早点休息";
    if (hour < 9)  return "早上好，很高兴见到你";
    if (hour < 11) return "上午好，很高兴见到你";
    if (hour < 13) return "中午好，吃点东西吧";
    if (hour < 18) return "下午好，很高兴见到你";
    return "晚上好，欢迎回来";
}

/* 顶栏的日期和问候语: 按小时换, 跟板子语音里的问候呼应 */
static void refresh_header(void) {
    time_t now = time(NULL);
    struct tm local;
    if (!localtime_r(&now, &local)) return;
    if (date_label) {
        static const char *weekdays[7] = {"日", "一", "二", "三", "四", "五", "六"};
        char date_text[32];
        snprintf(date_text, sizeof(date_text), "%d月%d日 星期%s",
                 local.tm_mon + 1, local.tm_mday, weekdays[local.tm_wday]);
        if (strcmp(lv_label_get_text(date_label), date_text))
            lv_label_set_text(date_label, date_text);
    }
    if (greeting_label && local.tm_hour != displayed_hour) {
        displayed_hour = local.tm_hour;
        const char *greeting = greeting_text(local.tm_hour);
        if (strcmp(lv_label_get_text(greeting_label), greeting))
            lv_label_set_text(greeting_label, greeting);
    }
}

/* 眨眼: 5 秒一轮, 闭眼 170ms, 用 smoothstep 睁开; 说话时轻微律动 */
static int eye_height_at(uint64_t now_ms, int speaking) {
    const int closed = 12, open = 86;
    uint32_t phase = (uint32_t)(now_ms % 5000);
    if (phase < 170)
        return closed;
    if (phase < 310) {
        float t = (float)(phase - 170) / 140.0f;
        t = t * t * (3.0f - 2.0f * t);
        return closed + (int)((open - closed) * t);
    }
    if (speaking) {
        float wave = (float)((now_ms % 700) / 700.0f);
        int offset = (int)(6.0f * (wave < 0.5f ? wave : 1.0f - wave));
        return open - offset;
    }
    return open;
}

static void build_conversation(lv_obj_t *screen) {
    conversation_page = panel(screen, 0, 185, 1080, 1450, 0x0c171b, 0);
    greeting_label = label(conversation_page, "很高兴见到你", 74, 16, 900,
                           large_font.font, 0xf2f0e8);
    label(conversation_page, "一句「小瑞小瑞」，开始我们的对话", 76, 101, 920,
          body_font.font, 0x8ca5aa);
    halo = panel(conversation_page, 259, 258, 562, 562, 0x10272b, LV_RADIUS_CIRCLE);
    lv_obj_set_style_border_width(halo, 2, 0);
    lv_obj_set_style_border_color(halo, lv_color_hex(0x294548), 0);
    lv_obj_t *face = panel(halo, 59, 59, 440, 440, 0x17383b, LV_RADIUS_CIRCLE);
    for (int index = 0; index < 2; index++)
        eyes[index] = panel(face, 112 + index * 145, 163, 42, 86, 0x8fe3c3, 21);
    panel(face, 194, 280, 52, 8, 0x8fe3c3, 4);
    state_chip = panel(conversation_page, 0, 852, 240, 52, 0x14262a, 26);
    state_dot = panel(state_chip, 18, 17, 18, 18, 0x8ca5aa, LV_RADIUS_CIRCLE);
    state_label = label(state_chip, "等待语音服务连接", 48, 8, 180,
                        title_font.font, 0xc8e8dc);
    lv_obj_t *question = panel(conversation_page, 64, 980, 952, 170, 0x122327, 28);
    label(question, "你说", 30, 22, 800, tag_font, 0x6a9499);
    question_label = label(question, "识别后的文字会显示在这里", 30, 77, 884,
                           title_font.font, 0xdce5e4);
    answer_card = panel(conversation_page, 64, 1172, 952, 255, 0x152b2d, 28);
    label(answer_card, "小瑞", 30, 22, 800, tag_font, 0x82c6b0);
    answer_label = label(answer_card, answer_placeholder,
                          30, 60, 884, title_font.font, 0xe6eeea);
    /* 长回答要能往下翻: 卡片竖向可滚, 文字自然高度 */
    lv_obj_set_scroll_dir(answer_card, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(answer_card, LV_SCROLLBAR_MODE_AUTO);
    lv_obj_set_style_bg_opa(answer_card, LV_OPA_60, LV_PART_SCROLLBAR);
    lv_obj_set_style_bg_color(answer_card, lv_color_hex(0x8fe3c3), LV_PART_SCROLLBAR);
    lv_obj_set_style_width(answer_card, 4, LV_PART_SCROLLBAR);
    lv_obj_set_style_radius(answer_card, 2, LV_PART_SCROLLBAR);
    lv_obj_add_event_cb(answer_card, answer_scrolled, LV_EVENT_SCROLL, NULL);
}

static void build_settings(lv_obj_t *screen) {
    settings_page = panel(screen, 0, 185, 1080, 1450, 0x0c171b, 0);
    label(settings_page, "把小瑞调到刚刚好", 74, 16, 950, large_font.font, 0xf2f0e8);
    label(settings_page, "触摸控制与语音主链路相互独立",
          76, 105, 920, body_font.font, 0x8ca5aa);
    lv_obj_t *card = panel(settings_page, 64, 240, 952, 350, 0x122327, 28);
    label(card, "播放音量", 40, 38, 700, title_font.font, 0xdce5e4);
    volume_label = label(card, "等待连接", 40, 110, 800, body_font.font, 0x82c6b0);
    volume_slider = lv_slider_create(card);
    lv_obj_set_pos(volume_slider, 55, 200);
    lv_obj_set_size(volume_slider, 835, 22);
    lv_slider_set_range(volume_slider, 10, 100);
    lv_obj_set_style_bg_color(volume_slider, lv_color_hex(0x274346), LV_PART_MAIN);
    lv_obj_set_style_bg_color(volume_slider, lv_color_hex(0x8fe3c3), LV_PART_INDICATOR);
    lv_obj_set_style_bg_grad_color(volume_slider, lv_color_hex(0x8fe3c3), LV_PART_INDICATOR);
    lv_obj_set_style_bg_grad_dir(volume_slider, LV_GRAD_DIR_HOR, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(volume_slider, lv_color_hex(0xc9f7e7), LV_PART_KNOB);
    lv_obj_add_event_cb(volume_slider, volume_changed, LV_EVENT_RELEASED, NULL);
    lv_obj_add_state(volume_slider, LV_STATE_DISABLED);
    label(card, "10", 52, 240, 120, tag_font, 0x6a9499);
    label(card, "50", 450, 240, 120, tag_font, 0x6a9499);
    label(card, "100", 800, 240, 120, tag_font, 0x6a9499);
    lv_obj_t *details = panel(settings_page, 64, 640, 952, 470, 0x122327, 28);
    label(details, "设备与连接", 40, 38, 800, title_font.font, 0xdce5e4);
    label(details, "语音服务", 40, 120, 280, body_font.font, 0x8ca5aa);
    service_label = label(details, "未连接", 320, 120, 560, body_font.font, 0x82c6b0);
    label(details, "语音状态", 40, 185, 280, body_font.font, 0x8ca5aa);
    detail_volume = label(details, "--", 320, 185, 560, body_font.font, 0x82c6b0);
    label(details, "界面内存", 40, 250, 280, body_font.font, 0x8ca5aa);
    memory_label = label(details, "--", 320, 250, 560, body_font.font, 0x82c6b0);
    label(details, "运行时长", 40, 315, 280, body_font.font, 0x8ca5aa);
    uptime_label = label(details, "--", 320, 315, 560, body_font.font, 0x82c6b0);
    label(details, "RK3562 EVB2 · 1080×1920 · LVGL 8.4 / Wayland",
          40, 390, 880, tag_font, 0x7f9ea0);
    label(settings_page, "连接中断时控制会自动禁用，"
          "不影响语音程序继续运行。",
          76, 1180, 900, body_font.font, 0x8ca5aa);
    lv_obj_t *exit = button(settings_page, "退出界面", 64, 1250, 952, exit_ui, NULL);
    exit_caption = lv_obj_get_child(exit, 0);
    label(settings_page, "退出后回到系统界面，语音继续运行。",
          76, 1370, 900, body_font.font, 0x8ca5aa);
    lv_obj_add_flag(settings_page, LV_OBJ_FLAG_HIDDEN);
}

static void build_degraded_ui(lv_obj_t *screen) {
    label(screen, "X I A O R U I", 74, 57, 500, &lv_font_montserrat_28, 0x8fe3c3);
    label(screen, "CHINESE FONT MISSING", 74, 420, 900, &lv_font_montserrat_28, 0xf2f0e8);
    label(screen, "The panel runs in degraded mode.", 74, 500, 900, &lv_font_montserrat_20, 0x8ca5aa);
    label(screen, "Expected:", 74, 600, 300, &lv_font_montserrat_20, 0x7b989e);
    label(screen, font_path_used ? font_path_used : "(unset)", 74, 640, 930, &lv_font_montserrat_20, 0x7b989e);
    label(screen, "Voice assistant keeps working.", 74, 740, 900, &lv_font_montserrat_20, 0x8ca5aa);
    lv_obj_t *exit = button(screen, "EXIT", 64, 1750, 285, exit_ui, NULL);
    exit_caption = lv_obj_get_child(exit, 0);
}

static void build_ui(void) {
    lv_obj_t *screen = lv_scr_act();
    lv_obj_set_style_bg_color(screen, lv_color_hex(0x0c171b), 0);
    lv_obj_clear_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
    if (!fonts_ready) {
        build_degraded_ui(screen);
        return;
    }
    label(screen, "X I A O R U I", 74, 57, 500, &lv_font_montserrat_28, 0x8fe3c3);
    clock_label = label(screen, "--:--", 814, 49, 200, &lv_font_montserrat_32, 0xf2f0e8);
    date_label = label(screen, "", 740, 95, 274, tag_font, 0x6a9499);
    lv_obj_set_style_text_align(date_label, LV_TEXT_ALIGN_RIGHT, 0);
    label(screen, "小瑞 · 你的端侧语音伙伴", 74, 109, 900, body_font.font, 0x7f9ea0);
    build_conversation(screen);
    build_settings(screen);
    connection_label = label(screen, "语音 IPC 未连接，控制暂不可用", 76, 1660, 930,
                             body_font.font, 0x7b989e);
    button(screen, "对话", 64, 1750, 285, navigation, NULL);
    stop_button = button(screen, "停止回答", 369, 1750, 342, stop_answer, NULL);
    lv_obj_add_state(stop_button, LV_STATE_DISABLED);
    button(screen, "设置", 731, 1750, 285, navigation, (void *)1);
    label(screen, "ON DEVICE / PRIVATE BY DESIGN", 76, 1870, 900,
          &lv_font_montserrat_16, 0x4a6970);
}

static void refresh(lv_timer_t *timer) {
    (void)timer;
    uint64_t now_ms = monotonic_ms();
    xr_ui_client_tick(now_ms);
    if (!fonts_ready)
        return;
    refresh_header();
    if (exit_armed && (uint32_t)now_ms - exit_armed_at > 3000)
    {
        exit_armed = false;
        if (exit_caption) lv_label_set_text(exit_caption, "退出界面");
    }
    bool available = !demo && xr_ui_client_connected(now_ms);
    if (available != connected)
    {
        connected = available;
        displayed_state = UINT32_MAX;
        displayed_speaking = -1;
        const char *connection = connected ? "语音已连接 · 本机实时状态" :
                                            "语音 IPC 未连接 · 控制暂不可用";
        lv_label_set_text(connection_label, connection);
        if (connected)
        {
            lv_obj_clear_state(volume_slider, LV_STATE_DISABLED);
        }
        else
        {
            lv_obj_add_state(volume_slider, LV_STATE_DISABLED);
            lv_obj_add_state(stop_button, LV_STATE_DISABLED);
            lv_label_set_text(state_label, "等待语音服务连接");
            layout_state_chip("等待语音服务连接", state_palette[0]);
            lv_label_set_text(volume_label, "等待连接");
            displayed_volume = -1;
        }
    }
    const xr_ui_message_t *status = xr_ui_client_status();
    uint32_t state = demo ? (uint32_t)((now_ms / 5000) % 4) : status->state;
    if (state > XR_UI_SPEAKING) state = XR_UI_READY;
    if ((connected || demo) && state != displayed_state)
    {
        lv_label_set_text(state_label, state_text[state]);
        layout_state_chip(state_text[state], state_palette[state]);
        displayed_state = state;
    }
    /* 停止按钮只有在真在回答时才有意义, 否则点了没反应 */
    int speaking = (connected && state == XR_UI_SPEAKING) ? 1 : 0;
    if (speaking != displayed_speaking)
    {
        displayed_speaking = speaking;
        if (speaking) lv_obj_clear_state(stop_button, LV_STATE_DISABLED);
        else lv_obj_add_state(stop_button, LV_STATE_DISABLED);
    }
    if (connected)
    {
        if (strcmp(displayed_question, status->question))
        {
            memcpy(displayed_question, status->question, sizeof(displayed_question));
            lv_label_set_text(question_label, displayed_question[0] ?
                              displayed_question : question_placeholder);
        }
        if (strcmp(displayed_answer, status->answer))
        {
            memcpy(displayed_answer, status->answer, sizeof(displayed_answer));
            lv_label_set_text(answer_label, displayed_answer[0] ?
                              displayed_answer : answer_placeholder);
            if (!displayed_answer[0])
                answer_follow = true;   /* 新一轮: 重新从头跟随 */
            scroll_answer_to_bottom();
        }
        if (((int)status->volume != displayed_volume ||
             lv_slider_get_value(volume_slider) != (int)status->volume) &&
            !lv_obj_has_state(volume_slider, LV_STATE_PRESSED))
        {
            char value[32];
            displayed_volume = (int)status->volume;
            snprintf(value, sizeof(value), "%d%%", displayed_volume);
            lv_label_set_text(volume_label, value);
            lv_slider_set_value(volume_slider, displayed_volume, LV_ANIM_OFF);
        }
    }
    /* 麦克风电平: 眼睛和光圈跟着人说话亮起来 */
    int target_level = connected ? (int)status->level : 0;
    /* 整数除法在差值 <4 时得 0, 直接加会停在离目标 3 的地方(和前端同款坑),
       保证每次至少走 1, 静音时才能真的回到待机微光 */
    int level_delta = target_level - ui_level;
    if (level_delta)
    {
        int step = level_delta / 4;
        if (step == 0) step = level_delta > 0 ? 1 : -1;
        ui_level += step;
    }
    if (ui_level < 0) ui_level = 0;
    if (ui_level > 100) ui_level = 100;
    int glow = ui_level + IDLE_GLOW;
    if (glow > 100) glow = 100;
    uint32_t eye_color = mix_color(0x2c6a60, 0x8fe3c3, glow);
    uint32_t ring_color = mix_color(0x1e3d3e, 0x3f8f7a, glow);
    if (eye_color != displayed_eye_color)
    {
        displayed_eye_color = eye_color;
        for (int index = 0; index < 2; index++)
            lv_obj_set_style_bg_color(eyes[index], lv_color_hex(eye_color), 0);
    }
    if (ring_color != displayed_ring_color)
    {
        displayed_ring_color = ring_color;
        if (halo) lv_obj_set_style_border_color(halo, lv_color_hex(ring_color), 0);
    }
    if (service_label)
    {
        const char *service = connected ? "已连接 · 本机 IPC" : "未连接";
        if (strcmp(lv_label_get_text(service_label), service))
            lv_label_set_text(service_label, service);
    }
    if (detail_volume)
    {
        const char *phase = connected ? state_text[state] : "--";
        if (strcmp(lv_label_get_text(detail_volume), phase))
            lv_label_set_text(detail_volume, phase);
    }
    if (uptime_label)
    {
        char value[32];
        snprintf(value, sizeof(value), "%u 分钟", (unsigned int)((now_ms - started_ms) / 60000));
        if (strcmp(lv_label_get_text(uptime_label), value))
            lv_label_set_text(uptime_label, value);
    }
    if (memory_label && now_ms - last_memory_ms > 5000)
    {
        last_memory_ms = now_ms;
        char value[32];
        snprintf(value, sizeof(value), "%d MB", read_rss_kb() / 1024);
        if (strcmp(lv_label_get_text(memory_label), value))
            lv_label_set_text(memory_label, value);
    }
    int eye_height = eye_height_at(now_ms, state == XR_UI_SPEAKING);
    for (int index = 0; index < 2; index++)
    {
        if (lv_obj_get_height(eyes[index]) != eye_height)
        {
            lv_obj_set_height(eyes[index], eye_height);
            lv_obj_set_y(eyes[index], 163 + (86 - eye_height) / 2);
        }
    }
    time_t timestamp = time(NULL);
    struct tm local;
    char clock_text[16];
    if (localtime_r(&timestamp, &local) &&
        strftime(clock_text, sizeof(clock_text), "%H:%M", &local))
    {
        if (strcmp(lv_label_get_text(clock_label), clock_text))
            lv_label_set_text(clock_label, clock_text);
    }
}

static int load_font(lv_ft_info_t *font, const char *path, unsigned int size) {
    font->name = path;
    font->weight = (uint16_t)size;
    font->style = FT_FONT_STYLE_NORMAL;
    return lv_ft_font_init(font) ? 0 : -1;
}

int main(int argc, char **argv) {
    const char *snapshot = NULL;
    const char *font_path = getenv("XR_UI_FONT");
    const char *socket_path = getenv("XR_UI_SOCKET");
    if (!font_path)
        font_path = "/usr/share/fonts/source-han-sans-cn/SourceHanSansCN-Regular.otf";
    if (!socket_path)
        socket_path = "/run/xiaorui/frontend.sock";
    for (int index = 1; index < argc; index++)
    {
        if (!strcmp(argv[index], "--demo"))
            demo = true;
        else if (!strcmp(argv[index], "--settings"))
            settings_visible = true;
        else if (!strcmp(argv[index], "--snapshot") && index + 1 < argc)
            snapshot = argv[++index];
        else
        {
            fprintf(stderr, "usage: %s [--demo] [--settings] [--snapshot output.ppm]\n", argv[0]);
            return 2;
        }
    }
    font_path_used = font_path;
    started_ms = monotonic_ms();
    if (xr_display_open(snapshot != NULL) != 0)
    {
        fprintf(stderr, "[ui] cannot initialize Wayland/LVGL display\n");
        return 1;
    }
    int result = 1;
    fonts_ready = access(font_path, R_OK) == 0;
    if (fonts_ready && !lv_freetype_init(4, 4, 4 * 1024 * 1024))
        fonts_ready = false;
    if (fonts_ready && (load_font(&body_font, font_path, 28) ||
                        load_font(&title_font, font_path, 34) ||
                        load_font(&large_font, font_path, 58)))
        fonts_ready = false;
    if (fonts_ready)
    {
        /* 小字号只用于卡片标签, 加载失败就退回正文, 不因此退出 */
        tag_font = load_font(&small_font, font_path, 22) == 0 ? small_font.font : body_font.font;
        caption_font = title_font.font;
    }
    else
    {
        /* 没有中文字体不再黑屏退出: 退化成纯 ASCII 界面, 语音照常跑 */
        tag_font = &lv_font_montserrat_20;
        caption_font = &lv_font_montserrat_28;
        fprintf(stderr, "[ui] Chinese font unavailable (%s), degraded ASCII UI\n", font_path);
    }
    if (!snapshot && !demo && xr_ui_client_open(socket_path) != 0)
    {
        fprintf(stderr, "[ui] cannot initialize IPC client\n");
        goto cleanup;
    }
    struct sigaction action = {.sa_handler = on_signal};
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGINT, &action, NULL) != 0 ||
        sigaction(SIGTERM, &action, NULL) != 0 ||
        sigaction(SIGUSR1, &action, NULL) != 0)
        goto cleanup;
    build_ui();
    if (settings_visible)
    {
        lv_obj_add_flag(conversation_page, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(settings_page, LV_OBJ_FLAG_HIDDEN);
    }
    if (demo)
    {
        lv_label_set_text(connection_label,
                          "界面预览模式 · 非真实语音状态 · 控制已禁用");
        lv_label_set_text(question_label, "你叫什么名字？");
        lv_label_set_text(answer_label,
                          "我叫小瑞，是运行在开发板上的语音助手。"
                          "很高兴认识你。");
    }
    lv_timer_create(refresh, 50, NULL);
    refresh(NULL);
    lv_refr_now(NULL);
    if (snapshot)
    {
        result = xr_display_dump(snapshot) == 0 ? 0 : 1;
        goto cleanup;
    }
    fprintf(stderr, "[ui] ready: 1080x1920, Wayland touch, demo=%d\n", demo);
    result = 0;
    while (!interrupted)
    {
        uint32_t idle_ms = lv_timer_handler();
        if (dump_requested)
        {
            dump_requested = 0;
            const char *dump_path = getenv("XR_UI_DUMP");
            if (dump_path && xr_display_dump(dump_path) != 0)
                fprintf(stderr, "[ui] screenshot write failed\n");
        }
        /* 等 wayland 事件, 最多等下一个 LVGL 定时器: 原来固定 10ms 空转
           (100Hz 唤醒), 现在 ~30Hz, 触摸照样即时(有事件 poll 立刻返回) */
        int wait = idle_ms > 50 ? 50 : (int)idle_ms;
        if (wait < 1) wait = 1;
        if (xr_display_pump(wait) != 0)
        {
            result = 1;
            break;
        }
    }
cleanup:
    xr_ui_client_close();
    xr_display_close();
    return result;
}
