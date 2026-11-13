#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "json_field.h"

static int hex_digit(unsigned char value) {
    if (value >= '0' && value <= '9') return value - '0';
    if (value >= 'a' && value <= 'f') return value - 'a' + 10;
    if (value >= 'A' && value <= 'F') return value - 'A' + 10;
    return -1;
}

static int read_hex(const char **cursor, uint32_t *value) {
    *value = 0;
    for (int index = 0; index < 4; index++)
    {
        int digit = hex_digit((unsigned char)**cursor);
        if (digit < 0) return -1;
        *value = (*value << 4) | (uint32_t)digit;
        (*cursor)++;
    }
    return 0;
}

static int read_string(const char **cursor, char *output, size_t capacity) {
    if (**cursor != '"' || !capacity) return -1;
    (*cursor)++;
    size_t used = 0;
    bool full = false;
    while (**cursor && **cursor != '"')
    {
        unsigned char bytes[4];
        size_t count = 1;
        uint32_t codepoint = (unsigned char)*(*cursor)++;
        if (codepoint == '\\')
        {
            unsigned char escape = (unsigned char)**cursor;
            if (!escape) return -1;
            (*cursor)++;
            switch (escape)
            {
                case '"': case '\\': case '/': codepoint = escape; break;
                case 'b': codepoint = '\b'; break;
                case 'f': codepoint = '\f'; break;
                case 'n': codepoint = '\n'; break;
                case 'r': codepoint = '\r'; break;
                case 't': codepoint = '\t'; break;
                case 'u':
                    if (read_hex(cursor, &codepoint) != 0) return -1;
                    if (codepoint >= 0xd800 && codepoint <= 0xdbff)
                    {
                        if ((*cursor)[0] != '\\' || (*cursor)[1] != 'u') return -1;
                        *cursor += 2;
                        uint32_t low;
                        if (read_hex(cursor, &low) != 0 || low < 0xdc00 || low > 0xdfff)
                            return -1;
                        codepoint = 0x10000 + ((codepoint - 0xd800) << 10) + low - 0xdc00;
                    }
                    else if (codepoint >= 0xdc00 && codepoint <= 0xdfff)
                        return -1;
                    if (!codepoint) return -1;
                    break;
                default: return -1;
            }
        }
        else if (codepoint < 0x20)
            return -1;
        else if (codepoint >= 0x80)
        {
            unsigned char lead = (unsigned char)codepoint;
            if (lead >= 0xc2 && lead <= 0xdf) count = 2;
            else if (lead >= 0xe0 && lead <= 0xef) count = 3;
            else if (lead >= 0xf0 && lead <= 0xf4) count = 4;
            else return -1;
            codepoint = lead & ((1u << (7 - count)) - 1);
            for (size_t index = 1; index < count; index++)
            {
                unsigned char next = (unsigned char)**cursor;
                if ((next & 0xc0) != 0x80) return -1;
                codepoint = (codepoint << 6) | (next & 0x3f);
                (*cursor)++;
            }
            if ((count == 2 && codepoint < 0x80) || (count == 3 && codepoint < 0x800) ||
                (count == 4 && codepoint < 0x10000) || codepoint > 0x10ffff ||
                (codepoint >= 0xd800 && codepoint <= 0xdfff)) return -1;
        }
        if (codepoint < 0x80)
            bytes[0] = (unsigned char)codepoint;
        else if (codepoint < 0x800)
        {
            count = 2;
            bytes[0] = 0xc0 | (codepoint >> 6);
            bytes[1] = 0x80 | (codepoint & 0x3f);
        }
        else if (codepoint < 0x10000)
        {
            count = 3;
            bytes[0] = 0xe0 | (codepoint >> 12);
            bytes[1] = 0x80 | ((codepoint >> 6) & 0x3f);
            bytes[2] = 0x80 | (codepoint & 0x3f);
        }
        else
        {
            count = 4;
            bytes[0] = 0xf0 | (codepoint >> 18);
            bytes[1] = 0x80 | ((codepoint >> 12) & 0x3f);
            bytes[2] = 0x80 | ((codepoint >> 6) & 0x3f);
            bytes[3] = 0x80 | (codepoint & 0x3f);
        }
        if (!full && used + count < capacity)
        {
            memcpy(output + used, bytes, count);
            used += count;
        }
        else
            full = true;
    }
    if (**cursor != '"') return -1;
    (*cursor)++;
    output[used] = 0;
    return 0;
}

int xr_json_field_string(const char *json, const char *key, char *output, size_t capacity) {
    if (!json || !key || !output || !capacity) return -1;
    output[0] = 0;
    int depth = 0;
    const char *cursor = json;
    while (*cursor)
    {
        if (*cursor == '"')
        {
            char candidate[64];
            if (read_string(&cursor, candidate, sizeof(candidate)) != 0) return -1;
            while (*cursor == ' ' || *cursor == '\t' || *cursor == '\r' || *cursor == '\n')
                cursor++;
            if (depth == 1 && *cursor == ':' && !strcmp(candidate, key))
            {
                cursor++;
                while (*cursor == ' ' || *cursor == '\t' || *cursor == '\r' || *cursor == '\n')
                    cursor++;
                if (read_string(&cursor, output, capacity) == 0) return 1;
                output[0] = 0;
                return -1;
            }
        }
        else
        {
            if (*cursor == '{' || *cursor == '[') depth++;
            if (*cursor == '}' || *cursor == ']') depth--;
            if (depth < 0) return -1;
            cursor++;
        }
    }
    return 0;
}
