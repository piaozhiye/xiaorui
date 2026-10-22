import json
import os
import uuid
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.dialogue import Message
from core.providers.tts.dto.dto import ContentType
from core.handle.helloHandle import checkWakeupWords
from plugins_func.register import Action, ActionResponse
from core.handle.sendAudioHandle import send_stt_message
from core.handle.reportHandle import enqueue_tool_report
from core.utils.util import remove_punctuation_and_length
from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType

TAG = __name__


async def _notify_silent_drop(conn: "ConnectionHandler", reason: str):
    """静默丢弃回执: 告诉前端"这一轮不是命令", 让它立刻回到聆听。

    服务端的噪声门和"恰好只有唤醒词"两条路径都是静默 return True —— 不发
    任何消息。前端于是在 RESPOND 里等满 response_timeout(默认 20s) 才收轮,
    这段时间麦克风是聋的: 紧随其后的指令段整段丢失(声学套件实测 A2/A3
    第一次尝试 = missing-command), 最后还会播一声弃轮提示音, 用户观感是
    "喊了没反应 / 总是断开"。

    reason=wake: 纯唤醒, 前端据此开跟说窗(服务端已经认为用户在跟说);
    reason=noise: 噪声/填充词, 前端只收轮不开口。
    回执发不出去不能影响这一轮的处理(客户端可能已断线)。
    """
    try:
        await conn.websocket.send(
            json.dumps({"type": "silent", "reason": reason}, ensure_ascii=False)
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"静默回执发送失败: {e}")


# ---------- 本地意图注册表(2026-10-16): 顺序即语义, 越靠前优先级越高 ----------
# 契约: (名称, 文本模式, match, handle)
#   文本模式 "filtered"=去标点文本(默认), "raw"=ASR 原文(时刻类必须用原文:
#   remove_punctuation_and_length 会剥掉冒号和点, "8:28a.m."->"828am" 失配)
#   match(t) 返回 None=不命中继续下一项; 其余值=命中并作为第三参传给 handle
#   handle(conn, text, m) 返回 True=已处理(不再走 LLM), False=继续下一项
# 顺序约束(动顺序前先读):
#   reboot/volume 在最前(最高频零误伤面); ha-light 紧随(封闭短语零误伤)
#   声纹注册在"我是谁"前(注册句含"我是X"会被 whoami 抢走)
#   切歌在电台前(裸"换个电台"会被电台匹配器吃掉且总选第一个台)
#   定时列表在时长制定时前; 时长制在时刻制前(互斥但保险)


def _b(fn):
    """bool 匹配器适配: False->None(统一 None=不命中)。"""
    def wrap(t):
        return t if fn(t) else None
    return wrap


# 单字语气词/ASR 噪声幻觉(2026-10-19): "嗯。"这类杂波进 LLM 会闲聊出
# "有心事要和小瑞说说吗？", 安静环境里很瘆人 -> 静默吃掉。
_NOISE_TOKENS = frozenset("嗯 啊 哦 噢 呃 哎 欸 嘛 呀 哼 唉".split())
# 多字礼貌/填充词 + 英文误听(2026-10-20 实测补): 云 ASR 把"小瑞小瑞"听成
# "Thank you."/"Hmm."/"Okay.", 板子回一句"嗯？有什么事要我帮忙吗", 看着像
# "噪声被当人声", 实际是垃圾文本漏过了单字门直达 LLM -> 礼貌废话循环。
# 本地 RKNN 的误听("好好""嗯好的")同理。
_NOISE_PHRASES = frozenset((
    "好", "好的", "好吧", "好好", "好呀", "好不好", "嗯好的", "好的好的",
    "嗯嗯", "嗯哼", "行", "行吧", "可以", "可以的", "没事", "没事了",
    "不用了", "不用谢", "哈哈", "呵呵", "嘻嘻", "嘿嘿",
    "thankyou", "thanks", "thank", "hmm", "hm", "okay", "ok",
))
_PUNCTUATION = " \t\r\n，。！？,.!?;；:：'\"“”‘’~～…·-—_、"

# 纯填充字组成的整句(2026-10-20 实测): 云 ASR 空回执后 hybrid 转本地续答,
# "嗯好好好的" 直达 LLM, 板子答"好的，请问有什么可…" —— 看着就是"噪声被当人声"。
# 规则: 整句每个字都在填充集里才算噪声; 只要出现一个实词就放行。
# 不收 没/不/用/事/在 这类可独立成答的字(它们已由 _NOISE_PHRASES 精确覆盖)。
_FILLER_CHARS = frozenset("嗯啊哦噢呃哎欸嘛呀哼唉好了的吧哈呵嘿嘻行可以嘞")


