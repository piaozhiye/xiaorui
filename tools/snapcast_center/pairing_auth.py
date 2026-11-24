"""One-time local pairing codes and revocable browser credentials."""
import hashlib
import hmac
import ipaddress
import json
import os
import pathlib
import secrets
import tempfile
import threading
import time


class PairingAuth:
    CODE_TTL_SECONDS = 15 * 60
    MAX_ATTEMPTS_PER_ADDRESS = 5
    MAX_ATTEMPTS_PER_CODE = 20

    def __init__(self, state_file, code_file, clock=None):
        self.state_file = pathlib.Path(state_file).expanduser()
        self.code_file = pathlib.Path(code_file).expanduser()
        self.clock = clock or time.time
        self._lock = threading.RLock()
        self._attempts = {}
        self._total_attempts = 0
        self.state = self._read_state(self.state_file, {"version": 1, "devices": {}})
        if not isinstance(self.state, dict) or not isinstance(self.state.get("devices"), dict):
            self.state = {"version": 1, "devices": {}}
        self._normalize_devices()
        self.code_data = self._read_state(self.code_file, None)
        if not isinstance(self.code_data, dict):
            self.code_data = None
        self._normalize_code()
        if not self.state["devices"] and self.code_data is None:
            self.rotate_code()

    @staticmethod
    def _read_state(path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return default

    @staticmethod
    def _atomic_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _normalize_devices(self):
        devices = {}
        for device_id, value in self.state["devices"].items():
            if (isinstance(device_id, str) and isinstance(value, dict)
                    and isinstance(value.get("token_hash"), str)
                    and len(value["token_hash"]) == 64
                    and all(char in "0123456789abcdef" for char in value["token_hash"])):
                try:
                    created_at = int(value.get("created_at", 0))
                except (ValueError, TypeError, OverflowError):
                    created_at = 0
                devices[device_id] = {
                    "token_hash": value["token_hash"],
                    "name": self._clean_name(value.get("name", "浏览器")),
                    "created_at": created_at,
                }
        self.state = {"version": 1, "devices": devices}

    @staticmethod
    def _clean_name(value):
        name = value if isinstance(value, str) else "浏览器"
        name = "".join(char for char in name.strip() if char.isprintable())[:64]
        return name or "浏览器"

    def _normalize_code(self):
        value = self.code_data
        if (not isinstance(value, dict) or not isinstance(value.get("code"), str)
                or not value["code"].isdigit() or len(value["code"]) != 8
                or not isinstance(value.get("created_at"), (int, float))):
            self.code_data = None
            return
        if self.clock() - value["created_at"] > self.CODE_TTL_SECONDS:
            self.code_data = None
            self.code_file.unlink(missing_ok=True)

    def rotate_code(self):
        with self._lock:
            code = f"{secrets.randbelow(100_000_000):08d}"
            self.code_data = {"code": code, "created_at": self.clock()}
            self._atomic_json(self.code_file, self.code_data)
            self._attempts.clear()
            self._total_attempts = 0
            return code

    @property
    def code(self):
        with self._lock:
            self._normalize_code()
            return self.code_data["code"] if self.code_data else None

    def pair(self, code, name, remote_address):
        with self._lock:
            try:
                address = str(ipaddress.ip_address(remote_address))
            except ValueError:
                address = "unknown"
            attempts = self._attempts.get(address, 0)
            if (attempts >= self.MAX_ATTEMPTS_PER_ADDRESS
                    or self._total_attempts >= self.MAX_ATTEMPTS_PER_CODE):
                return None
            expected = self.code
            if (expected is None or not isinstance(code, str)
                    or not hmac.compare_digest(code, expected)):
                self._attempts[address] = attempts + 1
                self._total_attempts += 1
                return None
            token = secrets.token_urlsafe(32)
            device_id = secrets.token_hex(8)
            self.state["devices"][device_id] = {
                "token_hash": hashlib.sha256(token.encode("ascii")).hexdigest(),
                "name": self._clean_name(name),
                "created_at": int(self.clock()),
            }
            self._atomic_json(self.state_file, self.state)
            self.code_data = None
            self.code_file.unlink(missing_ok=True)
            self._attempts.clear()
            self._total_attempts = 0
            return {"device_id": device_id, "token": token}

    def authorize(self, token):
        with self._lock:
            if not isinstance(token, str) or len(token) > 128:
                return False
            candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
            return any(hmac.compare_digest(candidate, row["token_hash"])
                       for row in self.state["devices"].values())

    def devices(self):
        with self._lock:
            return [{"id": device_id, "name": row["name"], "created_at": row["created_at"]}
                    for device_id, row in sorted(self.state["devices"].items())]

    def revoke(self, device_id):
        with self._lock:
            if device_id not in self.state["devices"]:
                return False
            del self.state["devices"][device_id]
            self._atomic_json(self.state_file, self.state)
            return True
