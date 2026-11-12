# RK3562 EVB2：从公开代码准备部署

## 边界与已测基线

目标硬件是 **RK3562 EVB2 DDR4 V10 / 2GB + 外接 6mic PDM 阵列 + RK809**，
不是任意 RK3562 板。先合法取得 `RK3562_LINUX6.1_SDK_RELEASE_V1.3.0`、
工具链和 VQE 授权资源；仓库不分发厂商 SDK、私有库或镜像。
接口与通道连接以本目录 DTS 开头的硬件说明为准。

已测组合：Linux 6.1.141、RKNPU 0.9.8、Python 3.14.7、Miniforge 26.7.2-0、
RKLLM Toolkit 1.3.1、sherpa-onnx Python/C API 1.13.8。
`assets.lock.json` 给出 37 个资源文件的大小、SHA256 和来源，
`requirements-board.lock` 给出实机网关环境的精确包版本。
这些是版本基线，不是“源码克隆后自动拥有全部资源”的承诺。

## 1. 内核与设备树

在自己的 SDK 主机操作，不需要访问作者的构建服务器：

```bash
export XR_REPO=/absolute/path/to/xiaorui
export XR_SDK=/absolute/path/to/RK3562_LINUX6.1_SDK_RELEASE_V1.3.0
git -C "$XR_SDK/kernel-6.1" apply --check \
  "$XR_REPO/ports/rk3562/kernel/rk817-loopback-0db.patch"
git -C "$XR_SDK/kernel-6.1" apply \
  "$XR_REPO/ports/rk3562/kernel/rk817-loopback-0db.patch"
cp "$XR_REPO/ports/rk3562/rk3562-evb2-ddr4-v10-linux-pdm-mic-array.dts" \
  "$XR_SDK/kernel-6.1/arch/arm64/boot/dts/rockchip/"
```

补丁的对照内核基线是 `5766ccb7dcd12f589f4b40ea94c1d8ada6c91f74`。
只包含回采 0dB 修复，不包含调试打印；不向内核上游推送。
`--check` 失败时停止，不能通过强行覆盖驱动来“解决”版本差异。
若已经应用，先用 `git apply --reverse --check` 确认，不要重复应用。

在 `arch/arm64/boot/dts/rockchip/Makefile` 添加一次：

```make
dtb-$(CONFIG_ARCH_ROCKCHIP) += rk3562-evb2-ddr4-v10-linux-pdm-mic-array.dtb
```

在 SDK 的 `device/rockchip/.chips/rk3562/rockchip_rk3562_evb2_ddr4_v10_defconfig`
将 `RK_KERNEL_DTS_NAME` 设为 `rk3562-evb2-ddr4-v10-linux-pdm-mic-array`，
使用 SDK 原有配置选择流程选中该 defconfig，然后执行：

```bash
cd "$XR_SDK"
LC_ALL=C.UTF-8 LANG=C.UTF-8 ./build.sh kernel-6.1
```

产物是 SDK 的 `kernel/boot.img`。先保存原 boot 镜像，并按该板 SDK 的刷机流程刷入。
**不要照抄其他板子的固定块设备号或直接执行未经核对的 dd。**
重启后确认内核版本、PDM 8ch 16k 和 RK809 声卡符合预期，再部署用户态。
采集中回采音量应为 0dB；停止采集时寄存器恢复默认值并不能说明修复失败。

## 2. 模型和库

沿用仓内已经验证的下载入口，不替换成同名但字节不同的模型：

```bash
cd "$XR_REPO"
bash tools/download-models.sh kws-enc kws-dec kws-join kws-tokens matcha rkllm-rt
mkdir -p tts-matcha board-server
ln -s "$XR_REPO/models/matcha-icefall-zh-baker" tts-matcha/matcha-icefall-zh-baker
cp models/librkllmrt.so board-server/librkllmrt.so
```

上述链接创建仅用于新克隆；已有目录时不要覆盖。
KWS 必须使用脚本中的 ModelScope `pkufool` 文件，不能换成同名 GitHub 模型。
SenseVoice 按清单中的官方 2024-07-17 包解压，将 `model.int8.onnx`、`tokens.txt`
放到 `models/sherpa-onnx-sense-voice-zh/`。
Miniforge 使用清单指定发布版本的 Linux-aarch64 安装器，保存为
`board-server/miniforge.sh`。

Silero ONNX 放到
`models/snakers4_silero-vad/src/silero_vad/data/silero_vad.onnx`；
以清单 SHA256 为准。原始 Silero revision 未留档，不能声称任意最新版与基线等价。
若上游已无法取得相同文件，需要重新选择并验证版本，不能静默跳过校验。

Qwen 使用 `Qwen/Qwen2.5-0.5B-Instruct`，在安装了 RKLLM Toolkit 1.3.1 的
独立 PC Python 环境中执行：

```bash
python ports/rk3562/convert_rk3562.py /path/to/Qwen2.5-0.5B-Instruct \
  models/qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm
```

