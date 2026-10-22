# xiaorui — 小瑞语音助手（RK3576 云端与离线双模）

中文 | [English](README_EN.md)

小瑞是跑在 RK3576 开发板上的中文语音助手：**单进程 C 前端**负责唤醒/断句/识别/播放，
**板内双网关**分别提供"云端快"与"断网可用"两条链路，**Home Assistant** 接米家设备语音控制。
唤醒、识别、对话、合成在板上闭环——拔掉外网，对话/报时/定时/闹钟/音量全部照常
（本地网关 RKLLM 承接）；联网时走云端大模型，云端失联自动回落，双向无感。

```
板上电即全栈自启（/etc/init.d/S99xrvoice → start-all.sh）。可用的唤醒示例：
    "小瑞小瑞，现在几点了"              ← 报时纯本地，0 依赖外网
    "小瑞小瑞，帮我关灯"                ← HA 意图零 LLM 直控设备（真翻转）
    "喂，小瑞小瑞…" / "谢谢小瑞小瑞…"    ← 引导词白名单（为/喂同音等价）
    "他说小度小度"                      ← 反例：冷态静默不搭话
```

## 功能一览（均为实机声学验收过的状态）

| 功能 | 说明 |
| --- | --- |
| 声学唤醒 | sherpa-onnx KeywordSpotter 3.3M 常驻喂帧；服务端另有形状唤醒门（小X小Y + 引导词白名单）双保险 |
| 采集增强 | ES8388 4ch（活麦+空位+2 路数字回采参考）→ VQE 插件 aec/anr/agc/bf 四开，1kHz 回声全链抑制 52dB |
| 打断（barge-in） | 回答/音乐/电台播放中说话即可打断，控制线程三路覆盖 |
| 对话 | 双网关混合路由：云 qwen-flash + 云 ASR ⇄ 本地 RKLLM Qwen2.5-1.5B，按轮自动切换 |
| 噪声免疫 | 服务端判噪声 → silent 收轮，前端静默收轮**不白送跟说窗**；单字气口门；短文本门不进 LLM |
| 在线音乐/电台 | MCP 流式音乐 + SomaFM 电台；播放中语音控制音量/下一首/换台/停止；播放中问答**自动让位、答完续播**，纯唤醒只忽略不打断 |
| 定时提醒/闹钟 | 中文点式表达 + 云 ASR 变体收编；到点 announce 通道主动播报；sqlite 全局化抗断连 |
| 报时/再说一遍 | 完全离线，东八区口语形秒答 |
| 本地声纹 | campplus 192 维：注册/"我是谁"/LLM 个性化注入 |
| Home Assistant | 米家灯/插座语音控制零 LLM 直调 HA REST（主设备白名单防群发误伤）；板上 8123 |
| 看门狗自愈 | 主循环 120s 停摆 → 自动 reboot → 开机链无人值守恢复 |

## 支持平台

| 平台 | 模式 | NPU | 状态 |
|---|---|---|---|
| RK3576 | 双模（云 + 端侧全离线） | 有（ASR 精修 + 本地 LLM） | **已验证**（本仓主述） |
| RK3308 | 云端音频模式 | 无 | 云端单模端到端**已跑通**（2026-10-21 板测，网关在板外） |

共享核心不按 SoC 分叉，平台差异收在 `ports/<soc>/`；`XR_BACKEND` 选择服务端路由，
`XR_LOCAL_ASR=off|rknn` 独立选择本地识别能力。默认保持 RK3576 现有行为。

---

## 1. 系统框图

### 1.1 整体拓扑

