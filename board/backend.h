/* backend.h -- 语音前端后端接口 (Step1 解耦: local 实现保持现行为,
 * Step2 将提供 backend_cloud 按小智 v1 协议上行 Opus 音频).
 *
 * 事件模型(统一): poll_event 返回一枚事件, 主循环只看事件类型:
 *   EV_STT        服务端已受理本轮(识别文本回执)
 *   EV_TTS_START  一句回答开始(带文本)
 *   EV_TTS_AUDIO  一段 opus 音频(24k mono 载荷)
 *   EV_TTS_STOP   本轮回答结束
 *   EV_TIMEOUT    等待超时(非错误, 可继续等)
 *   EV_ERROR      连接错误(应重连)
 *   EV_RESCUED    同轮换路完成(重置等待预算，不新开一轮)
 *   EV_VOLUME     服务端判定本轮是音量指令(ev_text="up|down|max|min|set:NN"),
 *                 客户端本地执行(软件增益+确认音)并按回答收尾
 *   EV_ANNOUNCE   服务端主动播报(定时提醒到点): 客户端进入播放态收音频
 */#ifndef XR_BACKEND_H
#define XR_BACKEND_H
#include <stddef.h>
#include <stdint.h>

typedef int (*backend_transcribe_fn)(const int16_t *, size_t, char *, size_t);
typedef struct {
    const int16_t *pcm; /* 16k mono S16; borrowed for submit, hybrid retains a copy. */
    size_t samples;
    const char *fallback_text; /* optional text already validated by local wake gate */
    /* 冷启动轮: 这段开头音频必须先过服务端唤醒门(wake=required)。本地 RKNN
       的转写只当预筛, 判定用云 ASR 的转写 —— 否则本地的"小想/小看看看"会把
       真唤醒一票否决, 而放宽又会让任何说话都直达 LLM。 */
    int wake_required;
    backend_transcribe_fn transcribe; /* lazy local fallback, called only before first audio */
} backend_audio_request_t;

typedef enum {
    EV_NONE = 0, EV_STT, EV_TTS_START, EV_TTS_AUDIO, EV_TTS_STOP,
    EV_TIMEOUT, EV_ERROR, EV_RESCUED, EV_VOLUME, EV_ANNOUNCE, EV_REBOOT,
    EV_ANNOUNCE_REQUEST,
    /* 服务端静默丢弃(纯唤醒/噪声): 本轮不是命令, 前端应立即收轮回聆听,
       不要等 response_timeout(20s)。ev_text = "wake" | "noise"。 */
    EV_SILENT
} backend_ev_t;

typedef struct backend backend_t;

typedef struct {
    backend_t *(*create)(void);            /* 读配置/建实例 */
    int        (*connect)(backend_t *);    /* 握手(hello/listen start); 0=ok */
    void       (*disconnect)(backend_t *);
    /* 打断当前轮但**保持连接**: 服务端收到 {"type":"abort"} 会停 LLM/TTS 并
       置 client_abort(音乐流的 cancelled() 立刻收敛)。用于"停止音乐"——
       以前只能 disconnect 整条 WS, 结果前端重连+重新标定(用户观感: 播放
       的时候设备被切了一刀)。返回 0=已发出, -1=发送失败。 */
    int        (*abort)(backend_t *);
    /* 提交一轮指令文本(local: detect+text); 返回 0=ok, -1=发送失败 */
    int        (*submit_text)(backend_t *, const char *text);
    /* 轮询一枚事件; timeout_ms=0 用于采音期非阻塞检查,
       回答期可传正数等待; EV_TIMEOUT 表示暂无事件 */
    backend_ev_t (*poll_event)(backend_t *, unsigned char *buf, size_t cap,
                               size_t *out_len, char *ev_text, size_t text_cap,
                               int timeout_ms);
    /* 心跳(LISTEN 空闲期调用); 返回 0=ok */
    int        (*ping)(backend_t *);
    /* 供前端记录首包延迟: 本轮是否已收到过音频 */
    int        (*has_audio)(backend_t *);
    /* 本轮服务端是否已受理(local: stt 回执; cloud: stt/llm/tts 任一文本事件).
       迟到 stop 过滤与空回答兜底判定共用 */
    int        (*seen_stt)(backend_t *);
    /* 放弃当前轮: 清未补发指令与轮内标志. 仅 hybrid 交叉兜底时对失败侧调用,
       前端不调(否则会废掉 local 断线补发语义).
       返回 1 = 后端已换路补发本轮(前端应重置等待继续本轮), 0 = 已弃轮 */
    int        (*drop_turn)(backend_t *);
    int        (*wants_audio)(backend_t *);
    int        (*submit_audio)(backend_t *, const backend_audio_request_t *);
    int        (*announce_reply)(backend_t *, const char *request_id, int accepted);
} backend_ops_t;

struct backend {
    const backend_ops_t *ops;
};

/* 薄封装: 主循环只调这些 */
int  be_connect(backend_t *b);
void be_disconnect(backend_t *b);
/* 打断当前轮但保持连接(见 ops.abort)。返回 0=已发出。 */
int  be_abort(backend_t *b);
int  be_submit_text(backend_t *b, const char *text);
backend_ev_t be_poll_event(backend_t *b, unsigned char *buf, size_t cap,
                           size_t *out_len, char *ev_text, size_t text_cap,
                           int timeout_ms);
int  be_ping(backend_t *b);
int be_wants_audio(backend_t *b);
int be_submit_audio(backend_t *b, const backend_audio_request_t *request);
int be_announce_reply(backend_t *b, const char *request_id, int accepted);
int  be_has_audio(backend_t *b);
int  be_seen_stt(backend_t *b);  /* 本轮服务端是否已受理(空回答兜底判定用) */
int  be_drop_turn(backend_t *b); /* 前端 8s/20s 兜底弃轮时调用: 返回 1=后端已换路
                                    补发本轮(前端继续等), 0=已弃轮 */
backend_t *be_open_local(void);  /* Step1: local 后端实例(默认) */
backend_t *be_open_cloud(void);  /* Step2: 云端 LLM 后端(小智 v1 detect+text,
                                    XR_BACKEND=cloud 时启用; 端点见 backend_cloud.c) */
backend_t *be_open_hybrid(void); /* 云端优先/本地兜底(XR_BACKEND=hybrid):
                                    云健康走云端, 发送/轮内/空闲断与心跳连续失败
                                    逐轮转本地, 60s 探测恢复; 详见 backend_hybrid.c */
#endif
