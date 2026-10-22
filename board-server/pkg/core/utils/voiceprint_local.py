"""本地声纹识别 Provider(2026-10-16): sherpa-onnx 说话人嵌入, 板内推理。

替换上游的外部 HTTP 声纹服务(需第三方 URL+key, 我们没有)。接口兼容:
identify_speaker(wav_bytes, session_id) -> Optional[名字]。
上游钩子(asr.base 每轮 ASR 后)与个性化注入(connection: 首次出现的
说话人名字注入 system prompt)原样复用。

注册语音入库存 {name}.json(向量+元数据); 识别=余弦最近邻过阈值。
模型: 3dspeaker campplus zh-cn 16k(sherpa release, 27.6MB)。
"""
import asyncio
import json
import math
import os
import wave

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

_EXTRACTOR = None
_EXTRACTOR_MODEL = ""
_EXTRACTOR_LOCK = __import__("threading").Lock()


def _get_extractor(model_path: str):
    global _EXTRACTOR, _EXTRACTOR_MODEL
    if _EXTRACTOR is None or _EXTRACTOR_MODEL != model_path:
        import sherpa_onnx
        # 1.13.8 python API: SpeakerEmbeddingExtractor(非 Offline 前缀),
        # compute() 直接返回嵌入向量(无独立 embedding 方法)
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=model_path, num_threads=2, provider="cpu")
        try:
            _EXTRACTOR = sherpa_onnx.SpeakerEmbeddingExtractor(config)
            _EXTRACTOR_MODEL = model_path
        except Exception as e:
            # 1.13.8 无模型自检方法, 构造抛异常即模型坏
            _EXTRACTOR = None
            logger.bind(tag=TAG).error(f"声纹模型加载失败: {e}")
    return _EXTRACTOR


def _wav_to_samples(wav_bytes: bytes):
    """16k/mono/s16 wav -> float32 samples; 其他格式返回 None。"""
    import io
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
                logger.bind(tag=TAG).warning(
                    f"声纹音频格式非预期: {w.getframerate()}Hz/{w.getnchannels()}ch")
                return None
            raw = w.readframes(w.getnframes())
    except Exception as e:
        logger.bind(tag=TAG).warning(f"声纹音频解析失败: {e}")
        return None
    n = len(raw) // 2
    if n < 16000 * 1.5:      # <1.5s 声音太短, 嵌入不稳
        return None
    return [int.from_bytes(raw[i * 2:i * 2 + 2], "little", signed=True) / 32768.0
            for i in range(n)]


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return dot / (na * nb)


class LocalVoiceprintProvider:
    """与上游 VoiceprintProvider 同形: .enabled / identify_speaker()。"""

    def __init__(self, config: dict):
        self.model = config.get("model", "/data/rknn-test/sherpa/model-voiceprint/campplus.onnx")
        self.threshold = float(config.get("threshold", 0.62))
        self.enroll_dir = config.get("enroll_dir", "/data/xiaorui-gateway/server/data/voiceprints")
        self.enabled = os.path.exists(self.model)
        if not self.enabled:
            logger.bind(tag=TAG).warning(f"本地声纹模型缺失, 禁用: {self.model}")
        else:
            os.makedirs(self.enroll_dir, exist_ok=True)
            logger.bind(tag=TAG).info(
                f"本地声纹已启用: 模型={os.path.basename(self.model)} "
                f"阈值={self.threshold} 已注册={len(self.list_enrolled())}人")

    # ---- 兼容上游钩子 ----
    async def identify_speaker(self, audio_data: bytes, session_id: str):
        return await asyncio.to_thread(self._identify_sync, audio_data)

    # ---- 注册(意图调用) ----
    def enroll(self, name: str, wav_bytes: bytes) -> bool:
        if not self.enabled or not name:
            return False
        vector = self._embed(wav_bytes)
        if vector is None:
            return False
        safe = "".join(c for c in name if c not in '/\\:*?"<>| ').strip()
        if not safe:
            return False
        path = os.path.join(self.enroll_dir, f"{safe}.json")
        data = {"name": safe, "vector": vector}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        logger.bind(tag=TAG).info(f"声纹注册: {safe} (dim={len(vector)})")
        return True

    def list_enrolled(self):
        try:
            return [f[:-5] for f in os.listdir(self.enroll_dir) if f.endswith(".json")]
        except OSError:
            return []

    # ---- 内部 ----
    def _embed(self, wav_bytes: bytes):
        extractor = _get_extractor(self.model)
        if extractor is None:
            return None
        samples = _wav_to_samples(wav_bytes)
        if samples is None:
            return None
        import array as _array
        # 提取器单实例, 并发轮次(重连重叠)下 compute 非文档保证线程安全
        with _EXTRACTOR_LOCK:
            stream = extractor.create_stream()
            stream.accept_waveform(16000, _array.array("f", samples))
            vector = extractor.compute(stream)
        if vector is None:
            return None
        return [float(x) for x in vector]

    def _identify_sync(self, wav_bytes: bytes):
        # 无人注册时直接返回: 嵌入推理(约200ms CPU)不该在空库上每轮白烧
        if not self.list_enrolled():
            return None
        vector = self._embed(wav_bytes)
        if vector is None:
            return None
        best_name, best_score = None, 0.0
        try:
            files = [f for f in os.listdir(self.enroll_dir) if f.endswith(".json")]
        except OSError:
            return None
        for f in files:
            try:
                with open(os.path.join(self.enroll_dir, f), encoding="utf-8") as fp:
                    data = json.load(fp)
                score = _cos(vector, data.get("vector", []))
                if score > best_score:
                    best_name, best_score = data.get("name", f[:-5]), score
            except (OSError, ValueError):
                continue
        if best_name and best_score >= self.threshold:
            logger.bind(tag=TAG).info(f"声纹识别: {best_name} ({best_score:.3f})")
            return best_name
        logger.bind(tag=TAG).info(f"声纹未命中 (best={best_score:.3f})")
        return None
