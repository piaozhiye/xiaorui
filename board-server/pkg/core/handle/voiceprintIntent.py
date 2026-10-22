"""声纹意图(2026-10-16): 语音注册 + "我是谁"。

"记住我的声音，我叫小明" -> 用本轮音频(上一条语音)注册声纹 -> LLM 对话
自动个性化(上游把 current_speaker 注入 system prompt)。
"我是谁" -> 报最近识别到的说话人。
名字解析: 我叫X/我是X/名字叫X/叫做X, X=1-6 个汉字; 解析不出名字时
提示怎么说。宁漏不抢: 不含"记住我的声音"或"声纹"字样的一律放行。
"""
import re
import uuid

TAG = __name__

_NAME_RE = re.compile(r"(?:我叫|我是|名字叫|名字是|叫做|叫)([\u4e00-\u9fa5]{1,6})")

_WHO_QUERIES = {"我是谁", "你知道我是谁吗", "我是谁呀", "我是谁呀",
                "你知道我是谁", "猜猜我是谁", "认得我吗", "认识我吗"}


def match_voiceprint_enroll(text):
    """命中返回名字(可为空串=没解析到), 不命中返回 None。"""
    if not text or ("记住我的声音" not in text and "注册声纹" not in text
                    and "记住我的声音" not in text.replace(" ", "")):
        return None
    m = _NAME_RE.search(text)
    name = m.group(1) if m else ""
    # 名字里排除"的声音"等误捕("记住我的声音我是小明"的我是分支已优先生效)
    for junk in ("的声音", "声音"):
        name = name.replace(junk, "")
    return name


def match_who_am_i(text) -> bool:
    if not text:
        return False
    t = text.strip().strip("，。！？；,.!?; \t")
    return t in _WHO_QUERIES


async def handle_voiceprint_intent(conn, text) -> bool:
    """返回 True 表示已处理。provider 未启用时放行(不拦截)。"""
    from core.handle.sendAudioHandle import send_stt_message
    from core.handle.intentHandler import speak_txt

    provider = getattr(conn, "voiceprint_provider", None)
    if provider is None or not getattr(provider, "enabled", False):
        return False

    name = match_voiceprint_enroll(text)
    if name is not None:
        wav = getattr(conn, "_last_voice_wav", None)
        await send_stt_message(conn, text)
        conn.sentence_id = str(uuid.uuid4().hex)
        if not name:
            speak_txt(conn, "好的，请说记住我的声音，我叫你的名字")
        elif wav and provider.enroll(name, wav):
            speak_txt(conn, f"好的{name}，我已经记住你的声音了，以后就认识你啦")
        else:
            speak_txt(conn, "刚才的话有点短，请再试一次，说久一点")
        return True
    return False


async def handle_whoami_intent(conn, text) -> bool:
    from core.handle.sendAudioHandle import send_stt_message
    from core.handle.intentHandler import speak_txt

    speaker = (getattr(conn, "current_speaker", None) or "").strip()
    await send_stt_message(conn, text)
    conn.sentence_id = str(uuid.uuid4().hex)
    if speaker and speaker != "未知说话人":
        speak_txt(conn, f"你是{speaker}呀，我听出来的")
    else:
        speak_txt(conn, "我还没听出你是谁，可以说，记住我的声音，我叫你的名字")
    return True
