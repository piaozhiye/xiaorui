#ifndef XR_VOLUME_H
#define XR_VOLUME_H
#include <stddef.h>
#include <stdint.h>

/* 语音音量控制(本地意图, 零 LLM 延迟; 2026-10-15):
 *   xr_volume_parse  ASR 文本 -> 音量操作(纯函数, 主机可测)
 *   xr_volume_next   当前值 + 操作 -> 新值(步进/夹取, 纯函数)
 *   xr_volume_apply  PCM 软件增益(播放线程每批调用; 100=原样直通)
 *   load/save        一行整数文件的持久化
 *
 * 判定哲学与纯唤醒一致: 只认"整句几乎全是音量词"的说法。剥掉全部音量词、
 * 数字和语气助词后还剩实义词的("大声唱首歌")一律放行给 LLM —— 宁可漏
 * 不可抢, 抢走真指令的代价远大于漏一条音量说法。 */
typedef enum {
    XR_VOL_NONE = 0,
    XR_VOL_UP,      /* 大声一点 */
    XR_VOL_DOWN,    /* 小声一点 / 太吵了 */
    XR_VOL_SET,     /* 音量调到百分之六十 / 音量50 */
    XR_VOL_MAX,     /* 最大声 */
    XR_VOL_MIN      /* 静音/最小声 -> 压到下限但保持可听(纯语音设备静音=语音无法恢复) */
} xr_vol_op_t;

/* 命中返回 1 并填 op/value(仅 SET 用到 value); 不命中返回 0。 */
int xr_volume_parse(const char *text, xr_vol_op_t *op, int *value);

#define XR_VOLUME_MIN 10
#define XR_VOLUME_MAX 100
#define XR_VOLUME_STEP 20
int xr_volume_next(int current, xr_vol_op_t op, int value);

/* pct 超出 (0,100) 不动样本(100 逐位直通, 0/负值视为未设置)。 */
void xr_volume_apply(int16_t *pcm, size_t n, int pct);

int xr_volume_load(const char *path, int fallback);
void xr_volume_save(const char *path, int pct);
#endif