只使用本板支持的 w8a8，不能照搬 RK3576 的 w4a16。
原始 HF revision 未留档，因此只对已测转换产物固定了 SHA256；不同工具链、
模型 revision 重新转换的产物不保证逐字节一致。版本变化必须单独做板端验收。

VQE 包通过 `XR_VQE_PKG` 指定，配套库通过 `XR_VQE_LIB_DIR` 指定。
清单记录了已测插件、6mic_2ref 配置、波束权重及库的指纹，但不授予其分发或商用许可。
Matcha Baker 数据也有非商用限制。不要把这些二进制补进 Git。

## 3. 前端构建

前端使用 ARMv8-A 的 aarch64 Buildroot 工具链，已测 GCC 9.3.0。
准备同工具链构建的静态 Opus，设置 `XR_OPUS_ROOT`，其中应有
`include/opus/` 和 `lib/libopus.a`；板上另需动态 `libopus.so.0`、`libasound.so.2`。
工具链本身、Opus 和系统库属于 BSP/构建前置依赖，不由部署脚本安装。

```bash
export XR_TOOLCHAIN_ROOT=/path/to/aarch64-buildroot-toolchain
export XR_OPUS_ROOT=/path/to/opus-aarch64
./build-board-live.sh
bash tools/check-no-lse.sh
```

CPU 版 sherpa C API 使用上游 commit
`0bdca37ac0695a873bcb553f6b60f6d744d2bafd`，不要使用 RK3576 RKNN 版库替代。
取得对应源码后，用本目录公开的工具链文件构建：

```bash
cmake -S /path/to/sherpa-onnx -B /path/to/sherpa-build \
  -DCMAKE_TOOLCHAIN_FILE="$XR_REPO/ports/rk3562/aarch64-buildroot.cmake" \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
  -DSHERPA_ONNX_ENABLE_C_API=ON -DSHERPA_ONNX_ENABLE_TTS=ON \
  -DSHERPA_ONNX_ENABLE_RKNN=OFF -DSHERPA_ONNX_ENABLE_BINARY=OFF \
  -DSHERPA_ONNX_ENABLE_PYTHON=OFF -DSHERPA_ONNX_ENABLE_PORTAUDIO=OFF \
  -DSHERPA_ONNX_ENABLE_TESTS=OFF -DSHERPA_ONNX_ENABLE_WEBSOCKET=OFF
cmake --build /path/to/sherpa-build --target sherpa-onnx-c-api -j4
```

把 `lib/libsherpa-onnx-c-api.so` 和
`_deps/onnxruntime-src/lib/libonnxruntime.so` 放在同一目录，设置 `XR_SHERPA_LIB_DIR`。
清单中的库 SHA256 是实机已测构建的指纹；不同构建目录和工具链可能产生不同文件，
不能仅凭相同版本号宣称逐字节复现，需记录自己的构建指纹并做运行验证。

## 4. 离线依赖和配置

PC 使用 Python 3.11+，安装 PyYAML、pytest，以及现有测试所需 loguru、opuslib_next、portalocker。
不要使用 `board-server/pkg/requirements.txt` 为本板装全套 torch/FunASR：
那是上游 Python 3.10 通用依赖，不是本板 Python 3.14 的锁。

```bash
python3 ports/rk3562/prepare-wheels.py --directory board-server/wheels314 --download
python3 ports/rk3562/prepare-wheels.py --directory board-server/wheels314
python3 ports/rk3562/check-assets.py
python3 ports/rk3562/render-config.py --output-dir /tmp/xiaorui-package-new
```

`--download` 只补齐缺失的精确版本；验证时排除错误架构和 Python ABI 的 wheel，
生成带 SHA256 的安装锁。无完整 wheel 集时部署会在首次写板之前停止。
锁来自实机已安装环境，使用 `--no-deps`；未选中的第三方 provider 的额外依赖
不在离线运行承诺内，不能把整个上游 requirements 当成已验证组合。

配置包同时提供 `server/config.yaml` 和 `data/.config.yaml`，
补齐 VAD、Memory、Intent、hello、退出词和鉴权字段；唤醒词只取仓内真源。
不会从本机私有 config.yaml、data/ 或 .env 继承凭据。
外网工具默认不启用；天气实时数据和真实 HA 硬件接入不属于当前复现范围。

## 5. 部署与验收

资源校验通过后，按 [DEPLOYMENT.md](DEPLOYMENT.md) 顺序执行 VQE、网关、前端和时间部署。
HA 是可选设备控制面，使用精确版本锁，显式提供密码，首次安装仍需要板端网络。
不要为了测试而清空正在运行设备的 `/data`。

先做协议握手、`/health/ready`、HA API 检查，再用人声或独立声源测试
“小瑞小瑞，你叫什么名字”，确认识别、语音返回和板载喇叭实际播完。
只看端口或单测全绿不算完整硬件复现；完整 SDK 构建与新刷板验收需在独立板上执行。

当前已验证：公开模板独立生成、隔离配置加载、离线依赖检查及已有板的轻量/声学链路。
尚不能声称已经完成独立新刷板的完整复现；原始模型 revision 缺档及厂商资源获取
是明确边界，不以复制私有配置或备份环境来掩盖。
