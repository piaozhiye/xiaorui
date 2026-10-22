"""定时提醒/计时器意图(服务端侧, 2026-10-15).

"帮我设一个三十秒的计时器" / "一分钟后提醒我" / "取消提醒"。
到点后服务端主动播报: 发 announce 消息让客户端进入播放态, 再走正常
TTS 管线说"时间到了"。与 volumeIntent 同款挂法(handle_user_intent 前段,
两种 intent_type 都经过)。

判定哲学同前: 宁漏不抢——必须有 设置/提醒/叫 类动词 + 时长, 剥干净后
剩实义词的一律放行("讲一个十分钟的睡前故事"是故事不是定时)。
仅支持时长制(秒/分钟/小时), 不支持"明早八点"类时刻制(放行给 LLM)。
"""
import asyncio
import json
import re
import time

TAG = __name__

# 定时器全局化(2026-10-15 review): 停止音乐/电台走断连, 绑在旧 conn 上的
# 定时会跟着死(实测定时撞电台场景必丢)。模块级表跨连接存活, _CONN 指针
# 由每次 listen start / 意图调用刷新, 到点时找活连接播。
_TIMERS = {}   # name -> {"task":, "speak":, "due": monotonic 时刻,
               #            "kind": clock|duration, "target_at": UTC epoch 秒,
               #            "dur_seconds": 时长键}  -- 定点取消按键匹配
_CONN = None


def note_conn(conn):
    from core.timer_service import service_for
    service = service_for(conn)
    if service:
        service.attach(conn)
    global _CONN
    if _CONN is not conn:
        try:
            conn.logger.bind(tag=TAG).info("定时连接指针已刷新")
        except Exception:
            pass
    _CONN = conn

from core.handle.volumeIntent import _parse_number  # 同一套 0-100+ 中文/ASCII 数字

_SET_VERBS = ("设", "定", "设置", "弄", "来", "帮我", "创建", "再",
               "是")   # 云ASR把"设"听岔成同音"是"(实测 帮我是一个三十秒的...)
# 云 ASR 会把"三十秒"写成"30s"(阿拉伯数字+英文后缀), 两套单位都收
_UNIT_SECONDS = {"秒": 1, "分钟": 60, "分": 60, "小时": 3600, "时": 3600, "钟": 60,
                 "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hour": 3600}
_NOUNS = ("计时器", "闹钟", "提醒", "定时", "倒计时")
_FILLERS = ("一个", "一下", "个", "的", "吧", "呢", "啊", "哦", "嗯", "了", "呀",
            "请", "我", "你", "给", "帮", "把", "再", "后", "半小时", "钟", "小时",
            "分钟", "分", "秒", "半个", "当", "跟", "让", "要", "想", "为", "和",
            "是", "设", "定", "弄", "来", "创建")

# 时长: [数字|半] 单位 [半]? -- "三十秒/十分钟/半小时/1小时/两分半"
_DUR_RE = re.compile(r"(半|[0-9零一二两三四五六七八九十百]+)\s*(小时|分钟|分|秒|时|钟|sec|min|hour|[smh])(半)?", re.IGNORECASE)


def _parse_duration(token: str, unit: str, tail_half: str):
    """'三十'+'分钟' -> 1800; '半'+'小时' -> 1800; '两'+'分'+'半' -> 150。"""
    if token == "半":
        value = 0.5
    else:
        n = _parse_number(token)
        if n is None:
            return None
        value = n
    unit_s = _UNIT_SECONDS.get(unit)
    if unit_s is None:
        return None
    value *= unit_s
    if tail_half:
        value += 0.5 * unit_s
    return value


def _speak_duration(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)}小时"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)}分钟"
    if seconds % 1 == 0:
        return f"{int(seconds)}秒"
    return f"{seconds}秒"


