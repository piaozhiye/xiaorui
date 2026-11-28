#!/usr/bin/env python3
"""Persistence and validation tests for user-created music collections."""
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "snapcast_center"))

from music_collections import CollectionError, MusicCollections  # noqa: E402


class MusicCollectionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "collections.json"
        self.store = MusicCollections(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_favorites_and_playlists_persist_separately(self):
        self.assertTrue(self.store.toggle_favorite("a" * 20))
        playlist = self.store.playlist_create("通勤", ["b" * 20, "c" * 20])
        self.assertFalse(self.store.toggle_favorite("a" * 20))
        restored = MusicCollections(self.path).list()
        self.assertEqual(restored["favorites"], [])
        self.assertEqual(restored["playlists"][0]["id"], playlist["id"])
        self.assertEqual(restored["playlists"][0]["track_ids"], ["b" * 20, "c" * 20])

    def test_playlist_rename_reorder_tracks_and_delete(self):
        first = self.store.playlist_create("甲", ["a" * 20, "b" * 20])
        second = self.store.playlist_create("乙", ["c" * 20])
        renamed = self.store.playlist_update(first["id"], name="通勤")
        reordered = self.store.playlist_update(renamed["id"], track_ids=["b" * 20, "a" * 20])
        self.assertEqual(reordered["track_ids"], ["b" * 20, "a" * 20])
        self.store.reorder("playlists", [second["id"], first["id"]])
        self.assertEqual(self.store.list()["playlists"][0]["id"], second["id"])
        self.assertTrue(self.store.delete("playlists", first["id"]))

    def test_station_rejects_unsafe_or_invalid_schemes(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "https://u:p@example.com/a",
                    "http://example.com:99999/stream", "https://example.com/stream#fragment"):
            with self.subTest(url=url), self.assertRaises(CollectionError):
                self.store.station_create("bad", url)
        station = self.store.station_create("电台", "http://192.168.1.2:8000/stream")
        self.assertEqual(station["url"], "http://192.168.1.2:8000/stream")

    def test_device_group_validates_members_and_order(self):
        one = self.store.device_group_create("客厅", ["player-a", "player-b"])
        two = self.store.device_group_create("卧室", ["player-c"])
        self.assertEqual(self.store.reorder("device_groups", [two["id"], one["id"]])[0]["id"], two["id"])
        with self.assertRaises(CollectionError):
            self.store.device_group_create("空组", [])


if __name__ == "__main__":
    unittest.main()
