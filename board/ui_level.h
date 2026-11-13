#ifndef XR_UI_LEVEL_H
#define XR_UI_LEVEL_H

/* 麦克风电平的显示平滑: 上去立即跟, 下来按 1/16 衰减。
   末段必须至少走 1 —— 整数除法在差值 <16 时得 0, 不兜底会永远停在
   目标上方 15 的位置(实测: 喂纯静音 10s, 电平钉死在 15 不回落)。 */
static inline int xr_ui_level_next(int current, int target) {
    if (target > current) return target;
    if (current > target) {
        int drop = (current - target) / 16;
        return current - (drop > 0 ? drop : 1);
    }
    return current;
}

#endif
