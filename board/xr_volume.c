/* xr_volume -- 语音音量控制的纯逻辑部分(无音频设备/线程依赖) */
#define _POSIX_C_SOURCE 200809L
#include "xr_volume.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---------- UTF-8 小工具 ---------- */
static size_t utf8_len(unsigned char c) {
    if (c < 0x80) return 1;
    if ((c & 0xe0) == 0xc0) return 2;
    if ((c & 0xf0) == 0xe0) return 3;
    if ((c & 0xf8) == 0xf0) return 4;
    return 1;
}
static size_t char_count(const char *s) {
    size_t n = 0;
    while (*s) { n++; s += utf8_len((unsigned char)*s); }
    return n;
}
/* 从 buf 删除 needle 的全部出现, 返回删除次数 */
static int remove_all(char *buf, const char *needle) {
    size_t nl = strlen(needle);
    if (!nl) return 0;
    int hits = 0;
    char *p;
    while ((p = strstr(buf, needle))) {
        memmove(p, p + nl, strlen(p + nl) + 1);
        hits++;
    }
    return hits;
}

/* ---------- 中文数字(0-100) ---------- */
static int digit_val(const char *s, size_t cl) {
    /* 单字数值: ASCII 数字或 零一二三四五六七八九十百两; 非数字返回 -1 */
    if (cl == 1 && s[0] >= '0' && s[0] <= '9') return s[0] - '0';
    if (cl != 3) return -1;
    static const struct { const char *w; int v; } tab[] = {
        {"零", 0}, {"一", 1}, {"二", 2}, {"两", 2}, {"三", 3}, {"四", 4},
        {"五", 5}, {"六", 6}, {"七", 7}, {"八", 8}, {"九", 9},
        {"十", 10}, {"百", 100},
    };
    for (size_t i = 0; i < sizeof(tab) / sizeof(tab[0]); i++)
        if (!memcmp(s, tab[i].w, 3)) return tab[i].v;
    return -1;
}
/* 解析从 *pos 起的数字串(贪心吃数字字/十/百), 成功写 *out 返回吃掉的字符数;
   不是数字返回 0。支持: 五十/六十/一百/10/100/百分之三十 的数字部分。 */
static size_t parse_number(const char *s, size_t from, int *out) {
    int vals[8]; size_t cls[8], n = 0, i = from;
    while (s[i] && n < 8) {
        size_t cl = utf8_len((unsigned char)s[i]);
        int v = digit_val(s + i, cl);
        if (v < 0) break;
        vals[n] = v; cls[n] = cl; n++; i += cl;
    }
    if (!n) return 0;
    /* 组合规则(只覆盖 0-100 的常见说法, 组合不了视为整体失败):
       百: 仅 "一百"(100); 十位: [d]十[d]; 纯数字串(含 ASCII 多位) */
    int value = -1;
    int has_bai = 0, has_shi = 0;
    for (size_t k = 0; k < n; k++) {
        if (vals[k] == 100) has_bai = 1;
        if (vals[k] == 10) has_shi = 1;
    }
    if (n == 1) value = vals[0];
    else if (has_bai) {
        if (n == 2 && vals[0] == 1 && vals[1] == 100) value = 100;   /* 一百 */
    } else if (has_shi) {
        int hi = 1, lo = 0, ok = 1;                                  /* 五十/十六/三十 */
        size_t idx = 0;
        if (vals[0] != 10) { hi = vals[0]; idx = 1; }
        if (idx < n) {
            if (vals[idx] != 10) ok = 0;
            if (idx + 1 < n) lo = vals[idx + 1];
        }
        if (ok && hi >= 1 && hi <= 9 && lo >= 0 && lo <= 9) value = hi * 10 + lo;
    } else {
        /* 纯数字串("50"): 逐位拼, 上限 100 */
        value = 0;
        for (size_t k = 0; k < n; k++) {
            if (vals[k] > 9) { value = -1; break; }
            value = value * 10 + vals[k];
        }
    }
    if (value < 0 || value > 100) return 0;
    *out = value;
    size_t eaten = 0;
    for (size_t k = 0; k < n; k++) eaten += cls[k];
    return eaten;
}

