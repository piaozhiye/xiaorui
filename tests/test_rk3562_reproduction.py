import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import zipfile

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
PORT = ROOT / "ports/rk3562"


def module(filename):
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), PORT / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_public_config_is_complete_and_uses_authoritative_wakeup_words(tmp_path):
    renderer = module("render-config.py")
    defaults = renderer.load_mapping(PORT / "gateway-defaults.yaml")
    overlay = renderer.load_mapping(PORT / "gateway-board.yaml")
    wakeup = renderer.load_mapping(ROOT / "board-server/config/wakeup_words.xiaorui.yaml")
    config = renderer.render_config(defaults, overlay, wakeup)
    assert config["selected_module"]["VAD"] == "SileroVAD"
    for kind, name in config["selected_module"].items():
        assert config[kind][name]["type"]
    assert config["xiaorui"]["audio_params"]["sample_rate"] == 24000
    assert config["exit_commands"]
    assert len(config["server"]["auth_key"]) == 64
    assert config["server"]["auth_key"] != renderer.render_config(defaults, overlay, wakeup)["server"]["auth_key"]
    assert config["wakeup_words"] == wakeup["wakeup_words"]
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("print('public code')")
    (source / "config.yaml").write_text("PRIVATE_SENTINEL")
    (source / "config_from_api.yaml").write_text("PRIVATE_SENTINEL")
    (source / ".env").write_text("PRIVATE_SENTINEL")
    (source / "data").mkdir()
    (source / "data/ha_token.txt").write_text("PRIVATE_SENTINEL")
    (source / "__pycache__").mkdir()
    (source / "__pycache__/app.pyc").write_bytes(b"cache")
    destination = tmp_path / "package"
    renderer.package_gateway(source, destination, config)
    assert (destination / "server/app.py").exists()
    assert not (destination / "server/data").exists()
    assert not (destination / "server/.env").exists()
    assert not (destination / "server/config_from_api.yaml").exists()
    assert not (destination / "server/__pycache__").exists()
    for name in ("server/config.yaml", "data/.config.yaml"):
        path = destination / name
        assert yaml.safe_load(path.read_text()) == config
        assert "PRIVATE_SENTINEL" not in path.read_text()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("words", [[], "小瑞", [None], [" "]])
def test_renderer_rejects_invalid_words(words):
    renderer = module("render-config.py")
    with pytest.raises(ValueError, match="唤醒词"):
        renderer.render_config({"server": {}}, {}, {"wakeup_words": words})


def make_wheel(path, name="demo", version="1.0"):
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr(f"{name}-{version}.dist-info/METADATA", f"Name: {name}\nVersion: {version}\n")


def test_offline_wheel_lock_checks_version_architecture_and_hash(tmp_path):
    request = tmp_path / "pins.txt"
    request.write_text("demo==1.0\n")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    make_wheel(wheels / "demo-1.0-cp314-cp314-manylinux2014_x86_64.whl")
    command = [sys.executable, str(PORT / "prepare-wheels.py"), "--directory", str(wheels),
               "--requirements", str(request)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert not (wheels / "requirements-board.lock").exists()
    make_wheel(wheels / "demo-2.0-py3-none-any.whl", version="2.0")
    assert subprocess.run(command, capture_output=True).returncode != 0
    valid = wheels / "demo-1.0-py3-none-any.whl"
    make_wheel(valid)
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (wheels / "requirements-board.lock").read_text().strip() == (
        "demo==1.0 --hash=sha256:" + hashlib.sha256(valid.read_bytes()).hexdigest()
    )


def test_wheel_requirements_disallow_unpinned_packages_and_urls(tmp_path):
    preparer = module("prepare-wheels.py")
    for invalid in ["demo>=1.0", "https://example.com/demo.whl", "-e ."]:
        request = tmp_path / "requirements.txt"
        request.write_text(invalid)
        with pytest.raises(ValueError):
            preparer.requirements(request)


def test_assets_missing_mismatched_and_verified(tmp_path):
    checker = module("check-assets.py")
    content = b"known model"
    entry = {"group": "gateway", "root": "repo", "path": "model.onnx",
             "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    manifest = {"files": [entry]}
    assert checker.check_assets(manifest, {"repo": tmp_path}, {"gateway"})[1]
    path = tmp_path / "model.onnx"
    path.write_bytes(b"wrong")
    assert checker.check_assets(manifest, {"repo": tmp_path}, {"gateway"})[1]
    path.write_bytes(b"wrong model")
    assert checker.check_assets(manifest, {"repo": tmp_path}, {"gateway"})[1]
    path.write_bytes(content)
    assert checker.check_assets(manifest, {"repo": tmp_path}, {"gateway"}) == (1, [])
    assert checker.check_assets(manifest, {"repo": tmp_path}, {"frontend"})[1]
    for invalid in ("../private", "/etc/passwd"):
        with pytest.raises(ValueError):
            checker.asset_path({**entry, "path": invalid}, {"repo": tmp_path})


def test_published_resource_manifest_has_no_personal_paths_or_secrets():
    manifest = json.loads((PORT / "assets.lock.json").read_text())
    assert manifest["baseline"]["rknpu"] == "0.9.8"
    for entry in manifest["files"]:
        assert len(entry["sha256"]) == 64
        assert entry["source"] in manifest["sources"]
        assert not Path(entry["path"]).is_absolute()
        assert ".." not in Path(entry["path"]).parts
    assert "/home/" not in json.dumps(manifest)
