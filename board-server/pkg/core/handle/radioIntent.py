"""电台意图纯匹配(2026-10-15): 动词门+剥壳+模糊, 无重依赖便于主机单测。
播放动作与歌单装载在 plugins_func.functions.play_music(try_radio_intent)。"""
import difflib

# 疑问/科普句: 含这些词就不是"点台", 是聊天, 放行给 LLM
CHAT_MARKERS = ("怎么", "如何", "为什么", "什么", "是谁", "吗", "呢", "哪", "谁",
                "多少", "历史", "原理", "发明", "介绍", "讲解", "科普", "知识",
                "资料", "搜索", "搜一下")
# 动词: 有动词说明是明确的动作请求
VERBS = ("播放", "放", "听", "打开", "换", "切", "来")
# 无动词时, 剥壳后这么长以内才当"点台"(短句才像在报台名)
BARE_REQUEST_MAX_CHARS = 6


def match_radio(radios, text):
    """命中返回电台条目; None=不拦截(交给原链路)。
    动词门挡聊天("广播是怎么发明的"); 剥壳后与电台名模糊匹配;
    说不出名字的泛指("放个电台")取第一个; 点名但匹配不上交给 LLM。
    无动词的短点台句("畅音电台")也接住: 放走了就落到意图 LLM, 而 LLM 会把
    "电台"字样吞成 song_name=random(线上实测), 电台链路当场变死路。"""
    if "电台" not in text and "广播" not in text:
        return None
    has_verb = any(v in text for v in VERBS)
    if not has_verb and any(marker in text for marker in CHAT_MARKERS):
        return None
    stripped = text
    for shell in ("播放", "放", "听", "打开", "换", "切", "来", "电台", "广播", "频道",
                  "一个", "个", "请", "帮我", "，", "。"):
        stripped = stripped.replace(shell, "")
    best, ratio = None, 0.0
    for e in radios:
        r = difflib.SequenceMatcher(None, stripped, str(e["name"])).ratio()
        if r > ratio:
            best, ratio = e, r
    if best is not None and ratio > 0.3:
        return best
    if not stripped.strip():
        return radios[0]
    if not has_verb and len(stripped) <= BARE_REQUEST_MAX_CHARS:
        return radios[0]     # 无动词的短点台句: 报不出名字就取第一个台
    return None
