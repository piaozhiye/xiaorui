""""再说一遍"(2026-10-16): 重播上一轮回答, 本地零 LLM。

sendAudioHandle 按句累积本轮播报全文(conn._last_spoken_text); 命中
封闭短语直接重新 TTS。没有可重播内容时如实说。匹配用精确集合 --
"把故事再讲一遍"这类指代前文的长句放行给 LLM(有对话上下文, 能答)。
"""
import uuid

TAG = __name__

_REPEAT_QUERIES = {
    "再说一遍", "再讲一遍", "再说一次", "再讲一次", "重复一遍",
    "重复一次", "你再说一遍", "再说一遍好吗", "再念一遍",
}


def match_repeat_query(text) -> bool:
    if not text:
        return False
    t = text.strip().strip("，。！？；,.!?; \t")
    return t in _REPEAT_QUERIES


async def handle_repeat_intent(conn, text) -> bool:
    from core.handle.sendAudioHandle import send_stt_message
    from core.handle.intentHandler import speak_txt

    await send_stt_message(conn, text)
    conn.sentence_id = str(uuid.uuid4().hex)
    last = (getattr(conn, "_last_spoken_text", "") or "").strip()
    if last:
        speak_txt(conn, last)
    else:
        speak_txt(conn, "刚刚没有说过什么呢，你可以先问我一个问题")
    return True
