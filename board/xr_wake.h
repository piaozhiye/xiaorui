#ifndef XR_WAKE_H
#define XR_WAKE_H

#include <stddef.h>
#include <stdio.h>
#include <string.h>

/* Text-wake fallback until a dedicated acoustic KWS is validated on the board.
 * Return 0 = ignore, 1 = wake only, 2 = command in out. */
static const char *xr_skip_separators(const char *p) {
    for (;;) {
        if (*p == ' ' || *p == '\t' || *p == ',' || *p == '.') { ++p; continue; }
        if (!strncmp(p, "，", 3) || !strncmp(p, "。", 3) ||
            !strncmp(p, "！", 3) || !strncmp(p, "？", 3)) { p += 3; continue; }
        return p;
    }
}

/* 拉丁形态的唤醒词: ASR 听"你好小瑞"有时直接吐 Latin 拼写(上游 ITN 也
 * 会这么干)。服务端那侧已经认(实例配置 wakeup_words + 大小写不敏感比较),
 * 前端不认的话这一轮会白绕一次云往返才被接住, 而 LLM 的应答 TTS 还会把
 * 随后的指令段整个吃掉。单独一张表, 不混进汉字变体表。 */
static const char *xr_wake_latin_forms[] = {
    "xiaorui", "你好xiaorui", NULL
};

/* ASCII 大小写不敏感的前缀比较(自己写: -std=c11 下 strncasecmp 不保证可用) */
static int xr_prefix_nocase(const char *p, const char *prefix) {
    for (size_t i = 0; prefix[i]; ++i) {
        char a = p[i], b = prefix[i];
        if (!a) return 0;
        if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
        if (b >= 'A' && b <= 'Z') b = (char)(b - 'A' + 'a');
        if (a != b) return 0;
    }
    return 1;
}

/* 拉丁形态的唤醒前缀, 返回其后的指针; 没有则 NULL。 */
static const char *xr_skip_wake_latin(const char *p) {
    for (int i = 0; xr_wake_latin_forms[i]; ++i) {
        if (xr_prefix_nocase(p, xr_wake_latin_forms[i])) return p + strlen(xr_wake_latin_forms[i]);
    }
    return NULL;
}

/* 整句(去分隔符, 忽略 ASCII 大小写)是不是拉丁形态的唤醒词。
 * 汉字原样保留: "你好xiaorui" 是合法形态(带"你好"前缀), 而
 * "xiaorui帮我开灯" 是一句式(唤醒+指令) —— 整句不在表里自然就不匹配,
 * 命令不会丢。 */
static int xr_is_wake_only_latin(const char *text) {
    if (!text) return 0;
    char buf[96];
    size_t n = 0;
    for (const char *p = xr_skip_separators(text); *p; ) {
        if (*p == ' ' || *p == '\t' || *p == ',' || *p == '.') { ++p; continue; }
        if (n + 4 >= sizeof(buf)) return 0;          /* 放不下: 不是唤醒词 */
        if (*p >= 'A' && *p <= 'Z') buf[n++] = (char)(*p - 'A' + 'a');
        else buf[n++] = *p;
        ++p;
    }
    buf[n] = 0;
    for (int i = 0; xr_wake_latin_forms[i]; ++i)
        if (!strcmp(buf, xr_wake_latin_forms[i])) return 1;
    return 0;
}

static const char *xr_skip_wake_unit(const char *p) {
    static const char *variants[] = {
        /* 前排=高频听岔(实测房间链路 ASR 渲染), 后排=冷门。
         * 已核对: 120 条负例转写中不存在 小+任意变体 的双单元组合。
         * 2026-10-20 新增 想/看 两字: 板端实测唤醒词被本地 RKNN 转写成
         * 小想 / 小看看看(见 xr_is_wake_candidate), 双单元形态要能认。 */
        "瑞", "翠", "队", "子", "薇", "妹", "绿", "贵", "月",
        "院", "丽", "热", "略", "玉", "想", "看", NULL
    };
    if (strncmp(p, "小", 3)) return NULL;
    p += 3;
    /* ASR 有时把开头听成“小小瑞”；只容许一个重复。 */
    if (!strncmp(p, "小", 3)) p += 3;
    for (int i = 0; variants[i]; ++i) {
        size_t n = strlen(variants[i]);
        if (!strncmp(p, variants[i], n)) return p + n;
    }
    return NULL;
}

