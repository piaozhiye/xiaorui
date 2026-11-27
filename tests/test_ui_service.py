import os
from pathlib import Path
import signal
import socket
import subprocess
import time


ROOT = Path(__file__).resolve().parents[1]


def test_exit_ui_stops_whole_stack_not_just_display():
    """退出 UI 时要停掉整套音频服务(前端/网关/LLM/NPU), 不只是界面。

    回归(2026-11-20 用户要求): UI 退出后 LLM 还占着 NPU, 后续 RockTTS
    合成没法独占, 用户还得手动停后台。
    """
    ui = (ROOT / "ui/main.c").read_text(encoding="utf-8")
    marker = ui.index("exit confirmed")
    window = ui[marker:marker + 300]
    assert "xiaorui-stop.sh" in window
    assert "setsid" in window          # 后台执行, 不被脚本里的 S99 stop 连带杀掉
    # 按钮文案要反映"退整套"而不是"只退界面"
    assert '"退出小瑞"' in ui
    assert "退出界面" not in ui
    # 按钮下方的说明也不能还写着"语音继续运行"(review Important 1)
    assert "语音继续运行" not in ui
    assert "一并释放" in ui


def test_ui_ignores_first_status_frame_to_avoid_stale_text():
    """UI 刚连上的第一帧 STATUS 是上一轮残留, 必须忽略。

    回归(2026-11-20): 重开 app 时文本框还显示上一次的问答。
    """
    ui = (ROOT / "ui/main.c").read_text(encoding="utf-8")
    assert "first_status_seen" in ui
    marker = ui.index("if (connected)")
    assert ui.index("first_status_seen", marker) < ui.index("displayed_question", marker) + 200
    # IPC 断线重连也要重置(避免旧文本闪现), review Minor 6
    reconnect = ui[ui.index("available != connected"):
                   ui.index("const xr_ui_message_t *status")]
    assert "first_status_seen = false" in reconnect


def test_cue_generator_skips_hmm_and_documents_alpha_direction():
    """生成脚本不能产出空的 hmm wav; alpha 说明方向要对。"""
    script = (ROOT / "tools/gen-rock-cues.py").read_text(encoding="utf-8")
    cues = script[script.index("CUES = {"):script.index("}", script.index("CUES = {"))]
    assert "xr_hmm24k.wav" not in cues       # RockTTS 单字无输出, 跳过
    assert "越大越慢" in script
    assert "越小越慢" not in script


def test_running_pids_matches_by_name_not_exe_path():
    """UI 进程查找必须按 comm, 不能按 exe 完整路径。

    回归: 二进制被 mv 改名后旧进程 exe 是 "...xiaorui_ui.old (deleted)",
    完整路径匹配会漏掉它 -> stop 杀不掉 -> 双进程抢屏幕、文本框不刷新。
    """
    service = (ROOT / "ports/rk3562/S99xiaorui-ui").read_text(encoding="utf-8")
    start = service.index("running_pids()")
    body = service[start:service.index("}", start)]
    assert "pgrep -x xiaorui_ui" in body
    assert "readlink" not in body


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


def test_voice_error_state_replaces_ready_when_voice_never_becomes_ready():
    """语音一直没就绪时不能显示'等你说一句', 要显示异常态。

    回归(2026-11-20 用户诉求): 180 秒兜底原来直接切 READY -> 界面说
    "等你说一句", 但语音服务其实没好, 用户说了没反应。
    """
    protocol = (ROOT / "board/ui_protocol.h").read_text(encoding="utf-8")
    assert "XR_UI_VOICE_ERROR" in protocol
    assert "XR_UI_TTS_GAIN" in protocol and "XR_UI_TTS_QUERY" in protocol
    source = (ROOT / "board/xiaorui_live.c").read_text(encoding="utf-8")
    # 没就绪且超过兜底 -> 异常态(而不是 READY)
    assert "XR_UI_VOICE_ERROR" in source
    assert "preparing_seconds()" in source
    ui = (ROOT / "ui/main.c").read_text(encoding="utf-8")
    assert "语音服务异常" in ui or "语音服务不可用" in ui


def test_touchscreen_exposes_independent_music_and_voice_volume_controls():
    source = (ROOT / "ui/main.c").read_text(encoding="utf-8")
    assert '"本机总音量（音乐 + 语音）"' in source
    assert '"小瑞语音 / TTS"' in source
    assert "XR_UI_TTS_GAIN" in source
    assert "xr_ui_client_tts_gain()" in source


def test_voice_preparing_restarts_clock_on_ws_reconnect():
    """ws 重连要重置预热起点, 不能让旧的兜底时间导致提前显示正常态。"""
    source = (ROOT / "board/xiaorui_live.c").read_text(encoding="utf-8")
    restart = source[source.index("restart:"):source.index("restart:") + 300]
    assert "g_started_at = now_s()" in restart


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
