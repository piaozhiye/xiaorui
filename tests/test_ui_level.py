import ctypes
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_level_falls_all_the_way_to_silence(tmp_path):
    source = tmp_path / "level_wrap.c"
    source.write_text(
        '#include "board/ui_level.h"\n'
        "int next(int current, int target) { return xr_ui_level_next(current, target); }\n"
    )
    library = tmp_path / "level.so"
    subprocess.run([
        "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
        f"-I{ROOT}", str(source), "-o", str(library),
    ], check=True)
    interface = ctypes.CDLL(str(library))
    interface.next.argtypes = [ctypes.c_int, ctypes.c_int]
    interface.next.restype = ctypes.c_int

    value = 15
    for _ in range(200):
        value = interface.next(value, 0)
    assert value == 0, "差值 <16 时整数除法得 0，电平会永远停在 15"


def test_level_rises_at_once_and_never_undershoots(tmp_path):
    source = tmp_path / "level_wrap.c"
    source.write_text(
        '#include "board/ui_level.h"\n'
        "int next(int current, int target) { return xr_ui_level_next(current, target); }\n"
    )
    library = tmp_path / "level.so"
    subprocess.run([
        "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
        f"-I{ROOT}", str(source), "-o", str(library),
    ], check=True)
    interface = ctypes.CDLL(str(library))
    interface.next.argtypes = [ctypes.c_int, ctypes.c_int]
    interface.next.restype = ctypes.c_int

    assert interface.next(0, 80) == 80, "说话要立刻亮起来，不做延迟"
    for start, target in ((100, 0), (80, 13), (40, 39), (17, 2)):
        value = start
        for _ in range(400):
            value = interface.next(value, target)
            assert value >= target, "衰减不能穿过目标值"
        assert value == target