```
                 RK3576 开发板 (Buildroot 2024.02, 内核 6.1.99 #69)
┌───────────────────────────────────────────────────────────────────────┐
│                                                                       │
│  ┌─────────────────────┐  WebSocket(小智v1协议)   ┌────────────────┐  │
│  │  xiaorui_live (C)   │ ──────── 8001 ────────▶ │  云端网关       │  │
│  │  单进程前端          │ ◀──── detect/text ───── │  qwen-flash    │  │
│  │  KWS+VAD+ASR+播放    │                        │  云ASR + matcha │  │
│  │                     │ ──────── 8002 ────────▶ ├────────────────┤  │
│  │  XR_BACKEND=hybrid  │ ◀───── 本地全离线 ────── │  本地网关       │  │
│  └────────┬────────────┘                        │  RKLLM@8112     │  │
│           │                                      │  paraformer    │  │
│  ALSA     │ xr_vqe_16k / xr_spk                 │  matcha+意图族  │  │
│  ┌────────▼────────────┐                        └───────┬────────┘  │
│  │ ES8388 (named card)│                                │ REST      │
│  │ 4ch=麦+空+refL+refR │                        ┌───────▼────────┐  │
│  │ SAI1 SDI1 回环参考   │                        │ Home Assistant │──┼──▶ 小米云
│  └─────────────────────┘                        │  8123 + 米家    │  │
│                                                 └────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
   以太网                            crond */5 看护双网关         USB adb
```

hybrid 模式按轮切换：云端网关不可用或未应答 → 本地网关无缝续答；本地网关周期探测云端恢复。
**两侧网关均判定为噪声 → silent 回执，前端静默收轮不搭话**。

### 1.2 前端内部（board/，39 个 .c）

```
 ALSA 采集 16k/1ch (xr_vqe_16k, period 1600 EXACT)
     │
     ▼
 ┌──────────┐ 命中    ┌───────────┐ 说完     ┌───────────────┐
 │ KWS 3.3M  │───────▶ │ 双门限VAD  │────────▶ │ 20s RKNN ASR   │ 0.56s 精修
 │ thr=0.10  │ 常驻喂帧 │ 跟说窗续窗 │         │ paraformer     │ (音频轮上云转写)
 └──────────┘         └───────────┘         └───────┬───────┘
                                                    │ 文本/音频轮
     ┌──────────────── 会话状态机 voice_session.c ─────────────────┐
     │ LISTEN⇄RESPOND；跟说窗 30s——只有真出声的回答才续窗，         │
     │ 噪声/零音频轮静默收轮不白送窗；回声护栏；20s 绝对兜底         │
     └────────────────────────────┬────────────────────────────────┘
                                  ▼
                     backend_hybrid ─┬─ backend_cloud (8001)
                                      └─ backend_local  (8002)
                                  │
                    TTS opus 24k ─▶ xr_spk 播放线程（音乐期 duck/三路打断）
```

### 1.3 音频采集链（AEC 关键）

```
 ES8388 ──左麦 PGA+24dB(numid52=8)───────────────┐ ch0 活麦
         └右麦位预留(未接)──────────────────────┼ ch1 静
 SAI1 回环(numid22=1, 源=SDO0 numid18=0)─────────┼ ch2 refL ┐ 数字参考
                                                  └ ch3 refR ┘ (播放侧回采)
     4ch@48k  hw:rockchipes8388,0
        │
        ▼  rkaudio_vqe ALSA 插件（瑞芯微 VQE，修复版）
   aec+anr+agc+bf 四开其余全关；ref_channels=2, out_channels=1
        │   1kHz 回声全链抑制实测 52dB
        ▼  rkaudio_vqe_48k（slave period_size=4800 ← 1600×3 落格）
        ▼  xr_vqe_16k = 薄 plug 仅 48k→16k ──▶ 前端

 播放：TTS 24k/1ch ─▶ xr_spk（plug→hw:rockchipes8388,0 48k/2ch）─▶ 板载喇叭
```

---

## 2. 仓库结构

```
board/                  前端 C 源码（xiaorui_live 单进程，39 个 .c）
board-server/           网关（fork 自 xiaozhi-esp32-server，Python）
  pkg/core/handle/      意图族（12 族注册表）+ 唤醒门/噪声门/短文本门
  start-gateway-instance.sh   双实例启动器（runtime.env + pidfile + release）
  es8388-aec-mixer.sh   采集 mixer 持久化脚本
  xiaorui.env.example   前端环境变量模板
  config/               音频配置（asound-vqe/asound-novqe/VQE json/唤醒词表）
tools/                  构建与全部测试入口（§6）
tests/                  单测 + 声学套件（audio/ 语料不入库，gen 脚本复现）
```

## 3. 板端环境与模型资源

### 3.1 /data 目录布局（合计 ~4.9G，全部可从仓内/备份重建）

