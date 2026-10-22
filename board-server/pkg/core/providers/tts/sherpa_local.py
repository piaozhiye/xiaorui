"""本地 TTS: matcha 进程内常驻合成 (sherpa-onnx python API), vits 子进程仅留对照.

matcha 常驻后 (2026-10-09 板上实测): 模型加载 5.6 s 一次, 之后每句合成
0.4~1.5 s, 比该句播放时长还短 -> 服务端合成与发送流水线化, 客户端连续播放
不再有句间停顿. 旧子进程方案每句多付 ~2 s 进程启动, 已弃用.

config:
    TTS:
      SherpaLocal:
        type: sherpa_local
        engine: matcha          # vits = 旧 aishell3 子进程方案(RTF≈9, 慢, 对照用)
        model_dir: /data/xiaorui-server/tts-matcha/matcha-icefall-zh-baker
        num_threads: 4
        speed: 1.0
        output_dir: tmp/

输出 wav (matcha 原生采样率单声道 16bit), 基类负责重采样到 conn.sample_rate 并转 opus.
Baker 语料限非商用.
"""
import asyncio
import glob
import os
import subprocess
import threading
import wave

from core.providers.tts.base import TTSProviderBase

_tts = None
_tts_lock = threading.Lock()
_tts_key = None


def _get_tts(model_dir, num_threads):
    """进程内单例; 加载过一次后所有连接复用, 不再每句付加载成本."""
    global _tts, _tts_key
    with _tts_lock:
        if _tts is not None and _tts_key == (model_dir, num_threads):
            return _tts
        from sherpa_onnx import (OfflineTts, OfflineTtsConfig,
                                 OfflineTtsMatchaModelConfig, OfflineTtsModelConfig)
        d = model_dir
        acoustic = sorted(glob.glob(os.path.join(d, "model*.onnx")))
        vocoder = sorted(glob.glob(os.path.join(d, "hifigan*.onnx"))) or \
            sorted(glob.glob(os.path.join(d, "vocoder*.onnx")))
        if not acoustic or not vocoder:
            raise Exception(f"matcha 模型缺失: {d} 下需要 model*.onnx 和 hifigan*.onnx")
        fsts = ",".join(sorted(glob.glob(os.path.join(d, "*.fst"))))
        m = OfflineTtsMatchaModelConfig(
            acoustic_model=acoustic[0], vocoder=vocoder[0],
            lexicon=os.path.join(d, "lexicon.txt"),
            tokens=os.path.join(d, "tokens.txt"),
            dict_dir=os.path.join(d, "dict"))
        cfg = OfflineTtsConfig(
            model=OfflineTtsModelConfig(matcha=m, num_threads=num_threads),
            rule_fsts=fsts)
        _tts = OfflineTts(cfg)
        _tts_key = (model_dir, num_threads)
        return _tts


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.audio_file_type = "wav"
        self.tts_bin = config.get(
            "tts_bin",
            "/data/rknn-test/sherpa/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/bin/sherpa-onnx-offline-tts",
        )
        self.engine = config.get("engine", "matcha")
        self.model_dir = config.get("model_dir", "")
        self.sid = int(config.get("sid", 14))
        self.speed = float(config.get("speed", 1.0))
        self.num_threads = int(config.get("num_threads", 4))
        if self.engine == "matcha":
            # 预热: 用户说话期间后台加载模型, 首句不付 5.6 s
            threading.Thread(target=_get_tts,
                             args=(self.model_dir, self.num_threads),
                             daemon=True).start()

    async def text_to_speak(self, text, output_file):
        out = output_file or self.generate_filename(".wav")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if self.engine == "matcha":
            await asyncio.to_thread(self._synth_matcha, text, out)
        else:
            await asyncio.to_thread(self._synth_vits, text, out)
        if output_file is None:
            with open(out, "rb") as f:
                return f.read()

    def _synth_matcha(self, text, out):
        tts = _get_tts(self.model_dir, self.num_threads)
        audio = tts.generate(text, speed=self.speed)
        if len(audio.samples) < 200:
            raise Exception(f"matcha 合成结果过短: {len(audio.samples)} samples")
        # API 返回归一化浮点样本(-1.0~1.0), 直接转 int16 会全部截断成 0(静音),
        # 必须先判峰值再放大; int16 列表则原样通过
        import numpy as np
        arr = np.asarray(audio.samples, dtype=np.float64)
        peak = float(np.abs(arr).max())
        if peak <= 1.5:
            arr = arr * 32767.0
        data = arr.astype(np.int16).tobytes()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(audio.sample_rate)
            w.writeframes(data)

    def _synth_vits(self, text, out):
        d = self.model_dir
        fars = os.path.join(d, "rule.far")
        cmd = [
            self.tts_bin,
            f"--vits-model={os.path.join(d, 'vits-aishell3.int8.onnx')}",
            f"--vits-lexicon={os.path.join(d, 'lexicon.txt')}",
            f"--vits-tokens={os.path.join(d, 'tokens.txt')}",
            f"--sid={self.sid}",
            f"--speed={self.speed}",
            f"--num-threads={self.num_threads}",
            f"--output-filename={out}",
            text,
        ]
        if os.path.isfile(fars):
            cmd.append(f"--tts-rule-fars={fars}")
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        size = os.path.getsize(out) if os.path.exists(out) else 0
        if proc.returncode != 0 or size < 200:
            raise Exception(
                f"sherpa vits TTS 失败: rc={proc.returncode} size={size} "
                f"{proc.stderr[-300:] if proc.stderr else ''}")
