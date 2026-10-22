/* json_escape.h — JSON 字符串字面量转义(不含外层引号)。
 *
 * 为什么需要它: 两个后端都把 ASR 文本原样拼进 listen 报文。ASR 文本取自
 * 模型 JSON 的引号之间(speech_pipeline.c 只剥尾部语气词), 不做任何字符清洗,
 * 所以控制字符(换行/制表/回车...)会原样进入报文。RFC 8259 要求 U+0000..
 * U+001F 必须转义, 否则整条 JSON 非法 —— 服务端要么拒收要么错位解析, 而
 * 前端只看到"发出去没报错", 表现为**整轮静默丢失**。
 *
 * RFC 8259 §7 要求转义 " \\ 以及全部 U+0000..U+001F; 其中 \b \t \n \f \r
 * 有短形式, 其余写成 \u00XX。DEL(0x7F) 与 C1(U+0080..U+009F) 按 RFC 允许
 * 原样透传(UTF-8 下本就是多字节序列的前导), 不做处理。
 * / < > & 不转义 —— JSON 不需要, 转了只让线上报文变难读。 */
#ifndef XR_JSON_ESCAPE_H
#define XR_JSON_ESCAPE_H
#include <stddef.h>

/* 把 text 转义成可安全嵌入 JSON 字符串字面量的字节序列(不含两侧引号)。
   写入 out(容量 cap, 恒以 NUL 结尾), 返回写入字节数(不含 NUL)。
   空间不足时截断; 截断点永不落在一条多字节转义中间。

   契约: text 是 NUL 结尾的 C 字符串 —— 遇到 NUL 即结束。调用方(两个后端的
   submit_text)拿到的都是 C 字符串, 所以不存在"数据里的 NUL"。 */
static inline size_t xr_json_escape(const char *text, char *out, size_t cap) {
    static const char HEX[] = "0123456789abcdef";
    size_t n = 0;
    if (cap == 0) return 0;
    for (const unsigned char *p = (const unsigned char *)text; *p; ++p) {
        char esc[7];
        size_t elen = 1;
        esc[0] = (char)*p;
        switch (*p) {
            case '"':  esc[0] = '\\'; esc[1] = '"';  elen = 2; break;
            case '\\': esc[0] = '\\'; esc[1] = '\\'; elen = 2; break;
            case '\b': esc[0] = '\\'; esc[1] = 'b';  elen = 2; break;
            case '\f': esc[0] = '\\'; esc[1] = 'f';  elen = 2; break;
            case '\n': esc[0] = '\\'; esc[1] = 'n';  elen = 2; break;
            case '\r': esc[0] = '\\'; esc[1] = 'r';  elen = 2; break;
            case '\t': esc[0] = '\\'; esc[1] = 't';  elen = 2; break;
            default:
                if (*p < 0x20) {
                    esc[0] = '\\'; esc[1] = 'u'; esc[2] = '0'; esc[3] = '0';
                    esc[4] = HEX[*p >> 4]; esc[5] = HEX[*p & 0xf];
                    elen = 6;
                }
                break;
        }
        /* 先判容量再落盘: 半个 \u00 或尾随反斜杠都会造出非法 JSON */
        if (n + elen >= cap) break;
        for (size_t i = 0; i < elen; ++i) out[n + i] = esc[i];
        n += elen;
    }
    out[n] = 0;
    return n;
}
#endif