#!/usr/bin/env python3
"""Offline regression: no implicit provider credentials; keyless fallback works."""
import ast
import asyncio
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

SOURCE = (Path(__file__).resolve().parents[1] / "board-server/pkg/"
          "plugins_func/functions/get_weather.py")


class WeatherCredentialsTests(unittest.TestCase):
    def run_weather(self, config):
        # Extract the real function without importing the server or making requests.
        tree = ast.parse(SOURCE.read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "get_weather")
        fn.decorator_list = []
        fn.returns = None
        for arg in fn.args.args:
            arg.annotation = None
        cache = Mock()
        cache.get.return_value = None
        manager = types.ModuleType("core.utils.cache.manager")
        manager.cache_manager = cache
        manager.CacheType = types.SimpleNamespace(WEATHER="weather", IP_INFO="ip")
        city = AsyncMock(return_value=None)
        fallback = AsyncMock(return_value="forecast")
        env = dict(fetch_city_info=city, _open_meteo_report=fallback,
                   logger=Mock(), TAG="test",
                   Action=types.SimpleNamespace(REQLLM="reply"),
                   ActionResponse=lambda *args: args)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SOURCE), "exec"), env)
        conn = types.SimpleNamespace(config={"plugins": {"get_weather": config}},
                                     client_ip=None)
        with patch.dict(sys.modules, {"core.utils.cache.manager": manager}):
            result = asyncio.run(env["get_weather"](conn, "杭州"))
        self.assertEqual(result[1], "forecast")
        fallback.assert_awaited_once_with("杭州")
        return city

    def test_no_credentials_skips_provider(self):
        self.run_weather({}).assert_not_awaited()

    def test_partial_credentials_skip_provider(self):
        for config in ({"api_host": "weather.invalid"}, {"api_key": "test-only"}):
            with self.subTest(config=config):
                self.run_weather(config).assert_not_awaited()

    def test_explicit_credentials_are_used(self):
        city = self.run_weather({"api_host": "weather.invalid", "api_key": "test-only"})
        city.assert_awaited_once_with("杭州", "test-only", "weather.invalid")


if __name__ == "__main__":
    unittest.main()