| 目录 | 大小 | 内容 | 谁在用 |
|---|---|---|---|
| `xiaorui-server/` | 1.6G | 前端二进制（board-scripts/）、RKLLM 模型、matcha、start-all.sh、xiaorui.env、ha_token.txt、mdns-ha.py、llm_rkllm.py | 前端 + RKLLM + mdns |
| `ha-env/` | 1.4G | HA 2026.10.2 的 Python 3.14 venv（含 pymicro-vad/pyspeex-noise 交叉编译 wheel） | Home Assistant |
| `miniforge/` | 630M | 双网关 Python 3.14 运行时（conda，已去重精简：版本别名硬链合一+去包管理器/头文件） | 8001/8002 网关 + RKLLM 启动器 + HA 借 libstdc++ |
| `xiaorui-gateway/` | 119M | instances/（双实例 profile）、releases/（当前版+回滚版，测试候选用毕即清）、deps/（MCP 音乐）、声纹库、定时 sqlite；模型经软链共享 `/data/xiaorui-server/server/models`，不再每实例复制 | 双网关 |
| `xiaorui-ffmpeg/` | 588M | ffmpeg 9.0.2 束（conda 无头版，硬链 openvino 107M 删不得） | 音乐/电台拉流转码、HA |
| `rknn-test/sherpa/` | 566M | KWS/ASR/声纹三模型 + sherpa-onnx v1.13.8 运行库 | 前端（内嵌路径）+ 网关声纹 |
| `kws-corp/` | 14M | 唤醒正负语料（100+100，转写地面真值） | 声学回归 |
| `ha-config/` | 7.7M | HA 配置 + .storage（米家 OAuth 绑定存于此目录） | Home Assistant |

### 3.2 运行环境（三个独立 Python/二进制束，互不共享）

| 环境 | 版本 | 角色 | 关键约束 |
|---|---|---|---|
| `miniforge` | Python 3.14.7 | 双网关 app.py + llm_rkllm.py（RKLLM 守护） | — |
| `ha-env` | Python 3.14.7 venv | homeassistant + mdns-ha.py | **启动必须 `LD_LIBRARY_PATH=/data/miniforge/lib`**（系统 libstdc++ 缺 CXXABI_1.3.15） |
| `xiaorui-ffmpeg` | ffmpeg 9.0.2 | 音乐链路拉流解码→PCM→opus | 软链 `/usr/bin/ffmpeg`；openvino 是二进制 NEEDED 硬依赖 |
| VQE 库 | 瑞芯微 VQE 四件套库 | 采集增强 | `librkaudio_{vqe,common,nn}.so`→`/usr/lib/`；插件修复版→`/usr/lib/alsa-lib/`；json+wopt→`/usr/share/rkaudio/` |

### 3.3 常驻进程与端口

| 进程 | 可执行 | 端口 |
|---|---|---|
| xiaorui_live（单实例持麦） | `/data/xiaorui-server/board-scripts/xiaorui_live` | — |
| 云端网关 8001 | miniforge python + releases/<id>/server/app.py | WS **8001** / 健康 **8004** |
| 本地网关 8002 | 同上（可与 8001 共享 release） | WS **8002** / 健康 **8005** |
| RKLLM 推理服务 | `llm_rkllm.py --model qwen25-1.5b…` | **8112**（8002 独占管理） |
| Home Assistant | ha-env python -m homeassistant -c /data/ha-config | **8123** |
| mdns-ha 应答器 | ha-env python mdns-ha.py | :5353（homeassistant.local→板 IP） |
| crond 看护 | watch-gateways.sh */5 | 双网关 + RKLLM 自愈（跑 8001/8002 两个启动器） |

### 3.4 模型资源详表

