#!/bin/sh
# 交叉编译 pymicro-vad / pyspeex-noise 的 aarch64 wheel —— 给板上 HA(8123) 用。
# 背景: RK3576 是 Buildroot(无 g++/apt), HA assist_pipeline 钉死
#   pymicro-vad==1.0.1 + pyspeex-noise==1.0.2, 板上 pip 只能源码编译 → 必败。
#   本脚本在 PC 用交叉工具链产出 cp314/linux_aarch64 wheel(成品在的本目录)。
#
# 用法(仅在 HA 目标 HA 版本/板 python 变更时重跑):
#   1. 拉板上 python 头文件:  adb pull /data/miniforge/include/python3.14 /tmp/ha-x/pyinc
#   2. host pybind11 头:      python3 -m venv /tmp/ha-x/v && /tmp/ha-x/v/bin/pip install pybind11
#   3. pip download <pkg>==<ver> --no-deps --no-binary :all:  取 sdist
#   4. 编译(见下方 g++ 行), 关键点:
#      -march=armv8-a            # 工具链默认架构含 LSE, 板上即 SIGILL(与前端同坑!)
#      -I<板上 python3.14 头>    # 目标 ABI 是板上解释器
#      动态链 libstdc++          # 工具链的 libstdc++.a 静态成员带 LSE, 静态链必炸;
#                                # 板上 /lib/libstdc++.so.6 存在, 直接用
#      零 LSE 验收:              交叉 objdump -d | grep -c ldaddal  == 0
#   5. 手工打 wheel: <ext>.cpython-314-aarch64-linux-gnu.so + 包内 *.py +
#      dist-info(METADATA=PKG-INFO, WHEEL Tag cp314-cp314-linux_aarch64, RECORD)
#      注意 wheel 文件名必须带版本号, 且 sdist 里的 python 包装包(pymicro_vad/
#      pyspeex_noise 目录)必须进 wheel —— 缺了 HA 报 "No module named pymicro_vad"。
#   6. adb push → 板上 /data/ha-env/bin/pip install --force-reinstall --no-deps
#   7. 重启 HA, 验收: 本次启动 "Setup failed" 计数为 0。
# 已出成品(2026-10-17, HA 2026.10.2 / 板 python 3.14.7):
#   pymicro_vad-1.0.1-cp314-cp314-linux_aarch64.whl
#   pyspeex_noise-1.0.2-cp314-cp314-linux_aarch64.whl
set -eu
echo "本文件是配方文档; 步骤见注释, 无自动化逻辑。"