def match_timer_intent(text):
    """命中返回 dict(op=set/cancel, seconds, speak), 不命中返回 None。"""
    if not text:
        return None
    t = text.strip().strip("，。！？；,.!?; \t")
    if not t or len(t) > 24:
        return None
    if "取消" in t and any(n in t for n in _NOUNS):
        # 语序不限: "取消提醒"/"把计时器取消一下"; 剥净才算, "取消订单"不受影响
        dur = _DUR_RE.search(t)
        dur_seconds = None
        if dur is not None:
            d = _parse_duration(dur.group(1), dur.group(2), dur.group(3))
            if d is not None and 10 <= d <= 12 * 3600:
                dur_seconds = int(d)
        rest = t
        for n in sorted(_NOUNS, key=len, reverse=True):
            rest = rest.replace(n, "")
        rest = rest.replace("取消", "")
        rest = _DUR_RE.sub("", rest)   # "取消三十秒的提醒": 点名时长的取消
        for f in sorted(_FILLERS, key=len, reverse=True):
            rest = rest.replace(f, "")
        if not rest.strip():
            if dur is not None and (dur_seconds is None or len(_DUR_RE.findall(t)) != 1):
                return {"op": "invalid_cancel"}
            if dur_seconds is not None:
                # 点名时长只删该时长, 不殃及共存的其他提醒
                return {"op": "cancel", "seconds": dur_seconds}
            return {"op": "cancel"}
    m = _DUR_RE.search(t)
    if not m:
        return None
    seconds = _parse_duration(m.group(1), m.group(2), m.group(3))
    if seconds is None or seconds < 10 or seconds > 12 * 3600:
        return None
    rest = t.replace(m.group(0), "")
    # "X后提醒我/叫我"句式本身自带意图, 不另要求设置动词
    # 三条件即意图: 时长(已匹配) + 定时名词或提醒句式 + 剩余剥净。
    # 不再要求设置动词 -- 云ASR把"帮我设"听岔成"当我是个"(bang->dang->当,
    # 设->是), 同音家族枚举不完; 名词门+剥净门才是真防线, "不/有/什么"
    # 类残留词天然挡住疑问句和陈述句。
    cue = any(c in t for c in ("提醒我", "叫我", "叫醒我"))
    if not (any(n in rest for n in _NOUNS) or cue):
        return None
    # 多字词先剥: 单字"设/定"会把"设置/定时"拆出残字("设置"->"置")
    for n in sorted(_NOUNS, key=len, reverse=True):
        rest = rest.replace(n, "")
    for v in sorted(_SET_VERBS, key=len, reverse=True):
        rest = rest.replace(v, "")
    for cue in ("提醒我", "叫醒我", "叫我", "提醒"):
        rest = rest.replace(cue, "")
    for f in _FILLERS:
        rest = rest.replace(f, "")
    if rest.strip():
        return None
    return {"op": "set", "seconds": int(seconds), "speak": _speak_duration(seconds)}


def _pick_cancel_targets(intent):
    """按意图挑出该取消的提醒键名(纯函数, 多提醒共存单测入口)。
    None=未点名全删; []=点名了但没有匹配项。
    匹配键=实际目标时间戳 / 时长秒数, 设定时已随条目存档。"""
    if intent.get("op") == "cancel_clock":
        target = intent.get("target_at")
        if target is None:
            return []
        return [name for name, it in _TIMERS.items()
                if it.get("kind") == "clock" and it.get("target_at") == target]
    if intent.get("op") == "cancel" and intent.get("seconds") is not None:
        sec = intent["seconds"]
        return [name for name, it in _TIMERS.items()
                if it.get("kind") == "duration" and it.get("dur_seconds") == sec]
    return None if intent.get("op") == "cancel" else []


