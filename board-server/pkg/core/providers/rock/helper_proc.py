"""provider 侧的 helper 子进程管理: 拉起/就绪握手/请求/崩溃重启."""
import os
import subprocess
import sys
import threading
import time

from core.providers.rock import framing

HELPER_DIR = os.path.dirname(os.path.abspath(__file__))


class HelperError(Exception):
    pass


class HelperProcess:
    """一个常驻 helper(asr_helper/tts_helper)的进程内代理.

    - start(): 拉起并等 ready 帧(模型加载 ~3.5s, 就绪超时应 >= 30s)
    - request(): 线程安全地发一帧收一帧
    - helper 死亡后 alive=False, 下次 start() 重新拉起
    """

    def __init__(self, script, args, ready_timeout=60.0, env_extra=None):
        self.script = os.path.join(HELPER_DIR, script)
        self.args = [str(a) for a in args]
        self.ready_timeout = ready_timeout
        self.env_extra = dict(env_extra or {})
        self.proc = None
        # RLock: request() 持锁时会调 start() 补拉进程, 不可重入锁会死锁
        self.lock = threading.RLock()

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.alive:
            return
        with self.lock:
            if self.alive:
                return
            env = dict(os.environ)
            env.update(self.env_extra)
            # 板上 PATH 没有 python3, 必须用网关自己的解释器
            self.proc = subprocess.Popen(
                [sys.executable, self.script] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                env=env,
                cwd=HELPER_DIR,
            )
            t0 = time.monotonic()
            header, _ = framing.recv_msg(self.proc.stdout)
            if not header.get("ok"):
                err = header.get("error", "未知错误")
                self._reap()
                raise HelperError(f"helper init 失败: {err}")
            if header.get("event") != "ready":
                self._reap()
                raise HelperError(f"helper 握手异常: {header}")
            elapsed = time.monotonic() - t0

    def _reap(self):
        if self.proc is not None:
            try:
                self.proc.kill()
            except OSError:
                pass
            self.proc = None

    def request(self, header, body=b"", timeout=60.0):
        """发一帧收一帧; helper 崩溃/超时抛 HelperError(由上层决定回退)."""
        with self.lock:
            if not self.alive:
                self.start()
            try:
                framing.send_msg(self.proc.stdin, header, body)
            except (BrokenPipeError, OSError) as e:
                self._reap()
                raise HelperError(f"helper 管道断裂: {e}") from e
            # 帧协议自身没有超时: 依赖读写阻塞。用一个看门线程兜底。
            import threading as _t

            result = {}

            def _read():
                try:
                    result["msg"] = framing.recv_msg(self.proc.stdout)
                except Exception as e:  # noqa: BLE001
                    result["err"] = e

            reader = _t.Thread(target=_read, daemon=True)
            reader.start()
            reader.join(timeout)
            if "msg" not in result:
                self._reap()
                raise HelperError(f"helper 响应超时({timeout}s)")
            if "err" in result:
                self._reap()
                raise HelperError(f"helper 读帧失败: {result['err']}")
            header, body = result["msg"]
            if not header.get("ok"):
                raise HelperError(header.get("error", "helper 返回失败"))
            return header, body

    def stop(self):
        with self.lock:
            if self.proc is not None:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
                try:
                    self.proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    self._reap()
                self.proc = None
