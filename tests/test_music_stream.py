"""Real FFmpeg/HTTP regression: first frame before EOF, cancellation and stalls."""
import asyncio
import importlib
import io
import json
import logging
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "board-server/pkg"))
from core.utils import music_stream
from core.providers.tts.dto.dto import ContentType


class StreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = shutil.which("ffmpeg")
        if not cls.ffmpeg:
            raise RuntimeError("ffmpeg required; this gate must not silently skip")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\x01\0" * 32000)
        cls.audio = output.getvalue()
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\x01\0" * 160000)
        cls.slow_audio = output.getvalue()
        cls.eof = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path == "/bad":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.end_headers()
                try:
                    if self.path == "/stall":
                        time.sleep(2)
                        return
                    if self.path == "/slow":
                        cls.eof.clear()
                    audio = cls.slow_audio if self.path == "/slow" else cls.audio
                    self.wfile.write(audio[:44])
                    data = audio[44:]
                    for offset in range(0, len(data), 2048):
                        self.wfile.write(data[offset:offset+2048])
                        self.wfile.flush()
                        if self.path == "/slow":
                            time.sleep(0.04)
                    if self.path == "/slow":
                        cls.eof.set()
                except (BrokenPipeError, ConnectionResetError):
                    pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def stream(self, path, **kwargs):
        return music_stream.pcm_frames(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            ffmpeg=self.ffmpeg, **kwargs)

    def test_first_frame_arrives_before_http_eof(self):
        self.eof.clear()
        stream = self.stream("/slow", cancelled=lambda: False)
        try:
            self.assertEqual(len(next(stream)), 1920)
            self.assertFalse(self.eof.is_set(), "decoded only after the entire download")
        finally:
            stream.close()

    def test_cancellation_reaps_decoder(self):
        children = []
        popen = subprocess.Popen
        def spawn(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child
        start = time.monotonic()
        with patch.object(music_stream.subprocess, "Popen", spawn):
            self.assertEqual(list(self.stream("/stall", cancelled=lambda: time.monotonic()-start > 0.2)), [])
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertLess(time.monotonic()-start, 1.5)

    def test_startup_stall_is_bounded(self):
        start = time.monotonic()
        with self.assertRaisesRegex(music_stream.MusicStreamError, "超时"):
            list(self.stream("/stall", cancelled=lambda: False, startup_timeout=0.25))
        self.assertLess(time.monotonic()-start, 1.5)

    def test_source_error_is_not_success(self):
        with self.assertRaises(music_stream.MusicStreamError):
            list(self.stream("/bad", cancelled=lambda: False))

    def test_local_url_is_not_an_online_stream(self):
        with self.assertRaises(music_stream.MusicStreamError):
            list(music_stream.pcm_frames("file:///etc/passwd", cancelled=lambda: False))

    def test_final_partial_frame_is_padded(self):
        frames = list(self.stream("/finite", cancelled=lambda: False))
        self.assertEqual(len(frames), 34)
        self.assertTrue(all(len(frame) == 1920 for frame in frames))

    def test_slow_consumer_has_bounded_queue_and_can_abort(self):
        stopped = threading.Event()
        conn = SimpleNamespace(stop_event=stopped, client_abort=False,
                               sentence_id="music-1", config={"plugins": {"play_music": {"ffmpeg_bin": self.ffmpeg}}},
                               logger=logging.getLogger("music-test"))
        provider = SimpleNamespace(conn=conn, tts_audio_queue=queue.Queue())
        message = SimpleNamespace(sentence_id="music-1", content_detail="test music",
                                  content_file=f"http://127.0.0.1:{self.server.server_port}/finite")
        errors = []
        def run():
            try:
                music_stream.play_stream(provider, message)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic()+3
        while provider.tts_audio_queue.qsize() < 8 and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertLessEqual(provider.tts_audio_queue.qsize(), 8)
        conn.client_abort = True
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])


    def test_music_yields_to_pending_answer(self):
        """2026-10-21: 回答排队时音乐必须让位。

        音乐流占着 TTS 文本线程(play_stream 在文本线程里阻塞), 电台又是无限流
        —— 不让位, 音乐期间的回答永远排在队列里出不来。
        """
        stopped = threading.Event()
        conn = SimpleNamespace(stop_event=stopped, client_abort=False,
                               sentence_id="music-1",
                               config={"plugins": {"play_music": {"ffmpeg_bin": self.ffmpeg}}},
                               logger=logging.getLogger("music-test"))
        provider = SimpleNamespace(conn=conn, tts_audio_queue=queue.Queue(),
                                   tts_text_queue=queue.Queue())
        message = SimpleNamespace(sentence_id="music-1", content_detail="test music",
                                  content_file=f"http://127.0.0.1:{self.server.server_port}/slow",
                                  is_radio=False)
        errors = []
        def run():
            try:
                music_stream.play_stream(provider, message)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 5
        while provider.tts_audio_queue.qsize() == 0 and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreater(provider.tts_audio_queue.qsize(), 0, "音乐应先推几帧")
        # 回答排队 -> 音乐让位
        provider.tts_text_queue.put(SimpleNamespace(content_type=ContentType.TEXT,
                                                     content_detail="现在几点了"))
        thread.join(6)
        self.assertFalse(thread.is_alive(), "音乐应让位退出, 不能一直占着 TTS 文本线程")
        self.assertEqual(errors, [])
        self.assertIs(provider.music_resume, message, "让位的音乐应被记住, 供回答结束后续播")

    def test_music_keeps_playing_when_only_its_own_markers_are_queued(self):
        """音乐自己的 FIRST/MIDDLE(STREAM)/LAST(ACTION) 不能被当成"回答"。

        音乐播放期间队里就杵着 LAST(ACTION); 误判成回答会让音乐一秒都放不出来。
        """
        stopped = threading.Event()
        conn = SimpleNamespace(stop_event=stopped, client_abort=False,
                               sentence_id="music-1",
                               config={"plugins": {"play_music": {"ffmpeg_bin": self.ffmpeg}}},
                               logger=logging.getLogger("music-test"))
        provider = SimpleNamespace(conn=conn, tts_audio_queue=queue.Queue(),
                                   tts_text_queue=queue.Queue())
        provider.tts_text_queue.put(SimpleNamespace(content_type=ContentType.ACTION))
        provider.tts_text_queue.put(SimpleNamespace(content_type=ContentType.LAST
                                                     if hasattr(ContentType, "LAST") else ContentType.ACTION))
        message = SimpleNamespace(sentence_id="music-1", content_detail="test music",
                                  content_file=f"http://127.0.0.1:{self.server.server_port}/finite",
                                  is_radio=False)
        errors = []
        def run():
            try:
                music_stream.play_stream(provider, message)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 5
        while provider.tts_audio_queue.qsize() < 8 and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(provider.tts_audio_queue.qsize(), 8, "音乐应照常推帧(背压上限 8)")
        self.assertIsNone(getattr(provider, "music_resume", None), "不该让位")
        conn.client_abort = True   # 收尾: 停掉还在推帧的生产者
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_music_stopped_by_new_turn_is_resumable(self):
        """新轮对话打断音乐 -> 记下待续播; 用户明说停止播放 -> 不续。

        区别在句 id: 新一轮对话会换 conn.sentence_id, 而前端的"停止播放"
        走 abort(句 id 不变)。
        """
        stopped = threading.Event()
        conn = SimpleNamespace(stop_event=stopped, client_abort=False,
                               sentence_id="music-1",
                               config={"plugins": {"play_music": {"ffmpeg_bin": self.ffmpeg}}},
                               logger=logging.getLogger("music-test"))
        provider = SimpleNamespace(conn=conn, tts_audio_queue=queue.Queue(),
                                   tts_text_queue=queue.Queue())
        message = SimpleNamespace(sentence_id="music-1", content_detail="test music",
                                  content_file=f"http://127.0.0.1:{self.server.server_port}/finite",
                                  is_radio=False)
        # 新轮对话: 播了一段之后句 id 变了(会话协调器 abort 旧轮)
        errors = []
        def run():
            try:
                music_stream.play_stream(provider, message)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 5
        while provider.tts_audio_queue.qsize() == 0 and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        conn.sentence_id = "music-2"     # 新一轮对话
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertIs(provider.music_resume, message, "新轮对话打断 -> 该续播")

        # 用户明说停止播放: abort(client_abort) 但句 id 没变
        conn2 = SimpleNamespace(stop_event=stopped, client_abort=False,
                                sentence_id="music-1",
                                config={"plugins": {"play_music": {"ffmpeg_bin": self.ffmpeg}}},
                                logger=logging.getLogger("music-test"))
        provider2 = SimpleNamespace(conn=conn2, tts_audio_queue=queue.Queue(),
                                    tts_text_queue=queue.Queue())
        conn2.client_abort = True
        music_stream.play_stream(provider2, message)
        self.assertIsNone(getattr(provider2, "music_resume", None), "用户停止 -> 不该续播")

    def test_real_tts_worker_streams_before_last(self):
        self.check_real_worker_stream(resume=False)

    def test_resume_runs_real_tts_worker_and_produces_audio(self):
        self.check_real_worker_stream(resume=True)

    def test_resume_waits_for_audio_last_completion(self):
        from core.providers.tts.base import TTSProviderBase
        from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType
        class Provider(TTSProviderBase):
            async def text_to_speak(self, text, output_file):
                raise AssertionError("not synthesizing in this test")
        provider = Provider({}, False)
        stopped = threading.Event()
        provider.conn = SimpleNamespace(stop_event=stopped, client_abort=False,
            sentence_id="answer", loop=None, max_output_size=0)
        provider.music_resume = TTSMessageDTO("old-music", SentenceType.MIDDLE,
            ContentType.STREAM, content_detail="radio", content_file="http://example.test/radio")
        provider._music_resume_content_seen = True
        provider.tts_audio_queue.put((SentenceType.LAST, [], None, "answer"))
        order = []
        resume = provider._resume_music_if_idle
        def after_audio():
            order.append("resume")
            resume()
            stopped.set()
        provider._resume_music_if_idle = after_audio
        def submit(coro, loop):
            coro.close()
            return SimpleNamespace(result=lambda: order.append("audio-completed"))
        with patch("core.providers.tts.base.asyncio.run_coroutine_threadsafe", side_effect=submit):
            provider._audio_play_priority_thread()
        self.assertEqual(order, ["audio-completed", "resume"])
        self.assertEqual([m.sentence_type for m in provider.tts_text_queue.queue],
                         [SentenceType.FIRST, SentenceType.MIDDLE, SentenceType.LAST])

    def test_continuation_does_not_close_client_turn(self):
        from core.handle import sendAudioHandle as sender
        from core.providers.tts.dto.dto import SentenceType
        logger = SimpleNamespace(bind=lambda **kw: SimpleNamespace(info=lambda *a: None))
        conn = SimpleNamespace(sentence_id="answer", session_id="session", client_abort=False,
            tts=SimpleNamespace(tts_audio_first_sentence=False), calling=False,
            close_after_chat=False, logger=logger, websocket=SimpleNamespace(send=AsyncMock()))
        with patch.object(sender, "sendAudio", new_callable=AsyncMock), \
             patch.object(sender, "_wait_for_audio_completion", new_callable=AsyncMock) as drain, \
             patch.object(sender, "send_tts_message", new_callable=AsyncMock) as notify:
            asyncio.run(sender.sendAudioMessage(conn, SentenceType.LAST, [], None,
                                                "answer", continue_music=True))
            drain.assert_awaited_once_with(conn)
            notify.assert_not_awaited()  # In particular, no whole-turn stop.
            marker = json.loads(conn.websocket.send.await_args.args[0])
            self.assertEqual((marker["state"], marker["continuation"]), ("sentence_end", "music"))
            asyncio.run(sender.sendAudioMessage(conn, SentenceType.LAST, [], None, "answer"))
            notify.assert_awaited_once_with(conn, "stop", None)

    def test_abort_clears_music_waiting_to_resume(self):
        from core.handle.abortHandle import handleAbortMessage
        logger = SimpleNamespace(bind=lambda **kw: SimpleNamespace(info=lambda *a: None))
        tts = SimpleNamespace(music_resume=object(), _music_resume_content_seen=True)
        conn = SimpleNamespace(logger=logger, tts=tts, session_id="session",
            clear_queues=lambda: None, clearSpeakStatus=lambda: None,
            websocket=SimpleNamespace(send=AsyncMock()))
        asyncio.run(handleAbortMessage(conn))
        self.assertTrue(conn.client_abort)
        self.assertIsNone(tts.music_resume)
        self.assertFalse(tts._music_resume_content_seen)

    def check_real_worker_stream(self, resume):
        from core.providers.tts.base import TTSProviderBase
        from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType
        class Provider(TTSProviderBase):
            async def text_to_speak(self, text, output_file):
                raise AssertionError("music should not synthesize speech")
        provider = Provider({}, False)
        stopped = threading.Event()
        provider.conn = SimpleNamespace(stop_event=stopped, client_abort=False,
            sentence_id="worker-1", config={"plugins": {"play_music": {"ffmpeg_bin": self.ffmpeg}}},
            logger=logging.getLogger("worker-test"))
        thread = threading.Thread(target=provider.tts_text_priority_thread)
        thread.start()
        url = f"http://127.0.0.1:{self.server.server_port}/finite"
        if resume:
            provider.music_resume = TTSMessageDTO("previous-turn", SentenceType.MIDDLE,
                ContentType.STREAM, content_detail="music", content_file=url)
            provider._music_resume_content_seen = True
            provider._resume_music_if_idle()
        else:
            provider.tts_text_queue.put(TTSMessageDTO("worker-1", SentenceType.FIRST, ContentType.ACTION))
            provider.tts_text_queue.put(TTSMessageDTO("worker-1", SentenceType.MIDDLE, ContentType.STREAM,
                content_detail="music", content_file=url))
            provider.tts_text_queue.put(TTSMessageDTO("worker-1", SentenceType.LAST, ContentType.ACTION))
        packets = 0
        try:
            while True:
                kind, audio, text, sentence = provider.tts_audio_queue.get(timeout=4)
                self.assertEqual(sentence, "worker-1")
                if kind == SentenceType.LAST:
                    break
                if isinstance(audio, bytes):
                    packets += 1
            self.assertEqual(packets, 34)
        finally:
            stopped.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.plugin = importlib.import_module("plugins_func.functions.play_music")
        self.plugin.MUSIC_CACHE.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = Path(self.temp.name) / "music"
        self.library.mkdir()
        logger = SimpleNamespace(bind=lambda **kw: logging.getLogger("plugin-test"))
        self.conn = SimpleNamespace(config={"plugins": {"play_music": {"music_dir": str(self.library)}}},
                                    sentence_id="m1", intent_type="intent_llm", logger=logger,
                                    tts=SimpleNamespace(tts_text_queue=queue.Queue(),
                                                        store_tts_text=lambda *args: None))

    def test_online_selection_queues_stream_without_cache_file(self):
        online = self.library.parent / "music-online"
        online.mkdir()
        (online / "playlist.json").write_text(json.dumps({"entries": [
            {"name": "演示曲", "url": "https://example.invalid/music.mp3"}]}))
        self.assertTrue(asyncio.run(self.plugin.handle_music_command(self.conn, "播放音乐 演示曲")))
        messages = list(self.conn.tts.tts_text_queue.queue)
        self.assertTrue(any(message.content_type == ContentType.STREAM for message in messages))
        self.assertEqual(list(self.library.iterdir()), [])

    def test_radio_request_selects_a_station(self):
        # 电台分支(线上实测踩过: 走到这里就 NameError, 网关只回"播放音乐时出错了")
        online = self.library.parent / "music-online"
        online.mkdir()
        (online / "playlist.json").write_text(json.dumps({"entries": [
            {"name": "轻音乐电台", "url": "http://ice1.somafm.com/groovesalad-128-mp3",
             "radio": True},
            {"name": "爵士电台", "url": "http://ice1.somafm.com/sonicuniverse-128-mp3",
             "radio": True}]}))
        self.assertTrue(asyncio.run(self.plugin.handle_music_command(self.conn, "畅音电台。")))
        streams = [m for m in self.conn.tts.tts_text_queue.queue
                   if m.content_type == ContentType.STREAM]
        self.assertTrue(streams)
        self.assertTrue(streams[0].is_radio)

    def test_named_radio_prefers_the_fuzzy_match(self):
        online = self.library.parent / "music-online"
        online.mkdir()
        (online / "playlist.json").write_text(json.dumps({"entries": [
            {"name": "轻音乐电台", "url": "http://ice1.somafm.com/groovesalad-128-mp3",
             "radio": True},
            {"name": "爵士电台", "url": "http://ice1.somafm.com/sonicuniverse-128-mp3",
             "radio": True}]}))
        self.assertTrue(asyncio.run(self.plugin.handle_music_command(self.conn, "播放爵士电台")))
        streams = [m for m in self.conn.tts.tts_text_queue.queue
                   if m.content_type == ContentType.STREAM]
        self.assertTrue(streams)
        self.assertIn("sonicuniverse", streams[0].content_file)

    def test_empty_library_reports_failure(self):
        self.assertFalse(asyncio.run(self.plugin.handle_music_command(self.conn, "随机播放音乐")))

    def test_offline_library_fallback(self):
        (self.library / "离线曲.p3").write_bytes(b"offline fixture")
        self.assertTrue(asyncio.run(self.plugin.handle_music_command(self.conn, "播放音乐 离线曲")))
        self.assertTrue(any(message.content_type == ContentType.FILE
                            for message in self.conn.tts.tts_text_queue.queue))

    def test_mcp_url_becomes_audio_stream(self):
        async def execute(tool, arguments):
            return {"structuredContent": {"play_url": "https://example.invalid/s.mp3",
                                           "song": {"title": arguments["query"]}}}
        manager = SimpleNamespace(is_mcp_tool=lambda name: True, execute_tool=execute)
        self.conn.func_handler = SimpleNamespace(server_mcp_executor=SimpleNamespace(mcp_manager=manager))
        self.assertTrue(asyncio.run(self.plugin.handle_music_command(self.conn, "播放音乐 测试歌")))
        self.assertTrue(any(message.content_type == ContentType.STREAM
                            for message in self.conn.tts.tts_text_queue.queue))

    def test_hidden_resolver_is_callable_without_exposing_resource_tools(self):
        async def execute(tool, arguments):
            return {"structuredContent": {"play_url": "https://example.invalid/s.mp3"}}
        manager = SimpleNamespace(is_mcp_tool=lambda name: False, execute_tool=execute,
                                  clients={"music": SimpleNamespace(has_tool=lambda name: name == "get_play_url")})
        self.conn.func_handler = SimpleNamespace(server_mcp_executor=SimpleNamespace(mcp_manager=manager))
        self.assertTrue(asyncio.run(self.plugin.handle_music_command(self.conn, "播放音乐 测试歌")))


if __name__ == "__main__":
    unittest.main()