/* 常见礼貌前缀, 用户会说"麻烦小瑞小瑞…"“请问小瑞小瑞…”。
 * 只白名单这几个, 不做任意前缀匹配: 句中任意位置含唤醒词不应放行。 */
static const char *xr_skip_leading_filler(const char *p) {
    static const char *fillers[] = {
        "哎", "嘿", "你好", "麻烦", "请问", "那个", "帮我", "请", NULL
    };
    for (int i = 0; fillers[i]; ++i) {
        size_t n = strlen(fillers[i]);
        if (!strncmp(p, fillers[i], n))
            return xr_skip_separators(p + n);
    }
    return p;
}

/* 在句中任意位置找 "小X小X" 双唤醒单元; 找到返回第二个单元之后的指针。
 * 仅在声学 KWS 已命中时使用(见 xiaorui_live 唤醒门): 变体表保证真负例
 * 语料中不存在双单元结构, 配合 KWS 前置门不会引入新误触。 */
static const char *xr_find_wake_mid(const char *p) {
    while (*p) {
        const char *q = xr_skip_wake_unit(p);
        if (q) {
            const char *sep = xr_skip_separators(q);
            const char *r = xr_skip_wake_unit(sep);
            if (r) return xr_skip_separators(r);
            p = q;
        } else {
            /* 前进一个 UTF-8 字符 */
            if ((*p & 0xE0) == 0xC0) p += 2;
            else if ((*p & 0xF0) == 0xE0) p += 3;
            else if ((*p & 0xF8) == 0xF0) p += 4;
            else p += 1;
        }
    }
    return NULL;
}

/* UTF-8 字符数: "单单位放宽档"要求尾巴 >=2 字(单字尾巴是回声特征, 不放行) */
static int xr_utf8_chars(const char *p) {
    int n = 0;
    if (!p) return 0;
    while (*p) {
        if ((*p & 0xE0) == 0xC0) p += 2;
        else if ((*p & 0xF0) == 0xE0) p += 3;
        else if ((*p & 0xF8) == 0xF0) p += 4;
        else p += 1;
        n++;
    }
    return n;
}

/* 句中任意位置找**单个**唤醒单元, 返回其后的指针(已跳过分隔符);
 * 没有任何唤醒单元返回 NULL(不做"无唤醒词也放行")。
 * 仅在声学 KWS 已命中时使用: KWS 是主门(阈值 0.15 已在 120 条负例上零误唤醒),
 * 文本门在此只做二次过滤。之所以需要单单位档: ASR 抖动掉一个字时双单元门会把
 * 整轮丢弃(实测 "瑞小瑞你叫什么名字" 被判无双唤醒词而忽略 = 用户喊了没反应)。 */
static const char *xr_find_wake_once(const char *p) {
    while (p && *p) {
        const char *q = xr_skip_wake_unit(p);
        if (q) return xr_skip_separators(q);
        if ((*p & 0xE0) == 0xC0) p += 2;
        else if ((*p & 0xF0) == 0xE0) p += 3;
        else if ((*p & 0xF8) == 0xF0) p += 4;
        else p += 1;
    }
    return NULL;
}

/* KWS 命中时的唤醒门。返回 0=忽略 / 1=纯唤醒 / 2=指令(写入 out)。
 * 档位顺序: 双单元(句中任意位置)优先, 其次单单位兜底。
 * 单单位兜底的护栏: 必须有唤醒单元; 尾巴 >=2 字(单字尾巴是回声特征,
 * 放行会造成回声自问自答)。*single_unit 置 1 表示走了单单位档(仅用于日志)。
 * 只在声学 KWS 已命中时调用 —— KWS 才是主门, 这里只是二次过滤。 */
