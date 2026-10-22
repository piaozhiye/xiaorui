#!/usr/bin/env python3
"""噪声/礼貌填充词静默门单测。

2026-10-20 实测: 云 ASR 把"小瑞小瑞"听成 "Thank you."/"Hmm."/"Okay.",
本地 RKNN 听成"好好""嗯好的" —— 这些垃圾文本原样直达 LLM, 板子回一句
"嗯？有什么事要我帮忙吗", 看着像"噪声被当人声"。门要能吃掉它们, 又不能
吃掉带命令的整句("好的, 帮我开灯" 必须放行)。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "board-server" / "pkg"))

from core.handle.intentHandler import is_noise_token  # noqa: E402


class NoiseTokenTests(unittest.TestCase):
    def test_single_char_fillers(self):
        for text in ("嗯", "啊", "哦", "噢", "呃", "哎", "欸", "嘛", "呀", "哼", "唉"):
            self.assertTrue(is_noise_token(text), text)

    def test_politeness_and_filler_phrases(self):
        for text in ("好好", "嗯好的", "好的好的", "嗯嗯", "好的。", "好吧", "哈哈",
                     "不用谢", "没事", "不用了"):
            self.assertTrue(is_noise_token(text), text)

    def test_filler_only_sentences(self):
        """纯填充字拼接的整句(2026-10-20 实测): 云 ASR 空回执后 hybrid 转本地,
        "嗯好好好的" 直达 LLM, 板子答"好的，请问有什么可…"。"""
        for text in ("嗯好好好的", "好好好的", "嗯嗯好的", "嗯好啊", "好嘞好嘞"):
            self.assertTrue(is_noise_token(text), text)

    def test_asr_mishearing_garbage(self):
        for text in ("Thank you.", "thank you", "Hmm.", "Okay.", "OK", "Thanks"):
            self.assertTrue(is_noise_token(text), text)

    def test_real_requests_are_not_noise(self):
        for text in ("好的帮我开灯", "帮我关灯", "开灯", "好的, 帮我开灯",
                     "打开客厅的灯", "换首歌", "讲个笑话", "现在几点了",
                     "小瑞小瑞", "小瑞", "谢谢"):
            # "谢谢" 有意不拦: 用户道谢理应得到回应, 拦了是改正常交互
            self.assertFalse(is_noise_token(text), text)

    def test_empty_is_not_noise(self):
        self.assertFalse(is_noise_token(""))
        self.assertFalse(is_noise_token(None))


if __name__ == "__main__":
    unittest.main()
