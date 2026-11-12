# RK3562 部署边界与前置资源

从空白环境准备资源、内核补丁、构建与校验步骤见 [REPRODUCE.md](REPRODUCE.md)。

这些脚本面向 RK3562 EVB2 的板内网关形态。ADB 需要访问目标板；有多台设备时，
为脚本显式传入 serial（云端和 HA 脚本使用 `-s serial`）。
部署会更新板端程序、配置与开机脚本，请勿在生产设备上直接试跑。

## 不随 Git 分发的资源

仓库不包含模型、Rockchip SDK/运行时、sherpa-onnx 动态库、Python wheel 和私有凭据。
部署前需从对应上游发布或合法获得的 SDK 准备资源，并记录版本及 SHA256。
没有这些资源时，克隆仓库本身不足以复现整套系统。

| 资源 | 默认位置或参数 |
| --- | --- |
| Rockchip VQE 插件包及 `6mic_2ref` 配置 | 必须设置 `XR_VQE_PKG` |
| VQE 配套动态库 | `XR_VQE_LIB_DIR`，默认插件包的 `lib/linux-aarch64` |
| sherpa-onnx C API 和 ONNX Runtime 的 aarch64 库 | `XR_SHERPA_LIB_DIR`，默认 `vendor/sherpa-onnx/lib` |
| aarch64 Miniforge 安装器（首次安装） | `XR_MINIFORGE`，默认 `board-server/miniforge.sh` |
| 匹配板端 Python ABI 的 aarch64 wheel 集 | `XR_GATEWAY_WHEELS`，默认 `board-server/wheels314` |
| RK3562 RKLLM 运行时 | `board-server/librkllmrt.so` |
| Qwen2.5-0.5B-Instruct 的 RK3562 w8a8 模型 | `XR_RKLLM_MODEL`，默认 `models/qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm` |
| SenseVoice ASR 模型目录 | `models/sherpa-onnx-sense-voice-zh` |
| Matcha TTS 模型目录 | `tts-matcha/matcha-icefall-zh-baker` |
| Silero VAD ONNX | `XR_SILERO_VAD_MODEL`，默认 `models/snakers4_silero-vad/src/silero_vad/data/silero_vad.onnx` |
| 前端程序与 KWS 模型 | `board/xiaorui_live` 和 `models/model-kws` |

PC 需要 `adb`、Bash、Python 3.11+ 和 PyYAML。模型、运行时与工具链须配套，
尤其不要将 RK3576 模型或不支持 RK3562 的量化类型直接用于本板。
Miniforge 安装器须提供与 wheel 集相匹配的 Python 版本；安装脚本不会自动下载这些资源。
HA 首次安装需要板端网络；依赖版本见本目录的 HA 锁文件。

## 推荐顺序

准备好上述资源及环境变量后，在仓根执行：

```bash
bash ports/rk3562/deploy-vqe.sh SERIAL
bash ports/rk3562/deploy-board-gateway.sh SERIAL
bash ports/rk3562/deploy-and-run.sh SERIAL
bash ports/rk3562/sync-board-time.sh SERIAL
```

`deploy-vqe.sh` 负责生成 `/etc/asound.conf`。前端部署只检查 `xr_vqe_16k` 和
`xr_spk` 是否存在，不会覆盖该配置。板内网关部署覆盖其离线基线配置，
应先保存用户自定义设置；该入口不支持缺少 RKLLM 模型时自动回退到 PC。
部署脚本的端口检查只代表服务监听，不等于语音 E2E 验收通过。
前端重部署通过 S98 启动链重新初始化混音器，包括先关再开采集路径，
避免前端退出后 RK809 回采未重新上电。
如需另行使用 PC 网关备选形态，先将 `gateway-local.example.yaml` 中的
`/path/to/...` 替换为本机模型绝对路径；它不是上述板内部署流程的配置。

## HA 凭据与重复部署

HA 部署不再提供默认管理员密码。首次显式设置安全密码；已有 `xiaorui` 账号时
必须使用原密码。可通过 Bash 无回显读取，避免把密码写入命令历史：

```bash
read -r -s -p 'HA password: ' XR_HA_ADMIN_PWD
printf '\n'
export XR_HA_ADMIN_PWD
bash ports/rk3562/deploy-ha.sh -s SERIAL
unset XR_HA_ADMIN_PWD
```

密码通过 ADB 标准输入传递，不拼接到远端 shell 命令或 Python 参数中。
令牌使用权限为 `0600` 的临时文件原子替换。重新部署 HA 不再调用整栈 `start`，
避免切换正在使用的音频路径和重复拉起前端。

**已经用旧默认密码创建的账号不会自动改密。** 请在 HA 中修改该账号密码，
核查已有长期令牌，必要时撤销并重新生成。更新脚本不能撤回已经公开的历史内容。

## 云端切换与回滚

```bash
bash ports/rk3562/deploy-cloud-gateway.sh -s SERIAL --key-file /private/path/cloud-key
bash ports/rk3562/rollback-cloud-gateway.sh -s SERIAL
```

也可用 `XR_CLOUD_PC_CONFIG` 指定包含 `LLM.AliLLM.api_key` 的私有 YAML。
脚本不再猜测本机其他项目的配置路径。切换包含一次真实云 LLM 请求，会产生用量。

每次从已识别的离线形态切换时，原子更新 `.config.yaml.board-local-bak`；
已经处于云端时保留并校验离线快照。未知形态或无效快照会报错。
回滚先校验 ASR/LLM 均为离线模块，再原子恢复；切换后发生错误不会自动回滚，
请根据错误提示显式恢复。

## 本地工具与公开仓边界

现场录音重放、HA A/B、LLM 延迟测量和资源采样脚本只在本地保留，由 `.gitignore`
排除，不作为公开部署依赖。真人录音、文章素材、交接文档和私有密钥不得提交。
公开验收需单独检查语音问答、设备实际状态及云端/离线形态，不能仅看日志或退出码。

## 测试前置条件

本次部署与唤醒词配置的回归可在准备好 PC Python 测试依赖后执行：

```bash
python3 -m pytest tests/test_rk3562_deployment.py tests/test_wakeup_config.py -q
```

完整 `tests/` 还包含原有的板端候选验收和 VQE 契约测试，依赖
`board/xiaorui_live` 构建产物及未公开的 `vendor/rkaudio-vqe/` 源码、测试程序和插件库。
这些资源缺失时对应测试会失败，不能把本机完整工作区的全绿结果等同于干净克隆全绿。
请自行准备合法获得的依赖，不要为了通过测试将私有 SDK 或二进制加入公开提交。
