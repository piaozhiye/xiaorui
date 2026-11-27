# RK3562 AIOT DEMO LP4X V10 + ES8388：小瑞板内离线移植

适用板：`Rockchip RK3562 AIOT DEMO LP4X V10 Board`

系统：Buildroot 2026.02.3，Linux 6.12.81，4GB RAM，rootfs 5.9GB

音频：ES8388，板载模拟麦克风 + 板载扬声器
ADB serial（本轮验证）：`<SERIAL>`

## 当前状态

- RKLLM/Qwen2.5-0.5B w8a8 已在本板 RK3562 NPU 上加载和推理：
  - runtime 1.3.1，NPU driver 0.9.8，`target_platform: RK3562`
  - Python 3.14.7，glibc 2.43
  - NPU render node 为 `/dev/dri/renderD129`；当前 NPU 1GHz
- 板内网关、SenseVoice ASR、Matcha TTS 与前端已部署。
- 前端主要采集路径使用已存在的 ES8388 专用 FIR 路径：

  ```text
  hw:rockchipes8388,0 (48kHz/2ch)
      → XR_MIC_MODE=es8388, 选 mic ch0
      → 72-tap FIR anti-alias ÷3
      → 16kHz mono → KWS/VAD → 板内网关
  ```

- ES8388 profile 使用独立 `keywords.txt`，包括双单元“小瑞小瑞”、单单元“小瑞”兜底和“你好小瑞”。PC 喇叭旁的板载麦实录经 SenseVoice 识别成“**小瑞现在几点了？**”，说明近讲/扬声器链路可能只保留一个唤醒单元；旧词表只有双单元时 KWS 会漏掉这种情况。
- 用本板 sherpa-onnx 1.13.8、同一套 KWS 模型/token，对本机离线正负语料回归，KWS 参数与前端一致（score=1.25、threshold=0.15）：
  - 原双单元词表：83/100 正样本命中，0/120 负样本命中。
  - ES8388 单/双单元词表：100/100 正样本命中，2/120 负样本产生单词级 KWS 候选。
  - 这两条负例经 SenseVoice 转写为“你叫小瑞吗？”和“你叫小绿吗？”。前端 KWS 后的文本门要求有至少 2 个非标点指令字符，因此问句尾巴“吗？”不能单独通过；对应回归在 `board/xr_wake_test.c`。
- 对 ES8388 48k 原始采集用高质量 soxr 降到 16k 后，板端 sherpa-onnx Python API 与同配置 C API 探针均在 mic0 上命中“**小瑞**”（mic1 未命中）；与 SenseVoice 转写相符。前端 KWS 初始化已验证；**端到端冷唤醒的前端 `KWS 命中` 日志仍需在无 TTS 播报/跟说窗时完成最后一次确认**，不能把跟说窗口内的问答记作冷唤醒通过。
- 播放路径：

  ```text
  板内 Matcha 24kHz/mono → xr_spk (ALSA plug → ES8388 48kHz/2ch) → 板载喇叭
  ```

- 用户反馈扬声器音量偏大。Output 1/2 从 `27/33`（−4.5dB）逐档下调：21（−13.5dB）仍偏大 → 19（−16.5dB）仍偏大 →
  现为 **`15/33`（−21dB，相对原档共降 16.5dB）**；每档 1.5dB，`mixer-es8388.sh` 会在开机时固定该值。
  往回调就是每档 +1（17 → −18dB、19 → −16.5dB、21 → −13.5dB），实时改可用
  `amixer -c 0 cset name="Output 1 Playback Volume" <档位>`（Output 2 同样）。
