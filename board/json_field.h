#ifndef XR_JSON_FIELD_H
#define XR_JSON_FIELD_H

#include <stddef.h>

/* 从一段 JSON 对象文本里取顶层字符串字段, 结果按 JSON 字符串转义规则解码
 * (\" \\ \n 等, 以及 \uXXXX -> UTF-8, 含代理对)。
 * 返回 1=取到, 0=该字段不存在, -1=JSON 非法或字段值不是字符串。 */
int xr_json_field_string(const char *json, const char *key, char *output, size_t capacity);

#endif