| 模型 | 用途 / 加载方 | 大小 | 板上路径 | 来源 |
|---|---|---|---|---|
| Qwen2.5-1.5B w4a16 | 本地网关 LLM；RKLLM@8112（8002 独占），短答+prompt 缓存实测首 token 0.2s/整句 1.5s | 1.4G | `/data/xiaorui-server/qwen25-1.5b-rk3576-w4a16.rkllm` | RKLLM 转换件，**不入 git**，获取见 §5.3（另有 0.5B，身份答非所问已弃用） |
| Paraformer-large（20s 单遍） | 前端识别精修（RKNN NPU 0.56s/句，流式已删）；音频轮上云时由云端 ASR 代替 | 458M | `/data/rknn-test/sherpa/model-20s` | sherpa-onnx RKNN 转换 |
| KWS 唤醒 3.3M | 声学唤醒，常驻喂帧，阈值 0.10；**keywords.txt v1.13.8 格式=行内前缀 `@名 :boost tokens`，三字段格式会报错** | 5.5M | `/data/rknn-test/sherpa/model-kws` | ModelScope |
| campplus 192 维 | 声纹注册/"我是谁"；网关 voiceprint.model 加载 | 27M | `/data/rknn-test/sherpa/model-voiceprint` | 随 sherpa |
| Matcha zh-baker（icefall） | 双网关本地 TTS（乐声一般但全离线） | 92M | `/data/xiaorui-server/tts-matcha/matcha-icefall-zh-baker` | hf-mirror 直链 |
| wopt 2mic 线性 60mm | VQE bf 波束权重 | 80K | `/usr/share/rkaudio/wopt_2mic_linear_60mm_*.bin` | 随瑞芯微 VQE 发布包（2mic_2ref 配置） |
| 云端模型（qwen-flash + 云 ASR） | 在云端不占用板端资源 | — | dashscope | — |

sherpa-onnx v1.13.8 运行库（libonnxruntime/libsherpa-*.so）在
`/data/rknn-test/sherpa/sherpa-onnx-*/lib`，由 start-all.sh 经
`LD_LIBRARY_PATH` 注入前端。

## 4. 配置参考

### 4.1 前端环境变量（`/data/xiaorui-server/xiaorui.env`，须 export）

| 变量 | 默认 | 说明 |
|---|---|---|
| `XR_BACKEND` | local | `hybrid`（生产）/ `cloud` / `local` |
| `XR_CLOUD_URL` | — | 云端网关 ws 地址（板上 `ws://127.0.0.1:8001/xiaorui/v1/`） |
| `XR_LOCAL_PORT` | 8002 | 本地网关端口 |
| `XR_MIC` | asr_mic | 采集设备；ES8388 AEC 链 = `xr_vqe_16k`（设备名由这里给，核心不认平台） |
| `XR_MIC_PERIOD` | 1600 | VQE 采集周期（帧）；3576 插件 48k/4800 → 16k 落 1600，RK3308 插件只认 256 |
| `XR_SPK` | xr_spk | 播放设备名（plug → hw:rockchipes8388,0，48k/2ch） |
| `XR_KWS_THRESHOLD` | 0.15 | 唤醒阈值；ES8388+bf 实测定 **0.10** |
| `XR_CLOUD_ASR` | 0 | 音频轮上云转写（hybrid 生产=1） |
| `XR_MUSIC_CONTROLS` | 0 | 播放期语音控制 |
| `XR_THINK_CUE_MS` | — | 等答"嗯"填充音阈值 |
| `XR_TEXT_INJECT` | — | 调试钩子：注入文本轮（**不过冷态唤醒门**） |
| `XR_UTT_DUMP` | — | 调试：落 `<dir>/raw.pcm`+`utt-NNN.wav`（主循环：滚动原始帧/上传前句子）与 `ctrl-raw.pcm`+`ctrl-utt-NNN.wav`（播放期控制线程同款，KWS 漏检归因用）；不设零开销 |
| `XR_VAD_TRIGGER_DB` / `XR_VAD_SUSTAIN_DB` / `XR_VAD_PREROLL` | 6 / 2 / 5 | 触发门、持续门（相对底噪的 dB）与预滚帧数 |

### 4.2 音频节点（/etc/asound.conf，VQE 版=board-server/config/asound-vqe.conf）

- `rkaudio_vqe_48k`：VQE 插件。slave **直挂 hw:rockchipes8388,0**（不要加 dsnoop，插件不支持
  refine）；`period_size 4800`（前端 EXACT 1600@16k 的 3 倍落格，768 打不开）；
  `ref_channels 2` + `out_channels 1`（原生单声道直出，外包 route/plug 转通道
  必报 Slave PCM not usable）。
- `xr_vqe_16k`：薄 plug，只做 48k→16k。
- `xr_spk`：plug → hw:rockchipes8388,0，48k/2ch 播放。

### 4.3 VQE 开关（/usr/share/rkaudio/config_aivqe_new.json）