- PC 播放声源、板载麦位于 PC 喇叭附近时，ASR「现在几点了」→ 板内 Qwen 回答 → TTS 的端到端已多轮跑通。**声学冷唤醒也已确认**（前端日志 `[xr] KWS 命中: 小瑞`，网关转写「小瑞小瑞。」）；但仍要把"跟说窗内成功"与"冷唤醒"分开记 —— 只有等 30s 窗口过期后的命中才算冷唤醒。
- 当前前端和两个板内服务进程应各一个。启动后可用下文命令核验。
- **重启恢复已验证**（2026-11-17）：`reboot` 后 buildroot 的 rcS 以 `start` 调用 `/etc/init.d/S98xiaorui`，
  自动拉起板内栈与前端；8001/8004/8112 全部就绪，mixer 15/15 与 PGA=8 保持，CPU `performance`，
  DDR 时钟仍 1332MHz，重启后冷唤醒仍能命中。

## 与旧 RK3562 RK809/PDM 方案的区别

本板不接旧板的 PDM 阵列、RK809 loopback ref 或 `rkaudio_vqe`：

| 项 | 本板 ES8388 | 旧 RK809/PDM 板 |
|---|---|---|
| 麦克风 | ES8388 板载模拟麦 | PDM 6mic + RK809 2ref |
| 采集 | `XR_MIC_MODE=es8388`，48k/2ch → 前端 FIR ÷3 | `xr_vqe_16k`，VQE 插件 |
| AEC/波束 | 无 | VQE AEC/BF/ANR |
| ALSA mixer 寻址 | **只认数字卡号**，card0 | 曾需要按 RK809 卡名 |
| SAI loopback | 禁用 | 旧 VQE 4ch 方案专用 |

不要在本板运行 `deploy-vqe.sh`，也不要把旧版 `init-S98xiaorui`、`XR_MIC_PERIOD` 或 RK809 mixer 设置推到本板。

## 硬件/ALSA 实测要点

- `/proc/asound/cards`：card0 id 为 `rockchipes8388`，短描述 `rockchip-es8388`。
- `/proc/asound/pcm` 显示 `dailink-multicodecs ES8323 HiFi-0`；这是 PCM 的描述名，不要据此误判 codec。
- `amixer -c rockchipes8388` 会报 `Invalid card number`；本板 mixer 设置必须用数字 card0。脚本从 `/proc/asound/cards` 解析卡号，不能假设其他声卡枚举顺序。
- ES8388 采集硬件实测固定 48kHz，1/2 声道；请求 16kHz 会被硬件顶回 48kHz。前端 16kHz 采集必须走 `XR_MIC_MODE=es8388` 内置 FIR 抽取。
- 直采 PGA：
  - 档位 3（+9dB）：真人语音段约 −50.2dBFS rms、−38.6dBFS peak。
  - 档位 8（+24dB）：约 −42.6dBFS rms、−18.3dBFS peak，削波 0%。
  - 底噪约 −58dBFS。当前固定档位 8。
- 播放实测：板端直放 48kHz/2ch PCM 可用，`Speaker` 与 `spk switch` 必须开启。

## ABI 与 NPU 运行时

板端 Buildroot rootfs 没有 `ldd`、`getconf` 或系统 Python；实测 `/lib/libc.so.6` 为 glibc 2.43。

本次离线使用旧板备份中的 aarch64 Miniforge Python 3.14（1.1GB，排除 package cache 后推入），运行 `Python 3.14.7` 正常。RKLLM：

- `librkllmrt.so` 与仓内官方 runtime SHA 相同。
- runtime 依赖 `libgomp.so.1`，本板 `/lib` 没有此库；将 aarch64 `libgomp.so.1` 放 `/data/xiaorui-server/lib/`，启动时加入 `LD_LIBRARY_PATH`。
- 仓内 `qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm` 在本板成功加载并返回 SSE 内容；不要只凭 renderD129 存在就宣称 NPU 可用。
- 当前 `/sys/devices/platform/ff300000.npu/devfreq/ff300000.npu/` 支持 `performance`，最高 1GHz。

## 部署

### 前置资源

在本机准备以下资源：

