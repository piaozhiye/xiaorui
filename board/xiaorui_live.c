/* xiaorui_live -- 小瑞常驻语音前端 (单进程, 零文件, 数据流架构).
 *
 *   ALSA readi 直采 (16k, 100ms 帧, dlopen 板载 libasound, abi 组合已验证)
 *     -> 帧级能量 VAD (自适应噪声门 + 3 帧预滚防掐头, 说完 0.3s 即停)
 *     -> 常驻 KWS + 离线 RKNN paraformer ASR (dlopen libsherpa-onnx-c-api.so)
 *     -> listen detect+text 推服务端 (LLM+TTS)
 *     -> 收 opus 回答流式解码 (24k mono) -> snd_pcm_writei 直写 xr_spk 节点
 *        (asound.conf: plug -> hw:1,0 @48k/2ch, 全部重采样归 ALSA)
 *
 * 对话半双工；可选 USB 控制线程只收播放期的唤醒+音量/停止，不上送对话.
 * 唤醒窗口: 应答后 30s 内免唤醒直接说. ws 断开自动重连; Ctrl-C 退出.
 *
 * 用法(板上):
 *   LD_LIBRARY_PATH=/data/rknn-test/sherpa/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib:/usr/lib \
 *   /data/xiaorui-server/board-scripts/xiaorui_live [mic设备,默认asr_mic]
 */
#define _POSIX_C_SOURCE 200809L
#include <arpa/inet.h>
#include <errno.h>
#include <dirent.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <sys/wait.h>
extern long syscall(long, ...);   /* -std=c11 下 glibc 不声明, reboot 子进程 sync 用 */
#include <pthread.h>
#include <time.h>
#include <unistd.h>
#include "opus.h"              /* 静态链自建 opus 1.4 -- 板上系统 libopus 的
                                  opus_encoder_create 返回 NULL(err=0), 静态库正常 */
#include "xr_wake.h"
#include "xr_vqe_wake.h"
#include "audio_player.h"
#include "audio_capture.h"
#include "cap_accum.h"
#include "xr_mic4.h"
#include "xr_volume.h"
#include <fcntl.h>
#include <errno.h>
#include "xr_audio_format.h"
#include "speech_pipeline.h"
#include "voice_session.h"
#include "xr_vad.h"
#include "xr_fifo_pcm.h"

#define WS_PORT 8000

/* ---------- Backend (Step1 解耦: WS 细节在 backend_local.c) ---------- */
#include "backend.h"
#include "runtime_config.h"
#include "music_controls.h"
#include "speech_route.h"
#include "playback_control.h"
#include "audio_cue.h"
#include "ui_server.h"
#include "ui_level.h"

/* 提示音: ack=唤醒确认("哎我在请说"), hmm=指令应答("嗯", 350ms, 预留未用),
   fail=弃轮反馈("没听清"), 让干等一轮的用户知道设备还在、可以重说。 */
static int16_t *ack_pcm; static size_t ack_n;
static int16_t *hmm_pcm; static size_t hmm_n;
static int16_t *fail_pcm; static size_t fail_n;
static int16_t *vol_pcm;  static size_t vol_n;   /* 音量确认"好的" */
/* 软件音量当前值(百分比): 解析命中时更新+落盘+下发播放线程 */
static int g_vol_pct = 100;
static int g_tts_gain_pct = 100;
/* 停止播报(2026-10-16): 本地消化, 覆盖窗内本地文本路径喊停 -- 该路径
 * 原会把"停止播放"当指令发给 LLM(LLM 停不了播放, 实测窗内打断失效)。
 * 音频直传/音乐轮由控制线程硬断, 这里补第三条路。 */
static int is_stop_speech_command(const char *cmd) {
    static const char *words[] = {"停止播放", "停止音乐", "暂停音乐", "别放了",
                                  "停下", "别说了", "闭嘴", "停止", NULL};
    if (!cmd) return 0;
    size_t n = strlen(cmd);
    if (n >= 128) return 0;
    char buf[128];
    memcpy(buf, cmd, n + 1);
    for (size_t l = strlen(buf); l > 0; l--) {
        unsigned char c = (unsigned char)buf[l - 1];
        if (c == ' ' || c == ',' || c == '.' || c == '!' || c == '?'
            || c == '\t' || c == '\r' || c == '\n') { buf[l - 1] = 0; continue; }
        if (l >= 3) {
            unsigned char b2 = (unsigned char)buf[l - 2], b3 = (unsigned char)buf[l - 3];
            if ((c == 0x82 || c == 0x83 || c == 0x81 || c == 0x84 || c == 0x85)
                && (b2 & 0xc0) == 0x80 && (b3 & 0xc0) == 0x80) { buf[l - 3] = 0; continue; }
        }
        break;
    }
    for (int i = 0; words[i]; i++)
        if (!strcmp(buf, words[i])) return 1;
    return 0;
}

/* 语音重启(2026-10-16): 剥标点后精确等值 -- "帮我重启一下手机"类长句
 * 不受影响。开发板远程救活手段, 须搭配已持久化的自启链(S99xrvoice)。 */
static int is_reboot_command(const char *cmd) {
    static const char *words[] = {"重启", "重启一下", "重新启动", "你重启一下",
                                  "重启吧", "重启系统", NULL};
    if (!cmd) return 0;
    size_t n = strlen(cmd);
    if (n >= 128) return 0;   /* 重启短语必短, 长指令直接放行 */
    char buf[128];
    memcpy(buf, cmd, n + 1);
    for (size_t l = strlen(buf); l > 0; l--) {
        unsigned char c = (unsigned char)buf[l - 1];
        if (c == ' ' || c == ',' || c == '.' || c == '!' || c == '?'
            || c == '\t' || c == '\r' || c == '\n') { buf[l - 1] = 0; continue; }
        if (l >= 3) {
            unsigned char b2 = (unsigned char)buf[l - 2], b3 = (unsigned char)buf[l - 3];
            if ((c == 0x82 || c == 0x83 || c == 0x81 || c == 0x84 || c == 0x85)
                && (b2 & 0xc0) == 0x80 && (b3 & 0xc0) == 0x80) { buf[l - 3] = 0; continue; }
        }
        break;
    }
    for (int i = 0; words[i]; i++)
        if (!strcmp(buf, words[i])) return 1;
    return 0;
}
static const char *volume_state_path(void) {
    const char *p = getenv("XR_VOLUME_FILE");
    return (p && *p) ? p : "/data/xiaorui-server/xiaorui_volume";
}

static const char *tts_gain_state_path(void) {
    const char *path = getenv("XR_TTS_GAIN_STATE");
    return (path && *path) ? path : "/data/xiaorui-server/xiaorui_tts_gain";
}

typedef struct {
    xr_ui_server_t *server;
    backend_t *backend;
    xr_session_t *session;
    xr_vad_t *vad;
    const xr_runtime_config_t *config;
    int was_responding;
    int content_seen;
    int level;
    int abort_failed;
    const char *socket_path;   /* XR_UI_SOCKET */
    double retry_at;           /* 下次重试打开 socket 的时刻(秒) */
} xr_ui_context_t;

static double now_s(void);
static void xr_tick(void);
static void ui_socket_retry(xr_ui_context_t *context);
/* 前端进程启动时刻(CLOCK_MONOTONIC 秒): 语音栈预热期用它算。 */
static double g_started_at = 0.0;
/* 网关已经发得出 TTS(语音真的能用了): 预热提示据此提前结束。 */
static int g_tts_ready = 0;
/* 预热期长度(秒): 网关 + RockASR/RockTTS 模型加载到 NPU 实测 30~60s,
 * 慢机器更久。收到网关业务事件(TTS/STT/静默回执)会提前结束准备提示;
 * 这个值只是异常兜底(网关一直没业务事件时别让用户永远看着"准备中")。
 * 可用 XR_PREPARING_SECONDS 现场调。 */
static double preparing_seconds(void) {
    const char *env = getenv("XR_PREPARING_SECONDS");
    if (env && *env) {
        double value = atof(env);
        if (value >= 0) return value;
    }
    return 180.0;
}

static int service_ui(xr_ui_context_t *context) {
    if (!context->server) {
        ui_socket_retry(context);   /* 打开失败就定时重试, 别等下次 ws 重连 */
        return 0;
    }
    int responding = context->session->state == XR_SESSION_RESPOND;
    if (responding && !context->was_responding)
        context->content_seen = 0;   /* 新一轮: 等第一段内容到了再清屏 */
    context->was_responding = responding;
    enum xr_ui_state state = responding ?
        (context->session->pkt_n ? XR_UI_SPEAKING : XR_UI_THINKING) :
        (context->vad->in_utt || xr_session_in_window(context->session, now_s()) ?
         XR_UI_LISTENING : XR_UI_READY);
    /* ★ 语音栈预热期(2026-11-20 真板反馈: 点桌面图标重启后, 界面写着
     *   "我在听，请说", 但网关/模型还在加载, 用户说了没反应 -> 以为坏了)。
     *   前端启动后这段时间一律报"正在准备语音", 界面如实说;
     *   一旦网关真的发得出 TTS(问候音播了), 立刻切回正常状态 ——
     *   比死等固定窗口准(慢机器 60 秒可能还没好, 快机器早就能说了)。 */
    if (!responding && !g_tts_ready) {
        /* 没真就绪就绝不报 READY("等你说一句")—— 用户说了没反应。
         * 预热窗口内报 PREPARING; 超时报 VOICE_ERROR(明确的异常态),
         * 让用户知道是服务没起来, 而不是傻等一句永远没人应的唤醒词。 */
        if (now_s() - g_started_at < preparing_seconds())
            state = XR_UI_PREPARING;
        else
            state = XR_UI_VOICE_ERROR;
    }
    xr_ui_server_state(context->server, state, (unsigned int)g_vol_pct,
                       (unsigned int)g_tts_gain_pct);
    unsigned int volume = 0;
    int command = xr_ui_server_poll(context->server, &volume);
    if (command == XR_UI_VOLUME && context->config->volume_control) {
        g_vol_pct = xr_volume_next(g_vol_pct, XR_VOL_SET, (int)volume);
        xr_player_set_volume(g_vol_pct);
        xr_volume_save(volume_state_path(), g_vol_pct);
        fprintf(stderr, "[ui] volume=%d%%\n", g_vol_pct);
    }
    if (command == XR_UI_TTS_GAIN) {
        g_tts_gain_pct = (int)volume;
        xr_player_set_voice_gain(g_tts_gain_pct);
        xr_volume_save(tts_gain_state_path(), g_tts_gain_pct);
        fprintf(stderr, "[ui] tts_gain=%d%%\n", g_tts_gain_pct);
    }
    if (command == XR_UI_STOP && !responding)
        fprintf(stderr, "[ui] stop 忽略(当前不在回答)\n");
    return command == XR_UI_STOP && responding;
}

/* 本轮第一段内容(问句或回答首句)到达: 清掉上一轮的字幕, 一轮只清一次。
   以前是"进入 RESPOND 就清", 但那种轮次可能被网关当噪声丢掉(房间里
   随便一声就能触发), 清了就把用户刚问的和刚答的全擦掉。 */
static void ui_turn_content(xr_ui_context_t *context) {
    if (context->content_seen || !context->server) return;
    context->content_seen = 1;
    xr_ui_server_turn_begin(context->server);
}

/* 新一轮开始: UI 清屏门复位。
 * ★ 2026-11-20 review: 原来靠 service_ui 观察到 RESPOND 的"边沿"清零
 *   (responding && !was_responding), 但边沿只在"上一轮不在 RESPOND"时才
 *   出现 —— 连续轮/打断场景(上一轮还在说话就开新一轮)边沿不出现,
 *   content_seen 留着上一轮的 1 -> ui_turn_content 空操作 -> 新一轮字幕
 *   追加到上一轮字幕后面, 旧字幕永不清除(answer 缓冲 4096 字节满了之后
 *   后面整轮字幕全丢)。所以复位必须跟"轮开始"走, 不能靠观察边沿。 */
static void ui_turn_reset(xr_ui_context_t *context) {
    context->content_seen = 0;
}

/* UI socket 打开失败后的定时重试(2026-11-20 review, Important 4)。
 * 之前只在 ws 重连时再试一次 —— ws 可能几小时不重连, 于是:
 *   - UI 一直连不上(界面停在"等待语音服务连接");
 *   - 更糟: 另一个**活着的**旧实例占着 socket(设计如此, 不抢活端点),
 *     UI 会被那个旧实例回答, 显示陈旧状态, 而横幅还写着"语音已连接"。
 * 所以改成每 5s 重试一次, 并在日志里点名路径与失败原因。 */
static void ui_socket_retry(xr_ui_context_t *context) {
    if (!context->socket_path || !*context->socket_path) return;
    double now = now_s();
    if (context->retry_at && now < context->retry_at) return;
    context->retry_at = now + 5.0;
    context->server = xr_ui_server_open(context->socket_path);
    if (context->server) {
        fprintf(stderr, "[ui] socket 打开成功: %s\n", context->socket_path);
        context->retry_at = 0;
    } else {
        fprintf(stderr, "[ui] socket 仍不可用(%s): %s, 5s 后重试\n",
                context->socket_path, strerror(errno));
    }
}

static int service_ui_drain(void *opaque) {
    xr_ui_context_t *context = opaque;
    xr_tick();
    if (!service_ui(context)) return 0;
    context->abort_failed = be_abort(context->backend) != 0;
    fprintf(stderr, "[ui] stop during drain\n");
    return 1;
}
static int cue_load(const char *path, int16_t **out_pcm, size_t *out_n, const char *tag) {
    int rc = xr_cue_load(path, out_pcm, out_n);
    if (rc) { fprintf(stderr, "[xr] %s cue rejected: %s (%d)\n", tag, path, rc); return rc; }
    fprintf(stderr, "[xr] %s loaded (%zu samples @24k)\n", tag, *out_n);
    return 0;
}
static int ack_load(const char *path) {
    int rc = cue_load(path, &ack_pcm, &ack_n, "ack");
    /* 唤醒确认音靠近用户且容易被感知为突兀；单独做 PCM 预衰减，仍
       叠加用户总音量，不碰 codec/mixer，也不改变回答和音乐的电平。 */
    if (!rc) xr_volume_apply(ack_pcm, ack_n, 60);
    return rc;
}

static long mic_read_raw(snd_pcm_t *mic, int16_t *out16k, int frames16k);
static int g_fifo_fd;   /* 真实定义见下方; 这里先声明给 fifo 感知的读适配用 */

