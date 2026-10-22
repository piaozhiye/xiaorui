#!/usr/bin/env python3
"""服务端唤醒词表 vs 前端变体表的一致性门禁。

2026-10-20 实测: 板上网关的 config.yaml 是上游小智原版, wakeup_words 里
是"你好小智/小爱同学/小龙小龙" —— 根本没有"小瑞"。于是服务端的唤醒词门
对本机等于关着, "小瑞小瑞"被当普通语句喂给 LLM(回"🙂"), 而 LLM 的应答
TTS 还会把随后的指令段整个吃掉(云 ASR 只听到 "Okay")。

这条门禁把"两端变体表一致"变成机械不变量: 前端 board/xr_wake.h 的变体表
每个字, 都要有对应的双单元形式出现在服务端 config.yaml 的 wakeup_words 里。
"""
import re
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "board-server" / "config" / "wakeup_words.xiaorui.yaml"
# config.yaml 含 API 密钥, 在 .gitignore 里不入库 —— 所以真源是上面的 overlay
LOCAL_CONFIG = ROOT / "board-server" / "pkg" / "config.yaml"
WAKE_HEADER = ROOT / "board" / "xr_wake.h"


def frontend_variants():
    source = WAKE_HEADER.read_text()
    # 先剥注释: 注释里的示例文本(如 小想)不是变体字, 否则会被当成变体
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"//[^\n]*", "", source)
    block = re.search(r"static const char \*variants\[\] = \{(.*?)\};", source, re.S)
    assert block, "找不到前端变体表"
    return re.findall(r'"([^"]+)"', block.group(1))


def overlay_wakeup_words():
    words = yaml.safe_load(OVERLAY.read_text()).get("wakeup_words") or []
    assert isinstance(words, list), "wakeup_words 必须是列表"
    return words


def local_config_wakeup_words():
    if not LOCAL_CONFIG.exists():
        return None
    data = yaml.safe_load(LOCAL_CONFIG.read_text()) or {}
    if "wakeup_words" not in data:
        # 公开克隆里 config.yaml 是脱敏模板, 该段明确缺席 —— 比对无对象。
        return None
    words = data["wakeup_words"]
    if not isinstance(words, list) or not words:
        # 字段存在但内容非法(空列表/类型错误)是配置写坏了, 必须失败,
        # 不许用跳过掩盖。
        raise AssertionError(f"wakeup_words 存在但非法: {words!r}")
    return words


class WakeupConfigTests(unittest.TestCase):
    def test_device_wake_phrase_is_registered(self):
        words = overlay_wakeup_words()
        # 板上 KWS 关键词(@小瑞小瑞 / @小瑞, /data/rknn-test/sherpa/model-kws/keywords.txt)
        for word in ("小瑞小瑞", "小瑞"):
            self.assertIn(word, words, f"服务端唤醒词表缺 {word}")

    def test_every_frontend_variant_has_a_server_form(self):
        words = set(overlay_wakeup_words())
        for variant in frontend_variants():
            self.assertIn(f"小{variant}小{variant}", words,
                          f"前端能听岔成 小{variant}小{variant}, 服务端唤醒词表却没有")

    def test_stale_upstream_words_are_gone(self):
        # 上游小智的唤醒词对本机无意义, 留着会让人喊"小爱同学"时板子答"在的"
        # 而前端 KWS 根本没醒(设备只有一个唤醒词: 小瑞)
        words = set(overlay_wakeup_words())
        for word in ("你好小智", "小爱同学", "小龙小龙"):
            self.assertNotIn(word, words, word)

    def test_hello_and_pinyin_families_are_supported(self):
        words = set(overlay_wakeup_words())
        # 用户明确要求: "小瑞小瑞" / "你好xiaorui" 这一族都要认
        for word in ("小瑞小瑞", "小瑞", "你好小瑞", "xiaorui", "你好xiaorui"):
            self.assertIn(word, words, word)
        for variant in frontend_variants():
            self.assertIn(f"你好小{variant}", words, f"你好小{variant} 缺失")

    def test_local_config_matches_the_overlay(self):
        local = local_config_wakeup_words()
        if local is None:
            self.skipTest("本地 config.yaml 不在(不入库的密钥文件)")
        self.assertEqual(sorted(local), sorted(overlay_wakeup_words()),
                         "本地 config.yaml 的 wakeup_words 与 overlay 不一致")

    def test_invalid_local_wakeup_words_fails_not_skips(self):
        """反例(2026-10-22 Codex 复审): 字段存在但非法必须 FAIL, 不许跳过。"""
        import sys
        import tempfile
        from unittest.mock import patch
        for bad in ("wakeup_words: []\n", "wakeup_words: 小瑞\n"):
            with self.subTest(bad=bad):
                with tempfile.TemporaryDirectory() as d:
                    bogus = Path(d) / "config.yaml"
                    bogus.write_text(bad, encoding="utf-8")
                    with patch.object(sys.modules[__name__],
                                      "LOCAL_CONFIG", bogus):
                        with self.assertRaises(AssertionError):
                            local_config_wakeup_words()


if __name__ == "__main__":
    unittest.main()