# 短转写门(2026-10-20, HA 的 no_intent_match 等价物)。
# 实测: 跟说窗内说"开灯", 云 ASR 转成"海海参", 意图链没命中, 文本直接喂 LLM,
# 模型只能顺着字面编"海参？是想吃海参吗？"。HA 的 pipeline 遇到 STT 空文本
# (stt-no-text-recognized)或意图不匹配(no_intent_match)都是**终止事件**,
# 回一句"没听清"; 只有配了 LLM agent 才自由闲聊。短文本里没有足够信息,
# 任何模型都只能编 —— 先挡在 LLM 之前。阈值可用 XR_MIN_CHAT_CHARS 调。
_MIN_CHAT_CHARS_DEFAULT = 4


def min_chat_chars() -> int:
    raw = os.environ.get("XR_MIN_CHAT_CHARS")
    if raw is None:
        return _MIN_CHAT_CHARS_DEFAULT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _MIN_CHAT_CHARS_DEFAULT
    return value if value > 0 else _MIN_CHAT_CHARS_DEFAULT


def is_too_short_to_chat(text, min_chars=None) -> bool:
    """去标点后短到不成句 -> 不该问 LLM。

    只判长度, 不判内容: 短句里既可能是"开灯"这样的真指令, 也可能是"海海参"
    这样的听岔。真指令由本地意图族先接管(本门在意图链之后), 所以到这里还
    剩下的一律按"没听清"处理。
    """
    if not text:
        return False
    if min_chars is None:
        min_chars = min_chat_chars()
    return len(text) < min_chars


def is_noise_token(text) -> bool:
    """去标点后是语气词/礼貌填充词 -> 噪声, 静默吃掉。

    单字语气词是原始集合; 多字礼貌词与英文误听是 2026-10-20 补的。
    整句匹配(不是子串): "好的, 帮我开灯" 含命令, 不能被当成噪声吃掉。
    """
    if not text:
        return False
    normalized = "".join(ch for ch in text if ch not in _PUNCTUATION).lower()
    if len(normalized) == 1:
        return normalized in _NOISE_TOKENS
    if normalized in _NOISE_PHRASES:
        return True
    # 纯填充字拼接("嗯好好好的"/"好嘞好嘞"): 长度设上限, 免得整段口语被吃
    if 0 < len(normalized) <= 8 and all(ch in _FILLER_CHARS for ch in normalized):
        return True
    return False


# 本地 RKNN 与云 ASR 对"小瑞"的实测渲染(见 tests/test_wake_shape.py 与
# docs/BOARD-RESTORE.md): 云端常给"小度小度"(按常见助手名做语义偏好),
# 本地给"小想"/"小看看看"。枚举词表追不上, 双单元形状是稳定的。
# 2026-10-21 补 弟/肉/猪/蜜: 快速说"小瑞小瑞"时云 ASR 把"瑞"吞成近音
# (实测 小小弟 / 小小肉 / 小小猪)。
_WAKE_VARIANTS = set("瑞翠队子薇妹绿贵月院丽热略玉思想看度弟肉猪蜜")


def _clean_wake_rest(rest: str) -> str:
    return rest.lstrip(" \t，。！？,.!?、")


_WAKE_SHAPE_PREFIXES = ("", "喂", "为", "谢谢", "哎", "嗯")


def _wake_shape_at(text, start):
    """在 text[start:] 上判 小X小Y, 返回 (剩余文本, 命中词) 或 (None, None)。"""
    seg = text[start:]
    if len(seg) < 4 or not seg.startswith("小") or seg[2] != "小":
        # 3 字形态"小小X"(2026-10-21 板端实测): 用户快速说"小瑞小瑞"
        # (两个单元各 ~0.3s) 时云 ASR 稳定给 3 字并把"瑞"吞成近音 ——
        # 小小热 / 小小弟 / 小小肉 / 小小鳄 / 小小猪 / 小小子 (6/6)。
        # 慢说才会给 4 字"小瑞小瑞"。不收这条就等于"喊一声叫不醒"。
        # 收的条件(故意保守):
        #   - 纯唤醒(整句就 3 个字): 一律收 —— 正常句子里"小小X"独立成句罕见
        #     ("小小的心愿"这类都带尾巴, 走下一档);
        #   - 带尾巴: 第 3 字必须在听岔表里, 否则"小小年纪就..."会被当唤醒。
        if len(seg) >= 3 and seg.startswith("小小"):
            if len(seg) == 3:
                return "", seg
            if seg[2] in _WAKE_VARIANTS:
                return _clean_wake_rest(seg[3:]), seg[:3]
        return None, None
    a, b = seg[1], seg[3]
    if a != b and (a not in _WAKE_VARIANTS or b not in _WAKE_VARIANTS):
        return None, None
    return _clean_wake_rest(seg[4:]), seg[:4]


