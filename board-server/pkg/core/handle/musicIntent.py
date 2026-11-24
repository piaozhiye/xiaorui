"""放歌意图纯匹配(2026-11-23): 规则门, 不靠 LLM 发起 function call。

为什么不用 LLM: 板上跑的是 Qwen2.5-0.5B, function call 命中率不稳 ——
实测"播放音乐"被它当成闲聊, 答"对不起, 我无法直接播放音乐", 而工具明明
就在 tools 列表里(Intent.function_call.functions 里有 play_music)。
电台那条(_h_radio)本来就是纯规则接住的, 放歌照同样的路子来: 原文命中
就进 play_music, 没命中才放行给 LLM。

本模块无重依赖, 便于主机单测(同 radioIntent 的做法)。
"""
import re

# 疑问/科普/设备类: 含这些就不是"放歌", 是聊天, 放行给 LLM
CHAT_MARKERS = (
    "怎么", "如何", "为什么", "什么", "是谁", "吗", "呢", "哪", "谁", "多少",
    "历史", "原理", "发明", "介绍", "讲解", "科普", "知识", "资料", "搜索",
    "搜一下", "推荐", "播放器", "应用", "软件", "下载", "歌词", "APP",
)
# 播放期控制句: "下一首"/"停一下"/"太吵了"是控制, 不是新的放歌请求
CONTROL_MARKERS = (
    "下一首", "换一首", "换台", "停止", "暂停", "关闭", "关掉", "别再",
    "别放", "不要放", "停下", "退出", "太吵", "很吵", "难听", "声音小",
    "小声", "大声", "音量",
)
# 动词: 有动词才像"要放歌"的请求
VERBS = ("播放", "放", "来", "听", "唱", "点", "整", "搞")
# 明确动词: 这两个基本只用于放歌, 没有音乐名词也认(播放演示曲)
EXPLICIT_VERBS = ("播放", "放")
# 软动词: 日常用语里太常见("来杯水"), 没有音乐名词时不认
SOFT_VERBS = ("来", "听", "唱", "点", "整", "搞")
# 音乐名词: 长的排前面, 剥壳时先剥长词
MUSIC_NOUNS = (
    "一首歌", "音乐儿", "纯音乐", "歌曲", "首歌", "听歌", "点歌",
    "曲子", "旋律", "歌单", "音乐", "一首", "歌",
)
# 礼貌词/语气词/量词: 剥壳时清掉
FILLER = re.compile(r"^(我想要|我要|我想|给我|请|帮我|替我|麻烦|来)+")
NOISE = re.compile(r"[的地得吧呀啊呗哦嗯了个一下首只儿]+")
# 名字最长这么长, 再长当聊天("播放一段非常长的关于音乐的历史")
MAX_SONG_CHARS = 8


def _genre_words():
    """风格词表跟 play_music 的保持一份(那边是权威)。"""
    try:
        from plugins_func.functions.play_music import _GENRE_WORDS
        return _GENRE_WORDS
    except Exception:
        # 主机单测环境里 plugins_func 依赖重, 退化到一份同样的词表。
        return {
            "轻音乐", "纯音乐", "流行", "流行音乐", "古典", "古典音乐", "摇滚",
            "摇滚音乐", "爵士", "爵士乐", "民谣", "电子", "电子音乐", "儿歌",
            "助眠", "白噪音", "白噪声", "相声", "新闻", "故事", "小说",
            "脱口秀", "红歌", "老歌", "劲爆", "伤感",
        }


def extract_song(text):
    """从请求句里剥出歌名; 剥不出来返回空串(调用方按随机处理)。"""
    value = (text or "").strip()
    # "放首歌叫晴天": 名字在"叫"后面
    if "叫" in value:
        value = value.split("叫", 1)[1]
    for verb in VERBS:
        value = value.replace(verb, "")
    value = FILLER.sub("", value).strip()
    genres = _genre_words()
    if value in genres:
        return value
    for noun in MUSIC_NOUNS:
        value = value.replace(noun, "")
    value = NOISE.sub("", value).strip()
    if value in genres:
        return value
    return value


def match_music(text):
    """命中返回歌名(空串=随机), None=不拦截(交给原链路)。"""
    if not text:
        return None
    value = text.strip()
    if any(marker in value for marker in CONTROL_MARKERS):
        return None
    if any(marker in value for marker in CHAT_MARKERS):
        return None
    has_explicit = any(verb in value for verb in EXPLICIT_VERBS)
    has_soft = any(verb in value for verb in SOFT_VERBS)
    has_noun = any(noun in value for noun in MUSIC_NOUNS)
    if not (has_explicit or has_soft):
        return None
    song = extract_song(value)
    genres = _genre_words()
    # 风格词("轻音乐"/"爵士")软动词也算: "来点爵士"
    if song in genres:
        return song
    if not has_noun and not has_explicit:
        # 没有音乐名词又只是软动词 -> 可能是"来杯水"这类日常话
        return None
    if len(song) > MAX_SONG_CHARS:
        # 剥出来这么长不像歌名, 多半是聊天句
        return None
    # "歌"/"音乐"这类空壳按随机处理
    if song in ("", "歌", "音乐", "曲"):
        song = ""
    return song
