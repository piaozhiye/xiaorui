"""Connection-owned unsolicited playback, bounded and acknowledged before TTS.

No audio/model dependencies. All methods run on the connection's asyncio loop.
Old clients retain an idle-only legacy path; they never receive offer messages.
"""
import asyncio
from dataclasses import dataclass
import json
import uuid


@dataclass
class PlaybackRequest:
    id: str
    text: str
    deadline: float
    result: asyncio.Future


class SessionCoordinator:
    def __init__(self, conn, speak, *, queue_limit=8, ack_timeout=2, finish_timeout=60):
        self.conn = conn
        self.speak = speak
        self.queue = asyncio.PriorityQueue(maxsize=queue_limit)
        self.ack_timeout = ack_timeout
        self.finish_timeout = finish_timeout
        self.sequence = 0
        self.worker = None
        self.offer_id = None
        self.ack = None
        self.finished = None
        self.sentence_id = None

    async def request(self, text, *, priority=10, ttl=15, request_id=None):
        loop = asyncio.get_running_loop()
        request = PlaybackRequest(request_id or uuid.uuid4().hex, text,
                                  loop.time() + ttl, loop.create_future())
        if self.queue.full() or self.conn.stop_event.is_set():
            return False
        self.sequence += 1
        self.queue.put_nowait((priority, self.sequence, request))
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._run())
        try:
            return await asyncio.shield(request.result)
        except asyncio.CancelledError:
            request.result.cancel()
            raise

    def acknowledge(self, request_id, accepted):
        if request_id == self.offer_id and self.ack is not None and self.ack.done():
            return False  # Duplicate ACK must not cancel an already accepted offer.
        if request_id != self.offer_id or self.ack is None or self.ack.done():
            if accepted is True and isinstance(request_id, str) and len(request_id) == 32:
                asyncio.create_task(self._offer_state(request_id, "cancel"))
            return False
        self.ack.set_result(accepted is True)
        return True

    async def _offer_state(self, request_id, state):
        try:
            await self.conn.websocket.send(json.dumps({"type": "announce", "state": state,
                "request_id": request_id, "session_id": self.conn.session_id}))
            return True
        except Exception:
            return False  # Connection cleanup releases the reservation.

    def complete(self, sentence_id, success=True):
        if sentence_id != self.sentence_id or self.finished is None or self.finished.done():
            return False
        self.finished.set_result(success)
        return True

    def idle(self):
        tts = getattr(self.conn, "tts", None)
        return (tts is not None and not self.conn.client_is_speaking and
                not getattr(tts, "music_sentence_id", None) and
                tts.tts_text_queue.empty() and tts.tts_audio_queue.empty())

    async def _reserve(self, request):
        loop = asyncio.get_running_loop()
        while loop.time() < request.deadline and not self.conn.stop_event.is_set():
            if request.result.cancelled():
                return False
            if not self.idle():
                await asyncio.sleep(.1)
                continue
            if not self.conn.features.get("announce_ack", False):
                await self.conn.websocket.send(json.dumps({"type": "announce",
                    "session_id": self.conn.session_id}))
                return True
            self.offer_id = uuid.uuid4().hex  # Late ACK from an earlier offer is invalid.
            self.ack = loop.create_future()
            await self.conn.websocket.send(json.dumps({"type": "announce", "state": "offer",
                "request_id": self.offer_id, "message_id": request.id,
                "session_id": self.conn.session_id}))
            try:
                if await asyncio.wait_for(self.ack, min(self.ack_timeout,
                                                       request.deadline - loop.time())):
                    return await self._offer_state(self.offer_id, "grant")
            except asyncio.TimeoutError:
                await self._offer_state(self.offer_id, "cancel")
            finally:
                self.offer_id = None
                self.ack = None
            await asyncio.sleep(.1)
        return False

    async def _play(self, request):
        if not await self._reserve(request):
            return False
        # No await between reservation and assignment: another user turn cannot
        # interleave on this loop and have its sentence id overwritten.
        self.sentence_id = request.id
        self.finished = asyncio.get_running_loop().create_future()
        self.conn.sentence_id = request.id
        self.conn.client_abort = False
        self.conn.client_is_speaking = True
        try:
            self.speak(self.conn, request.text)
            deadline = asyncio.get_running_loop().time() + self.finish_timeout
            while (not self.finished.done() and not self.conn.stop_event.is_set() and
                   not request.result.cancelled() and asyncio.get_running_loop().time() < deadline):
                await asyncio.wait([self.finished], timeout=.1)
            if self.finished.done():
                return self.finished.result()
            if self.conn.sentence_id == request.id:
                from core.handle.abortHandle import handleAbortMessage
                await handleAbortMessage(self.conn)
            return False
        except Exception:
            if self.conn.sentence_id == request.id:
                from core.handle.abortHandle import handleAbortMessage
                await handleAbortMessage(self.conn)
            raise
        finally:
            self.finished = None
            self.sentence_id = None

    async def _run(self):
        while not self.queue.empty():
            _, _, request = self.queue.get_nowait()
            try:
                result = False if request.result.cancelled() else await self._play(request)
            except asyncio.CancelledError:
                if not request.result.done():
                    request.result.set_result(False)
                self.queue.task_done()
                raise
            except Exception as error:
                self.conn.logger.warning(f"announcement failed id={request.id}: {type(error).__name__}")
                result = False
            if not request.result.done():
                request.result.set_result(result)
            self.queue.task_done()

    async def close(self):
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        while not self.queue.empty():
            _, _, request = self.queue.get_nowait()
            if not request.result.done():
                request.result.set_result(False)
            self.queue.task_done()


def coordinator(conn):
    instance = getattr(conn, "session_coordinator", None)
    if instance is None:
        from core.handle.intentHandler import speak_txt
        instance = conn.session_coordinator = SessionCoordinator(conn, speak_txt)
    return instance
