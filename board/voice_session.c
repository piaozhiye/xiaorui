/* voice_session.c — 轮次状态机实现 (值与顺序照搬拆分前 xiaorui_live.c 的同名逻辑) */
#include <stdio.h>
#include <string.h>
#include "voice_session.h"

void xr_session_init(xr_session_t *s) {
    s->state = XR_SESSION_LISTEN;
    s->awake_until = 0; s->cool_until = 0; s->echo_guard_until = 0;
    s->respond_t0 = 0; s->last_pkt_t = 0; s->last_ping = 0;
    s->pkt_n = 0; s->rescue_used = 0; s->think_cue_played = 0;
    s->follow_window_s = 30;
}

/* conn_rc==2 / XR_TEXT_INJECT / 提交成功 / 换路续答 四处共用的"进轮次"重置 */
void xr_session_begin_turn(xr_session_t *s, double now) {
    s->state = XR_SESSION_RESPOND;
    s->respond_t0 = now; s->pkt_n = 0; s->last_pkt_t = 0; s->rescue_used = 0;
    s->think_cue_played = 0;   /* 新一轮重新武装填充音 */
}

void xr_session_end_turn(xr_session_t *s, double now, double cooldown_s) {
    s->state = XR_SESSION_LISTEN;
    s->awake_until = now + s->follow_window_s;   /* 跟说窗口 */
    s->cool_until = now + cooldown_s;
}

void xr_session_end_turn_silent(xr_session_t *s, double now, double cooldown_s) {
    s->state = XR_SESSION_LISTEN;
    s->cool_until = now + cooldown_s;
    /* awake_until 故意不动: 没出声的轮次不换免唤醒窗。 */
}

void xr_session_abort_turn(xr_session_t *s) { s->state = XR_SESSION_LISTEN; }

void xr_session_awake(xr_session_t *s, double now) { s->awake_until = now + s->follow_window_s; }
void xr_session_set_follow_window(xr_session_t *s, int seconds) {
    if (seconds > 0) s->follow_window_s = seconds;
}
void xr_session_set_echo_guard(xr_session_t *s, double until) { s->echo_guard_until = until; }
int xr_session_echo_guarded(const xr_session_t *s, double now) { return now < s->echo_guard_until; }
int xr_session_drop_echo(const xr_session_t *s, double now, const char *text, int min_chars) {
    if (!text || !xr_session_in_window(s, now) || !xr_session_echo_guarded(s, now)) return 0;
    while (*text == ' ' || *text == '\t') ++text;
    size_t len = strlen(text);
    if (len >= 64) return 0;
    char word[64];
    memcpy(word, text, len + 1);
    for (;;) {
        if (len && strchr(" \t\r\n,.!?", word[len - 1])) { word[--len] = 0; continue; }
        if (len >= 3 && (!memcmp(word + len - 3, "，", 3)
                        || !memcmp(word + len - 3, "。", 3)
                        || !memcmp(word + len - 3, "！", 3)
                        || !memcmp(word + len - 3, "？", 3))) {
            len -= 3; word[len] = 0; continue;
        }
        break;
    }
    int chars = 0;
    for (size_t i = 0; i < len; ++i)
        if (((unsigned char)word[i] & 0xc0) != 0x80) ++chars;
    if (chars >= min_chars) return 0;
    const char *residue[] = {"好", "好的", "好好", "好好好", "我", "嗯", "啊", "哦", "然后"};
    for (size_t i = 0; i < sizeof(residue) / sizeof(residue[0]); ++i)
        if (!strcmp(word, residue[i])) return 1;
    return 0;
}
void xr_session_set_cool(xr_session_t *s, double now, double cooldown_s) {
    s->cool_until = now + cooldown_s;
}
int xr_session_in_window(const xr_session_t *s, double now) { return now < s->awake_until; }
int xr_session_cooling(const xr_session_t *s, double now) { return now < s->cool_until; }

int xr_session_ping_due(const xr_session_t *s, double now) { return now - s->last_ping >= 25.0; }
void xr_session_note_ping(xr_session_t *s, double now) { s->last_ping = now; }

int xr_session_rescue_used(const xr_session_t *s) { return s->rescue_used; }
void xr_session_mark_rescued(xr_session_t *s, double now) {
    s->rescue_used = 1;
    s->respond_t0 = now; s->pkt_n = 0; s->last_pkt_t = 0;
}

int xr_session_timed_out(const xr_session_t *s, double now, int seen_stt,
                          double accepted_silence_s, double response_timeout_s) {
    if (s->pkt_n != 0) return 0;
    double waited = now - s->respond_t0;
    return (seen_stt && waited > accepted_silence_s) || waited > response_timeout_s;
}

/* 照搬 xiaorui_live.c:426-440 的顺序: 先用 push 前的 pkt_n 判首包/gap 并更新
   last_pkt_t, 之后才 if (ns > 0) pkt_n++ —— 顺序不可调换 */
void xr_session_note_packet(xr_session_t *s, double now, int ns, double ring_used_s,
                            int timing_on) {
    if (timing_on) {
        if (s->pkt_n == 0)
            fprintf(stderr, "[T] 首包 %.2ffs ring_used=%.1fs\n",
                    now - s->respond_t0, ring_used_s);
        else if (now - s->last_pkt_t > 0.15)
            fprintf(stderr, "[T] pkt#%d gap=%.2ffs ring_used=%.1fs\n",
                    s->pkt_n, now - s->last_pkt_t, ring_used_s);
        s->last_pkt_t = now;
    }
    if (ns > 0) s->pkt_n++;
}

int xr_session_wants_think_cue(const xr_session_t *s, double now, int threshold_ms) {
    if (!s || threshold_ms <= 0) return 0;          /* 0 = 现场关闭 */
    if (s->think_cue_played) return 0;             /* 本轮已补过 */
    if (s->pkt_n != 0) return 0;                   /* 音频已开响, 再补就是噪音 */
    return (now - s->respond_t0) * 1000.0 >= (double)threshold_ms;
}
void xr_session_mark_think_cue(xr_session_t *s) { if (s) s->think_cue_played = 1; }

double xr_session_cool_until(const xr_session_t *s) { return s->cool_until; }
double xr_session_echo_guard_until(const xr_session_t *s) { return s->echo_guard_until; }

int xr_session_play_fail_cue(xr_session_t *s, double now,
                             const xr_cue_ops_t *ops,
                             const int16_t *pcm, size_t samples,
                             int cool_ms, int echo_guard_s) {
    /* 提示音固定 24kHz 单声道(cue_load 已保证), 时长由样本数推。
       任何一步失败都直接返回 0 且不动状态: 播不出来时把麦聋掉只会让
       用户从"干等"变成"干等且听不见"。 */
    if (!s || !ops || !ops->play_cue || !pcm || !samples) return 0;
    double dur = (double)samples / 24000.0;
    if (ops->play_cue(pcm, samples)) return 0;
    if (ops->finish_cue) ops->finish_cue();
    /* 只延长不缩短: 晚到的、now 更小的一次不该把已有冷却改短 */
    double cool = now + dur + (cool_ms > 0 ? cool_ms / 1000.0 : 0.0);
    if (cool > s->cool_until) s->cool_until = cool;
    double guard = now + dur + (echo_guard_s > 0 ? echo_guard_s : 0.0);
    if (guard > s->echo_guard_until) s->echo_guard_until = guard;
    return 1;
}
