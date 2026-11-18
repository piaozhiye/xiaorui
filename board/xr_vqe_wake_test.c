/* xr_vqe_wake 的主机单测: 状态文件解析 + 唤醒边沿检测。
 *
 * 用法: 编译后跑 ./xr_vqe_wake_test
 * 覆盖:
 *   - 状态文件格式解析(doa:pitch:wakeup:keyword)
 *   - 边沿语义: 只在 0 -> >0 那一刻报一次, 窗口期内(一直是 >0)不重复报
 *   - reset() 不会让同一个唤醒被重复计数(插件窗口还没过期)
 *   - recent(): 句尾收割用
 */
#define _POSIX_C_SOURCE 200809L

#include "xr_vqe_wake.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int g_fail = 0;

#define CHECK(cond, msg)                                                   \
    do {                                                                   \
        if (!(cond)) {                                                     \
            fprintf(stderr, "FAIL: %s (%s:%d)\n", msg, __FILE__, __LINE__); \
            g_fail++;                                                      \
        }                                                                  \
    } while (0)

static void write_status(const char *path, const char *content)
{
    FILE *f = fopen(path, "w");
    if (!f) { perror("fopen"); exit(2); }
    fputs(content, f);
    fclose(f);
}

int main(void)
{
    char path[] = "/tmp/xr_vqe_wake_test_XXXXXX";
    int fd = mkstemp(path);
    if (fd < 0) { perror("mkstemp"); return 2; }
    close(fd);
    setenv("XR_VQE_STATUS_FILE", path, 1);

    /* 1) 文件不存在 -> 不可用, poll 返回 0 */
    unlink(path);
    CHECK(!xr_vqe_wake_available(), "文件不存在时 available 应为 0");
    CHECK(xr_vqe_wake_poll() == 0, "文件不存在时 poll 应为 0");

    /* 2) 正常状态 -> 无事件 */
    write_status(path, "-1:-1:0:\n");
    CHECK(xr_vqe_wake_available(), "文件存在时 available 应为 1");
    CHECK(xr_vqe_wake_poll() == 0, "wakeup=0 时不应有事件");
    CHECK(xr_vqe_wake_recent(1500) == 0, "没有唤醒时 recent 应为 0");

    /* 3) 0 -> 1: 边沿, 报一次 */
    write_status(path, "-1:-1:1:");
    CHECK(xr_vqe_wake_poll() == 1, "0->1 应报一次唤醒");
    CHECK(xr_vqe_wake_recent(1500) == 1, "刚唤醒后 recent 应为 1");

    /* 4) 窗口期一直是 1: 不重复报 */
    write_status(path, "41:0:1:");
    CHECK(xr_vqe_wake_poll() == 0, "窗口期内不应重复报");
    CHECK(xr_vqe_wake_recent(1500) == 1, "窗口期内 recent 仍为 1");

    /* 5) reset() 之后仍在窗口期: 也不能误报(插件还没清零) */
    xr_vqe_wake_reset();
    CHECK(xr_vqe_wake_recent(1500) == 0, "reset 后 recent 应为 0");
    write_status(path, "41:0:1:");
    CHECK(xr_vqe_wake_poll() == 0, "reset 后仍在窗口期不应误报新唤醒");

    /* 6) 过期清 0, 再来一次唤醒: 正常报 */
    write_status(path, "-1:-1:0:");
    CHECK(xr_vqe_wake_poll() == 0, "过期清 0 不应报事件");
    write_status(path, "-1:-1:1:");
    CHECK(xr_vqe_wake_poll() == 1, "过期后再次唤醒应报一次");

    /* 7) 格式容错: 无换行 / 带关键词 / 字段缺失 */
    write_status(path, "-1:-1:0:");          /* 先清零, 下面才是新边沿 */
    CHECK(xr_vqe_wake_poll() == 0, "清零不应报事件");
    write_status(path, "45:10:1:打开灯");     /* 带关键词 */
    CHECK(xr_vqe_wake_poll() == 1, "带关键词的格式应正常报唤醒");
    write_status(path, "-1:-1:0:");
    CHECK(xr_vqe_wake_poll() == 0, "清零不应报事件");
    write_status(path, "45:10:1:");          /* 无关键词 */
    CHECK(xr_vqe_wake_poll() == 1, "无关键词但 wakeup=1 应报事件");
    write_status(path, "garbage");
    CHECK(xr_vqe_wake_poll() == 0, "非法格式不应崩溃也不应报事件");
    write_status(path, "");
    CHECK(xr_vqe_wake_poll() == 0, "空文件不应报事件");

    unlink(path);
    if (g_fail) {
        fprintf(stderr, "xr_vqe_wake_test: %d 项失败\n", g_fail);
        return 1;
    }
    printf("xr_vqe_wake_test: OK\n");
    return 0;
}
