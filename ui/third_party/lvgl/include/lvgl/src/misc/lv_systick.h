#ifndef LV_SYSTICK_H
#define LV_SYSTICK_H

#include <stdint.h>
#include <time.h>

#define millis()    lv_systick()

static inline uint32_t lv_systick(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
        return 0;
    return (uint32_t)((uint64_t)now.tv_sec * 1000 + (uint64_t)now.tv_nsec / 1000000);
}

#endif
