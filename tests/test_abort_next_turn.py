import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import queue
import sys
import threading
import uuid
from types import MethodType
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("speaking,mode", [(False, "manual"), (True, "manual"),
                                          (True, "auto")])
def test_new_intent_can_speak_after_explicit_abort(speaking, mode):
    source = Path(__file__).resolve().parents[1] / (
        "board-server/pkg/core/handle/receiveAudioHandle.py")
    tree = ast.parse(source.read_text())
    function = next(node for node in tree.body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "startToChat")
    connection = SimpleNamespace(need_bind=False, max_output_size=0, client_abort=True,
                                 client_is_speaking=speaking, client_listen_mode=mode)

    async def intent(conn, text):
        assert text == "现在几点了"
        assert conn.client_abort is False
        return True

    async def abort(conn):
        conn.client_abort = True

    abort_handler = AsyncMock(side_effect=abort)
    intent_handler = AsyncMock(side_effect=intent)
    namespace = {"json": json, "handle_user_intent": intent_handler,
                 "handleAbortMessage": abort_handler}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    asyncio.run(namespace["startToChat"](connection, "现在几点了"))
    intent_handler.assert_awaited_once()
    assert abort_handler.await_count == int(speaking and mode != "manual")


@pytest.mark.parametrize("invalidate", ["before", "during", "never"])
def test_synthesis_keeps_its_own_turn_and_discards_cancelled_work(monkeypatch, invalidate):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "board-server/pkg"))
    from core.providers.tts import base

    conn = SimpleNamespace(client_abort=False, stop_event=threading.Event(),
                           sentence_id="old", sample_rate=24000)
    output = queue.Queue()
    calls = []

    async def synthesize(text, path):
        calls.append(text)
        if invalidate == "during":
            conn.sentence_id = "new"
        return b"audio"

    def shared_handler(packet):
        raise AssertionError("must not stamp audio with a mutable shared turn ID")

    provider = SimpleNamespace(conn=conn, tts_audio_queue=output,
                               handle_opus=shared_handler, text_to_speak=synthesize,
                               audio_file_type="wav", opus_encoder=None,
                               current_sentence_id="old")

    def encode(audio, **kwargs):
        provider.current_sentence_id = "new"
        kwargs["callback"](b"packet")

    monkeypatch.setattr(base, "audio_bytes_to_data_stream", encode)
    if invalidate == "before":
        conn.sentence_id = "new"
    base._synth_one(provider, "text", "original", shared_handler, "old")
    if invalidate == "never":
        first, middle = output.get_nowait(), output.get_nowait()
        assert first[3] == middle[3] == "old"
        assert middle[1] == b"packet"
    else:
        assert output.empty()
    assert len(calls) == int(invalidate != "before")


@pytest.mark.parametrize("outcome", ["current", "stale", "timeout"])
def test_tool_result_cannot_answer_a_new_turn(outcome):
    source = Path(__file__).resolve().parents[1] / "board-server/pkg/core/connection.py"
    tree = ast.parse(source.read_text())
    connection_class = next(node for node in tree.body
                            if isinstance(node, ast.ClassDef) and node.name == "ConnectionHandler")
    methods = [node for node in connection_class.body if isinstance(node, ast.FunctionDef)
               and node.name in ("chat", "_handle_function_result")]
    quiet = SimpleNamespace(info=lambda *args: None, debug=lambda *args: None,
                            error=lambda *args: None)
    conn = SimpleNamespace(logger=SimpleNamespace(bind=lambda **kwargs: quiet),
                           sentence_id=None, client_abort=False, memory=None,
                           current_speaker="", system_introduced_speakers=set(),
                           session_id="session", loop=None, config={}, features={},
                           intent_type="function_call")
    conn.dialogue = SimpleNamespace(put=lambda *args: None,
                                    get_llm_dialogue_with_memory=lambda *args: [])
    spoken = []
    conn.tts = SimpleNamespace(tts_text_queue=queue.Queue(), store_tts_text=lambda *args: None,
        tts_one_sentence=lambda current, kind, **kwargs:
            spoken.append((kwargs["sentence_id"], current.sentence_id, kwargs["content_detail"])))
    conn.func_handler = SimpleNamespace(get_functions=lambda: [],
                                        handle_llm_function_call=lambda *args: object())
    calls = [{"name": "slow_tool", "id": "call", "arguments": "{}"}]
    conn.llm = SimpleNamespace(response_with_functions=lambda *args, **kwargs: iter([(None, calls)]))
    conn._merge_tool_calls = lambda destination, tools: destination.extend(tools)

    class ToolFuture:
        def result(self, timeout):
            if outcome != "current":
                conn.client_abort = True
                conn.sentence_id = None
                conn.sentence_id = "new-turn"
                conn.client_abort = False
            if outcome == "timeout":
                raise TimeoutError("old tool timed out after abort")
            return SimpleNamespace(action="response", response="old answer", result="old")

    namespace = dict(TAG="test", uuid=uuid, json=json, Message=lambda **kwargs: SimpleNamespace(**kwargs),
        TTSMessageDTO=lambda **kwargs: SimpleNamespace(**kwargs),
        SentenceType=SimpleNamespace(FIRST="first", LAST="last", MIDDLE="middle"),
        ContentType=SimpleNamespace(ACTION="action", TEXT="text"), DIRECT_ANSWER_TOOL={},
        asyncio=SimpleNamespace(run_coroutine_threadsafe=lambda *args: ToolFuture()),
        enqueue_tool_report=lambda *args, **kwargs: None, ActionResponse=SimpleNamespace,
        Action=SimpleNamespace(RESPONSE="response", NOTFOUND="notfound", ERROR="error",
                               REQLLM="reqllm", RECORD="record"))
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
    conn._handle_function_result = MethodType(namespace["_handle_function_result"], conn)
    namespace["chat"](conn, "old request")
    if outcome == "current":
        assert spoken == [(conn.sentence_id, conn.sentence_id, "old answer")]
    else:
        assert spoken == []
