import hashlib
import json
from pathlib import Path
import struct
import subprocess


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "ui/third_party/lvgl"


def test_shipped_binaries_match_the_recorded_copy_and_architecture():
    metadata = json.loads((VENDOR / "upstream-differences.json").read_text())
    machines = {"aarch64": 183, "x86": 62}
    for architecture, machine in machines.items():
        relative = f"lib/{architecture}/liblvgl.so"
        contents = (VENDOR / relative).read_bytes()
        assert hashlib.sha256(contents).hexdigest() == metadata["binary_sha256"][relative]
        assert contents[:6] == b"\x7fELF\x02\x01"
        assert struct.unpack_from("<H", contents, 18)[0] == machine


def test_configuration_and_recorded_header_differences_are_current():
    metadata = json.loads((VENDOR / "upstream-differences.json").read_text())
    assert hashlib.sha256((VENDOR / "include/lv_conf.h").read_bytes()).hexdigest() == (
        metadata["config_sha256"])
    assert not (VENDOR / "include/lvgl/lv_conf.h").exists()
    assert not (VENDOR / "include/lvgl/lv_drivers").exists()
    for difference in metadata["differences"]:
        header = VENDOR / "include/lvgl" / difference["path"]
        assert hashlib.sha256(header.read_bytes()).hexdigest() == difference["vendored_sha256"]


def test_tick_adapter_links_standalone_and_advances_without_manual_ticks(tmp_path):
    source = tmp_path / "tick.c"
    source.write_text('''#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include "lv_systick.h"
int main(void) {
    uint32_t before = lv_systick();
    struct timespec delay = {.tv_nsec = 20000000};
    assert(nanosleep(&delay, 0) == 0);
    uint32_t elapsed = lv_systick() - before;
    assert(elapsed >= 10);
    return 0;
}
''')
    binary = tmp_path / "tick"
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-I", str(VENDOR / "include/lvgl/src/misc"), str(source),
                    "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=5)