def _match_wake_shape(text):
    """返回 (剩余文本, 命中词); 没命中返回 (None, None)。"""
    if not text:
        return None, None
    for prefix in _WAKE_SHAPE_PREFIXES:
        if not text.startswith(prefix):
            continue
        rest, hit = _wake_shape_at(text, len(prefix))
        if rest is not None:
            return rest, hit
    return None, None


def match_wake_shape(text):
    """小X小Y 形状判定。

    返回剥掉唤醒前缀后的剩余文本(可能是空串); 不是唤醒形状返回 None。
    只认双单元, 且两个单元要么相同、要么都是已知听岔字:
      "小度小度" / "小想小想" / "小丽小瑞" -> 唤醒
      "小明小刚来了" -> 两个单元既不相同也不在听岔表 -> 不是唤醒(不误剥)
      "小度" / "小看看看" -> 单单元 -> 不是唤醒
    仅允许白名单里的至多 2 字引导词(喂/为/谢谢/哎/嗯): “为”保留作“喂”的同音转写。
    云 ASR 会把同一句唤醒词转成
    "为小鹿小鹿帮我的忙""谢谢小度小度晚安" —— 唤醒词完整在里面, 只是前面
    多了个"为/谢谢"。以前只认句首, 这两条真转写都被判"未含唤醒词"丢掉。
    前缀再长(如"我认识小度小度")就不认: 那更像是在说别的助手。
    """
    rest, _ = _match_wake_shape(text)
    return rest


def strip_wake_prefix(text, words):
    """剥掉一句式唤醒前缀。

    返回 (剩余文本, 命中的唤醒词); 没命中返回 (None, None)。
    比较大小写不敏感: 拼音形态(xiaorui)的大小写由 ASR 决定, 配置里只列小写。
    词表之外再退回"形状"判定(小X小Y): 云 ASR 把"小瑞小瑞"转成"小度小度"
    时, 枚举词表永远追不上, 但形状稳定。
    """
    lowered = text.lower()
    for w in sorted(words, key=len, reverse=True):
        if lowered.startswith(w.lower()):
            return text[len(w):], w
    rest = match_wake_shape(text)
    if rest is not None:
        return rest, text[: len(text) - len(rest)]
    return None, None


def wake_word_list(config) -> list:
    """wakeup_words 兼容解析: 上游是逗号分隔字符串, 也容忍 list。
    板上实测(2026-10-19): 配置漏写该键时此处得到空 -> 唤醒词门失效,
    "小瑞小瑞"整句漏进 LLM(RKLLM 还会幻觉"好的，已关闭")。"""
    raw = config.get("wakeup_words") or []
    if isinstance(raw, str):
        return [w.strip() for w in raw.split(",") if w.strip()]
    return list(raw)


async def _h_volume(conn, text, m):
    from core.handle.volumeIntent import send_volume_action
    await send_volume_action(conn, text, m)
    return True


async def _h_light(conn, text, m):
    from core.handle.haIntent import handle_light_intent
    return await handle_light_intent(conn, text, m)


async def _h_reboot(conn, text, m):
    from core.handle.rebootIntent import handle_reboot_intent
    return await handle_reboot_intent(conn, text)


async def _h_voiceprint_enroll(conn, text, m):
    from core.handle.voiceprintIntent import handle_voiceprint_intent
    return await handle_voiceprint_intent(conn, text)


async def _h_whoami(conn, text, m):
    from core.handle.voiceprintIntent import handle_whoami_intent
    return await handle_whoami_intent(conn, text)


async def _h_repeat(conn, text, m):
    from core.handle.repeatIntent import handle_repeat_intent
    return await handle_repeat_intent(conn, text)


async def _h_time(conn, text, m):
    from core.handle.timeIntent import handle_time_intent
    return await handle_time_intent(conn, text)


async def _h_next(conn, text, m):
    from core.handle.nextIntent import handle_next_intent
    return await handle_next_intent(conn, text)


