"""HA 设备控制意图(2026-10-19): 开关灯/插座零 LLM 直调 Home Assistant REST。

设计沿 timeIntent 的"封闭短语集合+精确等值"路线, 宁漏不抢: "把台灯调成
暖光""插座多少钱"这类长尾必须放行给 LLM, 漏掉的变体以后由 LLM 工具路径
兜底, 不损失能力。

HA 侧(实测 2026-10-19, docs/HA-TESTING.md):
- 地址/令牌: XR_HA_URL(默认 http://127.0.0.1:8123) + XR_HA_TOKEN,
  token 缺省回退读 /data/xiaorui-server/ha_token.txt(3650 天长期令牌)。
- 用 urllib 不用 httpx: 板上 httpx 外部请求 TLS 握手超时的老坑(本地无
  TLS 倒不至于, 但统一走 urllib 少一份依赖行为)。
- REST 调用放 to_thread: 网关事件循环上不能有阻塞 IO。
- 目标 = 主设备(ha_entities 白名单): light.* 排除指示灯; switch.* 只选
  米家主电源开关(*_on_p_*), 无匹配宁可不动作——群发会误伤音箱静音/
  勿扰/空调网关等附属开关(实测踩过)。
- 状态感知回复: 已开还开 -> "灯已经是开的"; HA 不可达 -> 诚实报"没连
  上"并返回 True(吃掉本轮, 防止 LLM 幻觉"好的已开灯")。
- 短语对称性是硬要求: 补"打开X"必须同步检查"关闭X"(实测漏过"关闭插座")。
"""
import asyncio
import json
import os
import urllib.error
import urllib.request

TAG = __name__

HA_TOKEN_FILE = "/data/xiaorui-server/ha_token.txt"
_TIMEOUT = 3  # 秒; 本机回环正常 <50ms, 3s 已是 HA 重启中的量级


def _family(open_cores, close_cores, open_extra=(), close_extra=()):
    prefixes = ["", "帮我", "麻烦", "请", "给我", "我要", "我想"]
    on_, off_ = set(), set()
    for c in open_cores:
        on_.add(c)
        for p in prefixes:
            on_.add(p + c)
    for c in close_cores:
        off_.add(c)
        for p in prefixes:
            off_.add(p + c)
    on_.update(open_extra)
    off_.update(close_extra)
    return on_, off_


# 灯(light.*)
_ON, _OFF = _family(
    ["开灯", "开开灯", "打开灯", "把灯打开", "开一下灯", "开个灯",
     "灯打开", "灯开一下", "把灯开一下", "把灯开了"],
    ["关灯", "关关灯", "关闭灯", "把灯关掉", "把灯关了", "关一下灯", "关个灯",
     "灯关掉", "灯关一下", "灯关了", "把灯关闭"],
    # 云 ASR 常见变体(实测收编)
    open_extra=("开下灯", "帮我开下灯", "把灯整打开"),
    close_extra=("关下灯", "帮我关下灯"))

# 插座(switch.*) -- 2026-10-19 米家插座接入
_SW_ON, _SW_OFF = _family(
    ["开插座", "打开插座", "把插座打开", "开一下插座", "插座打开",
     "把插座开一下", "插座通电", "给插座通电", "通上电", "插上电"],
    ["关插座", "关掉插座", "把插座关掉", "把插座关了", "关一下插座",
     "插座关掉", "插座关了", "把插座关闭", "插座断电", "给插座断电",
     "断掉插座电", "拔了插座"],
    open_extra=("插座通一下电",),
    close_extra=("把插座电断了",
                 # 对称补齐(2026-10-19 用户实测漏词: "关闭插座"不命中)
                 "关闭插座", "帮我关闭插座", "请关闭插座", "给我关闭插座"))


def match_light_cmd(text):
    """(op, domain)/None: op='on'/'off', domain='light'/'switch'。
    去标点后精确等值, 不做包含匹配。"""
    if not text:
        return None
    t = text.strip().strip("，。！？；,.!?; \t")
    if t in _ON:
        return ("on", "light")
    if t in _OFF:
        return ("off", "light")
    if t in _SW_ON:
        return ("on", "switch")
    if t in _SW_OFF:
        return ("off", "switch")
    return None