async def handle_timer_intent(conn, text, intent):
    from core.handle.sendAudioHandle import send_stt_message
    from core.handle.intentHandler import speak_txt
    import uuid

    if intent["op"] == "invalid_cancel":
        await send_stt_message(conn, text)
        conn.sentence_id = str(uuid.uuid4().hex)
        speak_txt(conn, "取消范围不明确，请指定一个十秒到十二小时之间的提醒时长")
        return True

    from core.timer_service import service_for
    service = service_for(conn)
    if service:
        service.attach(conn)
        await send_stt_message(conn, text)
        conn.sentence_id = str(uuid.uuid4().hex)
        if intent["op"] in ("cancel", "cancel_clock"):
            victims = await asyncio.to_thread(service.store.cancel, service.device, intent)
            if intent["op"] == "cancel" and intent.get("seconds") is None:
                reply = "好的，已取消全部提醒" if victims else "当前没有正在进行的提醒"
            elif victims:
                reply = f"好的，已取消{victims[0]['speak']}的提醒"
            else:
                reply = "没有找到这个时间的提醒" if intent["op"] == "cancel_clock" else "没有找到这个时长的提醒"
        else:
            timer_id = await asyncio.to_thread(service.store.add, service.device, intent, time.time())
            speak = intent["speak"]
            reply = (f"好的，{speak}提醒你" if "点" in speak else f"好的，{speak}后提醒你") \
                if timer_id else "最多同时三个提醒，请先取消一个"
        speak_txt(conn, reply)
        return True

    if intent["op"] in ("cancel", "cancel_clock"):
        await send_stt_message(conn, text)
        conn.sentence_id = str(uuid.uuid4().hex)
        names = _pick_cancel_targets(intent)
        if names is None:   # 未点名 -> 全删
            n = len(_TIMERS)
            for item in list(_TIMERS.values()):
                item["task"].cancel()
            _TIMERS.clear()
            speak_txt(conn, "好的，已取消全部提醒" if n else "当前没有正在进行的提醒")
            conn.logger.bind(tag=TAG).info(f"定时取消: 原有{n}个")
            return True
        victims = []
        for name in names:
            item = _TIMERS.get(name)
            if item is not None:
                item["task"].cancel()   # done 回调负责出表
                victims.append(item["speak"])
        if victims:
            speak_txt(conn, f"好的，已取消{victims[0]}的提醒")
            conn.logger.bind(tag=TAG).info(f"定点取消: {victims[0]}")
        elif intent.get("h24") is not None:
            speak_txt(conn, "没有找到这个时间的提醒")
        else:
            speak_txt(conn, "没有找到这个时长的提醒")
        return True

    global _CONN
    _CONN = conn
    if len(_TIMERS) >= 3:
        await send_stt_message(conn, text)
        conn.sentence_id = str(uuid.uuid4().hex)
        speak_txt(conn, "最多同时三个提醒，请先取消一个")
        return True

    await send_stt_message(conn, text)
    conn.sentence_id = str(uuid.uuid4().hex)
    # 时刻式("明早八点")不带"后", 时长式("三十秒")要"后"
    speak = intent["speak"]
    speak_txt(conn, f"好的，{speak}提醒你" if "点" in speak else f"好的，{speak}后提醒你")
    delay = intent["seconds"]
    task = asyncio.create_task(_fire_later(delay, intent["speak"]))
    _TIMERS[str(task.get_name())] = {
        "task": task, "speak": intent["speak"], "due": time.monotonic() + delay,
        "kind": "clock" if intent.get("h24") is not None else "duration",
        "h24": intent.get("h24"), "minute": intent.get("minute"),
        "target_at": intent.get("target_at"),
        "dur_seconds": None if intent.get("h24") is not None else delay}

    def _done(task_):
        _TIMERS.pop(str(task_.get_name()), None)
    task.add_done_callback(_done)
    conn.logger.bind(tag=TAG).info(f"定时设置: {intent['speak']}({delay}s) 现存{len(_TIMERS)}个")
    return True


