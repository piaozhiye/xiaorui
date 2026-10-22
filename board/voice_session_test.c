/* voice_session_test.c — 固定 now 序列的状态机断言 (无时钟、无 IO) */
#include <stdio.h>
#include <string.h>
#include "voice_session.h"

static int failures;
#define CHECK(cond, msg) do { \
    if (!(cond)) { failures++; fprintf(stderr, "FAIL %d: %s\n", __LINE__, (msg)); } \
} while (0)

int main(void) {
    xr_session_t s;
    xr_session_init(&s);
    CHECK(s.state == XR_SESSION_LISTEN, "init 应为 LISTEN");
    CHECK(s.pkt_n == 0 && s.rescue_used == 0, "init 应清零计数");

    /* begin_turn 统一四处重置 */
    xr_session_begin_turn(&s, 100.0);
    CHECK(s.state == XR_SESSION_RESPOND, "begin_turn 应进 RESPOND");
    CHECK(s.respond_t0 == 100.0, "respond_t0 应为 now");
    s.pkt_n = 7; s.last_pkt_t = 99.0; s.rescue_used = 1;
    xr_session_begin_turn(&s, 200.0);
    CHECK(s.pkt_n == 0 && s.last_pkt_t == 0 && s.rescue_used == 0,
          "begin_turn 应重置 pkt_n/last_pkt_t/rescue_used");

    /* timed_out: pkt_n==0 且 seen_stt => 8s 分支; 无 stt => 20s 分支 */
    xr_session_begin_turn(&s, 1000.0);
    CHECK(!xr_session_timed_out(&s, 1007.0, 1, 8.0, 20.0), "7s 时不该超时");
    CHECK(xr_session_timed_out(&s, 1009.0, 1, 8.0, 20.0), "seen_stt 后 9s 应超时");
    CHECK(!xr_session_timed_out(&s, 1009.0, 0, 8.0, 20.0), "无 stt 时 9s 不该超时");
    CHECK(xr_session_timed_out(&s, 1021.0, 0, 8.0, 20.0), "无 stt 时 21s 应超时");

    /* 有音频包就不再判超时 */
    s.pkt_n = 1;
    CHECK(!xr_session_timed_out(&s, 1025.0, 1, 8.0, 20.0), "有音频包时不该超时");

    /* 换路续答: 标记后重置等待, 且查询得到已用 */
    CHECK(!xr_session_rescue_used(&s), "初始不该已用救援");
    xr_session_mark_rescued(&s, 3000.0);
    CHECK(xr_session_rescue_used(&s), "mark_rescued 后应标记已用");
    CHECK(s.respond_t0 == 3000.0 && s.pkt_n == 0 && s.last_pkt_t == 0,
          "mark_rescued 应重置等待计时");

    /* end_turn: 冷却值按调用点传入, 跟说窗 30s */
    xr_session_end_turn(&s, 4000.0, 1.2);
    CHECK(s.state == XR_SESSION_LISTEN, "end_turn 应回 LISTEN");
    CHECK(s.awake_until == 4030.0, "跟说窗应为 now+30");
    CHECK(s.cool_until == 4001.2, "冷却应为 now+1.2");
    CHECK(xr_session_in_window(&s, 4029.9), "窗内应为 1");
    CHECK(!xr_session_in_window(&s, 4030.1), "出窗应为 0");
    CHECK(xr_session_cooling(&s, 4001.0), "冷却内应为 1");
    CHECK(!xr_session_cooling(&s, 4001.3), "冷却结束应为 0");
    xr_session_end_turn(&s, 5000.0, 0.8);
    CHECK(s.cool_until == 5000.8, "放弃路径冷却应为 now+0.8");

    /* end_turn_silent: 只回 LISTEN + 冷却, 不开跟说窗。
       噪声/零音频轮不许白送 30s 免唤醒(否则之后随口一句话就被当"跟说")。 */
    s.awake_until = 0;
    xr_session_begin_turn(&s, 8000.0);
    xr_session_end_turn_silent(&s, 8000.5, 1.0);
    CHECK(s.state == XR_SESSION_LISTEN, "end_turn_silent 应回 LISTEN");
    CHECK(s.awake_until == 0, "end_turn_silent 不应开跟说窗");
    CHECK(!xr_session_in_window(&s, 8001.0), "静默收轮后窗口应仍关闭");
    CHECK(s.cool_until == 8001.5, "静默收轮仍应设冷却");
    /* 已有窗口不动: 窗属于更早那次真唤醒, 静默收轮不该把它清掉 */
    xr_session_awake(&s, 9000.0);
    xr_session_begin_turn(&s, 9001.0);
    xr_session_end_turn_silent(&s, 9002.0, 0.5);
    CHECK(xr_session_in_window(&s, 9010.0), "静默收轮不应清掉已有窗口");
    CHECK(s.awake_until == 9030.0, "静默收轮不能延长已有窗口");
    xr_session_begin_turn(&s, 9031.0);
    xr_session_end_turn_silent(&s, 9032.0, 0.5);
    CHECK(s.awake_until == 9030.0, "静默收轮应保留已过期窗口时间");
    CHECK(!xr_session_in_window(&s, 9032.0), "静默收轮不能复活过期窗口");

    /* awake 只开窗不改状态; set_cool 单独设冷却 */
    xr_session_awake(&s, 6000.0);
    CHECK(s.state == XR_SESSION_LISTEN, "awake 不应改状态");
    CHECK(s.awake_until == 6030.0, "awake 应开 30s 窗");
    xr_session_set_cool(&s, 6000.0, 3.0);
    CHECK(s.cool_until == 6003.0, "set_cool 应设置冷却");

    /* abort_turn 回 LISTEN 但不开窗 */
    xr_session_begin_turn(&s, 7000.0);
    s.awake_until = 12345.0;
    xr_session_abort_turn(&s);
    CHECK(s.state == XR_SESSION_LISTEN, "abort_turn 应回 LISTEN");
    CHECK(s.awake_until == 12345.0, "abort_turn 不应动跟说窗");

    /* ping 节律 25s */
    xr_session_init(&s);
    xr_session_note_ping(&s, 100.0);
    CHECK(!xr_session_ping_due(&s, 124.9), "24.9s 不该到点");
    CHECK(xr_session_ping_due(&s, 125.0), "25.0s 应到点");
    xr_session_note_ping(&s, 125.0);
    CHECK(!xr_session_ping_due(&s, 149.9), "重置后 24.9s 不该到点");

    /* note_packet: 先按 push 前的 pkt_n 判首包/gap 并更新 last_pkt_t, 之后才 pkt_n++ */
    xr_session_init(&s);
    xr_session_begin_turn(&s, 50.0);
    xr_session_note_packet(&s, 51.0, 1, 0.5, 0);      /* timing 关: 只计数 */
    CHECK(s.pkt_n == 1 && s.last_pkt_t == 0, "timing 关时不该更新 last_pkt_t");
    xr_session_note_packet(&s, 52.0, 1, 0.5, 0);
    CHECK(s.pkt_n == 2, "每次成功解码应计数");
    xr_session_note_packet(&s, 53.0, -1, 0.5, 1);     /* 解码失败: 更新 last_pkt_t, 不计数 */
    CHECK(s.pkt_n == 2, "解码失败不应计数");
    CHECK(s.last_pkt_t == 53.0, "timing 开时 last_pkt_t 应更新");
    xr_session_note_packet(&s, 53.1, 1, 0.5, 1);
    CHECK(s.pkt_n == 3, "解码失败后恢复计数");
    xr_session_begin_turn(&s, 100.0);
    xr_session_note_packet(&s, 101.0, 1, 0.5, 1);
    CHECK(s.pkt_n == 1, "begin_turn 后计数从头开始");

    /* 回声护栏: 截止时刻前拦, 之后放行 */
    CHECK(!xr_session_echo_guarded(&s, 100.0), "未设护栏应放行");
    xr_session_set_echo_guard(&s, 104.0);
    CHECK(xr_session_echo_guarded(&s, 103.9), "护栏期内应拦截");
    CHECK(!xr_session_echo_guarded(&s, 104.0), "护栏到期应放行");
    xr_session_awake(&s, 100.0);
    CHECK(xr_session_drop_echo(&s, 101.0, "好", 3), "护栏期内已知余音应拦截");
    CHECK(xr_session_drop_echo(&s, 101.0, "好的。", 3), "余音尾标点不应绕过护栏");
    CHECK(xr_session_drop_echo(&s, 101.0, "好好", 3), "重复余音应拦截");
    CHECK(!xr_session_drop_echo(&s, 101.0, "继续", 3), "真实短指令继续应放行");
    CHECK(!xr_session_drop_echo(&s, 101.0, "停下", 3), "真实短指令停下应放行");
    CHECK(!xr_session_drop_echo(&s, 101.0, "再见", 3), "真实短指令再见应放行");
    CHECK(!xr_session_drop_echo(&s, 101.0, "开", 3), "不按长度丢弃未知短指令");
    CHECK(!xr_session_drop_echo(&s, 104.0, "好", 3), "到期余音也放行");
    CHECK(!xr_session_drop_echo(&s, 101.0, "好", 1), "字数阈值边界应放行");
    s.awake_until = 100.0;
    CHECK(!xr_session_drop_echo(&s, 101.0, "好", 3), "窗外不应用护栏");
    xr_session_awake(&s, 100.0);
    xr_session_set_echo_guard(&s, 100.0);
    CHECK(!xr_session_drop_echo(&s, 101.0, "好", 3), "关闭护栏时放行");

    fprintf(stderr, failures ? "voice_session_test: %d FAILED\n" : "voice_session_test: OK\n",
            failures);
    return failures ? 1 : 0;
}