- `board-server/wheels314/`、`board-server/miniforge.sh`、`board-server/librkllmrt.so`
- Qwen2.5-0.5B RK3562 w8a8 模型、SenseVoice、Matcha、Silero VAD
- 固定 SHA 的 sherpa-onnx aarch64 CPU C API + ONNX Runtime（用 `XR_SHERPA_BUILD` 指向固定 SHA 的 aarch64 build，或用 `XR_SHERPA_LIB_DIR` 直接给两个 .so 的 bundle）：`libsherpa-onnx-c-api.so`、`libonnxruntime.so`。部署前由 `ports/rk3562/check-assets.py --groups frontend` 校验；ES8388 前端 KWS 使用 CPU，不要换成 RK3576/RKNN 变体。
- aarch64 `libgomp.so.1`（必须通过 `XR_LIBGOMP` 指向 aarch64 的 `libgomp.so.1`）。
- Silero VAD ONNX（默认 `models/snakers4_silero-vad/src/silero_vad/data/silero_vad.onnx`；该模型不随 Git 分发。可从本机备份复制，或用 `XR_SILERO_VAD_MODEL` 指向已授权的本地文件）。

若本机目录不同，可显式指定：

```bash
export XR_SHERPA_BUILD=/path/to/sherpa-onnx/build-aarch64-capi
export XR_LIBGOMP=/path/to/aarch64/libgomp.so.1
export XR_SILERO_VAD_MODEL=/path/to/silero_vad.onnx
# 或者直接提供包含两个 .so 的固定 SHA bundle:
export XR_SHERPA_LIB_DIR=/path/to/sherpa-aarch64-cpu-libs
```
- `board/xiaorui_live` 与 KWS 模型

**端侧运行无需外网/API key**。资源不足时部署应停止，不能悄悄切云。

### 首次部署

```bash
cd /path/to/xiaorui-edge-client

# 验证板上 ABI、Python、RKLLM runtime 和真实 NPU 推理
bash ports/rk3562-es8388/check-runtime.sh <SERIAL>

# 全量部署（备份保存在 /tmp/opencode/rk3562-es8388/backup-<timestamp>/）
bash ports/rk3562-es8388/deploy.sh <SERIAL>
```

部署脚本会拒绝非 RK3562/无 ES8388 声卡的设备；如果板上已有 Xiaorui/网关/ALSA 配置但没有 `.rk3562-es8388-profile` 标记，也会拒绝覆盖。它复用 `deploy-board-gateway.sh` 部署板内栈，但设置 `XR_SKIP_INIT_INSTALL=1`，以免安装旧 RK809 启动脚本。

写板前还有一道存储门：`deploy.sh` 会用 `du -sk` 统计随包资源，首装另预留 Miniforge 解包与 Python venv 膨胀（合计约 2.4GB），再和板端 `df -Pk /data` 的可用空间比较；空间不足或读不到可用空间时直接退出，不写任何板端文件。

### 日常控制

```bash
adb -s <SERIAL> shell 'bash /etc/init.d/S98xiaorui start'
adb -s <SERIAL> shell 'bash /etc/init.d/S98xiaorui stop'

# 进程与频率
adb -s <SERIAL> shell 'ps aux | grep -E "[x]iaorui_live|[l]lm_rkllm|[s]erver/app.py"'
adb -s <SERIAL> shell 'cat /sys/devices/platform/ff300000.npu/devfreq/ff300000.npu/{cur_freq,governor}'
```

### 回滚

部署日志最后会打印本次备份目录：

```bash
bash ports/rk3562-es8388/rollback.sh <SERIAL> \
  /tmp/opencode/rk3562-es8388/backup-<timestamp>
```

先看回滚会动什么（只校验并打印计划，不停服务、不改板）：

```bash
bash ports/rk3562-es8388/rollback.sh <SERIAL> \
  /tmp/opencode/rk3562-es8388/backup-<timestamp> --dry-run
```

回滚按 manifest 恢复配置与文件，只停止本 profile 的进程。默认保留大模型、Python runtime、日志等目录；如果确认部署前不存在的受管目录只包含本 profile 资产，可追加 `--remove-created-data` 显式清理。对已安装 profile 的更新会备份实际被覆盖的前端文件；共享网关/模型目录在重复部署时不重写。

