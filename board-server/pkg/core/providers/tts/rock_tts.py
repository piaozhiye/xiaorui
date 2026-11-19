"""RockTTS provider: 板内 NPU 上的 Rockchip TTS (RockTTSV1).

config:
    TTS:
      RockTTS:
        type: rock_tts
        lib_dir: /data/xiaorui-server/rocktts/lib
        model_dir: /data/xiaorui-server/rocktts/data
        auth_config: /data/xiaorui-server/rocktts/rockx_auth_config.json
        alpha: 1.0
        timeout_s: 30

输出 24kHz/mono/int16 wav —— 与网关 conn.sample_rate=24000 一致,
基类直接重采样/编 opus。模型放在 helper 进程里, 但**不常驻**:
2GB 板子装不下多个 NPU 模型同时在(见 core/providers/rock/runtime.py),
由 runtime 槽位按实际可用内存仲裁 —— 空闲就卸载, 用之前按需拉起(~3.5s)。
"""
import asyncio
import os
import wave

from core.providers.tts.base import TTSProviderBase
from core.providers.rock import runtime
from core.providers.rock.helper_proc import HelperError, HelperProcess
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

SAMPLE_RATE = 24000


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.audio_file_type = "wav"
        self.lib_dir = config.get("lib_dir")
        self.model_dir = config.get("model_dir")
        self.auth_config = config.get("auth_config")
        self.alpha = float(config.get("alpha", 1.0))
        self.timeout_s = float(config.get("timeout_s", 30))
        if not (self.lib_dir and self.model_dir and self.auth_config):
            raise ValueError("RockTTS 需要 lib_dir/model_dir/auth_config 配置")

        # 2GB 板子装不下多个 NPU 模型同时驻留(见 core/providers/rock/runtime.py),
        # 所以 helper 不常驻: 由 runtime 槽位按实际可用内存仲裁。
        # TTS 模型 ~229MB, 空闲 idle_s 后卸载。
        self.idle_s = float(config.get("idle_s", 60))
        self.slot = runtime.get_slot(
            "tts",
            factory=lambda: HelperProcess(
                "tts_helper.py",
                ["--lib-dir", self.lib_dir, "--data-dir", self.model_dir,
                 "--auth-config", self.auth_config,
                 "--alpha", self.alpha, "--timeout", self.timeout_s],
                env_extra={"LD_LIBRARY_PATH": self.lib_dir},
            ),
            footprint_mb=250,
            idle_timeout_s=self.idle_s,
        )
        # 预热: 启动即拉起 helper 加载模型, 首句不付 ~3.5s
        import threading
        threading.Thread(target=self._warm, daemon=True).start()

    def prefetch(self):
        """提前把模型拉起来(非阻塞): LLM 出第一句时用得上。"""
        self.slot.prefetch()

    def _warm(self):
        try:
            self.slot.acquire()
            logger.bind(tag=TAG).info("RockTTS helper 就绪")
        except Exception as e:  # noqa: BLE001
            logger.bind(tag=TAG).error(f"RockTTS helper 预热失败: {e}")

    def _synthesize(self, text, out_path):
        header, pcm = self.slot.request(
            {"cmd": "tts", "text": text, "alpha": self.alpha},
            timeout=self.timeout_s + 30)
        if len(pcm) < 400:
            raise HelperError(f"RockTTS 合成结果过短: {len(pcm)} 字节")
        with wave.open(out_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(header.get("sample_rate", SAMPLE_RATE))
            w.writeframes(pcm)
        logger.bind(tag=TAG).info(
            f"RockTTS 合成({header.get('elapsed_s')}s): {len(pcm)//2} 样本")

    async def text_to_speak(self, text, output_file):
        out = output_file or self.generate_filename(".wav")
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        await asyncio.to_thread(self._synthesize, text, out)
        if output_file is None:
            with open(out, "rb") as f:
                return f.read()