async def _h_radio(conn, text, m):
    from plugins_func.functions.play_music import try_radio_intent
    return await try_radio_intent(conn, text)


async def _h_timer_list(conn, text, m):
    from core.handle.timerIntent import handle_timer_list_intent
    return await handle_timer_list_intent(conn, text)


async def _h_timer_set(conn, text, m):
    from core.handle.timerIntent import handle_timer_intent
    return await handle_timer_intent(conn, text, m)


async def _h_clock(conn, text, m):
    # match_clock_alarm 已按 handle_timer_intent 契约返回:
    # set -> op/seconds/speak; cancel_clock -> op/speak
    from core.handle.timerIntent import handle_timer_intent
    return await handle_timer_intent(conn, text, m)


def _local_intents():
    """惰性装配(避免模块级拉起重依赖链)。"""
    from core.handle.rebootIntent import match_reboot_query
    from core.handle.volumeIntent import match_volume_intent
    from core.handle.haIntent import match_light_cmd
    from core.handle.voiceprintIntent import match_voiceprint_enroll, match_who_am_i
    from core.handle.repeatIntent import match_repeat_query
    from core.handle.timeIntent import match_time_query
    from core.handle.nextIntent import match_next_query
    from core.handle.timerIntent import (match_timer_intent, match_clock_alarm,
                                         match_timer_list_query)
    return [
        ("reboot",   "filtered", _b(match_reboot_query),         _h_reboot),
        ("volume",   "filtered", match_volume_intent,            _h_volume),
        ("ha-light", "filtered", match_light_cmd,                _h_light),
        ("vp-enroll","filtered", match_voiceprint_enroll,        _h_voiceprint_enroll),
        ("whoami",   "filtered", _b(match_who_am_i),             _h_whoami),
        ("repeat",   "filtered", _b(match_repeat_query),         _h_repeat),
        ("time",     "filtered", _b(match_time_query),           _h_time),
        ("next",     "filtered", _b(match_next_query),           _h_next),
        ("radio",    "filtered", lambda t: t if ("电台" in t or "广播" in t) else None,
                    _h_radio),
        ("timer-list", "filtered", _b(match_timer_list_query),   _h_timer_list),
        ("timer-set",  "filtered", match_timer_intent,           _h_timer_set),
        ("clock",      "raw",      match_clock_alarm,            _h_clock),
    ]


