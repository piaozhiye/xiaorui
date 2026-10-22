"""Pure, fail-closed scoring for one acoustic HA command."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "board-server/pkg"))
from core.handle.haIntent import match_light_cmd

# 每个用例: (标签, 音频, 期望终态, 强制前置态)
# 前置态强制取相反值 —— 否则目标态已等于期望态时"已经是开的"会零翻转算通过,
# 3/3 可以一个真翻转都没有(2026-10-20 实测 A1 就是 noop 假通过)。
CASES = (
    ("A1", "wake2_ha_on.wav", "on", "off"),
    ("A2", "wake2_ha_off.wav", "off", "on"),
    ("A3", "wake2_ha_on2.wav", "on", "off"),
)

# 声学抖动类失败: 唤醒漏检 / 识别错 / 灯没翻 / 审计没出 -> 重放同一段音频
RETRYABLE = frozenset((
    "no-voice-evidence", "missing-command", "wrong-command", "ambiguous-commands",
    "state-mismatch", "missing-player-finish", "missing-or-ambiguous-audit",
    "missing-complete-audio", "wrong-frontend-session",
))

# 审计契约被破坏: 立刻判失败, 不靠重放掩盖
HARD_FAIL = frozenset((
    "malformed-audit", "invalid-audit", "mismatched-completion",
    "invalid-noop", "extra-entity", "dangling-audit",
))

# 这些理由再等也不会变好(音频已经播完, 不会再来一次) -> 立刻收工进入下一次重放。
# 反过来, 审计/音频完成/灯态都是"还没到"而不是"坏了": 云端首包 2.5~3.2s,
# 审计的 completed 记录在 TTS 播完后才落盘(实测比 accepted 晚 2s),
# 早收工会把"再等两秒就好"判成失败。
SETTLED = frozenset((
    "no-voice-evidence", "wrong-command", "ambiguous-commands",
    "malformed-audit", "invalid-audit", "extra-entity",
    "mismatched-completion", "invalid-noop", "no-transition",
))

DEFAULT_FOLLOW_WINDOW_S = 30


def normalize(text):
    return re.sub(r"[\s，。！？,.!?]", "", text)


def exit_code(passed, failed, restored):
    return 0 if passed == 3 and failed == 0 and restored else 1


def playback_ready(mixer):
    """播放控件是否真的有声音。

    只看百分比会漏掉"音量 90% 但 -65dB"这种等于静音的状态 —— 那样跑完一轮
    三条用例只会得到三个 no-wake, 白烧几分钟。
    """
    levels = [int(value) for value in re.findall(r"\[(\d+)%\]", mixer)]
    if not levels or not any(levels) or "[off]" in mixer:
        return False
    decibels = [float(value) for value in re.findall(r"\[(-?[\d.]+)dB\]", mixer)]
    return all(value >= -40.0 for value in decibels)


def parse_follow_window(frontend_log):
    """前端跟说窗时长(秒)。

    以启动时的 [config] follow_window=Ns 为准(那是真实生效值);
    "回答完毕, Ns 内免唤醒" 里的 N 在源码里是写死的 30, 只作次选。
    """
    config = re.findall(r"follow_window=(\d+)s", frontend_log or "")
    if config:
        return int(config[-1])
    answered = re.findall(r"内免唤醒", frontend_log or "")
    fallback = re.findall(r"(\d+)s 内免唤醒", frontend_log or "")
    if answered and fallback:
        return int(fallback[-1])
    return DEFAULT_FOLLOW_WINDOW_S


def retryable(reason):
    """只有白名单里的声学抖动理由才重放。

    反过来写(不在 HARD_FAIL 里就重放)会把 adb/HA 掉线这类基础设施故障也当成
    声学抖动, 悄悄重放掉、把真问题藏起来。
    """
    return reason in RETRYABLE


def pre_state(want):
    return "off" if want == "on" else "on"


# 语音入口证据: 这一轮语音确实进过识别链。
# "KWS 命中"  = 声学唤醒证据
# "ASR 精修"  = 走了本地 RKNN 识别
# "跟说音频交服务端 ASR" = 音频直接交给服务端 ASR
# 注意: **不要求**声学 KWS 必须命中 —— 冷唤醒那一轮若 KWS 没命中, 前端会把
# 整句当普通语句送上云, 而云 ASR 反而更准(实测整句"小瑞小瑞帮我关灯"被云
# 一次认成"帮我关灯", 本地 RKNN 只认得出"小绿小绿")。那种形态链路是通的,
# 旧判据会把它判成 no-wake —— 假失败。
VOICE_EVIDENCE = ("KWS 命中", "ASR 精修", "跟说音频交服务端 ASR")
REJECTED_WAKE = "无唤醒单元"


def voice_evidence_before(frontend, position):
    """指令之前是否有一份可用的语音入口证据。

    "KWS 命中但文本无唤醒单元, 忽略" 是被文本门丢掉的那次命中, 不算证据
    (它意味着这一轮被丢了, 后面不该有真正的指令)。
    """
    head = frontend[:position]
    for marker in VOICE_EVIDENCE:
        for match in re.finditer(re.escape(marker) + r"[^\n]*", head):
            if marker == "KWS 命中" and REJECTED_WAKE in match.group(0):
                continue
            return True
    return False


def score(frontend, gateway, want, before, after, entity):
    frontend = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), frontend)
    commands = list(re.finditer(
        r"(?:ASR 回执:|\((?:唤醒|跟说中)\) 指令:)\s*([^\n]+)", frontend))
    if not commands:
        return False, "missing-command"
    # 同一轮可能出现两行: 前端本地 ASR 的决定("(跟说中) 指令: X")与后端回执
    # ("ASR 回执: X")。文本相同就是同一条指令, 只有文本不同才是"混入多指令"。
    if len({normalize(match[1].strip()) for match in commands}) > 1:
        return False, "ambiguous-commands"
    command = commands[0]
    if not voice_evidence_before(frontend, command.start()):
        # 这一轮的语音没进过识别链, 后面的证据都不可信
        return False, "no-voice-evidence"
    if match_light_cmd(normalize(command[1].strip())) != (want, "light"):
        return False, "wrong-command"
    records = []
    for line in gateway.splitlines():
        if "HA_AUDIT " in line:
            try:
                fragment = line.split("HA_AUDIT ", 1)[1].strip()
                # stdout 副本可能带尾随内容(控制符/换行), 用 raw_decode 取前缀
                record, _ = json.JSONDecoder().raw_decode(fragment)
                if not isinstance(record, dict):
                    return False, "malformed-audit"
                if (not isinstance(record.get("text"), str) or
                        not isinstance(record.get("entities"), list) or
                        not all(isinstance(e, str) for e in record["entities"])):
                    return False, "malformed-audit"
                records.append(record)
            except (ValueError, TypeError):
                return False, "malformed-audit"
    accepted_all = [r for r in records if r.get("phase") == "accepted"]
    if len(accepted_all) != 1:
        # 窗口内出现 0 条或多条"已受理": 0=没走到意图, 多=窗口串进了别的轮次
        # (上一轮的受理没等到完成就会永远悬在日志里, 不能当没看见)
        return False, "dangling-audit"
    accepted = [r for r in accepted_all if
                normalize(r.get("text", "")) == normalize(command[1]) and
                r.get("op") == want and r.get("domain") == "light" and
                entity in r.get("entities", [])]
    if len(accepted) != 1:
        return False, "missing-or-ambiguous-audit"
    record = accepted[0]
    if (not record.get("sentence_id") or not record.get("session_id") or
            record.get("result") not in ("acted", "noop")):
        return False, "invalid-audit"
    if set(record.get("entities", [])) != {entity}:
        # 意图会作用于全部可用灯; 夹带别的灯说明隔离前提被破坏
        return False, "extra-entity"
    complete = [r for r in records if r.get("phase") == "completed" and
                r.get("sentence_id") == record["sentence_id"] and
                r.get("session_id") == record["session_id"]]
    if (len(complete) != 1 or complete[0].get("aborted") is not False or
            type(complete[0].get("packets")) is not int or complete[0]["packets"] < 1):
        return False, "missing-complete-audio"
    if any(complete[0].get(key) != record.get(key)
           for key in ("text", "op", "domain", "entities", "result")):
        return False, "mismatched-completion"
    if before not in ("on", "off") or after != want:
        return False, "state-mismatch"
    if before == want:
        # 前置态等于期望态: 本轮没有任何翻转, 不构成一次成功的语音控制
        return False, "no-transition"
    if record["result"] != "acted":
        return False, "invalid-noop"
    reply = frontend[command.end():]
    sessions = list(re.finditer(r'"session_id"\s*:\s*"([^"\n]+)"', reply))
    matching = [event for event in sessions if event[1] == record["session_id"]]
    if not matching:
        return False, "wrong-frontend-session"
    if "回答完毕" not in reply[matching[0].end():]:
        return False, "missing-player-finish"
    return True, record["result"]
