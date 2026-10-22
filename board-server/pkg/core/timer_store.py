"""Durable device-scoped timers. SQLite transactions arbitrate both gateways.

One short connection per operation, safe from worker threads/processes. Never
store API keys, audio or connection objects. Delivery is leased, not exactly-once:
a crash after playback but before commit can cause replay after lease expiry.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import uuid


class TimerStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("unsupported timer store schema")
            db.execute("""CREATE TABLE IF NOT EXISTS timers (
                id TEXT PRIMARY KEY, device TEXT NOT NULL, due REAL NOT NULL,
                payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
                retry_owner TEXT, retry_until REAL NOT NULL DEFAULT 0)""")
            db.execute("CREATE INDEX IF NOT EXISTS timer_due ON timers(device,state,due)")
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=.5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def add(self, device, payload, now):
        timer_id = uuid.uuid4().hex
        due = payload.get("target_at") if payload.get("h24") is not None else now + payload["seconds"]
        with self.transaction() as db:
            count = db.execute("SELECT count(*) FROM timers WHERE device=? AND state IN ('pending','claimed')",
                               (device,)).fetchone()[0]
            if count >= 3:
                return None
            db.execute("INSERT INTO timers(id,device,due,payload) VALUES(?,?,?,?)",
                       (timer_id, device, due, json.dumps(payload, ensure_ascii=False)))
        return timer_id

    def list(self, device):
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM timers WHERE device=? AND state IN ('pending','claimed') ORDER BY due,id",
                              (device,)).fetchall()
        return [dict(json.loads(row["payload"]), id=row["id"], due_at=row["due"], state=row["state"])
                for row in rows]

    def cancel(self, device, intent):
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM timers WHERE device=? AND state IN ('pending','claimed')",
                              (device,)).fetchall()
            victims = []
            for row in rows:
                item = json.loads(row["payload"])
                if intent["op"] == "cancel_clock":
                    match = (intent.get("target_at") is not None and
                             item.get("h24") is not None and item.get("target_at") == intent["target_at"])
                elif intent["op"] == "cancel":
                    match = (intent.get("seconds") is None or
                             (item.get("h24") is None and item["seconds"] == intent["seconds"]))
                else:
                    match = False
                if match:
                    db.execute("UPDATE timers SET state='cancelled' WHERE id=?", (row["id"],))
                    victims.append(item)
        return victims

    def claim(self, device, owner, now, lease_s=90):
        with self.transaction() as db:
            # Give another gateway an opportunity after an inactive route NACKs.
            row = db.execute("""SELECT * FROM timers WHERE device=? AND due<=?
                AND (state='pending' OR (state='claimed' AND lease_until<?))
                AND (retry_owner IS NULL OR retry_owner!=? OR retry_until<=?)
                ORDER BY due,id LIMIT 1""", (device, now, now, owner, now)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE timers SET state='claimed',owner=?,lease_until=? WHERE id=?",
                       (owner, now + lease_s, row["id"]))
            return dict(json.loads(row["payload"]), id=row["id"], due_at=row["due"])

    def owned(self, timer_id, owner):
        with self.transaction() as db:
            return db.execute("SELECT 1 FROM timers WHERE id=? AND state='claimed' AND owner=?",
                              (timer_id, owner)).fetchone() is not None

    def finish(self, timer_id, owner, delivered, now):
        with self.transaction() as db:
            db.execute("""UPDATE timers SET state=?,owner=NULL,lease_until=0,
                       retry_owner=?,retry_until=? WHERE id=? AND state='claimed' AND owner=?""",
                       ("delivered" if delivered else "pending", owner, now + 3, timer_id, owner))

    def expire(self, now, max_lateness=3600):
        """迟到清扫**不按设备过滤**——任何活设备的调度循环都在全表兜底。

        为什么: claim/调度循环都按 device 域(service_for 按 device 建服务),
        短命/失联设备(测试探针、退役终端)的行没有任何活服务替它 expire,
        设备域过滤会让这些行永久 pending(2026-10-22 实测: timer-probe 的
        计时器卡 pending, 绊倒唤醒套件 preflight 的 active-reminders 断言)。
        迟到提醒只在 max_lateness 内有意义, 由任一活连接的清扫过期掉。
        边界(2026-10-22 复审限定): 清扫寄生在活连接的调度循环上——全部
        设备离线时无人清扫, 重连后才补扫(全离线期间本也无设备能收提醒);
        "pending 归零"是现场结果, 不是不变量。
        """
        with self.transaction() as db:
            db.execute("UPDATE timers SET state='expired' WHERE due<? AND state IN ('pending','claimed')",
                       (now - max_lateness,))
            # Bounded audit retention; avoid an ever-growing embedded database.
            db.execute("DELETE FROM timers WHERE due<? AND state IN ('delivered','cancelled','expired')",
                       (now - 7 * 86400,))