回滚还会还原 **mixer 现状**：本 profile 会把麦 PGA 设成 8、把喇叭 Output 1/2 调到 15，部署时会把改动前的控件值存进
`backup-<timestamp>/mixer-state.txt`，并在**第一次**部署时另存一份到板端 `/data/xiaorui/.rk3562-es8388-mixer-original`
（后续重部署不再覆盖它），回滚时优先用这份"装本 profile 之前"的值。回滚前若前端在跑，回滚结束会重新拉起板内栈与前端
（避免板子从"能说话"变成"完全静默"）。

注意：本板这份初始快照是在 profile 已经生效之后才抓的（里面是 21/8），因为抓取功能是在调完音量之后才补上的；
用户口述的原始音量是 `27/33`，若要回到那一份，需要手工 `amixer -c 0 cset name="Output 1 Playback Volume" 27`
（Output 2 同样处理）。麦 PGA 在本 profile 之前没有实测记录，代码里不替用户猜。

**回滚不能还原的东西**：`/data/xiaorui-gateway/server/`、`/data/xiaorui-server/` 下的模型与代码、`/data/gwvenv`
等共享目录里的内容不在 manifest 里（它们是共享资源，可能已被后续使用修改）。这些目录只能整体保留，或在确认只含本
profile 资产时用 `--remove-created-data` 删除，不能按文件精确还原。

部署会重置 `/data/xiaorui-server/xiaorui_volume` 为 100（软件音量），这是本 profile 有意覆盖的项，回滚时会还原旧值。

## 测试

```bash
python3 -m pytest tests/test_rk3562_es8388_profile.py tests/test_rk3562_deployment.py -q
bash test-voice.sh
```

端到端验收步骤：

1. **播放源**：PC 喇叭播放 `tests/audio/wake2_time.wav`（22050Hz mono），送到 PC 喇叭的设备 PCM 必须为 48kHz/2ch。
2. **拾音端**：板载 mic 放到 PC 喇叭附近，且不得与板载 TTS 播放重叠。
3. **判定**：日志应有 KWS 冷唤醒（非仅跟说窗口）、ASR「现在几点了」、本地 Qwen 回答和 TTS 完成；人耳确认板载喇叭确实出声。

不要仅凭 `aplay` 的退出码或"Playing WAVE"打印认定人耳听到了。板载喇叭音量当前比原档低 16.5dB。

端口探测统一使用 `board-server/probe-port.py`（推板后为 `/data/xiaorui-gateway/probe-port.py`）：PC 侧内联
`python -c` 会被 adb shell 的多层引号吃掉，板端 busybox 也没有可用的 `ss`。

## 已知限制 / 未完成验证

- **VQE 采集链**：可选的 rkaudio 配置使用 2 路麦克风 + SAI1 SDI1 上的 2 路播放参考，AEC、BF、ANR 均启用。AEC 纯音对照显示约 2–3s 收敛；真人近端语音质量仍需单独声学验收，详见下文。
- **KWS 冷唤醒已打通，但对拾音位置敏感**（2026-11-17 实测）：板载麦放在笔记本喇叭旁约 5–10cm 时，
  PC 播放 `tests/audio/wake2_time.wav`（转 48k/2ch）后前端打出 `[xr] KWS 命中: 小瑞`，
  随后本地 ASR → 本地 Qwen → 本地 TTS 全链走通（TTS 播出「没听清，请再说一遍」和「现在几点了」的本地回答）。
  重启后复测同样能命中。命中率的瓶颈在拾音：同一条件下连播两遍测 6 轮，命中 3 轮；
  麦稍微远一点，唤醒词就会被 ASR 听成「嗯」并被服务端的唤醒词门拒掉（前端日志 `服务端静默回执(噪声)`，无 TTS）。
  因此验收时必须把麦贴近喇叭，且**不要把处于 30 秒免唤醒窗中的问答记作冷唤醒通过**。
