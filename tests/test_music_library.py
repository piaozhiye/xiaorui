#!/usr/bin/env python3
"""Security and lifecycle tests for the confined Snapcast music library."""
import os
import pathlib
import signal
import stat
import sys
import tempfile
import unittest
import wave
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

        def popen(command, **kwargs):
            process = FakeProcess()
            self.processes.append((process, command, kwargs))
            return process

        self.library = MusicLibrary(self.root, self.base / "pipes", ffmpeg="/usr/bin/ffmpeg",
                                    popen=popen)

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

    def test_track_ids_do_not_accept_paths(self):
        with self.assertRaises(MusicLibraryError):
            self.library.get_track("../secret.wav")

    def test_source_is_realtime_pcm_fifo_inside_private_directory(self):
        track = self.library.list_tracks()[0]
        uri = self.library.source_uri(track["id"])
        self.assertTrue(uri.startswith("pipe:///"))
        self.assertIn("codec=pcm", uri)
        self.assertIn("sampleformat=48000%3A16%3A2", uri)
        self.library.start(track["id"])
        fifo = self.base / "pipes" / f"{track['id']}.pcm"
        self.assertTrue(stat.S_ISFIFO(fifo.lstat().st_mode))
        self.assertEqual(stat.S_IMODE(fifo.lstat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.base / "pipes").stat().st_mode), 0o700)
        _, command, kwargs = self.processes[0]
        self.assertIn("-re", command)
        self.assertIn("-y", command)
        self.assertIn("48000", command)
        self.assertEqual(kwargs["start_new_session"], True)

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
