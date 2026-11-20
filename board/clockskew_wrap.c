/* 墙钟回拨注入(Codex review P2 回归, 2026-11-20):
 * --wrap=clock_gettime 把 CLOCK_REALTIME 读数前移 1 小时, 内核
 * (pthread_cond_timedwait)的等待时钟不变 —— 等价于"构造绝对 deadline
 * 后墙钟回拨"。播放线程的等待必须全走 CLOCK_MONOTONIC 才不受影响;
 * 仍按 REALTIME 构造 deadline 的等待会被拖满注入的偏移量, 尾巴排空
 * 测试在 assert(flushed) 处失败(fail-first 已在旧代码上复现)。 */
#define _POSIX_C_SOURCE 200809L
#include <time.h>

int __real_clock_gettime(clockid_t, struct timespec *);
int __wrap_clock_gettime(clockid_t clock, struct timespec *ts) {
    int rc = __real_clock_gettime(clock, ts);
    if (!rc && clock == CLOCK_REALTIME) ts->tv_sec += 3600;
    return rc;
}
