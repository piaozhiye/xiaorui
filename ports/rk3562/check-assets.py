#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
from pathlib import Path


DIRECTORY = Path(__file__).resolve().parent
REPO = DIRECTORY.parents[1]


def asset_path(entry, roots):
    relative = Path(entry["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("清单只允许根目录内的相对路径")
    return roots[entry["root"]] / relative


def check_assets(manifest, roots, groups):
    errors = []
    checked = 0
    for entry in manifest["files"]:
        if entry["group"] not in groups:
            continue
        root = roots.get(entry["root"])
        if root is None:
            errors.append(f"未设置资源根: {entry['root']}")
            continue
        path = asset_path(entry, roots)
        if not path.is_file():
            errors.append(f"缺资源: {entry['root']}/{entry['path']}")
            continue
        if path.stat().st_size != entry["bytes"]:
            errors.append(f"大小不匹配: {entry['root']}/{entry['path']}")
            continue
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != entry["sha256"]:
            errors.append(f"SHA256 不匹配: {entry['root']}/{entry['path']}")
            continue
        checked += 1
    if not checked and not errors:
        errors.append("选定分组没有任何资源")
    return checked, errors


def main():
    parser = argparse.ArgumentParser(description="校验 RK3562 已测基线资源，不下载或修改文件")
    parser.add_argument("--manifest", type=Path, default=DIRECTORY / "assets.lock.json")
    parser.add_argument("--groups", nargs="+", choices=("gateway", "frontend", "vqe"),
                        default=["gateway", "frontend", "vqe"])
    args = parser.parse_args()
    roots = {"repo": REPO}
    for name, variable in (("sherpa", "XR_SHERPA_LIB_DIR"), ("vqe-package", "XR_VQE_PKG"),
                           ("vqe-libs", "XR_VQE_LIB_DIR")):
        roots[name] = Path(os.environ[variable]) if os.environ.get(variable) else None
    if roots["vqe-libs"] is None and roots["vqe-package"] is not None:
        roots["vqe-libs"] = roots["vqe-package"] / "lib/linux-aarch64"
    with args.manifest.open(encoding="utf-8") as source:
        manifest = json.load(source)
    checked, errors = check_assets(manifest, roots, set(args.groups))
    for error in errors:
        print(error)
    print(f"ASSETS: {checked} verified, {len(errors)} errors")
    if errors:
        parser.exit(2, "资源不齐或不是已测版本；停止部署，见 REPRODUCE.md。\n")


if __name__ == "__main__":
    main()
