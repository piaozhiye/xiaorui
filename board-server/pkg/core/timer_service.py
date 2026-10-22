"""Scheduler independent of intent parsing and of any one websocket lifetime."""
import asyncio
import os
import time
import uuid
from core.timer_store import TimerStore

_SERVICES = {}


class TimerService:
    def __init__(self, store, device):
        self.store, self.device = store, device
        self.owner = uuid.uuid4().hex
        self.conn = None
        self.task = None

    def attach(self, conn):
        self.conn = conn
        if conn.features.get("announce_ack", False) and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.run())

    async def deliver(self, timer):
        from core.session_coordinator import coordinator
        conn = self.conn
        task = asyncio.create_task(coordinator(conn).request(
            f"时间到了，这是你设置的{timer['speak']}提醒", priority=0, ttl=4,
            request_id=timer["id"]))
        try:
            while not task.done():
                await asyncio.wait([task], timeout=.1)
                if not await asyncio.to_thread(self.store.owned, timer["id"], self.owner):
                    task.cancel()
                    return False
            return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run(self):
        while True:
            try:
                conn = self.conn
                if (conn is not None and not conn.stop_event.is_set() and
                        conn.features.get("announce_ack", False)):
                    await asyncio.to_thread(self.store.expire, time.time())
                    timer = await asyncio.to_thread(self.store.claim, self.device, self.owner, time.time())
                    if timer:
                        success = await self.deliver(timer)
                        await asyncio.to_thread(self.store.finish, timer["id"], self.owner, success, time.time())
                        conn.logger.info(f"timer delivery id={timer['id']} success={success}")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self.conn:
                    self.conn.logger.warning(f"timer scheduler: {type(error).__name__}")
            await asyncio.sleep(.5)


def service_for(conn):
    directory = os.environ.get("XR_STATE_DIR")
    if not directory:
        return None  # Legacy deployments opt in only after the ACK client is validated.
    device = conn.headers.get("device-id")
    if not device:
        raise ValueError("persistent timers require a device-id")
    path = os.path.join(directory, "timers.sqlite3")
    key = (path, device)
    if key not in _SERVICES:
        _SERVICES[key] = TimerService(TimerStore(path), device)
    return _SERVICES[key]
