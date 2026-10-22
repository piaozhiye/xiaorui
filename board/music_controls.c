#define _POSIX_C_SOURCE 200809L
#include "music_controls.h"
#include "xr_wake.h"
#include "xr_audio_format.h"
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

enum { PRE = RATE * 8 / 10, CAP = RATE * 6 };
static pthread_t thread;
static atomic_int stop_requested;
static atomic_int capturing;
static int started;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static xr_music_command_t pending;
static xr_music_control_ops_t callbacks;
static void *capture;
static size_t window_samples;

/* ---------- 现场调试转储 (XR_UTT_DUMP=<目录>, 与主循环同门控同目录) ----------
 * ctrl-raw.pcm   : 控制线程每帧原始采集(16k/mono/S16), 滚动 ~120s。
 *   音乐播放期主循环不读麦——主循环的 raw.pcm 只有空闲期内容; 而 KWS 漏检时
 *   连捕获句都不存在, 这份原始帧是"mic 到底收到了什么"的唯一证据
 *   (2026-10-21 停止腿满音量漏检定位补)。
 * ctrl-utt-NNN.wav: 控制窗捕获的句子(recognize 前)。
 * 不设变量时零开销、不建文件。 */
#define CTRL_DUMP_RAW_MAX_FRAMES 1200L   /* 120s @100ms */
static FILE *g_ctrl_raw;
static const char *g_ctrl_dir;
static long g_ctrl_raw_frames;
static unsigned g_ctrl_utt_seq;

static void ctrl_raw_rotate(void) {
    if (g_ctrl_raw) { fclose(g_ctrl_raw); g_ctrl_raw = NULL; }
    char p[512];
    snprintf(p, sizeof(p), "%s/ctrl-raw.pcm", g_ctrl_dir);
    g_ctrl_raw = fopen(p, "wb");
    g_ctrl_raw_frames = 0;
    if (!g_ctrl_raw) {
        fprintf(stderr, "[music] 调试: 无法写 %s: %s; 本线程转储关闭\n", p, strerror(errno));
        g_ctrl_dir = NULL;
    }
}
static void ctrl_raw_frame(const int16_t *pcm, size_t n) {
    if (!g_ctrl_dir || n == 0) return;
    if (!g_ctrl_raw || g_ctrl_raw_frames >= CTRL_DUMP_RAW_MAX_FRAMES) ctrl_raw_rotate();
    if (!g_ctrl_raw) return;
    if (fwrite(pcm, sizeof(int16_t), n, g_ctrl_raw) != n || fflush(g_ctrl_raw)) {
        fprintf(stderr, "[music] 调试: 原始帧写入失败; 本线程转储关闭\n");
        fclose(g_ctrl_raw); g_ctrl_raw = NULL; g_ctrl_dir = NULL;
        return;
    }
    g_ctrl_raw_frames++;
}
static void ctrl_dump_wav(const int16_t *pcm, size_t n) {
    if (!g_ctrl_dir || n == 0) return;
    char p[512];
    snprintf(p, sizeof(p), "%s/ctrl-utt-%03u.wav", g_ctrl_dir, ++g_ctrl_utt_seq);
    FILE *f = fopen(p, "wb");
    if (!f) { fprintf(stderr, "[music] 调试: 无法写 %s: %s\n", p, strerror(errno)); return; }
    uint32_t data = (uint32_t)(n * 2), riff = data + 36;
    uint32_t fmt_size = 16, byte_rate = (uint32_t)RATE * 2;
    uint16_t fmt_pcm = 1, mono = 1, block_align = 2, bits = 16;
    fwrite("RIFF", 1, 4, f); fwrite(&riff, 4, 1, f); fwrite("WAVEfmt ", 1, 8, f);
    fwrite(&fmt_size, 4, 1, f);
    /* fmt 块逐字段落盘: blockAlign 是 2 字节, 整写 4 字节会头部错位
       (主循环 dump_wav 同款教训, 见 xiaorui_live.c) */
    fwrite(&fmt_pcm, 2, 1, f); fwrite(&mono, 2, 1, f);
    fwrite(&(uint32_t){(uint32_t)RATE}, 4, 1, f);
    fwrite(&byte_rate, 4, 1, f);
    fwrite(&block_align, 2, 1, f); fwrite(&bits, 2, 1, f);
    fwrite("data", 1, 4, f); fwrite(&data, 4, 1, f);
    fwrite(pcm, 2, n, f);
    fclose(f);
    fprintf(stderr, "[music] 调试: 控制句转储 %s (%.2fs)\n", p, (double)n / RATE);
}

