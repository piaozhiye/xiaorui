import asyncio
import json
from pathlib import Path
import queue
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "board-server/pkg"))
from core.session_coordinator import SessionCoordinator


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.offers = []
        self.spoken = []
        self.accept = True
        self.conn = SimpleNamespace(features={"announce_ack": True}, stop_event=threading.Event(),
            client_is_speaking=False, sentence_id="user-turn", session_id="session",
            logger=MagicMock(), tts=SimpleNamespace(music_sentence_id=None,
                tts_text_queue=queue.Queue(), tts_audio_queue=queue.Queue()))
        async def send(data):
            message = json.loads(data)
            self.offers.append(message)
            if message.get("state") == "offer":
                self.owner.acknowledge(message["request_id"], self.accept)
        self.conn.websocket = SimpleNamespace(send=send)
        def speak(conn, text):
            self.spoken.append(text)
            self.assertEqual(conn.sentence_id, self.owner.sentence_id)
            conn.client_is_speaking = False
            asyncio.get_running_loop().call_soon(self.owner.complete, conn.sentence_id)
        self.owner = SessionCoordinator(self.conn, speak, ack_timeout=.01)

    async def test_accepted_then_finish(self):
        self.assertTrue(await self.owner.request("hello"))
        self.assertEqual(self.spoken, ["hello"])
        self.assertFalse(self.owner.acknowledge(self.offers[0]["request_id"], True))
        self.assertFalse(self.owner.complete("late-id"))

    async def test_duplicate_ack_does_not_cancel_grant(self):
        async def send(data):
            message = json.loads(data)
            self.offers.append(message)
            if message.get("state") == "offer":
                self.assertTrue(self.owner.acknowledge(message["request_id"], True))
                self.assertFalse(self.owner.acknowledge(message["request_id"], True))
        self.conn.websocket.send = send
        self.assertTrue(await self.owner.request("alarm"))
        self.assertEqual([event["state"] for event in self.offers], ["offer", "grant"])

    async def test_busy_does_not_overwrite_user_turn(self):
        self.conn.client_is_speaking = True
        self.assertFalse(await self.owner.request("alarm", ttl=.02))
        self.assertEqual(self.conn.sentence_id, "user-turn")
        self.assertFalse(self.offers)
        self.assertFalse(self.spoken)

    async def test_client_busy_nack_then_retry(self):
        self.accept = False
        asyncio.get_running_loop().call_later(.02, setattr, self, "accept", True)
        self.assertTrue(await self.owner.request("alarm", ttl=.5))
        self.assertGreaterEqual(len(self.offers), 2)
        self.assertNotEqual(self.offers[0]["request_id"], self.offers[1]["request_id"])
        self.assertEqual(len(self.spoken), 1)

    async def test_missing_ack_expires_without_speech(self):
        async def send(data):
            self.assertFalse(self.owner.acknowledge("wrong", True))
        self.conn.websocket.send = send
        self.assertFalse(await self.owner.request("alarm", ttl=.03))
        self.assertEqual(self.conn.sentence_id, "user-turn")
        self.assertFalse(self.spoken)

    async def test_waits_for_tts_readiness(self):
        tts = self.conn.tts
        self.conn.tts = None
        asyncio.get_running_loop().call_later(.01, setattr, self.conn, "tts", tts)
        self.assertTrue(await self.owner.request("welcome", ttl=.5))

    async def test_legacy_client_never_gets_offer(self):
        self.conn.features = {}
        self.assertTrue(await self.owner.request("welcome"))
        self.assertNotIn("request_id", self.offers[0])


if __name__ == "__main__":
    unittest.main()