生产定版：**aec / bf / agc / anr 四开，其余全关**；agc=max_gain 15 / max_peak -10；
bf 用随包 2mic 线性 60mm wopt。采集 mixer 三件套每次开机必须重设（已挂
start-all.sh 调 `/data/es8388-aec-mixer.sh`）：`numid 52=8`（左麦 PGA）、
`22=1`（SAI1 SDI1 回环开）、`18=0`（回环源=SDO0）。

### 4.4 网关双实例（release 机制）

实例 = `instances/<role>/runtime.env`（指向 `releases/<id>/server` 代码目录 +
work/data 目录）+ `run/server.pid`。启动/重启**只经** `start-gateway-instance.sh
<role>` + pidfile 判活；改代码 = 新建候选 release → runtime.env 指向 → pidfile
重启，回滚 = 指回旧 release。**8001/8002 可能共享同一 release，不要原地修改共享代码**。
HA 侧读 `XR_HA_URL` / `XR_HA_TOKEN`。

## 5. 复现指南（裸板 → 全栈跑通）

### 5.1 前提（按采音路线分两种情况）

- RK3576 EVB1 + **RK3576_LINUX6.1_SDK 出来的固件**
- USB adb（`2207:0006` 系统态 / `2207:350e` loader 态）；upgrade_tool v2.30

**路线 A：无 VQE（纯仓内件，无需任何专有件）**——详见 §5.2。
**路线 B：VQE AEC 采音（当前生产）**——瑞芯微 VQE 相关件（算法库四件套+
ALSA 插件+wopt 权重）**向瑞芯微申请 .so 即可**，均只以二进制形式提供、
不随本仓分发。还需烧内核补丁
`patches/es8323-capture-channels-16.patch`（capture channels_max 2→16，SAI
回环 4ch 采集的硬前提；`./build.sh rockchip_rk3576_evb1_v10_defconfig &&
./build.sh kernel` 产 FIT boot.img，`di -b` 只烧 boot 分区）。实测回声抑制
1kHz 52dB。配置见 §4.2/§4.3 与 board-server/config/。

### 5.2 无 VQE 采集配置（路线 A，纯仓内件）

不依赖任何专有件即可运行完整语音链（唤醒/对话/意图/HA 控制全通），只是没有
回声抑制。配置文件 `board-server/config/asound-novqe.conf`，两种采音选路：

| 选路 | XR_MIC | 前提 | 说明 |
|---|---|---|---|
| USB 麦 | `asr_mic`（前端默认） | ZR USB 声卡 | 最稳，零 mixer 配置，底噪 -75dB |
| 板载 ES8388 直采 | `xr_mic_16k` | 原厂内核即可（2ch） | 每次开机需 `amixer -c rockchipes8388 cset numid=52 8`（左麦 PGA，建议并入 start-all.sh） |

```sh
adb push board-server/config/asound-novqe.conf /etc/asound.conf
# xiaorui.env: 按 §4.1 设置；KWS 阈值用 0.15（VQE 版的 0.10 是 bf/agc 抬升电平后的值）
```

与 VQE 路线的差异（预期管理）：无 AEC，喇叭播放期间的回声靠前端回声护栏缓解，
安静房间打断基本可靠、嘈杂环境成功率下降；无 ANR/AGC，识别对环境噪声更敏感；
ES8388 直采底噪/灵敏度弱于 USB 麦（追求唤醒稳定优先 USB）。两条路都**不需要**
内核补丁与 SAI 回环 mixer。

### 5.3 模型与依赖获取（大文件不入 git）

git 仓库不含任何模型/大二进制（`.gitignore`：`*.rkllm *.onnx *.rknn *.so
*.wav miniforge*/ tts-*/ wheels*/`；提示音素材 board-server/cues/ 除外），
clone 后跑：

```sh
tools/download-models.sh            # 公开件自动下载+sha256 核对; --list 看清单
```

