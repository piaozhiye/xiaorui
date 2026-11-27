#!/usr/bin/env python3
"""Security and lifecycle tests for the confined Snapcast music library."""
import os
import pathlib
import json
import signal
import stat
import sys
import subprocess
import tempfile
import unittest
import urllib.parse
import wave
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "snapcast_center"))

from music_library import MusicLibrary, MusicLibraryError  # noqa: E402


class FakeProcess:
    def __init__(self):
        self.pid = 12345
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


class MusicLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.root = self.base / "musics"
        self.root.mkdir()
        self.wav = self.root / "dukou.wav"
        with wave.open(str(self.wav), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\0\0\0\0" * 44100)
        self.processes = []
        self.now = [100.0]

        def popen(command, **kwargs):
            process = FakeProcess()
            self.processes.append((process, command, kwargs))
            return process

        self.library = MusicLibrary(self.root, self.base / "pipes", ffmpeg="/usr/bin/ffmpeg",
                                    popen=popen, monotonic=lambda: self.now[0])

    def tearDown(self):
        with mock.patch("music_library.os.killpg"):
            self.library.close()
        self.tmp.cleanup()

    def test_catalog_lists_audio_metadata_but_uses_opaque_ids(self):
        tracks = self.library.list_tracks()
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["title"], "dukou.wav")
        self.assertEqual(tracks[0]["duration_seconds"], 1.0)
        self.assertEqual(len(tracks[0]["id"]), 20)
        self.assertNotIn("absolute_path", tracks[0])

    def test_catalog_skips_symlinks_and_unsupported_files(self):
        outside = self.base / "secret.wav"
        outside.write_bytes(b"private")
        (self.root / "escape.wav").symlink_to(outside)
        (self.root / "notes.txt").write_text("not audio")
        self.assertEqual([t["title"] for t in self.library.list_tracks()], ["dukou.wav"])

    def test_catalog_cache_avoids_rescanning_each_state_poll_and_expires_for_external_changes(self):
        first = self.library.list_tracks()
        extra = self.root / "later.wav"
        extra.write_bytes(self.wav.read_bytes())
        self.assertEqual(len(self.library.list_tracks()), len(first))
        self.now[0] += 5.1
        self.assertEqual(len(self.library.list_tracks()), len(first) + 1)

    def test_catalog_reads_and_caches_album_tags_without_cover_extraction(self):
        self.library.ffprobe = "/usr/bin/ffprobe"
        metadata = {"format": {"tags": {"TITLE": "Rainy Morning", "ARTIST": "Xiaorui", "ALBUM": "Demo"}},
                    "streams": [{"disposition": {"attached_pic": 1}, "tags": {}}]}
        completed = subprocess.CompletedProcess([], 0, json.dumps(metadata), "")
        with mock.patch("music_library.subprocess.run", return_value=completed) as run:
            first = self.library.list_tracks()[0]
            second = self.library.list_tracks()[0]
        self.assertEqual(first["title"], "Rainy Morning")
        self.assertEqual(first["artist"], "Xiaorui")
        self.assertEqual(first["album"], "Demo")
        self.assertNotIn("cover_url", first)
        self.assertNotIn("cover_url", second)
        self.assertEqual(second["title"], first["title"])
        self.assertEqual(run.call_count, 1)

    def test_track_ids_do_not_accept_paths(self):
        with self.assertRaises(MusicLibraryError):
            self.library.get_track("../secret.wav")

    def test_import_upload_saves_valid_audio_under_private_upload_directory(self):
        payload = self.wav.read_bytes()
        track = self.library.import_upload("../手机测试.wav", [payload[:17], payload[17:]], len(payload))
        self.assertEqual(track["title"], "手机测试.wav")
        self.assertEqual(track["duration_seconds"], 1.0)
        saved = self.root / track["relative_path"]
        self.assertTrue(saved.is_relative_to(self.root / "uploads"))
        self.assertEqual(saved.read_bytes(), payload)
        self.assertEqual(stat.S_IMODE((self.root / "uploads").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)

    def test_import_upload_rejects_unsupported_names_and_invalid_audio(self):
        with self.assertRaises(MusicLibraryError):
            self.library.import_upload("notes.txt", [b"hello"], 5)
        with self.assertRaises(MusicLibraryError):
            self.library.import_upload("broken.wav", [b"not a wave"], 10)
        self.assertEqual(list((self.root / "uploads").iterdir()), [])

    def test_import_upload_rejects_size_mismatch_without_leaving_partial_file(self):
        payload = self.wav.read_bytes()
        with self.assertRaises(MusicLibraryError):
            self.library.import_upload("partial.wav", [payload[:-1]], len(payload))
        self.assertEqual(list((self.root / "uploads").iterdir()), [])

    def test_upload_quota_preserves_system_free_space_and_reports_usage(self):
        payload = self.wav.read_bytes()
        with mock.patch("music_library.shutil.disk_usage",
                        return_value=SimpleNamespace(free=128 * 1024 * 1024 + 1)):
            with self.assertRaisesRegex(MusicLibraryError, "空间不足"):
                self.library.import_upload("too-large.wav", [payload], len(payload))
        self.assertEqual(self.library.upload_usage()["used_bytes"], 0)

    def test_uploaded_track_can_be_deleted_but_library_source_cannot(self):
        payload = self.wav.read_bytes()
        uploaded = self.library.import_upload("phone.wav", [payload], len(payload))
        self.assertEqual(self.library.upload_usage()["used_bytes"], len(payload))
        with self.assertRaisesRegex(MusicLibraryError, "只能删除"):
            self.library.delete_upload(self.library.list_tracks()[0]["id"])
        usage = self.library.delete_upload(uploaded["id"])
        self.assertEqual(usage["used_bytes"], 0)
        self.assertFalse((self.root / uploaded["relative_path"]).exists())

    def test_upload_root_can_live_outside_music_library(self):
        external = self.root.parent / "phone-uploads"
        library = MusicLibrary(self.root, self.base / "external-pipes", "/usr/bin/ffmpeg",
                               upload_root=external)
        try:
            payload = self.wav.read_bytes()
            uploaded = library.import_upload("external.wav", [payload], len(payload))
            self.assertEqual(uploaded["relative_path"], "uploads/external.wav")
            self.assertTrue((external / "external.wav").is_file())
            self.assertTrue((self.root / "uploads").exists() is False)
            self.assertEqual(library.upload_usage()["used_bytes"], len(payload))
            self.assertEqual(library.delete_upload(uploaded["id"])["used_bytes"], 0)
        finally:
            library.close()

    def test_source_is_realtime_pcm_fifo_inside_private_directory(self):
        track = self.library.list_tracks()[0]
        uri = self.library.source_uri(track["id"])
        self.assertTrue(uri.startswith("pipe:///"))
        self.assertIn("codec=pcm", uri)
        self.assertIn("sampleformat=48000%3A16%3A2", uri)
        self.library.start(track["id"])
        fifo = pathlib.Path(urllib.parse.unquote(urllib.parse.urlsplit(uri).path))
        self.assertTrue(stat.S_ISFIFO(fifo.lstat().st_mode))
        self.assertEqual(stat.S_IMODE(fifo.lstat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.base / "pipes").stat().st_mode), 0o700)
        _, command, kwargs = self.processes[0]
        self.assertIn("-re", command)
        self.assertIn("-y", command)
        self.assertIn("48000", command)
        self.assertEqual(kwargs["start_new_session"], True)

    def test_radio_source_decodes_http_input_to_private_snapcast_pcm_fifo(self):
        track_id = "radio:" + "c" * 32
        url = "https://radio.example/live.m3u8?token=a%2Fb"
        source = self.library.start_radio(track_id, url)
        fifo = pathlib.Path(urllib.parse.unquote(urllib.parse.urlsplit(source).path))
        self.assertTrue(source.startswith("pipe:///"))
        self.assertTrue(stat.S_ISFIFO(fifo.lstat().st_mode))
        self.assertEqual(stat.S_IMODE(fifo.lstat().st_mode), 0o600)
        _process, command, kwargs = self.processes[-1]
        self.assertEqual(command[command.index("-i") + 1], url)
        self.assertEqual(command[command.index("-ar") + 1], "48000")
        self.assertEqual(command[-4:-2], ["-f", "s16le"])
        self.assertEqual(command[-1], str(fifo))
        self.assertEqual(kwargs["start_new_session"], True)
        with mock.patch("music_library.os.killpg"):
            self.library.stop(track_id)
        self.assertFalse(fifo.exists())

    def test_radio_sync_recovery_restarts_writer_on_persisted_fifo(self):
        track_id = "radio:" + "d" * 32
        url = "http://radio.example/live.mp3"
        source = self.library.start_radio(track_id, url)
        fifo = pathlib.Path(urllib.parse.unquote(urllib.parse.urlsplit(source).path))
        with mock.patch("music_library.os.killpg"):
            self.library.close()
        self.library = MusicLibrary(self.root, self.base / "pipes", ffmpeg="/usr/bin/ffmpeg",
                                    popen=lambda *a, **k: FakeProcess())
        self.library.sync_sessions([{"session_id": "radio-session", "track_id": track_id,
                                     "source": source, "source_url": url, "state": "playing"}])
        self.assertTrue(stat.S_ISFIFO(fifo.lstat().st_mode))

    def test_each_replay_gets_a_distinct_snapcast_stream_and_fifo(self):
        track_id = self.library.list_tracks()[0]["id"]
        first = self.library.source_uri(track_id)
        self.library.start(track_id)
        with mock.patch("music_library.os.killpg"):
            self.library.stop(track_id)
        second = self.library.source_uri(track_id)
        self.assertNotEqual(first, second)
        first_query = urllib.parse.parse_qs(urllib.parse.urlsplit(first).query)
        second_query = urllib.parse.parse_qs(urllib.parse.urlsplit(second).query)
        self.assertNotEqual(first_query["name"], second_query["name"])
        self.assertFalse(pathlib.Path(urllib.parse.unquote(
            urllib.parse.urlsplit(first).path)).exists())
        self.library.start(track_id)

    def test_sync_recovery_reuses_the_persisted_fifo_uri(self):
        track_id = self.library.list_tracks()[0]["id"]
        source = self.library.source_uri(track_id)
        fifo = pathlib.Path(urllib.parse.unquote(urllib.parse.urlsplit(source).path))
        self.library.start(track_id)
        with mock.patch("music_library.os.killpg"):
            self.library.close()
        # Simulate a new controller instance recovering an active session.
        self.library = MusicLibrary(self.root, self.base / "pipes", ffmpeg="/usr/bin/ffmpeg",
                                    popen=lambda *a, **k: FakeProcess())
        self.library.sync_sessions([{"session_id": "s1", "track_id": track_id,
                                     "source": source, "state": "playing"}])
        self.assertEqual(self.library.source_uri(track_id), source)
        self.assertTrue(stat.S_ISFIFO(fifo.lstat().st_mode))

    def test_process_error_reads_log_for_unique_fifo_source(self):
        track_id = self.library.list_tracks()[0]["id"]
        source = self.library.source_uri(track_id)
        fifo = pathlib.Path(urllib.parse.unquote(urllib.parse.urlsplit(source).path))
        (self.base / "pipes" / f"{fifo.stem}.ffmpeg.log").write_text(
            "decoder error: invalid packet\n", encoding="utf-8")
        self.assertEqual(self.library._read_process_error(track_id, 1, source),
                         "decoder error: invalid packet")

    def test_refuses_fifo_directory_symlink(self):
        outside = self.base / "outside"
        outside.mkdir()
        os.chmod(outside, 0o755)
        fifo_root = self.base / "pipes-link"
        fifo_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(MusicLibraryError):
            MusicLibrary(self.root, fifo_root, ffmpeg="/usr/bin/ffmpeg",
                         popen=lambda *a, **k: FakeProcess())
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)

    def test_only_one_controller_can_own_fifo_directory(self):
        with self.assertRaises(MusicLibraryError):
            MusicLibrary(self.root, self.base / "pipes", ffmpeg="/usr/bin/ffmpeg")

    def test_sync_restarts_missing_writer_and_reaps_finished_track(self):
        track_id = self.library.list_tracks()[0]["id"]
        sessions = [{"session_id": "s1", "track_id": track_id, "state": "playing"}]
        self.assertEqual(self.library.sync_sessions(sessions), [])
        process = self.processes[0][0]
        process.returncode = 0
        self.assertEqual(self.library.sync_sessions(sessions), ["s1"])

    def test_stop_all_terminates_writer_process_group(self):
        track_id = self.library.list_tracks()[0]["id"]
        self.library.start(track_id)
        process = self.processes[0][0]
        with mock.patch("music_library.os.killpg") as killpg:
            self.library.stop_all()
        killpg.assert_called_once()
        self.assertEqual(killpg.call_args.args[0], process.pid)
        self.assertEqual(process.returncode, 0)

    def test_pause_and_resume_signal_only_the_track_process_group(self):
        track_id = self.library.list_tracks()[0]["id"]
        self.library.start(track_id)
        process = self.processes[0][0]
        with mock.patch("music_library.os.killpg") as killpg:
            self.library.pause(track_id)
            self.library.pause(track_id)
            self.library.resume(track_id)
            self.library.resume(track_id)
        self.assertEqual(killpg.call_args_list, [
            mock.call(process.pid, signal.SIGSTOP),
            mock.call(process.pid, signal.SIGCONT),
        ])

    def test_position_clock_advances_freezes_on_pause_and_resumes(self):
        track_id = self.library.list_tracks()[0]["id"]
        self.library.start(track_id)
        self.now[0] += 0.4
        self.assertAlmostEqual(self.library.position(track_id), 0.4)
        with mock.patch("music_library.os.killpg"):
            self.library.pause(track_id)
            self.now[0] += 5
            self.assertAlmostEqual(self.library.position(track_id), 0.4)
            self.library.resume(track_id)
            self.now[0] += 0.25
        self.assertAlmostEqual(self.library.position(track_id), 0.65)

    def test_duration_probe_is_cached_for_non_wav_track(self):
        mp3 = self.root / "sample.mp3"
        mp3.write_bytes(b"audio")
        track_id = next(t["id"] for t in self.library.list_tracks() if t["title"] == "sample.mp3")
        with mock.patch.object(self.library, "_probe_duration", return_value=123.4) as probe:
            self.assertEqual(self.library.duration(track_id), 123.4)
            self.assertEqual(self.library.duration(track_id), 123.4)
        probe.assert_called_once()

    def test_sync_keeps_paused_sources_stopped_and_resumes_playing_sources(self):
        track_id = self.library.list_tracks()[0]["id"]
        with mock.patch("music_library.os.killpg") as killpg:
            self.library.sync_sessions([{"session_id": "s1", "track_id": track_id,
                                         "state": "paused"}])
            self.library.sync_sessions([{"session_id": "s1", "track_id": track_id,
                                         "state": "playing"}])
        self.assertEqual([call.args[1] for call in killpg.call_args_list], [
            signal.SIGSTOP, signal.SIGCONT,
        ])

    def test_stop_resumes_a_stopped_writer_before_terminating_it(self):
        track_id = self.library.list_tracks()[0]["id"]
        self.library.start(track_id)
        process = self.processes[0][0]
        with mock.patch("music_library.os.killpg") as killpg:
            self.library.pause(track_id)
            self.library.stop(track_id)
        self.assertEqual([call.args[1] for call in killpg.call_args_list], [
            signal.SIGSTOP, signal.SIGCONT, signal.SIGTERM,
        ])


if __name__ == "__main__":
    unittest.main()
