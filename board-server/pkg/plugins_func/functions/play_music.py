import os
import re
import json
import time
import random
import difflib
import asyncio
import traceback
from pathlib import Path
from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType, ContentType
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__

MUSIC_CACHE = {}

play_music_function_desc = {
    "type": "function",
    "function": {
        "name": "play_music",
        "description": "当用户要求播放音乐、歌曲时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": "歌曲名称，如果用户没有指定具体歌名则为'random', 明确指定的时返回音乐的名字 示例: ```用户:播放两只老虎\n参数：两只老虎``` ```用户:播放音乐 \n参数：random ```",
                }
            },
            "required": ["song_name"],
        },
    },
}


@register_function("play_music", play_music_function_desc, ToolType.SYSTEM_CTL)
async def play_music(conn: "ConnectionHandler", song_name: str):
    try:
        music_intent = (
            f"播放音乐 {song_name}" if song_name != "random" else "随机播放音乐"
        )
        if not await handle_music_command(conn, music_intent):
            return ActionResponse(action=Action.RESPONSE, result="未找到可播放音乐",
                                  response="暂时没有找到可播放的音乐，请换首歌试试。")
        return ActionResponse(
            action=Action.RECORD, result="指令已接收", response="正在为您播放音乐"
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"处理音乐意图错误: {e}")
        return ActionResponse(
            action=Action.RESPONSE, result=str(e), response="播放音乐时出错了"
        )


def _extract_song_name(text):
    """从用户输入中提取歌名"""
    for keyword in ["播放音乐"]:
        if keyword in text:
            parts = text.split(keyword)
            if len(parts) > 1:
                return parts[1].strip()
    return None


def _find_best_match(potential_song, music_files):
    """查找最匹配的歌曲"""
    best_match = None
    highest_ratio = 0

    for music_file in music_files:
        song_name = os.path.splitext(music_file)[0]
        ratio = difflib.SequenceMatcher(None, potential_song, song_name).ratio()
        if ratio > highest_ratio and ratio > 0.4:
            highest_ratio = ratio
            best_match = music_file
    return best_match


def get_music_files(music_dir, music_ext):
    music_dir = Path(music_dir)
    music_files = []
    music_file_names = []
    for file in music_dir.rglob("*"):
        # 判断是否是文件
        if file.is_file():
            # 获取文件扩展名
            ext = file.suffix.lower()
            # 判断扩展名是否在列表中
            if ext in music_ext:
                # 添加相对路径
                music_files.append(str(file.relative_to(music_dir)))
                music_file_names.append(
                    os.path.splitext(str(file.relative_to(music_dir)))[0]
                )
    return music_files, music_file_names


def initialize_music_handler(conn: "ConnectionHandler"):
    global MUSIC_CACHE
    if MUSIC_CACHE == {}:
        plugins_config = conn.config.get("plugins", {})
        MUSIC_CACHE["music_config"] = {}
        if "play_music" in plugins_config:
            MUSIC_CACHE["music_config"] = plugins_config["play_music"]
            MUSIC_CACHE["music_dir"] = os.path.abspath(
                MUSIC_CACHE["music_config"].get("music_dir", "./music")  # 默认路径修改
            )
            MUSIC_CACHE["music_ext"] = MUSIC_CACHE["music_config"].get(
                "music_ext", (".mp3", ".wav", ".p3")
            )
            MUSIC_CACHE["refresh_time"] = MUSIC_CACHE["music_config"].get(
                "refresh_time", 60
            )
        else:
            MUSIC_CACHE["music_dir"] = os.path.abspath("./music")
            MUSIC_CACHE["music_ext"] = (".mp3", ".wav", ".p3")
            MUSIC_CACHE["refresh_time"] = 60
        # 在线直链歌单: music_dir 同级的 music-online/playlist.json
        # [{"name": "歌名", "url": "https://...mp3", "trim_s": 60(可选,演示曲裁剪)}]
        MUSIC_CACHE["playlist_path"] = os.path.join(
            os.path.dirname(MUSIC_CACHE["music_dir"]), "music-online", "playlist.json"
        )
        # 获取音乐文件列表
        MUSIC_CACHE["music_files"], MUSIC_CACHE["music_file_names"] = get_music_files(
            MUSIC_CACHE["music_dir"], MUSIC_CACHE["music_ext"]
        )
        MUSIC_CACHE["scan_time"] = time.time()
    return MUSIC_CACHE


def _load_online_playlist(conn: "ConnectionHandler"):
    """在线直链歌单(用户可编辑, 无第三方搜索 API 依赖)。"""
    path = MUSIC_CACHE.get("playlist_path", "")
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("entries", [])
        return [e for e in data
                if isinstance(e, dict) and e.get("name") and e.get("url")]
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"在线歌单读取失败: {e}")
        return []


