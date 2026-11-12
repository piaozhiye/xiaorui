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
# 前端 KWS 词表(sherpa-onnx 拼音格式), 是仓根那一份
KWS_KEYWORDS = ROOT / "keywords.txt"
# 板内网关配置的基线, wakeup_words 由 deploy 脚本注入
BOARD_GATEWAY_YAML = ROOT / "ports" / "rk3562" / "gateway-board.yaml"
# 前端 KWS 的部署脚本: 必须推仓根的 keywords.txt
FRONTEND_DEPLOY = ROOT / "ports" / "rk3562" / "deploy-and-run.sh"

# sherpa-onnx 词表的音节 token -> 不带调拼音音节。
# 只收本仓实际用到的音节: 出现表外的音节要 FAIL(拼错的音节不许静默放过)。
SYLLABLE_TO_PINYIN = {
    "x": "x", "iǎo": "iao", "r": "r", "uì": "ui",
    "n": "n", "ǐ": "i", "h": "h", "ǎo": "ao",
}

# 标签汉字 -> 不带调拼音音节(用于校验"音节 == 标签")
HANZI_TO_PINYIN = {
    "小": "xiao", "瑞": "rui", "你": "ni", "好": "hao",
}


def kws_entries():
    """解析仓根 keywords.txt -> [(音节token列表, 标签)]。

    格式: "<音节> <音节> ... @标签" (标签可省, 但本仓必须带)。
    """
    entries = []
    for lineno, raw in enumerate(KWS_KEYWORDS.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert "@" in line, f"keywords.txt 第 {lineno} 行缺 @标签: {raw!r}"
        tokens, _, label = line.partition("@")
        syllables = tokens.split()
        assert syllables, f"keywords.txt 第 {lineno} 行没有音节: {raw!r}"
        entries.append((syllables, label.strip()))
    return entries


def kws_labels():
    return [label for _, label in kws_entries()]


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


class FrontendKwsTests(unittest.TestCase):
    """前端 KWS 词表(sherpa-onnx keywords.txt) vs 服务端唤醒词表。

    2026-11-11 实测踩坑: 仓根 keywords.txt 里
      1) 有一行 "x iǎo z i x iǎo z i @小瑞小瑞" —— 音节是"小智小智",
         标签却写成"小瑞小瑞"(sherpa 按音节匹配, 这行实际是个"小智小智"关键词);
      2) 还有一行 "@小爱同学" —— 服务端词表明确不许有它(前端 KWS 醒了,
         服务端"小X小Y"形状兜底匹配不上, 板子一声不吭)。
    """

    def test_phoneme_tokens_are_known(self):
        for syllables, label in kws_entries():
            for tok in syllables:
                self.assertIn(tok, SYLLABLE_TO_PINYIN,
                              f"keywords.txt 音节 {tok!r} 不在已知音节表里(标签 {label})")

    def test_phonemes_match_the_label(self):
        """音节必须真的拼出标签 —— 抓住"标签写错"的行。"""
        for syllables, label in kws_entries():
            # 用 .get 兜住未知音节: 报清晰的断言失败, 而不是 KeyError
            decoded = "".join(SYLLABLE_TO_PINYIN.get(t, f"?{t}") for t in syllables)
            for ch in label:
                self.assertIn(ch, HANZI_TO_PINYIN,
                              f"keywords.txt 标签 {label!r} 里的 {ch!r} 没有拼音映射")
            expected = "".join(HANZI_TO_PINYIN[ch] for ch in label)
            self.assertEqual(decoded, expected,
                             f"keywords.txt 音节拼出 {decoded!r}, 与标签 {label!r} 不符")

    def test_no_duplicate_phoneme_lines(self):
        seen = set()
        for syllables, label in kws_entries():
            key = " ".join(syllables)
            self.assertNotIn(key, seen, f"keywords.txt 音节行重复: {key} ({label})")
            seen.add(key)

    def test_labels_are_registered_server_side(self):
        """前端能唤醒的词, 服务端词表必须都认 —— 否则前端醒了服务端不理。"""
        words = set(overlay_wakeup_words())
        for label in kws_labels():
            self.assertIn(label, words, f"前端 KWS 词 {label!r} 不在服务端 wakeup_words 里")

    def test_no_stale_upstream_words(self):
        """上游小智的唤醒词不许出现在前端词表里(前端醒了服务端不认)。"""
        for label in kws_labels():
            for stale in ("小爱同学", "小智", "小龙", "你好小智"):
                self.assertNotIn(stale, label,
                                 f"前端 KWS 词表里有上游遗留词 {label!r}")

    def test_board_gateway_config_gets_wakeup_words_injected(self):
        """板内网关配置必须有 wakeup_words, 且来自同一个真源。

        2026-11-11 实测: 推上板的 .config.yaml 压根没有 wakeup_words,
        服务端的唤醒词门只剩"小X小Y"形状兜底。
        """
        base = BOARD_GATEWAY_YAML.read_text(encoding="utf-8")
        self.assertIn("# >>> wakeup_words <<<", base,
                      "gateway-board.yaml 缺注入标记, 部署脚本没法注入词表")
        words = overlay_wakeup_words()
        block = "wakeup_words:\n" + "".join(f'  - "{w}"\n' for w in words)
        merged = base.replace("# >>> wakeup_words <<<", block, 1)
        import yaml as _yaml
        data = _yaml.safe_load(merged)
        self.assertIsNotNone(data)
        self.assertEqual(data["wakeup_words"], words)

    def test_frontend_deploy_pushes_the_repo_keyword_file(self):
        """前端部署必须推仓根 keywords.txt, 不能推别处的一份。"""
        text = FRONTEND_DEPLOY.read_text(encoding="utf-8")
        self.assertIn('"$REPO/keywords.txt"', text,
                      "deploy-and-run.sh 没有推仓根 keywords.txt")


if __name__ == "__main__":
    unittest.main()