async def handle_user_intent(conn: "ConnectionHandler", text):
    # 预处理输入文本，处理可能的JSON格式
    try:
        if text.strip().startswith("{") and text.strip().endswith("}"):
            parsed_data = json.loads(text)
            if isinstance(parsed_data, dict) and "content" in parsed_data:
                text = parsed_data["content"]  # 提取content用于意图分析
                conn.current_speaker = parsed_data.get("speaker")  # 保留说话人信息
    except (json.JSONDecodeError, TypeError):
        pass

    # 检查是否有明确的退出命令
    _, filtered_text = remove_punctuation_and_length(text)
    if await check_direct_exit(conn, filtered_text):
        return True

    # 噪声/礼貌填充词静默门(须在 stt 回显前): 嗯/啊等杂波不回显不进 LLM;
    # 多字礼貌词("好好""嗯好的")与英文误听("Thank you.")同理, 否则板子会对
    # 一句噪声闲聊, 看着像"噪声被当人声"(2026-10-20 实测)
    if is_noise_token(filtered_text):
        conn.logger.bind(tag=TAG).info(f"噪声/填充词, 静默忽略: {filtered_text}")
        await _notify_silent_drop(conn, "noise")
        return True

    # 云端优先唤醒门(2026-10-20): 冷启动轮由设备标记 wake=required —— 这段
    # 音频到底是不是在叫它, 用云 ASR 的转写判定(本地 RKNN 只做预筛: 同一句
    # "小瑞小瑞", 本地给小想, 云端给小度小度, 后者才靠得住)。没过门就静默
    # 回执, 绝不进 LLM —— 否则"随便说句话都被答应"比漏唤醒严重得多。
    if getattr(conn, "wake_required", False):
        conn.wake_required = False      # 只对这一轮生效, 下一轮 listen/start 重设
        _, hit_word = strip_wake_prefix(filtered_text, wake_word_list(conn.config))
        if hit_word is None:
            conn.logger.bind(tag=TAG).info(f"云端转写未含唤醒词, 不答话: {filtered_text}")
            await _notify_silent_drop(conn, "nowake")
            return True

    # 检查是否是唤醒词
    if await checkWakeupWords(conn, filtered_text):
        return True

    # 唤醒词静默/剥离门(2026-10-19): 缓存开关关闭时 checkWakeupWords 不拦截,
    # "小瑞小瑞…"漏进 LLM 会闲聊出"嗯？有事要找我吗？"(RKLLM 甚至幻觉
    # "好的，已关闭")。处理:
    #   恰好只有唤醒词 -> 静默吃掉; 唤醒词+指令(一句式) -> 剥前缀继续走意图链
    # 词表之外还认"小X小Y"形状: 云 ASR 常把"小瑞小瑞"转成"小度小度"。
    rest, hit_word = strip_wake_prefix(filtered_text, wake_word_list(conn.config))
    if hit_word:
        if not rest:
            conn.logger.bind(tag=TAG).info(f"唤醒词文本静默忽略: {filtered_text}")
            await _notify_silent_drop(conn, "wake")
            return True
        conn.logger.bind(tag=TAG).info(f"一句式唤醒剥前缀: {hit_word} + {rest}")
        text, filtered_text = rest, rest

    # 本地意图族(注册表顺序即优先级)
    for name, mode, match_fn, handle_fn in _local_intents():
        payload = text if mode == "raw" else filtered_text
        m = match_fn(payload)
        if m is None:
            continue
        if await handle_fn(conn, payload, m):
            return True

    # 短转写门(2026-10-20): 意图链没接住、文本又短到不成句 -> "没听清", 不叫 LLM。
    # 见 is_too_short_to_chat 注释。两个位置约束缺一不可:
    #   1. 必须晚于本地意图族: "开灯/下一首/停下/几点了/静音"都是两三个字的
    #      真指令, 由意图接管;
    #   2. 必须早于 function_call 的 return False —— 板内 8001 就是
    #      Intent: function_call, 放在它后面等于永远不执行, 实测"不知道"
    #      (3 字)照样进了 LLM, 答出"那我来帮你找找看吧"。
    if is_too_short_to_chat(filtered_text):
        conn.logger.bind(tag=TAG).info(f"转写过短, 回没听清(不进 LLM): {filtered_text}")
        await send_stt_message(conn, filtered_text)
        conn.sentence_id = str(uuid.uuid4().hex)
        speak_txt(conn, "没听清，请再说一遍")
        return True

    if conn.intent_type == "function_call":
        # 使用支持function calling的聊天方法,不再进行意图分析
        return False

    # 使用LLM进行意图分析
    intent_result = await analyze_intent_with_llm(conn, text)
    if not intent_result:
        return False
    # 会话开始时生成sentence_id
    conn.sentence_id = str(uuid.uuid4().hex)
    # 处理各种意图
    return await process_intent_result(conn, intent_result, text)


async def check_direct_exit(conn: "ConnectionHandler", text):
    """检查是否有明确的退出命令"""
    _, text = remove_punctuation_and_length(text)
    cmd_exit = conn.cmd_exit
    for cmd in cmd_exit:
        if text == cmd:
            conn.logger.bind(tag=TAG).info(f"识别到明确的退出命令: {text}")
            await send_stt_message(conn, text)
            await conn.close()
            return True
    return False


async def analyze_intent_with_llm(conn: "ConnectionHandler", text):
    """使用LLM分析用户意图"""
    if not hasattr(conn, "intent") or not conn.intent:
        conn.logger.bind(tag=TAG).warning("意图识别服务未初始化")
        return None

    # 对话历史记录
    dialogue = conn.dialogue
    try:
        intent_result = await conn.intent.detect_intent(conn, dialogue.dialogue, text)
        return intent_result
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"意图识别失败: {str(e)}")

    return None