async def _resolve_mcp(conn, song_name):
    """Use the configured MCP resolver; never speak a ResourceLink as music."""
    handler = getattr(conn, "func_handler", None)
    executor = getattr(handler, "server_mcp_executor", None)
    manager = getattr(executor, "mcp_manager", None)
    if not manager:
        return None
    tool = MUSIC_CACHE["music_config"].get("mcp_tool", "get_play_url")
    internal = any(client.has_tool(tool) for client in getattr(manager, "clients", {}).values())
    if not manager.is_mcp_tool(tool) and not internal:
        return None
    query = "轻音乐" if song_name == "random" else song_name
    try:
        result = await asyncio.wait_for(manager.execute_tool(tool, {"query": query}), timeout=6)
        data = result if isinstance(result, dict) else result.model_dump()
        if data.get("isError"):
            return None
        structured = data.get("structuredContent") or {}
        if not structured.get("play_url"):
            for item in data.get("content", []):
                if item.get("type") == "text":
                    try:
                        candidate = json.loads(item.get("text", ""))
                        if isinstance(candidate, dict) and candidate.get("play_url"):
                            structured = candidate
                            break
                    except (ValueError, TypeError):
                        continue
        url = structured.get("play_url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            return None
        song = structured.get("song") or {}
        return {"name": song.get("title") or structured.get("title") or query, "url": url}
    except Exception as error:
        conn.logger.bind(tag=TAG).warning(f"MCP音乐搜索失败: {type(error).__name__}")
        return None


# 最近播放(2026-10-16): "下一首/换台"需要知道刚才放的是什么
_LAST_MEDIA = {"kind": None, "radio_name": None}


def _queue_online_stream(conn, entry):
    """Queue a decoder request; the existing TTS worker owns its lifetime."""
    text = f"正在播放，{entry['name']}"
    _LAST_MEDIA["kind"] = "radio" if entry.get("radio") else "music"
    if entry.get("radio"):
        _LAST_MEDIA["radio_name"] = str(entry["name"])
    conn.tts.store_tts_text(conn.sentence_id, text)
    if conn.intent_type == "intent_llm":
        conn.tts.tts_text_queue.put(TTSMessageDTO(
            conn.sentence_id, SentenceType.FIRST, ContentType.ACTION))
    conn.tts.tts_text_queue.put(TTSMessageDTO(
        conn.sentence_id, SentenceType.MIDDLE, ContentType.STREAM,
        content_detail=text, content_file=entry["url"],
        is_radio=bool(entry.get("radio"))))
    if conn.intent_type == "intent_llm":
        conn.tts.tts_text_queue.put(TTSMessageDTO(
            conn.sentence_id, SentenceType.LAST, ContentType.ACTION))


async def handle_music_command(conn: "ConnectionHandler", text):
    """Online selection first; local files remain an explicit offline fallback."""
    initialize_music_handler(conn)
    online = await asyncio.to_thread(_load_online_playlist, conn)
    potential_song = _extract_song_name(text) or "random"
    selected = None
    if online:
        songs = [e for e in online if not e.get("radio")]
        radios = [e for e in online if e.get("radio")]
        wants_radio = "电台" in text or "广播" in text
        if wants_radio and radios:
            # 电台意图优先于歌曲归一: "播放轻音乐电台"不含"播放音乐"关键词,
            # 走 potential_song=random 会掉进 MCP 搜歌(实测返回日文歌, 11s 断)。
            # 原文剥壳后与电台名模糊匹配, 不中取第一个电台。
            # (原写 `stripped = clean_text` —— clean_text 全仓无定义, 走到这里
            #  必然 NameError, 网关只回"播放音乐时出错了"。线上实测踩过。)
            stripped = text
            for shell in ("播放", "放", "听", "打开", "电台", "广播", "频道", "一个",
                          "个", "请", "帮我", "，", "。", "、", "?", "？", ",", "."):
                stripped = stripped.replace(shell, "")
            best, ratio = None, 0.0
            for e in radios:
                r = difflib.SequenceMatcher(None, stripped, str(e["name"])).ratio()
                if r > ratio:
                    best, ratio = e, r
            selected = best if ratio > 0.3 else radios[0]
        if selected is None:
            # 电台分支已经选中的台不能被这里覆盖: "畅音电台"不含"播放音乐"前缀 ->
            # potential_song="random" -> 走 else 的歌名匹配 -> 匹配不上 -> None,
            # 电台选择被吃掉(线上实测: 电台请求既不报错也不出声)。
            if potential_song == "random":
                selected = random.choice(songs) if songs else None
            else:
                names = {str(entry["name"]): entry for entry in online}
                match = difflib.get_close_matches(potential_song, names, n=1, cutoff=0.6)
                selected = names[match[0]] if match else None
                if selected and selected.get("radio") and not wants_radio:
                    selected = None   # 得点名电台/广播才进无限流
        if wants_radio and potential_song != "random" and not (selected and selected.get("radio")):
            for e in radios:      # "播放电台"模糊命中任一电台
                if difflib.SequenceMatcher(None, potential_song, str(e["name"])).ratio() > 0.4:
                    selected = e
                    break
            else:
                selected = radios[0] if radios else None
    if not selected:
        selected = await _resolve_mcp(conn, potential_song)
    if selected:
        _queue_online_stream(conn, selected)
        return True
    # Network search absent/unavailable: preserve local p3/WAV support.
    files, names = await asyncio.to_thread(
        get_music_files, MUSIC_CACHE["music_dir"], MUSIC_CACHE["music_ext"])
    MUSIC_CACHE.update(music_files=files, music_file_names=names, scan_time=time.time())
    if potential_song == "random":
        return bool(await play_local_music(conn))
    match = _find_best_match(potential_song, files)
    return bool(await play_local_music(conn, specific_file=match)) if match else False


def _get_random_play_prompt(song_name):
    """生成随机播放引导语"""
    # 移除文件扩展名
    clean_name = os.path.splitext(song_name)[0]
    prompts = [
        f"正在为您播放，《{clean_name}》",
        f"请欣赏歌曲，《{clean_name}》",
        f"即将为您播放，《{clean_name}》",
        f"现在为您带来，《{clean_name}》",
        f"让我们一起聆听，《{clean_name}》",
        f"接下来请欣赏，《{clean_name}》",
        f"此刻为您献上，《{clean_name}》",
    ]
    # 直接使用random.choice，不设置seed
    return random.choice(prompts)


async def play_local_music(conn: "ConnectionHandler", specific_file=None):
    global MUSIC_CACHE
    """播放本地音乐文件"""
    try:
        if not os.path.exists(MUSIC_CACHE["music_dir"]):
            conn.logger.bind(tag=TAG).error(
                f"音乐目录不存在: " + MUSIC_CACHE["music_dir"]
            )
            return False

        # 确保路径正确性
        if specific_file:
            selected_music = specific_file
            music_path = os.path.join(MUSIC_CACHE["music_dir"], specific_file)
        else:
            if not MUSIC_CACHE["music_files"]:
                conn.logger.bind(tag=TAG).error("未找到MP3音乐文件")
                return False
            selected_music = random.choice(MUSIC_CACHE["music_files"])
            music_path = os.path.join(MUSIC_CACHE["music_dir"], selected_music)

        if not os.path.exists(music_path):
            conn.logger.bind(tag=TAG).error(f"选定的音乐文件不存在: {music_path}")
            return
        # 本地库也是"歌": "下一首"的媒体追踪不能只认在线流(否则离线歌后
        # kind 陈旧, 会去换台)
        _LAST_MEDIA["kind"] = "music"
        _LAST_MEDIA["radio_name"] = None
        text = _get_random_play_prompt(selected_music)
        conn.tts.store_tts_text(conn.sentence_id, text)
        # conn.dialogue.put(Message(role="assistant", content=text))

        if conn.intent_type == "intent_llm":
            conn.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=conn.sentence_id,
                    sentence_type=SentenceType.FIRST,
                    content_type=ContentType.ACTION,
                )
            )
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.MIDDLE,
                content_type=ContentType.TEXT,
                content_detail=text,
            )
        )
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.MIDDLE,
                content_type=ContentType.FILE,
                content_file=music_path,
            )
        )
        if conn.intent_type == "intent_llm":
            conn.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=conn.sentence_id,
                    sentence_type=SentenceType.LAST,
                    content_type=ContentType.ACTION,
                )
            )

        return True

    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"播放音乐失败: {str(e)}")
        conn.logger.bind(tag=TAG).error(f"详细错误: {traceback.format_exc()}")
        return False

async def try_radio_intent(conn: "ConnectionHandler", text) -> bool:
    """电台/广播意图, 挂 handle_user_intent 前段(原文判定)。意图 LLM 会把
    '轻音乐电台'吞成 song_name=random, 改写文本丢失'电台'字样(实测),
    必须在改写前的原文上判。无电台条目/不匹配返回 False 走原链路。"""
    initialize_music_handler(conn)
    radios = [e for e in _load_online_playlist(conn) if e.get("radio")]
    if not radios:
        return False
    from core.handle.radioIntent import match_radio
    selected = match_radio(radios, text)
    if selected is None:
        return False
    from core.handle.sendAudioHandle import send_stt_message
    import uuid
    await send_stt_message(conn, text)
    conn.sentence_id = str(uuid.uuid4().hex)
    _queue_online_stream(conn, selected)
    conn.logger.bind(tag=TAG).info(f"电台: {selected['name']}")
    return True
