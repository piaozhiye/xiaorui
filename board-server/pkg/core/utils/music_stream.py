"""Bounded, cancellable FFmpeg HTTP decoding. No downloaded media files."""
import glob
import os
import selectors
import shutil
import subprocess
import time
import re
import threading
from functools import lru_cache
from urllib.parse import urlsplit


class MusicStreamError(RuntimeError):
    pass


@lru_cache(maxsize=8)
def find_ffmpeg(configured=""):
    candidates = [configured, os.environ.get("XR_FFMPEG", "")]
    candidates += ["/data/xiaorui-ffmpeg/bin/ffmpeg"]
    candidates += sorted(glob.glob(
        "/data/xiaorui-gateway/deps/ffmpeg-runtime/imageio_ffmpeg/binaries/ffmpeg-*"))
    candidates += ["/data/miniforge/bin/ffmpeg", shutil.which("ffmpeg")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            result = subprocess.run([candidate, "-version"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=3)
            if result.returncode == 0:
                return candidate
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise MusicStreamError("没有可运行的FFmpeg")


def pcm_frames(url, *, cancelled, ffmpeg="", startup_timeout=7, idle_timeout=7):
    """Yield complete 60ms/16k/mono/PCM16 frames; reap child even on cancellation."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise MusicStreamError("音乐地址必须为HTTP(S)")
    # rw_timeout 是单次网络读超时: 消费限速期 pipe 背压会让 ffmpeg 停读网络,
    # 7s 会误杀健康流(实机电台 11s 断, 直连同流 30s 稳) -> 跟随 idle_timeout
    # 由调用方按流型给值(歌曲 7s, 电台 30s)。
    command = [find_ffmpeg(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error",
               "-rw_timeout", str(int(idle_timeout * 1_000_000)),
               "-protocol_whitelist", "http,https,tcp,tls,crypto",
               "-probesize", "65536", "-analyzeduration", "1000000",
               "-i", url, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
               "-f", "s16le", "pipe:1"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    diagnostic = bytearray()
    def drain_errors():
        while True:
            chunk = process.stderr.read(1024)
            if not chunk:
                return
            diagnostic.extend(chunk)
            del diagnostic[:-4096]
    reader = threading.Thread(target=drain_errors, daemon=True)
    reader.start()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    os.set_blocking(process.stdout.fileno(), False)
    pending = bytearray()
    frame_bytes = 1920
    last_data = time.monotonic()
    received = False
    try:
        while not cancelled():
            limit = idle_timeout if received else startup_timeout
            if time.monotonic() - last_data > limit:
                raise MusicStreamError("音乐源首包超时" if not received else "音乐源读取超时")
            if not selector.select(0.1):
                continue
            chunk = os.read(process.stdout.fileno(), frame_bytes * 4)
            if not chunk:
                code = process.wait(timeout=1)
                if code or not received:
                    reader.join(timeout=1)
                    detail = re.sub(r"https?://\S+", "<url>", diagnostic.decode(errors="replace"))[-500:]
                    raise MusicStreamError(f"音乐解码失败(rc={code}): {detail}")
                if pending:
                    if len(pending) % 2:
                        raise MusicStreamError("PCM尾包长度错误")
                    yield bytes(pending).ljust(frame_bytes, b"\0")
                return
            received = True
            last_data = time.monotonic()
            pending.extend(chunk)
            while len(pending) >= frame_bytes:
                if cancelled():
                    return
                frame = bytes(pending[:frame_bytes])
                del pending[:frame_bytes]
                yield frame
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        reader.join(timeout=1)
        process.stderr.close()


def play_stream(provider, message):
    """Called on the existing TTS text worker, never on the websocket event loop."""
    from core.utils.opus_encoder_utils import OpusEncoderUtils
    from core.providers.tts.dto.dto import ContentType, SentenceType

    conn = provider.conn
    sentence_id = message.sentence_id
    provider.music_sentence_id = sentence_id
    config = conn.config.get("plugins", {}).get("play_music", {})
    def cancelled():
        return (conn.stop_event.is_set() or conn.client_abort or
                conn.sentence_id != sentence_id)

    def answer_pending():
        """音乐让位(2026-10-21): 回答已排队 -> 先让回答播, 播完再续音乐。

        背景: 音乐流是**占着 TTS 文本线程**跑的(play_stream 在文本线程里阻塞),
        音乐期间来的回答只能排在队列里等 —— 电台是无限流, 等于永远等不到。
        所以音乐流自己盯一下文本队列: 出现**待播正文**(TEXT/FILE)就主动让位。

        只认 TEXT/FILE 是关键: 音乐自己也往队列里排了 FIRST(ACTION) /
        MIDDLE(STREAM) / LAST(ACTION) 三个消息, 音乐播放期间队里就杵着一个
        LAST(ACTION); 把它当"回答"会让音乐一秒都放不出来。
        """
        try:
            pending = list(provider.tts_text_queue.queue)
        except AttributeError:
            return False
        return any(getattr(m, "content_type", None) in (ContentType.TEXT, ContentType.FILE)
                   for m in pending)

    def bounded_wait(seconds):
        deadline = time.monotonic() + seconds
        while not cancelled() and time.monotonic() < deadline:
            conn.stop_event.wait(min(0.05, max(0, deadline - time.monotonic())))

    encoder = OpusEncoderUtils(16000, 1, 60)
    encoder.encoder.signal = 3002  # OPUS_SIGNAL_MUSIC, distinct from speech TTS.
    start = time.monotonic()
    first = None
    count = 0
    # 电台是无限流: 背压容忍窗口放宽到 30s, 且断流(EOF/网络掐)自动重连,
    # 重连间隙 1s, 只有取消/连败 3 次才结束。歌曲仍用 7s 单次不重连。
    is_radio = bool(getattr(message, "is_radio", False))
    idle = 30 if is_radio else 7
    attempts = 3 if is_radio else 1
    yielded = False
    try:
        for attempt in range(attempts):
            stream = pcm_frames(message.content_file, cancelled=cancelled,
                                ffmpeg=config.get("ffmpeg_bin", ""),
                                idle_timeout=idle)
            reconnect = False
            exhausted = False
            try:
                for frame in stream:
                    if cancelled():
                        break
                    if answer_pending():
                        # 让位: 停流, 记住这段音乐, 回答播完由 TTS 文本线程续播
                        yielded = True
                        provider.music_resume = message
                        conn.logger.info("music yield: 回答已排队, 音乐让位")
                        break
                    # Limit both producer lookahead and TTS-queue occupancy. The network
                    # rate controller is downstream and otherwise accepts unbounded data.
                    if first is not None:
                        bounded_wait(max(0, first + count * 0.06 - time.monotonic() - 0.24))
                    while not cancelled() and provider.tts_audio_queue.qsize() >= 8:
                        # 背压等待里也得看让位信号: 队列满时生产者卡在这个循环里,
                        # 不到下一帧就检查不到 -> 回答永远排不上(2026-10-21 修)
                        if answer_pending():
                            yielded = True
                            provider.music_resume = message
                            conn.logger.info("music yield: 回答已排队, 音乐让位")
                            break
                        bounded_wait(0.05)
                    if yielded:
                        break
                    if cancelled():
                        break
                    packet = encoder.encoder.encode(frame, 960)
                    if not packet:
                        raise MusicStreamError("音乐Opus编码失败")
                    if first is None:
                        first = time.monotonic()
                        provider.tts_audio_queue.put((SentenceType.FIRST, None,
                                                      message.content_detail, sentence_id))
                        conn.logger.info(f"music stream first_audio={first-start:.3f}s")
                    provider.tts_audio_queue.put((SentenceType.MIDDLE, packet, None, sentence_id))
                    conn.last_activity_time = time.time() * 1000
                    count += 1
                else:
                    exhausted = True   # 生成器正常跑完(EOF): 歌曲=播完, 电台=断流
            except MusicStreamError as error:
                if cancelled() or attempt + 1 >= attempts:
                    raise
                reconnect = True
                conn.logger.warning(f"电台断流({error}), 1s 后重连 attempt={attempt + 1}")
            finally:
                stream.close()
            if cancelled():
                break
            if yielded:
                break   # 让位: 不重连, 等回答播完后由文本线程重新排队续播
            if is_radio and exhausted and count > 0 and attempt + 1 < attempts:
                conn.logger.warning(f"电台流EOF(已播{count * 0.06:.0f}s), 1s 后重连")
                bounded_wait(1.0)
                continue
            if reconnect:
                bounded_wait(1.0)
            else:
                break   # 歌曲播完
    finally:
        encoder.close()
        # 清标记: 定时到点的音乐等待循环靠它判断流是否结束(取消路径没有
        # LAST 音频, 不清会挂到上限)
        if getattr(provider, "music_sentence_id", None) == sentence_id:
            provider.music_sentence_id = None
        # 续播判定(2026-10-21): 音乐要分两种断法
        #   1) 因为**新一轮对话**被打断(句 id 变了): 答完该续播(让位语义);
        #   2) 用户明说"停止播放"(前端发 abort -> client_abort, 句 id 没变): 不续。
        # 只在真播过音频(count>0)时才记, 否则源失败会反复重排、反复报"暂时无法播放"。
        if count > 0 and (yielded or conn.sentence_id != sentence_id):
            provider.music_resume = message
            conn.logger.info("music yield: 记下待续播(新轮对话打断)")
        else:
            provider.music_resume = None
        conn.logger.info(f"music stream finished packets={count} cancelled={cancelled()}")
