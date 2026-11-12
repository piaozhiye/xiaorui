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
    ):
        shutil.copy(PORT / filename, port / filename)
    for directory in (
        "board-server/wheels314", "board-server/pkg", "models/sherpa-onnx-sense-voice-zh",
        "tts-matcha/matcha-icefall-zh-baker", "vendor/sherpa-onnx/lib",
    ):
        (repo / directory).mkdir(parents=True)
    for filename in (
        "board-server/librkllmrt.so", "board-server/llm_rkllm.py",
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
if "cat /proc/device-tree/model" in command:
    print("RK3562 EVB2")
elif "snd_config_search" in command:
    sys.exit(0 if os.environ.get("ALSA_CONFIG_VALID", "1") == "1" else 1)
elif command == "/data/miniforge/bin/python3 -V":
    print("Python 3.14")
elif "grep -c" in command:
    print("1")
elif "/dev/tcp/" in command:
    port = os.environ.get("FAIL_PORT")
    sys.exit(1 if port and "/" + port + ")" in command else 0)
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
    source = source.replace(
        'probe() { (echo > /dev/tcp/127.0.0.1/"$1") 2>/dev/null; }',
        'probe() { [ "$1" != "${FAIL_PORT:-}" ]; }',
    )
    monkeypatch.setenv("FAIL_PORT", "" if http_ready else "8004")
    result = subprocess.run(["bash", "-c", source], capture_output=True, text=True, check=False)
    assert (result.returncode == 0) == http_ready
    if not http_ready:
        assert "8004 未就绪" in result.stdout


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