static long music_read(void *context, int16_t *pcm, size_t samples) {
    if (g_fifo_fd >= 0)   /* fifo 模式没有 snd_pcm 句柄, 走外部供流读 */
        return mic_read_raw(NULL, pcm, (int)samples);
    long n = xr_capture_read(context, pcm, samples);
    if (n == -EPIPE) { xr_capture_prepare(context); return 0; }
    return n;
}
/* 唤醒源(2026-11-19 定): 默认 sherpa-onnx zipformer KWS。
 * 瑞芯微 VQE 内置唤醒的接入代码都留着(board/xr_vqe_wake.c, 读
 * /dev/shm/rkaudio_vqe_status 的 wakeup 边沿), 但**默认不开**:
 * 实测它在本板这套采集参数下 0/10 触发(6mic 波束 + 16ms 周期, VQE 每帧
 * 要 16~19ms, 唤醒 FSM 拿不到连续帧), 而 sherpa KWS 命中 124 次。
 * 想试 VQE: XR_WAKE_SOURCE=vqe。
 * 真正的唤醒判定权在**网关侧**(RockASR 转写 + 唤醒词门), 前端这个只是
 * 声学证据, 用来开跟说窗和提前起采集。 */
static int wake_use_vqe(void)
{
    static int cached = -1;
    if (cached >= 0) return cached;
    const char *s = getenv("XR_WAKE_SOURCE");
    if (s && !strcmp(s, "vqe")) {
        /* VQE 插件在打开采集设备时写共享内存; 万一还没写出来, 等最多 3s */
        for (int i = 0; i < 30 && !xr_vqe_wake_available(); i++) {
            struct timespec ts = { .tv_sec = 0, .tv_nsec = 100 * 1000 * 1000 };
            nanosleep(&ts, NULL);
        }
        cached = xr_vqe_wake_available();
        fprintf(stderr, "[xr] 唤醒源: %s\n",
                cached ? "rkaudio_vqe 内置" : "sherpa KWS (VQE 状态不可用)");
        return cached;
    }
    cached = 0;
    fprintf(stderr, "[xr] 唤醒源: sherpa KWS%s\n",
            (s && !strcmp(s, "sherpa")) ? " (XR_WAKE_SOURCE=sherpa)" : " (默认)");
    return cached;
}
static int wake_available(void)
{
    return wake_use_vqe() ? 1 : xr_speech_kws_available();
}
/* VQE 的唤醒是"状态边沿"不是"帧内命中", 所以不需要 PCM 参数 */
static int wake_poll(const int16_t *pcm, int samples)
{
    if (wake_use_vqe()) return xr_vqe_wake_poll();
    return xr_speech_kws_feed(pcm, samples);
}
/* 句尾收割: VQE 侧用插件自己的检测窗口状态(唤醒词那一句结束时窗口通常
   还没过期), 比按时间猜准 —— 前端要等 1.2~1.6s 静音才判"说完"。 */
static int wake_flush(void)
{
    if (wake_use_vqe()) return xr_vqe_wake_active();
    return xr_speech_kws_flush();
}
/* RESPOND 等待服务端判定期间积压的音频(3s @100ms/帧)。
   VQE 硬件缓冲只有 ~400ms(见 xr_vad 注释), 等待期不采音就会 overrun,
   prepare 丢掉积压 -> "喊完立刻说指令"那句整段没了。
   攒着, 等回执到了补喂 VAD(比当前帧更早, 所以要先喂)。 */
#define PENDING_MAX_FRAMES 30
/* 小步采音的步长: 一个 VQE 周期(256 样本 = 16ms) */
#define PENDING_STEP_SAMPLES 256
static int16_t g_pending[PENDING_MAX_FRAMES][FRAME_SAMPLES];
static int g_pending_n;
/* 采集累加器: 拆成 cap_accum 模块(带回归测试)。
 * 2026-11-19 板端 rc=139 x2: 旧内嵌实现 n=1536 时再写 256 写穿 384 字节,
 * 砸中 BSS 紧邻全局(g_cap_acc_n/g_pending_n/g_original_argv/看门狗心跳),
 * 每轮回答期必炸。契约见 cap_accum.h。 */
static cap_accum_t g_cap_acc;

static long cap_read_adapter(void *dev, int16_t *dst, unsigned long want)
{
    if (g_fifo_fd >= 0)   /* fifo 模式: mic 句柄为 NULL, 不能直接 readi */
        return mic_read_raw(NULL, dst, (int)want);
    return xr_capture_read(dev, dst, want);
}

static int cap_pull(snd_pcm_t *mic)
{
    return cap_accum_pull(&g_cap_acc, cap_read_adapter, mic,
                          PENDING_STEP_SAMPLES);
}

static int cap_frame_ready(void) { return cap_accum_ready(&g_cap_acc); }

static void cap_frame_take(int16_t *out)
{
    cap_accum_take(&g_cap_acc, out);
}

/* 声学临时窗口: KWS 命中时开, 等服务端文本确认。
   命中前��� awake_until 记在这里, 服务端回 nowake/噪声时恢复回去 ——
   否则 KWS 误触留下的窗口还在, 屋里下一句随口话就能免唤醒进命令链。
   -1 = 没有待确认的声学窗口。 */
static double g_awake_before_acoustic = -1.0;

static void wake_reset(void)
{
    if (wake_use_vqe()) xr_vqe_wake_reset();
    else xr_speech_kws_reset();
    g_awake_before_acoustic = -1.0;   /* 声学临时窗口作废 */
}
/* 播放期控制线程用的适配(ops 表要的是 (pcm, samples) 签名) */
static int wake_feed(const int16_t *pcm, int samples)
{
    return wake_poll(pcm, samples);
}

static const xr_music_control_ops_t MUSIC_OPS = {
    music_read, wake_feed, wake_reset, xr_speech_recognize
};

/* 弃轮提示音的播放器 ops: 起播线程 + 推入 PCM。策略(冷却/护栏多长)在
   voice_session, 这里只提供最薄的一层播放能力, 便于主机确定性测试。
   不设 finish_cue —— 与纯唤醒 ack 一致, 让播放线程后台播, 主循环继续读麦,
   靠冷却把这段窗口聋掉即可, 不必阻塞 join。 */
static int cue_play(const int16_t *pcm, size_t n) {
    if (xr_player_start()) return -1;
    xr_player_push_pcm(pcm, n);
    return 0;
}
static const xr_cue_ops_t CUE_OPS = { .play_cue = cue_play, .finish_cue = NULL };

/* ---------- 状态机 ---------- */

/* XR_MIC_MODE=es8388(阶段2): 48k/4ch 交织 → ch[XR_ES8388_MIC_CH] →
 * 抗混叠 ÷3 → 16k/mono, 下游 KWS/VAD/ASR 接口不变. 默认 usb(asr_mic 16k/1ch)
 * 行为完全不变. 未接 mic 通道不求平均不送下游; ref(ch2/3)不混入. */
static int g_mic4, g_mic_ch = 0, g_hw_ch = 4;
/* XR_MIC=fifo:<路径>: 由外部进程(如 arecord 走 VQE 插件)供流的原始 PCM
 * (16k/mono/S16)。进程隔离避免 sherpa(onnx)与 rkaudio(MNN)同进程的
 * 堆损坏(实测 setsid 后台形态必崩, arecord 单独跑稳定)。 */
static int g_fifo_fd = -1;
/* rkaudio_vqe 节点专用读取策略(实测): 插件按 16ms(256帧@16k)节律处理,
 * 单次 readi 1600 会触发 wrong in_size 并最终堆损坏; 256 帧读取 60s 全稳,
 * drop+prepare 安全, 仅 close 析构有 free invalid bug → 永不 close, 保句柄. */
static int g_vqe_mic;
static xr_deci3_t g_deci;
/* 容量=整读(frames16k*3)+余数(<3)+裕量: 部分读后余数留下, 下一次整读
   永远放得下。老容量恰为整读, 余数 1-2 时整读 take=0 且余数常驻,
   麦克风从此无声(评审反例)。 */
static int16_t g_m48[FRAME_SAMPLES * 3 + 8];
static int g_m48_n;

/* ---------- 现场调试转储 (XR_UTT_DUMP=<目录>) ----------
 * raw.pcm  : 每帧原始采集(16k/mono/S16), 滚动保留最近 ~120s
 * utt-NNN.wav: 交给云端之前的那一句(编码/上传前)
 * 两者同源, 用来判断转写失真发生在采集合句, 还是 Opus/网络/解码一侧。
 * 不设变量时不建文件、分支一次即返回。 */
static FILE *g_dump_raw;
static const char *g_dump_dir;
static long g_dump_raw_frames;
#define XR_DUMP_RAW_MAX_FRAMES 1200L   /* 120s @100ms */
static void dump_raw_rotate(void) {
    if (!g_dump_dir) return;
    if (g_dump_raw) fclose(g_dump_raw);
    char p[512];
    snprintf(p, sizeof(p), "%s/raw.pcm", g_dump_dir);
    g_dump_raw = fopen(p, "wb");
    g_dump_raw_frames = 0;
    if (!g_dump_raw) fprintf(stderr, "[xr] 调试: 无法写 %s: %s\n", p, strerror(errno));
}
static void dump_raw_frame(const int16_t *pcm, long n) {
    if (!g_dump_raw) return;
    if (n <= 0) return;
    if (g_dump_raw_frames >= XR_DUMP_RAW_MAX_FRAMES) dump_raw_rotate();
    if (!g_dump_raw) return;
    fwrite(pcm, sizeof(int16_t), (size_t)n, g_dump_raw);
    fflush(g_dump_raw);
    g_dump_raw_frames++;
}
static void dump_wav(const char *path, const int16_t *pcm, size_t n, int rate) {
    FILE *f = fopen(path, "wb");
    if (!f) { fprintf(stderr, "[xr] 调试: 无法写 %s: %s\n", path, strerror(errno)); return; }
    uint32_t data = (uint32_t)(n * 2), riff = data + 36;
    uint32_t fmt_size = 16, byte_rate = (uint32_t)rate * 2;
    uint16_t fmt_pcm = 1, mono = 1, block_align = 2, bits = 16;
    fwrite("RIFF", 1, 4, f); fwrite(&riff, 4, 1, f); fwrite("WAVEfmt ", 1, 8, f);
    fwrite(&fmt_size, 4, 1, f);
    /* fmt 块必须逐字段落盘: blockAlign 是 2 字节, 曾误按 4 字节写导致
       头部错位、wave 模块报 "bad sample width" (2026-10-21 调试发现) */
    fwrite(&fmt_pcm, 2, 1, f); fwrite(&mono, 2, 1, f);
    fwrite(&(uint32_t){(uint32_t)rate}, 4, 1, f);
    fwrite(&byte_rate, 4, 1, f);
    fwrite(&block_align, 2, 1, f); fwrite(&bits, 2, 1, f);
    fwrite("data", 1, 4, f); fwrite(&data, 4, 1, f);
    fwrite(pcm, 2, n, f);
    fclose(f);
    fprintf(stderr, "[xr] 调试: 句子上传前转储 %s (%.2fs)\n", path, (double)n / rate);
}
/* 本轮播放期是否有别的线程读过麦(播放期语音控制线程)。
 *
 * 2026-10-21 板上实测(3576): 音乐轮结束后前端出现 WS 重连 + 麦克风重开
 * (日志: "[be-cloud] 已连接 / [xr] connected / [xr] mic=... 聆听中" 三连),
 * 根因是收尾分支里的 snd_pcm_drop + snd_pcm_prepare: VQE 插件经不起
 * drop/prepare(内部流会被掐死), prepare 失败 -> goto restart -> 整个前端
 * 重连重开 —— 用户观感就是"播放的时候设备被切了一刀"。
 *
 * 播放期控制线程一直在读麦(它自己那个 read 循环), 采集缓冲早被读走,
 * 没有"自家播音积压"要清; 这时再 drop/prepare 纯属自伤。控制线程没跑过
 * (XR_MUSIC_CONTROLS=0, 如 RK3308)时缓冲里确实可能积着播音, 维持原 drop+prepare。
 */
static int g_mic_drained_by_control;

/* 采集流收尾: 返回 -1 表示需要整进程重启(仅 USB 路径可能)。
 * 什么都不做也是合法收尾(VQE + 控制线程读过麦的情形)。 */
static int capture_finish_turn(snd_pcm_t *mic) {
    if (!mic) return 0;
    g_pending_n = 0;         /* 上一轮积压的帧作废, 别喂进下一轮 */
    cap_accum_init(&g_cap_acc); /* 采集累加器里的半帧也作废 */
    if (g_mic_drained_by_control) {   /* 缓冲已被读走: 不动流 */
        g_mic_drained_by_control = 0;
        return 0;
    }
    xr_capture_drop(mic);
    if (xr_capture_prepare(mic) < 0) return -1;
    return 0;
}

/* 软件数字增益(PDM 麦 + 6mic 波束后电平偏低, 板载 809 的 MIC Boost 管不到
 * PDM 通路, VQE 里也没有独立的数字增益口, 所以在软件里补一级)。
 * XR_MIC_GAIN 是线性倍数: 1.0=关(默认), 2.0=+6dB, 4.0=+12dB。
 * 饱和削顶会引入失真, 所以按样本计数上报, 不做静默截断。 */
