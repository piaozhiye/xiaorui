"""RockASR/RockTTS provider 与 helper 的 PC 侧测试(不需要 SDK).

- framing: 帧协议往返、非法长度
- asr/tts helper: 用假 rockx 模块驱动真实 helper 脚本, 验证协议与结果提取
- HelperProcess: 拉起/握手/请求/崩溃重启
- provider: mock HelperProcess 后验证 PCM->文本 / 文本->wav
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "board-server" / "pkg"
ROCK = PKG / "core" / "providers" / "rock"
sys.path.insert(0, str(ROCK))
sys.path.insert(0, str(PKG))
import framing  # noqa: E402


class FramingTests(unittest.TestCase):
    def test_roundtrip_with_body(self):
        import io
        buf = io.BytesIO()
        framing.send_msg(buf, {"cmd": "asr"}, body=b"\x01\x02\x03\x04")
        buf.seek(0)
        header, body = framing.recv_msg(buf)
        self.assertEqual(header["cmd"], "asr")
        self.assertEqual(header["body_len"], 4)
        self.assertEqual(body, b"\x01\x02\x03\x04")

    def test_roundtrip_unicode(self):
        import io
        buf = io.BytesIO()
        framing.send_msg(buf, {"text": "你好，小瑞。"})
        buf.seek(0)
        header, body = framing.recv_msg(buf)
        self.assertEqual(header["text"], "你好，小瑞。")
        self.assertEqual(body, b"")

    def test_short_stream_raises(self):
        import io
        buf = io.BytesIO(struct.pack(">I", 100) + b"{}")
        with self.assertRaises(framing.ProtocolError):
            framing.recv_msg(buf)

    def test_bad_header_len_raises(self):
        import io
        buf = io.BytesIO(struct.pack(">I", 0))
        with self.assertRaises(framing.ProtocolError):
            framing.recv_msg(buf)


FAKE_ROCKX = '''
"""测试用假 rockx 模块: 行为级模拟, 不加载任何 .so."""
import ctypes as _real_ctypes


_cast_registry = []


class _CtypesShim:
    """真 cast 之外把原回调登记下来, 假引擎按类型取回(c_void_p 字段存不回函数)."""

    def __getattr__(self, name):
        return getattr(_real_ctypes, name)

    @staticmethod
    def cast(obj, typ):
        _cast_registry.append(obj)
        return _real_ctypes.cast(obj, typ)


ctypes = _CtypesShim()

import threading  # noqa: E402
import time  # noqa: E402

ROCKX_RET_SUCCESS = 0
ROCKX_CALLBACK_ON_SCHED_THREAD = 0
ROCKX_KEY_MODULE_NAME = "module_name"
ROCKX_KEY_DATA_PATH = "data_path"
ROCKX_KEY_AUTH_CONFIG = "auth_config"
ROCKX_STREAM_STATE_FIRST = 0
ROCKX_STREAM_STATE_CONTINUE = 1
ROCKX_STREAM_STATE_LAST = 2
ROCKX_DATA_TYPE_INT16 = 3
ROCKASR_MODE_STREAM = 0
ROCKASR_MODE_TRANSCRIBE = 1
ROCKASR_LANG_ENGLISH = 0
ROCKASR_LANG_CHINESE = 1
ASR_STATE_RUNNING = 2
ASR_STATE_FINISH = 3
ASR_STATE_COMPLETE = 4


class RockXError(Exception):
    pass


class FakeResult:
    def __init__(self, state, text):
        self.state = state
        self.result = text.encode() if text else None
        self.newResult = None


ROCKASR_RESULT_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.py_object, ctypes.c_int, ctypes.py_object)
RKTTS_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.py_object, ctypes.c_int, ctypes.c_bool, ctypes.py_object)
RESULT_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.py_object, ctypes.py_object, ctypes.py_object)


class FakeLangMode(ctypes.Structure):
    _fields_ = [("source", ctypes.c_int), ("target", ctypes.c_int)]


class FakeInitParam(ctypes.Structure):
    _fields_ = [
        ("modelName", ctypes.c_char_p),
        ("workMode", ctypes.c_int),
        ("langMode", FakeLangMode),
        ("sampleRate", ctypes.c_int),
        ("userdata", ctypes.c_void_p),
        ("resultCallback", ctypes.c_void_p),
    ]


RockXAsrInitParam = FakeInitParam


class FakeRx:
    def __init__(self, mode):
        self.mode = mode
        self.params = {}
        self.ptrs = {}
        self.outputs = {}

    def create(self):
        return 1

    def init(self, handle):
        if self.params.get("module_name") == "ASR":
            if self.params.get("data_path") != "/fake/data":
                raise RockXError("data_path 未透传")
        return 0

    def set_str(self, handle, node, name, value):
        self.params[name] = value
        return 0

    def set_int(self, handle, node, name, value):
        self.params[name] = value
        return 0

    def set_ptr(self, handle, node, name, value, need_release=0):
        # byref(x) 包装要解出原对象(ctypes byref 带 _obj)
        self.ptrs[(node, name)] = getattr(value, "_obj", value)
        return 0

    def set_output_info(self, handle, name, out_names, cb):
        self.outputs[name] = cb
        return 0

    def input_create(self, handle):
        return ["pkt0", "pkt1"]

    def wait_finish(self, handle, inp):
        return 0

    def input_destroy(self, handle, inp):
        return 0

    def input_get_packet(self, inp, i):
        return inp[i]

    def packet_set_pcm(self, pkt, pcm, sr, state):
        self.last_pcm = pcm
        return 0

    def packet_set_string(self, pkt, text):
        self.last_text = text
        return 0

    def packet_set_float(self, pkt, v):
        self.last_alpha = v
        return 0

    def process_async(self, handle, inp):
        if self.mode == "asr":
            cb = next(c for c in _cast_registry
                      if isinstance(c, ROCKASR_RESULT_CALLBACK))
            pcm = self.last_pcm

            def run():
                time.sleep(0.05)
                cb([FakeResult(2, "正在"), FakeResult(2, "识别中")], 2, None)
                n = len(pcm) // 320  # 每 20ms 一"字"
                cb([FakeResult(4, f"共{n}帧的识别结果")], 1, None)
            threading.Thread(target=run, daemon=True).start()
        else:
            cb = next(c for c in _cast_registry if isinstance(c, RKTTS_CALLBACK))

            def run():
                time.sleep(0.05)
                n = len(self.last_text.encode()) * 100
                cb((ctypes.c_int16 * n)(*([100] * n)), n, True, None)
            threading.Thread(target=run, daemon=True).start()
        return 0


_load_mode = None


def load_rockx(lib_dir, algo_lib, extra_preload=()):
    return FakeRx(_load_mode)
'''


def make_fake_env(mode):
    """临时目录: 真实 helper/framing + 假 rockx, 让 helper 的 sys.path[0] 命中."""
    d = tempfile.mkdtemp(prefix="rock-fake-")
    for f in ("asr_helper.py", "tts_helper.py", "framing.py"):
        shutil.copy(ROCK / f, d)
    global _load_mode
    src = FAKE_ROCKX.replace("_load_mode = None", f"_load_mode = {mode!r}")
    (Path(d) / "rockx.py").write_text(src)
    return d


class AsrHelperTests(unittest.TestCase):
    def setUp(self):
        self.d = make_fake_env("asr")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(self.d, "asr_helper.py"),
             "--lib-dir", "/fake/lib", "--data-dir", "/fake/data",
             "--auth-config", "/fake/auth.json"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=self.d)

    def tearDown(self):
        self.proc.kill()
        shutil.rmtree(self.d, ignore_errors=True)

    def test_ready_and_transcribe(self):
        header, _ = framing.recv_msg(self.proc.stdout)
        self.assertTrue(header["ok"])
        self.assertEqual(header["event"], "ready")

        pcm = b"\x00\x00" * 160  # 160 帧 * 20ms
        framing.send_msg(self.proc.stdin, {"cmd": "asr"}, body=pcm)
        header, _ = framing.recv_msg(self.proc.stdout)
        self.assertTrue(header["ok"], header)
        self.assertEqual(header["text"], "共1帧的识别结果")

    def test_unknown_cmd_reports_error(self):
        framing.recv_msg(self.proc.stdout)  # ready
        framing.send_msg(self.proc.stdin, {"cmd": "bogus"})
        header, _ = framing.recv_msg(self.proc.stdout)
        self.assertFalse(header["ok"])
        self.assertIn("未知命令", header["error"])


class TtsHelperTests(unittest.TestCase):
    def setUp(self):
        self.d = make_fake_env("tts")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(self.d, "tts_helper.py"),
             "--lib-dir", "/fake/lib", "--data-dir", "/fake/data",
             "--auth-config", "/fake/auth.json", "--alpha", "1.0"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=self.d)

    def tearDown(self):
        self.proc.kill()
        shutil.rmtree(self.d, ignore_errors=True)

    def test_synthesize_returns_pcm(self):
        framing.recv_msg(self.proc.stdout)  # ready
        framing.send_msg(self.proc.stdin,
                              {"cmd": "tts", "text": "你好", "alpha": 1.0})
        header, body = framing.recv_msg(self.proc.stdout)
        self.assertTrue(header["ok"], header)
        self.assertEqual(header["sample_rate"], 24000)
        # "你好" = 6 字节 * 100 样本 * 2 字节
        self.assertEqual(len(body), 6 * 100 * 2)
        self.assertEqual(body[:4], b"\x64\x00\x64\x00")  # int16 100,100 LE

    def test_partial_audio_without_last_frame_fails_and_exits(self):
        self.proc.kill()
        self.proc.wait()
        fake = Path(self.d) / "rockx.py"
        fake.write_text(fake.read_text().replace(
            "n, True, None)", "n, False, None)"))
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(self.d, "tts_helper.py"),
             "--lib-dir", "/fake/lib", "--data-dir", "/fake/data",
             "--auth-config", "/fake/auth.json", "--timeout", "0.15"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=self.d)
        framing.recv_msg(self.proc.stdout)
        framing.send_msg(self.proc.stdin, {"cmd": "tts", "text": "你好"})
        header, body = framing.recv_msg(self.proc.stdout)
        self.assertFalse(header["ok"])
        self.assertIn("超时", header["error"])
        self.assertEqual(body, b"")
        self.assertEqual(self.proc.wait(timeout=3), 4)

    def test_empty_text_raises_helper_error_visible(self):
        framing.recv_msg(self.proc.stdout)
        framing.send_msg(self.proc.stdin,
                              {"cmd": "tts", "text": "  ", "alpha": 1.0})
        header, _ = framing.recv_msg(self.proc.stdout)
        self.assertFalse(header["ok"])


class HelperProcessTests(unittest.TestCase):
    def test_request_and_crash_restart(self):
        d = make_fake_env("asr")
        from core.providers.rock.helper_proc import HelperError, HelperProcess
        hp = HelperProcess("asr_helper.py",
                           ["--lib-dir", "/fake/lib", "--data-dir", "/fake/data",
                            "--auth-config", "/fake/auth.json"])
        # 指向临时目录里的脚本(带假 rockx)
        hp.script = os.path.join(d, "asr_helper.py")
        try:
            header, _ = hp.request({"cmd": "asr"}, body=b"\x00" * 640,
                                   timeout=30)
            self.assertTrue(header["ok"])
            self.assertEqual(header["text"], "共2帧的识别结果")

            # 杀掉 helper: 下一次请求应自动重启并成功
            hp.proc.kill()
            hp.proc.wait()
            header, _ = hp.request({"cmd": "asr"}, body=b"\x00" * 320,
                                   timeout=30)
            self.assertTrue(header["ok"])
        finally:
            hp.stop()
            shutil.rmtree(d, ignore_errors=True)


class ProviderTests(unittest.TestCase):
    """provider 层: mock HelperProcess, 验证 PCM->文本 / 文本->wav."""

    def setUp(self):
        # runtime 的槽位是全局单例, 跨用例会串味(上一个用例的 factory 被复用)
        from core.providers.rock import runtime
        runtime.reset_slots()

    def tearDown(self):
        from core.providers.rock import runtime
        runtime.reset_slots()

    def test_rock_asr_speech_to_text(self):
        from core.providers.asr.rock_asr import ASRProvider
        cfg = {"lib_dir": "/l", "model_dir": "/d", "auth_config": "/a"}
        # patch 必须覆盖整个用例: provider 构造时会起后台预热线程,
        # 预热线程在 patch 失效后才会真正建 HelperProcess(就会去加载真的 .so)
        with patch("core.providers.asr.rock_asr.HelperProcess") as HP:
            inst = HP.return_value
            inst.request.return_value = (
                {"ok": True, "text": "现在几点了", "elapsed_s": 0.9}, b"")
            p = ASRProvider(cfg, True)
            from core.providers.asr.base import ASRProviderBase
            art = ASRProviderBase.AudioArtifacts(
                pcm_frames=[b"a"], pcm_bytes=b"\x00" * 3200,
                file_path=None, temp_path=None)
            import asyncio
            text, path = asyncio.run(
                p.speech_to_text([b"\x00" * 3200], "s1", art))
            self.assertEqual(text, "现在几点了")
            self.assertIsNone(path)
            inst.request.assert_called_once()

    def test_rock_tts_text_to_speak(self):
        from core.providers.tts.rock_tts import TTSProvider
        cfg = {"lib_dir": "/l", "model_dir": "/d", "auth_config": "/a"}
        with patch("core.providers.tts.rock_tts.HelperProcess") as HP:
            inst = HP.return_value
            inst.request.return_value = (
                {"ok": True, "sample_rate": 24000, "elapsed_s": 0.5},
                b"\x01\x00" * 2400)
            p = TTSProvider(cfg, True)
            import asyncio
            with tempfile.TemporaryDirectory() as td:
                out = os.path.join(td, "o.wav")
                asyncio.run(p.text_to_speak("你好", out))
                import wave
                with wave.open(out) as w:
                    self.assertEqual(w.getframerate(), 24000)
                    self.assertEqual(w.getnchannels(), 1)
                    self.assertEqual(w.getnframes(), 2400)


if __name__ == "__main__":
    unittest.main()
