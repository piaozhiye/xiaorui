#!/usr/bin/env python3
import argparse
from email.parser import BytesParser
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile


DIRECTORY = Path(__file__).resolve().parent


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def requirements(path):
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9.+!-]+", line):
            raise ValueError(f"要求精确版本，不接受路径、URL 或未固定依赖: {line}")
        name, version = line.split("==")
        result[canonical(name)] = (name, version)
    return result


def compatible(path):
    _, python_tag, abi, platform = path.stem.rsplit("-", 3)
    architecture = platform == "any" or "aarch64" in platform
    interpreter = "cp314" in python_tag.split(".") or (
        abi == "none" and "py3" in python_tag.split(".")
    ) or (abi == "abi3" and python_tag.startswith("cp3"))
    return architecture and interpreter


def inventory(directory):
    result = {}
    for path in sorted(directory.glob("*.whl")):
        if not compatible(path):
            continue
        with zipfile.ZipFile(path) as wheel:
            metadata_files = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata_files) != 1:
                raise ValueError(f"wheel 元数据异常: {path.name}")
            metadata = BytesParser().parsebytes(wheel.read(metadata_files[0]))
        key = (canonical(metadata["Name"]), metadata["Version"])
        result.setdefault(key, []).append(path)
    return result


def main():
    parser = argparse.ArgumentParser(description="准备并校验 CPython 3.14/aarch64 离线 wheel 集")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, default=DIRECTORY / "requirements-board.lock")
    parser.add_argument("--download", action="store_true", help="显式允许从 pip 配置的索引下载缺失包")
    args = parser.parse_args()
    pinned = requirements(args.requirements)
    args.directory.mkdir(parents=True, exist_ok=True)
    wheels = inventory(args.directory)
    missing = [value for name, value in pinned.items() if (name, value[1]) not in wheels]
    if missing and args.download:
        with tempfile.TemporaryDirectory(prefix="rk3562-wheels-") as temporary:
            request = Path(temporary) / "requirements.txt"
            request.write_text("".join(f"{name}=={version}\n" for name, version in missing))
            subprocess.run([
                sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--no-deps",
                "--platform", "manylinux_2_28_aarch64", "--platform", "manylinux_2_17_aarch64",
                "--platform", "manylinux2014_aarch64", "--platform", "linux_aarch64",
                "--python-version", "3.14", "--implementation", "cp",
                "--abi", "cp314", "--abi", "abi3", "--abi", "none",
                "--dest", str(args.directory), "-r", str(request),
            ], check=True)
        wheels = inventory(args.directory)
        missing = [value for name, value in pinned.items() if (name, value[1]) not in wheels]
    if missing:
        parser.exit(2, "缺少匹配的 aarch64/CPython 3.14 wheel:\n" + "\n".join(
            f"  {name}=={version}" for name, version in missing
        ) + "\n可显式传 --download 补齐；不会使用上游通用 requirements.txt 安装 torch。\n")
    lines = []
    for normalized, (name, version) in sorted(pinned.items()):
        hashes = []
        for wheel in wheels[(normalized, version)]:
            with wheel.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            hashes.append("--hash=sha256:" + digest)
        lines.append(f"{name}=={version} " + " ".join(hashes))
    output = args.directory / "requirements-board.lock"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"WHEELHOUSE OK: {len(lines)} 个精确版本，安装哈希锁: {output}")


if __name__ == "__main__":
    main()
