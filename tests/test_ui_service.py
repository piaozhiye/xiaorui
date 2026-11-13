import os
from pathlib import Path
import signal
import socket
import subprocess
import time


ROOT = Path(__file__).resolve().parents[1]


def test_ui_service_start_is_idempotent_and_manual_exit_stays_exited(tmp_path):
    directory = tmp_path / "app"
    directory.mkdir()
    source = directory / "fake.c"
    source.write_text("""
#include <signal.h>
#include <unistd.h>
static volatile sig_atomic_t stopped;
static void stop(int number) { (void)number; stopped = 1; }
int main(void) {
    signal(SIGTERM, stop);
    while (!stopped) pause();
    return 0;
}
""")
    binary = directory / "xiaorui_ui"
    subprocess.run(["cc", str(source), "-o", str(binary)], check=True)
    (directory / "run.sh").write_text('#!/bin/sh\nexec "$XR_UI_DIRECTORY/xiaorui_ui"\n')
    environment = dict(os.environ, XR_UI_DIRECTORY=str(directory),
                       XR_UI_LOCK_FILE=str(tmp_path / "ui.lock"),
                       XDG_RUNTIME_DIR=str(tmp_path), WAYLAND_DISPLAY="wayland-0")

    def service(operation):
        return subprocess.run(["sh", str(ROOT / "ports/rk3562/S99xiaorui-ui"), operation],
                              env=environment, capture_output=True, text=True, timeout=25)

    def pids():
        candidates = subprocess.run(["pgrep", "-x", "xiaorui_ui"],
                                    capture_output=True, text=True).stdout.split()
        result = []
        for candidate in candidates:
            try:
                if os.readlink(f"/proc/{candidate}/exe") == str(binary):
                    result.append(int(candidate))
            except FileNotFoundError:
                pass
        return result

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as compositor:
        compositor.bind(str(tmp_path / "wayland-0"))
        try:
            assert service("start").returncode == 0
            original = pids()
            assert len(original) == 1
            assert service("start").returncode == 0
            assert pids() == original
            assert service("status").returncode == 0
            os.kill(original[0], signal.SIGTERM)
            for attempt in range(30):
                if not pids():
                    break
                time.sleep(0.1)
            assert service("status").returncode == 1
            time.sleep(0.2)
            assert not pids()
            assert service("start").returncode == 0
            assert service("stop").returncode == 0
            assert not pids()
        finally:
            service("stop")