/* 唤醒词可能进两遍: 800ms 预滚环一份 + KWS 命中帧起捕获窗又一份
 * (2026-10-15 实机: ASR 转写"小瑞小瑞小瑞小瑞停止播放")。迭代剥到没有
 * 唤醒单元为止, 最多 4 轮防病态输入。返回是否剥到过至少一个。 */
static int strip_all_wake(const char *text, char *out, size_t cap) {
    snprintf(out, cap, "%s", text);
    int stripped = 0;
    for (int guard = 0; guard < 4; guard++) {
        char next[512];
        int single = 0;
        int action = xr_parse_wake_text(out, 0, next, sizeof(next));
        if (!action) action = xr_parse_wake_kws(out, next, sizeof(next), &single);
        if (action == 1) {  /* Pure wake, including a final repeated wake unit. */
            out[0] = 0;
            return 1;
        }
        if (action != 2) break;
        memmove(out, next, strlen(next) + 1);
        stripped = 1;
    }
    return stripped;
}

/* 剥掉唤醒词, 只留正文。剥不出(实机"音量小一点"整句无唤醒词)时按全文判。
   返回 1 = 有正文, 0 = 纯唤醒无正文。 */
static int wake_tail(const char *text, char *out, size_t cap) {
    if (!text) { out[0] = 0; return 0; }
    if (!strip_all_wake(text, out, cap))
        snprintf(out, cap, "%s", text);
    return out[0] != 0;
}

int xr_music_command_parse(const char *text, xr_music_command_t *command) {
    if (!command) return 0;
    memset(command, 0, sizeof(*command));
    if (!text) return 0;
    char tail[512];
    /* KWS 是必需的声学门。其唤醒音节可能掉出 ASR 预滚(实机"音量小一点"
       整句无唤醒词), 所以一个唤醒单元都剥不到时, 全文按严格控制句再判。 */
    if (!wake_tail(text, tail, sizeof(tail))) return 0;  /* 纯唤醒, 无指令 */
    if (xr_volume_parse(tail, &command->volume_op, &command->value)) {
        command->action = XR_MUSIC_VOLUME;
        return 1;
    }
    if (!strcmp(tail, "停止播放") || !strcmp(tail, "停止音乐") ||
        !strcmp(tail, "暂停音乐") || !strcmp(tail, "别放了") ||
        !strcmp(tail, "停下") || !strcmp(tail, "别说了") ||
        !strcmp(tail, "闭嘴")) {
        command->action = XR_MUSIC_STOP;
        return 1;
    }
    return 0;
}

