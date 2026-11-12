#!/usr/bin/env python3
import argparse
import copy
from pathlib import Path
import secrets
import shutil

import yaml


DIRECTORY = Path(__file__).resolve().parent
REPO = DIRECTORY.parents[1]


def merge_config(defaults, overlay):
    result = copy.deepcopy(defaults)
    for name, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(name), dict):
            result[name] = merge_config(result[name], value)
        else:
            result[name] = copy.deepcopy(value)
    return result


def load_mapping(path):
    with path.open(encoding="utf-8") as source:
        value = yaml.safe_load(source)
    if not isinstance(value, dict):
        raise ValueError(f"配置必须是 YAML mapping: {path}")
    return value


def render_config(defaults, overlay, wakeup):
    config = merge_config(defaults, overlay)
    words = wakeup.get("wakeup_words")
    if not isinstance(words, list) or not words or not all(
        isinstance(word, str) and word.strip() for word in words
    ):
        raise ValueError("唤醒词表必须是非空字符串列表")
    config["wakeup_words"] = words
    config["server"]["auth_key"] = secrets.token_hex(32)
    for kind in ("VAD", "ASR", "LLM", "TTS", "Memory", "Intent"):
        selected = config["selected_module"][kind]
        if not config.get(kind, {}).get(selected, {}).get("type"):
            raise ValueError(f"选中模块缺 type: {kind}.{selected}")
    return config


def package_gateway(code_dir, destination, config):
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"输出目录必须不存在或为空: {destination}")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)

    def ignored(directory, names):
        excluded = {name for name in names if name == "__pycache__" or name.endswith(".pyc")}
        excluded.update(name for name in names if name.startswith("."))
        if Path(directory) == code_dir:
            excluded.update({"config.yaml", "config_from_api.yaml", "data"})
        return excluded

    shutil.copytree(code_dir, destination / "server", ignore=ignored)
    (destination / "data").mkdir(mode=0o700)
    for relative in ("server/config.yaml", "data/.config.yaml"):
        path = destination / relative
        path.touch(mode=0o600)
        path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="用公开模板生成不依赖私有配置的板内网关包")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--code-dir", type=Path, default=REPO / "board-server/pkg")
    args = parser.parse_args()
    config = render_config(
        load_mapping(DIRECTORY / "gateway-defaults.yaml"),
        load_mapping(DIRECTORY / "gateway-board.yaml"),
        load_mapping(REPO / "board-server/config/wakeup_words.xiaorui.yaml"),
    )
    package_gateway(args.code_dir.resolve(), args.output_dir, config)
    print(f"网关包已生成: {args.output_dir} (不读取私有 config.yaml/data/)")


if __name__ == "__main__":
    main()
