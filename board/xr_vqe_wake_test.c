/* xr_vqe_wake 的主机单测: 状态解析 + 唤醒边沿检测。
 *
 * 用法: xr_vqe_wake_test [file|shm]
 *   file(默认): 走状态文件降级路径(共享内存名字指向不存在的)
 *   shm:       走共享内存路径(自己建一个 96 字节的 shm)
 * 覆盖:
 *   - 状态文件格式解析(doa:pitch:wakeup:keyword)
 *   - 边沿语义: 只在 0 -> >0 那一刻报一次, 窗口期内(一直是 >0)不重复报
 *   - reset() 不会让同一个唤醒被重复计数(插件窗口还没过期)
 *   - recent()/active(): 句尾收割用
 */
#define _POSIX_C_SOURCE 200809L

#include "xr_vqe_wake.h"

#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
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

/* 插件的 rkaudio_status_t 布局(必须和 xr_vqe_wake.c 里的一致) */
typedef struct {
    int doa;
    int pth;
    int wakeup;
    int command_id;
    float command_score;
    char triggered_keyword[64];
    unsigned int seq;
    unsigned int keyword_seq;
    unsigned int command_seq;
} vqe_status_shm_t;

static vqe_status_shm_t *make_shm(const char *name)
{
    shm_unlink(name);
    int fd = shm_open(name, O_CREAT | O_RDWR, 0600);
    if (fd < 0) { perror("shm_open"); exit(2); }
    if (ftruncate(fd, sizeof(vqe_status_shm_t)) < 0) { perror("ftruncate"); exit(2); }
    void *p = mmap(NULL, sizeof(vqe_status_shm_t), PROT_READ | PROT_WRITE,
                   MAP_SHARED, fd, 0);
    close(fd);
    if (p == MAP_FAILED) { perror("mmap"); exit(2); }
    memset(p, 0, sizeof(vqe_status_shm_t));
    return (vqe_status_shm_t *)p;
}

static void set_wakeup(vqe_status_shm_t *s, int v)
{
    s->wakeup = v;
    s->seq++;
}

int main(int argc, char **argv)
{
    const char *mode = (argc > 1) ? argv[1] : "file";
    char path[] = "/tmp/xr_vqe_wake_test_XXXXXX";
    int fd = mkstemp(path);
    if (fd < 0) { perror("mkstemp"); return 2; }
    close(fd);
    setenv("XR_VQE_STATUS_FILE", path, 1);

    vqe_status_shm_t *shm = NULL;
    if (!strcmp(mode, "shm")) {
        char shmname[64];
        snprintf(shmname, sizeof(shmname), "/xr_vqe_wake_test_%d", (int)getpid());
        shm = make_shm(shmname);
        setenv("XR_VQE_SHM_NAME", shmname, 1);
        printf("模式: 共享内存 (%s)\n", shmname);
    } else {
        setenv("XR_VQE_SHM_NAME", "/xr_vqe_wake_test_no_such_shm", 1);
        printf("模式: 状态文件 (%s)\n", path);
    }

    /* 1) 读不到 -> 不可用, poll 返回 0
     *    (shm 模式下一开始就建好了 shm, 所以只查 poll 不报错) */
    if (!shm) unlink(path);
    CHECK(shm ? 1 : !xr_vqe_wake_available(), "读不到时 available 应为 0");
    CHECK(xr_vqe_wake_poll() == 0, "读不到时 poll 应为 0");

    /* 2) 正常状态 -> 无事件 */
    if (shm) set_wakeup(shm, 0);
    else write_status(path, "-1:-1:0:\n");
    CHECK(xr_vqe_wake_available(), "可读时 available 应为 1");
    CHECK(xr_vqe_wake_poll() == 0, "wakeup=0 时不应有事件");
    CHECK(xr_vqe_wake_recent(1500) == 0, "没有唤醒时 recent 应为 0");
    CHECK(xr_vqe_wake_active() == 0, "没有唤醒时 active 应为 0");

    /* 3) 0 -> 1: 边沿, 报一次 */
    if (shm) set_wakeup(shm, 1);
    else write_status(path, "-1:-1:1:");
    CHECK(xr_vqe_wake_poll() == 1, "0->1 应报一次唤醒");
    CHECK(xr_vqe_wake_recent(1500) == 1, "刚唤醒后 recent 应为 1");
    CHECK(xr_vqe_wake_active() == 1, "插件窗口开着时 active 应为 1");

    /* 4) 窗口期一直是 1: 不重复报 */
    if (shm) set_wakeup(shm, 1);
    else write_status(path, "41:0:1:");
    CHECK(xr_vqe_wake_poll() == 0, "窗口期内不应重复报");
    CHECK(xr_vqe_wake_recent(1500) == 1, "窗口期内 recent 仍为 1");
    CHECK(xr_vqe_wake_active() == 1, "窗口期内 active 仍为 1");

    /* 5) reset() 之后仍在窗口期: 也不能误报(插件还没清零) */
    xr_vqe_wake_reset();
    CHECK(xr_vqe_wake_recent(1500) == 0, "reset 后 recent 应为 0");
    CHECK(xr_vqe_wake_active() == 1, "reset 不该动插件的窗口状态");
    if (shm) set_wakeup(shm, 1);
    else write_status(path, "41:0:1:");
    CHECK(xr_vqe_wake_poll() == 0, "reset 后仍在窗口期不应误报新唤醒");

    /* 6) 过期清 0, 再来一次唤醒: 正常报 */
    if (shm) set_wakeup(shm, 0);
    else write_status(path, "-1:-1:0:");
    CHECK(xr_vqe_wake_poll() == 0, "过期清 0 不应报事件");
    if (shm) set_wakeup(shm, 1);
    else write_status(path, "-1:-1:1:");
    CHECK(xr_vqe_wake_poll() == 1, "过期后再次唤醒应报一次");

    /* 7) 格式容错(只有文件路径有意义) */
    if (!shm) {
        write_status(path, "-1:-1:0:");
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
    }

    if (g_fail) {
        fprintf(stderr, "xr_vqe_wake_test[%s]: %d 项失败\n", mode, g_fail);
        return 1;
    }
    printf("xr_vqe_wake_test[%s]: OK\n", mode);
    return 0;
}
