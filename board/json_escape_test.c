/* json_escape_test.c — JSON 字符串转义的门禁。
 *
 * 契约: xr_json_escape 的输出里不允许出现任何裸 C0 控制字符(U+0000..U+001F),
 * 且转义序列本身不会被截断成半个 —— 否则拼进 listen 报文就是非法 JSON,
 * 服务端拒收, 前端表现为整轮静默丢失。 */
#include "json_escape.h"
#include <stdio.h>
#include <string.h>

static int failures;
#define CHECK(cond, msg) do { \
    if (!(cond)) { failures++; fprintf(stderr, "FAIL %d: %s\n", __LINE__, (msg)); } \
} while (0)

static int has_raw_c0(const char *s) {
    for (const unsigned char *p = (const unsigned char *)s; *p; ++p)
        if (*p < 0x20) return 1;
    return 0;
}

/* Python json.loads 必须接受拼出来的整条报文 —— 由 tools/ 的 python 门禁复核,
   这里只守 C 侧的转义契约。 */
static void check(const char *in, const char *want, const char *what) {
    char out[256];
    xr_json_escape(in, out, sizeof out);
    if (has_raw_c0(out)) {
        failures++;
        fprintf(stderr, "FAIL %s: 输出含裸控制字符: ", what);
        for (const unsigned char *p = (const unsigned char *)out; *p; ++p)
            fprintf(stderr, "%02x ", *p);
        fprintf(stderr, "\n");
    }
    if (strcmp(out, want)) {
        failures++;
        fprintf(stderr, "FAIL %s: 期望 [%s] 实得 [%s]\n", what, want, out);
    }
}

int main(void) {
    char out[256];

    check("现在几点了", "现在几点了", "纯中文原样透传");
    check("hello \"world\"", "hello \\\"world\\\"", "双引号");
    check("a\\b", "a\\\\b", "反斜杠");
    check("换行\n换行", "换行\\n换行", "换行必须转义");
    check("制表\t符", "制表\\t符", "制表必须转义");
    check("回车\r", "回车\\r", "回车必须转义");
    check("退格\b换页\f", "退格\\b换页\\f", "退格/换页短转义");
    check("\x01\x1f", "\\u0001\\u001f", "其余 C0 用 \\u00XX");

    /* UTF-8 多字节必须原样透传(不能按字节转义成 \\u) */
    check("瑞小瑞", "瑞小瑞", "UTF-8 透传");

    /* 截断: 绝不切在转义序列中间, 且永远 NUL 结尾 */
    static const char nasty[] = "a\nb\tc";
    for (size_t cap = 1; cap <= 8; ++cap) {
        xr_json_escape(nasty, out, cap);
        CHECK(strlen(out) < cap, "截断后必须留出 NUL 位");
        CHECK(!has_raw_c0(out), "截断后不得留半个转义(会漏出裸控制字符)");
        /* 尾部若是反斜杠, 说明 '\\' 被切开了 */
        size_t l = strlen(out);
        CHECK(l == 0 || out[l - 1] != '\\', "截断不得落在反斜杠之后");
    }
    /* cap=0 不写任何东西 */
    CHECK(xr_json_escape("x", out, 0) == 0, "cap=0 应返回 0");

    /* 精确容量: "a\n" 转义成 3 字节, 加 NUL 需要 cap=4; cap=3 只能放下 "a" */
    xr_json_escape("a\n", out, 3);
    CHECK(!strcmp(out, "a"), "cap=3 时只放得下 a, 不得留半个转义");
    xr_json_escape("a\n", out, 4);
    CHECK(!strcmp(out, "a\\n"), "cap=4 刚好放下完整转义");
    /* 半个 \u: \u0001 要 6 字节, cap=5 时必须整条丢掉而不是写 "\\u00" */
    xr_json_escape("\x01", out, 5);
    CHECK(out[0] == 0, "cap=5 放不下 6 字节转义时应整条丢弃");
    CHECK(xr_json_escape("\x01", out, 7) == 6, "cap=7 恰好放下 6 字节转义");

    fprintf(stderr, failures ? "json_escape_test: %d FAILED\n"
                             : "json_escape_test: OK\n", failures);
    return failures ? 1 : 0;
}