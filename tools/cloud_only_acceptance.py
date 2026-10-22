"""Pure readiness checks for live frontend tests (no board access)."""
import re


def window_events(log):
    return sum(log.count(token) for token in (
        "回答完毕", "回执(唤醒)", "(纯唤醒, 等指令)"))


def startup_settled(log, elapsed, since_window, window_s):
    # Welcome is scheduled asynchronously with TTL=15s. Mic-ready is not idle.
    if elapsed < 16 or since_window < window_s + 2:
        return False
    begins = [m.start() for m in re.finditer(
        r"audio submitted:|主动播报已受理:|后端已换路续答", log)]
    ends = [m.start() for m in re.finditer(
        r"回答完毕|回执\(噪声\)|回执\(唤醒\)|空轮\(零音频\)|放弃本轮", log)]
    return max(begins, default=-1) <= max(ends, default=-1)


def capture_idle(log):
    diagnostics = list(re.finditer(r'\[diag\][^\n]*in_utt=([01])', log))
    if not diagnostics or diagnostics[-1][1] != "0":
        return False
    starts = list(re.finditer(r'检测到说话|KWS 触发采集', log))
    if starts and starts[-1].start() > diagnostics[-1].start():
        return False
    # Pending ASR/TTS must settle too; an old idle mic sample alone is not enough.
    return startup_settled(log, 1000, 1000, 30)
