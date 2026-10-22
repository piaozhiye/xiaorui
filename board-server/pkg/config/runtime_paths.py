"""Separate installed code from per-instance configuration and mutable data."""
import os
from pathlib import Path


def data_dir():
    return Path(os.environ.get("XR_DATA_DIR", Path(__file__).resolve().parents[1] / "data")).resolve()


def config_file():
    return Path(os.environ.get("XR_CONFIG_FILE", data_dir() / ".config.yaml")).resolve()
