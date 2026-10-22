"""Run the real vendored route/listen methods without model or cloud dependencies."""
import ast
import asyncio
from enum import Enum
from pathlib import Path
import queue
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1] / "board-server/pkg/core"


class InterfaceType(Enum):
    LOCAL = "LOCAL"
    NON_STREAM = "NON_STREAM"
    STREAM = "STREAM"


def load_method(path, name, namespace):
    tree = ast.parse(path.read_text())
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == name)
    module = ast.Module(body=[method], type_ignores=[])
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


class Logger:
    def bind(self, **kwargs):
        return self

    def debug(self, *args):
        pass

    def info(self, *args):
        pass


class ManualAudioTest(unittest.IsolatedAsyncioTestCase):
    async def test_order_and_stop(self):
        for kind in (InterfaceType.LOCAL, InterfaceType.NON_STREAM):
            results = []

            async def stop(conn, frames):
                results.append(frames)

            namespace = {"asyncio": asyncio, "Dict": dict, "Any": object,
                         "InterfaceType": InterfaceType, "TAG": "test"}
            handle = load_method(ROOT / "handle/textHandler/listenMessageHandler.py",
                                 "handle", namespace)

            async def handle_text(conn, message):
                await handle(None, conn, {"state": message})

            namespace["handleTextMessage"] = handle_text
            route = load_method(ROOT / "connection.py", "_route_message", namespace)
            event = asyncio.Event()
            event.set()
            conn = SimpleNamespace(bind_completed_event=event, need_bind=False,
                                   vad=None, client_listen_mode="manual",
                                   conn_from_mqtt_gateway=False, asr_audio=[],
                                   asr_audio_queue=queue.Queue(), logger=Logger(),
                                   asr=SimpleNamespace(interface_type=kind, handle_voice_stop=stop))
            conn._decode_opus_packet = lambda frame: frame
            conn.reset_audio_states = lambda: conn.asr_audio.clear()
            await route(conn, b"first PCM")
            await route(conn, b"last PCM")
            await route(conn, "stop")
            self.assertEqual(results, [[b"first PCM", b"last PCM"]])
            self.assertTrue(conn.asr_audio_queue.empty())
            self.assertEqual(conn.asr_audio, [])
            await route(conn, "stop")
            self.assertEqual(len(results), 1)  # duplicate stop cannot recognize twice
            conn.client_listen_mode = "auto"
            conn.vad = object()
            await route(conn, b"queued PCM")
            self.assertEqual(conn.asr_audio_queue.get_nowait(), b"queued PCM")
            conn.client_listen_mode = "manual"
            conn.vad = None
            conn.asr.interface_type = InterfaceType.STREAM
            await route(conn, b"streaming PCM without VAD")
            self.assertTrue(conn.asr_audio_queue.empty())
            conn.asr.interface_type = kind
            conn.client_listen_mode = "manual"
            conn.closed = False

            async def close():
                conn.closed = True

            conn.close = close
            await route(conn, b"x" * (16000 * 2 * 16 + 1))
            self.assertTrue(conn.closed)
            self.assertEqual(conn.asr_audio, [])


if __name__ == "__main__":
    unittest.main()