static double mic_gain_factor(void)
{
    static double g = -1.0;
    if (g < 0.0) {
        const char *s = getenv("XR_MIC_GAIN");
        g = (s && *s) ? atof(s) : 1.0;
        if (g <= 0.0) g = 1.0;
        if (g != 1.0) fprintf(stderr, "[xr] 麦克风数字增益: %.2fx\n", g);
    }
    return g;
}
static void mic_apply_gain(int16_t *pcm, long frames)
{
    double g = mic_gain_factor();
    if (g == 1.0) return;
    long clipped = 0;
    for (long i = 0; i < frames; i++) {
        double v = (double)pcm[i] * g;
        if (v > 32767.0) { v = 32767.0; clipped++; }
        else if (v < -32768.0) { v = -32768.0; clipped++; }
        pcm[i] = (int16_t)v;
    }
    if (clipped) fprintf(stderr, "[xr] 增益饱和削顶 %ld 样本\n", clipped);
}
static long mic_read_raw(snd_pcm_t *mic, int16_t *out16k, int frames16k) {
    if (g_fifo_fd >= 0) {
        static int fifo_eof_seen;
        int was_eof = fifo_eof_seen;
        long n = xr_fifo_read_s16(g_fifo_fd, out16k, (size_t)frames16k,
                                  &fifo_eof_seen);
        if (n < 0) { perror("[xr] fifo read"); return -1; }
        if (!was_eof && fifo_eof_seen)
            fprintf(stderr, "[xr] fifo EOF(供给进程退出)，静音等待写端恢复，不重启播放栈\n");
        return n;
    }
    if (g_vqe_mic) {
        /* xr_vqe_16k = ALSA plug(48k→16k 由 plug 层完成), 前端 16k/1ch 直读。
         * ★ 自适应步长: 按 VQE 实际开的 period 读。
         *   RK809 板 period=256: 一次读 100ms 会等不到 -> EPIPE(222 次),
         *     小步(256)能修; ES8388 板 period=1600: 小步(256)反而崩
         *     (前端静默退出), 大步(一次读完)没问题。
         *   所以按 period 大小选: <=512 用小步攒帧, >512 直接一次读完。 */
        static int vqe_small_step = -1;
        if (vqe_small_step < 0) {
            const char *e = getenv("XR_VQE_SMALL_STEP");
            vqe_small_step = (e && atoi(e) > 0) ? 1 : 0;
            fprintf(stderr, "[xr] VQE 采集: %s\n",
                    vqe_small_step ? "小步读" : "大步读");
        }
        if (vqe_small_step) {
            while (!cap_frame_ready()) {
                int r = cap_pull(mic);
                if (r < 0) {
                    xr_capture_prepare(mic);
                    memset(out16k, 0, (size_t)frames16k * 2);
                    return 0;
                }
            }
            cap_frame_take(out16k);
            return frames16k;
        }
        /* period 大的板: 直接一次读完(原来的做法, ES8388 实测稳定) */
        long got = 0;
        while (got < frames16k) {
            long r = xr_capture_read(mic, (char *)out16k + got * 2,
                                     (unsigned long)(frames16k - got));
            if (r < 0) {
                xr_capture_prepare(mic);
                memset(out16k, 0, (size_t)frames16k * 2);
                return 0;
            }
            got += r;
        }
        return got;
    }
    if (!g_mic4) return xr_capture_read(mic, out16k, (unsigned long)frames16k);
    static int16_t raw4[FRAME_SAMPLES * 3 * 4];
    long n = xr_capture_read(mic, raw4, (unsigned long)frames16k * 3);
    if (n < 0) {   /* overrun 等错误: 恢复设备+重置抽取器状态, 本帧静默 */
        xr_capture_prepare(mic);
        xr_deci3_reset(&g_deci);
        g_m48_n = 0;
        memset(out16k, 0, (size_t)frames16k * sizeof(int16_t));
        return 0;
    }
    if (n == 0) return 0;
    /* 拼进 48k mono 缓冲(支持部分读取)。防御分支重置续流而非丢新数据:
       丢新数据会让余数永久卡住(见 g_m48 容量注释), 重置只损失相位对齐一瞬 */
    int take = (int)n;
    if (g_m48_n + take > (int)(sizeof(g_m48) / sizeof(g_m48[0]))) {
        xr_deci3_reset(&g_deci);
        g_m48_n = 0;
    }
    if (xr_mic4_pick(raw4, take, g_hw_ch, g_mic_ch, g_m48 + g_m48_n) < 0) return -1;
    g_m48_n += take;
    int useable = g_m48_n - g_m48_n % 3;
    int got = xr_deci3_run(&g_deci, g_m48, useable, out16k);
    if (got < 0) return -1;
    if (got > frames16k) got = frames16k;      /* 理论不超, 保护 */
    g_m48_n -= useable;
    if (g_m48_n > 0) memmove(g_m48, g_m48 + useable, (size_t)g_m48_n * sizeof(int16_t));
    return got;
}

/* FIFO 采集开法: O_NONBLOCK 打开(写端未就绪也立即返回, 重连循环不挂死),
 * 开成后清 NONBLOCK 恢复阻塞读。写端退出时 read 返 0(EOF) → mic_read
 * 返 -1 → 主循环按麦克风错误走 restart, 在那里重开等供给回来。 */
static long mic_read(snd_pcm_t *mic, int16_t *out16k, int frames16k)
{
    long n = mic_read_raw(mic, out16k, frames16k);
    if (n > 0) mic_apply_gain(out16k, n);
    return n;
}

static int fifo_open_nb(const char *path) {
    int fd = open(path, O_RDONLY | O_NONBLOCK);
    if (fd < 0) return -1;
    int fl = fcntl(fd, F_GETFL, 0);
    if (fl >= 0) fcntl(fd, F_SETFL, fl & ~O_NONBLOCK);
    return fd;
}

static volatile sig_atomic_t got_sig = 0;
static void on_sig(int s) { (void)s; got_sig = 1; }
#ifdef XR_CRASH_BT
#include <execinfo.h>
static void on_crash(int s) {
    /* 纯 syscall 级: fprintf/backtrace 都可能再进 malloc 二次崩 */
    static const char msg[] = "[xr] CRASH bt:";
    (void)write(2, msg, sizeof(msg) - 1);
    void *bt[32];
    int n = backtrace(bt, 32);
    for (int i = 0; i < n; i++) {
        unsigned long v = (unsigned long)bt[i];
        char hex[24]; int k = 0;
        char buf[2] = {' ', 0};
        hex[k++] = ' ';
        for (int sh = 60; sh >= 0; sh -= 4) {
            int d = (int)((v >> sh) & 0xf);
            hex[k++] = d < 10 ? '0' + d : 'a' + d - 10;
        }
        hex[k] = 0;
        (void)write(2, hex, k);
        (void)buf;
    }
    (void)write(2, "\n", 1);
    _exit(128 + s);
}
#endif
static double now_s(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
}
static int transcribe_followup(const int16_t *pcm, size_t n, char *out, size_t cap) {
    return xr_speech_recognize(pcm, n, out, cap);
}

/* ---------- 看门狗: 主循环卡死自救 ----------
 * deaf-mode 现象(2026-10-16): 双腿 CLOSE_WAIT、九分钟无响应。ALSA/RKNN 内联
 * 卡死是待验证假说，不是唯一已证实根因；本看门狗只为无推进提供恢复兜底。
 * 正常一次迭代 <= 数秒(RESPOND poll 1s / LISTEN 100ms / 精修典型 0.57s),
 * 超过阈值即视为卡死: 与服务端语音重启(EV_REBOOT)同一自救语义 -- sync+reboot,
 * 自启链拉回全栈。XR_WATCHDOG_S=0 关闭。 */
static atomic_ulong g_loop_tick_ms;
static atomic_int g_reboot_started;
static long long g_health_generation;
static char **g_original_argv;
static void xr_reexec_frontend(void) {
    /* exec replaces all stuck threads. Mark inherited ALSA/socket/model FDs
     * close-on-exec, preserving only stdio; no fork of the audio runtime. */
    DIR *fds = opendir("/proc/self/fd");
    if (fds) {
        struct dirent *entry;
        while ((entry = readdir(fds)) != NULL) {
            char *end;
            long fd = strtol(entry->d_name, &end, 10);
            if (*end || fd <= 2) continue;
            int flags = fcntl((int)fd, F_GETFD);
            if (flags >= 0) (void)fcntl((int)fd, F_SETFD, flags | FD_CLOEXEC);
        }
        closedir(fds);
        if (g_original_argv) execv("/proc/self/exe", g_original_argv);
    }
    fprintf(stderr, "[xr] frontend re-exec failed: %s\n", strerror(errno));
    _exit(75);
}
/* A stuck sync/reboot must not strand the watchdog forever. Both reboot
 * sources share one gate; failure re-execs the frontend with clean FDs. */
static void xr_request_reboot(void) {
    int expected = 0;
    if (!atomic_compare_exchange_strong(&g_reboot_started, &expected, 1)) return;
    pid_t child = fork();
    if (child == 0) {
        /* 只做 async-signal-safe: sync(2)+绝对路径 execl。不走 /bin/sh+PATH --
         * PATH 缺 /sbin 时 reboot 查找不得(exit 127), 机器永远不重启。板上
         * /sbin/reboot -> busybox 符号链接。 */
        if (setsid() < 0) _exit(126);
        syscall(SYS_sync);   /* -std=c11 下 sync() 无声明, 裸系统调用 */
        execl("/sbin/reboot", "reboot", (char *)NULL);
        execl("/bin/busybox", "busybox", "reboot", (char *)NULL);
        _exit(127);
    }
    if (child > 0) {
        for (int i = 0; i < 15; ++i) {
            int status;
            pid_t rc = waitpid(child, &status, WNOHANG);
            if (rc == child) {
                fprintf(stderr, "[xr] reboot command exited status=%d; re-exec frontend\n", status);
                xr_reexec_frontend();
            }
            if (rc < 0 && errno != EINTR) break;
            sleep(1);
        }
        if (kill(-child, SIGKILL) < 0 && errno == ESRCH) (void)kill(child, SIGKILL);
    }
    fprintf(stderr, "[xr] reboot unavailable/timed out; re-exec frontend\n");
    xr_reexec_frontend();
}
static long long wd_now_ms(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}
static void xr_tick(void) { atomic_store(&g_loop_tick_ms, (unsigned long)wd_now_ms()); }
static void *xr_watchdog(void *arg) {
    (void)arg;
    long stall_s = 120;
    const char *env = getenv("XR_WATCHDOG_S");
    if (env) {
        char *end; errno = 0;
        long parsed = strtol(env, &end, 10);
        if (!errno && end != env && !*end && (parsed == 0 || (parsed >= 30 && parsed <= 3600)))
            stall_s = parsed;
        else fprintf(stderr, "[xr] invalid XR_WATCHDOG_S, using 120\n");
    }
    fprintf(stderr, "[xr] watchdog armed (%lds 无推进 -> sync+reboot)\n", stall_s);
    for (;;) {
        if (got_sig) return NULL;
        unsigned long tick = atomic_load(&g_loop_tick_ms);
        /* Atomic snapshot for soak: a living PID is not proof of loop progress. */
        char path[96], temporary[104];
        snprintf(path, sizeof(path), "/tmp/xr-live-health-%ld", (long)getpid());
        snprintf(temporary, sizeof(temporary), "%s.tmp", path);
        FILE *health = fopen(temporary, "w");
        if (health) {
            int written = fprintf(health, "%lu %lld\n", tick, g_health_generation);
            int closed = fclose(health);
            if (written > 0 && closed == 0) {
                if (rename(temporary, path) < 0) (void)unlink(temporary);
            } else (void)unlink(temporary);
        }
        long long stalled = wd_now_ms() - (long long)tick;
        if (stall_s > 0 && tick && stalled > stall_s * 1000) {
            fprintf(stderr, "[xr] 看门狗: 主循环 %.0fs 无推进, sync+reboot 自救\n",
                    stalled / 1000.0);
            fflush(stderr);
            xr_request_reboot();
        }
        struct timespec ts = {10, 0}; nanosleep(&ts, NULL);
    }
    return NULL;
}
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wpragmas"
/* ---- Snapcast 音乐: FIFO -> 播放环 ----------------------------------------
 * 音乐由板上的 snapclient 解码后写进这个 FIFO(24kHz 单声道 s16le),
 * 前端把它推进自己的播放环, 这样:
 *   - 声卡只有前端一个主人(不存在 snapclient 抢 hw / mixer)
 *   - 语音提示可以和音乐在同一个环里按时间交织
 * 用 XR_MUSIC_FIFO=<路径> 打开; 默认不开, 保持原行为。
 *
 * 注意: snapclient 用 fopen(path,"wb") 打开出口, FIFO 没有读者时它会卡住,
 * 所以本线程必须在客户端启动**之前**就在跑(开机顺序)。
 */
static void *xr_music_fifo_thread(void *arg) {
    const char *path = (const char *)arg;
    int16_t buf[2880];
    int fd = -1;
    int announced = 0;
    int music_started = 0;
    int have_signal = 0;
    int writer_waiting = 0;
    struct timespec last_signal = {0};
    /* XR_MUSIC_TRACE=1: 每次 read 打一行累计字节+本次字节, 用来量供流节奏。
       板内核没开 /proc/<pid>/io, 只能在这里埋。默认关闭不影响行为。 */
    const int trace = getenv("XR_MUSIC_TRACE") != NULL;
    size_t trace_total = 0;
    unsigned reconnects = 0;
    /* Correlate FIFO lifecycle with audio_player's XR_PLAY_TRACE monotonic
       timestamps.  In particular, a FIFO EOF can leave PCM already queued in
       the frontend ring/ALSA device; record that exact queue before changing
       session state so a reconnect phase jump is measurable, not guessed. */
    #define MUSIC_TRACE_EVENT(event_name, nbytes) do { \
        if (trace) { \
            struct timespec event_ts; \
            clock_gettime(CLOCK_MONOTONIC, &event_ts); \
            fprintf(stderr, "[xr] music_fifo_event %ld.%06ld %s reconnect=%u bytes=%zu ring_samples=%zu\n", \
                    (long)event_ts.tv_sec, (long)(event_ts.tv_nsec / 1000), \
                    (event_name), reconnects, (size_t)(nbytes), xr_player_music_queued_samples()); \
            fflush(stderr); \
        } \
    } while (0)
    for (;;) {
        if (fd < 0) {
            /* 用 open+read 而不是 fopen+fread: fread 会**阻塞到读满请求的条数**
             * (snapclient 每 ~50ms 才来一股, 凑 120ms 会让 80ms 的 ALSA 缓冲干转,
             * 实测持续 underrun)。read() 是"有多少读多少"。
             * open(O_RDONLY) 在 FIFO 上会等写端, 与客户端的 fopen("wb") 对称。 */
            /* Nonblocking open lets us observe the controller's active marker
             * while Snapclient briefly closes/reopens its FIFO writer (stream
             * handoff/reconnect). A blocking open plus a fixed sleep made that
             * transient look like the end of a music session. */
            fd = open(path, O_RDONLY | O_NONBLOCK);
            if (fd < 0) {
                struct timespec retry = {.tv_nsec = 50000000};
                nanosleep(&retry, NULL);
                continue;
            }
            if (reconnects > 0) MUSIC_TRACE_EVENT("writer_reopened", trace_total);
            if (!announced) {
                fprintf(stderr, "[xr] music fifo: %s\n", path);
                announced = 1;
            }
        }
        ssize_t n = read(fd, buf, sizeof(buf));
        if (n <= 0) {
            if (n < 0 && errno == EINTR) continue;
            if (n == 0) {
                if (!writer_waiting) {
                    MUSIC_TRACE_EVENT("writer_eof", trace_total);
                    reconnects++;
                    writer_waiting = 1;
                }
            } else if (errno != EAGAIN && errno != EWOULDBLOCK) {
                MUSIC_TRACE_EVENT("read_error", trace_total);
                close(fd); fd = -1;
                reconnects++;
            }
            /* FIFO writer absent is a normal short gap. Keep the playback
             * session alive while the activity marker (or recent nonzero
             * samples) says music is still active; expire it only after the
             * same quiet grace used for streams without a local marker. */
            const char *active_path = getenv("XR_MUSIC_ACTIVE_FILE");
            if (!active_path || !*active_path)
                active_path = "/data/xiaorui/snapcast/music.active";
            struct stat active_stat;
            time_t wall_now = time(NULL);
            int active = stat(active_path, &active_stat) == 0
                         && active_stat.st_mtime >= wall_now - 2
                         && active_stat.st_mtime <= wall_now + 2;
            if (have_signal) {
                struct timespec mono_now;
                clock_gettime(CLOCK_MONOTONIC, &mono_now);
                long quiet_ms = (long)(mono_now.tv_sec - last_signal.tv_sec) * 1000L
                              + (mono_now.tv_nsec - last_signal.tv_nsec) / 1000000L;
                if (quiet_ms >= 0 && quiet_ms <= 3000) active = 1;
            }
            if (active && !music_started) {
                MUSIC_TRACE_EVENT("music_session_start", trace_total);
                xr_player_set_music(1);
                if (xr_player_start() == 0) music_started = 1;
                else {
                    fprintf(stderr, "[xr] music fifo: 播放环起不来\n");
                    xr_player_set_music(0);
                }
            } else if (!active && music_started) {
                MUSIC_TRACE_EVENT("music_session_idle", trace_total);
                xr_player_set_music(0);
                music_started = 0;
                have_signal = 0;
            }
            struct timespec retry = {.tv_nsec = 20000000};
            nanosleep(&retry, NULL);
            continue;
        }
        writer_waiting = 0;
        /* Snapclient 的 file player 在空闲流期间也会持续输出静音 PCM，因此不能
         * 把 FIFO 收到字节当作“正在播放”。本机音乐控制器的短时 music.active 心跳
         * 优先；其他客户端没有这个本地文件时，用非零 PCM + 3s 静音宽限识别音频。
         */
        const char *active_path = getenv("XR_MUSIC_ACTIVE_FILE");
        if (!active_path || !*active_path) active_path = "/data/xiaorui/snapcast/music.active";
        struct stat active_stat;
        time_t now = time(NULL);
        int active = stat(active_path, &active_stat) == 0
                     && active_stat.st_mtime >= now - 2
                     && active_stat.st_mtime <= now + 2;
        size_t samples = (size_t)n / sizeof(buf[0]);
        int chunk_signal = 0;
        for (size_t i = 0; i < samples; i++) {
            if (buf[i] != 0) {
                chunk_signal = 1;
                break;
            }
        }
        if (chunk_signal) {
            have_signal = 1;
            clock_gettime(CLOCK_MONOTONIC, &last_signal);
        }
        if (have_signal) {
            struct timespec mono_now;
            clock_gettime(CLOCK_MONOTONIC, &mono_now);
            long quiet_ms = (long)(mono_now.tv_sec - last_signal.tv_sec) * 1000L
                          + (mono_now.tv_nsec - last_signal.tv_nsec) / 1000000L;
            if (quiet_ms >= 0 && quiet_ms <= 3000) active = 1;
        }
        if (active && !music_started) {
            MUSIC_TRACE_EVENT("music_session_start", trace_total);
            xr_player_set_music(1);
            if (xr_player_start() != 0) {
                fprintf(stderr, "[xr] music fifo: 播放环起不来\n");
                xr_player_set_music(0);
                close(fd); fd = -1; sleep(1); continue;
            }
            music_started = 1;
        } else if (!active && music_started) {
            MUSIC_TRACE_EVENT("music_session_idle", trace_total);
            xr_player_set_music(0);
            music_started = 0;
        }
        if (!active) {
            continue;
        }
        trace_total += (size_t)n;
        if (trace) {
            struct timespec tnow;
            clock_gettime(CLOCK_MONOTONIC, &tnow);
            fprintf(stderr, "[xr] fifo_read %ld.%06ld total=%zu bytes=%zd ring_samples=%zu\n",
                    (long)tnow.tv_sec, (long)(tnow.tv_nsec / 1000),
                    trace_total, n, xr_player_music_queued_samples());
        }
        xr_player_push_music_pcm(buf, samples);
    }
    #undef MUSIC_TRACE_EVENT
    return NULL;
}

