/* voice_session_think_test.c — 慢响应填充音("嗯")的触发条件门禁。
 *
 * 实测(2026-10-15, 云端音频路由): 用户说完指令到第一个音频包 **2.58~3.16s**。
 * 这 3 秒里设备完全静音, 用户无法判断是否已被听见, 很可能重复说话造成双重提交。
 * 空闲等待超时前先补一声短"嗯"(xr_hmm24k.wav 已加载但一直没用), 把静默打断,
 * 同时明确"听到了, 在想"。
 *
 * 触发条件(xr_session_wants_think_cue):
 *   本轮一个音频包都还没来(pkt_n==0)  且  本轮还没补过  且  距提交已过阈值
 * 三条任一不满足都不补 —— 尤其音频已开响时再补一声是噪音。
 */
#include "voice_session.h"
#include <stdio.h>

static int failures;
#define CHECK(cond, msg) do { \
    if (!(cond)) { failures++; fprintf(stderr, "FAIL %d: %s\n", __LINE__, (msg)); } \
} while (0)

int main(void) {
    xr_session_t s;
    const int MS = 700;   /* 默认值: 必须小于 RESPOND 的 1000ms 轮询周期 */

    /* 1. 没到阈值不补: 用户刚说完, 0.5s 时不该出声 */
    xr_session_init(&s);
    xr_session_begin_turn(&s, 100.0);
    CHECK(!xr_session_wants_think_cue(&s, 100.5, MS), "刚提交 0.5s 不该补");
    CHECK(!xr_session_wants_think_cue(&s, 100.69, MS), "0.69s(阈值前)不该补");

    /* 2. 过阈值且还没出声 -> 补 */
    CHECK(xr_session_wants_think_cue(&s, 100.71, MS), "0.71s(过阈值)应补");
    xr_session_mark_think_cue(&s);
    CHECK(!xr_session_wants_think_cue(&s, 101.25, MS),
          "本轮已补过就不该再补(否则双重\"嗯\")");

    /* 3. 音频已开响 -> 不补(哪怕还没补过, 也已不需要) */
    xr_session_init(&s);
    xr_session_begin_turn(&s, 100.0);
    xr_session_note_packet(&s, 101.5, 480, 0.0, 0);
    CHECK(s.pkt_n == 1, "记包后 pkt_n 应为 1");
    CHECK(!xr_session_wants_think_cue(&s, 101.6, MS),
          "已有音频包时不该再补\"嗯\"");

    /* 4. 新一轮重新武装: 用户又问了一次, 应该能再补。
          上一轮 pkt_n=1, 补前先确认旧状态确实不该补(仍被音频包挡着)。 */
    CHECK(!xr_session_wants_think_cue(&s, 200.0, MS),
          "上一轮已有音频包时仍不该补");
    xr_session_begin_turn(&s, 200.0);
    CHECK(xr_session_wants_think_cue(&s, 201.3, MS), "begin_turn 后应重新武装");

    /* 4b. 关键: 上一轮**确实补过**"嗯"时, begin_turn 必须清掉标记。
           只查 pkt_n 挡不住这一条 —— 上一轮补过但一个包都没来就超时了, 是
           最容易连续触发双重填充音的场景。 */
    xr_session_init(&s);
    xr_session_begin_turn(&s, 300.0);
    CHECK(xr_session_wants_think_cue(&s, 301.3, MS), "本轮该补");
    xr_session_mark_think_cue(&s);
    CHECK(!xr_session_wants_think_cue(&s, 301.4, MS), "补过就不该再补");
    xr_session_begin_turn(&s, 400.0);            /* 新一轮 */
    CHECK(xr_session_wants_think_cue(&s, 401.3, MS),
          "begin_turn 必须清掉上一轮的 think_cue_played, 否则永远不再补");

    /* 5. 阈值 0 = 关闭: 现场嫌\"嗯\"吵可以直接关 */
    xr_session_init(&s);
    xr_session_begin_turn(&s, 0.0);
    CHECK(!xr_session_wants_think_cue(&s, 99.0, 0), "阈值 0 应视为关闭");

    /* 6. 换路续答会重置 respond_t0(重新计时), 但不得让\"嗯\"补第二遍 */
    xr_session_init(&s);
    xr_session_begin_turn(&s, 100.0);
    CHECK(xr_session_wants_think_cue(&s, 101.3, MS), "该补");
    xr_session_mark_think_cue(&s);
    xr_session_mark_rescued(&s, 101.5);      /* respond_t0 被重置为 101.5 */
    CHECK(!xr_session_wants_think_cue(&s, 110.0, MS),
          "换路续答后不得补第二次\"嗯\"");

    /* 7. init 后状态干净(默认没补过) */
    xr_session_init(&s);
    CHECK(!s.think_cue_played, "init 应把 think_cue_played 清零");
    CHECK(!xr_session_wants_think_cue(&s, 0.0, MS), "init 后 t=0 不该补");

    fprintf(stderr, failures ? "voice_session_think_test: %d FAILED\n"
                             : "voice_session_think_test: OK\n", failures);
    return failures ? 1 : 0;
}