def _ws_alive(conn):
    """三种 ws 库的存活判断, 兜底返回 False(宁可错过不误发):
    - websockets 库 ServerConnection: .state 枚举 OPEN/CLOSING/CLOSED
      (既无 .closed 也无 .client_state, 兜底 True 会把死连当活, send 炸
      'no close frame received or sent' -- 2026-10-15 实测)
    - aiohttp: .closed
    - starlette: .client_state 枚举 CONNECTED/DISCONNECTED
    """
    ws = getattr(conn, "websocket", None)
    if ws is None:
        return False
    if hasattr(ws, "closed"):               # aiohttp
        return not ws.closed
    state = getattr(ws, "state", None)
    if state is not None:                    # websockets 库(State 枚举, str() 形式跨版本不稳)
        return "OPEN" in str(getattr(state, "name", state)).upper()
    client_state = getattr(ws, "client_state", None)  # starlette
    if client_state is not None:
        return str(client_state).endswith("CONNECTED")
    return False


async def _live_conn_or_none(deadline):
    while time.monotonic() < deadline:
        conn = _CONN
        if conn is not None and _ws_alive(conn):
            return conn
        await asyncio.sleep(3)
    return None


async def _fire_later(delay, speak):
    """到点播报: 不绑定设置时的连接 -- 停止音乐走断连会让旧连接作废
    (实测), 到点时从全局指针找活连接; 等重连最多 120s。音乐/电台播放中
    等它结束(TTS worker 被无限流阻塞, speak 排队会丢/僵尸复活), 上限 1h。"""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    conn = await _live_conn_or_none(time.monotonic() + 120)
    if conn is None:
        return
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if getattr(getattr(conn, "tts", None), "music_sentence_id", None) is not None:
            break   # 音乐播放中 last_activity 恒新鲜, 静默等待无意义, 交给下面的音乐等待
        last = getattr(conn, "last_activity_time", 0) or 0
        if time.time() * 1000 - last > 8000:
            break
        await asyncio.sleep(1)
        if not _ws_alive(conn):
            conn = await _live_conn_or_none(time.monotonic() + 30)
            if conn is None:
                return
            deadline = time.monotonic() + 60
    music_deadline = time.monotonic() + 3600
    while getattr(getattr(conn, "tts", None), "music_sentence_id", None) is not None:
        if time.monotonic() > music_deadline:
            conn.logger.bind(tag=TAG).warning(
                f"提醒({speak})因音乐长播超1h放弃")
            return
        await asyncio.sleep(2)
        if not _ws_alive(conn):
            conn = await _live_conn_or_none(time.monotonic() + 30)
            if conn is None:
                return
    # 发送前总取活连接: 静默/音乐等待期间连接可能已被"停止音乐"(断连)换掉
    if not _ws_alive(conn):
        conn = await _live_conn_or_none(time.monotonic() + 30)
        if conn is None:
            conn.logger.bind(tag=TAG).error(f"提醒({speak})找不到活连接, 丢弃")
            return
    try:
        from core.session_coordinator import coordinator
        delivered = await coordinator(conn).request(
            f"时间到了，这是你设置的{speak}提醒", priority=0, ttl=120)
        conn.logger.bind(tag=TAG).info(f"定时触发: {speak} delivered={delivered}")
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"定时播报失败: {e}")

# ---------- 时刻制闹钟(2026-10-16): "明早八点叫我/下午三点半提醒我" ----------
import re as _re

_CLOCK_RE = _re.compile(
    r"(明早|明天早上|明天上午|明天中午|明天下午|明天晚上|明晚|今晚|今天早上|今天上午|今天中午|今天下午|"
    r"明天|今天|凌晨|早上|上午|中午|下午|晚上)?"
    r"([0-9零一二两三四五六七八九十]+)点"
    r"(半|([0-9一二两三四五六七八九十]+)分?)?"
    r"(的)?(闹钟|提醒|定时|叫我|叫醒我)")
# 云 ASR 会把"早上八点二十四分"写成"8:24a.m."(实测), 冒号式单独收
_CLOCK_COLON_RE = _re.compile(
    r"([0-9]{1,2})[:：]([0-9]{2})"
    r"\s*(a\.m\.|am|AM|p\.m\.|pm|PM)?"
    r"(的)?(闹钟|提醒|定时|叫我|叫醒我)")
