/* voice_session_cue_test.c — 弃轮反馈音的会话策略门禁。
 *
 * 背景: 用户喊完指令后如果云端 20s 不出声, 前端只打一行 stderr 就收轮,
 * 设备表现为"完全没反应" —— 用户无法分辨是没听见 / 还在想 / 已经失败,
 * 多数人只会更大声地重复, 反而制造更多误触发。
 *
 * 这里的策略集中在 xr_session_play_fail_cue: 播什么由调用方给(ops 表注入
 * 播放器, 与 speech_pipeline 同一套可注入做法), 本函数只管
 *   1) 该不该播(音频缺失 / 播放器失败 -> 不播, 且**不得动会话状态**)
 *   2) 播完把麦聋多久(冷却 = 提示音时长 + 余音冷却)
 *   3) 回声护栏怎么设
 *   4) 重复调用只延长不缩短(防止后一次用更早的 now 把已有冷却改短)
 */
#include "voice_session.h"
#include <math.h>
#include <stdio.h>
#include <string.h>

static int failures;
#define CHECK(cond, msg) do { \
    if (!(cond)) { failures++; fprintf(stderr, "FAIL %d: %s\n", __LINE__, (msg)); } \
} while (0)

#define RATE 24000
static int16_t cue[RATE];
static size_t cue_n;
static int played;              /* play_cue 调用次数 */
static int finished;            /* finish_cue 调用次数 */
static int fail_play;           /* 令 play_cue 返回失败 */

static int play_cue(const int16_t *pcm, size_t n) {
    if (fail_play) return -1;
    if (pcm != cue || n != cue_n) { failures++;
        fprintf(stderr, "FAIL play_cue 收到非预期音频\n"); return -1; }
    ++played;
    return 0;
}
static void finish_cue(void) { ++finished; }

static const xr_cue_ops_t OPS = { .play_cue = play_cue, .finish_cue = finish_cue };

static const double COOL_MS = 500, GUARD_S = 4;
static void setup(size_t samples) {
    memset(&cue[0], 0, sizeof cue);
    cue_n = samples;
    played = finished = 0; fail_play = 0;
}

int main(void) {
    xr_session_t s;

    /* 1. 正常播: 成功则返回 1, 并把麦按"提示音时长 + 余音冷却"聋掉 */
    xr_session_init(&s);
    setup(4000);                       /* 4000/24000 = 0.1667s */
    CHECK(xr_session_play_fail_cue(&s, 100.0, &OPS, cue, cue_n, COOL_MS, GUARD_S) == 1,
          "有音频且播放器可用时应播放");
    CHECK(played == 1, "play_cue 应被调用一次");
    CHECK(fabs(xr_session_cool_until(&s) - (100.0 + 0.1666666666 + 0.5)) < 1e-6,
          "冷却截止 = now + 提示音时长 + 余音冷却");
    CHECK(xr_session_cooling(&s, 100.0 + 0.5), "冷却期内应处于 cooling");

    /* 2. 回声护栏: 提示音播完后再多护 echo_guard_s, 防自己的余音被当跟说指令 */
    CHECK(xr_session_echo_guarded(&s, 100.0 + 0.1666666666 + GUARD_S - 0.01),
          "护栏应覆盖 提示音时长 + echo_guard_s");
    CHECK(!xr_session_echo_guarded(&s, 100.0 + 0.1666666666 + GUARD_S + 0.01),
          "护栏到期后应放行");

    /* 3. 无音频: 不播, 且会话状态必须原样(否则白聋一段麦) */
    xr_session_init(&s);
    setup(4000);
    CHECK(xr_session_play_fail_cue(&s, 50.0, &OPS, NULL, 0, COOL_MS, GUARD_S) == 0,
          "音频缺失时不应播放");
    CHECK(played == 0, "音频缺失时不得调 play_cue");
    CHECK(!xr_session_cooling(&s, 50.0), "音频缺失时不得设置冷却");
    CHECK(!xr_session_echo_guarded(&s, 50.0), "音频缺失时不得设置回声护栏");

    /* 4. 播放器起不来: 同样不能动状态 —— 否则播放失败反而把麦聋掉 */
    xr_session_init(&s);
    setup(4000);
    fail_play = 1;
    CHECK(xr_session_play_fail_cue(&s, 60.0, &OPS, cue, cue_n, COOL_MS, GUARD_S) == 0,
          "播放器失败时应返回 0");
    fail_play = 0;
    CHECK(played == 0, "播放器失败时不得记为已播放");
    CHECK(!xr_session_cooling(&s, 60.0), "播放器失败时不得设置冷却");

    /* 5. 重复调用只延长不缩短: 晚到的、now 更小的一次不得把已有冷却改短 */
    xr_session_init(&s);
    setup(4000);
    xr_session_play_fail_cue(&s, 200.0, &OPS, cue, cue_n, COOL_MS, GUARD_S);
    double cool_after_first = xr_session_cool_until(&s);
    xr_session_play_fail_cue(&s, 100.0, &OPS, cue, cue_n, COOL_MS, GUARD_S);
    CHECK(xr_session_cool_until(&s) >= cool_after_first - 1e-9,
          "重复调用不得把已有冷却改短");
    CHECK(xr_session_echo_guard_until(&s) >= 100.0 + 0.1666666666 + GUARD_S - 1e-9,
          "回声护栏同样只延长不缩短");

    /* 6. 冷却必须盖住整段提示音: 否则余音会被 VAD 当成用户开口 */
    xr_session_init(&s);
    setup(RATE * 2);                    /* 2 秒的提示音 */
    xr_session_play_fail_cue(&s, 0.0, &OPS, cue, cue_n, 0, 0);
    CHECK(xr_session_cooling(&s, 1.99), "2s 提示音的第 1.99s 仍在冷却内");
    CHECK(!xr_session_cooling(&s, 2.01), "提示音播完即出冷却");

    fprintf(stderr, failures ? "voice_session_cue_test: %d FAILED\n"
                             : "voice_session_cue_test: OK\n", failures);
    return failures ? 1 : 0;
}