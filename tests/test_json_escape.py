#!/usr/bin/env python3
"""JSON 转义报文的真实解析器复核。

board/json_escape_test.c 只守字节契约(无裸 C0、转义不截断), 证明不了
拼出来的 listen 报文真的能被 JSON 解析器接受。这里用宿主机的 json 解析器
跑同一份 C 实现, 把"转义正确"升级成"报文合法"。

做法: 把一个故意刁难的字节串写进文件(含 NUL, 所以带长度), C probe 按长度
fread 进来 -> 调 xr_json_escape -> 拼成 listen 报文 -> 以十六进制打印;
本脚本再喂给 json.loads, 断言字段原样还原。
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BOARD = os.path.join(REPO, "board")

# 除 \b\f\n\r\t 外的 C0 控制字符 + 引号 + 反斜杠 + 中文。
# 不含 NUL: 输入按契约是 C 字符串, NUL 是终止符而不是数据, 放进 1024 缓冲区
# 测试时会被天然截断, 那不是转义的锅(见 json_escape.h 的契约说明)。
NASTY = (
    "现在几点了\n"
    "第二行\t制表\r回车\x0b竖制表\x0c换页\x08退格\x01SOH\x1fUS"
    '带"引号"和\\反斜杠\\'
    "结尾"
).encode("utf-8")

PROBE = r"""
#include "json_escape.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(int argc, char **argv) {
    if (argc != 3) return 2;
    size_t len = (size_t)atoi(argv[2]);
    char *in = malloc(len + 1);
    if (!in) return 2;
    FILE *f = fopen(argv[1], "rb");
    if (!f || fread(in, 1, len, f) != len) return 2;
    fclose(f);
    in[len] = 0;
    char esc[1024];
    xr_json_escape(in, esc, sizeof esc);
    char msg[1400];
    snprintf(msg, sizeof msg,
             "{\"type\":\"listen\",\"state\":\"detect\",\"text\":\"%s\"}", esc);
    /* 十六进制打印, 免得控制字符/引号把 stdout 搞乱 */
    for (const unsigned char *p = (const unsigned char *)msg; *p; ++p)
        printf("%02x", *p);
    free(in);
    return 0;
}
"""


def main():
    if not re.search(rb"[\x01\x1f]", NASTY):
        print("probe 输入必须含控制字符", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as td:
        datapath = os.path.join(td, "nasty.bin")
        cpath = os.path.join(td, "probe.c")
        binp = os.path.join(td, "probe")
        with open(datapath, "wb") as f:
            f.write(NASTY)
        with open(cpath, "w", encoding="utf-8") as f:
            f.write(PROBE)
        cc = subprocess.run(
            ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
             "-I", BOARD, cpath, "-o", binp],
            capture_output=True, text=True,
        )
        if cc.returncode != 0:
            print("编译 probe 失败:\n" + cc.stderr, file=sys.stderr)
            return 1
        proc = subprocess.run([binp, datapath, str(len(NASTY))],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            print("probe 运行失败 rc=%d\n%s" % (proc.returncode, proc.stderr),
                  file=sys.stderr)
            return 1

    msg = bytes.fromhex(proc.stdout).decode("utf-8")
    try:
        parsed = json.loads(msg)
    except json.JSONDecodeError as exc:
        print("FAIL: 转义后的报文不是合法 JSON: %s\n%s" % (exc, msg),
              file=sys.stderr)
        return 1

    expected = NASTY.decode("utf-8")
    if parsed.get("text") != expected:
        print("FAIL: text 字段未原样还原\n  期望 %r\n  实得 %r"
              % (expected, parsed.get("text")), file=sys.stderr)
        return 1
    if parsed.get("type") != "listen" or parsed.get("state") != "detect":
        print("FAIL: 外层报文结构被破坏: %r" % parsed, file=sys.stderr)
        return 1
    print("json-escape-roundtrip: PASS (%d 字节, 13 个控制字符往返无损)"
          % len(msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())