/* ---------- 意图关键词(长的在前, 先匹配先赢) ---------- */
static const struct { const char *w; xr_vol_op_t op; } OP_WORDS[] = {
    {"太大声", XR_VOL_DOWN},
    {"别出声", XR_VOL_MIN},
    {"静音",   XR_VOL_MIN},
    {"最小",   XR_VOL_MIN},
    {"最大",   XR_VOL_MAX},
    {"小一点", XR_VOL_DOWN},
    {"小点声", XR_VOL_DOWN},
    {"小声点", XR_VOL_DOWN},
    {"小声些", XR_VOL_DOWN},
    {"再小点", XR_VOL_DOWN},
    {"小一点", XR_VOL_DOWN},
    {"轻一点", XR_VOL_DOWN},
    {"小些",   XR_VOL_DOWN},
    {"太吵",   XR_VOL_DOWN},
    {"小点",   XR_VOL_DOWN},
    {"小声",   XR_VOL_DOWN},
    {"调小",   XR_VOL_DOWN},
    {"调低",   XR_VOL_DOWN},
    {"大一点", XR_VOL_UP},
    {"大点声", XR_VOL_UP},
    {"大声点", XR_VOL_UP},
    {"大声些", XR_VOL_UP},
    {"再大点", XR_VOL_UP},
    {"响一点", XR_VOL_UP},
    {"大些",   XR_VOL_UP},
    {"响点",   XR_VOL_UP},
    {"大点",   XR_VOL_UP},
    {"大声",   XR_VOL_UP},
    {"调大",   XR_VOL_UP},
    {"调高",   XR_VOL_UP},
};
/* 语境词与语气助词: 命中操作词后全部剥掉, 剩下的必须为空。
   "声"放最后单字兜底(最小声/最大声剥掉操作词后剩它)。 */
static const char *FILLERS[] = {
    "音量", "声音", "声儿", "百分之", "调", "到", "把", "给", "请", "帮我",
    "帮", "我", "设置", "一下", "一点儿", "一点", "儿", "弄", "搞", "开",
    "再", "点", "吧", "呢", "啊", "哦", "噢", "嗯", "了", "吗", "呀", "哈",
    "谢谢", "下", "声",
};

int xr_volume_parse(const char *text, xr_vol_op_t *op, int *value) {
    if (op) *op = XR_VOL_NONE;
    if (value) *value = 0;
    if (!text) return 0;
    size_t len = strlen(text);
    if (!len || len >= 96 || char_count(text) > 16) return 0;
    char buf[128];
    memcpy(buf, text, len + 1);
    /* 去首尾空白与常见中英标点 */
    for (;;) {
        size_t l = strlen(buf);
        if (!l) break;
        unsigned char c = (unsigned char)buf[l - 1];
        if (c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == ','
            || c == '.' || c == '!' || c == '?' || c == ';') { buf[l - 1] = 0; continue; }
        if (l >= 3) {
            unsigned char b2 = (unsigned char)buf[l - 2], b3 = (unsigned char)buf[l - 3];
            if ((c == 0x82 || c == 0x83 || c == 0x81 || c == 0x84 || c == 0x85)
                && (b2 & 0xc0) == 0x80 && (b3 & 0xc0) == 0x80) {   /* ，。！？； */
                buf[l - 3] = 0; continue;
            }
        }
        break;
    }

    /* 1. 数字(明确形态): 优先吃 "百分之<数字>"(无歧义, 先于操作词 --
       操作词里的"大一点/小一点"含数字"一", 裸数字扫描不能先跑) */
    int val = -1, had_percent = 0;
    char *pp = strstr(buf, "百分之");
    if (pp) {
        size_t off = (size_t)(pp - buf) + 9;
        int v;
        size_t eaten = parse_number(buf, off, &v);
        if (eaten) {
            val = v; had_percent = 1;
            char saved[128];
            snprintf(saved, sizeof(saved), "%.*s%s", (int)(pp - buf), buf, buf + off + eaten);
            memcpy(buf, saved, strlen(saved) + 1);
        } else {
            remove_all(buf, "百分之");   /* "百分之"后没数字: 当语境词剥掉 */
        }
    }

    /* 2. 操作词(长词优先已由表序保证); 同词出现即定方向 */
    xr_vol_op_t found = XR_VOL_NONE;
    for (size_t i = 0; i < sizeof(OP_WORDS) / sizeof(OP_WORDS[0]); i++)
        if (strstr(buf, OP_WORDS[i].w)) { found = OP_WORDS[i].op; break; }

    /* 3. 裸数字("音量50"/"X%")只在没扫到操作词时才找(避免吃掉"大一点"的"一") */
    if (found == XR_VOL_NONE && val < 0) {
        for (size_t i = 0; buf[i];) {
            int v;
            size_t eaten = parse_number(buf, i, &v);
            if (eaten) {
                val = v;
                size_t j = i + eaten;
                if (buf[j] == '%') { had_percent = 1; j++; }
                memmove(buf + i, buf + j, strlen(buf + j) + 1);
                break;
            }
            i += utf8_len((unsigned char)buf[i]);
        }
    }

    /* 4. 定性: 无操作词时 SET 的成立条件 = 有数字 且 (百分之/百分号 或 音量
       语境词) -- 裸数字("六十"这种应答句)没有语境不许抢。有操作词且带明确
       百分比数值的("声音调小到百分之二十")按 SET, 方向词只是修辞。 */
    int has_ctx = strstr(text, "音量") || strstr(text, "声音") || strstr(text, "声儿");
    if (found == XR_VOL_NONE) {
        if (val < 0 || (!had_percent && !has_ctx)) return 0;
        found = XR_VOL_SET;
    } else if (val >= 0 && had_percent && found != XR_VOL_MIN && found != XR_VOL_MAX) {
        found = XR_VOL_SET;
    }

    /* 4. 剥掉全部操作词/语境词/助词, 剩余必须为空 */
    for (size_t i = 0; i < sizeof(OP_WORDS) / sizeof(OP_WORDS[0]); i++)
        remove_all(buf, OP_WORDS[i].w);
    for (size_t i = 0; i < sizeof(FILLERS) / sizeof(FILLERS[0]); i++)
        remove_all(buf, FILLERS[i]);
    for (size_t i = 0; i < sizeof(OP_WORDS) / sizeof(OP_WORDS[0]); i++)
        remove_all(buf, OP_WORDS[i].w);   /* 删词后新邻接出的词再扫一遍 */
    remove_all(buf, "%");
    for (char *p = buf; *p; p++)
        if (*p != ' ' && *p != '\t') return 0;

    if (op) *op = found;
    if (value) *value = val;
    return 1;
}

