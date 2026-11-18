/* 瑞芯微 rkaudio_vqe 内置唤醒: 读状态文件抓 wakeup 边沿。接口见 xr_vqe_wake.h。 */
#define _POSIX_C_SOURCE 200809L

#include "xr_vqe_wake.h"

#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define VQE_STATUS_FILE_DEFAULT "/tmp/rkaudio_vqe_status"
#define VQE_STATUS_MAX 160

static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static int g_last_wakeup = 0;        /* 上次读到的 wakeup 字段(插件窗口期内会一直是 >0) */
static long long g_last_wake_ms = -1; /* 最近一次唤醒边沿的时间戳 */

static long long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static const char *status_path(void)
{
    const char *p = getenv("XR_VQE_STATUS_FILE");
    return (p && *p) ? p : VQE_STATUS_FILE_DEFAULT;
}

/* 解析 "doa:pitch:wakeup:keyword" 的第 3 个字段(下标 2)。读不到返回 -1。 */
static int read_wakeup_field(int *out)
{
    int fd = open(status_path(), O_RDONLY);
    if (fd < 0) return -1;
    char buf[VQE_STATUS_MAX];
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return -1;
    buf[n] = '\0';
    /* 插件可能写成 "45:10:1:打开灯"(无换行), 也可能带换行/空白 */
    int field = 0;
    char *save = NULL;
    for (char *tok = strtok_r(buf, ":\r\n", &save); tok;
         tok = strtok_r(NULL, ":\r\n", &save)) {
        if (field == 2) {
            *out = atoi(tok);
            return 0;
        }
        field++;
    }
    return -1;
}

int xr_vqe_wake_available(void)
{
    return access(status_path(), R_OK) == 0;
}

int xr_vqe_wake_poll(void)
{
    int cur = 0;
    if (read_wakeup_field(&cur) != 0) return 0;
    int evt = 0;
    pthread_mutex_lock(&g_mu);
    if (cur > 0 && g_last_wakeup == 0) {
        evt = 1;
        g_last_wake_ms = now_ms();
    }
    g_last_wakeup = cur;
    pthread_mutex_unlock(&g_mu);
    return evt;
}

int xr_vqe_wake_recent(int keep_ms)
{
    long long last;
    pthread_mutex_lock(&g_mu);
    last = g_last_wake_ms;
    pthread_mutex_unlock(&g_mu);
    if (last < 0) return 0;
    return (now_ms() - last) <= (long long)keep_ms;
}

void xr_vqe_wake_reset(void)
{
    pthread_mutex_lock(&g_mu);
    /* 只清"最近唤醒"的时间戳; g_last_wakeup 必须留着 —— 插件的检测窗口
     * (默认 3s)还没过期, 清了会让下一次轮询把同一个唤醒再当成新的。 */
    g_last_wake_ms = -1;
    pthread_mutex_unlock(&g_mu);
}
