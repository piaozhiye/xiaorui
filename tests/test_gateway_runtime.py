import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "board-server/pkg"))
from config.runtime_paths import config_file, data_dir


class RuntimePathTests(unittest.TestCase):
    def test_legacy_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config_file(), data_dir() / ".config.yaml")
            self.assertEqual(data_dir().name, "data")

    def test_independent_instance_paths(self):
        with patch.dict(os.environ, {"XR_DATA_DIR": "/tmp/instance-a",
                                     "XR_CONFIG_FILE": "/tmp/config-b.yaml"}):
            self.assertEqual(data_dir(), Path("/tmp/instance-a"))
            self.assertEqual(config_file(), Path("/tmp/config-b.yaml"))
        with patch.dict(os.environ, {"XR_DATA_DIR": "/tmp/instance-c"}):
            os.environ.pop("XR_CONFIG_FILE", None)
            self.assertEqual(config_file(), Path("/tmp/instance-c/.config.yaml"))


if __name__ == "__main__":
    unittest.main()