/* 测试钩子(默认关): XR_VOICE_INJECT=<24k/mono/s16le 文件> 时周期注入"语音"
 * (走正常语音 push 路径), 用来在无 ASR/TTS 后端时验证"音乐让位 -> 语音 -> 音乐恢复"。
 *   XR_VOICE_INJECT_COUNT(默认 100)、XR_VOICE_INJECT_GAP_MS(默认 1500)。
 * 每次: 从头读该 PCM, 按实时节奏 push(前面显式 voice_begin), 排空后 voice_end。 */
static void *xr_voice_inject_thread(void *arg) {
    (void)arg;
    const char *path = getenv("XR_VOICE_INJECT");
    if (!path || !*path) return NULL;
    int count = 100, gap_ms = 1500;
    if (getenv("XR_VOICE_INJECT_COUNT")) count = atoi(getenv("XR_VOICE_INJECT_COUNT"));
    if (getenv("XR_VOICE_INJECT_GAP_MS")) gap_ms = atoi(getenv("XR_VOICE_INJECT_GAP_MS"));
    FILE *f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "[xr] voice_inject: 打不开 %s\n", path); return NULL; }
    int16_t pcm[2400];
    fprintf(stderr, "[xr] voice_inject: %s x%d gap=%dms\n", path, count, gap_ms);
    sleep(8);   /* 等音乐 FIFO 先打开并置 music_active, 避免第一轮和音乐启动抢环 */
    for (int i = 0; i < count; i++) {
        struct timespec d = {.tv_nsec = (long)gap_ms * 1000000L};
        while (d.tv_nsec >= 1000000000L) { d.tv_sec++; d.tv_nsec -= 1000000000L; }
        nanosleep(&d, NULL);
        if (fseek(f, 0, SEEK_SET) != 0) { }
        xr_player_start();                 /* 音乐在位: 复用线程/环 */
        xr_player_voice_begin();           /* 显式让位(回合级) */
        size_t total = 0, n;
        while ((n = fread(pcm, sizeof(int16_t), 2400, f)) > 0) {
            xr_player_push_pcm(pcm, n);    /* 语音 PCM 入环(让位已显式开启) */
            total += n;
            struct timespec px = {.tv_nsec = (long)(n / 24) * 1000000L};  /* 24k 实时节流 */
            nanosleep(&px, NULL);
        }
        for (int w = 0; w < 200 && xr_player_queued_samples() > 0; w++) {
            struct timespec px = {.tv_nsec = 20000000L};
            nanosleep(&px, NULL);
        }
        xr_player_voice_end();
        fprintf(stderr, "[xr] voice_inject %d/%d %zu samples\n", i + 1, count, total);
    }
    fclose(f);
    return NULL;
}

