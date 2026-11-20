import configparser
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
from unittest.mock import AsyncMock, patch
import zipfile

import pytest
import yaml


REPO = Path(__file__).resolve().parents[1]
PORT = REPO / "ports" / "rk3562"


def load_onboard():
    spec = importlib.util.spec_from_file_location("rk3562_ha_onboard", PORT / "ha-onboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("password", ["", "safe'$(touch /tmp/not-executed)\n密码"])
def test_ha_password_from_stdin(tmp_path, monkeypatch, password):
    onboard = load_onboard()
    token_path = tmp_path / "ha_token.txt"
    monkeypatch.setattr(onboard, "TOKEN_FILE", str(token_path))
    monkeypatch.setattr(sys, "argv", ["ha-onboard.py", "--password-stdin"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(password))
    responses = [
        (200, [{"step": "user", "done": False}]),
        (200, {"auth_code": "code"}),
        (200, {"access_token": "access"}),
        (200, [{"entity_id": "light.test_light"}]),
    ]
    with patch.object(onboard, "http", side_effect=responses) as request:
        with patch.object(onboard, "get_llat", new_callable=AsyncMock, return_value="llat"):
            if not password:
                with pytest.raises(SystemExit):
                    onboard.main()
                request.assert_not_called()
                assert not token_path.exists()
            else:
                onboard.main()
                assert request.call_args_list[1].kwargs["data"]["password"] == password
                assert token_path.read_text() == "llat"
                assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
                assert list(tmp_path.iterdir()) == [token_path]


def test_ha_password_flag_is_required(monkeypatch):
    onboard = load_onboard()
    monkeypatch.setattr(sys, "argv", ["ha-onboard.py"])
    with patch.object(onboard, "http") as request:
        with pytest.raises(SystemExit):
            onboard.main()
        request.assert_not_called()


def test_ha_token_replace_failure_keeps_existing_token(tmp_path, monkeypatch):
    onboard = load_onboard()
    token_path = tmp_path / "ha_token.txt"
    token_path.write_text("existing-token")
    monkeypatch.setattr(onboard, "TOKEN_FILE", str(token_path))
    monkeypatch.setattr(sys, "argv", ["ha-onboard.py", "--password-stdin"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("explicit-password"))
    responses = [
        (200, [{"step": "user", "done": False}]),
        (200, {"auth_code": "code"}),
        (200, {"access_token": "access"}),
    ]
    with patch.object(onboard, "http", side_effect=responses):
        with patch.object(onboard, "get_llat", new_callable=AsyncMock, return_value="new-token"):
            with patch.object(onboard.os, "replace", side_effect=OSError("replace failed")):
                with pytest.raises(OSError, match="replace failed"):
                    onboard.main()
    assert token_path.read_text() == "existing-token"
    assert list(tmp_path.iterdir()) == [token_path]


def run_config_block(script, config):
    source = (PORT / script).read_text()
    match = re.search(r"<<'PY'\n(.*?)\nPY\"", source, re.DOTALL)
    assert match is not None
    return subprocess.run(
        [sys.executable, "-c", match.group(1), str(config)],
        capture_output=True, text=True, check=False,
    )


def write_config(path, offline=True, **settings):
    selected = {"ASR": "LocalSenseVoice", "LLM": "LocalQwen"} if offline else {
        "ASR": "Qwen3ASRFlash", "LLM": "AliLLM",
    }
    path.write_text(yaml.safe_dump({"selected_module": selected, **settings}))


def test_cloud_snapshot_refreshes_and_preserves_latest_offline_config(tmp_path):
    config = tmp_path / ".config.yaml"
    backup = Path(str(config) + ".board-local-bak")
    write_config(config, speed=1.0)
    assert run_config_block("deploy-cloud-gateway.sh", config).returncode == 0
    write_config(config, speed=1.1, wakeup_words=["小瑞小瑞"])
    expected = config.read_bytes()
    assert run_config_block("deploy-cloud-gateway.sh", config).returncode == 0
    assert backup.read_bytes() == expected
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    write_config(config, offline=False)
    assert run_config_block("deploy-cloud-gateway.sh", config).returncode == 0
    assert backup.read_bytes() == expected
    assert run_config_block("rollback-cloud-gateway.sh", config).returncode == 0
    assert config.read_bytes() == expected
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".offline-*"))
    assert not list(tmp_path.glob(".restore-*"))


@pytest.mark.parametrize("invalid", ["missing", "cloud", "malformed", "unknown"])
def test_cloud_rejects_invalid_offline_snapshot(tmp_path, invalid):
    config = tmp_path / ".config.yaml"
    backup = Path(str(config) + ".board-local-bak")
    write_config(config, offline=False)
    if invalid == "cloud":
        write_config(backup, offline=False)
    elif invalid == "malformed":
        backup.write_text("[]")
    elif invalid == "unknown":
        write_config(backup)
        config.write_text("selected_module: {ASR: unknown, LLM: unknown}")
    expected = config.read_bytes()
    assert run_config_block("deploy-cloud-gateway.sh", config).returncode != 0
    assert config.read_bytes() == expected
    if invalid != "unknown":
        assert run_config_block("rollback-cloud-gateway.sh", config).returncode != 0
        assert config.read_bytes() == expected


@pytest.fixture
def fake_deployment(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    port = repo / "ports" / "rk3562"
    port.mkdir(parents=True)
    for filename in (
        "deploy-board-gateway.sh", "deploy-and-run.sh", "deploy-ha.sh",
        "start-board-stack.sh", "init-S98xiaorui", "gateway-board.yaml",
        "frontend.env.example", "start-frontend.sh", "board-time.sh",
        "ha-onboard.py", "start-ha.sh", "ha-configuration.yaml",
        "render-config.py", "gateway-defaults.yaml", "prepare-wheels.py",
        "requirements-ha.lock",
        # 部署脚本会推它并依赖它存在(2GB 板的内存兜底: zram 768M zstd)
        "setup-zram.sh",
    ):
        shutil.copy(PORT / filename, port / filename)
    for directory in (
        "board-server/wheels314", "board-server/pkg", "models/sherpa-onnx-sense-voice-zh",
        "tts-matcha/matcha-icefall-zh-baker", "vendor/sherpa-onnx/lib",
    ):
        (repo / directory).mkdir(parents=True)
    for filename in (
        "board-server/librkllmrt.so", "board-server/llm_rkllm.py",
        "board-server/probe-port.py",
        "models/qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm",
        "models/snakers4_silero-vad/src/silero_vad/data/silero_vad.onnx",
        "board/xiaorui_live", "keywords.txt",
        "vendor/sherpa-onnx/lib/libsherpa-onnx-c-api.so",
        "vendor/sherpa-onnx/lib/libonnxruntime.so",
        "models/model-kws/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        "models/model-kws/decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        "models/model-kws/joiner-epoch-12-avg-2-chunk-16-left-64.onnx",
        "models/model-kws/tokens.txt",
        "board-server/cues/xr_ack24k.wav", "board-server/cues/xr_hmm24k.wav",
        "board-server/cues/xr_fail24k.wav", "board-server/cues/xr_volack24k.wav",
    ):
        path = repo / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    overlay = repo / "board-server/config/wakeup_words.xiaorui.yaml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text('wakeup_words: ["小瑞小瑞"]\n')
    (port / "requirements-board.lock").write_text("demo==1.0\n")
    with zipfile.ZipFile(repo / "board-server/wheels314/demo-1.0-py3-none-any.whl", "w") as wheel:
        wheel.writestr("demo-1.0.dist-info/METADATA", "Name: demo\nVersion: 1.0\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    adb = binaries / "adb"
    adb.write_text(f"#!{sys.executable}\n" + '''import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
with open(os.environ["ADB_LOG"], "a") as output:
    output.write(json.dumps(arguments) + "\\n")
if "push" in arguments:
    source = Path(arguments[arguments.index("push") + 1])
    sys.exit(0 if source.exists() else 1)
command = arguments[-1]
if command == "get-state":
    print("device")
elif "cat /proc/device-tree/model" in command:
    print("RK3562 EVB2")
elif "wc -l" in command and "frontend.log" in command:
    print("123")
elif "amixer" in command and "cget" in command:
    print("numid=52,iface=MIXER,name='Left Channel Capture Volume'")
    print("  ; type=INTEGER,access=rw---R--,values=1,min=0,max=8,step=0")
    if "Output" in command:
        # 区间控件: amixer 打印 ": values=15,15"
        print("  : values=15,15")
    elif "Capture Volume" in command:
        # 单值控件: ": values=8"
        print("  : values=8")
    else:
        print("  : values=27")
elif "amixer" in command and "sget" in command:
    print("Simple mixer control 'Main Mic',0")
    print("  Mono: Playback [on]")
elif "cat /proc/asound/cards" in command:
    print(" 0 [rockchipes8388 ]: rockchip-es8388 - rockchip-es8388")
elif "cat /data/xiaorui/.rk3562-es8388-state" in command:
    print(os.environ.get("PROFILE_STATE", ""))
elif "df -Pk /data" in command:
    available = os.environ.get("AVAILABLE_KB", "9000000")
    print("Filesystem 1024-blocks Used Available Capacity Mounted on")
    print(f"/dev/root 10000000 1000000 {available} 10% /data")
elif "test -f" in command and os.environ.get("FAIL_BOARD_FILE") and \
        os.environ["FAIL_BOARD_FILE"] in command:
    sys.exit(1)
elif "pgrep -x xiaorui_live" in command:
    if os.environ.get("FRONTEND_ALIVE", "1") != "1":
        sys.exit(1)
    # 每次调用给一个不同的 PID: 部署要求"新前端 PID 与旧的不同"。
    counter_path = os.environ.get("PGREP_COUNTER")
    pid = 1000
    if counter_path and os.path.exists(counter_path):
        pid = int(Path(counter_path).read_text().strip() or 1000)
    if counter_path:
        Path(counter_path).write_text(str(pid + 1))
    print(pid)
elif "snd_config_search" in command:
    sys.exit(0 if os.environ.get("ALSA_CONFIG_VALID", "1") == "1" else 1)
elif command == "/data/miniforge/bin/python3 -V":
    print("Python 3.14")
elif "grep -c" in command:
    print("1")
elif "connect_ex" in command or "/data/xiaorui-gateway/probe-port.py" in command:
    port = command.rsplit(None, 1)[-1].strip(chr(39) + chr(34))
    sys.exit(1 if port == os.environ.get("FAIL_PORT") else 0)
elif "--password-stdin" in command:
    Path(os.environ["PASSWORD_CAPTURE"]).write_text(sys.stdin.read())
''')
    adb.chmod(0o755)
    sleep = binaries / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    log = tmp_path / "adb.log"
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("ADB_LOG", str(log))
    monkeypatch.setenv("PGREP_COUNTER", str(tmp_path / "pgrep-counter"))
    monkeypatch.setenv("XR_MINIFORGE", str(repo / "board-server/miniforge.sh"))
    monkeypatch.setenv("PASSWORD_CAPTURE", str(tmp_path / "password"))
    return port, log


def run_deployment(port, filename, *arguments):
    return subprocess.run(
        ["bash", str(port / filename), *arguments],
        capture_output=True, text=True, check=False, timeout=30,
    )


@pytest.mark.parametrize("failed_port", ["", "8001", "8004", "8112"])
def test_gateway_deployment_requires_every_port(fake_deployment, monkeypatch, failed_port):
    port, log = fake_deployment
    monkeypatch.setenv("FAIL_PORT", failed_port)
    result = run_deployment(port, "deploy-board-gateway.sh", "fake-board")
    assert (result.returncode == 0) == (not failed_port), result.stderr
    if failed_port:
        assert f"端口 {failed_port} 未就绪" in result.stderr
    assert "timeout 20" not in log.read_text()


def test_shared_stack_uses_neutral_probe_path():
    """共享脚本不能依赖某个板型的文件名(旧板没有 es8388 探针)。"""
    source = (PORT / "start-board-stack.sh").read_text()
    assert "PROBE=/data/xiaorui-gateway/probe-port.py" in source
    assert "xr-es8388-probe" not in source


def test_gateway_pushes_probe_before_starting_stack(fake_deployment):
    port, log = fake_deployment
    result = run_deployment(port, "deploy-board-gateway.sh", "fake-board")
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    probe_push = next(
        i for i, command in enumerate(commands)
        if "push" in command and "probe-port.py" in " ".join(command)
    )
    assert commands[probe_push][-1] == "/data/xiaorui-gateway/probe-port.py"
    stack_start = next(
        i for i, command in enumerate(commands)
        if "shell" in command and "setsid sh /data/xiaorui-gateway/start-board-stack.sh" in command[-1]
    )
    assert probe_push < stack_start


@pytest.mark.parametrize("alsa_config_valid", [False, True])
def test_frontend_preserves_alsa_config(fake_deployment, monkeypatch, alsa_config_valid):
    port, log = fake_deployment
    monkeypatch.setenv("ALSA_CONFIG_VALID", "1" if alsa_config_valid else "0")
    result = run_deployment(port, "deploy-and-run.sh", "fake-board")
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    pushes = [command for command in commands if "push" in command]
    assert not any("/etc/asound.conf" in command for command in pushes)
    assert (result.returncode == 0) == alsa_config_valid, result.stderr
    if not alsa_config_valid:
        assert not pushes
    else:
        assert any("sh /etc/init.d/S98xiaorui start" in command[-1] for command in commands)


def test_frontend_preflight_accepts_alsa_devices_without_hints(tmp_path, monkeypatch):
    config = tmp_path / "asound.conf"
    config.write_text('pcm.xr_vqe_16k { type null }\npcm.xr_spk { type null }\n')
    monkeypatch.setenv("ALSA_CONFIG_PATH", str(config))
    result = run_config_block("deploy-and-run.sh", config)
    assert result.returncode == 0, result.stderr
    config.write_text('pcm.xr_vqe_16k { type null }\n')
    result = run_config_block("deploy-and-run.sh", config)
    assert result.returncode != 0
    assert "xr_spk" in result.stderr


@pytest.mark.parametrize("http_ready", [False, True])
def test_running_gateway_requires_http_port(tmp_path, monkeypatch, http_ready):
    source = (PORT / "start-board-stack.sh").read_text()
    source = source.replace("GW=/data/xiaorui-gateway", "GW=" + shlex.quote(str(tmp_path)))
    fake_python = tmp_path / "probe-python"
    fake_python.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "port = sys.argv[-1]\n"
        "sys.exit(1 if port == os.environ.get('FAIL_PORT') else 0)\n"
    )
    fake_python.chmod(0o755)
    fake_probe = tmp_path / "probe-port.py"
    fake_probe.write_text("#!/bin/sh\nexit 0\n")
    source = source.replace(
        "PY=/data/gwvenv/bin/python",
        "PY=" + shlex.quote(str(fake_python)),
    )
    source = source.replace(
        "PROBE=/data/xiaorui-gateway/probe-port.py",
        "PROBE=" + shlex.quote(str(fake_probe)),
    )
    monkeypatch.setenv("FAIL_PORT", "" if http_ready else "8004")
    result = subprocess.run(["bash", "-c", source], capture_output=True, text=True, check=False)
    assert (result.returncode == 0) == http_ready
    if not http_ready:
        assert "8004 未就绪" in result.stdout


def test_stack_start_falls_back_to_inline_probe_when_probe_file_missing(
    tmp_path, monkeypatch
):
    """旧板路径(deploy-cloud-gateway/rollback-cloud-gateway)不推探针文件, 不能因此起不来。

    回归: 探针文件被改成硬依赖后, 旧板的云网关切换/回滚会因为
    "缺端口探针"直接退出, 板内栈起不来。
    """
    source = (PORT / "start-board-stack.sh").read_text()
    source = source.replace("GW=/data/xiaorui-gateway", "GW=" + shlex.quote(str(tmp_path)))
    fake_python = tmp_path / "probe-python"
    fake_python.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "port = sys.argv[-1]\n"
        "sys.exit(1 if port == os.environ.get('FAIL_PORT') else 0)\n"
    )
    fake_python.chmod(0o755)
    source = source.replace(
        "PY=/data/gwvenv/bin/python",
        "PY=" + shlex.quote(str(fake_python)),
    )
    source = source.replace(
        "PROBE=/data/xiaorui-gateway/probe-port.py",
        "PROBE=" + shlex.quote(str(tmp_path / "missing-probe.py")),
    )
    monkeypatch.setenv("FAIL_PORT", "")
    result = subprocess.run(["bash", "-c", source], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "缺端口探针" not in result.stdout


def test_ha_deployment_requires_explicit_password(fake_deployment, monkeypatch):
    port, log = fake_deployment
    monkeypatch.delenv("XR_HA_ADMIN_PWD", raising=False)
    result = run_deployment(port, "deploy-ha.sh", "-s", "fake-board")
    assert result.returncode != 0
    assert not log.exists()


def test_ha_deployment_password_not_in_commands(fake_deployment, monkeypatch, tmp_path):
    port, log = fake_deployment
    password = "safe'$(do-not-execute) 密码"
    monkeypatch.setenv("XR_HA_ADMIN_PWD", password)
    result = run_deployment(port, "deploy-ha.sh", "-s", "fake-board")
    assert result.returncode == 0, result.stderr
    commands = log.read_text()
    assert password not in commands
    assert password not in result.stdout + result.stderr
    assert (tmp_path / "password").read_text() == password
    assert "S98xiaorui start" not in commands


@pytest.mark.parametrize("stage_success", [True, False])
def test_rock_sdk_is_staged_before_start_and_failure_stops_deployment(
        fake_deployment, monkeypatch, stage_success):
    port, log = fake_deployment
    monkeypatch.setenv("XR_ROCK_ASR_SDK", "/fake-sdk")
    (port / "deploy-rock.sh").write_text(
        '#!/bin/bash\nadb -s "$1" shell rock-stage-marker\n'
        + ('exit 0\n' if stage_success else 'exit 1\n'))
    result = run_deployment(port, "deploy-board-gateway.sh", "serial")
    records = [json.loads(line) for line in log.read_text().splitlines()]
    commands = [r[-1] for r in records if "shell" in r]
    stage = commands.index("rock-stage-marker")
    assert any("sh /data/setup-zram.sh" == c for c in commands[:stage])
    assert any("S98xiaorui stop" in c for c in commands[:stage])
    starts = [i for i, c in enumerate(commands)
              if "setsid sh /data/xiaorui-gateway/start-board-stack.sh" in c]
    if stage_success:
        assert result.returncode == 0, result.stderr
        assert starts and min(starts) > stage
    else:
        assert result.returncode != 0
        assert not starts


def test_s98_data_script_gates_use_f_not_x():
    """★ 2026-11-19: /data 上执行位重启即丢(实测两次, S98 静默跳过起栈,
    板子"开机了但全 DOWN")。/data 脚本门禁必须 -f(反正 sh 调用);
    /usr/sbin/crond 是二进制直接执行, 保留 -x。
    ★ 2026-11-20: 扩展覆盖 rk3562-es8388 版 —— 实机事故: 板上装了
    RK809 版 S98(es8388 卡的 mixer 设置全静默失败), 重启后 ES8388
    采集增益回默认 3/8, 指令电平低于 VAD 门限, "喊得醒问不应"。
    该版本自身也有 5 处 -x 漏网(含 mixer 门禁), 一并钉住。
    ★ 2026-11-20: S99xiaorui-ui 的 UI 可执行文件检查同样改 -f。
    """
    ports = [PORT / "init-S98xiaorui",
             PORT.parent / "rk3562-es8388" / "init-S98xiaorui"]
    for s98 in ports:
        text = s98.read_text(encoding="utf-8")
        assert "-x /data" not in text, \
            f"{s98.name}({s98.parent.name}): /data 脚本门禁应为 -f(执行位重启会丢)"
    # crond 断言只对 rk3562 版: es8388 版没有 HA/crond 段
    text = ports[0].read_text(encoding="utf-8")
    assert "[ -x /usr/sbin/crond ]" in text, "crond 是二进制, 应保持 -x"
    # UI 启动脚本: 二进制在 /data, 执行位重启会丢 -> 只判文件存在
    ui99 = (PORT / "S99xiaorui-ui").read_text(encoding="utf-8")
    assert '-x "$directory/xiaorui_ui"' not in ui99, \
        "S99xiaorui-ui 的 UI 二进制检查应为 -f(/data 执行位重启会丢)"


def test_es8388_weston_pins_native_portrait_mode():
    """★ 2026-11-20 实机: 面板原生 1080x1920 竖屏, 出厂 weston.ini 没有
    [output] 段时 weston 自己挑 1920x1080 -> UI(LVGL 写死 1080x1920)
    收到尺寸不符直接退出 -> 界面全黑, 而语音链路完全正常(极难定位)。
    部署必须带 mode=1080x1920, 且不能用 transform 绕(transform=90 在
    本板 weston 16 被判 Invalid)。

    ★ 用 configparser 解析而不是子串匹配: 子串匹配会被注释里的同名字符串
      骗过 —— 评审实测三个变异(删 mode 行 / 把 mode 挪进 [core] / 删掉
      deploy 里的 push 行)都能通过子串版断言, 等于制造虚假信心。
    """
    ini_path = PORT.parent / "rk3562-es8388" / "weston.ini"
    parser = configparser.ConfigParser()
    parser.read_string(ini_path.read_text(encoding="utf-8"))
    assert "output" in parser, "weston.ini 缺少 [output] 段(会挑到横置 mode)"
    assert parser["output"].get("name") == "DSI-1", \
        f"[output] name 应为 DSI-1, 实际 {parser['output'].get('name')!r}"
    assert parser["output"].get("mode") == "1080x1920", \
        f"[output] mode 应为 1080x1920(面板原生竖屏), 实际 {parser['output'].get('mode')!r}"
    # 生效行里不许有 transform(本板 weston 16 判 Invalid, 且不交换逻辑几何)
    active = [line.strip() for line in ini_path.read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.strip().startswith("#")]
    assert not any(line.startswith("transform") for line in active), \
        "不要用 transform 旋转: 本板 weston 16 不认 transform=90"
    # deploy 必须真的执行 push(注释里出现同样字符串不算)
    deploy_lines = (PORT.parent / "rk3562-es8388" / "deploy.sh").read_text(
        encoding="utf-8").splitlines()
    pushed = [l for l in deploy_lines
              if l.strip().startswith("$ADB push") and "weston.ini" in l]
    assert pushed, "deploy.sh 必须有实际执行的 $ADB push ... weston.ini"
    assert any(l.strip().startswith("$ADB push") and "S99xiaorui-ui" in l
               for l in deploy_lines), "deploy.sh 必须有实际执行的 S99xiaorui-ui push"
    # 推完必须让 weston/UI 真正生效: 重启命令不能只在注释里
    assert any("S49weston" in l and not l.strip().startswith("#")
               for l in deploy_lines), "deploy.sh 推完 weston.ini 必须重启 weston"
    assert any("S99xiaorui-ui" in l and "restart" in l and not l.strip().startswith("#")
               for l in deploy_lines), "deploy.sh 推完 UI 必须重启 UI"


def test_ui_start_script_uses_f_not_x_for_data_binary():
    """★ 2026-11-20: /data 执行位重启即丢(第三次踩)。UI 二进制检查必须
    是 -f, 且要**正向**断言(只断言"没有 -x"的话, 整个检查被删掉也过)。
    """
    ui99 = (PORT / "S99xiaorui-ui").read_text(encoding="utf-8")
    assert '-f "$directory/xiaorui_ui"' in ui99, \
        "S99xiaorui-ui 必须正向断言 UI 二进制用 -f 检查(/data 执行位重启会丢)"
    assert '-x "$directory/xiaorui_ui"' not in ui99, \
        "UI 二进制检查不能是 -x(/data 执行位重启会丢)"


def test_wire_lead_fits_alsa_card_buffer():
    """★ 2026-11-19: 声卡缓冲(period 数 × period 样本)必须 >= tts_lead_ms。

    lead 超过卡缓冲时, 多出的提前量堵在前端环里进不了卡, 句间隙保护
    打折。此约束横跨 C 常量与 yaml 配置, 靠静态测试防漂移。
    """
    c_src = (REPO / "board" / "audio_player.c").read_text(encoding="utf-8")
    m_periods = re.search(r"a_set_periods\(spk, hw, (\d+), 0\)", c_src)
    assert m_periods, "audio_player.c 里找不到 a_set_periods 调用"
    m_period = re.search(r"spk_period = SPK_PERIOD_SAMPLES;", c_src)
    assert m_period, "spk_period 应与 SPK_PERIOD_SAMPLES 同源"
    m_samples = re.search(r"#define SPK_PERIOD_SAMPLES (\d+)", c_src)
    assert m_samples, "找不到 SPK_PERIOD_SAMPLES 定义"
    card_ms = (int(m_periods.group(1)) * int(m_samples.group(1))
               / 24000 * 1000)

    cfg = yaml.safe_load((PORT / "gateway-board.yaml").read_text(encoding="utf-8"))
    lead_ms = int(cfg.get("tts_lead_ms", 0) or 0)
    assert card_ms >= lead_ms, (
        f"卡缓冲 {card_ms:.0f}ms < tts_lead_ms {lead_ms}ms: "
        f"lead 超出卡容量, 多余提前量堵在前端环")


def test_es8388_mixer_sets_dac_volume_and_mic_pga():
    """★ 2026-11-20 实机事故: ES8388 的 "PCM Volume" 是**出声闸门** ——
    值为 0 时整路 DAC 静音, 声卡流卡在 SETUP 不推进, 表现是"语音链路
    全对(ASR/LLM/TTS/字幕都对), 但喇叭一声不吭"。
    板上的 S42audio-es8388 也会设它(=184,184), 但那是开机极早期跑的,
    早于 codec 就绪, cset 失败被 >/dev/null 2>&1 吞掉 -> 值留在默认 0,
    **每次重启都复发**; 而 S42 不在本仓管理范围内。
    所以仓库的 mixer 脚本(S98 跑, 晚于 S42 且前端启动之前)必须兜住:
      - 采集 PGA = 8(8 档每档 3dB = +24dB; 3 档差 7.6dB, 指令电平就
        低于 VAD 门限 -> "喊得醒问不应")
      - DAC PCM Volume = 184,184(安卓对照板实测值, bit7 解 mute)
    """
    mixer = (PORT.parent / "rk3562-es8388" / "mixer-es8388.sh").read_text(
        encoding="utf-8")
    assert '"Left Channel Capture Volume" 8' in mixer, \
        "mixer 脚本必须把采集 PGA 设成 8 档(3 档差 7.6dB, 指令过不了 VAD 门)"
    assert '"PCM Volume" 184,184' in mixer, \
        "mixer 脚本必须设 DAC PCM Volume=184,184(0=静音, 流卡 SETUP 不出声)"
