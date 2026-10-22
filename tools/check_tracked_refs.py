#!/usr/bin/env python3
"""检查: 被跟踪的构建/测试脚本所引用的仓库内文件, 是否也已被跟踪。

为什么需要: 2026-10-15 实机更新程序时发现 `board/audio_cue.c` 与 `audio_cue.h`
长期未跟踪, 却是 build-board-live.sh 的必需源文件 —— 结果**干净克隆完全构建不出
本程序**(编译器直接报 "audio_cue.h: 没有那个文件")。仓库没有如实记录将要装到
板子上的东西, 任何新机器/新克隆/`git stash` 都会踩。

同批查出的还有: tools/run-gates.sh 引用了 3 个未跟踪的测试脚本,
tools/run-board-acceptance.sh 引用了未跟踪的 board-server/restore-frontend.sh。

tests/audio/ 下的测试音频在 .gitignore 里(测试音不入库是对的), 因此单独归类为
WARNING 而不是 FAIL; 但要显式列出, 不然就是无声的缺口。
"""
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 构建/测试工具链: 只看这些, 因为只有它们决定"程序能不能被复现出来"
TOOLCHAIN_GLOBS = ["*.sh", "tools/*.sh", "tools/*.py", "board-server/*.sh"]
# 上游生成物/第三方引导脚本: 不是本仓库的源码, 扫它只会报出 pip 包路径之类的
# 假阳性。board-server/miniforge.sh 是 50 万行的 Miniforge 安装器。
# 本脚本自身也排除: 它的文档字符串里举了 "board/x.c" 这类例子, 会被自己的
# 正则当成真实引用(自指)。
SKIP_SCRIPTS = {"board-server/miniforge.sh", "tools/check_tracked_refs.py"}
# 超过这个行数的脚本同样按上游生成物处理, 避免把闸门变慢
MAX_SCAN_LINES = 5000
# 允许未跟踪的路径前缀(测试音等本地资产), 单独报 WARNING
ALLOWED_UNTRACKED_PREFIX = ("tests/audio/", "vendor/")
# vendor/ 是本地维护件(瑞芯微专有, 不随仓分发, 见 docs/PUBLICATION.md):
# 引用它的构建/契约脚本只服务维护机, 缺件时对应门禁自动跳过。

# 形如 "$DIR/board/x.c" / "${REPO}/tests/y.py" / "tools/z.sh" 的引用。
# 变量前缀必须连同斜杠一起吃掉 —— 否则 "$DIR" 后面紧跟的是 "/board...",
# 捕获组要求以 board/ 开头就匹配不上(这个坑踩过一次, 门禁假 PASS)。
REF_RE = re.compile(
    rb"(?:\$\{?(?:DIR|REPO)\}?/)?"
    rb"((?:board|tools|tests|vendor|board-server)/[A-Za-z0-9_./-]+"
    rb"\.(?:c|h|py|sh|wav|onnx|so))"
)


def git(*args):
    return subprocess.run(["git", "-C", REPO, *args],
                          capture_output=True, text=True, check=True).stdout


def toolchain_scripts():
    out = set()
    for pattern in TOOLCHAIN_GLOBS:
        out.update(git("ls-files", pattern).split())
    return sorted(out - SKIP_SCRIPTS)


def main():
    tracked = set(git("ls-files").split())
    missing, ignored = [], []
    for script in toolchain_scripts():
        try:
            with open(os.path.join(REPO, script), "rb") as fh:
                blob = fh.read()
        except OSError:
            continue
        if blob.count(b"\n") > MAX_SCAN_LINES:
            continue                            # 上游生成物, 同上
        for ref in sorted({m.decode() for m in REF_RE.findall(blob)}):
            full = os.path.join(REPO, ref)
            exists = os.path.exists(full)
            is_code = ref.endswith((".c", ".h", ".py", ".sh"))
            # 顺序要紧: 先看存不存在, 再看是否被跟踪。git ls-files 含暂存区,
            # 已 git add 过但文件被移走时它仍算"已跟踪" —— 先查 tracked 会漏掉
            # 最严重的那一类(源文件压根不存在, 干净克隆直接编译失败)。
            if not exists:
                # 源文件缺失比未跟踪更严重; 白名单前缀(tests/audio、vendor)
                # 是有意不入库的本地可选资产, 缺失只提醒不挡门禁——干净克隆里
                # 对应门禁会自行跳过(见 run-gates.sh 的 vqe SKIP)。
                (ignored if ref.startswith(ALLOWED_UNTRACKED_PREFIX) or not is_code
                 else missing).append((ref, script, "缺失"))
            elif ref not in tracked:
                (ignored if ref.startswith(ALLOWED_UNTRACKED_PREFIX)
                 else missing).append((ref, script, "未跟踪"))

    for ref, script, why in sorted(ignored):
        print(f"WARNING {why}(白名单内的本地可选资产, 不随仓分发): {ref}  <- {script}")

    if missing:
        print("FAIL 以下被工具链引用的文件缺失或未被 git 跟踪 —— "
              "干净克隆将无法构建/跑门禁:", file=sys.stderr)
        for ref, script, why in sorted(missing):
            print(f"  [{why}] {ref}  <- {script}", file=sys.stderr)
        print("\n修复: git add <文件>（若确实不该入库, 改掉脚本里的引用）",
              file=sys.stderr)
        return 1

    print("tracked-refs: PASS (工具链引用的仓库文件均已跟踪/存在"
          + (f", {len(ignored)} 个测试资产在白名单内)" if ignored else ")"))
    return 0


if __name__ == "__main__":
    sys.exit(main())