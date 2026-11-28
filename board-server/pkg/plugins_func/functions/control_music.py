"""Voice controls for Music Assistant and its Snapcast player."""
from core.utils.snapcast_music import control_async, enabled
from core.handle.musicControlIntent import MUSIC_ACTIONS_SILENT_ON_SUCCESS
from plugins_func.register import register_function, ToolType, ActionResponse, Action


control_music_function_desc = {
    "type": "function",
    "function": {
        "name": "control_music",
        "description": "控制小瑞音乐播放器。启用 Music Assistant 时，播放指定歌曲会在 MA 已配置的音乐源（如 QQ 音乐）中搜索，并通过 Snapcast 播放；也可暂停、继续、停止、切换歌曲、查询状态和调整音量。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": [
                    "play", "pause", "resume", "stop", "next", "previous",
                    "volume_set", "volume_up", "volume_down", "status"],
                    "description": "要执行的播放器操作"},
                "query": {"type": "string", "description": "播放时的曲名或已配置电台名；未指定曲目时留空"},
                "volume": {"type": "integer", "minimum": 0, "maximum": 100,
                           "description": "volume_set 时的目标百分比"},
            },
            "required": ["action"],
        },
    },
}


@register_function("control_music", control_music_function_desc, ToolType.SYSTEM_CTL)
async def control_music(conn, action: str, query: str = None, volume: int = None):
    if not enabled(conn):
        return ActionResponse(Action.RESPONSE, result="多设备音乐语音控制未启用",
                              response="多设备音乐控制还没有配置好")
    result = await control_async(conn, action, query=query, volume=volume)
    if result.get("ok"):
        if action in MUSIC_ACTIONS_SILENT_ON_SUCCESS:
            return ActionResponse(Action.RECORD,
                                  result=result.get("response", "音乐控制已执行"))
        return ActionResponse(Action.RESPONSE, result=result.get("response", "指令已完成"),
                              response=result.get("response", "好的"))
    return ActionResponse(Action.RESPONSE, result=result.get("error", "音乐控制失败"),
                          response=result.get("error", "音乐控制失败"))
