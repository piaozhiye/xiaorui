"""TTS 分句回归: 逗号不得切句。

2026-11-19 板端听感事故: 用户反馈"一个句子里字有卡顿, 有时候整段没听到"。
根因: TTSProviderBase.punctuations 把"，；："也算句末 —— 每个逗号切一句,
单句合成、单句下发, 句间再插静音, 一段话被剁成"于是"/"是啊"/"小狐狸说"
这种碎片, 听感就是字级卡顿。

契约:
- 非首句只按强句末标点(。！？!? 和换行)切分; 逗号留在句内。
- 首句保留逗号切分(开口要快, 6 字就出声)。
- 一直等不到强标点的长句, 到阈值后按逗号兜底切, 不能无限等。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "board-server" / "pkg"))

from core.providers.tts.base import TTSProviderBase  # noqa: E402


class _Probe(TTSProviderBase):
    """只测分句, 不碰音频。"""

    def audio_to_pcm_data_stream(self, *args, **kwargs):
        return iter(())

    def text_to_speak(self, *args, **kwargs):
        return None


def make_provider():
    p = _Probe({}, False)
    p.tts_stop_request = False
    p.tts_text_buff = []
    p.processed_chars = 0
    p.is_first_sentence = False
    return p


def feed(p, text):
    p.tts_text_buff.append(text)
    return p._get_segment_text()


class TtsSegmentationTest(unittest.TestCase):
    def test_comma_does_not_end_sentence(self):
        """逗号后面还有话: 不许切, 等强标点。"""
        p = make_provider()
        self.assertIsNone(feed(p, "从前有一只勇敢的小狐狸，它住在一个美丽的大森林里，"),
                          "逗号处不得切句(否则就是碎片化合成)")
        self.assertIsNone(feed(p, "它非常聪明，"),
                          "还没到句号, 继续攒")
        seg = feed(p, "总是想办法找到食物。")
        self.assertIsNotNone(seg, "句号到了必须出句")
        self.assertFalse(seg.rstrip().endswith(("，", "。")), "送 TTS 的文本应去掉句末标点")
        self.assertIn("从前有一只勇敢的小狐狸", seg)
        self.assertIn("它住在一个美丽的大森林里", seg, "逗号两侧必须同句合成")
        self.assertIn("总是想办法找到食物", seg, "整句必须一次送 TTS, 不能被逗号剁开")

    def test_first_sentence_still_splits_at_comma(self):
        """首句为了开口快, 保留逗号切分。"""
        p = make_provider()
        p.is_first_sentence = True
        seg = feed(p, "好的，我来给你讲一个故事，")
        self.assertIsNotNone(seg, "首句应尽早出声")
        self.assertLessEqual(len(seg), 20, "首句应只取到第一个逗号附近")

    def test_long_runon_has_comma_fallback(self):
        """一直没有句号的超长句, 到阈值按逗号兜底, 不能无限等。"""
        p = make_provider()
        long_text = "，".join(["这是一个很长的分句"] * 6) + "，"
        self.assertGreater(len(long_text), 40)
        seg = feed(p, long_text)
        self.assertIsNotNone(seg, "超长无句号文本必须兜底切分, 否则整轮没有声音")
        self.assertGreater(len(seg), 10)

    def test_newline_ends_sentence(self):
        """模型按行输出时, 换行仍视为句末。"""
        p = make_provider()
        seg = feed(p, "小狐狸出发了\n")
        self.assertIsNotNone(seg)
        self.assertIn("小狐狸出发了", seg)


if __name__ == "__main__":
    unittest.main()