int xr_volume_next(int current, xr_vol_op_t op, int value) {
    if (current < XR_VOLUME_MIN) current = XR_VOLUME_MIN;
    if (current > XR_VOLUME_MAX) current = XR_VOLUME_MAX;
    int next = current;
    switch (op) {
    case XR_VOL_UP:   next = current + XR_VOLUME_STEP; break;
    case XR_VOL_DOWN: next = current - XR_VOLUME_STEP; break;
    case XR_VOL_SET:  next = value; break;
    case XR_VOL_MAX:  next = XR_VOLUME_MAX; break;
    case XR_VOL_MIN:  next = XR_VOLUME_MIN; break;
    default: return current;
    }
    if (next < XR_VOLUME_MIN) next = XR_VOLUME_MIN;
    if (next > XR_VOLUME_MAX) next = XR_VOLUME_MAX;
    return next;
}

void xr_volume_apply(int16_t *pcm, size_t n, int pct) {
    if (pct <= 0 || pct >= 100) return;   /* 100 逐位直通; 0/负=未设置 */
    for (size_t i = 0; i < n; i++) {
        int32_t v = (int32_t)pcm[i] * pct / 100;
        if (v > 32767) v = 32767;
        else if (v < -32768) v = -32768;
        pcm[i] = (int16_t)v;
    }
}

int xr_volume_load(const char *path, int fallback) {
    if (!path || !*path) return fallback;
    FILE *f = fopen(path, "r");
    if (!f) return fallback;
    int v = -1;
    if (fscanf(f, "%d", &v) != 1) v = -1;
    fclose(f);
    if (v < XR_VOLUME_MIN || v > XR_VOLUME_MAX) return fallback;
    return v;
}

void xr_volume_save(const char *path, int pct) {
    if (!path || !*path) return;
    if (pct < XR_VOLUME_MIN) pct = XR_VOLUME_MIN;
    if (pct > XR_VOLUME_MAX) pct = XR_VOLUME_MAX;
    FILE *f = fopen(path, "w");
    if (!f) { fprintf(stderr, "[xr] 音量写不进 %s\n", path); return; }
    fprintf(f, "%d\n", pct);
    fclose(f);
}
