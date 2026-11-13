#ifndef LV_SYSTICK_H
#define LV_SYSTICK_H

#define millis()    lv_systick()
uint32_t lv_systick(void);

#endif