def _ha_cfg():
    url = os.environ.get("XR_HA_URL", "http://127.0.0.1:8123").rstrip("/")
    tok = os.environ.get("XR_HA_TOKEN", "")
    if not tok and os.path.exists(HA_TOKEN_FILE):
        try:
            with open(HA_TOKEN_FILE) as f:
                tok = f.read().strip()
        except OSError:
            tok = ""
    return url, tok


def _ha_request(url, tok, path, data=None):
    """同步 urllib 调用(在 to_thread 里跑), 返回反序列化后的 JSON。"""
    req = urllib.request.Request(url + path, method="POST" if data else "GET")
    req.add_header("Authorization", "Bearer " + tok)
    body = None
    if data is not None:
        req.add_header("Content-Type", "application/json")
        body = json.dumps(data).encode()
    with urllib.request.urlopen(req, body, timeout=_TIMEOUT) as resp:
        payload = resp.read().decode()
    return json.loads(payload) if payload else {}


_DOMAIN_NOUN = {"light": "灯", "switch": "插座"}


async def ha_entities(url, tok, domain):
    """可用设备: 只选"主设备", 严防附属开关误伤(2026-10-19 实测教训:
    群发 switch.* 会把音箱静音/勿扰/空调网关全开一遍)。
    - switch.*: 米家平台主电源开关固定命名 *_on_p_* (psv3_on/b19_on);
      无匹配时返回空(宁可不动作, 不群发)。
    - light.*: 排除指示灯(*_indicator_light / friendly_name 含"指示灯")。
    - 一律排除 unavailable/unknown。"""
    states = await asyncio.to_thread(_ha_request, url, tok, "/api/states")
    out = []
    for s in states:
        eid = s.get("entity_id", "")
        if not eid.startswith(domain + "."):
            continue
        if s.get("state") in ("unavailable", "unknown"):
            continue
        if domain == "switch":
            if "_on_p_" in eid:
                out.append(s)
        else:
            if "indicator" in eid or "指示灯" in (s.get("attributes", {})
                                                 .get("friendly_name", "")):
                continue
            out.append(s)
    return out


async def handle_light_intent(conn, text, m) -> bool:
    op, domain = m
    from core.handle.sendAudioHandle import send_stt_message
    from core.handle.intentHandler import speak_txt
    import uuid
    from core.ha_audit import accepted

    noun = _DOMAIN_NOUN[domain]
    await send_stt_message(conn, text)
    url, tok = _ha_cfg()
    if not tok:
        conn.logger.bind(tag=TAG).error("HA 令牌未配置(XR_HA_TOKEN/ha_token.txt)")
        speak_txt(conn, "设备控制还没有配置好")
        return True

    conn.sentence_id = str(uuid.uuid4().hex)
    try:
        entities = await ha_entities(url, tok, domain)
        if not entities:
            speak_txt(conn, "家里还没有能控制的%s" % noun)
            return True
        on_now = [s for s in entities if s["state"] == "on"]
        want_on = op == "on"
        if (want_on and len(on_now) == len(entities)) or \
           (not want_on and not on_now):
            accepted(conn, text, op, domain, [s["entity_id"] for s in entities], "noop")
            speak_txt(conn, ("%s已经是开的" if want_on else "%s已经是关的") % noun)
            return True
        service = "turn_on" if want_on else "turn_off"
        for s in entities:
            await asyncio.to_thread(
                _ha_request, url, tok,
                f"/api/services/{domain}/{service}",
                {"entity_id": s["entity_id"]})
        conn.logger.bind(tag=TAG).info(
            f"HA {domain}/{service}: {[s['entity_id'] for s in entities]}")
        accepted(conn, text, op, domain, [s["entity_id"] for s in entities], "acted")
        speak_txt(conn, "好的，%s开了" % noun if want_on else "好的，%s关了" % noun)
    except (urllib.error.URLError, OSError, ValueError) as e:
        conn.logger.bind(tag=TAG).error(f"HA 调用失败: {e}")
        speak_txt(conn, "设备控制没连上，稍后再试")
    return True
