"""Gateway ASR for manual-listen 16k PCM, using an existing local paraformer model."""
import asyncio
from pathlib import Path
import threading

import numpy as np
import sherpa_onnx
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType


class ASRProvider(ASRProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__()
        self.interface_type = InterfaceType.LOCAL
        self.output_dir = config.get("output_dir", "tmp/asr_gateway")
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)
        self.delete_audio_file = delete_audio_file
        root = Path(config["model_dir"])
        paths = [root / name for name in ("encoder.int8.onnx", "decoder.int8.onnx", "tokens.txt")]
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(path)
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_paraformer(
            encoder=str(paths[0]), decoder=str(paths[1]), tokens=str(paths[2]),
            num_threads=2, sample_rate=16000, feature_dim=80,
        )
        self.lock = threading.Lock()

    def _recognize(self, data):
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        if not 0 < len(samples) <= 16000 * 16:
            return ""
        with self.lock:
            stream = self.recognizer.create_stream()
            stream.accept_waveform(16000, samples)
            stream.accept_waveform(16000, np.zeros(8000, dtype=np.float32))
            stream.input_finished()
            while self.recognizer.is_ready(stream):
                self.recognizer.decode_stream(stream)
            return self.recognizer.get_result(stream).strip()

    async def speech_to_text(self, pcm_frames, session_id, artifacts=None):
        data = artifacts.pcm_bytes if artifacts else b"".join(pcm_frames)
        return await asyncio.to_thread(self._recognize, data), None
