"""语音音量意图(服务端侧, 2026-10-15).

为什么在服务端: hybrid/cloud 的窗内指令走音频直传(端侧不跑本地识别),
客户端的本地文本钩子看不到这些轮次; 服务端 ASR 出文本后在这里判定,
发一条 volume 指令消息让客户端执行(客户端有软件增益+持久化+"好的"确认音)。

判定哲学与客户端 board/xr_volume.c 一致: 只认"整句几乎全是音量词"的说法,
剥掉全部音量词/数字/助词后还有实义词的一律放行给 LLM —— 宁漏不抢。
"""
import json
import re

TAG = __name__

# (词, 操作) 长词在前: "太大声"必须先于"大声", 否则方向判反
_OP_WORDS = (
    ("太大声", "down"), ("别出声", "min"), ("静音", "min"), ("最小", "min"),
    ("最大", "max"),
    ("小一点", "down"), ("小点声", "down"), ("小声点", "down"), ("小声些", "down"),
    ("再小点", "down"), ("轻一点", "down"), ("小些", "down"), ("太吵", "down"),
    ("小点", "down"), ("小声", "down"), ("调小", "down"), ("调低", "down"),
    ("大一点", "up"), ("大点声", "up"), ("大声点", "up"), ("大声些", "up"),
    ("再大点", "up"), ("响一点", "up"), ("大些", "up"), ("响点", "up"),
    ("大点", "up"), ("大声", "up"), ("调大", "up"), ("调高", "up"),
)
_FILLERS = (
    "音量", "声音", "声儿", "百分之", "调", "到", "把", "给", "请", "帮我",
    "帮", "我", "设置", "一下", "一点儿", "一点", "儿", "弄", "搞", "开",
    "再", "点", "吧", "呢", "啊", "哦", "噢", "嗯", "了", "吗", "呀", "哈",
    "谢谢", "下", "声",
)
_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "百": 100}
_PERCENT_RE = re.compile(r"百分之([0-9零一二两三四五六七八九十百]+)")
_BARE_NUM_RE = re.compile(r"([0-9]+|[零一二两三四五六七八九十百]+)%?")


def _parse_number(s):
    """0-100 的中文/ASCII 数字; 组合不了返回 None。"""
    if not s:
        return None
    if s.isdigit():
        v = int(s)
        return v if 0 <= v <= 100 else None
    if s == "一百":
        return 100
    if "百" in s:
        return 100 if s in ("一百", "百") else None
    if "十" in s:
        hi_s, _, lo_s = s.partition("十")
        hi = _DIGITS.get(hi_s, 1) if hi_s else 1
        lo = _DIGITS.get(lo_s, 0) if lo_s else 0
        if hi_s and hi_s not in _DIGITS:
            return None
        if lo_s and lo_s not in _DIGITS:
            return None
        v = hi * 10 + lo
        return v if 0 <= v <= 100 else None
    return _DIGITS.get(s)


def match_volume_intent(text):
    """命中返回 {"op": "up|down|set|max|min", "value": int}, 不命中返回 None。"""
    if not text:
        return None
    t = text.strip().strip("，。！？；,.!?; \t")
    if not t or len(t) > 24:
        return None
    val = None
    had_percent = False
    m = _PERCENT_RE.search(t)
    if m:
        val = _parse_number(m.group(1))
        had_percent = True
        t = t[:m.start()] + t[m.end():]
    op = None
    for w, o in _OP_WORDS:
        if w in t:
            op = o
            break
    if op is None and val is None:
        m = _BARE_NUM_RE.search(t)
        if m:
            val = _parse_number(m.group(1))
            had_percent = m.end() > m.end(1)   # 消费了 % 后缀
            t = t[:m.start()] + t[m.end():]
    has_ctx = ("音量" in text) or ("声音" in text) or ("声儿" in text)
    if op is None:
        if val is None or not (had_percent or has_ctx):
            return None
        op = "set"
    elif val is not None and had_percent and op not in ("min", "max"):
        op = "set"
    for w, _ in _OP_WORDS:
        t = t.replace(w, "")
    for w in _FILLERS:
        t = t.replace(w, "")
    for w, _ in _OP_WORDS:   # 删词后新邻接出的词再扫一遍
        t = t.replace(w, "")
    if t.strip():
        return None
    return {"op": op, "value": val if val is not None else 0}


async def send_volume_action(conn, text, intent):
    """回执文本 + 下发音量指令。客户端自己收轮(播"好的"后按正常回答收尾),
    服务端不排 TTS, 不进 LLM。"""
    from core.handle.sendAudioHandle import send_stt_message

    await send_stt_message(conn, text)
    await conn.websocket.send(json.dumps({
        "type": "volume",
        "op": intent["op"],
        "value": intent["value"],
        "session_id": conn.session_id,
    }))
    conn.logger.bind(tag=TAG).info(
        f"音量指令: op={intent['op']} value={intent['value']} text={text}")