static int xr_is_wake_only_kws(const char *text);

static int xr_parse_wake_kws(const char *text, char *out, size_t cap,
                             int *single_unit) {
    if (single_unit) *single_unit = 0;
    if (!text || !out || cap == 0) return 0;
    out[0] = 0;   /* 忽略时也不留旧内容, 调用方不会误用上一次的指令 */
    if (xr_is_wake_only_latin(text)) return 1;   /* 拉丁形态唤醒词 */
    const char *rest = xr_find_wake_mid(text);
    if (rest) {
        if (*rest) { snprintf(out, cap, "%s", rest); return 2; }
        return 1;
    }
    /* A cold wake can contain the same ASR drift as an in-window wake.
     * Check the complete wake-only shape before interpreting a second
     * misheard unit as a command (observed: 小院小路 -> command 小路). */
    if (xr_is_wake_only_kws(text)) return 1;
    rest = xr_find_wake_once(text);
    if (!rest) return 0;
    if (*rest && xr_utf8_chars(rest) < 2) return 0;
    if (single_unit) *single_unit = 1;
    if (*rest) { snprintf(out, cap, "%s", rest); return 2; }
    out[0] = 0;
    return 1;
}

/* 窗内归纯唤醒必须匹配全文，不能复用会丢弃任意前缀的句中查找。
 * KWS 已命中时容许双单元中一个字听岔，但至少一个单元在已有变体表内。 */
static int xr_is_wake_only_kws(const char *text) {
    if (!text) return 0;
    const char *p = xr_skip_leading_filler(xr_skip_separators(text));
    int known = 0, units = 0;
    while (*p && units < 2) {
        const char *q = xr_skip_wake_unit(p);
        if (q) {
            known = 1;
        } else {
            if (strncmp(p, "小", 3)) return 0;
            q = p + 3;
            /* 只接受完整的三字节汉字，不把 ASCII 或任意短句当听岔。 */
            if (!q[0] || !q[1] || !q[2]) return 0;
            const unsigned char *u = (const unsigned char *)q;
            if ((u[0] & 0xf0) != 0xe0 || (u[1] & 0xc0) != 0x80
                || (u[2] & 0xc0) != 0x80) return 0;
            unsigned code = ((u[0] & 15u) << 12) | ((u[1] & 63u) << 6) | (u[2] & 63u);
            if (code < 0x4e00 || code > 0x9fff) return 0;
            q += 3;
        }
        ++units;
        p = xr_skip_separators(q);
    }
    return known && units > 0 && !*p;
}

/* 纯唤醒的**形状**判定: "小?小?"(4 个汉字, 第 1/3 个是"小")。
 * 变体表(xr_skip_wake_unit)只收了 14 个实测听岔字, 表外的听岔(实测
 * "小看小看")会让纯唤醒被判成指令上云, 服务端再判一次(两端表不一致)
 * 唤醒词就被当成普通语句喂给 LLM(实测回"🙂"), LLM 的 TTS 还会把随后的
 * 指令段吃掉(实测云 ASR 只听到 "Okay")。形状本身是稳的: 120 条负例转写里
 * 不存在 "小?小?" 结构, 且只在声学 KWS 已命中时使用(强前提)。 */
static inline int xr_is_wake_only_shape(const char *text) {
    if (!text) return 0;
    const char *p = xr_skip_leading_filler(xr_skip_separators(text));
    for (int i = 0; i < 4; ++i) {
        if (!p || !p[0] || !p[1] || !p[2]) return 0;
        const unsigned char *u = (const unsigned char *)p;
        if ((u[0] & 0xf0) != 0xe0 || (u[1] & 0xc0) != 0x80 || (u[2] & 0xc0) != 0x80)
            return 0;
        unsigned code = ((u[0] & 15u) << 12) | ((u[1] & 63u) << 6) | (u[2] & 63u);
        if (code < 0x4e00 || code > 0x9fff) return 0;
        if (i % 2 == 0 && code != 0x5c0fu) return 0;   /* "小" = U+5C0F */
        p += 3;
    }
    p = xr_skip_separators(p);
    return *p == 0;
}

