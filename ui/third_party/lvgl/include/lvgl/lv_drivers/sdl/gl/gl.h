#ifndef __GL_H__
#define __GL_H__

#include <lvgl/lvgl.h>
#include <SDL2/SDL.h>
#include <SDL2/SDL_opengl.h>
#include <SDL2/SDL_opengles2.h>

enum {
    LV_GL_FMT_ALPHA,
    LV_GL_FMT_LUMINANCE,
    LV_GL_FMT_LUMINANCE_ALPHA,
    LV_GL_FMT_INTENSITY,
    LV_GL_FMT_RGB,
    LV_GL_FMT_RGBA,
    LV_GL_FMT_BGRA,
};

enum {
    GL_TEX_TYPE_2D,
    GL_TEX_TYPE_CUBE,
};

enum {
    CUBE_LEFT,
    CUBE_RIGHT,
    CUBE_TOP,
    CUBE_BOTTOM,
    CUBE_FRONT,
    CUBE_BACK,
};

typedef void (*lv_gl_render_cb_t)(void);

typedef struct {
    const void *pixels;
    int format;
    int w;
    int h;
} lv_gl_img_t;

typedef struct {
    int w;
    int h;
} lv_gl_size_t;

typedef struct {
    GLuint gl_tex;
    GLuint FBO;
    lv_gl_size_t size[6];
    int format;
    int type;
    int ref_cnt;
} lv_gl_tex_t;

typedef struct {
    float x, y, z;
} lv_gl_vec_t;

typedef struct {
    int w;
    int h;
    int reverse_y;
    lv_gl_tex_t *tex;
    SDL_Rect r;
    SDL_Rect view;
    SDL_Rect crop;
    int view_dirty;
    int crop_dirty;
    int crop_en;
} lv_gl_base_t;

typedef struct {
    lv_gl_base_t base;
    GLuint VAO;
    GLuint VBO;
    int out_type;
    lv_gl_vec_t p[4];
    SDL_Rect tp;

    float alpha;
    lv_gl_vec_t scale;
    lv_gl_vec_t offset;
    lv_gl_vec_t self_rot;
    lv_gl_vec_t view_rot;
    lv_gl_vec_t move;
    int steps;
    int pos;
} lv_gl_obj_t;

static inline int lv_gl_tex_get_w(lv_gl_tex_t *tex, int index)
{
    if (tex->type == GL_TEX_TYPE_2D)
        return tex->size[0].w;

    if (index > CUBE_BACK || index < 0)
        return 0;

    return tex->size[index].w;
}

static inline int lv_gl_tex_get_h(lv_gl_tex_t *tex, int index)
{
    if (tex->type == GL_TEX_TYPE_2D)
        return tex->size[0].w;

    if (index > CUBE_BACK || index < 0)
        return 0;

    return tex->size[index].w;
}

static inline void lv_gl_obj_set_angle(lv_gl_obj_t *obj,
    float x, float y, float z)
{
    obj->self_rot.x = x;
    obj->self_rot.y = y;
    obj->self_rot.z = z;
}

static inline void lv_gl_obj_set_view_angle(lv_gl_obj_t *obj,
    float x, float y, float z)
{
    obj->view_rot.x = x;
    obj->view_rot.y = y;
    obj->view_rot.z = z;
}

/* Called by sdl_gpu.c */
int lv_gl_ctx_init(void);
void lv_gl_render(void);

/* Called by application */
void lv_gl_set_render_cb(lv_gl_render_cb_t cb);
void lv_gl_set_fb(lv_gl_obj_t *obj);
void lv_gl_read_pixels(void *ptr, SDL_Rect *r, int type);

lv_gl_tex_t *lv_gl_tex_create(int type, int w, int h, lv_gl_img_t *img);
void lv_gl_tex_del(lv_gl_tex_t *tex);
void lv_gl_tex_import_img(lv_gl_tex_t *tex, lv_gl_img_t *img);
void lv_gl_tex_clear(lv_gl_tex_t *tex, float r, float g, float b, float a);

lv_gl_obj_t *lv_gl_obj_create(int w, int h);
void lv_gl_obj_del(lv_gl_obj_t *obj);
void lv_gl_obj_resize(lv_gl_obj_t *obj, lv_gl_obj_t *parent);
void lv_gl_obj_move(lv_gl_obj_t *obj, lv_gl_obj_t *parent);
void lv_gl_obj_reset_points(lv_gl_obj_t *obj);
void lv_gl_obj_reset_tex_points(lv_gl_obj_t *obj);
void lv_gl_obj_update_vao(lv_gl_obj_t *obj);
void lv_gl_obj_release_vao(lv_gl_obj_t *obj);
void lv_gl_obj_bind_tex(lv_gl_obj_t *obj, lv_gl_tex_t *tex);
void lv_gl_obj_set_crop(lv_gl_obj_t *obj, SDL_Rect *r, int en);
void lv_gl_obj_get_crop(lv_gl_obj_t *obj, SDL_Rect *r);
void lv_gl_obj_set_viewport(lv_gl_obj_t *obj, SDL_Rect *r);
void lv_gl_obj_get_viewport(lv_gl_obj_t *obj, SDL_Rect *r);

/* Must called in render cb */
void lv_gl_obj_render(lv_gl_obj_t *obj);

#endif

