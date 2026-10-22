#!/usr/bin/env python3
"""logger 导入期兜底反例单测（2026-10-22 Codex 复审补）。

反例 1: 先缺配置(导入期兜底)再传正式配置, 正式配置必须生效 —— 兜底不得
        借 _logger_initialized 锁死默认值(曾实测被锁成 INFO/tmp/server.log)。
反例 2: 已按正式配置初始化后, 后续兜底调用不得降级、不得重复配置。
兜底不应产生文件日志/目录副作用。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "board-server" / "pkg"))

import config.logger as cl  # noqa: E402


class LoggerFallbackTests(unittest.TestCase):
    def setUp(self):
        self._flag = cl._logger_initialized

    def tearDown(self):
        cl._logger_initialized = self._flag

    def test_fallback_then_real_config_reinitializes(self):
        with patch.object(cl, "logger") as m, \
                patch.object(cl, "_logger_initialized", False):
            cl.setup_logging(config={})  # 导入期兜底: 无 log 段
            self.assertFalse(cl._logger_initialized, "兜底不得置完成标志")
            self.assertEqual(m.add.call_count, 1, "兜底只配 stdout, 不建文件日志")

            cl.setup_logging(config={"log": {"log_level": "ERROR",
                                             "log_file": "custom.log"}})
            self.assertTrue(cl._logger_initialized, "正式配置应完成初始化")
            levels = [c.kwargs.get("level") for c in m.add.call_args_list]
            filesink = any(not isinstance(c.args[0], int) and c.args[0] != sys.stdout
                           for c in m.add.call_args_list)
            self.assertIn("ERROR", levels, "正式级别必须生效, 而非锁死 INFO")
            self.assertTrue(filesink, "正式配置应重建文件日志 sink")

    def test_initialized_short_circuits_without_downgrade(self):
        with patch.object(cl, "logger") as m, \
                patch.object(cl, "_logger_initialized", True):
            cl.setup_logging(config={})  # 已初始化后的兜底调用
            m.configure.assert_not_called()
            m.remove.assert_not_called()
            m.add.assert_not_called()

    def test_fallback_creates_no_directories(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, \
                patch.object(cl, "logger"), \
                patch.object(cl, "_logger_initialized", False), \
                patch.object(cl.os, "makedirs", side_effect=AssertionError("兜底不应建目录")):
            cwd = cl.os.getcwd()
            cl.os.chdir(d)
            try:
                cl.setup_logging(config={})
            finally:
                cl.os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
