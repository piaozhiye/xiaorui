/**
 * @file lv_stiker_matrix.h
 *
 */

#ifndef LV_STIKER_MATRIX_H
#define LV_STIKER_MATRIX_H

#ifdef __cplusplus
extern "C" {
#endif

/*********************
 *      INCLUDES
 *********************/
#include "../../../lvgl.h"

#if LV_USE_STIKER_MATRIX

/*********************
 *      DEFINES
 *********************/

/**********************
 *      TYPEDEFS
 **********************/

typedef struct
{
    lv_obj_t lv_obj;
    lv_coord_t pct_w;
    lv_coord_t pct_h;
} lv_stiker_matrix_item_t;

typedef struct
{
    lv_obj_t obj;
    lv_obj_t * items[5];
    lv_coord_t fixed_width;
    lv_coord_t fixed_height;
    lv_coord_t wfill;
    lv_coord_t cnt;
    lv_coord_t line;
} lv_stiker_matrix_t;

extern const lv_obj_class_t lv_stiker_matrix_class;
extern const lv_obj_class_t lv_stiker_matrix_item_class;

/**********************
 * GLOBAL PROTOTYPES
 **********************/

lv_obj_t * lv_stiker_matrix_create(lv_obj_t * parent);

void lv_stiker_matrix_set_size(lv_obj_t * obj, lv_coord_t w, lv_coord_t h);

void lv_stiker_matrix_set_size(lv_obj_t * obj, lv_coord_t w, lv_coord_t h);

lv_obj_t * lv_stiker_matrix_new_item(lv_obj_t * obj, lv_coord_t pct_w, lv_coord_t pct_h);

/**********************
 *      MACROS
 **********************/

#endif /*LV_USE_STIKER_MATRIX*/

#ifdef __cplusplus
} /*extern "C"*/
#endif

#endif /*LV_INPUTBOX_H*/
