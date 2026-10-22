"""语音重启(2026-10-16): 音频直传轮的本地文本钩子够不到, 服务端判。

"重启/重启一下/重新启动"精确短语(剥标点等值, "帮我重启一下手机"类
长句放行) -> 发 {type:reboot} 消息, 客户端 sync+reboot, 自启链拉起。
不排 TTS(设备马上重启, 说了也听不全)。
"""
import json

TAG = __name__

_REBOOT_QUERIES = {"重启", "重启一下", "重新启动", "你重启一下",
                   "重启吧", "重启系统", "重启一下吧"}


def match_reboot_query(text) -> bool:
    if not text:
        return False
    t = text.strip().strip("，。！？；,.!?; \t")
    return t in _REBOOT_QUERIES


async def handle_reboot_intent(conn, text) -> bool:
    from core.handle.sendAudioHandle import send_stt_message

    await send_stt_message(conn, text)
    await conn.websocket.send(json.dumps({
        "type": "reboot", "session_id": conn.session_id}))
    conn.logger.bind(tag=TAG).info(f"语音重启指令已下发: {text}")
    return True
