"""报时意图(2026-10-16): 纯本地, 零云调用。

"现在几点了/几点了/报时" -> 本地时间 -> 本地 matcha 直接播报。
原路径走 qwen-flash(云) 意图+生成, 断网不可用且多 2-3s; 本模块与
volumeIntent/radioIntent 同款挂 handle_user_intent 前段。

匹配用封闭短语集合(去标点后精确等值), 不做包含匹配 -- "从北京到上海
要几点"这类含"几点"的真问题必须放行给 LLM。宁漏不抢: 漏掉的变体
(如"现在北京时间几点了")仍走原 LLM+上下文路径, 不损失能力。
板时钟为 UTC, 统一按东八区报时。
"""
import datetime

TAG = __name__

_NUMS = "零一二三四五六七八九十"


def _cn(x: int) -> str:
    """0-59 的中文数字(口语形)。"""
    if x <= 0:
        return _NUMS[0]
    if x < 10:
        return _NUMS[x]
    if x == 10:
        return "十"
    if x < 20:
        return "十" + _NUMS[x - 10]
    if x % 10 == 0:
        return _NUMS[x // 10] + "十"
    return _NUMS[x // 10] + "十" + _NUMS[x % 10]


def spoken_now(now=None) -> str:
    """东八区时间的口语形: '现在是下午三点零五分'。"""
    tz = datetime.timezone(datetime.timedelta(hours=8))
    now = now or datetime.datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    now = now.astimezone(tz)
    h, m = now.hour, now.minute
    if h < 5:
        period = "凌晨"
    elif h < 9:
        period = "早上"
    elif h < 12:
        period = "上午"
    elif h < 14:
        period = "中午"
    elif h < 18:
        period = "下午"
    else:
        period = "晚上"
    h12 = h % 12 or 12
    if m == 0:
        return f"现在是{period}{_cn(h12)}点整"
    minute = f"零{_cn(m)}分" if m < 10 else f"{_cn(m)}分"
    return f"现在是{period}{_cn(h12)}点{minute}"


_TIME_QUERIES = {
    "现在几点了", "几点了", "现在几点", "几点", "几点钟", "几点钟了",
    "现在几点钟", "现在几点钟了", "什么时间", "什么时间了", "现在什么时间",
    "现在什么时间了", "报时", "报一下时间", "现在时间", "现在的时间",
}


def match_time_query(text) -> bool:
    if not text:
        return False
    t = text.strip().strip("，。！？；,.!?; \t")
    return t in _TIME_QUERIES


async def handle_time_intent(conn, text) -> bool:
    from core.handle.sendAudioHandle import send_stt_message
    from core.handle.intentHandler import speak_txt
    import uuid

    await send_stt_message(conn, text)
    conn.sentence_id = str(uuid.uuid4().hex)
    speak_txt(conn, spoken_now())
    return True
