/* voice_session.h — 轮次状态机: LISTEN/RESPOND、跟说窗、冷却、计时、ping 节律.
 * 所有函数显式接收 now, 不取时钟 => 主机可确定性测试. 不依赖 backend/player. */
#ifndef XR_VOICE_SESSION_H
#define XR_VOICE_SESSION_H
#include <stddef.h>
#include <stdint.h>

typedef enum { XR_SESSION_LISTEN, XR_SESSION_RESPOND } xr_session_state_t;

typedef struct {
    xr_session_state_t state;
    double awake_until;   /* 跟说窗(免唤醒直接说) */
    double echo_guard_until; /* 回声护栏截止时刻 */
    int follow_window_s;  /* 窗长(默认30, main 可注入现场值) */
    double cool_until;    /* 播放余音冷却: 期间整帧丢弃, KWS 也不喂 */
    double respond_t0;    /* 本轮 RESPOND 起点: 首包耗时统计 */
    double last_pkt_t;    /* XR_TIMING 句间 gap 统计 */
    double last_ping;
    int pkt_n;            /* 本轮 TTS 包计数(成功解码才增) */
    int rescue_used;      /* 本轮是否已用过后端换路续答(防双路都静默打转) */
    int think_cue_played; /* 本轮是否已补过"嗯"(防双重填充音) */
} xr_session_t;

void xr_session_init(xr_session_t *s);
void xr_session_begin_turn(xr_session_t *s, double now);
void xr_session_end_turn(xr_session_t *s, double now, double cooldown_s);
/* 静默收轮: 回 LISTEN 并设冷却, 但**不开**跟说窗。
   用于"没出声的轮次"—— 服务端判定噪声的 silent 收轮、零音频收轮。
   以前这两处走 end_turn(), 等于任何噪声/空轮白送 30s 免唤醒: 之后屋里
   随口一句话都会被当"跟说"直接上云(2026-10-20 静默实测)。已有窗口不动
   (窗属于更早那次真唤醒)。 */
void xr_session_end_turn_silent(xr_session_t *s, double now, double cooldown_s);
void xr_session_abort_turn(xr_session_t *s);
void xr_session_awake(xr_session_t *s, double now);
void xr_session_set_follow_window(xr_session_t *s, int seconds);
/* 回声护栏时间；提交门还需用 drop_echo 判断已知余音文本。 */
void xr_session_set_echo_guard(xr_session_t *s, double until);
int  xr_session_echo_guarded(const xr_session_t *s, double now);
/* Only known short residue words, in both follow and guard windows, are suppressed. */
int  xr_session_drop_echo(const xr_session_t *s, double now, const char *text, int min_chars);
void xr_session_set_cool(xr_session_t *s, double now, double cooldown_s);
int  xr_session_in_window(const xr_session_t *s, double now);
int  xr_session_cooling(const xr_session_t *s, double now);
int  xr_session_ping_due(const xr_session_t *s, double now);
void xr_session_note_ping(xr_session_t *s, double now);
int  xr_session_rescue_used(const xr_session_t *s);
void xr_session_mark_rescued(xr_session_t *s, double now);
int  xr_session_timed_out(const xr_session_t *s, double now, int seen_stt,
                          double accepted_silence_s, double response_timeout_s);
void xr_session_note_packet(xr_session_t *s, double now, int ns, double ring_used_s,
                            int timing_on);

/* ---------- 弃轮反馈音 ----------
 * 用户喊完指令、云端 20s 不出声时前端只打 stderr, 设备表现为"完全没反应",
 * 用户无从分辨是没听见/还在想/已失败, 只会更大声重复 -> 更多误触发。
 * 用一声短提示音明确告知"没听清"。
 * 播放器经 ops 注入(与 speech_pipeline 同一套做法), 本模块不碰 ALSA/线程。 */
typedef struct {
    int  (*play_cue)(const int16_t *pcm, size_t samples);  /* 起播并推入, 0=成功 */
    void (*finish_cue)(void);                              /* 排干收尾 */
} xr_cue_ops_t;

/* 播提示音并按"提示音时长 + 余音冷却"聋掉麦克风、按"时长 + 护栏"设回声护栏。
   音频缺失或播放器失败时不播, 且**完全不改动会话状态**(否则播放失败反而
   白聋一段麦)。冷却/护栏只延长不缩短, 后到的、now 更小的一次不会把已有
   冷却改短。返回 1=已播, 0=未播。 */
int  xr_session_play_fail_cue(xr_session_t *s, double now,
                              const xr_cue_ops_t *ops,
                              const int16_t *pcm, size_t samples,
                              int cool_ms, int echo_guard_s);
/* ---------- 慢响应填充音("嗯") ----------
 * 实测云端音频路由首包要 2.58~3.16s, 这段静默里用户会以为没被听见而重复说话。
 * 到阈值还没出声就先补一声短"嗯"。阈值 0 = 关闭。
 * 三条全满足才补: 本轮零音频包(pkt_n==0) + 本轮没补过 + 距提交已过阈值。
 * 换路续答会重置 respond_t0 重新计时, 但不清 think_cue_played ——
 * 一次用户发言只允许补一声。 */
int  xr_session_wants_think_cue(const xr_session_t *s, double now, int threshold_ms);
void xr_session_mark_think_cue(xr_session_t *s);

/* 供主机测试读取策略结果; 主循环不需要。 */
double xr_session_cool_until(const xr_session_t *s);
double xr_session_echo_guard_until(const xr_session_t *s);
#endif
