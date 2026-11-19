"""provider 侧的 helper 子进程管理: 拉起/就绪握手/请求/崩溃重启."""
import os
import subprocess
import sys
import threading

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
            self._reap()
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
            try:
                header, _ = self._exchange(timeout=self.ready_timeout)
                if not header.get("ok") or header.get("event") != "ready":
                    raise HelperError(f"helper 握手失败: {header}")
            except Exception:
                self._reap()
                raise

    def _reap(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()

    def _exchange(self, header=None, body=b"", timeout=60.0):
        # 捕获具体进程，超时线程不能意外读取下一代 helper 的 stdout。
        proc = self.proc
        result = {}

        def exchange():
            try:
                if header is not None:
                    framing.send_msg(proc.stdin, header, body)
                result["msg"] = framing.recv_msg(proc.stdout)
            except Exception as exc:
                result["err"] = exc

        worker = threading.Thread(target=exchange, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            # 先 kill/wait，解除阻塞的管道读写，再关闭文件对象。
            proc.kill()
            proc.wait(timeout=5)
            worker.join(5)
            self._reap()
            raise HelperError(f"helper 响应超时({timeout}s)")
        if "err" in result:
            self._reap()
            raise HelperError(f"helper 读写帧失败: {result['err']}") from result["err"]
        return result["msg"]

    def request(self, header, body=b"", timeout=60.0):
        """请求的超时覆盖管道写入及读取；SDK 异常后销毁进程隔离迟到回调。"""
        with self.lock:
            if not self.alive:
                self._reap()
                self.start()
            response, payload = self._exchange(header, body, timeout)
            if not response.get("ok"):
                self._reap()
                raise HelperError(response.get("error", "helper 返回失败"))
            return response, payload

    def stop(self):
        with self.lock:
            if self.proc is None:
                return
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                self._reap()