int main(int argc, char **argv) {
    g_started_at = now_s();   /* 预热期起点(见 preparing_seconds) */
    g_original_argv = argv;
    g_health_generation = wd_now_ms();
    const xr_runtime_config_t *config = xr_runtime_config();
    if (xr_runtime_config_validate(config, getenv("XR_BACKEND"))) return 2;
    g_dump_dir = getenv("XR_UTT_DUMP");
    if (g_dump_dir && *g_dump_dir) {
        dump_raw_rotate();
        fprintf(stderr, "[xr] 调试转储: %s/{raw.pcm, utt-NNN.wav}\n", g_dump_dir);
    }
    const char *dev = argc > 1 ? argv[1] : (getenv("XR_MIC") ? getenv("XR_MIC") : "asr_mic");
    if (!strncmp(dev, "fifo:", 5)) {
        g_fifo_fd = fifo_open_nb(dev + 5);
        if (g_fifo_fd < 0) { fprintf(stderr, "[xr] fifo %s: %s\n", dev + 5, strerror(errno)); return 2; }
        fprintf(stderr, "[xr] 采集=fifo %s (16k/mono/S16, 外部供给)\n", dev + 5);
    } else if (getenv("XR_MIC_MODE") && !strcmp(getenv("XR_MIC_MODE"), "es8388")) {
        /* 阶段2 采集适配(显式失败, 不静默回退) */
        if (!getenv("XR_MIC") && argc <= 1) dev = "hw:rockchipes8388,0";
        g_hw_ch = getenv("XR_ES8388_CHANNELS") ? atoi(getenv("XR_ES8388_CHANNELS")) : 4;
        g_mic_ch = getenv("XR_ES8388_MIC_CH") ? atoi(getenv("XR_ES8388_MIC_CH")) : 0;
        if (!xr_es8388_params_ok(g_hw_ch, g_mic_ch)) {
            fprintf(stderr, "[xr] es8388 参数非法: ch=%d mic=%d (支持 2ch(板载 ES8388 原生)"
                            " 或 4ch(SAI 回环), mic 取值 0..ch-1)\n",
                    g_hw_ch, g_mic_ch);
            return 2;
        }
        g_mic4 = 1;
        xr_deci3_init(&g_deci);
        fprintf(stderr, "[xr] 采集=es8388 %s 48k/%dch, mic=ch%d(其余不送), ÷3→16k\n",
                dev, g_hw_ch, g_mic_ch);
    }
#ifdef XR_CRASH_BT
    signal(SIGSEGV, on_crash); signal(SIGABRT, on_crash);
#endif
    signal(SIGINT, on_sig); signal(SIGTERM, on_sig);
    signal(SIGPIPE, SIG_IGN);   /* ws 对端关闭时 write 触发 SIGPIPE 会杀进程 */
    setvbuf(stdout, NULL, _IOLBF, 0);

    const xr_speech_config_t speech_config = {
        .local_asr = config->local_asr,
        .kws_dir = getenv("XR_KWS_DIR"),
        .library_path = getenv("XR_SHERPA_LIB"),
    };
    if (xr_audio_load() || xr_speech_init_config(NULL, &speech_config)) return 2;
    if (!config->local_asr && config->music_controls)
        fprintf(stderr, "[xr] playback voice controls disabled: local ASR is off\n");
    /* KWS 与离线识别器由 speech_pipeline 内部初始化: KWS 失败回退能量门(非致命),
       离线失败仅日志。流式 ASR 已移除(2026-10-11 定案): KWS 接管唤醒后草稿职责架空,
       单遍 20s RKNN 为唯一识别器 */

    /* encoder: 静态链的 opus 1.4(板上系统库 encoder 坏); 上行音频(cloud 模式)依赖它 */
    int oe = 0;
    OpusEncoder *enc = opus_encoder_create(RATE, 1, OPUS_APPLICATION_VOIP, &oe);
    if (enc) {
        opus_encoder_ctl(enc, OPUS_SET_BITRATE(24000));
        fprintf(stderr, "[xr] opus encoder ready (静态链 1.4)\n");
    } else {
        fprintf(stderr, "[xr] opus encoder 不可用(err=%d), 上行音频停用\n", oe);
    }
    if (xr_player_init(xr_audio_output_ops())) return 2;
    g_tts_gain_pct = xr_volume_load(tts_gain_state_path(), config->tts_gain_percent);
    xr_player_set_voice_gain(g_tts_gain_pct);

    static int16_t frame[FRAME_SAMPLES];
    static int16_t *utt_pcm;              /* 堆: 单句上限 15s */
    enum { UTT_CAP = RATE * 15 };
    utt_pcm = malloc((size_t)UTT_CAP * 2);
    /* VAD 状态: 底噪/门/预滚环/句子累积与端点 (参数默认值在 xr_vad_init) */
    xr_vad_t vad;
    xr_vad_init(&vad, utt_pcm, UTT_CAP);
    xr_vad_configure(&vad, config->vad_preroll_frames,
                     (double)config->vad_trigger_db, (double)config->vad_sustain_db);
    static unsigned char wsbuf[1 << 16];
    const char *ack_path = getenv("XR_ACK_WAV");
    if (!ack_path || !*ack_path || ack_load(ack_path))
        ack_load("/data/xiaorui-server/board-scripts/xr_ack24k.wav");
    cue_load("/data/xiaorui-server/board-scripts/xr_hmm24k.wav", &hmm_pcm, &hmm_n, "hmm");
    /* 弃轮反馈音: 用户干等一整轮后必须有声音, 否则设备表现为"完全没反应"。
       可用 XR_FAIL_CUE=0 关; 音频路径可现场换(XR_FAIL_WAV), 便于先在候选位
       听感验收再决定是否进生产。音频缺失只是降级为无声, 不影响主流程。 */
    if (config->fail_cue) {
        const char *fail_path = getenv("XR_FAIL_WAV");
        if (!fail_path || !*fail_path
            || cue_load(fail_path, &fail_pcm, &fail_n, "fail"))
            cue_load("/data/xiaorui-server/board-scripts/xr_fail24k.wav",
                     &fail_pcm, &fail_n, "fail");
    }
    /* 语音音量控制: 启动恢复上次音量(一行整数文件, 缺省 100), 软件增益对
       回答与全部提示音统一生效; "好的"确认音缺失只是无声降级。 */
    if (config->volume_control) {
        const char *vol_path = getenv("XR_VOL_WAV");
        if (!vol_path || !*vol_path
            || cue_load(vol_path, &vol_pcm, &vol_n, "volack"))
            cue_load("/data/xiaorui-server/board-scripts/xr_volack24k.wav",
                     &vol_pcm, &vol_n, "volack");
    }
    g_vol_pct = xr_volume_load(volume_state_path(), XR_VOLUME_DEFAULT);
    xr_player_set_volume(g_vol_pct);
    fprintf(stderr, "[xr] 音量=%d%% (软件增益, 状态=%s)\n", g_vol_pct, volume_state_path());
    fprintf(stderr, "[xr] 语音/TTS独立增益=%d%% (状态=%s)\n",
            g_tts_gain_pct, tts_gain_state_path());
    fprintf(stderr, "[xr] buffers ready\n");

    snd_pcm_t *mic = NULL;
    xr_playback_control_t playback_control = {0};
    int kws_wake_this_utt = 0;      /* 本句由 KWS 声学唤醒 */
    int music_vad_gated = 0;
    int music_kws_utterance = 0;    /* KWS 命中时音乐正在播放, 不依赖后续状态 */
    int music_kws_pending = 0;      /* 本地音乐 KWS 轮次, 等网关唤醒确认/静默回执 */
    double music_wake_hold_until = 0.0; /* 纯唤醒后留 8s 接收分句指令 */
    /* 轮次状态机: LISTEN/RESPOND、跟说窗、冷却、轮次计时、ping 节律 */
    xr_session_t ses;
    xr_session_init(&ses);
    xr_session_set_follow_window(&ses, config->follow_window_s);
    /* 真实后端端到端回归: XR_TEXT_INJECT 配合 _REPEAT/_GAP_MS 可连续提交 N 轮文本
       (绕过 ASR, 但 LLM+TTS 走真实网关)。默认 REPEAT=1 即原来的单次注入行为。
       DELAY_MS 可等外部音乐/FIFO 链启动后再提交, 仅用于受控实机测试。 */
    char inj_text[256] = {0};
    int inj_total = 1, inj_done = 0, inj_gap_ms = 2000, inj_delay_ms = 0;
    double inj_next = 0;
    {
        const char *ie = getenv("XR_TEXT_INJECT");
        if (ie && *ie) snprintf(inj_text, sizeof inj_text, "%s", ie);
        const char *ir = getenv("XR_TEXT_INJECT_REPEAT");
        if (ir) inj_total = atoi(ir);
        const char *ig = getenv("XR_TEXT_INJECT_GAP_MS");
        if (ig) inj_gap_ms = atoi(ig);
        const char *id = getenv("XR_TEXT_INJECT_DELAY_MS");
        if (id) {
            long delay = strtol(id, NULL, 10);
            if (delay > 0 && delay <= 60000) inj_delay_ms = (int)delay;
        }
        if (inj_total < 1) inj_total = 1;
    }
    /* 后端选择: 默认 local(板上服务端); XR_BACKEND=cloud 走小智 v1 协议连
       LAN 网关/云端(端点 XR_CLOUD_URL, 详见 backend_cloud.c); XR_BACKEND=hybrid
       云端优先/本地兜底(逐轮切换, 详见 backend_hybrid.c). 会话级选择,
       不热切: 换后端须重启前端. */
    backend_t *be = NULL;
    {
        const char *bk = getenv("XR_BACKEND");
        if (bk && !strcmp(bk, "cloud")) be = be_open_cloud();
        else if (bk && !strcmp(bk, "hybrid")) be = be_open_hybrid();
        else be = be_open_local();
        if (!be) { fprintf(stderr, "[xr] backend 创建失败(XR_BACKEND=%s)\n", bk ? bk : "local"); return 2; }
    }

    xr_ui_context_t ui = {
        .backend = be, .session = &ses, .vad = &vad, .config = config,
    };
    const char *ui_socket = getenv("XR_UI_SOCKET");
    ui.socket_path = ui_socket;   /* 打开失败时靠它定时重试 */

    xr_tick();   /* 看门狗基线: 重启路径内每处有界等待前都续命 */
    {
        pthread_t wd_tid;
        if (pthread_create(&wd_tid, NULL, xr_watchdog, NULL) == 0)
            pthread_detach(wd_tid);
        else fprintf(stderr, "[xr] ERROR watchdog thread unavailable (no heartbeat)\n");
    }
    {
        const char *fifo = getenv("XR_MUSIC_FIFO");
        if (fifo && *fifo) {
            pthread_t mt;
            if (pthread_create(&mt, NULL, xr_music_fifo_thread, (void *)fifo) == 0)
                pthread_detach(mt);
            else fprintf(stderr, "[xr] ERROR music fifo thread unavailable\n");
            const char *vi = getenv("XR_VOICE_INJECT");
            if (vi && *vi) {
                pthread_t vt;
                if (pthread_create(&vt, NULL, xr_voice_inject_thread, NULL) == 0)
                    pthread_detach(vt);
                else fprintf(stderr, "[xr] ERROR voice inject thread unavailable\n");
            }
        }
    }
    /* 诊断专用: 只跑 FIFO -> 播放环 -> ALSA，跳过声学采集/VAD/网关。
       声学同步探针会被常驻 VAD 当作用户说话，触发语音中断并污染测量。
       不设置时不改变正常语音交互行为。 */
    const char *music_only = getenv("XR_MUSIC_ONLY");
    if (music_only && atoi(music_only) != 0) {
        if (!getenv("XR_MUSIC_FIFO") || !*getenv("XR_MUSIC_FIFO")) {
            fprintf(stderr, "[xr] XR_MUSIC_ONLY requires XR_MUSIC_FIFO\n");
            return 2;
        }
        fprintf(stderr, "[xr] music-only diagnostic mode (capture/VAD loop/backend connection disabled)\n");
        while (!got_sig) {
            xr_tick();
            struct timespec wait = {.tv_nsec = 200000000};
            nanosleep(&wait, NULL);
        }
        xr_player_release_device();
        return 0;
    }

restart:
    music_vad_gated = 0;   /* 重连后重新清理可能残留的音乐回声预滚 */
    music_wake_hold_until = 0.0;
    /* 网关要重新连: 预热/兜底计时重新开始 —— 不能用进程启动时的旧
     * 时间, 否则后期重连只剩几秒就强行显示正常态(2026-11-20 review)。 */
    g_started_at = now_s();
    g_tts_ready = 0;
    xr_ui_server_close(ui.server);
    ui.server = NULL;
    ui.was_responding = 0;
    ui.content_seen = 0;
    xr_playback_control_stop(&playback_control, g_vol_pct);
    /* 重连前清理: 播放/麦克风/残留流, 否则 asr_mic 被占死后永远进不来 */
    xr_player_drop();
    if (mic) {
        xr_capture_drop(mic);
        if (!g_vqe_mic) { xr_capture_close(mic); mic = NULL; }   /* vqe: close 析构有 bug, 保句柄 */
    }
    /* fifo 模式: 句柄关了要在 restart 里重开(老代码关后不重开, 且把
       "fifo:路径" 当 ALSA 设备去开 → 永远 sleep(3) 空转)。 */
    if (!strncmp(dev, "fifo:", 5)) {
        if (g_fifo_fd >= 0) { close(g_fifo_fd); g_fifo_fd = -1; }
        g_fifo_fd = fifo_open_nb(dev + 5);
        if (g_fifo_fd < 0) {
            fprintf(stderr, "[xr] fifo 重开失败(%s), 3s 重试\n", strerror(errno));
            xr_tick(); sleep(3);
            if (!got_sig) goto restart;
            return 2;
        }
    }
    be_disconnect(be);
    ses.state = XR_SESSION_LISTEN;   /* 不动窗口/冷却: 与拆分前逐字一致 */

    int conn_rc = be_connect(be);
    if (conn_rc < 0) { fprintf(stderr, "[xr] ws 失败, 10s 重试\n"); xr_tick(); sleep(10); if (!got_sig) goto restart; return 2; }
    /* conn_rc==2: 重连时补发了上一轮指令, 直接进 RESPOND 等回答 */
    if (conn_rc == 2) {
        xr_session_begin_turn(&ses, now_s());
        ui_turn_reset(&ui);
        xr_player_start();
    }
    xr_session_note_ping(&ses, now_s());
    if (ui_socket && *ui_socket) {
        ui.server = xr_ui_server_open(ui_socket);
        if (!ui.server)
            /* 打开失败不放弃: service_ui 里每 5s 重试(见 ui_socket_retry)。
             * 之前只在这里试一次, 失败就只能等下次 ws 重连。 */
            fprintf(stderr, "[ui] socket unavailable: %s (稍后重试)\n", strerror(errno));
    }
    printf("[xr] connected\n");

    if (g_fifo_fd < 0) {
        if (!mic) {   /* vqe 句柄跨 restart 复用(close 有析构 bug), 只在首次打开 */
            if (strstr(dev, "rkaudio_vqe") || strstr(dev, "xr_vqe")) {
                g_vqe_mic = 1;
                /* 统一走 xr_vqe_16k(ALSA plug: slave 48k → 客户端 16k, ES8388 硬件锁 48k)
                   周期用 XR_MIC_PERIOD 覆盖: 不同平台的 rkaudio_vqe 插件只认自己
                   的周期, 客户端请求的周期不在插件约束内就 EINVAL(period)。
                     - RK3576(ES8388): 插件 48k/period 4800, 客户端 16k/1600(=4800/3)
                       —— 1600 是 4800 的约数, 成立, 默认保持不变;
                     - RK3308(rk3308-vad, 6mic+2ref): 插件原生 16k/period 256,
                       只接受 256; 强行把插件改成 1600/6400 虽能开, 但**双工会
                       坏**(采集 RUNNING 时播放 EBUSY, 实测) —— 所以 3308 用
                       XR_MIC_PERIOD=256, 插件配置保持 SDK 原样(256/3072)。 */
                unsigned long vqe_period = 1600;
                const char *vp = getenv("XR_MIC_PERIOD");
                if (vp && atoi(vp) > 0) vqe_period = (unsigned long)atoi(vp);
                /* 设备名用 XR_MIC 给的(不再硬编码 "xr_vqe_16k"): 各平台 rkaudio_vqe
                   插件暴露的名字不同 —— RK3576 叫 xr_vqe_16k(48k→16k plug),
                   RK3308 叫 rkaudio_vqe_16k(原生 16k, SDK /etc/asound.conf 里
                   的那个)。名字由平台配置给, 核心代码不认平台。 */
                const char *vqe_dev = (dev && *dev) ? dev : "xr_vqe_16k";
                mic = xr_capture_open_nch(vqe_dev, 1, 16000, vqe_period);
                if (!mic) {
                    /* SAI 瞬态(EINVAL/EPERM, 进程退出后非确定出现, 实测 0→5
                       重挂即愈)——自动重挂后重试一次 */
                    fprintf(stderr, "[xr] vqe 打开失败, SAI 0→5 重挂重试\n");
                    if (system("amixer -c rockchipes8388 cset name='SAI1 Software Digital Loopback Mode' 0 >/dev/null 2>&1") == 0) {
                        struct timespec ts = {1, 0}; nanosleep(&ts, NULL);
                        system("amixer -c rockchipes8388 cset name='SAI1 Software Digital Loopback Mode' 5 >/dev/null 2>&1");
                        nanosleep(&ts, NULL);
                        mic = xr_capture_open_nch(dev, 1, 16000, 256);
                    }
                    if (mic) fprintf(stderr, "[xr] SAI 重挂后 vqe 打开成功\n");
                }
            } else {
                mic = g_mic4 ? xr_capture_open_nch(dev, g_hw_ch, 48000, (unsigned long)FRAME_SAMPLES * 3)
                          : xr_capture_open(dev);
            }
        } else if (g_vqe_mic) {
            xr_capture_prepare(mic);   /* 复用: drop 过的重挂 */
        }
    }
    if (!mic && g_fifo_fd < 0) { xr_tick(); sleep(3); goto restart; }
    fprintf(stderr, "[xr] mic=%s 聆听中\n", dev);

    xr_vad_reset_gate(&vad);     /* goto 跳过测量段时保持 -75 初值 */
    for (int i = 0; i < 5; i++) mic_read(mic, frame, FRAME_SAMPLES);
    for (int i = 0; i < 20; i++) {
        double db = mic_read(mic, frame, FRAME_SAMPLES) > 0
                        ? xr_vad_frame_db(frame, FRAME_SAMPLES) : -120.0;
        xr_vad_calibrate_feed(&vad, db);
    }
    xr_vad_calibrate_finish(&vad);
    fprintf(stderr, "[xr] VAD基线=%.1f dBFS VAD门限=%.1f dBFS (PCM数字电平, 非声压级)\n",
            xr_vad_noise_db(&vad), xr_vad_noise_db(&vad) + vad.margin);

    /* 相对采集/标定完成时计时，给受控实机用例留出启动 Snapclient/FIFO 的窗口。 */
    if (inj_text[0] && inj_delay_ms > 0)
        inj_next = now_s() + inj_delay_ms / 1000.0;

    /* 测试钩子: XR_TEXT_INJECT=指令 -- 开麦+标定后直接提交文本(验证 backend 接口
       全链, 绕过声学; 正常使用不设此 env)。必须在 xr_capture_open 之后: TTS_STOP 的
       xr_capture_drop/xr_capture_prepare 与后续 LISTEN 读帧都依赖有效 mic 句柄 */
    {
        const char *inj = getenv("XR_TEXT_INJECT");
        if (inj && inj[0] && now_s() >= inj_next) {
            if (be_submit_text(be, inj) == 0) {
                fprintf(stderr, "[xr] 注入指令: %s\n", inj);
                unsetenv("XR_TEXT_INJECT"); /* A reconnect must not replay the test hook. */
                xr_session_begin_turn(&ses, now_s());
                ui_turn_reset(&ui);
                xr_player_start();
                inj_done = 1;
                inj_next = now_s() + inj_gap_ms / 1000.0;
                goto inject_wait;
            }
        }
    }

    /* 重连补发已移入 backend_local.connect(返回 2 时主循环进入 RESPOND) */

inject_wait:
    while (!got_sig) {
        xr_tick();   /* 每次迭代续命: 卡在 ALSA 读/RKNN 精修/poll 即停摆, 看门狗接管 */
        /* 回合级音乐恢复: 离开 RESPOND(说完/被打断/超时)就让音乐回来。
           FIFO 路径下音乐由 xr_player_push_music_pcm 持续供给, 这里只切让位态。 */
        /* 音乐 duck 自愈: FIFO 路径下只要仍处于 duck 态、且已经不在 RESPOND
           (说完/被打断/超时/ws 断线 goto restart 后 ses.state=LISTEN),
           就恢复音乐。这样不依赖某一次状态边沿, 任何离开路径都能恢复。 */
        if (xr_player_music_ducked() && ses.state != XR_SESSION_RESPOND &&
            !vad.in_utt && !kws_wake_this_utt && xr_player_queued_samples() == 0 &&
            now_s() >= music_wake_hold_until) {
            xr_player_voice_end();
            music_wake_hold_until = 0.0;
        }
        /* 真实后端回归: 每轮结束(非 RESPOND)且到间隔就提交下一轮。
           放在每轮循环里(而不是只在 RESPOND->LISTEN 那一次), 否则若首轮比
           间隔还快, 条件在那一次不成立就永远不再尝试。 */
        if (inj_text[0] && inj_done < inj_total &&
            ses.state != XR_SESSION_RESPOND && now_s() >= inj_next) {
            if (be_submit_text(be, inj_text) == 0) {
                inj_done++;
                xr_session_begin_turn(&ses, now_s());
                ui_turn_reset(&ui);
                xr_player_start();
                inj_next = now_s() + inj_gap_ms / 1000.0;
                fprintf(stderr, "[xr] 注入指令 %d/%d: %s\n",
                        inj_done, inj_total, inj_text);
            } else {
                inj_next = now_s() + 1.0;   /* 发送失败(多半断连): 1s 后重试 */
            }
        }
        if (service_ui(&ui)) {
            xr_playback_control_stop(&playback_control, g_vol_pct);
            if (be_abort(be) != 0) goto restart;
            xr_player_drop();
            if (capture_finish_turn(mic) < 0) goto restart;
            xr_vad_clear_preroll(&vad);
            wake_reset();
            xr_session_end_turn_silent(&ses, now_s(), config->finish_cool_ms / 1000.0);
            xr_session_set_echo_guard(&ses, now_s() + config->echo_guard_s);
            xr_vad_reset_gate(&vad);
            fprintf(stderr, "[ui] stop\n");
            continue;
        }
        if (ses.state == XR_SESSION_RESPOND) {
            /* 等待期也要采音: VQE 缓冲浅(~400ms), 停采会 overrun 丢音频。
               ★ 只读一个周期(256 样本 = 16ms): 大步读会阻塞 ws 轮询,
                 opus 音频包拉不动 -> 播放器欠载 -> TTS 卡顿(2026-11-19 实测)。
               板子正在播报时不攒(那是回声, 回声护栏会兜)。 */
            /* 等待期也要采音: VQE 缓冲浅(~400ms), 停采会 overrun 丢音频。
               板子正在播报时不攒(那是回声, 回声护栏会兜)。 */
            if (xr_player_queued_samples() == 0) {
                int pr = cap_pull(mic);
                if (pr < 0) {
                    /* 缓冲见底(EIPE): 立刻恢复流, 别让下一次读带着旧积压 */
                    xr_capture_prepare(mic);
                } else if (cap_frame_ready()) {
                    int16_t tmpf[FRAME_SAMPLES];
                    cap_frame_take(tmpf);
                    if (g_pending_n < PENDING_MAX_FRAMES) {
                        memcpy(g_pending[g_pending_n++], tmpf,
                               (size_t)FRAME_SAMPLES * 2);
                    } else {
                        /* 满了丢最老的, 保留最近的 */
                        memmove(g_pending[0], g_pending[1],
                                (size_t)(PENDING_MAX_FRAMES - 1) * FRAME_SAMPLES * 2);
                        memcpy(g_pending[PENDING_MAX_FRAMES - 1], tmpf,
                               (size_t)FRAME_SAMPLES * 2);
                    }
                }
            }
            xr_playback_control_tick(&playback_control, g_vol_pct);
            xr_music_command_t music_command;
            if (xr_playback_control_poll(&music_command)) {
                if (music_command.action == XR_MUSIC_STOP) {
                    fprintf(stderr, "[music] 语音停止播放\n");
                    xr_playback_control_stop(&playback_control, g_vol_pct);
                    /* 打断当前轮但**不断连**: 服务端 {"type":"abort"} 会停
                       LLM/TTS 并置 client_abort, 音乐流立刻收敛。
                       以前用 be_disconnect 整条 WS 断掉(为了取消解码器),
                       代价是前端重连 + 重新标定 —— 用户观感就是"播放的时候
                       设备被切了一刀"(日志: 已连接/connected/聆听中 三连)。 */
                    if (be_abort(be) == 0) {
                        xr_session_abort_turn(&ses);
                        xr_player_drop();
                    } else {
                        /* 发不出去(连接已断): 退回断连重连的老路 */
                        be_disconnect(be);
                        xr_session_abort_turn(&ses);
                        goto restart;
                    }
                }
                if (music_command.action == XR_MUSIC_FORWARD) {
                    /* 音乐播放中说了普通指令(开灯/报时/...): 播放期麦克风归
                       控制线程, 不转交就是黑洞(HA 意图收不到)。这里停掉音乐,
                       把正文作为一轮正常指令交回服务端(走完整意图链)。 */
                    fprintf(stderr, "[music] 转交普通指令: %s\n", music_command.text);
                    xr_playback_control_stop(&playback_control, g_vol_pct);
                    /* 不 abort 音乐: 网关侧有"让位"机制(音乐流发现有回答排队
                       就先让回答播, 答完再续播), abort 会把音乐彻底掐死。 */
                    xr_session_abort_turn(&ses);
                    xr_player_drop();             /* 丢掉还排队的音乐音频 */
                    if (be_submit_text(be, music_command.text) == 0) {
                        xr_session_begin_turn(&ses, now_s());
                        ui_turn_reset(&ui);
                        xr_player_start();        /* 准备播回答 */
                        fprintf(stderr, "[music] 已作为正常轮提交\n");
                    } else {
                        fprintf(stderr, "[music] 转交失败(发送错误)\n");
                    }
                }
                if (music_command.action == XR_MUSIC_VOLUME && config->volume_control) {
                    g_vol_pct = xr_volume_next(g_vol_pct, music_command.volume_op, music_command.value);
                    xr_player_set_volume(g_vol_pct);
                    xr_volume_save(volume_state_path(), g_vol_pct);
                    fprintf(stderr, "[music] 播放中音量=%d%%\n", g_vol_pct);
                }
            }
            /* 播放期: 只收 ws; 文本事件打印, 音频解码直写, stop 排干恢复.
               LLM 首句可能 >10s, ws_recv 超时(返 0)继续等不算断线. */
            size_t l = 0;
            char ev_text[XR_UI_ANSWER_CAPACITY] = {0};
            /* 只有真有 UI 客户端在看时才把 ws 轮询降到 50ms。UI 每 100ms 问
               一次状态, 2s 内问过就算"有人在看"; 没 UI 时回到 1000ms, 即
               UI 提交之前的行为, 回答期不必 20Hz 空转。
               注: 空闲采集期本来就是 timeout 0 非阻塞轮询, 与 UI 在不在无关。 */
            /* 轮询上限压到 200ms: 采音节律必须短于 VQE 的 ~400ms 硬件缓冲,
               否则等待期还是会 overrun(1000ms 一次轮询就撑爆了)。 */
            int ui_poll_ms = (ui.server && xr_ui_server_client_active(ui.server, 2000)) ? 50 : 200;
            backend_ev_t ev = be_poll_event(be, wsbuf, sizeof(wsbuf), &l,
                                            ev_text, sizeof(ev_text), ui_poll_ms);
            if (ev == EV_ERROR) { fprintf(stderr, "[xr] ws 断(播放期)\n"); goto restart; }
            /* 与空闲期一致: 收到网关业务事件 = 语音真可用了 */
            if (ev == EV_STT || ev == EV_SILENT || ev == EV_TTS_START ||
                ev == EV_ANNOUNCE)
                g_tts_ready = 1;
            if (ev == EV_ANNOUNCE_REQUEST) {
                if (be_announce_reply(be, ev_text, 0)) goto restart;
                continue;
            }
            if (ev == EV_RESCUED) {
                /* 控制窗内 duck 到 10% 时在这里收轮: 增益必须就地恢复 ——
                   出了 RESPOND 循环没人再管, 残留会让下一轮提示音异常小声 */
                xr_playback_control_stop(&playback_control, g_vol_pct);
                xr_session_mark_rescued(&ses, now_s());
                fprintf(stderr, "[xr] 断线换路续答，重置等待\n");
                continue;
            }
            /* 慢响应填充音: **每轮循环都要判**, 不能挂在 EV_TIMEOUT 分支里 —— 云端 STT
               事件通常在首包前就先到(那不是超时), 挂在超时分支会白等一整个
               轮询周期, 实测填充音拖到 2.0~2.3s 才响, 跟首包几乎挨着, 白做。 */
            if (xr_session_wants_think_cue(&ses, now_s(), config->think_cue_ms)) {
                /* 实测云端首包 2.58~3.16s(代码原注释"0.56+0.09 已够快"与实测
                   不符)。这段静默里用户会以为没被听见而重复说话, 造成双重提交。
                   到阈值仍一个包都没来就先补一声短"嗯"把静默打断。
                   播放器此刻已在跑(提交指令时就起了), 直接推入同一个环, TTS
                   音频会排在"嗯"之后, 不额外增加延迟。
                   换路续答后 respond_t0 会重置, 但标记不清 —— 一次发言只补一声。 */
                xr_session_mark_think_cue(&ses);
                /* 注意不能调 xr_player_start(): 它会先 finish 掉旧线程并清空
                   环(ring_w=ring_r=0), 把已排队的 TTS 音频抹掉。 */
                if (hmm_pcm) {
                    xr_player_push_pcm(hmm_pcm, hmm_n);
                    fprintf(stderr, "[xr] 补填充音(等待 %.1fs 无音频)\n",
                            now_s() - ses.respond_t0);
                }
            }
            if (ev == EV_TIMEOUT) {
                /* 兜底一: 服务端受理后 8s 无 sentence_start(如 ACTION/意图路径吞掉首句
                   标志), 视为本轮空回答, 放弃等待回聆听(窗仍在, 可重说)。
                   兜底二: 20s 内无任何受理事件(云端网关静默/半途断言)强制收轮——
                   没 this 兜底, 不回显 stt 的网关会让前端无限等待。 */
                if (xr_session_timed_out(&ses, now_s(), be_seen_stt(be),
                                       config->accepted_silence_s,
                                       config->response_timeout_s)) {
                    /* 先问后端能否换路续答: 云端受理但零音频(云端 TTS 挂/静默网关)
                       由 hybrid 翻本地补发, 返回 1 时本轮继续等; 每轮限一次 */
                    if (!xr_session_rescue_used(&ses) && be_drop_turn(be) == 1) {
                        xr_session_mark_rescued(&ses, now_s());
                        fprintf(stderr, "[xr] 后端已换路续答, 重置等待\n");
                        continue;
                    }
                    fprintf(stderr, "[xr] 等待 %.1fs 仍无音频(受理=%d)，放弃本轮\n",
                            now_s() - ses.respond_t0, be_seen_stt(be));
                    be_drop_turn(be);   /* 通知后端弃轮(hybrid 清轮状态, 防空闲 poll 路由失同步) */
                    xr_playback_control_stop(&playback_control, g_vol_pct);
                    xr_player_drop();
                    if (capture_finish_turn(mic) < 0) goto restart;
                    xr_vad_clear_preroll(&vad);
                    wake_reset();
                    xr_session_end_turn_silent(&ses, now_s(), config->abandon_cool_ms / 1000.0);
                    xr_vad_reset_gate(&vad);
                    /* 弃轮反馈: 前面用户已经干等了一整轮, 不给任何声音的话设备
                       表现为"完全没反应", 用户分不清是没听见/还在想/已失败, 只会
                       更大声重复 -> 更多误触发。只在这一处播 —— "未含唤醒词"和
                       "窗内超短指令"是我们在压自己的误触发, 那两处再出声只会
                       制造更多余音。 */
                    if (xr_session_play_fail_cue(&ses, now_s(), &CUE_OPS,
                                                fail_pcm, fail_n,
                                                config->fail_cue_cool_ms,
                                                config->echo_guard_s))
                        fprintf(stderr, "[xr] 已播弃轮提示音\n");
                    else if (fail_n)
                        fprintf(stderr, "[xr] 弃轮提示音未播(音频或播放器不可用)\n");
                }
                continue;
            }
            if (ev == EV_SILENT) {
                /* 服务端静默回执: 这一轮不是命令(纯唤醒/噪声)。没有它, 前端要
                   干等 response_timeout(默认 20s) 才收轮, 这段时间麦克风是聋的
                   —— 紧随其后的指令段整段丢失(声学套件 A2/A3 首试的
                   missing-command 就是这个), 最后还会播一声弃轮提示音, 用户
                   观感是"喊了没反应/总是断开"。 */
                int music_kws_fallback = music_kws_pending;
                music_kws_pending = 0;
                music_kws_utterance = 0;
                int is_wake = !strcmp(ev_text, "wake") || music_kws_fallback;
                if (music_kws_fallback && strcmp(ev_text, "wake"))
                    fprintf(stderr, "[xr] music KWS 有效但云端未识别唤醒词: 本地确认并保留跟说窗\n");
                if (!is_wake) music_wake_hold_until = 0.0;
                xr_playback_control_stop(&playback_control, g_vol_pct);
                xr_player_drop();
                /* 文本没确认唤醒(KWS 误触/噪声): 撤销声学临时窗口, 恢复命中前
                   的状态 —— 否则误触留下的窗口让下一句随口话免唤醒进命令链。 */
                double saved_awake = g_awake_before_acoustic;
                if (!is_wake) {
                    if (capture_finish_turn(mic) < 0) goto restart;
                    xr_vad_clear_preroll(&vad);
                    wake_reset();
                    if (saved_awake >= 0.0) {
                        ses.awake_until = saved_awake;
                        fprintf(stderr, "[xr] 服务端未确认唤醒: 撤销声学临时窗口\n");
                    }
                }
                xr_session_end_turn_silent(&ses, now_s(),
                                           is_wake ? 0.0 : config->abandon_cool_ms / 1000.0);
                xr_vad_reset_gate(&vad);
                if (is_wake) {
                    /* 服务端已认定纯唤醒: 与本地纯唤醒路径一致 —— 开跟说窗 + 确认音,
                       这样"唤醒词被服务端 ASR 接住"和"前端 KWS 接住"体验一致。 */
                    xr_session_awake(&ses, now_s());
                    if (xr_player_start() == 0 && ack_pcm) {
                        xr_player_push_pcm(ack_pcm, ack_n);
                        xr_session_note_ack(&ses, now_s(), (double)ack_n / 24000.0,
                                            config->ack_cool_ms / 1000.0);
                    }
                    fprintf(stderr, "[xr] 服务端静默回执(唤醒): 直接进跟说窗\n");
                } else {
                    fprintf(stderr, "[xr] 服务端静默回执(噪声): 收轮, 不搭话\n");
                }
                continue;
            }
            if (ev == EV_TTS_STOP) {
                int music_kws_empty = music_kws_pending && ses.pkt_n == 0;
                music_kws_pending = 0;
                music_wake_hold_until = 0.0;
                /* 先量 TTS_STOP 时环里还剩多少语音: 决定 voice_end 硬清环是否会截尾。 */
                fprintf(stderr, "[xr] tts stop: queued=%zu (%.2fs)\n",
                        xr_player_queued_samples(),
                        xr_player_queued_samples() / 24000.0);
                xr_playback_control_stop(&playback_control, g_vol_pct);
                ui.abort_failed = 0;
                int cancelled = xr_player_finish_with_poll(
                    ui.server ? service_ui_drain : NULL, &ui);
                if (ui.abort_failed) goto restart;
                /* 回答期间没有读麦克风，丢弃 ALSA 积压的自身播音，避免回声成为下一轮指令。 */
                if (capture_finish_turn(mic) < 0) goto restart;
                xr_vad_clear_preroll(&vad);
                wake_reset();
                /* 零音频轮(空识别, 或服务端有意不答却没发 silent)不开跟说窗:
                   静默实测里这种轮次会白送 30s 免唤醒, 之后屋里随口一句话就被
                   当"跟说"直接上云 —— 用户听到的就是"莫名其妙答话"。只有真出过
                   声的轮次才续窗。 */
                int answered = ses.pkt_n > 0 && !cancelled;
                if (answered)
                    xr_session_end_turn(&ses, now_s(), config->finish_cool_ms / 1000.0);
                else if (music_kws_empty) {
                    /* Cloud ASR can map an isolated wake to an unrelated word
                       (e.g. "Yeah"). KWS is the acoustic proof: acknowledge
                       locally and retain the follow-up window. */
                    xr_session_awake(&ses, now_s());
                    if (xr_player_music_active() && !xr_player_music_ducked())
                        xr_player_voice_begin();
                    music_wake_hold_until = now_s() + 8.0;
                    if (xr_player_start() == 0 && ack_pcm) {
                        xr_player_push_pcm(ack_pcm, ack_n);
                        xr_session_note_ack(&ses, now_s(), (double)ack_n / 24000.0,
                                            config->ack_cool_ms / 1000.0);
                    }
                    fprintf(stderr, "[xr] music KWS 未获云端唤醒文本: 本地播确认音并保留跟说窗\n");
                }
                else
                    xr_session_end_turn_silent(&ses, now_s(), config->finish_cool_ms / 1000.0);
                xr_session_set_echo_guard(&ses, now_s() + config->echo_guard_s);
                xr_vad_reset_gate(&vad);   /* 喇叭刚响过, 重测底噪 */
                if (answered)
                    fprintf(stderr, "[xr] 回答完毕, 30s 内免唤醒\n");
                else if (music_kws_empty)
                    fprintf(stderr, "[xr] 音乐唤醒确认完成, 30s 内免唤醒\n");
                else
                    fprintf(stderr, "[xr] 空轮(零音频)收轮, 不开跟说窗\n");
            } else if (ev == EV_STT) {
                if (ev_text[0]) {
                    ui_turn_content(&ui);
                    xr_ui_server_text(ui.server, true, ev_text);
                }
                fprintf(stderr, "[xr] ASR 回执: %s\n", ev_text);
            } else if (ev == EV_REBOOT) {
                /* 语音重启(服务端判定): sync 后整机重启, 自启链拉起本进程 */
                fprintf(stderr, "[xr] 语音重启(服务端)\n");
                fflush(stderr);
                sleep(1);
                xr_request_reboot();
            } else if (ev == EV_VOLUME) {
                /* 服务端音量指令(音频直传轮的意图判定在服务端): 本地软件增益
                   + "好的"确认音, 按正常回答收尾。零音频轮不能走 drop_turn
                   (hybrid 会当静默救援把同句补投本地 LLM, 造成双回答)。 */
                xr_vol_op_t vop = XR_VOL_NONE;
                int vval = 0;
                if (!strcmp(ev_text, "up")) vop = XR_VOL_UP;
                else if (!strcmp(ev_text, "down")) vop = XR_VOL_DOWN;
                else if (!strcmp(ev_text, "max")) vop = XR_VOL_MAX;
                else if (!strcmp(ev_text, "min")) vop = XR_VOL_MIN;
                else if (!strncmp(ev_text, "set:", 4)) {
                    vop = XR_VOL_SET;
                    vval = atoi(ev_text + 4);
                }
                if (vop != XR_VOL_NONE && config->volume_control) {
                    g_vol_pct = xr_volume_next(g_vol_pct, vop, vval);
                    xr_player_set_volume(g_vol_pct);
                    xr_volume_save(volume_state_path(), g_vol_pct);
                    fprintf(stderr, "[xr] 音量指令(服务端): %s -> %d%%\n",
                            ev_text, g_vol_pct);
                }
                xr_playback_control_stop(&playback_control, g_vol_pct);
                if (vol_pcm) xr_player_push_pcm(vol_pcm, vol_n);
                xr_player_finish();
                if (capture_finish_turn(mic) < 0) goto restart;
                xr_vad_clear_preroll(&vad);
                wake_reset();
                xr_session_end_turn(&ses, now_s(), config->finish_cool_ms / 1000.0);
                xr_session_set_echo_guard(&ses, now_s() + config->echo_guard_s);
                xr_vad_reset_gate(&vad);
                fprintf(stderr, "[xr] 回答完毕, 30s 内免唤醒\n");
            } else if (ev == EV_TTS_START) {
                music_wake_hold_until = 0.0;
                /* 语音真的能用了: 网关发得出 TTS, 说明 RockTTS 已加载就绪。
                 * 界面据此结束"正在准备语音…"—— 比固定 60 秒窗口准:
                 * 慢机器上 60 秒可能还没好, 快机器上早就能说了(2026-11-20
                 * 真板反馈: 问候都播了, 界面还写着"正在准备语音")。 */
                g_tts_ready = 1;
                if (ev_text[0]) {
                    ui_turn_content(&ui);
                    xr_ui_server_text(ui.server, false, ev_text);
                }
                /* 控制线程覆盖全部播放期(2026-10-16): 音乐+LLM 回答都可
                 * 语音停/调音量; duck 只在音乐轮(回答轮自听不压低)。
                 * USB capture only: VQE/fifo ownership stays unchanged. */
                if (!playback_control.music && (strstr((const char *)wsbuf, "\"media\":\"music\"") ||
                                       strstr((const char *)wsbuf, "\"media\": \"music\""))) {
                    playback_control.music = 1;
                }
                /* 每句 TTS_START 都会走到这: drop/start 只做一次 --
                   重复 drop 会反复掐断控制线程的 KWS 流(凑不满唤醒
                   chunk, 实测长回答喊不停), 且 start 本身幂等。 */
                /* VQE 采集下播放期控制默认关(2026-10-16 设备所有权顾虑);
                   XR_VQE_MUSIC_CONTROLS=1 解锁实测(SAI 回环拓扑, 2026-10-21) */
                const char *vmc = getenv("XR_VQE_MUSIC_CONTROLS");
                int vqe_ctl = g_vqe_mic && vmc && atoi(vmc);
                if (config->music_controls && config->local_asr && (!g_vqe_mic || vqe_ctl)
                    && !g_mic4 && g_fifo_fd < 0 && mic
                    && !xr_music_controls_running()) {
                    /* VQE 插件经不起 drop/prepare(内部流会被掐死, KWS 读零)——
                       播放期主循环不读麦, 控制线程自读, 跳过 drop 直接启动;
                       USB 路径维持 drop/prepare 清积压的既有行为 */
                    int ready = vqe_ctl;
                    if (!ready) {
                        xr_capture_drop(mic);
                        ready = (xr_capture_prepare(mic) == 0);
                    }
                    if (ready &&
                        xr_music_controls_start(&MUSIC_OPS, mic, config->music_command_ms) == 0) {
                        /* 控制线程会一直读麦 -> 采集缓冲被读走, 收尾时不必
                           drop/prepare(见 capture_finish_turn 的说明) */
                        g_mic_drained_by_control = 1;
                        fprintf(stderr, "[ctrl] 播放期语音控制已启用(%s)\n",
                                playback_control.music ? "音乐" : "回答");
                    }
                }
                printf("[event] %.160s\n", (const char *)wsbuf);
            } else if (ev == EV_TTS_AUDIO) {
                /* 第一句 TTS 音频到达: 此刻才让位(思考期音乐照常)。幂等。 */
                xr_player_voice_begin();
                int ns = xr_player_push_opus(wsbuf, l);
                xr_session_note_packet(&ses, now_s(), ns,
                                       xr_player_queued_samples() / 24000.0,
                                       getenv("XR_TIMING") != NULL);
                if (ns < 0) fprintf(stderr, "[xr] opus decode failed: %d\n", ns);
            }
            continue;
        }

        long n = mic_read(mic, frame, FRAME_SAMPLES);
        if (n == -EPIPE) { xr_capture_prepare(mic); continue; }
        if (n < 0) { fprintf(stderr, "[xr] mic: %s\n", xr_audio_error((int)n)); sleep(2); goto restart; }
        /* 麦克风电平给界面用(眼睛/声浪跟着人说话动): 相对底噪的 dB 映射成
           0..100, 上去快下来慢, 不然数值乱跳 */
        {
            double level_db = xr_vad_frame_db(frame, FRAME_SAMPLES);
            double reference = xr_vad_noise_db(&vad);
            int level = (int)((level_db - reference) * 3.0);
            if (level < 0) level = 0;
            if (level > 100) level = 100;
            ui.level = xr_ui_level_next(ui.level, level);
            xr_ui_server_level(ui.server, (unsigned int)ui.level);
        }
        /* 调试(XR_UTT_DUMP=<dir>): 落原始帧 + 上传前的句子, 用于现场比对
           "麦克风到底收到了什么" 与 "云端听到了什么"。不设变量时零开销。 */
        dump_raw_frame(frame, n);
        if (xr_session_ping_due(&ses, now_s())) {
            if (be_ping(be)) { fprintf(stderr, "[xr] ws 心跳发送失败, 重连\n"); goto restart; }
            xr_session_note_ping(&ses, now_s());
        }
        {
            char ev_text[256];
            backend_ev_t ev = be_poll_event(be, wsbuf, sizeof(wsbuf) - 1, &(size_t){0},
                                            ev_text, sizeof(ev_text), 0);
            if (ev == EV_ERROR) { fprintf(stderr, "[xr] ws 空闲期断开, 重连\n"); goto restart; }
            /* ★ 语音真的可用了 = 网关发来了业务事件(识别结果/静默回执/
             *   TTS/主动播报), 说明网关和 RockASR/TTS 已加载就绪。界面据此
             *   结束"正在准备语音…"(2026-11-20 用户诉求: 语音没好就不该
             *   显示"等你说一句")。 */
            if (ev == EV_STT || ev == EV_SILENT || ev == EV_TTS_START ||
                ev == EV_ANNOUNCE)
                g_tts_ready = 1;
            /* 服务端主动播报(定时提醒到点): 转播放态, 随后的音频/stop 由
               RESPOND 机器正常收尾(含开跟说窗, 用户可直接说"谢谢/关闭") */
            if (ev == EV_REBOOT) {
                fprintf(stderr, "[xr] 语音重启(服务端, 空闲)\n");
                fflush(stderr);
                sleep(1);
                xr_request_reboot();
            }
            if (ev == EV_ANNOUNCE_REQUEST) {
                if (vad.in_utt || xr_session_cooling(&ses, now_s())) {
                    if (be_announce_reply(be, ev_text, 0)) goto restart;
                    continue;
                }
                if (xr_player_start()) {
                    if (be_announce_reply(be, ev_text, 0)) goto restart;
                    continue;
                }
                xr_session_begin_turn(&ses, now_s());
                ui_turn_reset(&ui);
                if (be_announce_reply(be, ev_text, 1)) goto restart;
                fprintf(stderr, "[xr] 主动播报已受理: %s\n", ev_text);
                continue;
            }
            if (ev == EV_ANNOUNCE) {
                fprintf(stderr, "[xr] 主动播报(提醒)\n");
                xr_session_begin_turn(&ses, now_s());
                ui_turn_reset(&ui);
                if (xr_player_start()) xr_session_abort_turn(&ses);
                continue;
            }
            /* EV_NONE/TIMEOUT/迟到事件: 静默 */
        }
        if (xr_session_cooling(&ses, now_s())) continue;   /* 播放余音期: 丢弃不进 VAD */
        double db = xr_vad_frame_db(frame, FRAME_SAMPLES);
        if (getenv("XR_DIAG_AUDIO")) {
            static double diag_at = 0;
            static double diag_max_db = -120;
            if (db > diag_max_db) diag_max_db = db;
            double t = now_s();
            if (t - diag_at >= 1.0) {
                fprintf(stderr, "[diag] mic n=%ld peak_1s=%.1f dBFS vad_noise=%.1f dBFS vad_gate=%.1f dBFS in_utt=%d\n",
                        (long)n, diag_max_db, xr_vad_noise_db(&vad),
                        xr_vad_noise_db(&vad) + vad.margin, vad.in_utt);
                diag_at = t;
                diag_max_db = -120;
            }
        }

        /* FIFO 音乐在播放时会回灌麦克风。音乐期间只喂 KWS、不喂普通能量 VAD；
           KWS 命中即压低本机音乐，留出语音采集余量。纯唤醒后最多留 8s 接分句
           指令，超时恢复音乐增益后仍只接受 KWS。进入 gate 时清理回声预滚。 */
        int music_gate = xr_vad_should_gate_music(
            xr_player_music_active(), xr_player_music_ducked(), wake_available(),
            xr_session_in_window(&ses, now_s()));
        if (music_gate && !music_vad_gated) {
            xr_vad_discard_utterance(&vad);
            g_pending_n = 0;
            kws_wake_this_utt = 0;
            fprintf(stderr, "[xr] music VAD gate on (KWS remains active)\n");
        }
        music_vad_gated = music_gate;

        /* 声学 KWS 常驻: 无论是否在采集语句中都持续喂(命中需积累 ~640ms chunk,
           若只在空闲喂, 能量门总会在唤醒词第一个字就先触发 in_utt 导致 KWS 永远吃不满) */
        if (wake_available()) {
            if (wake_poll(frame, (int)n)) {
                kws_wake_this_utt = 1;      /* 声学证据: 后续文本判定兜底用 */
                if (xr_player_music_active() && !xr_player_music_ducked()) {
                    music_kws_utterance = 1;
                    xr_player_voice_begin(); /* 先停音乐, 再录用户指令 */
                    music_wake_hold_until = now_s() + 8.0;
                }
                /* ★ 声学唤醒命中 -> 本地**立刻**开跟说窗, 不等服务端判。
                   服务端判唤醒要等: VAD 静音 1.2~1.6s 判"说完" + RockASR 1.4s
                   + 一来一回, 约 3s。这 3s 里用户紧接着说的指令整段丢失
                   (麦克风在录, 但窗口没开, 路由把它当冷启动轮丢掉) ——
                   就是"反应慢 / 认不到"的根因(2026-11-19 声学套件实测:
                   唤醒词后 1s 说的指令根本没被采到, 网关只听到"小瑞小瑞")。
                   窗口本地先开好, 指令就能被接住; 确认音仍由服务端回执播,
                   避免本地抢答。 */
                /* 记住命中前的窗口: 服务端文本没确认时要恢复回来 */
                g_awake_before_acoustic = ses.awake_until;
                if (!xr_session_in_window(&ses, now_s())) {
                    xr_session_awake(&ses, now_s());
                    fprintf(stderr, "[xr] 声学唤醒: 本地开跟说窗(等服务端文本确认)\n");
                }
                /* KWS 命中即开采集: 唤醒词音频可能电平临界没过能量门,
                   声学证据优先 -- 命中帧起收集(预滚已环存) */
                int started = 0;
                if (db < xr_vad_noise_db(&vad) + vad.margin) {
                    if (now_s() < ses.awake_until) xr_session_awake(&ses, now_s());  /* 说话即续窗 */
                    xr_vad_force_start(&vad, frame, (int)n, db, &started);
                    if (started)
                        fprintf(stderr, "[xr] KWS 触发采集(回调当前帧 %.1f dBFS < VAD门 %.1f dBFS; 不代表环境底噪)\n",
                                db, xr_vad_noise_db(&vad) + vad.margin);
                }
            }
        }

        if (music_gate && !kws_wake_this_utt)
            continue;   /* 唤醒检测仍持续; 普通音乐能量不进入 ASR */

        int vad_started = 0;
        xr_vad_result_t vr = XR_VAD_CONTINUE;
        /* 上一轮等待服务端判定期间积压的帧: **先**补喂(它们比当前帧更早),
           VAD 的预滚环会把开头半个字回补, 端点判定照常 -> 指令不会被吞。 */
        if (g_pending_n > 0) {
            for (int i = 0; i < g_pending_n; i++) {
                double pdb = xr_vad_frame_db(g_pending[i], FRAME_SAMPLES);
                int pst = 0;
                vr = xr_vad_feed(&vad, g_pending[i], FRAME_SAMPLES, pdb, &pst);
                if (vr != XR_VAD_CONTINUE) break;
            }
            g_pending_n = 0;
            if (vr == XR_VAD_TOO_SHORT) continue;
            if (vr == XR_VAD_UTT_DONE) goto utterance_ready;
        }
        vr = xr_vad_feed(&vad, frame, (int)n, db, &vad_started);
        if (vr == XR_VAD_TOO_SHORT) {
            /* KWS 已经给出可靠的唤醒证据，但低电平唤醒词可能只有 1-2 个
               VAD 有声帧，正常会被 min_voice_frames 当成 TOO_SHORT 丢弃。
               之前这条路径同时清掉 KWS 标志，导致用户听不到“我在”，跟说窗
               也没有确认。仅在没有形成可识别语句时补本地确认音；完整语句仍
               走下方 ASR/文本门，因此“唤醒词+控制命令”不会被提前截断。 */
            if (kws_wake_this_utt) {
                fprintf(stderr, "[xr] KWS 唤醒但 VAD 语句过短: 本地补确认音并保留跟说窗\n");
                xr_session_awake(&ses, now_s());
                if (xr_player_start() == 0 && ack_pcm) {
                    xr_player_push_pcm(ack_pcm, ack_n);
                    xr_session_note_ack(&ses, now_s(), (double)ack_n / 24000.0,
                                        config->ack_cool_ms / 1000.0);
                }
                kws_wake_this_utt = 0;
                music_kws_utterance = 0;
            }
            if (xr_player_music_ducked() && ses.state != XR_SESSION_RESPOND) {
                if (now_s() >= music_wake_hold_until) {
                    xr_player_voice_end();
                    music_wake_hold_until = 0.0;
                }
            }
            /* VAD已抬门；必须继续读麦。1.5s阻塞大于VQE的400ms硬件缓冲，
               会造成overrun并吞掉紧接着的短指令/KWS。采样节律由readi约束。 */
            continue;
        }
        if (vr != XR_VAD_UTT_DONE) continue;

utterance_ready:
        /* 采集结束: 先收割 KWS 可能已触发的唤醒(触发点延后到尾静音区),
           再走文本识别 -- 否则这句的声学唤醒证据被丢 */
        if (wake_flush()) kws_wake_this_utt = 1;

        size_t utt_n = 0;
        const int16_t *utt = xr_vad_utterance(&vad, &utt_n);
        fprintf(stderr, "[xr] 说完 (%.1fs), 识别中\n", (double)utt_n / RATE);

        /* KWS 只证明含唤醒词，不证明没有指令。即使很短也提交文本门确认，
           防止“小瑞小瑞开灯”被时长阈值整句吞掉。 */
        if (g_dump_dir && *g_dump_dir) {
            static int dump_seq;
            char dpath[512];
            snprintf(dpath, sizeof(dpath), "%s/utt-%03d.wav", g_dump_dir, ++dump_seq);
            dump_wav(dpath, utt, utt_n, RATE);
        }

        /* 云端优先(2026-10-20): 只要云腿可用就直接上传, 不先跑本地 RKNN。
           本地转写本来就差("小想"/"嗯嗯嗯"), 判定权交给服务端:
             - 跟说窗内: 整句当指令
             - 冷启动轮: 带 wake=required, 服务端用云 ASR 的转写判唤醒
               (精确词表 + 小X小Y 形状), 不认就回 silent/nowake
           仍保留本地 ASR 的三种情况: 云腿不可用(纯本地模式)、KWS 刚命中
           (要区分"纯唤醒"和"唤醒+指令", 本地剥前缀更快)、回声护栏内
           (别把自家 TTS 送去云端识别)。本地文本仍是 hybrid 断线补答的兜底
           (transcribe 回调, 只在真断线时才会被调用)。 */
        int follow = xr_session_in_window(&ses, now_s());
        xr_speech_route_t route = xr_speech_route(config->local_asr, be_wants_audio(be),
            kws_wake_this_utt, xr_session_echo_guarded(&ses, now_s()));
        if (route == XR_SPEECH_NO_UPLINK || route == XR_SPEECH_ECHO_DROP) {
            fprintf(stderr, "[xr] cloud-only utterance dropped: %s\n",
                    route == XR_SPEECH_NO_UPLINK ? "uplink unavailable" : "echo guard (no KWS)");
            kws_wake_this_utt = 0;
            music_kws_utterance = 0;
            continue;
        }
        if (route == XR_SPEECH_UPLOAD) {
            /* KWS wakes the local follow window, but the utterance may contain
             * only the wake phrase. Keep the gateway wake check on this turn
             * so a cloud ASR miss (e.g. "Right") cannot be sent to the LLM as
             * a user command. If the gateway returns silent/no-wake for a
             * music-KWS turn, EV_SILENT below supplies the local ack; a later
             * command in the follow window is uploaded without wake_required. */
            int music_kws_wake = music_kws_utterance && kws_wake_this_utt;
            backend_audio_request_t request = {
                .pcm = utt, .samples = utt_n,
                .transcribe = config->local_asr ? transcribe_followup : NULL,
                .wake_required = !follow || kws_wake_this_utt,
            };
            if (music_kws_wake)
                fprintf(stderr, "[xr] music KWS wake turn: require server wake confirmation; later follow-up is command text\n");
            else if (!config->local_asr && kws_wake_this_utt)
                fprintf(stderr, "[xr] cloud-only KWS hit: server confirms wake\n");
            kws_wake_this_utt = 0;
            music_kws_pending = music_kws_wake;
            music_kws_utterance = 0;
            if (be_submit_audio(be, &request)) goto restart;
            g_mic_drained_by_control = 0;   /* 新的一轮: 上一轮的播放期标志作废 */
            if (follow)
                fprintf(stderr, "[xr] 跟说音频交服务端 ASR\n");
            else
                fprintf(stderr, "[xr] 冷启动音频交服务端 ASR(云端优先, 本地不转写)\n");
            xr_session_begin_turn(&ses, now_s());
            ui_turn_reset(&ui);
            if (xr_player_start()) xr_session_abort_turn(&ses);
            continue;
        }

        /* 单遍识别: 20s 大模型(RKNN NPU)直接出最终文本(2026-10-11 定案 --
           KWS 接管唤醒后流式草稿职责架空, 双模型纯浪费 0.5s; CLI 为回退)。
           双路(进程内+CLI)都失败时 text 为空 -> 本轮按未识别处理;
           尾部语气词/标点剥离在模块内一并完成。 */
        char text[1024] = {0};
        xr_speech_recognize(utt, utt_n, text, sizeof(text));
        /* text 空则下面 xr_parse_wake_text 自然走 0=忽略 */
        if (text[0]) {
            /* 唤醒语义 (常驻语音助手惯例):
           - 纯唤醒词(短且以小开头): 只播确认音+开窗, 不进 LLM
           - 唤醒词+指令同句: 剥掉唤醒词把指令送 LLM
           - 跟说窗口内: 全文送 LLM */
            int in_window = xr_session_in_window(&ses, now_s());
            char cmd[1024];
            int wake_action = xr_parse_wake_text(text, in_window, cmd, sizeof(cmd));
            if (getenv("XR_TRACE_DECISION"))
                fprintf(stderr, "[xr] 判定: in_window=%d kws=%d guarded=%d "
                        "cooling=%d text=%.40s action=%d\n",
                        in_window, kws_wake_this_utt,
                        xr_session_echo_guarded(&ses, now_s()),
                        xr_session_cooling(&ses, now_s()), text, wake_action);
            /* 声学 KWS 有近音误触，不能把任意 ASR 文本直接放行给 LLM。
               两次文本唤醒词是第二道门；未通过时仍可要求用户重说。 */
            if (!wake_action && !in_window && kws_wake_this_utt) {
                /* KWS 命中时允许唤醒词在句中: "麻烦/请问…"前缀或其听岔
                   (实测"马拉山小瑞小瑞听我说"/"问小瑞小瑞跟我说")。
                   真负例无 小X小X 双单元结构, 变体表+KWS 前置门双保险。
                   KWS 命中时的唤醒门: 双单元优先, 单单位兜底(ASR 抖动掉一个字时
                   双单元门会把整轮丢弃)。单单位档的护栏见 xr_parse_wake_kws。 */
                int single_unit = 0;
                wake_action = xr_parse_wake_kws(text, cmd, sizeof(cmd), &single_unit);
                if (wake_action)
                    fprintf(stderr, "[xr] KWS 命中+%s唤醒单元, %s\n",
                            single_unit ? "单" : "句中双",
                            wake_action == 1 ? "纯唤醒" : cmd);
            }
            /* 窗内 KWS 命中且文本只是唤醒词(含听岔变体) -> 重新纯唤醒而非整句送 LLM
               (实测"小运小玉"整句提交, 云端只能尬答一句). 真指令(有尾巴)不受影响。 */
            if (in_window && kws_wake_this_utt && wake_action == 2) {
                if (xr_is_wake_only_kws(text)) {
                    wake_action = 1;
                    fprintf(stderr, "[xr] 窗内再唤醒(含听岔变体), 归纯唤醒\n");
                }
            }
            if (!wake_action && !in_window && kws_wake_this_utt) {
                if (config->cloud_asr && (g_vqe_mic || config->kws_trust_acoustic)) {
                    /* 云 ASR + VQE 处理链: KWS 声学命中即信(本地 RKNN 在 VQE 链上
                       染色为表外变体, 云 ASR 转写正确——声学证据优先)。
                       音频整句上云, 云端识别全文含唤醒词+指令。
                       例外: 纯唤醒不上云(见 xr_cloud_asr_wake_action)——
                       服务端的唤醒词门只认配置里的精确字符串, 两端变体表不一致
                       时唤醒词会被当普通语句喂给 LLM(实测回"🙂"), 而 LLM 的
                       应答 TTS 还会把随后的指令段整个吃掉(实测云 ASR 只听到
                       "Okay")。纯唤醒只需播确认音+开跟说窗。 */
                    if (xr_cloud_asr_wake_action(text) == 1) {
                        wake_action = 1;
                        fprintf(stderr, "[xr] KWS 命中(纯唤醒形状), 不上云: %s\n", text);
                    } else {
                        snprintf(cmd, sizeof(cmd), "%s", text);
                        wake_action = 2;
                        fprintf(stderr, "[xr] KWS 命中(云ASR模式跳文本门), 指令: %s\n", text);
                    }
                } else {
                    /* 本地模式: 文本里连一个唤醒单元都没有 → 一律忽略 */
                    fprintf(stderr, "[xr] KWS 命中但文本无唤醒单元, 忽略\n");
                }
            }
            kws_wake_this_utt = 0;
            /* 云端优先(2026-10-20): 冷启动轮本地没通过唤醒门时不再一票否决 ——
               整句交云端, 由服务端用云 ASR 的转写判定(标记 wake=required)。
               实测同一句"小瑞小瑞": 本地 RKNN 转成"小想"/"小看看看", 云端转成
               "小度小度"。本地这条只会把真唤醒删掉; 而放宽本地门又会让任何
               说话直达 LLM。判定权交给转写更准的一侧: 服务端不认就回
               silent/nowake, 前端立刻收轮(不答话、不耗 20s)。 */
            int wake_required = 0;
            if (!wake_action && !in_window && config->cloud_asr) {
                snprintf(cmd, sizeof(cmd), "%s", text);
                wake_action = 2;
                wake_required = 1;
                fprintf(stderr, "[xr] 本地未过唤醒门, 交云端判定%s: %s\n",
                        xr_is_wake_candidate(text) ? "(疑似唤醒)" : "", text);
            }
            if (wake_action) {
                if (wake_action == 1) {
                    /* 纯唤醒: 确认音 + 开跟说窗, 不发 LLM */
                    fprintf(stderr, "[xr] (纯唤醒, 等指令)\n");
                    xr_session_awake(&ses, now_s());
                    if (xr_player_start() == 0 && ack_pcm) {
                        xr_player_push_pcm(ack_pcm, ack_n);
                        /* 确认音仍处于 LISTEN; 板麦在室内容易把 ack 余音
                           收成"好"之类短句并误开跟说窗(20:xx 实测), 余音冷却加长。 */
                        xr_session_note_ack(&ses, now_s(), (double)ack_n / 24000.0,
                                            config->ack_cool_ms / 1000.0);
                    }
                    continue;
                }
                fprintf(stderr, "[xr] %s 指令: %s\n", in_window ? "(跟说中)" : "(唤醒)", cmd);
                /* 长度本身不能区分真实短指令和回声，只抑制已知余音词。 */
                if (xr_session_drop_echo(&ses, now_s(), cmd, config->echo_min_cmd_chars)) {
                    fprintf(stderr, "[xr] 窗内超短指令(疑余音), 忽略: %s\n", cmd);
                    continue;
                }
                /* 语音重启(2026-10-16): 精确短语("帮我重启一下手机"类长句
                   不受影响), 开发板远程救活手段。 */
                /* 停止播报: 本地秒停(窗内本地路径的喊停此前会发给 LLM) */
                if (is_stop_speech_command(cmd)) {
                    fprintf(stderr, "[xr] 语音停止播报\n");
                    xr_playback_control_stop(&playback_control, g_vol_pct);
                    be_drop_turn(be);
                    xr_player_drop();
                    if (capture_finish_turn(mic) < 0) goto restart;
                    xr_vad_clear_preroll(&vad);
                    wake_reset();
                    xr_session_end_turn(&ses, now_s(), config->abandon_cool_ms / 1000.0);
                    xr_session_set_echo_guard(&ses, now_s() + config->echo_guard_s);
                    xr_vad_reset_gate(&vad);
                    continue;
                }
                if (is_reboot_command(cmd)) {
                    fprintf(stderr, "[xr] 语音重启\n");
                    fflush(stderr);
                    sleep(1);          /* 给日志落盘留时间 */
                    xr_request_reboot();
                    continue;
                }
                /* 音量指令(本地秒回, 不进 LLM): 判定器极保守, 剥掉音量词后
                   还有实义词的会落到这里之后原样放行。改完即时生效(含本声
                   "好的"确认音), 音量轮也算交互 -> 续跟说窗。 */
                if (config->volume_control) {
                    xr_vol_op_t vop; int vval;
                    if (xr_volume_parse(cmd, &vop, &vval)) {
                        g_vol_pct = xr_volume_next(g_vol_pct, vop, vval);
                        xr_player_set_volume(g_vol_pct);
                        xr_volume_save(volume_state_path(), g_vol_pct);
                        fprintf(stderr, "[xr] 音量指令: %s -> %d%%\n", cmd, g_vol_pct);
                        xr_session_awake(&ses, now_s());
                        if (xr_player_start() == 0 && vol_pcm) {
                            xr_player_push_pcm(vol_pcm, vol_n);
                            xr_session_set_cool(&ses, now_s(),
                                                (double)vol_n / 24000.0
                                                + config->ack_cool_ms / 1000.0);
                            xr_session_set_echo_guard(&ses, now_s()
                                                      + (double)vol_n / 24000.0
                                                      + config->echo_guard_s);
                        }
                        continue;
                    }
                }
                backend_audio_request_t request = {
                    .pcm = utt, .samples = utt_n, .fallback_text = cmd,
                    .wake_required = wake_required,
                };
                int submitted = be_wants_audio(be) ? be_submit_audio(be, &request)
                                                  : be_submit_text(be, cmd);
                if (submitted) {
                    fprintf(stderr, "[xr] ws 断(发送), 重连后补发一次\n");
                    goto restart;   /* backend 已缓存, 重连后补发 */
                }
                xr_session_begin_turn(&ses, now_s());
                ui_turn_reset(&ui);
                /* 指令轮: 起播放线程等回答(20s 识别 0.56s+LLM 首包 0.09s 已够快, 不再插嗯) */
                if (xr_player_start()) { fprintf(stderr, "[xr] 播放线程启动失败\n"); xr_session_abort_turn(&ses); }
            } else {
                fprintf(stderr, "[xr] 未含唤醒词, 忽略\n");
            }
        }
    }

    fprintf(stderr, "[xr] bye\n");
    xr_playback_control_stop(&playback_control, g_vol_pct);
    xr_player_drop();
    be_disconnect(be);
    if (mic) { xr_capture_drop(mic); xr_capture_close(mic); }
    xr_ui_server_close(ui.server);
    return 0;
}
