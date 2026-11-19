/* 瑞芯微 rkaudio_vqe 内置唤醒: 读唤醒状态抓 wakeup 边沿。接口见 xr_vqe_wake.h。 */
#define _POSIX_C_SOURCE 200809L

#include "xr_vqe_wake.h"

#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define VQE_STATUS_FILE_DEFAULT "/tmp/rkaudio_vqe_status"
#define VQE_SHM_NAME_DEFAULT "/rkaudio_vqe_status"
#define VQE_STATUS_MAX 160

/* 插件的公开状态结构(rkaudio_status_t, 见 pcm_rkaudio_vqe.h)。
 * 布局是插件的 ABI, 96 字节 —— 用静态断言钉住, 变了编译期就炸。 */
typedef struct {
    int doa;                  /* 0   DOA 方位角 (0-360, -1=无效) */
    int pth;                  /* 4   DOA 俯仰角 */
    int wakeup;               /* 8   唤醒状态 (0=无, >0=唤醒 ID) */
    int command_id;           /* 12 */
    float command_score;      /* 16 */
    char triggered_keyword[64];/* 20 */
    unsigned int seq;         /* 84  每次更新自增 */
    unsigned int keyword_seq;  /* 88 */
    unsigned int command_seq;  /* 92 */
} vqe_status_shm_t;

_Static_assert(sizeof(vqe_status_shm_t) == 96, "rkaudio_status_t 布局变了(应 96 字节)");

static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static int g_last_wakeup = 0;         /* 上次读到的 wakeup 字段 */
static long long g_last_wake_ms = -1;  /* 最近一次唤醒边沿的时间戳 */
static void *g_shm = NULL;            /* 共享内存映射(读) */
static int g_shm_tried = 0;           /* 试过一次就记着, 别反复失败 */

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

static const char *shm_name(void)
{
    const char *p = getenv("XR_VQE_SHM_NAME");
    return (p && *p) ? p : VQE_SHM_NAME_DEFAULT;
}

/* 映射插件的共享内存(懒加载)。插件在打开采集设备时创建它。 */
static const vqe_status_shm_t *shm_map(void)
{
    if (g_shm_tried) return (const vqe_status_shm_t *)g_shm;
    g_shm_tried = 1;
    int fd = shm_open(shm_name(), O_RDONLY, 0);
    if (fd < 0) return NULL;
    void *p = mmap(NULL, sizeof(vqe_status_shm_t), PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (p == MAP_FAILED) return NULL;
    g_shm = p;
    return (const vqe_status_shm_t *)p;
}

/* 状态文件读法(共享内存不可用时的降级路径)。
 * 注意: 插件写状态文件用 fopen("w")(会截断), 正好在写入瞬间读可能拿到
 * 空/半截内容 -> 这就是为什么优先走共享内存。 */
static int read_status_file(int *out)
{
    int fd = open(status_path(), O_RDONLY);
    if (fd < 0) return -1;
    char buf[VQE_STATUS_MAX];
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return -1;
    buf[n] = '\0';
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

/* 读当前 wakeup 值; 读不到返回 -1。 */
static int read_wakeup_value(void)
{
    const vqe_status_shm_t *s = shm_map();
    if (s) return s->wakeup;
    int v = 0;
    if (read_status_file(&v) == 0) return v;
    return -1;
}

int xr_vqe_wake_available(void)
{
    if (shm_map()) return 1;
    return access(status_path(), R_OK) == 0;
}

int xr_vqe_wake_poll(void)
{
    int cur = read_wakeup_value();
    if (cur < 0) return 0;
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

int xr_vqe_wake_active(void)
{
    int active;
    pthread_mutex_lock(&g_mu);
    active = (g_last_wakeup > 0);
    pthread_mutex_unlock(&g_mu);
    return active;
}

void xr_vqe_wake_reset(void)
{
    pthread_mutex_lock(&g_mu);
    /* 只清"最近唤醒"的时间戳; g_last_wakeup 必须留着 —— 插件的检测窗口
     * (默认 3s)还没过期, 清了会让下一次轮询把同一个唤醒再当成新的。 */
    g_last_wake_ms = -1;
    pthread_mutex_unlock(&g_mu);
}