static void *monitor(void *unused) {
    (void)unused;
    int16_t frame[FRAME_SAMPLES], prefix[PRE], utterance[CAP];
    size_t pre_n = 0, total = 0, remaining = 0;
    callbacks.kws_reset();
    g_ctrl_dir = getenv("XR_UTT_DUMP");
    if (g_ctrl_dir && *g_ctrl_dir) ctrl_raw_rotate();
    else g_ctrl_dir = NULL;
    while (!atomic_load(&stop_requested)) {
        long count = callbacks.read(capture, frame, FRAME_SAMPLES);
        if (count <= 0 || count > FRAME_SAMPLES) {
            struct timespec pause = {0, 20000000}; nanosleep(&pause, NULL);
            continue;
        }
        size_t n = (size_t)count;
        ctrl_raw_frame(frame, n);
        if (!remaining) {
            if (callbacks.kws_feed(frame, (int)n)) {
                memcpy(utterance, prefix, pre_n * sizeof(int16_t));
                memcpy(utterance + pre_n, frame, n * sizeof(int16_t));
                total = pre_n + n;
                remaining = window_samples;
                atomic_store(&capturing, 1);
                fprintf(stderr, "[music] KWS命中, 收集控制指令\n");
            }
            if (pre_n + n > PRE) {
                size_t drop = pre_n + n - PRE;
                memmove(prefix, prefix + drop, (pre_n - drop) * sizeof(int16_t));
                pre_n -= drop;
            }
            memcpy(prefix + pre_n, frame, n * sizeof(int16_t));
            pre_n += n;
            continue;
        }
        size_t take = n < remaining ? n : remaining;
        if (total + take > CAP) break;
        memcpy(utterance + total, frame, take * sizeof(int16_t));
        total += take; remaining -= take;
        if (remaining) continue;
        if (atomic_load(&stop_requested)) break;
        char text[512] = {0};
        ctrl_dump_wav(utterance, total);
        if (callbacks.recognize(utterance, total, text, sizeof(text)) > 0) {
            xr_music_command_t command;
            if (xr_music_command_parse(text, &command)) {
                pthread_mutex_lock(&lock);
                if (pending.action == XR_MUSIC_NONE) pending = command;
                pthread_mutex_unlock(&lock);
                fprintf(stderr, "[music] 控制识别: %s\n", text);
            } else {
                /* 不是控制句 -> 不许黑洞: 剥掉唤醒词后交回主循环当普通指令
                   (开灯/报时/问天气... 走正常意图链)。只是纯唤醒则忽略。 */
                char tail[256];
                if (wake_tail(text, tail, sizeof(tail))) {
                    pthread_mutex_lock(&lock);
                    if (pending.action == XR_MUSIC_NONE) {
                        pending.action = XR_MUSIC_FORWARD;
                        snprintf(pending.text, sizeof(pending.text), "%s", tail);
                    }
                    pthread_mutex_unlock(&lock);
                    fprintf(stderr, "[music] 转交普通指令: %s\n", tail);
                } else {
                    fprintf(stderr, "[music] 非控制语音忽略: %.100s\n", text);
                }
            }
        }
        pre_n = 0;
        callbacks.kws_reset();
        atomic_store(&capturing, 0);
    }
    return NULL;
}

int xr_music_controls_start(const xr_music_control_ops_t *ops, void *context, int window_ms) {
    if (started) return 0;
    if (!ops || !ops->read || !ops->kws_feed || !ops->kws_reset || !ops->recognize ||
        window_ms < 1000 || window_ms > 5000) return -EINVAL;
    callbacks = *ops; capture = context;
    window_samples = (size_t)window_ms * RATE / 1000;
    memset(&pending, 0, sizeof(pending));
    atomic_store(&stop_requested, 0);
    atomic_store(&capturing, 0);
    int rc = pthread_create(&thread, NULL, monitor, NULL);
    if (rc) return -rc;
    started = 1;
    return 0;
}

void xr_music_controls_stop(void) {
    if (!started) return;
    atomic_store(&stop_requested, 1);
    pthread_join(thread, NULL);
    if (g_ctrl_raw) { fclose(g_ctrl_raw); g_ctrl_raw = NULL; }
    started = 0;
    atomic_store(&capturing, 0);
    memset(&pending, 0, sizeof(pending));
}

int xr_music_controls_poll(xr_music_command_t *command) {
    if (!command) return 0;
    pthread_mutex_lock(&lock);
    *command = pending;
    memset(&pending, 0, sizeof(pending));
    pthread_mutex_unlock(&lock);
    return command->action != XR_MUSIC_NONE;
}

int xr_music_controls_capturing(void) {
    return atomic_load(&capturing);
}

int xr_music_controls_running(void) {
    return started;
}