| 项 | 体积 | 获取方式 |
|---|---|---|
| Matcha zh-baker | 92M | **脚本自动下载**（sherpa-onnx 官方 release，sha 实测合） |
| KWS 3.3M 声学 | 5.5M | **脚本自动下载**（ModelScope `pkufool` 镜像，四文件全部与板上件逐字节一致——实测；GitHub 官方包 sha 不同勿换源）；**keywords.txt 用仓根副本**（自定义"小瑞"词表） |
| campplus 声纹 | 27M | **脚本自动下载**（sherpa 官方 release，tag 名 `speaker-recongition-models` 即上游拼写，与板上件逐字节一致——实测） |
| Paraformer RKNN 三件 | 458M | **脚本自动下载**（sherpa-onnx 官方 rk3576 预转换包，与姊妹仓 [rk3576-asr-live](https://github.com/piaozhiye/rk3576-asr-live) 的 setup_models.sh 同源） |
| Qwen2.5-1.5B rkllm | 1.4G | 脚本可自动下载 **HF 预转换件**（`RKNNAI/RK3576-LLM-Qwen2.5-1.5B-Instruct`，文件名运行时发现、HF 不通自动走 hf-mirror，仓页面附 SHA-256 可手工核；**该组织官方性未核实**——Rockchip 官方分发渠道是 [airockchip/rknn-llm](https://github.com/airockchip/rknn-llm) README 所引 rkllm_model_zoo 网盘，取码 rkllm；此件与本项目精确件**非同字节**——实测 1.08G vs 1.4G，不同转换变体，换用需对齐 llm_rkllm.py 参数）；逐字节复现本项目则自备精确件（sha 见脚本）；工具链+runtime 见 [airockchip/rknn-llm](https://github.com/airockchip/rknn-llm) |
| sherpa-onnx RKNN 运行库 | 47M | **脚本自动下载**（姊妹仓 vendored 三件套直链） |
| wopt bf 权重 + VQE 库/插件 | ~1M+26M | **向瑞芯微申请 .so 即可**（仅路线 B 需要，全部二进制件，不随本仓分发） |
| miniforge / ha-env / ffmpeg 束 | 2.6G+588M | 板端备份恢复（conda 环境，无法由脚本重建） |

原则：**能公开下载的都给了 sha256 锚点自动核验**；自制/专有件在脚本里标 SELF
并写明指纹与来源，缺件时退出码非零，不会静默装一半。各组件许可与商用状态
见 §8.1（唯一例外：KWS 数据血统，§8.2）。

### 5.4 构建

```sh
./build-board-live.sh        # → board/xiaorui_live（buildroot aarch64 工具链,
                             #   -march=armv8-a 防 LSE，file-prefix-map 保 SHA 稳定）
tools/check-no-lse.sh        # LSE 门禁必须绿
```

### 5.5 板端铺板（从仓内件逐件推送，无预打包）

板上目录布局是前端/网关内嵌的，路径必须一致：

```
/data/xiaorui-server/            前端 + 模型 + 启动脚本
  board-scripts/xiaorui_live     构建产物
  start-all.sh xiaorui.env llm_rkllm.py mdns-ha.py
  qwen25-1.5b-rk3576-w4a16.rkllm tts-matcha/
/data/xiaorui-gateway/
  releases/<id>/server/          网关代码（=board-server/pkg 内容）
  instances/8001|8002/           runtime.env + work/ + data/（config 含密钥, 自备）
  start-gateway-instance.sh watch-gateways.sh
/data/rknn-test/sherpa/          model-kws/ model-20s/ model-voiceprint/ + sherpa-onnx 运行库 lib/
```

逐件铺板（在仓库根执行）：

```sh
adb shell 'mkdir -p /data/xiaorui-server/board-scripts /data/xiaorui-gateway/releases/r1/server \
  /data/xiaorui-gateway/instances/8001/work /data/xiaorui-gateway/instances/8001/data \
  /data/xiaorui-gateway/instances/8002/work /data/xiaorui-gateway/instances/8002/data \
  /data/rknn-test/sherpa /usr/share/rkaudio'
adb push board/xiaorui_live /data/xiaorui-server/board-scripts/
adb push board-server/start-all.sh board-server/llm_rkllm.py board-server/mdns-ha.py /data/xiaorui-server/
( cd board-server/pkg && tar cf - . ) | adb shell 'tar xf - -C /data/xiaorui-gateway/releases/r1/server'
adb push board-server/start-gateway-instance.sh board-server/watch-gateways.sh /data/xiaorui-gateway/
```

模型放置（来源见 §5.3，路径见 §3.4）：`model-kws/model-20s/model-voiceprint` 三目录 +
sherpa-onnx 运行库 → `/data/rknn-test/sherpa/`；`tts-matcha/`、qwen rkllm →
`/data/xiaorui-server/`；`keywords.txt`（仓根）→ `model-kws/` 内。

网关实例与运行环境：

```sh
# 两实例 runtime.env: 从 board-server/instance.env.example 拷改
#   8001: XR_HTTP_PORT=8004   8002: XR_HTTP_PORT=8005; CODE_DIR 指 releases/r1/server
# 网关 python 环境: conda python3.14 + pip install -r board-server/pkg/requirements.txt
#   (生产用 /data/miniforge, 自建等价即可; llm_rkllm 还需 librkllmrt.so 与 RKLLM runtime)
```

音频配置与系统位：

```sh
adb push board-server/config/asound-vqe.conf /etc/asound.conf        # 路线 A 换 asound-novqe.conf
adb push board-server/config/config_aivqe_new.json /usr/share/rkaudio/   # 路线 B
adb push board-server/es8388-aec-mixer.sh /data/                      # 路线 B, 每次开机要跑(start-all 已挂)
adb shell 'chmod +x /data/xiaorui-server/board-scripts/xiaorui_live /data/xiaorui-server/*.sh \
  /data/xiaorui-gateway/*.sh /data/es8388-aec-mixer.sh'               # adb 推送丢执行位
adb push board-server/xiaorui.env.example /data/xiaorui-server/xiaorui.env  # 按 §4.1 改(backend/URL/阈值)
cat > /tmp/S99xrvoice <<'EOS'
#!/bin/sh
sh /data/xiaorui-server/start-all.sh > /tmp/xr_start.log 2>&1 &
EOS
adb push /tmp/S99xrvoice /etc/init.d/ && adb shell 'chmod +x /etc/init.d/S99xrvoice'
adb shell '(crontab -l 2>/dev/null; echo "*/5 * * * * /data/xiaorui-gateway/watch-gateways.sh >/dev/null 2>&1") | crontab -'
```

路线 B 的瑞芯微 VQE 件（申请到的 .so）：四件套库 → `/usr/lib/`，ALSA 插件 →
`/usr/lib/alsa-lib/`，wopt 权重 → `/usr/share/rkaudio/`。
可选组件：Home Assistant（8123，设备控制用；需自装 venv，**启动须
`LD_LIBRARY_PATH` 指向新 libstdc++ 所在目录**，且不要起第二个实例写同一 .storage）、
ffmpeg 束 → 软链 `/usr/bin/ffmpeg`（在线音乐/电台链路）。

### 5.6 启动与验证

```sh
adb shell 'sh /data/xiaorui-server/start-all.sh'
adb shell "ss -tln | grep -E ':8001|:8002'"      # 8002 起 RKLLM 需 ~30s
adb shell 'tail -f /tmp/xr_live.log'             # 期待 "mic=xr_vqe_16k 聆听中"
adb forward tcp:18123 tcp:8123       # HA 声学套件判据通道
```

## 6. 测试体系

| 入口 | 覆盖 | 备注 |
|---|---|---|
| `tools/run-gates.sh` | **40 项**主机门禁：单测/意图族/交叉构建/no-LSE/引用追踪/天气私有凭据/日志兜底 | 提交前全绿 |
| `tools/run-ha-acoustic.sh` | 灯控声学闭环（PC 放语料→唤醒→意图→HA 翻转三重判据，单次计分；运行会出声） | 需接 HA 实例 |
| `tools/run-wake-silent-live.py --apply` | 隔离候选全套：静默 180s + 唤醒正反例 + HA 3 轮，结束自动恢复生产 | 候选晋升的唯一入口 |
| `tools/test-music-candidate.py --apply --acoustic` | 音乐播放期隔离候选套件：网关让位/续播协议探针 + 三腿声学（纯唤醒忽略不转发 / 问句让位+恢复 / 停止打断不断连，PC 放语料）；结束自动恢复生产 | `--pure-wake-wav` 显式语料（电平/削波校验+指纹入档）、`--utt-dump` 拉回板端 mic 录音与电平 |
| `tools/gen-*.py` | matcha 语料合成（响度对齐 rms≈4558） | tests/audio 不入库 |

**声学时序纪律**：任何"放语料→等回答"循环必须等前端日志真正静默
（`wait_board_quiet`，mtime ≥32s）再放下一句——固定 sleep 会在回答链变长时
与下一轮播放重叠。HA 轮间隔 ≥35s（免唤醒窗 30s）。

意图族注册表（intentHandler.py，顺序即语义）：`reboot → volume → ha-light →
vp-enroll → whoami → repeat → time → next → radio → timer-list → timer-set →
clock(raw)`。LLM 之前三道门：单字气口噪声门 → 唤醒词/形状剥前缀（引导词白名单
空/喂/为/谢谢/哎/嗯，最长两字）→ 短文本门（<4 字回"没听清"不进 LLM）。

## 7. 已知限制与注意事项

1. **LSE**：工具链默认架构产 `casal`，ARMv8.0 板上 SIGILL——交叉构建一律
   `-march=armv8-a`，`check-no-lse.sh` 门禁。
2. **前端 period EXACT 语义**：VQE 插件 slave period 必须 4800（=1600×3）。
3. **busybox ldd 静默失灵**：ffmpeg 实链 openvino 而 ldd 报 0，按 ldd 删库必炸
   （openvino 107M 删不得）。
4. **HA venv**：必须 `LD_LIBRARY_PATH=/data/miniforge/lib`（系统 libstdc++ 缺
   CXXABI_1.3.15）；**双 HA 实例写同一 .storage 会抹密码哈希**，单实例铁律。
5. **进程管理**：`pkill -x` 超 15 字符截断失配（判活读 /proc/*/exe）；杀网关只认
   `instances/<role>/run/server.pid`；adb shell 后台进程要 `(setsid … &)` 子壳。

## 8. 许可与致谢

### 8.1 授权一览

| 组件 | 许可 | 商用 |
|---|---|---|
| 本项目自有代码 | **MIT** | ✅ |
| 网关上游 [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) | MIT | ✅ |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 运行库 | Apache-2.0 | ✅ |
| [Home Assistant](https://www.home-assistant.io/) | Apache-2.0 | ✅ |
| RKLLM/RKNN 运行时（librkllmrt/librknnrt） | 以实际使用版本附带的许可为准，不由仓库级 LICENSE 推定 | 分发前核对 |
| Qwen2.5-1.5B rkllm 模型 | Apache-2.0（[RKNNAI](https://huggingface.co/RKNNAI/RK3576-LLM-Qwen2.5-1.5B-Instruct) 仓声明+Qwen2.5 基座均 Apache-2.0） | ✅ |
| Paraformer RKNN / campplus / Matcha TTS | Apache-2.0（sherpa 发布口径/3D-Speaker） | ✅ |
| KWS 唤醒模型（wenetspeech 3.3M） | Apache-2.0（作者 pkufool 声明）；**例外见 8.2** | ⚠️ 自评 |
| 瑞芯微 VQE 件（算法库/ALSA 插件/wopt 权重） | Rockchip SDK 条款 | 按 SDK 条款 |
| RK3576_LINUX6.1_SDK/固件 | Rockchip SDK 条款 | 按 SDK 条款 |

### 8.2 KWS 模型与训练数据

KWS 模型作者在 ModelScope 声明 Apache-2.0（模型层可商用），但其训练语料
WenetSpeech 为网络抓取音频、数据集本体仅限研究用途——数据层是否传导到模型
权重无司法定论。注意同作者的 GigaSpeech 版为**英文模型**（英文 BPE，认不了
中文唤醒词）。选型时应同时核对模型与训练数据的许可；可按作者声明评估，
或用授权明确的中文语料自训（icefall）。不对商用或演示用途作统一授权保证。

上游版权声明全文见 THIRD-PARTY-NOTICES.md。

### 8.3 致谢

- [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)——网关层上游
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) / pkufool 的 KWS 模型——唤醒与识别
- [Home Assistant](https://www.home-assistant.io/)——设备控制中枢
- Rockchip RKLLM / RKNN / rkaudio VQE——端侧推理与语音增强

---

## 9. 关注公众号

更多嵌入式 / 端侧 AI 的实战文章在这个微信公众号：

<p align="center">
  <img src="assets/wechat-account-qr.jpg" width="240" alt="微信公众号二维码" />
</p>
