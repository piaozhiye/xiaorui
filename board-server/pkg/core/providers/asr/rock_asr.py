"""RockASR provider: 板内 NPU 上的 Rockchip ASR (RockX2 框架).

config:
    ASR:
      RockASR:
        type: rock_asr
        lib_dir: /data/xiaorui-server/rockasr/lib
        model_dir: /data/xiaorui-server/rockasr/data
        auth_config: /data/xiaorui-server/rockasr/rockx_auth_config.json
        lang: zh
        timeout_s: 20

音频走网关 opus 解码后的 16k/mono/int16 PCM(artifacts.pcm_bytes),
与 RockASR 要求一致, 无需重采样。模型放在 helper 进程里, 但**不常驻**:
2GB 板子装不下多个 NPU 模型同时在(见 core/providers/rock/runtime.py),
由 runtime 槽位按实际可用内存仲裁 —— 空闲就卸载, 用之前按需拉起(~3s)。
"""
import asyncio
import os

from core.providers.asr.base import ASRProviderBase
from core.providers.rock import runtime
from core.providers.rock.helper_proc import HelperError, HelperProcess
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.interface_type = None
        from core.providers.asr.dto.dto import InterfaceType
        self.interface_type = InterfaceType.LOCAL
        self.delete_audio_file = delete_audio_file

        # 基类 speech_to_text_wrapper 会做 self.output_dir 的磁盘空间检查
        self.output_dir = config.get("output_dir", "tmp/asr")
        os.makedirs(self.output_dir, exist_ok=True)
        self.lib_dir = config.get("lib_dir")
        self.model_dir = config.get("model_dir")
        self.auth_config = config.get("auth_config")
        self.lang = config.get("lang", "zh")
        self.timeout_s = float(config.get("timeout_s", 20))
        if not (self.lib_dir and self.model_dir and self.auth_config):
            raise ValueError("RockASR 需要 lib_dir/model_dir/auth_config 配置")

        # 2GB 板子装不下多个 NPU 模型同时驻留(见 core/providers/rock/runtime.py),
        # 所以 helper 不常驻: 由 runtime 槽位按实际可用内存仲裁。
        # ASR 模型 ~591MB, 空闲 idle_s 后卸载, 下次用时再拉起(~3s,
        # 靠 listen start 时的 prefetch 藏在用户说话时间里)。
        self.idle_s = float(config.get("idle_s", 30))
        self.slot = runtime.get_slot(
            "asr",
            factory=lambda: HelperProcess(
                "asr_helper.py",
                ["--lib-dir", self.lib_dir, "--data-dir", self.model_dir,
                 "--auth-config", self.auth_config, "--lang", self.lang,
                 "--timeout", self.timeout_s],
                env_extra={"LD_LIBRARY_PATH": self.lib_dir},
            ),
            footprint_mb=600,
            idle_timeout_s=self.idle_s,
        )
        # 预热: 启动即拉起 helper 加载模型, 首句不付 ~3s
        import threading
        threading.Thread(target=self._warm, daemon=True).start()

    def prefetch(self):
        """语音开始时提前把模型拉起来(非阻塞)。"""
        self.slot.prefetch()

    def _warm(self):
        try:
            self.slot.acquire()
            logger.bind(tag=TAG).info("RockASR helper 就绪")
        except Exception as e:  # noqa: BLE001
            logger.bind(tag=TAG).error(f"RockASR helper 预热失败: {e}")

    def _transcribe(self, pcm: bytes) -> str:
        helper = self.slot.acquire()
        header, _ = helper.request({"cmd": "asr"}, body=pcm,
                                    timeout=self.timeout_s + 30)
        text = header.get("text", "")
        logger.bind(tag=TAG).info(
            f"RockASR 识别({header.get('elapsed_s')}s): {text}")
        return text

    async def speech_to_text(self, opus_data, session_id, artifacts=None):
        try:
            if artifacts is None or not artifacts.pcm_bytes:
                return "", None
            text = await asyncio.to_thread(self._transcribe, artifacts.pcm_bytes)
            return text, None
        except HelperError as e:
            logger.bind(tag=TAG).error(f"RockASR 失败: {e}")
            raise