_PERIOD_HOUR = {"凌晨": 0, "早上": 5, "上午": 8, "中午": 11, "下午": 13, "晚上": 17}
_PERIOD_MAX = {"凌晨": 5, "早上": 8, "上午": 11, "中午": 13, "下午": 17, "晚上": 24}
# 时段表(含单字兜底, 最长优先) -- 中文式与冒号式共用
_PERIOD_BASES = (
    ("凌晨", 0), ("明天早上", 5), ("明天上午", 8), ("明天中午", 11),
    ("明天下午", 13), ("明天晚上", 17), ("明晚", 17), ("今晚", 17),
    ("今天早上", 5), ("今天上午", 8), ("今天中午", 11), ("今天下午", 13),
    ("今天晚上", 17), ("早上", 5), ("上午", 8), ("中午", 11), ("下午", 13),
    ("晚上", 17), ("晚", 17), ("早", 5),
)
# 冒号式头段扫时段用: 去单字兜底, 防"早/晚"孤字误命中(如问候语)
_PERIOD_HEAD = tuple((w, b) for w, b in _PERIOD_BASES if len(w) >= 2)


def _cn_minute(n):
    """分钟 -> 口语串: 30='半', 5='零五分', 24='二十四分'(与中文式 speak 对齐)。"""
    CN = "零一二三四五六七八九十"
    if n == 0:
        return ""
    if n == 30:
        return "半"
    if n < 10:
        return "零" + CN[n] + "分"
    if n == 10:
        return "十分"
    if n < 20:
        return "十" + CN[n - 10] + "分"
    return CN[n // 10] + "十" + (CN[n % 10] + "分" if n % 10 else "")


def _speak_clock_cn(h24, minute):
    """24h 时刻 -> 口语短语: 15:30='下午三点三十分', 0:30='凌晨零点半'。"""
    CN = "零一二三四五六七八九十"
    h12 = h24 % 12 or 12
    hour_s = CN[h12] if h12 <= 10 else "十" + CN[h12 - 10]
    if h24 == 0:
        hour_s = "零"
    period = "凌晨" if h24 < 5 else "早上" if h24 < 9 else "上午" if h24 < 12 \
        else "中午" if h24 < 14 else "下午" if h24 < 18 else "晚上"
    return period + hour_s + "点" + _cn_minute(minute)


def _clock_target(h24, minute, tomorrow, roll_forward=True):
    """距下一次该时刻的秒数; 过期时刻滚到明天(与中文式同语义)。"""
    import datetime
    tz = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(tz)
    target = now.replace(hour=h24, minute=minute, second=0, microsecond=0)
    if tomorrow or (roll_forward and target <= now):
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds(), int(target.timestamp())


def _parse_clock(text, for_cancel=False):
    """'明早八点叫我' -> {"delay": 秒, "speak": 原时刻短语}; 解析不了 None。"""
    m = _CLOCK_RE.search(text)
    if not m:
        # 冒号式 "8:24a.m.提醒我"(云ASR实测产物)直接换算, 不再折中文回灌:
        # 回灌丢时段信息(2026-10-16 review 实测 "明天下午3:30p.m." 被折成
        # 明早三点半, "12:30a.m." 排到中午)
        mc = _CLOCK_COLON_RE.search(text)
        if not mc:
            return None
        h24 = int(mc.group(1))
        ampm = (mc.group(3) or "").lower()
        if "p" in ampm and h24 < 12:
            h24 += 12
        if "a" in ampm and h24 == 12:   # 12:xx a.m. = 凌晨 0:xx
            h24 = 0
        if not ampm:
            # 中文时段遇冒号数字("下午3:30提醒我"/"取消3:30下午的闹钟"):
            # 按 12 点制口语换算, 与中文式同时段同语义。时段可在时刻前
            # (头段)或紧随其后(尾窗 6 字), 远处的不算防误配
            scope = text[:mc.start()] + text[mc.end():mc.end() + 6]
            pbase = None
            for w, base in _PERIOD_HEAD:
                if w in scope:
                    pbase = base
                    break
            if pbase == 0:                      # 凌晨: 0-5 原样
                if h24 > 5:
                    return None
            elif pbase is not None and pbase >= 11:
                if h24 == 12:
                    h24 = 12 if pbase < 17 else 0   # 中午12=12:00; 晚上12=0点
                elif h24 < 12:
                    h24 += 12                     # 下午3=15, 晚上8=20, 中午1=13
        if h24 > 23:
            return None
        minute = int(mc.group(2))
        if minute > 59:
            return None
        tomorrow = "明" in text
        explicit_today = any(w in text for w in ("今天", "今晚", "今早"))
        delay, target_at = _clock_target(h24, minute, tomorrow,
                                       not (for_cancel and explicit_today))
        if not for_cancel and (delay < 10 or delay > 48 * 3600):
            return None
        phrase = ("明天" if tomorrow else "") + _speak_clock_cn(h24, minute)
        return {"delay": int(delay), "speak": phrase,
                "h24": h24, "minute": minute, "target_at": target_at}
    dayword, hour_s, minute_s, minute2 = m.group(1), m.group(2), m.group(3), m.group(4)
    hour = _parse_number(hour_s)
    if hour is None or hour > 12 and hour != 20 or hour == 0:
        # 口语 12 点制(1-12), 20(二十点罕见)兜底; 0 点须写"十二点"
        if hour is None or hour > 24:
            return None
    minute = 0
    if minute_s == "半":
        minute = 30
    elif minute2:
        minute = _parse_number(minute2)
        if minute is None or minute >= 60:
            return None
    tomorrow = "明" in (dayword or "")
    pbase = None
    for w, base in _PERIOD_BASES:
        if w and w in (dayword or ""):
            pbase = base
            break
    h24 = hour
    if pbase == 0:                        # 凌晨: 原样(0-5)
        if hour > 5:
            return None
    elif pbase is not None and pbase >= 11:
        if hour == 12:
            h24 = 12 if pbase < 17 else 0   # 中午12=12:00; 晚上12=次日0点
            if pbase >= 17:
                tomorrow = True
        elif hour < 12:
            h24 = hour + 12                 # 下午三=15, 晚上十=22, 中午一点=13
    # 早上/上午不做小时上限约束(早八/早十都合法), 12 点制由时段换算保证
    if h24 >= 24:
        h24 -= 24
        tomorrow = True
    explicit_today = any(w in text for w in ("今天", "今晚", "今早"))
    delay, target_at = _clock_target(h24, minute, tomorrow,
                                   not (for_cancel and explicit_today))
    if not for_cancel and (delay < 10 or delay > 48 * 3600):
        return None
    phrase = (m.group(1) or "") + hour_s + "点" + (minute_s if minute_s else "")
    return {"delay": int(delay), "speak": phrase,
            "h24": h24, "minute": minute, "target_at": target_at}


# 否定语义门(2026-10-16 review 实测: "取消明早八点闹钟"/"把今晚十点的闹钟删掉"
# 曾被时刻匹配当成新增)。头尾两段各自剥净才认 —— 整句乱扫会把
# "明早八点提醒我取消会议"误杀成取消。
_CLOCK_NEGATE_HEAD = ("取消", "不要", "不用", "删除", "删掉", "关掉", "关闭",
                      "别设", "别定", "别弄", "别来", "别开")
_HEAD_STRIP = ("设置", "创建", "新增", "添加", "那个", "这个", "了", "个", "一",
               "把", "我", "帮", "给", "请", "你", "的", "吧", "再", "要",
               "想", "弄", "开", "设", "定", "置", "来", "建")
_CLOCK_TAIL_CANCEL = ("取消", "删", "不要", "不用", "关")
_TAIL_STRIP = ("掉", "了", "吧", "一下", "呗", "啦", "呀", "哦", "嗯", "去")
_HEAD_PUNCT = "，。！？；、,.!?; \t"


def _head_wants_cancel(head: str) -> bool:
    for w in _CLOCK_NEGATE_HEAD:
        if w in head:
            rest = head.replace(w, "")
            for s in _HEAD_STRIP:
                rest = rest.replace(s, "")
            # "取消下午3:30"头段带时段词: 时段/日期词一并剥掉
            for s, _base in _PERIOD_BASES:
                rest = rest.replace(s, "")
            rest = rest.replace("明", "").replace("今", "")
            # 剥净判定必须清标点("不要设置，下午三点的闹钟"头段逗号残留)
            if not rest.strip(_HEAD_PUNCT):
                return True
    return False


def _tail_wants_cancel(tail: str) -> bool:
    """时刻短语之后: 词根开头且剥净才认("闹钟删掉"✓, "提醒我取消会议"✗)。"""
    t = tail.strip(_HEAD_PUNCT)
    for w in _CLOCK_TAIL_CANCEL:
        if t.startswith(w):
            rest = t[len(w):]
            for s in _TAIL_STRIP:
                rest = rest.replace(s, "")
            return not rest.strip(_HEAD_PUNCT)
    return False


def match_clock_alarm(text):
    """时刻制定时; 返回 {op:set/cancel_clock, ...} 或 None。
    两条路径都带 target_at(实际日期时间键, 不再用文本归一——
    "早上八点/晚上八点"归一后同为"八点"会误删, 2026-10-16 二轮 review)。"""
    if not text:
        return None
    m = _CLOCK_RE.search(text) or _CLOCK_COLON_RE.search(text)
    if not m:
        return None
    if _head_wants_cancel(text[:m.start()]) or _tail_wants_cancel(text[m.end():]):
        ti = _parse_clock(text, for_cancel=True)
        if ti is None:
            return {"op": "cancel_clock", "speak": None}
        return {"op": "cancel_clock", "speak": ti["speak"],
                "h24": ti["h24"], "minute": ti["minute"], "target_at": ti["target_at"]}
    ti = _parse_clock(text)
    if ti is None:
        return None
    ti["op"] = "set"
    ti["seconds"] = ti.pop("delay")
    return ti


_LIST_QUERIES = ("有什么提醒", "有什么闹钟", "我设了什么提醒", "设置了什么提醒",
                 "提醒列表", "闹钟列表", "还有几个提醒", "有几个提醒", "查一下提醒")


def match_timer_list_query(text) -> bool:
    if not text:
        return False
    t = text.strip().strip("，。！？；,.!?; 	")
    return t in _LIST_QUERIES


async def handle_timer_list_intent(conn, text) -> bool:
    from core.handle.sendAudioHandle import send_stt_message
    from core.handle.intentHandler import speak_txt
    import uuid

    await send_stt_message(conn, text)
    conn.sentence_id = str(uuid.uuid4().hex)
    from core.timer_service import service_for
    service = service_for(conn)
    if service:
        rows = await asyncio.to_thread(service.store.list, service.device)
        items = [dict(item, due=time.monotonic() + item["due_at"] - time.time()) for item in rows]
    else:
        items = sorted(_TIMERS.values(), key=lambda x: x["due"])
    if not items:
        speak_txt(conn, "现在没有正在进行的提醒")
        return True
    parts = []
    for it in items:
        left = it["due"] - time.monotonic()
        if left >= 3600:
            left_s = f"{int(left // 3600)}小时{int(left % 3600 // 60)}分后"
        elif left >= 60:
            left_s = f"{int(left // 60)}分钟后"
        else:
            left_s = f"{int(left)}秒后"
        parts.append(f"{it['speak']}({left_s})")
    speak_txt(conn, "你有" + "、".join(parts) + "的提醒")
    return True
