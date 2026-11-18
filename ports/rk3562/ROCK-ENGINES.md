# RK3562 板内 NPU 引擎（RockASR / RockTTS）+ 内存治理

本板（RK3562 EVB2 DDR4 V10 + RK809 + rkaudio_vqe）默认用瑞芯微官方 NPU 引擎做
识别和合成，SenseVoice / Matcha 作为回退保留在板上。

## 一、为什么要换

| | RockASR / RockTTS（板内 NPU） | SenseVoice / Matcha（板内 CPU） |
| --- | --- | --- |
| 识别延迟 | **1.4 s**（10 s 音频 1.1 s） | 3 s |
| 合成延迟 | **0.43 s**（出 2.8 s 音频） | 1~2 s |
| 说完到首包音频 | **2.6 s** | 3.4~5.3 s |
| 网关 RSS | **41 MB** | 786 MB |
| 识别正确率（websocket 注入 5 条） | 5/5 | 5/5 |

引擎在独立 helper 进程里跑（`core/providers/rock/`），SDK 崩了不影响网关。

## 二、切换方式（配置一行）

`data/.config.yaml` 的 `selected_module`：

```yaml
selected_module:
  ASR: RockASR        # 回退: LocalSenseVoice
  LLM: LocalQwen
  TTS: RockTTS        # 回退: SherpaLocal
```

两套 provider 段可以同时留在配置里，改 `selected_module` 即可切。
仓内的真源是 `ports/rk3562/gateway-board.yaml`（`render-config.py` 生成板端配置）。

## 三、内存治理（2GB 板的关键，不做必黑屏）

三个 NPU 模型同时驻留，**不可回收**的内存约 1700 MB / 1963 MB：

| 组件 | 不可回收 | 说明 |
| --- | --- | --- |
| RockASR helper | 591 MB | 558 MB 是 `/dev/dri/card1` 的 DRM GEM 映射（NPU 模型缓冲） |
| RockTTS helper | 229 MB | 同上 |
| RKLLM | 604 MB | `Private_Dirty` |
| 网关 + 前端 + UI + 系统 | ~280 MB | 网关在 NPU 模式下只要 41 MB |

余量只剩 259 MB，扛不住波动 → 内核 OOM 干掉 `init` → **整机黑屏**（实测三次）。

兜底靠 zram（`ports/rk3562/setup-zram.sh`，开机自启**无条件**跑）：

```
zram swap 768M（zstd）   # 默认 lzo 压缩比≈1.0 白换，zstd 能压到 1/4.4
swappiness 80
oom_score_adj：网关/RKLLM/前端 -500（保护），rock helper +200（先死，自动重拉）
```

要点：

- **算法必须 zstd**：内核 `CONFIG_ZRAM_DEF_COMP_ZSTD` 没开（默认 lzo-rle），
  但 zstd 后端编译进去了 —— 运行时 `/sys/block/zram0/comp_algorithm` 里能看到
  `zstd`，直接写即可。换算法要求设备为空：
  `swapoff` → `echo 1 > /sys/block/zram0/reset` → 改算法 → 改大小 → `swapon`。
- **zram 必须独立于 Home Assistant**：本 profile 原本把 zram 写在
  `start-ha.sh` 里，停 HA 时会一起没（实测 `SwapTotal=0` → 黑屏）。
  HA 现在默认不装，要开就创建 `/data/xiaorui/.ha_enabled`。
- `core/providers/rock/runtime.py` 还有一层自适应仲裁：口径 =
  `MemAvailable + zram 剩余 × 压缩比`，内存宽裕就三个模型共驻，紧张就互相
  让位、zram 没起来时自动退化成纯 RAM 口径。

实测稳态：`MemAvailable` 250~280 MB，zram 已用 190~285 MB。

## 四、部署 Rock 引擎

SDK 二进制和模型不入 git（~650 MB），本机先解压好：

```sh
export XR_ROCK_ASR_SDK=/path/to/rockasr_android_linux_rk356x_*
export XR_ROCK_TTS_SDK=/path/to/Rockchip_RockTTSV1_SDK_Release_*
export XR_ROCK_LIC_DIR=/path/to/licenses      # key_asr.lic + key_tts.lic
bash ports/rk3562/deploy-board-gateway.sh     # 会自动调 deploy-rock.sh 并切配置
# 或者单独装 + 自检:
bash ports/rk3562/deploy-rock.sh "$SERIAL"
```

授权是**设备绑定**的：板子 `rkdevice_info` → `device.inf` → PC 侧
`rkauth_tool_bin -d device.inf -m asr|tts` 生成 license，再推到板上。

板端布局：

```
/data/xiaorui-server/rockasr/{lib,data,key_asr.lic,rockx_auth_config.json}
/data/xiaorui-server/rocktts/{lib,data,key_tts.lic,rockx_auth_config.json}
```

**TTS 模型必须展平**：`data/models/rk356x/*.data` 要拷到 `data/models/`，
否则 rockx2 子图 init 静默失败（外层 `RockXInit` 仍返回 0），运行期空指针
崩溃（官方 demo 同样崩）。

## 五、踩坑

- **PR_SET_PDEATHSIG 不能用**：网关从守护线程 fork helper，那个线程 fork 完
  就退出，内核在**线程**退出时立刻发 SIGKILL，把刚握手成功的 helper 杀掉
  （日志显示"就绪"后进程秒退）。改成 `getppid()` 轮询。
- **SDK 的 C 层日志写 fd1**，会污染帧协议（`帧头长度非法: 1226863211` 就是
  把日志字节当帧头读了）。helper 启动时把协议 stdout `dup` 走，fd1 改指 stderr。
- **不能 pkill 掉 NPU 进程再重拉**：实测双起（两个 RKLLM + 两个网关 = 1.2 GB）
  直接 OOM 黑屏。init 脚本的 start 已加幂等守卫。
- **`pkill -f` 的模式串要错开自身**：`adb shell '... pkill -f "server/app.py" ...'`
  会把承载它的远端 shell 一起杀掉（`set -e` 的脚本直接中断）。用
  `server/[a]pp.py` 这种字符类。
- **profile 别推错**：本板是 `ports/rk3562/`（RK809 + PDM + VQE），不是
  `ports/rk3562-es8388/`（ES8388）。AEC 依赖 RK809 播放回采，全靠
  `ports/rk3562/init-S98xiaorui` 里的 mixer 设置（Playback Path=SPK /
  Capture MIC Path=Main Mic / ADC Capture Volume=255）—— 推错 profile 的
  init 脚本等于把 AEC 和喇叭一起弄坏。
