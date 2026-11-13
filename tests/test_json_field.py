import ctypes
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def interface(tmp_path):
    library = tmp_path / "json_field.so"
    subprocess.run([
        "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
        str(ROOT / "board/json_field.c"),
        "-o", str(library),
    ], check=True)
    api = ctypes.CDLL(str(library))
    api.xr_json_field_string.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                         ctypes.c_char_p, ctypes.c_size_t]
    return api


@pytest.mark.parametrize("payload,capacity,expected,result", [
    (b'{"text":"\\u4f60\\u597d\\ud83d\\ude00"}', 64, "你好😀", 1),
    (json.dumps({"text": '说"你好"\\再见\n'}).encode(), 64, '说"你好"\\再见\n', 1),
    ('{"text":"你好😀结束"}'.encode(), 8, "你好", 1),
    ('{"text":"你好"}'.encode(), 1, "", 1),
    (b'{"nested":{"text":"wrong"},"text":"right"}', 64, "right", 1),
    (b'{"other":"absent"}', 64, "", 0),
    (b'{"text":"\\ud800"}', 64, "", -1),
    (b'{"text":"\\u0000"}', 64, "", -1),
    (b'{"text":"\xf0\x80\x80\x80"}', 64, "", -1),
    (b'{"text":"unfinished', 64, "", -1),
])
def test_unicode_decoder(interface, payload, capacity, expected, result):
    output = ctypes.create_string_buffer(capacity)
    assert interface.xr_json_field_string(payload, b"text", output, capacity) == result
    assert output.value.decode() == expected


@pytest.mark.parametrize("payload,expected", [
    (b'{"type": "tts", "state": "sentence_start", "text": "\\u4f60\\u597d"}', "你好"),
    (b'{"type":"tts","state":"sentence_start","text":"\\u4f60\\u597d"}', "你好"),
    (b'{"type": "stt", "text": "\\u4f60\\u597d", "session_id": "abc"}', "你好"),
])
def test_wire_format_of_the_gateway(interface, payload, expected):
    output = ctypes.create_string_buffer(256)
    assert interface.xr_json_field_string(payload, b"text", output, 256) == 1
    assert output.value.decode() == expected
