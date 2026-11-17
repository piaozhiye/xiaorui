"""RK3562 + ES8388 (AIOT DEMO LP4X V10) 小瑞移植 profile 的约束测试。

这块板与 ports/rk3562 的 RK809 + PDM + VQE 板完全不同:
  - codec 是 ES8388(模拟麦/喇叭), 没有 PDM 阵列, 没有回采 ref
  - 采集走前端 es8388 直采(48k/2ch -> FIR ÷3 -> 16k mono), 不装 VQE
  - 前端设备名不能含 xr_vqe / rkaudio_vqe
  - 开机自启脚本与旧板(RK809 控件)必须分开

本文件守住这些约束, 防止把旧板配置误用到新板上。
"""

import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time
import wave

import pytest


REPO = Path(__file__).resolve().parents[1]
PORT = REPO / "ports" / "rk3562"
ES8388 = REPO / "ports" / "rk3562-es8388"


def _load_sibling(module_name):
    """加载同目录下的测试模块(复用其 fake adb 夹具)。"""
    spec = importlib.util.spec_from_file_location(
        module_name, Path(__file__).with_name(module_name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_rk3562_tests = _load_sibling("test_rk3562_deployment")


@pytest.fixture
def fake_deployment_factory(monkeypatch, tmp_path):
    """转调旧板部署测试里的 fake adb 夹具, 但把 ports/rk3562-es8388 也复制进去。"""
    original = _rk3562_tests.fake_deployment.__wrapped__

    def factory():
        port, log = original(tmp_path, monkeypatch)
        target = tmp_path / "repo" / "ports" / "rk3562-es8388"
        target.mkdir(parents=True, exist_ok=True)
        if ES8388.is_dir():
            for item in ES8388.iterdir():
                if item.is_file():
                    shutil.copy(item, target / item.name)
        return port, log

    return factory


def adb_commands(log):
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def pushed_paths(log):
    paths = []
    for command in adb_commands(log):
        if "push" in command:
            paths.extend(command[command.index("push") + 1:][:2])
    return paths


def install_guard_adb(
    tmp_path, monkeypatch, *, model="RK3562 AIOT DEMO", card=True,
    occupied=False, profile_marked=False,
):
    """Tiny adb fake for probe/deploy target guards; logs every call."""
    bindir = tmp_path / "guard-bin"
    bindir.mkdir(exist_ok=True)
    adb = bindir / "adb"
    adb.write_text(f"#!{sys.executable}\n" + r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["GUARD_ADB_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
if args[-1] == "get-state":
    print("device")
    raise SystemExit(0)
if args[-1] == "cat /proc/device-tree/model":
    print(os.environ["GUARD_MODEL"])
    raise SystemExit(0)
cmd = args[-1] if args else ""
if "cat /proc/asound/cards" in cmd:
    print(" 0 [rockchipes8388 ]: rockchip-es8388 - rockchip-es8388" if os.environ["GUARD_CARD"] == "1"
          else " 0 [rkspdifsound ]: simple-card - rk-spdif-sound")
    raise SystemExit(0)
if "test -f /data/xiaorui/.rk3562-es8388-profile" in cmd:
    raise SystemExit(0 if os.environ["GUARD_MARKED"] == "1" else 1)
if "[ -e /etc/asound.conf ]" in cmd:
    raise SystemExit(0 if os.environ["GUARD_OCCUPIED"] == "1" else 1)
raise SystemExit(0)
''')
    adb.chmod(0o755)
    log = tmp_path / "guard-adb.log"
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("GUARD_ADB_LOG", str(log))
    monkeypatch.setenv("GUARD_MODEL", model)
    monkeypatch.setenv("GUARD_CARD", "1" if card else "0")
    monkeypatch.setenv("GUARD_OCCUPIED", "1" if occupied else "0")
    monkeypatch.setenv("GUARD_MARKED", "1" if profile_marked else "0")
    return log


# --------------------------------------------------------------------------
# Task 1: 板内网关部署脚本必须能被 ES8388 profile 复用, 且不覆盖旧板启动脚本
# --------------------------------------------------------------------------


def test_gateway_installer_pushes_legacy_init_by_default(fake_deployment_factory):
    port, log = fake_deployment_factory()
    result = subprocess.run(
        ["bash", str(port / "deploy-board-gateway.sh"), "fake-board"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert any("init-S98xiaorui" in path for path in pushed_paths(log))


def test_gateway_installer_skips_legacy_init_when_opted_out(fake_deployment_factory, monkeypatch):
    port, log = fake_deployment_factory()
    monkeypatch.setenv("XR_SKIP_INIT_INSTALL", "1")
    result = subprocess.run(
        ["bash", str(port / "deploy-board-gateway.sh"), "fake-board"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert not any("init-S98xiaorui" in path for path in pushed_paths(log))
    # 其余板内栈资源仍然要推
    pushed = " ".join(pushed_paths(log))
    assert "llm_rkllm.py" in pushed
    assert "server" in pushed


def test_gateway_installer_does_not_install_vqe_or_frontend(fake_deployment_factory):
    """ES8388 profile 不允许安装 VQE 插件或旧板前端。"""
    port, log = fake_deployment_factory()
    subprocess.run(
        ["bash", str(port / "deploy-board-gateway.sh"), "fake-board"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    pushed = " ".join(pushed_paths(log))
    assert "rkaudio_vqe" not in pushed
    assert "deploy-vqe" not in pushed


# --------------------------------------------------------------------------
# Task 3: ES8388 音频 profile 的契约
# --------------------------------------------------------------------------


def effective_env(text):
    """只看真正生效的 export 行, 忽略注释(注释里提到 VQE 是无害的说明)。"""
    return "\n".join(
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def strip_comments(text):
    """去掉整行注释(shell / ALSA 配置), 只看真正会执行的语句。"""
    return "\n".join(
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def test_es8388_frontend_env_uses_direct_capture():
    env = (ES8388 / "frontend.env.example").read_text()
    live = effective_env(env)
    assert "XR_MIC=hw:rockchipes8388,0" in live
    assert "XR_MIC_MODE=es8388" in live
    assert "XR_ES8388_CHANNELS=2" in live
    assert "XR_ES8388_MIC_CH=0" in live
    assert "XR_SPK=xr_spk" in live
    # 非 VQE 直采: 阈值用直采档(0.15), 不给 VQE 设备名/period
    assert "XR_KWS_THRESHOLD=0.15" in live
    for forbidden in ("xr_vqe", "rkaudio_vqe", "XR_MIC_PERIOD"):
        assert forbidden not in live, forbidden


def test_es8388_frontend_env_has_no_cloud_dependency():
    env = effective_env((ES8388 / "frontend.env.example").read_text())
    for forbidden in ("api_key", "apiKey", "dashscope", "qwen3-asr", "alibaba", "openai.com"):
        assert forbidden.lower() not in env.lower(), forbidden
    # 板内网关形态
    assert "ws://127.0.0.1:8001/xiaorui/v1/" in env


def test_es8388_keywords_include_single_and_double_wake_units():
    lines = (ES8388 / "keywords.txt").read_text().splitlines()
    assert len(lines) == 3
    assert all(line.strip() and not line.lstrip().startswith("#") for line in lines)
    assert any("@小瑞小瑞" in line for line in lines)
    assert any("@小瑞" in line and ":1.0" in line for line in lines)
    assert any("@你好小瑞" in line for line in lines)


def test_es8388_deploy_uses_profile_specific_keywords():
    script = strip_comments((ES8388 / "deploy.sh").read_text())
    assert '"$DIR/keywords.txt" /data/xiaorui/assets/kws/' in script
    assert '"$REPO/keywords.txt" /data/xiaorui/assets/kws/' not in script


def test_shared_no_vqe_alsa_config_targets_es8388_card():
    conf = (REPO / "board-server" / "config" / "asound-novqe.conf").read_text()
    assert "hw:rockchipes8388,0" in conf
    assert "pcm.xr_mic_16k" in conf
    assert "pcm.xr_spk" in conf
    # 采集 16k/1ch, 播放 48k/2ch
    mic = conf.split("pcm.xr_mic_16k")[1].split("}")[0]
    spk = conf.split("pcm.xr_spk")[1].split("}")[0]
    assert "rate 16000" in mic and "channels 1" in mic
    assert "rate 48000" in spk and "channels 2" in spk
    # 不能含 VQE 插件
    assert "rkaudio_vqe" not in conf


# --------------------------------------------------------------------------
# Task 3: 板载麦/喇叭 mixer 脚本
# --------------------------------------------------------------------------


def run_mixer_script(tmp_path, monkeypatch, card_override):
    bindir = tmp_path / "mixer-bin"
    bindir.mkdir()
    calls = tmp_path / "amixer.jsonl"
    amixer = bindir / "amixer"
    amixer.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['MIXER_CALLS'], 'a') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    amixer.chmod(0o755)
    awk = bindir / "awk"
    awk.write_text(f"#!{sys.executable}\nprint('7')\n")
    awk.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("MIXER_CALLS", str(calls))
    if card_override is None:
        monkeypatch.delenv("XR_AUDIO_CARD", raising=False)
    else:
        monkeypatch.setenv("XR_AUDIO_CARD", card_override)
    result = subprocess.run(
        ["sh", str(ES8388 / "mixer-es8388.sh")],
        capture_output=True, text=True, check=False, timeout=20,
    )
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    return result, recorded


def test_mixer_script_applies_measured_gains_and_switches(tmp_path, monkeypatch):
    result, calls = run_mixer_script(tmp_path, monkeypatch, "7")
    assert result.returncode == 0, result.stderr
    assert calls == [
        ["-c", "7", "cset", "name=Left Channel Capture Volume", "8"],
        ["-c", "7", "cset", "name=Right Channel Capture Volume", "8"],
        ["-c", "7", "sset", "Main Mic", "on"],
        ["-c", "7", "sset", "Speaker", "on"],
        ["-c", "7", "sset", "spk switch", "on"],
        ["-c", "7", "cset", "name=Output 1 Playback Volume", "15"],
        ["-c", "7", "cset", "name=Output 2 Playback Volume", "15"],
        ["-c", "7", "cset", "numid=62", "2CH: 1 Mic + 1 Loopback"],
    ]


def test_mixer_script_sets_dlp_before_any_stream_opens(tmp_path, monkeypatch):
    """DLP 模式必须在开流前设, 否则驱动的 dlp_mode_put 会返回 -EPERM。"""
    result, calls = run_mixer_script(tmp_path, monkeypatch, "7")
    assert result.returncode == 0, result.stderr
    dlp = [i for i, call in enumerate(calls) if call[:3] == ["-c", "7", "cset"] and call[3] == "numid=62"]
    assert dlp, calls
    # 它必须是最后一步(前端还没开流), 且不能夹在别的 amixer 调用中间
    assert dlp[-1] == len(calls) - 1, calls


def test_mixer_script_resolves_numeric_card_when_not_overridden(tmp_path, monkeypatch):
    result, calls = run_mixer_script(tmp_path, monkeypatch, None)
    assert result.returncode == 0, result.stderr
    assert calls and all(call[1] == "7" for call in calls)
    # DLP 数字回环现在是有意打开的(1 Mic + 1 Loopback), 但不能去动旧板的
    # SAI 数字回环控件(numid 22/18 那种) —— 那是 VQE 4ch 用的。
    dlp = [c for c in calls if "numid=62" in c]
    assert len(dlp) == 1, calls
    assert not any(c[3:5] == ["numid=22", "on"] or c[3:5] == ["numid=18", "on"] for c in calls)










def test_deploy_rejects_wrong_model_before_first_push(tmp_path, monkeypatch):
    log = install_guard_adb(tmp_path, monkeypatch, model="RK3576 EVB")
    monkeypatch.setenv("XR_SHERPA_BUILD", str(tmp_path / "sherpa"))
    monkeypatch.setenv("XR_LIBGOMP", str(tmp_path / "libgomp.so.1"))
    result = subprocess.run(
        ["bash", str(ES8388 / "deploy.sh"), "fake-serial"],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 1
    assert "不是 RK3562" in result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any("push" in command for command in commands)


def test_deploy_refuses_existing_unowned_board_data(tmp_path, monkeypatch):
    log = install_guard_adb(tmp_path, monkeypatch, occupied=True)
    monkeypatch.setenv("XR_SHERPA_BUILD", str(tmp_path / "sherpa"))
    monkeypatch.setenv("XR_LIBGOMP", str(tmp_path / "libgomp.so.1"))
    result = subprocess.run(
        ["bash", str(ES8388 / "deploy.sh"), "fake-serial"],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 1
    assert "无 ES8388 profile 标记" in result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any("push" in command for command in commands)
    assert not any("amixer" in command[-1] for command in commands if command)


def seed_deploy_assets(repo):
    """Create minimal local inputs so the deploy test reaches its storage gate."""
    files = (
        "board-server/miniforge.sh",
        "board-server/wheels314/requirements-board.lock",
        "board-server/librkllmrt.so",
        "models/qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm",
        "models/snakers4_silero-vad/src/silero_vad/data/silero_vad.onnx",
        "models/sherpa-onnx-sense-voice-zh/model.int8.onnx",
        "tts-matcha/matcha-icefall-zh-baker/model-steps-3.onnx",
        "board/board-server-libgomp.so.1",
        "ports/rk3562/check-assets.py",
        "vendor/sherpa-onnx/lib/libsherpa-onnx-c-api.so",
        "vendor/sherpa-onnx/lib/libonnxruntime.so",
        "board/xiaorui_live",
        "models/model-kws/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        "models/model-kws/decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        "models/model-kws/joiner-epoch-12-avg-2-chunk-16-left-64.onnx",
        "models/model-kws/tokens.txt",
        "board-server/cues/xr_ack24k.wav",
        "board-server/cues/xr_hmm24k.wav",
        "board-server/cues/xr_fail24k.wav",
        "board-server/cues/xr_volack24k.wav",
    )
    for relative in files:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (repo / "board-server/pkg").mkdir(parents=True, exist_ok=True)
    (repo / "ports/rk3562/check-assets.py").write_text("raise SystemExit(0)\n")


@pytest.mark.parametrize(
    ("available_kb", "expected"),
    [("1", "空间不足"), ("invalid", "无法读取 /data 可用空间")],
)
def test_deploy_rejects_insufficient_or_unreadable_storage_before_any_board_write(
    fake_deployment_factory, monkeypatch, available_kb, expected
):
    port, log = fake_deployment_factory()
    repo = port.parent.parent
    seed_deploy_assets(repo)
    deploy_script = repo / "ports/rk3562-es8388/deploy.sh"
    deploy_script.write_text(
        deploy_script.read_text().replace(
            "WORK=/tmp/opencode/rk3562-es8388",
            f"WORK={repo / 'preflight-work'}",
        )
    )
    sherpa_build = repo / "sherpa-build"
    for relative in (
        "lib/libsherpa-onnx-c-api.so",
        "_deps/onnxruntime-src/lib/libonnxruntime.so",
    ):
        path = sherpa_build / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setenv("XR_LIBGOMP", str(repo / "board/board-server-libgomp.so.1"))
    monkeypatch.setenv("XR_SHERPA_BUILD", str(sherpa_build))
    monkeypatch.delenv("XR_SHERPA_LIB_DIR", raising=False)
    monkeypatch.setenv("AVAILABLE_KB", available_kb)

    result = subprocess.run(
        ["bash", str(repo / "ports/rk3562-es8388/deploy.sh"), "fake-board"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode != 0
    assert expected in result.stderr
    commands = adb_commands(log)
    assert all(command[:2] == ["-s", "fake-board"] for command in commands)
    assert not any("push" in command for command in commands)
    assert not any(
        "mkdir" in command[-1] or "pkill" in command[-1]
        for command in commands if command and "shell" in command
    )



def test_rollback_dry_run_validates_without_touching_the_board(tmp_path, monkeypatch):
    """--dry-run 只能校验并打印计划, 不能停服务/改板。"""
    log = install_guard_adb(tmp_path, monkeypatch, profile_marked=True)
    backup = tmp_path / "backup"
    files = backup / "files"
    files.mkdir(parents=True)
    (files / "etc__asound.conf").write_text("old alsa config\n")
    (backup / "manifest").write_text(
        "PRESENT\t/etc/asound.conf\tetc__asound.conf\t644\n"
        "ABSENT\t/data/mixer-es8388.sh\tdata__mixer-es8388.sh\t-\n"
        "DIR_ABSENT\t/data/xiaorui-server\t-\t-\n"
    )
    result = subprocess.run(
        ["bash", str(ES8388 / "rollback.sh"), "fake-serial", str(backup), "--dry-run"],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    shell_commands = [c[-1] for c in commands if c and "shell" in c]
    assert not any("pkill" in c for c in shell_commands)
    assert not any("rm -" in c for c in shell_commands)
    assert not any("push" in c for c in commands)


def test_rollback_dry_run_still_rejects_unmanaged_files(tmp_path, monkeypatch):
    log = install_guard_adb(tmp_path, monkeypatch, profile_marked=True)
    backup = tmp_path / "backup-bad"
    backup.mkdir()
    (backup / "manifest").write_text(
        "PRESENT\t/etc/passwd\tetc__passwd\t644\n"
    )
    result = subprocess.run(
        ["bash", str(ES8388 / "rollback.sh"), "fake-serial", str(backup), "--dry-run"],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode != 0
    assert "未受管" in result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any("pkill" in c[-1] for c in commands if c and "shell" in c)


def test_deploy_on_installed_profile_repairs_runtime_without_rewriting_models(
    fake_deployment_factory, monkeypatch
):
    """已安装 profile 重部署: 必须能自愈运行时, 又不能重写共享模型/网关目录。

    回归: 重部署路径原本整段跳过 stage/网关安装, 于是旧的
    start-board-stack.sh、librkllmrt.so、libgomp.so.1 永远修不好 ——
    坏掉的东西恰好在被跳过的分支里, 部署会一直失败。
    """
    port, log = fake_deployment_factory()
    repo = port.parent.parent
    seed_deploy_assets(repo)
    work = repo / "deploy-work"
    deploy_script = repo / "ports/rk3562-es8388/deploy.sh"
    deploy_script.write_text(
        deploy_script.read_text().replace(
            "WORK=/tmp/opencode/rk3562-es8388", f"WORK={work}"
        )
    )
    # 用桩替换真机冒烟脚本, 让测试只关注部署编排本身。
    for name in ("check-runtime.sh", "stage-runtime.sh"):
        stub = repo / "ports/rk3562-es8388" / name
        stub.write_text("#!/bin/bash\necho \"stub $0\"\nexit 0\n")
    sherpa = repo / "sherpa-lib"
    (sherpa / "lib").mkdir(parents=True, exist_ok=True)
    for lib in ("libsherpa-onnx-c-api.so", "libonnxruntime.so"):
        (sherpa / "lib" / lib).touch()
    monkeypatch.setenv("XR_LIBGOMP", str(repo / "board/board-server-libgomp.so.1"))
    monkeypatch.setenv("XR_SHERPA_LIB_DIR", str(sherpa / "lib"))
    monkeypatch.setenv("AVAILABLE_KB", "9000000")
    monkeypatch.setenv("PROFILE_STATE", "installed")

    result = subprocess.run(
        ["bash", str(deploy_script), "fake-board"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    commands = adb_commands(log)
    pushed = [
        command[command.index("push") + 1:][:2]
        for command in commands if "push" in command
    ]
    pushed_text = " ".join(" ".join(item) for item in pushed)
    # 运行时前提必须重推, 否则重部署修不了自己。
    assert "librkllmrt.so /data/xiaorui-server/" in pushed_text
    assert "libgomp.so.1 /data/xiaorui-server/lib/libgomp.so.1" in pushed_text
    assert "start-board-stack.sh /data/xiaorui-gateway/start-board-stack.sh" in pushed_text
    # 但共享的大目录不能重写。
    assert "qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm" not in pushed_text
    assert "asr-sense-voice" not in pushed_text
    assert "tts-matcha" not in pushed_text
    # 状态文件最后必须是 installed, 不能停在 installing。
    state_writes = [
        command[-1] for command in commands
        if command and "shell" in command and ".rk3562-es8388-state" in command[-1]
    ]
    assert state_writes, result.stdout
    assert "installed" in state_writes[-1]
    assert "installing" not in state_writes[-1]
    # 回滚能力必须还在: 有新的备份目录和清单。
    backups = list(work.glob("backup-*/manifest"))
    assert backups, sorted(work.glob("*"))
    # mixer 现状必须被备份, 否则回滚还原不了音量/增益。
    mixer_state = list(work.glob("backup-*/mixer-state.txt"))
    assert mixer_state, sorted(work.glob("backup-*"))
    captured = mixer_state[0].read_text()
    assert "Output 1 Playback Volume|15" in captured
    assert "Main Mic|on" in captured


def test_rollback_restarts_the_stack_when_frontend_was_running(tmp_path, monkeypatch):
    """回滚把服务停了就得再拉起来, 否则板子从"能说话"变成"完全静默"。"""
    log = install_guard_adb(tmp_path, monkeypatch, profile_marked=True)
    backup = tmp_path / "backup-restart"
    files = backup / "files"
    files.mkdir(parents=True)
    (files / "etc__init.d__S98xiaorui").write_text("#!/bin/sh\n")
    (backup / "manifest").write_text(
        "PRESENT\t/etc/init.d/S98xiaorui\tetc__init.d__S98xiaorui\t755\n"
    )
    result = subprocess.run(
        ["bash", str(ES8388 / "rollback.sh"), "fake-serial", str(backup)],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    shell_commands = [command[-1] for command in commands if command and "shell" in command]
    assert any("S98xiaorui start" in command for command in shell_commands)


def test_rollback_reports_missing_backup_file_instead_of_silently_skipping(tmp_path, monkeypatch):
    """备份文件存在但为空时, 不能报成"备份文件丢失"(文件其实在)。"""
    log = install_guard_adb(tmp_path, monkeypatch, profile_marked=True)
    backup = tmp_path / "backup-empty"
    files = backup / "files"
    files.mkdir(parents=True)
    (files / "data__xiaorui-server__xiaorui_volume").write_text("")
    (backup / "manifest").write_text(
        "PRESENT\t/data/xiaorui-server/xiaorui_volume\tdata__xiaorui-server__xiaorui_volume\t644\n"
    )
    result = subprocess.run(
        ["bash", str(ES8388 / "rollback.sh"), "fake-serial", str(backup)],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    pushes = [command for command in commands if "push" in command]
    assert any(command[-1] == "/data/xiaorui-server/xiaorui_volume" for command in pushes)


def test_deploy_fails_when_a_pushed_artifact_is_missing_on_the_board(
    fake_deployment_factory, monkeypatch
):
    """push 失败(空间/只读/传输断)必须让部署失败, 不能照样打印"部署完成"。

    回归: 旧实现不核对板端产物, asound.conf 推失败也会一路走到"部署完成",
    留下一块没有开机自启的半成品板子。
    """
    port, log = fake_deployment_factory()
    repo = port.parent.parent
    seed_deploy_assets(repo)
    work = repo / "deploy-work"
    deploy_script = repo / "ports/rk3562-es8388/deploy.sh"
    deploy_script.write_text(
        deploy_script.read_text().replace(
            "WORK=/tmp/opencode/rk3562-es8388", f"WORK={work}"
        )
    )
    for name in ("check-runtime.sh", "stage-runtime.sh"):
        (repo / "ports/rk3562-es8388" / name).write_text("#!/bin/bash\nexit 0\n")
    sherpa = repo / "sherpa-lib"
    (sherpa / "lib").mkdir(parents=True, exist_ok=True)
    for lib in ("libsherpa-onnx-c-api.so", "libonnxruntime.so"):
        (sherpa / "lib" / lib).touch()
    monkeypatch.setenv("XR_LIBGOMP", str(repo / "board/board-server-libgomp.so.1"))
    monkeypatch.setenv("XR_SHERPA_LIB_DIR", str(sherpa / "lib"))
    monkeypatch.setenv("AVAILABLE_KB", "9000000")
    monkeypatch.setenv("PROFILE_STATE", "installed")
    monkeypatch.setenv("FAIL_BOARD_FILE", "/etc/asound.conf")

    result = subprocess.run(
        ["bash", str(deploy_script), "fake-board"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode != 0
    assert "板端缺少产物: /etc/asound.conf" in result.stderr
    assert "部署完成" not in result.stdout


def test_rollback_restores_mixer_state_from_backup(tmp_path, monkeypatch):
    """本 profile 改过麦 PGA 和喇叭音量, 回滚必须把它们还原。"""
    log = install_guard_adb(tmp_path, monkeypatch, profile_marked=True)
    backup = tmp_path / "backup-mixer"
    (backup / "files").mkdir(parents=True)
    (backup / "files" / "etc__asound.conf").write_text("old\n")
    (backup / "manifest").write_text(
        "PRESENT\t/etc/asound.conf\tetc__asound.conf\t644\n"
    )
    (backup / "mixer-state.txt").write_text(
        "Output 1 Playback Volume|27\n"
        "Main Mic|on\n"
    )
    result = subprocess.run(
        ["bash", str(ES8388 / "rollback.sh"), "fake-serial", str(backup)],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    shell_commands = [command[-1] for command in commands if command and "shell" in command]
    assert any(
        "amixer -c 0 cset name='Output 1 Playback Volume' '27'" in command
        for command in shell_commands
    )
    assert any(
        "amixer -c 0 sset 'Main Mic' 'on'" in command
        for command in shell_commands
    )


def test_rollback_rejects_self_referential_opus_symlink(tmp_path, monkeypatch):
    """相对目标 ../lib/libopus.so.0 相对 /usr/local/lib 解析后指回自己, 必须拒绝。"""
    log = install_guard_adb(tmp_path, monkeypatch, profile_marked=True)
    backup = tmp_path / "backup-opus"
    backup.mkdir()
    (backup / "manifest").write_text(
        "SYMLINK\t/usr/local/lib/libopus.so.0\t../lib/libopus.so.0\n"
    )
    result = subprocess.run(
        ["bash", str(ES8388 / "rollback.sh"), "fake-serial", str(backup)],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode != 0
    assert "opus" in result.stderr


def test_deploy_fails_when_the_frontend_is_not_alive_after_start(
    fake_deployment_factory, monkeypatch
):
    """部署说"完成"时前端必须真的在跑。

    回归: 旧实现只查日志里有没有"聆听中"这行字。日志是追加的, 上一轮
    的旧行也能满足这个检查, 于是"前端已经死了"照样打印"部署完成"。
    """
    port, log = fake_deployment_factory()
    repo = port.parent.parent
    seed_deploy_assets(repo)
    work = repo / "deploy-work"
    deploy_script = repo / "ports/rk3562-es8388/deploy.sh"
    deploy_script.write_text(
        deploy_script.read_text().replace(
            "WORK=/tmp/opencode/rk3562-es8388", f"WORK={work}"
        )
    )
    for name in ("check-runtime.sh", "stage-runtime.sh"):
        (repo / "ports/rk3562-es8388" / name).write_text("#!/bin/bash\nexit 0\n")
    sherpa = repo / "sherpa-lib"
    (sherpa / "lib").mkdir(parents=True, exist_ok=True)
    for lib in ("libsherpa-onnx-c-api.so", "libonnxruntime.so"):
        (sherpa / "lib" / lib).touch()
    monkeypatch.setenv("XR_LIBGOMP", str(repo / "board/board-server-libgomp.so.1"))
    monkeypatch.setenv("XR_SHERPA_LIB_DIR", str(sherpa / "lib"))
    monkeypatch.setenv("AVAILABLE_KB", "9000000")
    monkeypatch.setenv("PROFILE_STATE", "installed")
    monkeypatch.setenv("FRONTEND_ALIVE", "0")

    result = subprocess.run(
        ["bash", str(deploy_script), "fake-board"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode != 0
    assert "前端未运行" in result.stderr
    assert "部署完成" not in result.stdout


def test_rollback_requires_explicit_backup_manifest(tmp_path):
    result = subprocess.run(
        ["bash", str(ES8388 / "rollback.sh")],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    result = subprocess.run(
        ["bash", str(ES8388 / "rollback.sh"), "fake-serial", str(empty)],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 2
    assert "缺回滚清单" in result.stderr


def test_rollback_restores_only_manifested_files(tmp_path, monkeypatch):
    log = install_guard_adb(tmp_path, monkeypatch, profile_marked=True)
    backup = tmp_path / "backup"
    files = backup / "files"
    files.mkdir(parents=True)
    (files / "etc__asound.conf").write_text("old alsa config\n")
    (backup / "manifest").write_text(
        "PRESENT\t/etc/asound.conf\tetc__asound.conf\n"
        "ABSENT\t/data/mixer-es8388.sh\tdata__mixer-es8388.sh\n"
    )
    result = subprocess.run(
        ["bash", str(ES8388 / "rollback.sh"), "fake-serial", str(backup)],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    pushes = [command for command in commands if "push" in command]
    assert any(command[-1] == "/etc/asound.conf" for command in pushes)
    shell_commands = [command[-1] for command in commands if command and "shell" in command]
    assert any("rm -f '/data/mixer-es8388.sh'" in command for command in shell_commands)
    assert not any("rm -rf" in command for command in shell_commands)


def test_rollback_restores_every_manifest_entry_despite_adb_stdin(tmp_path, monkeypatch):
    """真实 adb shell 会把 stdin 透传给板端；循环里必须挡住它，否则清单被吃掉。

    回归: 回滚循环用 `while read ... done < manifest` 读清单，而循环体内的
    `adb shell`/`adb push` 会继承同一个 stdin 并把清单剩余行当输入读走，
    结果只回滚第一条。这是真机上才会暴露的失败，fake adb 必须模仿 adb 的行为。
    """
    bindir = tmp_path / "stdin-adb"
    bindir.mkdir()
    adb = bindir / "adb"
    adb.write_text(f"#!{sys.executable}\n" + r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STDIN_ADB_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
if args[-1] == "cat /proc/device-tree/model":
    print("Rockchip RK3562 AIOT DEMO LP4X V10 Board")
    raise SystemExit(0)
if "test -f /data/xiaorui/.rk3562-es8388-profile" in args[-1]:
    raise SystemExit(0)
# 真 adb 会把 stdin 透传给板端命令；这里照做，用来暴露清单被读走的问题。
sys.stdin.read()
raise SystemExit(0)
''')
    adb.chmod(0o755)
    log = tmp_path / "stdin-adb.jsonl"
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("STDIN_ADB_LOG", str(log))

    backup = tmp_path / "backup-many"
    files = backup / "files"
    files.mkdir(parents=True)
    entries = [
        ("PRESENT", "/etc/asound.conf", "etc__asound.conf"),
        ("PRESENT", "/etc/init.d/S98xiaorui", "etc__init.d__S98xiaorui"),
        ("PRESENT", "/data/mixer-es8388.sh", "data__mixer-es8388.sh"),
        ("ABSENT", "/data/xiaorui/.rk3562-es8388-state", "data__xiaorui__.rk3562-es8388-state"),
    ]
    for _state, _remote, local_name in entries:
        (files / local_name).write_text("old content\n")
    (backup / "manifest").write_text(
        "".join(f"{state}\t{remote}\t{local}\n" for state, remote, local in entries)
    )

    result = subprocess.run(
        ["bash", str(ES8388 / "rollback.sh"), "fake-serial", str(backup)],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    commands = adb_commands(log)
    pushed = {command[-1] for command in commands if "push" in command}
    for _state, remote, _local in entries:
        if _state == "PRESENT":
            assert remote in pushed, f"清单条目未回滚: {remote}"
    shell_commands = [command[-1] for command in commands if command and "shell" in command]
    assert any("rm -f '/data/xiaorui/.rk3562-es8388-state'" in c for c in shell_commands)


def test_rollback_restores_file_mode_so_scripts_stay_executable(tmp_path, monkeypatch):
    """adb pull/push 不保留执行位；回滚必须按备份时的 mode 还原。

    回归: 备份只存内容不存权限，回滚 push 回去的 start-board-stack.sh 变成
    不可执行，下次 deploy.sh 的 "已安装 profile" 判定 (test -x) 就会失败，
    板子被卡在"后端未完整安装，拒绝覆盖"。
    """
    log = install_guard_adb(tmp_path, monkeypatch, profile_marked=True)
    backup = tmp_path / "backup-mode"
    files = backup / "files"
    files.mkdir(parents=True)
    (files / "data__xiaorui-gateway__start-board-stack.sh").write_text("#!/bin/sh\n")
    (backup / "manifest").write_text(
        "PRESENT\t/data/xiaorui-gateway/start-board-stack.sh\t"
        "data__xiaorui-gateway__start-board-stack.sh\t755\n"
    )
    result = subprocess.run(
        ["bash", str(ES8388 / "rollback.sh"), "fake-serial", str(backup)],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    shell_commands = [
        command[-1] for command in adb_commands(log) if command and "shell" in command
    ]
    assert any(
        "chmod 755 '/data/xiaorui-gateway/start-board-stack.sh'" in command
        for command in shell_commands
    ), f"回滚未还原执行位: {shell_commands}"


def test_rollback_preserves_created_data_unless_explicitly_requested(tmp_path, monkeypatch):
    log = install_guard_adb(tmp_path, monkeypatch, profile_marked=True)
    backup = tmp_path / "backup-with-new-root"
    backup.mkdir()
    (backup / "manifest").write_text(
        "DIR_ABSENT\t/data/xiaorui-server\t-\n"
        "DIR_PRESENT\t/data/xiaorui-gateway\t-\n"
    )
    result = subprocess.run(
        ["bash", str(ES8388 / "rollback.sh"), "fake-serial", str(backup)],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    shell_commands = [
        command[-1] for command in commands if command and "shell" in command
    ]
    assert not any("rm -rf" in command for command in shell_commands)
    assert "保留新建目录" in result.stdout

    result = subprocess.run(
        ["bash", str(ES8388 / "rollback.sh"), "fake-serial", str(backup), "--remove-created-data"],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    shell_commands = [
        command[-1] for command in commands if command and "shell" in command
    ]
    assert any(command == "rm -rf '/data/xiaorui-server'" for command in shell_commands)
    assert not any("rm -rf '/data/xiaorui-gateway'" in command for command in shell_commands)


def run_npu_evidence(tmp_path, runtime, loads, response):
    runtime_path = tmp_path / "rkllm.log"
    loads_path = tmp_path / "npu-load.log"
    response_path = tmp_path / "response.txt"
    runtime_path.write_text(runtime)
    loads_path.write_text(loads)
    response_path.write_text(response)
    return subprocess.run(
        [
            sys.executable,
            str(ES8388 / "check-npu-evidence.py"),
            "--runtime-log", str(runtime_path),
            "--npu-load-log", str(loads_path),
            "--response", str(response_path),
        ],
        capture_output=True, text=True, check=False, timeout=20,
    )


def test_npu_evidence_requires_rk3562_w8a8_and_nonzero_load(tmp_path):
    result = run_npu_evidence(
        tmp_path,
        "platform: RK3562\ntarget_platform: RK3562 model_dtype: W8A8 npu_core_num: 1\n",
        "NPU load: 0%\nNPU load: 40%\n",
        "现在是晚上八点。\n",
    )
    assert result.returncode == 0, result.stderr
    assert "RK3562" in result.stdout and "40%" in result.stdout


@pytest.mark.parametrize(
    ("runtime", "loads", "response", "expected"),
    [
        ("target_platform: RK3576 model_dtype: W4A16", "NPU load: 40%\n", "hi", "RK3562"),
        ("target_platform: RK3562 model_dtype: W8A8", "NPU load: 0%\n", "hi", "nonzero"),
        ("target_platform: RK3562 model_dtype: W8A8", "NPU load: 40%\n", "", "no text"),
    ],
)
def test_npu_evidence_rejects_incomplete_proof(tmp_path, runtime, loads, response, expected):
    result = run_npu_evidence(tmp_path, runtime, loads, response)
    assert result.returncode != 0
    assert expected in result.stderr


def install_stage_adb(tmp_path, monkeypatch, *, model="RK3562 AIOT DEMO", python_installed=False):
    bindir = tmp_path / "stage-bin"
    bindir.mkdir()
    adb = bindir / "adb"
    log = tmp_path / "stage-adb.jsonl"
    adb.write_text(f"#!{sys.executable}\n" + r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STAGE_ADB_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
if args[-1] == "get-state":
    print("device")
    raise SystemExit(0)
if args[-1] == "cat /proc/device-tree/model":
    print(os.environ["STAGE_MODEL"])
    raise SystemExit(0)
if "test -x /data/miniforge/bin/python3.14" in (args[-1] if args else ""):
    raise SystemExit(0 if os.environ["STAGE_PYTHON"] == "1" else 1)
if "test -e /data/miniforge" in (args[-1] if args else ""):
    raise SystemExit(0 if os.environ["STAGE_MINIFORGE_DIR"] == "1" else 1)
if "/data/miniforge/bin/python3.14 -V" in (args[-1] if args else ""):
    print("Python 3.14.7")
    raise SystemExit(0)
if "ctypes.CDLL" in (args[-1] if args else ""):
    print("loaded")
    raise SystemExit(0)
if "push" in args:
    raise SystemExit(0 if Path(args[args.index("push") + 1]).exists() else 1)
raise SystemExit(0)
''')
    adb.write_text(adb.read_text().replace("import json, os, sys", "import json, os, sys\nfrom pathlib import Path"))
    adb.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("STAGE_ADB_LOG", str(log))
    monkeypatch.setenv("STAGE_MODEL", model)
    monkeypatch.setenv("STAGE_PYTHON", "1" if python_installed else "0")
    monkeypatch.setenv("STAGE_MINIFORGE_DIR", "0")
    libgomp = tmp_path / "libgomp.so.1"
    libgomp.write_bytes(b"fake")
    monkeypatch.setenv("XR_LIBGOMP", str(libgomp))
    return log


def test_stage_runtime_requires_serial_before_adb(tmp_path, monkeypatch):
    log = install_stage_adb(tmp_path, monkeypatch)
    result = subprocess.run(
        ["bash", str(ES8388 / "stage-runtime.sh")],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 2
    assert not log.exists()


def test_stage_runtime_rejects_wrong_board_before_push(tmp_path, monkeypatch):
    log = install_stage_adb(tmp_path, monkeypatch, model="RK3576 EVB")
    result = subprocess.run(
        ["bash", str(ES8388 / "stage-runtime.sh"), "fake-serial"],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 1
    assert "不是 RK3562" in result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any("push" in command for command in commands)


def test_stage_runtime_pushes_only_runtime_prerequisites_before_smoke(tmp_path, monkeypatch):
    log = install_stage_adb(tmp_path, monkeypatch, python_installed=False)
    result = subprocess.run(
        ["bash", str(ES8388 / "stage-runtime.sh"), "fake-serial"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    pushed = " ".join(" ".join(command) for command in commands if "push" in command)
    assert "miniforge.sh" in pushed
    assert "librkllmrt.so" in pushed
    assert "llm_rkllm.py" in pushed
    assert "qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm" in pushed
    assert "libgomp.so.1" in pushed
    assert not any("xiaorui_live" in " ".join(command) for command in commands)
    assert not any("server/app.py" in " ".join(command) for command in commands)


# --------------------------------------------------------------------------
# Task 5: 部署脚本 / 开机脚本 / ALSA 配置
# --------------------------------------------------------------------------


def test_deploy_requires_explicit_serial():
    result = subprocess.run(
        ["bash", str(ES8388 / "deploy.sh")],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 2
    assert "必须显式指定 serial" in result.stderr


def test_deploy_script_never_touches_vqe_or_rk809_profile():
    script = strip_comments((ES8388 / "deploy.sh").read_text())
    for forbidden in ("deploy-vqe.sh", "rkaudio_vqe", "xr_vqe", "init-S98xiaorui" + "  # RK809"):
        assert forbidden not in script, forbidden
    # 只用自己的 profile 文件
    assert 'ports/rk3562-es8388' not in script  # 路径用 $DIR, 不写死
    assert '"$DIR/asound.conf"' in script


def test_deploy_script_backs_up_before_it_overwrites():
    script = (ES8388 / "deploy.sh").read_text()
    backup_at = script.index("备份板端将被替换的文件")
    first_push = script.index("推送前端二进制")
    assert backup_at < first_push
    assert "/etc/asound.conf" in script and "backup" in script.lower()


def test_deploy_script_runs_runtime_check_before_installing():
    script = (ES8388 / "deploy.sh").read_text()
    assert script.index("check-runtime.sh") < script.index("推送前端二进制")


def test_deploy_script_gates_gateway_install_on_skip_init():
    """板内网关安装必须带上 XR_SKIP_INIT_INSTALL, 否则会装旧板开机脚本。"""
    script = (ES8388 / "deploy.sh").read_text()
    assert "XR_SKIP_INIT_INSTALL=1" in script


def test_deploy_forwards_resolved_runtime_asset_paths():
    script = (ES8388 / "deploy.sh").read_text()
    assert 'XR_MINIFORGE="$MINIFORGE"' in script
    assert 'XR_GATEWAY_WHEELS="$WHEELS"' in script
    assert 'XR_RKLLM_MODEL="$RKLLM_MODEL"' in script


def test_init_script_uses_es8388_controls_not_rk809():
    script = strip_comments((ES8388 / "init-S98xiaorui").read_text())
    for forbidden in ("Playback Path", "Capture MIC Path", "ADC Capture Volume", "MIC Boost Gain"):
        assert forbidden not in script, forbidden
    assert "mixer-es8388.sh" in script
    assert "scaling_governor" in script
    assert "ff300000.npu" in script


def test_init_script_reports_ddr_as_fixed_without_dmc_node():
    """本板没有 DMC devfreq 节点 -> DDR 频率固定, 脚本要如实这么写。

    回归: 旧文案写的是"无法定频", 容易读成"DDR 在降频/不受控";
    实际是没有可改频率的接口, 频率就是固化的。
    """
    script = (ES8388 / "init-S98xiaorui").read_text()
    # 不能有写 DDR 频率的动作, 也不能出现 devfreq/dmc 节点路径
    assert "devfreq/dmc" not in script
    assert "dmc/devfreq" not in script
    # 注释里要说明"没有节点 = 固定"
    assert "没有 DMC devfreq 节点" in script
    assert "固定" in script


def test_init_script_probe_degrades_instead_of_stalling():
    """探针文件缺失时不能空等 60 秒: 必须有板内内联兜底。"""
    script = (ES8388 / "init-S98xiaorui").read_text()
    code = strip_comments(script)
    assert "/data/xiaorui-gateway/probe-port.py" in code
    assert "connect_ex" in code
    assert "if [ -f /data/xiaorui-gateway/probe-port.py ]" in code






def test_asound_conf_is_es8388_only():
    conf = strip_comments((ES8388 / "asound.conf").read_text())
    assert "hw:rockchipes8388,0" in conf
    assert "pcm.xr_mic_16k" in conf and "pcm.xr_spk" in conf
    assert "rkaudio_vqe" not in conf
    assert "dsnoop" not in conf


def test_asound_conf_mic_slave_uses_native_capture_rate():
    """ES8388 采集硬件只支持 48kHz; 新 profile 直接用前端 FIR ÷3。"""
    conf = strip_comments((ES8388 / "asound.conf").read_text())
    mic_rate = conf.split("pcm.xr_mic_rate")[1].split("}")[0]
    assert "rate 48000" in mic_rate
    # 16k 采集交给前端内置 72-tap FIR; asound.conf 只保留兼容 fallback。
    env = effective_env((ES8388 / "frontend.env.example").read_text())
    assert "XR_MIC_MODE=es8388" in env
    assert "XR_ES8388_CHANNELS=2" in env
    # 播放仍是 48k/2ch
    spk = conf.split("pcm.xr_spk")[1].split("}")[0]
    assert "rate 48000" in spk and "channels 2" in spk


def test_board_stack_provides_libgomp_path():
    """librkllmrt.so 依赖 libgomp.so.1, 板端镜像不带, 要把目录挂进 LD_LIBRARY_PATH。"""
    script = (PORT / "start-board-stack.sh").read_text()
    assert 'LD_LIBRARY_PATH="$SRV/lib' in script


# --------------------------------------------------------------------------
# Task: rkaudio VQE 20261110 (1 mic + 1 ref)
# --------------------------------------------------------------------------


def test_vqe_deploy_requires_explicit_package():
    """插件包路径必须显式给, 不能自动挑目录。"""
    result = subprocess.run(
        ["bash", str(ES8388 / "deploy-vqe.sh"), "fake-serial"],
        capture_output=True, text=True, check=False, timeout=20,
        env={k: v for k, v in os.environ.items() if k != "XR_VQE_PKG"},
    )
    assert result.returncode != 0
    assert "XR_VQE_PKG" in result.stderr


def test_vqe_config_is_1mic1ref():
    """官方 1mic1ref 配置: AEC 开, 阵列麦数 1。"""
    import json as _json
    cfg = _json.loads((ES8388 / "config_aivqe_new1mic1ref.json").read_text())
    s = cfg["skv_configs"]
    assert s["aec"]["status"] == "enable"
    # 官方 1mic1ref 配置: BF 段改成 1 个阵列麦; AEC 段保持 2 (库内派生)
    assert s["bf"]["num_array_mic"] == 1
    # 唤醒/KWS 交给前端 sherpa, VQE 包自带的 wakeup/unkws/command 必须关
    for key in ("wakeup", "unkws", "command"):
        assert s[key]["status"] == "disable", key
    # 2mic 以下不需要 license; 不能偷偷依赖授权模块
    assert s["aiec"]["status"] == "disable"
    assert s["aibf"]["status"] == "disable"


def test_vqe_alsa_config_uses_2ch_slave_and_1_ref():
    """硬件只有 2ch: ch0=mic, ch1=DLP 播放参考; 不能再写 4ch。"""
    text = (ES8388 / "asound-vqe.conf").read_text()
    conf = strip_comments(text)
    assert "channels 2" in conf
    assert "ref_channels 1" in conf
    assert "out_channels 1" in conf
    assert "hw:rockchipes8388,0" in conf  # 用卡名而非数字卡号
    # 48k->16k 必须带限重采样(plug 自带的是线性插值, 会把高频折回语音带)
    assert 'converter "samplerate_best"' in conf


def test_vqe_frontend_env_uses_vqe_mic():
    env = effective_env((ES8388 / "frontend.env.vqe.example").read_text())
    assert "XR_MIC=xr_vqe_16k" in env
    assert "XR_MIC_MODE=usb" in env
    assert "XR_MIC_MODE=es8388" not in env
    assert "XR_ES8388_CHANNELS=2" not in env


def test_vqe_patch_rejects_bad_package_config(tmp_path):
    """坏插件包(aec 关闭/阵列麦数错)必须被拒, 不能部署出一个静默失效的 AEC。"""
    import importlib.util
    import json as _json

    spec = importlib.util.spec_from_file_location(
        "patch_vqe", ES8388 / "patch-vqe-config.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    good = _json.loads((ES8388 / "config_aivqe_new1mic1ref.json").read_text())
    src = tmp_path / "good.json"
    src.write_text(_json.dumps(good))
    dst = tmp_path / "out.json"
    patched = mod.patch(str(src), str(dst))
    assert patched["skv_configs"]["wakeup"]["status"] == "disable"
    assert patched["skv_configs"]["aec"]["status"] == "enable"

    for bad_key, bad_val in (("aec", "disable"), ("bf", 4)):
        broken = _json.loads((ES8388 / "config_aivqe_new1mic1ref.json").read_text())
        if bad_key == "aec":
            broken["skv_configs"]["aec"]["status"] = bad_val
        else:
            broken["skv_configs"]["bf"]["num_array_mic"] = bad_val
        bad = tmp_path / f"bad-{bad_key}.json"
        bad.write_text(_json.dumps(broken))
        try:
            mod.patch(str(bad), str(dst))
        except ValueError as error:
            assert "VQE 配置不合格" in str(error)
        else:
            raise AssertionError(f"坏配置({bad_key})竟然通过了")


def test_deploy_vqe_checks_new_log_and_pid_after_restart():
    """部署后不能用旧日志/旧 PID 冒充本次 VQE 链就绪。"""
    script = (ES8388 / "deploy-vqe.sh").read_text()
    assert "MARK=$(" in script and "wc -l < /data/xiaorui/frontend.log" in script
    assert "tail -n +$((MARK+1))" in script
    assert "OLD_PID" in script and "NEW_PID" in script
    assert '[ "$NEW_PID" = "$OLD_PID" ]' in script