/* 云 ASR 模式下的唤醒门决定(声学 KWS 已命中后调用)。
 * 返回 1 = 纯唤醒(只播确认音+开跟说窗, **不上云**)
 * 返回 2 = 指令(整句上云, 云端识别全文)
 * 纯唤醒绝不能上云: 服务端的唤醒词门只认配置里的精确字符串, 两端变体表
 * 不一致时唤醒词会被当成普通语句喂给 LLM, 而 LLM 的应答 TTS 还会把
 * 随后的指令段整个吃掉(实测云 ASR 只听到 "Okay")。 */
static inline int xr_cloud_asr_wake_action(const char *text) {
    return xr_is_wake_only_shape(text) ? 1 : 2;
}

/* 形状像唤醒词、但本地变体表没通过(单单元"小想"/"小看看看", 或拉丁
 * "xiao…")。用在云端模式下: 不再一票否决, 整句交服务端唤醒词门裁决。
 * 实测(2026-10-20): 唤醒词确实被听见, 本地 RKNN 却转写成"小想"/"小看看看",
 * 被"未含唤醒词, 忽略"直接删掉, 云端根本没机会看到 —— 而云 ASR 的转写
 * 质量足以认出"小瑞小瑞"。 */
static inline int xr_is_wake_candidate(const char *text) {
    if (!text) return 0;
    const char *p = xr_skip_leading_filler(xr_skip_separators(text));
    if (!*p) return 0;
    if (!strncmp(p, "小", 3)) return 1;
    if (xr_prefix_nocase(p, "xiao")) return 1;
    return 0;
}

static int xr_parse_wake_text(const char *text, int in_window, char *out, size_t cap) {
    if (!text || !out || cap == 0) return 0;
    out[0] = 0;
    if (xr_is_wake_only_latin(text)) return 1;   /* 拉丁形态唤醒词: 纯唤醒 */
    /* 拉丁形态 + 指令("xiaorui帮我开灯"): 剥掉前缀, 命令部分照常走 */
    {
        const char *q = xr_skip_leading_filler(xr_skip_separators(text));
        const char *r = xr_skip_wake_latin(q);
        if (r) {
            r = xr_skip_separators(r);
            if (!*r) return 1;
            snprintf(out, cap, "%s", r);
            return 2;
        }
    }
    const char *p = xr_skip_separators(text);
    if (in_window) {
        if (!*p) return 0;
        /* 窗内用户会再喊一遍唤醒词: 应视为重新纯唤醒(ack+续窗), 不是指令。
           实测(2026-10-12 声学闭环): 窗内"小瑞小瑞"全文送 LLM, 服务端唤醒词
           剥除后仅得零文本/表情, 客户端整轮听不到声音。 */
        {
            const char *q = xr_skip_leading_filler(p);
            const char *u1 = xr_skip_wake_unit(q);
            if (u1) {
                const char *sep = xr_skip_separators(u1);
                const char *u2 = xr_skip_wake_unit(sep);
                if (u2 && !*xr_skip_separators(u2)) return 1;
            }
        }
        snprintf(out, cap, "%s", p);
        return 2;
    }
    p = xr_skip_leading_filler(p);
    p = xr_skip_wake_unit(p);
    if (!p) return 0;
    p = xr_skip_separators(p);
    p = xr_skip_wake_unit(p);
    if (!p) return 0;
    p = xr_skip_separators(p);
    if (!*p) return 1;
    snprintf(out, cap, "%s", p);
    return 2;
}

#endif
