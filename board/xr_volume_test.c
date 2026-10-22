/* xr_volume 主机单测: 意图判定(宁漏不抢)/步进/增益/持久化 */
#define _POSIX_C_SOURCE 200809L
#include "xr_volume.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int hits, total;
static void is(const char *text, xr_vol_op_t want_op, int want_val) {
    xr_vol_op_t op = XR_VOL_NONE; int val = -1;
    total++;
    if (!xr_volume_parse(text, &op, &val)) {
        printf("MISS  %-24s (期望 op=%d)\n", text, (int)want_op);
        return;
    }
    if (op != want_op || (want_op == XR_VOL_SET && val != want_val)) {
        printf("WRONG %-24s got op=%d val=%d 期望 op=%d val=%d\n",
               text, (int)op, val, (int)want_op, want_val);
        return;
    }
    hits++;
}
static void not_vol(const char *text) {
    xr_vol_op_t op; int val;
    total++;
    if (xr_volume_parse(text, &op, &val))
        printf("OVERGRAB %-24s got op=%d val=%d (必须放行给 LLM)\n", text, (int)op, val);
    else hits++;
}

int main(void) {
    /* ---- 正例: 常见说法 ---- */
    is("大声一点", XR_VOL_UP, 0);
    is("大点声", XR_VOL_UP, 0);
    is("声音大一点", XR_VOL_UP, 0);
    is("请把声音调大一点", XR_VOL_UP, 0);
    is("响一点", XR_VOL_UP, 0);
    is("再大点", XR_VOL_UP, 0);
    is("小声一点", XR_VOL_DOWN, 0);
    is("小点声", XR_VOL_DOWN, 0);
    is("调小一点", XR_VOL_DOWN, 0);
    is("声音小一点", XR_VOL_DOWN, 0);
    is("轻一点", XR_VOL_DOWN, 0);
    is("太吵了", XR_VOL_DOWN, 0);
    is("太大声了", XR_VOL_DOWN, 0);
    is("音量小点吧", XR_VOL_DOWN, 0);
    is("静音", XR_VOL_MIN, 0);
    is("别出声", XR_VOL_MIN, 0);
    is("最小声", XR_VOL_MIN, 0);
    is("最大声", XR_VOL_MAX, 0);
    is("音量调到最大", XR_VOL_MAX, 0);
    is("音量调到百分之六十", XR_VOL_SET, 60);
    is("音量百分之三十", XR_VOL_SET, 30);
    is("百分之五十", XR_VOL_SET, 50);
    is("音量50", XR_VOL_SET, 50);
    is("音量百分之百", XR_VOL_SET, 100);
    is("把音量开到百分之八十", XR_VOL_SET, 80);
    is("音量六十", XR_VOL_SET, 60);
    is("声音调小到百分之二十", XR_VOL_SET, 20);

    /* ---- 负例: 必须放行(宁漏不抢) ---- */
    not_vol("大声唱首歌");
    not_vol("讲一个大点声的故事");
    not_vol("五十六");                    /* 裸数字应答, 无音量语境 */
    not_vol("把窗户打开一点");
    not_vol("今天天气怎么样");
    not_vol("大点声唱歌好吗");            /* 好吗=询问, 剩实义词 */
    not_vol("音量");                      /* 光有语境词, 无操作无数字 */
    not_vol("小点声唱个摇篮曲");
    not_vol("声音大一点再讲个故事");
    not_vol("大声朗读这首诗");
    not_vol("帮我打开百分之五十亮度的灯");
    not_vol("小明你好");
    not_vol("");
    not_vol("这个音量控制是谁教你的呀");
    not_vol("一百八十八号房间在哪里");     /* 数字超界且无语境 */

    /* ---- 步进/夹取 ---- */
    assert(xr_volume_next(100, XR_VOL_UP, 0) == 100);   /* 顶格不越 */
    assert(xr_volume_next(100, XR_VOL_DOWN, 0) == 80);
    assert(xr_volume_next(80, XR_VOL_DOWN, 0) == 60);
    assert(xr_volume_next(60, XR_VOL_UP, 0) == 80);
    assert(xr_volume_next(20, XR_VOL_DOWN, 0) == XR_VOLUME_MIN);
    assert(xr_volume_next(60, XR_VOL_SET, 5) == XR_VOLUME_MIN);    /* SET 夹下界 */
    assert(xr_volume_next(60, XR_VOL_SET, 300) == XR_VOLUME_MAX);  /* SET 夹上界 */
    assert(xr_volume_next(40, XR_VOL_MAX, 0) == 100);
    assert(xr_volume_next(100, XR_VOL_MIN, 0) == XR_VOLUME_MIN);
    assert(xr_volume_next(55, XR_VOL_NONE, 0) == 55);
    assert(xr_volume_next(-3, XR_VOL_DOWN, 0) == XR_VOLUME_MIN);   /* 越界初值先夹 */

    /* ---- 增益: 100 直通 / 折半 / 越界 pct 不动 ---- */
    int16_t pcm[4] = {1000, -2000, 32767, -32768};
    xr_volume_apply(pcm, 4, 100);
    assert(pcm[0] == 1000 && pcm[1] == -2000 && pcm[2] == 32767 && pcm[3] == -32768);
    xr_volume_apply(pcm, 4, 50);
    assert(pcm[0] == 500 && pcm[1] == -1000 && pcm[2] == 16383 && pcm[3] == -16384);
    xr_volume_apply(pcm, 4, 0);    assert(pcm[0] == 500);    /* 0=未设置, 不动 */
    xr_volume_apply(pcm, 4, -1);   assert(pcm[0] == 500);    /* 负值同上 */
    xr_volume_apply(pcm, 4, 200);  assert(pcm[0] == 500);    /* >=100 不放大 */

    /* ---- 持久化: 回写/缺省/垃圾/越界 ---- */
    char path[64];
    snprintf(path, sizeof(path), "/tmp/xr_volume_test_%d", (int)getpid());
    unlink(path);
    assert(xr_volume_load(path, 100) == 100);           /* 不存在 -> 缺省 */
    xr_volume_save(path, 60);
    assert(xr_volume_load(path, 100) == 60);
    FILE *f = fopen(path, "w"); fputs("garbage\n", f); fclose(f);
    assert(xr_volume_load(path, 100) == 100);           /* 垃圾 -> 缺省 */
    f = fopen(path, "w"); fputs("5\n", f); fclose(f);   /* 越下界 -> 缺省 */
    assert(xr_volume_load(path, 100) == 100);
    unlink(path);
    xr_volume_save("/nonexistent-dir/x", 60);           /* 写失败不崩 */

    printf("xr_volume: parse %d/%d, 步进/增益/持久化断言全过\n", hits, total);
    assert(hits == total);
    return 0;
}