- **本地小模型不认表（已知特性，本方案接受）**：被问「现在几点了」时，板内 Qwen2.5-0.5B 是**猜**的
  （回答「早上八点五十分」，而板子时钟是 08:59）——小模型没有时钟概念，它并不知道板子的真实时间。
  用户确认这不影响使用，故不改；若将来要它准，办法是在系统提示里注入板端时间或接一个时间工具。
- **DDR 定频**：本板内核声明了 DMC 节点（`rockchip,rk3562-dmc`，`status=okay`）但驱动没有绑定，因此**没有 DMC
  devfreq 节点**——用户态和内核都没有任何可以改 DDR 频率的接口，DDR 就是以引导时固化的频率在跑，实测 DDR 时钟
  (`scmi_clk_ddr`) 恒为 **1332MHz**。所以"没有 DMC 节点"在这块板上的准确含义是"频率固定"，而不是"降频不受控"；
  启动脚本因此既不写也不能写 DDR 频率。
- **VQE/AEC/波束成形**：本 profile 的 VQE overlay 按 2 mic + 2 ref 配置；基础直采 profile 不加载 VQE。旧 RK809/PDM 板的采集路径仍独立保留。
- **RTC/时区**：实测已有 RTC，`Asia/Shanghai`，启动时间脚本成功从 RTC 校时。

## ES8388 SAI1 硬件回采与 VQE（2 mic + 2 ref）

4G 板使用 SAI1 的硬件回采：SDI0 保留 ES8388 两路麦克风，SDI1 选择 SDO0 并打开回采，形成两路播放参考。
当前链路**不使用** `rockchip,digital-loopback` 软件 DLP、`SAI1 Software Digital Loopback Mode`（numid=62），
也不启用 SDI0 loopback。混音控件必须在采集 PCM 打开前配置，`mixer-es8388.sh` 以控件名称设置并校验功放状态。

VQE overlay 以 4ch/48kHz 采集输入（2 mic + 2 ref），输出 mono 48kHz，经 `samplerate_best` 降到前端需要的
16kHz。AEC、BF、ANR 开启；VQE 自带的 wakeup/unkws/command/DOA 关闭，由 Sherpa KWS 负责唤醒。播放仍走 `xr_spk`。

部署需要 Rockchip 插件包与 AArch64 运行库（这些第三方文件不随仓库分发）：

```bash
XR_VQE_PKG=/path/to/rkaudio_vqe_plugin-20261110 \
XR_VQE_LIB_DIR=/path/to/Linux_8.3.0_aarch64 \
  bash ports/rk3562-es8388/deploy-vqe.sh <SERIAL>
```

部署脚本先确认型号、声卡与 SAI1 控件，再备份覆盖文件；停止前端后配置路由并做 2 秒 ALSA smoke capture，
最后检查本次启动的新日志是否打开 `xr_vqe_16k`。基础 profile 未安装时只安装并 smoke-test overlay，不启动完整语音栈。

**验证边界**：2026-11-26 的低电平 1kHz AEC 开/关对照显示，AEC 开启时约 2.5–3 秒后测试音显著衰减，关闭时没有相同衰减；
这证明受控纯音下回采与 AEC 有效，不代表近端真人说话质量已经验收。自适应滤波器约需 2–3 秒收敛，短提示音开头仍可能残留回声。

## 与共享源码的最小改动

- `ports/rk3562/deploy-board-gateway.sh` 增加 `XR_SKIP_INIT_INSTALL=1`，默认 RK809 部署行为不变。
- `ports/rk3562/start-board-stack.sh` 将 `/data/xiaorui-server/lib` 加入 `LD_LIBRARY_PATH`，并用 Python socket 探测服务端口（bash `/dev/tcp` 在板端 `setsid` 子会话中不可靠）。
- `board/xr_mic4.c`/`board/xiaorui_live.c` 将 ES8388 直采通道数参数从仅 4ch 扩展为 2ch 板载 codec + 4ch 旧 SAI loopback，分别仍走 FIR ÷3；默认模式和旧 4ch 路径不变。
