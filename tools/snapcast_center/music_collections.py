#!/usr/bin/env python3
"""Persistent favorites, playlists, room presets and network radio stations."""
import json
import os
import pathlib
import re
import tempfile
import threading
import urllib.parse
import uuid


class CollectionError(ValueError):
    pass


class MusicCollections:
    def __init__(self, path):
        self.path = pathlib.Path(path) if path else None
        self.lock = threading.RLock()
        self.data = {"version": 1, "favorites": [], "playlists": [],
                     "device_groups": [], "stations": []}
        self._load()

    def _load(self):
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in ("favorites", "playlists", "device_groups", "stations"):
                    if isinstance(data.get(key), list):
                        self.data[key] = data[key]
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            # A corrupt user file should not prevent the player from starting.
            pass

    def _save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _name(value):
        if not isinstance(value, str):
            raise CollectionError("名称不能为空")
        value = " ".join(value.split()).strip()
        if not value or len(value) > 80:
            raise CollectionError("名称长度须为 1~80 个字符")
        return value

    @staticmethod
    def _id(value):
        return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) else None

    def list(self):
        with self.lock:
            return {key: list(self.data[key]) for key in self.data if key != "version"}

    def toggle_favorite(self, track_id):
        if not isinstance(track_id, str) or not re.fullmatch(r"[a-f0-9]{20}", track_id):
            raise CollectionError("曲目 ID 无效")
        with self.lock:
            if track_id in self.data["favorites"]:
                self.data["favorites"].remove(track_id)
                result = False
            else:
                self.data["favorites"].append(track_id)
                result = True
            self._save()
            return result

    def _upsert(self, key, name, item_id=None, **fields):
        name = self._name(name)
        with self.lock:
            rows = self.data[key]
            row = next((r for r in rows if r.get("id") == item_id), None) if item_id else None
            if item_id and row is None:
                raise CollectionError("项目不存在")
            if row is None:
                row = {"id": uuid.uuid4().hex, "created_at": __import__("time").time()}
                rows.append(row)
            row.update(fields)
            row["name"] = name
            self._save()
            return dict(row)

    def rename(self, key, item_id, name):
        name = self._name(name)
        with self.lock:
            row = next((r for r in self.data[key] if r.get("id") == item_id), None)
            if row is None:
                raise CollectionError("项目不存在")
            row["name"] = name
            self._save()
            return dict(row)

    def playlist_create(self, name, track_ids):
        if (not isinstance(track_ids, list) or len(track_ids) > 2000
                or any(not isinstance(x, str) or not re.fullmatch(r"[a-f0-9]{20}", x)
                       for x in track_ids)):
            raise CollectionError("播放列表曲目无效")
        return self._upsert("playlists", name, track_ids=list(dict.fromkeys(track_ids)))

    def playlist_update(self, item_id, **values):
        with self.lock:
            row = next((r for r in self.data["playlists"] if r.get("id") == item_id), None)
            if row is None:
                raise CollectionError("播放列表不存在")
            if "name" in values:
                row["name"] = self._name(values["name"])
            if "track_ids" in values:
                tracks = values["track_ids"]
                if (not isinstance(tracks, list) or len(tracks) > 2000
                        or any(not isinstance(x, str) or not re.fullmatch(r"[a-f0-9]{20}", x)
                               for x in tracks)):
                    raise CollectionError("播放列表曲目无效")
                row["track_ids"] = list(dict.fromkeys(tracks))
            self._save()
            return dict(row)

    def delete(self, key, item_id):
        with self.lock:
            old = self.data[key]
            self.data[key] = [row for row in old if row.get("id") != item_id]
            changed = len(old) != len(self.data[key])
            if changed:
                self._save()
            return changed

    def reorder(self, key, ids):
        with self.lock:
            rows = self.data[key]
            by_id = {row.get("id"): row for row in rows}
            if (not isinstance(ids, list) or len(ids) != len(rows)
                    or len(set(ids)) != len(ids) or set(ids) != set(by_id)):
                raise CollectionError("排序列表与当前项目不一致")
            self.data[key] = [by_id[item] for item in ids]
            self._save()
            return list(self.data[key])

    def device_group_create(self, name, player_ids):
        if (not isinstance(player_ids, list) or not player_ids or len(player_ids) > 64
                or any(not isinstance(x, str) or not x or len(x) > 200 for x in player_ids)):
            raise CollectionError("设备组需要 1~64 台有效设备")
        return self._upsert("device_groups", name, player_ids=list(dict.fromkeys(player_ids)))

    def station_create(self, name, url, station_id=None):
        name = self._name(name)
        if station_id is not None and not self._id(station_id):
            raise CollectionError("电台 ID 无效")
        if not isinstance(url, str) or len(url) > 2048:
            raise CollectionError("电台地址无效")
        parsed = urllib.parse.urlsplit(url.strip())
        try:
            parsed.port
        except ValueError as exc:
            raise CollectionError("电台端口无效") from exc
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment
                or any(ord(char) < 32 for char in url)):
            raise CollectionError("电台地址必须是 HTTP(S) 音频流或局域网共享地址")
        return self._upsert("stations", name, item_id=station_id, url=url.strip())
