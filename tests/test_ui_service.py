import os
from pathlib import Path
import signal
import socket
import subprocess
import time


ROOT = Path(__file__).resolve().parents[1]


def test_state_chip_initial_text_uses_adaptive_width():
    """状态胶囊的初始文字必须按实际宽度自适应, 不能用硬编码宽度。

    回归(2026-11-20 用户反馈): 初始 "等待语音服务连接" 的 label 宽度被
    硬编码成 180px, 而这行字在 title_font(34px) 下要 ~240px, LVGL 的
    LV_LABEL_LONG_WRAP 会换行, 两行 68px 溢出 52px 高的胶囊 -> 文字跑到
    胶囊外面, 看着"不在中间"。
    """
    source = (ROOT / "ui/main.c").read_text(encoding="utf-8")
    marker = source.index('state_label = label(state_chip, "等待语音服务连接"')
    window = source[marker:marker + 400]
    assert "layout_state_chip(" in window, \
        "初始状态胶囊必须走 layout_state_chip 自适应宽度与居中"


def test_state_chip_measures_with_the_font_it_renders():
    """量宽度用的字体必须与 label 实际渲染的字体一致。"""
    source = (ROOT / "ui/main.c").read_text(encoding="utf-8")
    chip = source[source.index("static void layout_state_chip"):]
    chip = chip[:chip.index("}")]
    assert "title_font.font" in chip
    label_line = source[source.index('state_label = label(state_chip,'):]
    label_line = label_line[:label_line.index(";")]
    assert "title_font.font" in label_line


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
