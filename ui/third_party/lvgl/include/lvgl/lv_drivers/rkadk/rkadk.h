#ifdef __cplusplus
extern "C" {
#endif

/*********************
 *      INCLUDES
 *********************/
#ifndef LV_DRV_NO_CONF
#ifdef LV_CONF_INCLUDE_SIMPLE
#include "lv_drv_conf.h"
#else
#include "../../lv_drv_conf.h"
#endif
#endif

#if USE_RKADK
#include <rkadk_media_comm.h>
#include <rkadk_ui.h>

// Concrete realization

/**********************
 * GLOBAL PROTOTYPES
 **********************/
void rkadk_disp_drv_init(int rot);
void lv_port_disp_deinit(void);

#endif  /*USE_RKADK*/

#ifdef __cplusplus
} /* extern "C" */
#endif
