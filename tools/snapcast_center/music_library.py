#!/usr/bin/env python3
"""Confined local music catalog and real-time PCM source pump for Snapcast."""
import hashlib
import fcntl
import os
import pathlib
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import urllib.parse
import wave


SUPPORTED_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac"}


class MusicLibraryError(RuntimeError):
    pass


class MusicLibrary:
    """Expose opaque track IDs, never caller-provided paths, to the HTTP API."""

    def __init__(self, root, fifo_root="/tmp/xiaorui-music-pipes", ffmpeg=None,
                 popen=subprocess.Popen, monotonic=time.monotonic):
        self.root = pathlib.Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise MusicLibraryError(f"音乐目录不存在或不是目录: {self.root}")
        self.fifo_root = pathlib.Path(fifo_root).expanduser()
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")
        if not self.ffmpeg:
            raise MusicLibraryError("找不到 ffmpeg；请安装 ffmpeg 或使用 --ffmpeg 指定路径")
        self.fifo_root = self._prepare_runtime_dir(self.fifo_root)
        self._popen = popen
        self._monotonic = monotonic
        self._processes = {}
        self._paused = set()
        self._position_base = {}
        self._position_started = {}
        self._duration_cache = {}
        ffmpeg_path = pathlib.Path(self.ffmpeg)
        sibling_ffprobe = ffmpeg_path.with_name("ffprobe")
        self.ffprobe = str(sibling_ffprobe) if sibling_ffprobe.is_file() else shutil.which("ffprobe")
        self._lock = threading.RLock()
        self._lock_file = open(self.fifo_root / ".controller.lock", "a+")
        os.chmod(self.fifo_root / ".controller.lock", 0o600)
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise MusicLibraryError(f"音乐控制器已在使用音源目录: {self.fifo_root}") from exc
        self._reap_orphaned_writers()

    def list_tracks(self):
        tracks = []
        for current, dirs, files in os.walk(self.root, followlinks=False):
            base = pathlib.Path(current)
            dirs[:] = sorted(d for d in dirs if not (base / d).is_symlink())
            for name in sorted(files, key=str.casefold):
                path = base / name
                if path.suffix.casefold() not in SUPPORTED_SUFFIXES or path.is_symlink():
                    continue
                try:
                    resolved = path.resolve(strict=True)
                    if not resolved.is_relative_to(self.root) or not resolved.is_file():
                        continue
                    rel = resolved.relative_to(self.root).as_posix()
                    info = resolved.stat()
                except OSError:
                    continue
                track_id = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:20]
                tracks.append({"id": track_id, "title": pathlib.PurePosixPath(rel).name,
                               "relative_path": rel, "size_bytes": info.st_size,
                               "duration_seconds": self._duration(resolved)})
        tracks.sort(key=lambda item: item["relative_path"].casefold())
        return tracks

    def get_track(self, track_id):
        if not isinstance(track_id, str) or len(track_id) != 20:
            raise MusicLibraryError("无效曲目 ID")
        for track in self.list_tracks():
            if track["id"] == track_id:
                path = (self.root / track["relative_path"]).resolve(strict=True)
                if not path.is_relative_to(self.root) or not path.is_file():
                    break
                return track, path
        raise MusicLibraryError("曲目不存在或已从音乐库移除")

    def source_uri(self, track_id):
        _, _ = self.get_track(track_id)
        fifo = self._fifo_path(track_id)
        stream_id = f"music-{track_id}"
        query = urllib.parse.urlencode({"name": stream_id, "codec": "pcm",
                                        "sampleformat": "48000:16:2", "mode": "read"})
        return f"pipe://{urllib.parse.quote(str(fifo), safe='/')}?{query}"

    def start(self, track_id):
        track, audio_path = self.get_track(track_id)
        with self._lock:
            previous = self._processes.get(track_id)
            if previous is not None and previous.poll() is None:
                return track
            self._processes.pop(track_id, None)
            self._paused.discard(track_id)
            fifo = self._fifo_path(track_id)
            if fifo.is_symlink() or (fifo.exists() and not stat.S_ISFIFO(fifo.lstat().st_mode)):
                raise MusicLibraryError(f"音源出口不是 FIFO，拒绝覆盖: {fifo}")
            if not fifo.exists():
                os.mkfifo(fifo, 0o600)
            else:
                os.chmod(fifo, 0o600)
            log_path = self.fifo_root / f"{track_id}.ffmpeg.log"
            log = open(log_path, "ab", buffering=0)
            command = [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "warning",
                       "-re", "-i", str(audio_path), "-vn", "-acodec", "pcm_s16le",
                       "-ar", "48000", "-ac", "2", "-f", "s16le", "-y", str(fifo)]
            try:
                process = self._popen(command, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=log,
                                      start_new_session=True, close_fds=True)
            except OSError as exc:
                log.close()
                raise MusicLibraryError(f"无法启动 ffmpeg: {exc}") from exc
            log.close()
            self._processes[track_id] = process
            self._position_base[track_id] = 0.0
            self._position_started[track_id] = self._monotonic()
            return track

    def position(self, track_id):
        """Return estimated media seconds written by ffmpeg, excluding pause time."""
        with self._lock:
            if track_id not in self._position_base:
                return None
            position = self._position_base[track_id]
            if track_id not in self._paused:
                position += max(0.0, self._monotonic() - self._position_started.get(track_id, self._monotonic()))
        return round(position, 2)

    def duration(self, track_id):
        track, path = self.get_track(track_id)
        if track.get("duration_seconds") is not None:
            return track["duration_seconds"]
        try:
            info = path.stat()
        except OSError:
            return None
        key = (track_id, info.st_mtime_ns, info.st_size)
        with self._lock:
            if key in self._duration_cache:
                return self._duration_cache[key]
        value = self._probe_duration(path)
        with self._lock:
            self._duration_cache[key] = value
        return value

    def _probe_duration(self, path):
        try:
            if self.ffprobe:
                result = subprocess.run(
                    [self.ffprobe, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=2, check=False, close_fds=True)
                value = float(result.stdout.decode("ascii", errors="ignore").strip())
                return round(value, 1) if value > 0 else None
            result = subprocess.run([self.ffmpeg, "-hide_banner", "-i", str(path)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, timeout=2, check=False,
                                    close_fds=True)
            output = result.stderr.decode("utf-8", errors="replace")
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
            if not match:
                return None
            hours, minutes, seconds = int(match.group(1)), int(match.group(2)), float(match.group(3))
            return round(hours * 3600 + minutes * 60 + seconds, 1)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    def stop(self, track_id):
        with self._lock:
            process = self._processes.pop(track_id, None)
            was_paused = track_id in self._paused
            self._paused.discard(track_id)
            self._position_base.pop(track_id, None)
            self._position_started.pop(track_id, None)
        if process is None or process.poll() is not None:
            return
        if was_paused:
            try:
                os.killpg(process.pid, signal.SIGCONT)
            except ProcessLookupError:
                return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)

    def stop_all(self):
        with self._lock:
            track_ids = list(self._processes)
        for track_id in track_ids:
            self.stop(track_id)

    def close(self):
        self.stop_all()
        if getattr(self, "_lock_file", None) and not self._lock_file.closed:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()

    def process(self, track_id):
        with self._lock:
            return self._processes.get(track_id)

    def pause(self, track_id):
        with self._lock:
            process = self._processes.get(track_id)
            if process is None or process.poll() is not None:
                raise MusicLibraryError("播放音源已退出，无法暂停")
            if track_id in self._paused:
                return
            current = self._position_base.get(track_id, 0.0)
            current += max(0.0, self._monotonic() - self._position_started.get(track_id, self._monotonic()))
            try:
                os.killpg(process.pid, signal.SIGSTOP)
            except OSError as exc:
                raise MusicLibraryError(f"暂停音源失败: {exc}") from exc
            self._position_base[track_id] = current
            self._paused.add(track_id)

    def resume(self, track_id):
        with self._lock:
            process = self._processes.get(track_id)
            if process is None or process.poll() is not None:
                raise MusicLibraryError("播放音源已退出，无法继续")
            if track_id not in self._paused:
                return
            try:
                os.killpg(process.pid, signal.SIGCONT)
            except OSError as exc:
                raise MusicLibraryError(f"继续音源失败: {exc}") from exc
            self._position_started[track_id] = self._monotonic()
            self._paused.discard(track_id)

    def sync_sessions(self, sessions):
        """Keep one ffmpeg writer per active library session and reap ended tracks."""
        active = {}
        for session in sessions:
            track_id = session.get("track_id")
            if (track_id and session.get("state") in ("playing", "paused")
                    and track_id not in active):
                active[track_id] = session
        ended = []
        with self._lock:
            for track_id, session in active.items():
                process = self._processes.get(track_id)
                if process is not None and process.poll() is not None:
                    self._processes.pop(track_id, None)
                    self._paused.discard(track_id)
                    self._position_base.pop(track_id, None)
                    self._position_started.pop(track_id, None)
                    if session.get("session_id"):
                        ended.append(session["session_id"])
                    continue
                if process is None:
                    self.start(track_id)
                if session.get("state") == "paused":
                    self.pause(track_id)
                else:
                    self.resume(track_id)
            stale = set(self._processes) - set(active)
        for track_id in stale:
            self.stop(track_id)
        return ended

    def _fifo_path(self, track_id):
        if not isinstance(track_id, str) or len(track_id) != 20 or not all(
                c in "0123456789abcdef" for c in track_id):
            raise MusicLibraryError("无效曲目 ID")
        return self.fifo_root / f"{track_id}.pcm"

    @staticmethod
    def _prepare_runtime_dir(path):
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = path.lstat()
        except OSError as exc:
            raise MusicLibraryError(f"无法准备音源出口目录 {path}: {exc}") from exc
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid()):
            raise MusicLibraryError(f"音源出口目录不安全: {path}")
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            raise MusicLibraryError(f"无法保护音源出口目录 {path}: {exc}") from exc
        return path.resolve()

    def _reap_orphaned_writers(self):
        """After a controller crash, kill only our exact ffmpeg->private-FIFO jobs."""
        proc_root = pathlib.Path("/proc")
        expected_exe = os.path.realpath(self.ffmpeg)
        fifo_prefix = str(self.fifo_root) + os.sep
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                argv = [os.fsdecode(arg) for arg in (entry / "cmdline").read_bytes().split(b"\0") if arg]
                if not argv or os.path.realpath(argv[0]) != expected_exe:
                    continue
                outputs = [arg for arg in argv[1:] if arg.startswith(fifo_prefix)
                           and arg.endswith(".pcm")]
                if not outputs or any(not pathlib.Path(output).is_relative_to(self.fifo_root)
                                      for output in outputs):
                    continue
                pid = int(entry.name)
                if os.getpgid(pid) != pid:  # only processes launched in our own session
                    continue
                os.killpg(pid, signal.SIGTERM)
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
                else:
                    os.killpg(pid, signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError, PermissionError, OSError, ValueError):
                continue

    @staticmethod
    def _duration(path):
        if path.suffix.casefold() != ".wav":
            return None
        try:
            with wave.open(str(path), "rb") as wav:
                rate = wav.getframerate()
                return round(wav.getnframes() / rate, 1) if rate else None
        except (OSError, wave.Error):
            return None
