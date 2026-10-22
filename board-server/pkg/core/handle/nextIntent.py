""""下一首/换台"(2026-10-16): 按最近播放的媒体类型切换。

电台 -> 轮转到下一个电台; 歌曲 -> 随机再来一首(走完整选歌链)。
不知道刚才放了什么(开机后没播过) -> 放行给 LLM。必须排在电台意图
之前: 裸"换个电台"会被电台匹配器吃掉且总选第一个台。
"""
import uuid

TAG = __name__

_NEXT_QUERIES = {"下一首", "下一曲", "换一首", "切歌", "切一首",
                 "再来一首", "再来一首别的", "再来一个",
                 "换个电台", "换台", "换一个电台", "下一个电台", "换个频道",
                 "换个台", "下一台"}


def match_next_query(text) -> bool:
    if not text:
        return False
    t = text.strip().strip("，。！？；,.!?; \t")
    return t in _NEXT_QUERIES


async def handle_next_intent(conn, text) -> bool:
    from plugins_func.functions.play_music import (_LAST_MEDIA,
                                                    _load_online_playlist,
                                                    _queue_online_stream,
                                                    initialize_music_handler,
                                                    handle_music_command,
                                                    MUSIC_CACHE, TAG as _MT)
    from core.handle.sendAudioHandle import send_stt_message

    kind = _LAST_MEDIA.get("kind")
    if kind is None:
        return False   # 没播过东西, 放行 LLM

    await send_stt_message(conn, text)
    conn.sentence_id = str(uuid.uuid4().hex)
    if kind == "radio":
        initialize_music_handler(conn)
        radios = [e for e in _load_online_playlist(conn) if e.get("radio")]
        if not radios:
            conn.logger.bind(tag=TAG).warning("换台但歌单无电台")
            return False
        names = [str(e["name"]) for e in radios]
        cur = _LAST_MEDIA.get("radio_name")
        idx = (names.index(cur) + 1) % len(names) if cur in names else 0
        conn.logger.bind(tag=_MT).info(f"换台: {cur} -> {names[idx]}")
        _queue_online_stream(conn, radios[idx])
    else:
        conn.logger.bind(tag=_MT).info("下一首: 随机再选")
        await handle_music_command(conn, "随机播放音乐")
    return True