async def process_intent_result(
    conn: "ConnectionHandler", intent_result, original_text
):
    """处理意图识别结果"""
    try:
        # 尝试将结果解析为JSON
        intent_data = json.loads(intent_result)

        # 检查是否有function_call
        if "function_call" in intent_data:
            # 直接从意图识别获取了function_call
            conn.logger.bind(tag=TAG).debug(
                f"检测到function_call格式的意图结果: {intent_data['function_call']['name']}"
            )
            function_name = intent_data["function_call"]["name"]
            if function_name == "continue_chat":
                return False

            if function_name == "result_for_context":
                await send_stt_message(conn, original_text)
                conn.client_abort = False

                def process_context_result():
                    conn.dialogue.put(Message(role="user", content=original_text))

                    from core.utils.current_time import get_current_time_info

                    current_time, today_date, today_weekday, lunar_date = (
                        get_current_time_info()
                    )

                    # 构建带上下文的基础提示
                    context_prompt = f"""当前时间：{current_time}
                                        今天日期：{today_date} ({today_weekday})
                                        今天农历：{lunar_date}

                                        请根据以上信息回答用户的问题：{original_text}"""

                    # 使用异步调用避免阻塞事件循环，影响其他设备的音频播放
                    try:
                        response = asyncio.run_coroutine_threadsafe(
                            conn.intent.replyResult(context_prompt, original_text),
                            conn.loop,
                        ).result()
                    except Exception as e:
                        conn.logger.bind(tag=TAG).error(f"LLM生成回复失败: {e}")
                        response = None
                    if response:
                        speak_txt(conn, response)

                conn.executor.submit(process_context_result)
                return True

            function_args = {}
            if "arguments" in intent_data["function_call"]:
                function_args = intent_data["function_call"]["arguments"]
                if function_args is None:
                    function_args = {}
            # 确保参数是字符串格式的JSON
            if isinstance(function_args, dict):
                function_args = json.dumps(function_args)

            function_call_data = {
                "name": function_name,
                "id": str(uuid.uuid4().hex),
                "arguments": function_args,
            }

            await send_stt_message(conn, original_text)
            conn.client_abort = False

            # 准备工具调用参数
            tool_input = {}
            if function_args:
                if isinstance(function_args, str):
                    tool_input = json.loads(function_args) if function_args else {}
                elif isinstance(function_args, dict):
                    tool_input = function_args

            # 上报工具调用
            enqueue_tool_report(conn, function_name, tool_input)

            # 使用executor执行函数调用和结果处理
            def process_function_call():
                conn.dialogue.put(Message(role="user", content=original_text))
                
                # 工具调用超时时间
                tool_call_timeout = int(conn.config.get("tool_call_timeout", 30))
                # 使用统一工具处理器处理所有工具调用
                try:
                    result = asyncio.run_coroutine_threadsafe(
                        conn.func_handler.handle_llm_function_call(
                            conn, function_call_data
                        ),
                        conn.loop,
                    ).result(timeout=tool_call_timeout)
                except Exception as e:
                    conn.logger.bind(tag=TAG).error(f"工具调用失败: {e}")
                    result = ActionResponse(
                        action=Action.ERROR, result="工具调用超时，请一会再试下哈", response="工具调用超时，请一会再试下哈"
                    )

                # 上报工具调用结果
                if result:
                    enqueue_tool_report(conn, function_name, tool_input, str(result.result) if result.result else None, report_tool_call=False)

                    if result.action == Action.RESPONSE:  # 直接回复前端
                        text = result.response
                        if text is not None:
                            speak_txt(conn, text)
                    elif result.action == Action.REQLLM:  # 调用函数后再请求llm生成回复
                        text = result.result
                        conn.dialogue.put(Message(role="tool", content=text))
                        # 使用异步调用避免阻塞事件循环，影响其他设备的音频播放
                        try:
                            llm_result = asyncio.run_coroutine_threadsafe(
                                conn.intent.replyResult(text, original_text),
                                conn.loop,
                            ).result()
                        except Exception as e:
                            conn.logger.bind(tag=TAG).error(f"LLM生成回复失败: {e}")
                            llm_result = text
                        if llm_result is None:
                            llm_result = text
                        speak_txt(conn, llm_result)
                    elif (
                        result.action == Action.NOTFOUND
                        or result.action == Action.ERROR
                    ):
                        text = result.response if result.response else result.result
                        if text is not None:
                            speak_txt(conn, text)
                    elif function_name != "play_music":
                        # For backward compatibility with original code
                        # 获取当前最新的文本索引
                        text = result.response
                        if text is None:
                            text = result.result
                        if text is not None:
                            speak_txt(conn, text)

            # 将函数执行放在线程池中
            conn.executor.submit(process_function_call)
            return True
        return False
    except json.JSONDecodeError as e:
        conn.logger.bind(tag=TAG).error(f"处理意图结果时出错: {e}")
        return False


def speak_txt(conn: "ConnectionHandler", text):
    # 记录文本到 sentence_id 映射
    conn.tts.store_tts_text(conn.sentence_id, text)

    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
        )
    )
    conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text)
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.LAST,
            content_type=ContentType.ACTION,
        )
    )
    conn.dialogue.put(Message(role="assistant", content=text))
