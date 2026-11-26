"""Narrow local rules for explicit Snapcast music controls."""
def is_unqualified_music_control_candidate(text):
    if not text:
        return False
    value = text.strip().strip("，。！？；,.!?; \t")
    if value in {"暂停", "暂停播放", "停一下", "暂停一下", "继续", "继续播放",
                 "恢复", "恢复播放", "接着播放", "停止", "停止播放", "停下",
                 "别放了", "别播了", "停播", "上一首", "上一曲", "前一首",
                 "前一曲", "下一首", "下一曲", "换一首", "切歌"}:
        return True
    from core.handle.volumeIntent import match_volume_intent
    return match_volume_intent(value) is not None


def match_music_control(text, *, music_active=False):
    if not text:
        return None
    value = text.strip().strip("，。！？；,.!?; \t")
    has_music_context = any(word in value for word in ("音乐", "歌曲", "歌"))
    if not has_music_context:
        if not music_active:
            return None
        # During an active Snapcast session, concise transport/volume commands
        # refer to that session. Outside playback they continue through the
        # ordinary intent chain unchanged.
        if value in {"暂停", "暂停播放", "停一下", "暂停一下"}:
            return {"action": "pause"}
        if value in {"继续", "继续播放", "恢复", "恢复播放", "接着播放"}:
            return {"action": "resume"}
        if value in {"停止", "停止播放", "停下", "别放了", "别播了", "停播"}:
            return {"action": "stop"}
        if value in {"上一首", "上一曲", "前一首", "前一曲"}:
            return {"action": "previous"}
        if value in {"下一首", "下一曲", "换一首", "切歌"}:
            return {"action": "next"}
        from core.handle.volumeIntent import match_volume_intent
        volume = match_volume_intent(value)
        if volume:
            if volume["op"] == "set":
                return {"action": "volume_set", "volume": volume["value"]}
            if volume["op"] == "up":
                return {"action": "volume_up"}
            if volume["op"] == "down":
                return {"action": "volume_down"}
            if volume["op"] == "max":
                return {"action": "volume_set", "volume": 100}
            if volume["op"] == "min":
                return {"action": "volume_set", "volume": 0}
        return None
    if any(word in value for word in ("上一首", "前一首", "上一曲", "前一曲")):
        return {"action": "previous"}
    if any(word in value for word in ("下一首", "下一曲", "换一首", "切歌")):
        return {"action": "next"}
    if any(word in value for word in ("暂停", "停一下", "暂停播放")):
        return {"action": "pause"}
    if any(word in value for word in ("继续", "恢复", "接着播放", "继续播放")):
        return {"action": "resume"}
    if any(word in value for word in ("停止", "别放", "不要放", "关掉", "关闭")):
        return {"action": "stop"}
    if any(word in value for word in ("现在放什么", "正在放什么", "播放状态", "什么歌")):
        return {"action": "status"}
    if "音量" in value or "声音" in value:
        from core.handle.volumeIntent import match_volume_intent
        volume_text = value.replace("音乐", "").replace("歌曲", "").replace("歌", "")
        intent = match_volume_intent(volume_text)
        if intent:
            if intent["op"] == "set":
                return {"action": "volume_set", "volume": intent["value"]}
            if intent["op"] == "up":
                return {"action": "volume_up"}
            if intent["op"] == "down":
                return {"action": "volume_down"}
        if any(word in value for word in ("调到", "调为", "设为", "设置", "调高", "调低")):
            return {"action": "invalid_volume"}
    elif any(word in value for word in ("大声", "小声", "调大", "调小", "调高", "调低", "太吵")):
        from core.handle.volumeIntent import match_volume_intent
        volume_text = "音量" + value.replace("音乐", "").replace("歌曲", "").replace("歌", "")
        intent = match_volume_intent(volume_text)
        if intent and intent["op"] in ("up", "down"):
            return {"action": "volume_up" if intent["op"] == "up" else "volume_down"}
    return None
