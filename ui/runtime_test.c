#define main xr_ui_main
#include "main.c"
#undef main

#include <assert.h>

static void test_answer_scroll(void) {
    const char *font_path = getenv("XR_UI_TEST_FONT");
    assert(font_path);
    assert(lv_freetype_init(4, 4, 4 * 1024 * 1024));
    assert(load_font(&body_font, font_path, 28) == 0);
    assert(load_font(&title_font, font_path, 34) == 0);
    assert(load_font(&large_font, font_path, 58) == 0);
    fonts_ready = true;
    caption_font = title_font.font;
    tag_font = body_font.font;
    build_ui();
    assert(lv_obj_has_flag(answer_card, LV_OBJ_FLAG_SCROLLABLE));
    char text[4096] = {0};
    for (int index = 0; index < 40; index++)
        strcat(text, "这是一段可以向上翻阅的回答。\n");
    lv_label_set_text(answer_label, text);
    lv_obj_update_layout(answer_card);
    assert(lv_obj_get_scroll_bottom(answer_card) > 0);
    scroll_answer_to_bottom();
    assert(lv_obj_get_scroll_y(answer_card) > 0);
    assert(lv_obj_get_scroll_bottom(answer_card) <= 4);
    lv_obj_scroll_to_y(answer_card, 0, LV_ANIM_OFF);
    assert(!answer_follow);
    scroll_answer_to_bottom();
    assert(lv_obj_get_scroll_y(answer_card) == 0);
    lv_obj_scroll_to_y(answer_card, LV_COORD_MAX, LV_ANIM_OFF);
    assert(answer_follow);
}

static void test_settings_stays_inside_navigation_footer(void) {
    const char *font_path = getenv("XR_UI_TEST_FONT");
    assert(font_path);
    assert(lv_freetype_init(4, 4, 4 * 1024 * 1024));
    assert(load_font(&body_font, font_path, 28) == 0);
    assert(load_font(&title_font, font_path, 34) == 0);
    assert(load_font(&large_font, font_path, 58) == 0);
    fonts_ready = true;
    caption_font = title_font.font;
    tag_font = body_font.font;
    build_ui();
    lv_obj_clear_flag(settings_page, LV_OBJ_FLAG_HIDDEN);
    lv_obj_update_layout(settings_page);
    /* The settings page is a 1450px viewport above fixed bottom navigation;
     * its longer contents scroll internally instead of expanding underneath. */
    assert(lv_obj_get_height(settings_page) == 1450);
    assert(lv_obj_has_flag(settings_page, LV_OBJ_FLAG_SCROLLABLE));
    assert(lv_obj_get_scroll_bottom(settings_page) > 0);
    lv_obj_scroll_to_y(settings_page, LV_COORD_MAX, LV_ANIM_OFF);
    assert(lv_obj_get_scroll_y(settings_page) > 0);
}

static void test_degraded_exit(void) {
    caption_font = &lv_font_montserrat_28;
    fonts_ready = false;
    build_ui();
    exit_ui(NULL);
    assert(!interrupted && exit_armed);
    assert(!strcmp(lv_label_get_text(exit_caption), "CONFIRM EXIT?"));
    exit_armed_at = (uint32_t)(monotonic_ms() - 3001);
    refresh(NULL);
    assert(!exit_armed);
    assert(!strcmp(lv_label_get_text(exit_caption), "EXIT"));
    exit_ui(NULL);
    assert(!interrupted);
    exit_ui(NULL);
    assert(interrupted);
}

int main(int argc, char **argv) {
    assert(argc == 2);
    assert(xr_display_open(true) == 0);
    if (!strcmp(argv[1], "scroll"))
        test_answer_scroll();
    else if (!strcmp(argv[1], "settings-scroll"))
        test_settings_stays_inside_navigation_footer();
    else {
        assert(!strcmp(argv[1], "degraded-exit"));
        test_degraded_exit();
    }
    xr_display_close();
    puts("UI runtime regression passed");
    return 0;
}
