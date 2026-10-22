#!/usr/bin/env python3
"""Announce-ACK protocol matrix against a live gateway. NOT an acoustic test.

Cases (device must be a dedicated probe device; XR_STATE_DIR gateways persist
its timers -- clean up with cancel commands if a case aborts):
  welcome  offer->ACK->grant->audio->stop on connect (reconnect welcome path)
  noack    offer ignored -> server cancel, zero audio (missing-ACK budget)
  stale    ACK carries a foreign request_id -> cancels, offer times out
  dup      duplicate accepted ACK after grant -> playback still completes
  busy     timer due during a long answer -> NACK while busy, one delivery after
  legacy   client without announce_ack -> bare announce still plays welcome
"""
import argparse
import json
import time
from websockets.sync.client import connect

ACK = {"type": "listen", "state": "announce_ack"}


def send_ack(ws, request_id, accepted):
    ws.send(json.dumps(dict(ACK, request_id=request_id, accepted=accepted)))


class Client:
    """Tiny event pump: records announces/tts/stt and their timing."""

    def __init__(self, url, device, ack_feature=True):
        self.events = []
        self.t0 = time.monotonic()
        self.ws = connect(url, additional_headers={"device-id": device},
                          open_timeout=10, close_timeout=3)
        hello = {"type": "hello", "version": 1, "transport": "websocket",
                 "features": {"mcp": False, "aec": False},
                 "audio_params": {"format": "opus", "sample_rate": 16000,
                                  "channels": 1, "frame_duration": 60}}
        if ack_feature:
            hello["features"]["announce_ack"] = True
        self.ws.send(json.dumps(hello))
        while True:
            data = self.ws.recv(timeout=15)
            if isinstance(data, str) and json.loads(data).get("type") == "hello":
                break
        self.ack_feature = ack_feature

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def pump(self, seconds, on_event=None):
        """Collect events for a window; on_event may reply (returns stop flag)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                data = self.ws.recv(timeout=max(.1, deadline - time.monotonic()))
            except TimeoutError:
                continue
            elapsed = round(time.monotonic() - self.t0, 2)
            if isinstance(data, bytes):
                self.events.append({"t": elapsed, "audio": len(data)})
            else:
                event = json.loads(data)
                self.events.append({"t": elapsed, **{k: event.get(k) for k in
                    ("type", "state", "request_id", "text")}})
            if on_event and self.events[-1]:
                if on_event(self.events[-1]):
                    return

    def announce_events(self, state):
        return [e for e in self.events
                if e.get("type") == "announce" and e.get("state") == state]

    def texts(self):
        return "".join(e.get("text", "") for e in self.events
                       if e.get("type") == "tts" and e.get("state") == "sentence_start")

    def audio_packets(self):
        return sum(1 for e in self.events if "audio" in e)


def drain_welcome(client, accept=True):
    """Wait for the connect-time welcome offer and take it (or ignore it)."""
    def on_event(e):
        if e.get("type") == "announce" and e.get("state") == "offer":
            if accept:
                send_ack(client.ws, e["request_id"], True)
                return True
            return True
        return False
    client.pump(25, on_event)
    client.pump(30)  # grant -> audio -> stop


def case_welcome(args):
    c = Client(args.url, args.device)
    c.pump(25, lambda e: (e.get("type") == "announce" and e.get("state") == "offer"
                          and (send_ack(c.ws, e["request_id"], True) or True)))
    c.pump(40)
    c.close()
    grants = c.announce_events("grant")
    assert grants, "no grant after accepted ACK"
    text = c.texts()
    assert c.audio_packets() >= 1, f"welcome had no audio (events={c.events[:8]})"
    assert "小瑞" in text or "好" in text, f"unexpected welcome text: {text}"
    offer = c.announce_events("offer")[0]
    print(f"PASS welcome offer@{offer['t']}s grant@{grants[0]['t']}s "
          f"audio={c.audio_packets()}pkts text={text[:24]}")


def stops(c):
    return sum(1 for e in c.events if e.get("type") == "tts" and e.get("state") == "stop")


def arm_timer(c, text="十五秒后提醒我"):
    """Create a timer on this connection and wait for its spoken ack."""
    before = stops(c)  # snapshot first: old stops (welcome) must not fake completion
    c.ws.send(json.dumps({"type": "listen", "state": "detect", "text": text}))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        c.pump(1)
        if stops(c) > before:
            return
    raise TimeoutError("timer-set reply missing")


def await_offer(c, seconds):
    """Wait for a timer-driven announce offer (welcome is deduped for 10 min)."""
    got = []
    c.pump(seconds, lambda e: e.get("type") == "announce" and e.get("state") == "offer"
           and (got.append(e["request_id"]) or True))
    return got[0] if got else None


def audio_after(c, t):
    return sum(1 for e in c.events if "audio" in e and e["t"] > t)


def accept_and_deliver(c):
    """Accept the next offer and wait out one full delivery (cleanup helper)."""
    granted = {}
    def accept(e):
        if e.get("type") == "announce" and e.get("state") == "offer":
            send_ack(c.ws, e["request_id"], True)
        if e.get("type") == "announce" and e.get("state") == "grant":
            granted["t"] = e["t"]
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and not granted:
        c.pump(2, accept)
    assert granted, "no grant when finally accepting"
    c.pump(45)


def case_noack(args):
    c = Client(args.url, args.device)
    arm_timer(c)
    offer = await_offer(c, 25)
    assert offer, "timer offer missing"
    c.pump(12)  # ack_timeout=2 -> cancel; scheduler retries with new offers
    c.close()
    offers = c.announce_events("offer")
    cancels = c.announce_events("cancel")
    assert cancels, "no cancel after ACK budget expired"
    assert not c.announce_events("grant"), "grant without ACK"
    assert audio_after(c, offers[0]["t"]) == 0, "audio without ACK"
    # Timer still pending: reconnect and let one delivery through (cleanup).
    c2 = Client(args.url, args.device)
    accept_and_deliver(c2)
    c2.close()
    assert "时间到了" in c2.texts(), "no delivery after reconnect"
    print(f"PASS noack offers={len(offers)} cancels={len(cancels)} audio=0, "
          f"reconnect delivery ok")


def case_stale(args):
    c = Client(args.url, args.device)
    arm_timer(c)
    offer = await_offer(c, 25)
    assert offer, "timer offer missing"
    stale_id = "f" * 31 + "1"
    send_ack(c.ws, stale_id, True)  # foreign id: must not satisfy the offer
    c.pump(12)
    c.close()
    assert not c.announce_events("grant"), "stale ACK must not be granted"
    stale_cancel = [e for e in c.announce_events("cancel") if e.get("request_id") == stale_id]
    assert stale_cancel, "late accepted ACK was not cancelled"
    assert audio_after(c, c.announce_events("offer")[0]["t"]) == 0, \
        "audio must not start on stale ACK"
    c2 = Client(args.url, args.device)
    accept_and_deliver(c2)
    c2.close()
    assert "时间到了" in c2.texts(), "no delivery after stale round"
    print(f"PASS stale cancel-for-stale-id seen, "
          f"offers={len(c.announce_events('offer'))}, audio=0, delivery ok")


def case_dup(args):
    c = Client(args.url, args.device)
    arm_timer(c)
    real = {}
    def on_event(e):
        if e.get("type") == "announce" and e.get("state") == "offer" and not real:
            real["id"] = e["request_id"]
            send_ack(c.ws, e["request_id"], True)
        if e.get("type") == "announce" and e.get("state") == "grant":
            send_ack(c.ws, real["id"], True)  # duplicate ACK after grant
            return True
        return False
    c.pump(30, on_event)
    c.pump(45)
    c.close()
    text = c.texts()
    grant_t = c.announce_events("grant")[0]["t"]
    assert "时间到了" in text, f"delivery did not complete: {c.events[-6:]}"
    assert audio_after(c, grant_t) >= 1, "duplicate ACK killed playback"
    print(f"PASS dup playback complete audio_after_grant={audio_after(c, grant_t)}pkts")


def case_busy(args):
    """Race a timer offer into the LLM-latency window of a submitted command.

    The server-side idle gate holds offers while TTS streams, so the wire NACK
    only fires when the offer lands between text submit and the first sentence.
    Two rounds with different submit offsets; PASS needs one observed NACK and
    every created timer delivered exactly once.
    """
    c = Client(args.url, args.device)
    drain_welcome(c)

    def command(text):
        c.ws.send(json.dumps({"type": "listen", "state": "detect", "text": text}))

    def wait_stop(seconds):
        before = sum(1 for e in c.events if e.get("type") == "tts" and e.get("state") == "stop")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            c.pump(1)
            now = sum(1 for e in c.events if e.get("type") == "tts" and e.get("state") == "stop")
            if now > before:
                return True
        return False

    busy = {"on": False}
    nacked = 0
    def on_announce(e):
        nonlocal nacked
        if e.get("type") == "announce" and e.get("state") == "offer":
            send_ack(c.ws, e["request_id"], busy["on"])
            if busy["on"]:
                nacked += 1

    deliveries_before = c.texts().count("时间到了")
    for offset in (17.0, 16.0):
        command("二十秒后提醒我")
        stops_before = stops(c)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and stops(c) == stops_before:
            c.pump(1)
        assert stops(c) > stops_before, "timer-set reply did not finish"
        time.sleep(offset)
        command("用一百五十字介绍一下长城")  # LLM latency window is the race slot
        busy["on"] = True
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            c.pump(2, on_announce)
            stops_now = sum(1 for e in c.events
                            if e.get("type") == "tts" and e.get("state") == "stop")
            if stops_now > stops_before:
                break
        busy["on"] = False
        granted = {}
        def accept(e):
            if e.get("type") == "announce" and e.get("state") == "offer":
                send_ack(c.ws, e["request_id"], True)
            if e.get("type") == "announce" and e.get("state") == "grant":
                granted["t"] = e["t"]
        wait_until = time.monotonic() + 25
        while time.monotonic() < wait_until and not granted:
            c.pump(2, accept)
        c.pump(40)
    c.close()
    deliveries = c.texts().count("时间到了") - deliveries_before
    if deliveries != 2 or nacked < 1:
        for e in c.events:
            if "audio" not in e:
                print(e)
    assert deliveries == 2, f"expected 2 deliveries (one per timer), got {deliveries}"
    assert nacked >= 1, "race never produced a busy NACK on the wire"
    print(f"PASS busy nacks={nacked} deliveries={deliveries}")


def case_legacy(args):
    c = Client(args.url, args.device, ack_feature=False)
    c.pump(45)
    c.close()
    offers = [e for e in c.events if e.get("type") == "announce" and e.get("state")]
    assert not offers, f"legacy client must not receive offer/grant/cancel: {offers}"
    bare = [e for e in c.events if e.get("type") == "announce" and not e.get("state")]
    assert bare, "legacy announce missing"
    assert c.audio_packets() >= 1, "legacy welcome had no audio"
    print(f"PASS legacy bare-announce audio={c.audio_packets()}pkts")


CASES = {fn.__name__[5:]: fn for fn in
         (case_welcome, case_noack, case_stale, case_dup, case_busy, case_legacy)}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:18001/xiaorui/v1/")
    parser.add_argument("--device", default="arch-cand-probe")
    parser.add_argument("--case", choices=sorted(CASES), required=True)
    args = parser.parse_args()
    CASES[args.case](args)
