#!/usr/bin/env python3
"""Confined local music catalog and real-time PCM source pump for Snapcast."""
import hashlib
import fcntl
import json
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
import uuid
import wave


SUPPORTED_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac"}
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_UPLOAD_LIBRARY_BYTES = 512 * 1024 * 1024
UPLOAD_FREE_SPACE_RESERVE_BYTES = 128 * 1024 * 1024


class MusicLibraryError(RuntimeError):
    pass


class MusicLibrary:
    """Expose opaque track IDs, never caller-provided paths, to the HTTP API."""

    def __init__(self, root, fifo_root="/tmp/xiaorui-music-pipes", ffmpeg=None,
                 popen=subprocess.Popen, monotonic=time.monotonic, upload_root=None):
        self.root = pathlib.Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise MusicLibraryError(f"音乐目录不存在或不是目录: {self.root}")
        configured_upload_root = pathlib.Path(upload_root).expanduser() if upload_root else self.root / "uploads"
        if not configured_upload_root.is_absolute():
            configured_upload_root = self.root / configured_upload_root
        self.upload_root = pathlib.Path(os.path.abspath(configured_upload_root))
        if self.upload_root == self.root:
            raise MusicLibraryError("上传目录不能与音乐库根目录相同")
        self.fifo_root = pathlib.Path(fifo_root).expanduser()
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")
        if not self.ffmpeg:
            raise MusicLibraryError("找不到 ffmpeg；请安装 ffmpeg 或使用 --ffmpeg 指定路径")
        self.fifo_root = self._prepare_runtime_dir(self.fifo_root)
        self._popen = popen
        self._monotonic = monotonic
        self._processes = {}
        # Snapserver readers from a prior replay may remain attached to a
        # named pipe. Give each playback attempt its own stream and FIFO.
        self._sources = {}
        self._paused = set()
        self._position_base = {}
        self._position_started = {}
        self._duration_cache = {}
        self._metadata_cache = {}
        self._catalog_lock = threading.Lock()
        self._catalog_cache = None
        self._catalog_cached_at = 0.0
        self._last_errors = {}
        self._upload_lock = threading.Lock()
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
        with self._catalog_lock:
            now = self._monotonic()
            if (self._catalog_cache is not None
                    and now - self._catalog_cached_at < 5.0):
                return [dict(track) for track in self._catalog_cache]
            tracks = self._scan_tracks()
            self._catalog_cache = [dict(track) for track in tracks]
            self._catalog_cached_at = now
            return tracks

    def _invalidate_catalog(self):
        with self._catalog_lock:
            self._catalog_cache = None

    def _scan_tracks(self):
        tracks = []
        for current, dirs, files in os.walk(self.root, followlinks=False):
            base = pathlib.Path(current)
            if base == self.upload_root or base.is_relative_to(self.upload_root):
                dirs[:] = []
                continue
            dirs[:] = sorted(d for d in dirs
                             if not (base / d).is_symlink()
                             and d != "uploads"
                             and (base / d).absolute() != self.upload_root)
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
                metadata = self._metadata(resolved, track_id, info)
                tracks.append({"id": track_id, "title": metadata.get("title") or pathlib.PurePosixPath(rel).name,
                               "relative_path": rel, "size_bytes": info.st_size,
                               "duration_seconds": self._duration(resolved),
                               "artist": metadata.get("artist", ""),
                               "album": metadata.get("album", ""),
                               "folder": pathlib.PurePosixPath(rel).parent.as_posix(),
                               })
        if self.upload_root.is_dir() and not self.upload_root.is_symlink():
            for current, dirs, files in os.walk(self.upload_root, followlinks=False):
                base = pathlib.Path(current)
                dirs[:] = sorted(d for d in dirs if not (base / d).is_symlink())
                for name in sorted(files, key=str.casefold):
                    path = base / name
                    if path.suffix.casefold() not in SUPPORTED_SUFFIXES or path.is_symlink():
                        continue
                    try:
                        resolved = path.resolve(strict=True)
                        if not resolved.is_relative_to(self.upload_root) or not resolved.is_file():
                            continue
                        rel = "uploads/" + resolved.relative_to(self.upload_root).as_posix()
                        info = resolved.stat()
                    except OSError:
                        continue
                    track_id = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:20]
                    metadata = self._metadata(resolved, track_id, info)
                    tracks.append({"id": track_id, "title": metadata.get("title") or pathlib.PurePosixPath(rel).name,
                                   "relative_path": rel, "size_bytes": info.st_size,
                                   "duration_seconds": self._duration(resolved),
                                   "artist": metadata.get("artist", ""),
                                   "album": metadata.get("album", ""),
                                   "folder": pathlib.PurePosixPath(rel).parent.as_posix(),
                                   })
        tracks.sort(key=lambda item: item["relative_path"].casefold())
        return tracks

    def _metadata(self, path, track_id, info):
        """Read common tags once per file revision; missing ffprobe is non-fatal."""
        key = (str(path), info.st_mtime_ns, info.st_size)
        cached = self._metadata_cache.get(key)
        if cached is not None:
            return cached
        value = {"title": "", "artist": "", "album": ""}
        if self.ffprobe:
            try:
                proc = subprocess.run(
                    [self.ffprobe, "-v", "error", "-show_entries",
                     "format_tags=title,artist,album,album_artist:stream_tags=title,artist,album",
                     "-of", "json", str(path)], capture_output=True, text=True,
                    timeout=5, check=False)
                if proc.returncode == 0:
                    data = json.loads(proc.stdout or "{}")
                    tags = {}
                    tags.update(data.get("format", {}).get("tags", {}) or {})
                    for stream in data.get("streams", []) or []:
                        tags.update(stream.get("tags", {}) or {})
                    normalized = {str(k).casefold(): str(v).strip()
                                  for k, v in tags.items() if isinstance(v, (str, int, float))}
                    value["title"] = normalized.get("title", "")
                    value["artist"] = normalized.get("artist") or normalized.get("album_artist", "")
                    value["album"] = normalized.get("album", "")
            except (OSError, subprocess.SubprocessError, ValueError, TypeError):
                pass
        self._metadata_cache[key] = value
        return value

    def get_track(self, track_id):
        if not isinstance(track_id, str) or len(track_id) != 20:
            raise MusicLibraryError("无效曲目 ID")
        for track in self.list_tracks():
            if track["id"] == track_id:
                if track["relative_path"].startswith("uploads/"):
                    path = (self.upload_root / track["relative_path"][len("uploads/"):]).resolve(strict=True)
                    allowed_root = self.upload_root
                else:
                    path = (self.root / track["relative_path"]).resolve(strict=True)
                    allowed_root = self.root
                if not path.is_relative_to(allowed_root) or not path.is_file():
                    break
                return track, path
        raise MusicLibraryError("曲目不存在或已从音乐库移除")

    def import_upload(self, filename, chunks, size_bytes):
        """Store a validated audio upload in a private, library-visible subdirectory."""
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or not 0 < size_bytes <= MAX_UPLOAD_BYTES:
            raise MusicLibraryError("音频文件必须大于 0，且不能超过 256 MB")
        name = pathlib.PurePath(str(filename).replace("\\", "/")).name
        name = "".join(ch for ch in name if ch.isprintable() and ch not in "/\\")
        name = name.strip().strip(".")
        if not name or len(name.encode("utf-8")) > 180:
            raise MusicLibraryError("文件名无效或过长")
        suffix = pathlib.Path(name).suffix.casefold()
        if suffix not in SUPPORTED_SUFFIXES:
            raise MusicLibraryError("不支持此音频格式；支持 WAV、MP3、FLAC、OGG、Opus、M4A、AAC")

        upload_dir = self.upload_root
        with self._upload_lock:
            try:
                usage = self.upload_usage()
                if size_bytes > usage["available_bytes"]:
                    raise MusicLibraryError(
                        f"上传空间不足：当前最多还能保存 {usage['available_bytes'] // (1024 * 1024)} MB；"
                        "系统会保留至少 128 MB 空间")
                upload_dir.mkdir(mode=0o700, exist_ok=True)
                info = upload_dir.lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise MusicLibraryError("上传目录不安全")
                os.chmod(upload_dir, 0o700)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                original = pathlib.Path(name)
                stem = original.stem
                for index in range(10000):
                    candidate = name if index == 0 else f"{stem} ({index}){suffix}"
                    target = upload_dir / candidate
                    try:
                        fd = os.open(target, flags, 0o600)
                        break
                    except FileExistsError:
                        continue
                else:
                    raise MusicLibraryError("同名文件过多，无法保存上传文件")
                written = 0
                try:
                    with os.fdopen(fd, "wb") as output:
                        for chunk in chunks:
                            if not isinstance(chunk, bytes):
                                raise MusicLibraryError("上传数据无效")
                            written += len(chunk)
                            if written > size_bytes or written > MAX_UPLOAD_BYTES:
                                raise MusicLibraryError("上传数据超过声明大小或 256 MB 限制")
                            if chunk and shutil.disk_usage(upload_dir).free - len(chunk) < UPLOAD_FREE_SPACE_RESERVE_BYTES:
                                raise MusicLibraryError("板端可用空间不足；为保护系统，已取消上传")
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if written != size_bytes:
                        raise MusicLibraryError("上传未完整，请重新选择文件")
                    self._invalidate_catalog()
                    duration = (self._duration(target) if suffix == ".wav"
                                else self._probe_duration(target))
                    if duration is None:
                        raise MusicLibraryError("无法识别音频内容；请确认文件完整且格式受支持")
                    return next(track for track in self.list_tracks()
                                if track["relative_path"] ==
                                "uploads/" + target.relative_to(upload_dir).as_posix())
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
            except OSError as exc:
                raise MusicLibraryError(f"保存上传文件失败: {exc}") from exc

    def upload_usage(self):
        """Return the upload quota and headroom while preserving system disk reserve."""
        upload_dir = self.upload_root
        used = 0
        try:
            try:
                info = upload_dir.lstat()
            except FileNotFoundError:
                info = None
            if info is not None:
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise MusicLibraryError("上传目录不安全")
                for current, dirs, files in os.walk(upload_dir, followlinks=False):
                    base = pathlib.Path(current)
                    dirs[:] = [name for name in dirs if not (base / name).is_symlink()]
                    for name in files:
                        path = base / name
                        try:
                            file_info = path.lstat()
                        except FileNotFoundError:
                            continue
                        if stat.S_ISREG(file_info.st_mode):
                            used += file_info.st_size
            probe = upload_dir
            while not probe.exists():
                probe = probe.parent
            free = shutil.disk_usage(probe).free
        except FileNotFoundError:
            free = shutil.disk_usage(self.root).free
        except OSError as exc:
            raise MusicLibraryError(f"无法读取上传空间状态: {exc}") from exc
        available = min(MAX_UPLOAD_LIBRARY_BYTES - used,
                        free - UPLOAD_FREE_SPACE_RESERVE_BYTES,
                        MAX_UPLOAD_BYTES)
        return {"used_bytes": used, "max_bytes": MAX_UPLOAD_LIBRARY_BYTES,
                "available_bytes": max(0, available)}

    def delete_upload(self, track_id):
        """Delete only a regular file inside uploads/, never a library source track."""
        with self._upload_lock:
            _track, path = self.get_track(track_id)
            if not path.is_relative_to(self.upload_root):
                raise MusicLibraryError("只能删除手机上传的曲目")
            with self._lock:
                process = self._processes.get(track_id)
                if process is not None and process.poll() is None:
                    raise MusicLibraryError("曲目仍在播放，停止播放后再删除")
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise MusicLibraryError("上传曲目不是普通文件，拒绝删除")
            path.unlink()
            self._invalidate_catalog()
            return self.upload_usage()

    def source_uri(self, track_id):
        _, _ = self.get_track(track_id)
        with self._lock:
            source = self._sources.get(track_id)
            if source is None:
                token = uuid.uuid4().hex[:12]
                fifo = self._fifo_path(track_id, token)
                source = self._format_source_uri(fifo, f"music-{track_id}-{token}")
                self._sources[track_id] = source
            return source

    @staticmethod
    def _format_source_uri(fifo, stream_id):
        query = urllib.parse.urlencode({"name": stream_id, "codec": "pcm",
                                        "sampleformat": "48000:16:2", "mode": "read"})
        return f"pipe://{urllib.parse.quote(str(fifo), safe='/')}?{query}"

    def start(self, track_id, source=None):
        track, audio_path = self.get_track(track_id)
        with self._lock:
            previous = self._processes.get(track_id)
            if previous is not None and previous.poll() is None:
                return track
            self._processes.pop(track_id, None)
            self._last_errors.pop(track_id, None)
            self._paused.discard(track_id)
            if source is not None:
                fifo = self._fifo_from_source(track_id, source)
                self._sources[track_id] = source
            else:
                source = self._sources.get(track_id)
                if source is None:
                    token = uuid.uuid4().hex[:12]
                    fifo = self._fifo_path(track_id, token)
                    source = self._format_source_uri(fifo, f"music-{track_id}-{token}")
                    self._sources[track_id] = source
                else:
                    fifo = self._fifo_from_source(track_id, source)
            if fifo.is_symlink() or (fifo.exists() and not stat.S_ISFIFO(fifo.lstat().st_mode)):
                raise MusicLibraryError(f"音源出口不是 FIFO，拒绝覆盖: {fifo}")
            if not fifo.exists():
                os.mkfifo(fifo, 0o600)
            else:
                os.chmod(fifo, 0o600)
            log_path = self.fifo_root / f"{fifo.stem}.ffmpeg.log"
            log = open(log_path, "ab", buffering=0)
            command = [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "warning",
                       "-re", "-i", str(audio_path), "-vn", "-acodec", "pcm_s16le",
                       "-ar", "48000", "-ac", "2",
                       "-f", "s16le", "-y", str(fifo)]
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
        self._stop_writer(track_id, preserve_source=False)

    def _stop_writer(self, track_id, preserve_source=False):
        with self._lock:
            process = self._processes.pop(track_id, None)
            source = self._sources.pop(track_id, None)
            was_paused = track_id in self._paused
            self._paused.discard(track_id)
            self._position_base.pop(track_id, None)
            self._position_started.pop(track_id, None)
        if process is not None and process.poll() is None:
            if was_paused:
                try:
                    os.killpg(process.pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
        if not preserve_source:
            self._unlink_fifo_source(track_id, source)

    def stop_all(self):
        with self._lock:
            track_ids = list(self._processes)
        for track_id in track_ids:
            self.stop(track_id)

    def close(self):
        # The persisted MusicSession owns the source URI across controller
        # restarts. Stop writers but preserve their FIFO path/inode for recovery.
        with self._lock:
            track_ids = list(self._processes)
        for track_id in track_ids:
            self._stop_writer(track_id, preserve_source=True)
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
            if (track_id and not track_id.startswith("radio:")
                    and session.get("state") in ("playing", "paused")
                    and track_id not in active):
                active[track_id] = session
        ended = []
        with self._lock:
            for track_id, session in active.items():
                process = self._processes.get(track_id)
                if process is not None and process.poll() is not None:
                    exit_code = process.poll()
                    self._processes.pop(track_id, None)
                    source = self._sources.pop(track_id, None)
                    self._paused.discard(track_id)
                    self._position_base.pop(track_id, None)
                    self._position_started.pop(track_id, None)
                    # A completed queue item may be followed by the same track.
                    # Force that next attempt to receive a fresh stream/FIFO.
                    self._unlink_fifo_source(track_id, source)
                    if exit_code not in (0, -signal.SIGTERM, -signal.SIGKILL):
                        self._last_errors[track_id] = self._read_process_error(
                            track_id, exit_code, source)
                    if session.get("session_id"):
                        ended.append(session["session_id"])
                    continue
                if process is None:
                    self.start(track_id, source=session.get("source"))
                if session.get("state") == "paused":
                    self.pause(track_id)
                else:
                    self.resume(track_id)
            stale = set(self._processes) - set(active)
        for track_id in stale:
            self.stop(track_id)
        return ended

    def last_error(self, track_id):
        with self._lock:
            return self._last_errors.get(track_id)

    def _read_process_error(self, track_id, exit_code, source=None):
        fallback = f"音频解码进程异常退出（exit={exit_code}）"
        try:
            fifo = self._fifo_from_source(track_id, source) if source else self._fifo_path(track_id)
            path = self.fifo_root / f"{fifo.stem}.ffmpeg.log"
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 8192))
                lines = stream.read().decode("utf-8", errors="replace").splitlines()
            for line in reversed(lines):
                if re.search(r"error|failed|invalid|not found", line, re.IGNORECASE):
                    return (line.strip() or fallback)[:300]
        except OSError:
            pass
        return fallback

    def _fifo_path(self, track_id, token=None):
        if not isinstance(track_id, str) or len(track_id) != 20 or not all(
                c in "0123456789abcdef" for c in track_id):
            raise MusicLibraryError("无效曲目 ID")
        suffix = f"-{token}" if token else ""
        return self.fifo_root / f"{track_id}{suffix}.pcm"

    def _fifo_from_source(self, track_id, source):
        try:
            parsed = urllib.parse.urlsplit(source)
            query = urllib.parse.parse_qs(parsed.query)
            fifo = pathlib.Path(urllib.parse.unquote(parsed.path))
            name = query.get("name", [""])[0]
            legacy = fifo.name == f"{track_id}.pcm" and name == f"music-{track_id}"
            current = (fifo.name.startswith(track_id + "-") and fifo.name.endswith(".pcm")
                       and re.fullmatch(r"music-" + re.escape(track_id) + r"-[a-f0-9]{12}", name))
            if (parsed.scheme != "pipe" or parsed.netloc or not fifo.is_absolute()
                    or fifo.parent.resolve() != self.fifo_root or not (legacy or current)):
                raise ValueError("invalid source URI")
            return fifo
        except (TypeError, ValueError, OSError) as exc:
            raise MusicLibraryError("播放会话的 FIFO 音源 URI 无效") from exc

    def _unlink_fifo_source(self, track_id, source):
        if source is None:
            return
        try:
            fifo = self._fifo_from_source(track_id, source)
            if fifo.is_fifo() and not fifo.is_symlink():
                fifo.unlink()
        except (MusicLibraryError, OSError):
            pass

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
