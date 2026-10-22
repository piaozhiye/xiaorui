/* backend_hybrid.c -- 云端优先/本地兜底: XR_BACKEND=hybrid 启用.
 *
 * 路由状态机(带滞回, 防 flapping):
 *   云健康(use_cloud=1) --> 指令发云端
 *   发送失败 / 轮内断(未出声) / 空闲期断 / 心跳连续 2 次失败 --> use_cloud=0, 转本地
 *   use_cloud=0 时每 60s 在"下一轮指令提交前"探测云端一次(连接含握手 ≤5s,
 *   只阻塞那一轮; 不在空闲帧循环里探测 -- 会像 WS 阻塞读一样饿死麦克风)
 *
 * 轮内语义:
 *   云端受理后断开且音频未播一字 --> 立即改投本地续答, 用户无感
 *   已播出声 --> 不再补答(重复回答更怪), 合成 stop 正常收轮
 *   本地提交也失败 --> 返回 -1, 前端 restart, local 自带断线补发链路
 *
 * 已知代价: 切换引擎=对话上下文丢失(小智协议无历史注入口), 视为重新开聊.
 * 若无 XR_CLOUD_URL, 退化为纯本地(打日志).
 */
#define _POSIX_C_SOURCE 200809L   /* -std=c11 严格模式下 clock_gettime 需要 */
#include "backend.h"
#include "runtime_config.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <pthread.h>
#include <stdatomic.h>

static double now_s(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
}

typedef struct {
    backend_t base;            /* ops 指针必须首成员 */
    backend_t *cloud;          /* 可为 NULL(未配 XR_CLOUD_URL) */
    backend_t *local;
    int use_cloud;             /* 当前路由: 1=云端(健康) 0=本地 */
    int ping_fails;
    double last_probe;
    double cloud_rx_at;        /* 云端最近一次下行帧的时刻(poll 刷新) */
    backend_t *active;         /* 本轮实际受理者; NULL=空闲 */
    enum {
        HB_TURN_IDLE = 0,      /* 未提交，或上一轮已正常/主动收尾 */
        HB_TURN_WAIT_REPLY,    /* 已提交，尚未收到音频 */
        HB_TURN_STREAMING,     /* 已收到至少一个音频包 */
        HB_TURN_RESCUED,       /* 已从云端补投本地；本轮不可再补投 */
    } turn_state;
    char last_text[1400];
    int16_t *last_pcm;
    size_t last_samples;
    backend_transcribe_fn transcribe;
    backend_t *offer_from;
    int local_down, reconnect_running;
    pthread_t reconnect_thread;
    atomic_int reconnect_done;
    int reconnect_result;
    double local_retry_at;
} hybrid_be_t;

/* Only this worker touches the disconnected local backend. The main thread
 * joins before polling/submitting/disconnecting it; idle capture never waits. */
static void *hb_local_reconnect(void *arg) {
    hybrid_be_t *hb = arg;
    hb->reconnect_result = be_connect(hb->local);
    atomic_store(&hb->reconnect_done, 1);
    return NULL;
}

static void hb_reap_local(hybrid_be_t *hb, int wait) {
    if (!hb->reconnect_running || (!wait && !atomic_load(&hb->reconnect_done))) return;
    pthread_join(hb->reconnect_thread, NULL);
    hb->reconnect_running = 0;
    hb->local_down = hb->reconnect_result < 0;
    hb->local_retry_at = now_s() + 10;
    fprintf(stderr, "[hybrid] 后台本地腿重连%s\n", hb->local_down ? "失败" : "成功");
}

/* 后台腿只有在真的连着时才轮询。
   云腿: use_cloud=0 表示已被 hb_cloud_down() 拆掉(be_disconnect), 再去读它
         只会读到 EV_ERROR —— cb_poll_event 对已关闭的 fd 恒报错。于是空闲期
         每轮都把"已断开的云腿"当成新故障再拆一次再刷一行日志(板上实测
         ~10 行/秒, 1000+ 行), 还会把 last_probe 持续顶掉, 60s 探测再也
         回不了云腿。use_cloud=1 与云腿已连接一一对应。
   本地腿: 已断或正在后台重连时不能碰(重连线程独占它)。 */
static int hb_leg_pollable(hybrid_be_t *hb, backend_t *leg) {
    if (leg == hb->local) return !(hb->local_down || hb->reconnect_running);
    if (leg == hb->cloud) return hb->use_cloud;
    return 1;
}

static int hb_local_ready(hybrid_be_t *hb) {
    hb_reap_local(hb, 1);
    if (!hb->local_down) return 0;
    int rc = be_connect(hb->local);
    hb->local_down = rc < 0;
    return rc < 0 ? -1 : 0;
}

static int hb_turn_active(const hybrid_be_t *hb) {
    return hb->turn_state != HB_TURN_IDLE;
}

static int hb_turn_can_rescue(const hybrid_be_t *hb) {
    return hb->turn_state == HB_TURN_WAIT_REPLY && (hb->last_text[0] || hb->last_pcm);
}

static void hb_turn_clear(hybrid_be_t *hb) {
    hb->active = NULL;
    hb->turn_state = HB_TURN_IDLE;
    hb->last_text[0] = '\0';
    free(hb->last_pcm); hb->last_pcm = NULL;
    hb->last_samples = 0; hb->transcribe = NULL;
}

static int hb_submit_local_fallback(hybrid_be_t *hb) {
    if (!hb->last_text[0] && hb->last_pcm && hb->transcribe) {
        if (hb->transcribe(hb->last_pcm, hb->last_samples, hb->last_text,
                           sizeof(hb->last_text)) < 0) return -1;
        fprintf(stderr, "[hybrid] 音频轮本地 ASR 兜底\n");
    }
    if (!hb->last_text[0]) return -1;
    return hb_local_ready(hb) < 0 ? -1 : be_submit_text(hb->local, hb->last_text);
}

static void hb_cloud_down(hybrid_be_t *hb, const char *why) {
    if (hb->cloud->ops->drop_turn) hb->cloud->ops->drop_turn(hb->cloud);
    be_disconnect(hb->cloud);
    hb->use_cloud = 0;
    hb->ping_fails = 0;
    hb->last_probe = now_s();
    fprintf(stderr, "[hybrid] %s, 切本地\n", why);
}

static int hb_connect(backend_t *b) {
    hybrid_be_t *hb = (hybrid_be_t *)b;
    hb_reap_local(hb, 1);
    hb_turn_clear(hb); hb->ping_fails = 0;
    int lrc = be_connect(hb->local);
    hb->local_down = lrc < 0;
    if (lrc < 0) return -1;          /* 本地是兜底, 它失败才算连接失败(前端 10s 重试) */
    hb->use_cloud = 0;
    hb->last_probe = now_s();
    hb->cloud_rx_at = now_s();
    if (lrc == 2) {                  /* local 断线补发: 上一轮已重发, 别让急连云(≤5s)
                                        白等, 云留到 60s 随轮探测再连 */
        hb->active = hb->local;
        hb->turn_state = HB_TURN_WAIT_REPLY;
        return 2;
    }
    if (hb->cloud) {
        if (be_connect(hb->cloud) == 0) {
            hb->use_cloud = 1;
            fprintf(stderr, "[hybrid] 云端在线, 云端优先\n");
        } else {
            fprintf(stderr, "[hybrid] 云端不可达, 本地模式(%ds 后随轮探测)\n",
                    xr_runtime_config()->cloud_probe_s);
        }
    } else {
        fprintf(stderr, "[hybrid] 未配 XR_CLOUD_URL, 纯本地\n");
    }
    return 0;
}
static void hb_disconnect(backend_t *b) {
    hybrid_be_t *hb = (hybrid_be_t *)b;
    hb_reap_local(hb, 1);
    if (hb->cloud) be_disconnect(hb->cloud);
    be_disconnect(hb->local);
    hb_turn_clear(hb);
}

/* Cancel the actual owner before clearing it; fallback/announcements may be local. */
static int hb_abort(backend_t *b) {
    hybrid_be_t *hb = (hybrid_be_t *)b;
    backend_t *owner = hb->active;
    int rc = owner ? be_abort(owner) : 0;
    hb_turn_clear(hb);
    return rc;
}

static int hb_try_cloud(hybrid_be_t *hb, const char *text) {
    if (be_submit_text(hb->cloud, text) == 0) {
        hb->active = hb->cloud;
        hb->turn_state = HB_TURN_WAIT_REPLY;
        return 0;
    }
    hb_cloud_down(hb, "云端发送失败");
    return -1;
}
static int hb_submit_text(backend_t *b, const char *text) {
    hybrid_be_t *hb = (hybrid_be_t *)b;
    hb_turn_clear(hb);
    snprintf(hb->last_text, sizeof(hb->last_text), "%s", text);
    if (hb->use_cloud && hb->cloud) {
        if (hb_try_cloud(hb, text) == 0) return 0;
        fprintf(stderr, "[hybrid] 本轮转本地\n");
    } else if (hb->cloud && now_s() - hb->last_probe >= xr_runtime_config()->cloud_probe_s) {
        hb->last_probe = now_s();
        if (be_connect(hb->cloud) == 0) {
            hb->use_cloud = 1;
            fprintf(stderr, "[hybrid] 云端恢复, 本轮上云\n");
            if (hb_try_cloud(hb, text) == 0) return 0;
            fprintf(stderr, "[hybrid] 本轮转本地\n");
        } else {
            fprintf(stderr, "[hybrid] 云端探测失败, 本轮本地(%ds 后再探)\n",
                    xr_runtime_config()->cloud_probe_s);
        }
    }
    int rc = hb_local_ready(hb) < 0 ? -1 : be_submit_text(hb->local, text);
    if (rc == 0) { hb->active = hb->local; hb->turn_state = HB_TURN_WAIT_REPLY; }
    return rc;
}

static backend_ev_t hb_poll_event(backend_t *b, unsigned char *buf, size_t cap,
                                  size_t *out_len, char *ev_text, size_t text_cap,
                                  int timeout_ms) {
    hybrid_be_t *hb = (hybrid_be_t *)b;
    hb_reap_local(hb, 0);
    if (hb->local_down && !hb->reconnect_running && now_s() >= hb->local_retry_at) {
        atomic_store(&hb->reconnect_done, 0);
        int rc = pthread_create(&hb->reconnect_thread, NULL, hb_local_reconnect, hb);
        if (rc == 0) hb->reconnect_running = 1;
        else hb->local_retry_at = now_s() + 10;
    }
    if (hb_turn_active(hb) && hb->active) {
        backend_ev_t ev = be_poll_event(hb->active, buf, cap, out_len, ev_text, text_cap, timeout_ms);
        if (ev == EV_ANNOUNCE_REQUEST) hb->offer_from = hb->active;
        if (ev != EV_TIMEOUT && hb->active == hb->cloud) hb->cloud_rx_at = now_s();
        if (ev == EV_ERROR && hb->active == hb->cloud) {
            int had_audio = be_has_audio(hb->cloud);   /* flags 在实例内存里, 断后仍可读 */
            hb->active = NULL;
            hb_cloud_down(hb, "轮内云端断开");
            if (!had_audio && hb_turn_can_rescue(hb)) {
                fprintf(stderr, "[hybrid] 云端断(未出声), 转本地续答\n");
                if (hb_submit_local_fallback(hb) == 0) {
                    hb->active = hb->local;
                    hb->turn_state = HB_TURN_RESCUED;
                    return EV_RESCUED;   /* 重置前端等待预算，本地继续同一轮 */
                }
                hb_turn_clear(hb);
                return EV_ERROR;         /* 本地也失败: 前端 restart */
            }
            fprintf(stderr, "[hybrid] 云端断(已播音), 合成 stop 收轮\n");
            hb_turn_clear(hb);
            return EV_TTS_STOP;          /* 播了一半不补答, 正常收尾 */
        }
        if (ev == EV_TTS_AUDIO && hb->turn_state == HB_TURN_WAIT_REPLY)
            hb->turn_state = HB_TURN_STREAMING;
        /* 云端零音频收轮 = 服务端有意不答(噪声/纯唤醒/空识别), 不是失败。
           2026-10-20 起不再"翻本地补答": 云腿已是板内 8001, 替它用另一套
           模型答一轮, 用户听到的是莫名其妙的回复(实测"嗯好好好的"被本地
           LLM 答成"好的，请问有什么可…"), 而且补答走纯文本提交, 会绕过
           wake=required 唤醒门。真断线仍由上面的 EV_ERROR 兜底。
           注: 这里不能再用 be_has_audio() 判"有没有出声" —— 收到 stop 时
           本轮已被标成 FINISHED, has_audio 按设计恒为假(曾据此打出误导性
           日志"云端零音频收轮", 其实那一轮有声)。*/
        if (ev == EV_SILENT || ev == EV_TTS_STOP || ev == EV_VOLUME) hb_turn_clear(hb);
        /* 音量指令轮零音频, 不能走 drop_turn 的静默救援路径(会把同一句
           补投本地 LLM 造成双回答), 这里直接清轮。 */
        return ev;
    }
    /* 空闲: 轮询当前路由方, 云端断开静默切本地(不触发前端 restart) */
    backend_t *route = (hb->use_cloud && hb->cloud) ? hb->cloud : hb->local;
    if (route == hb->local && (hb->local_down || hb->reconnect_running)) return EV_TIMEOUT;
    backend_ev_t ev = be_poll_event(route, buf, cap, out_len, ev_text, text_cap, timeout_ms);
    if (ev == EV_ANNOUNCE_REQUEST) hb->offer_from = route;
    if (ev == EV_ANNOUNCE) {
        hb->active = route;
        hb->turn_state = HB_TURN_WAIT_REPLY;
    }
    if (ev != EV_TIMEOUT && route == hb->cloud) hb->cloud_rx_at = now_s();
    if (ev == EV_ERROR && route == hb->cloud) {
        hb_cloud_down(hb, "空闲期云端断开");
        return EV_TIMEOUT;
    }
    /* 后台腿也要有人喂: 服务端空闲超时会关掉不被路由的那条连接, 无人读它
       的 close 帧就静默烂掉(deaf-mode 排查 2026-10-16)。顺带让本地腿的
       定时主动播报不再依赖"恰好被路由"。 */
    if (ev == EV_NONE || ev == EV_TIMEOUT) {
        backend_t *other = (route == hb->cloud) ? hb->local : hb->cloud;
        if (other && hb_leg_pollable(hb, other)) {
            backend_ev_t ev2 = be_poll_event(other, buf, cap, out_len, ev_text, text_cap, 0);
            if (ev2 == EV_ANNOUNCE_REQUEST) hb->offer_from = other;
            if (ev2 == EV_ANNOUNCE) {
                hb->active = other;
                hb->turn_state = HB_TURN_WAIT_REPLY;
            }
            if (ev2 == EV_ERROR) {
                if (other == hb->cloud) hb_cloud_down(hb, "后台云端腿断开");
                else {
                    be_disconnect(hb->local);
                    hb->local_down = 1;
                    hb->local_retry_at = now_s();
                    fprintf(stderr, "[hybrid] 后台本地腿已断, 安排后台重连\n");
                }
                return EV_TIMEOUT;   /* 后台腿故障不折腾当前服务中的路由 */
            }
            if (ev2 != EV_NONE && ev2 != EV_TIMEOUT) return ev2;
        }
    }
    return ev;
}

static int hb_ping(backend_t *b) {
    hybrid_be_t *hb = (hybrid_be_t *)b;
    /* 后台腿也要心跳: 服务端会把"一直没人 ping"的连接按空闲回收, 于是后台本地腿
       每轮都断一次再后台重连(2026-10-20 实测日志刷屏), 而且留下"云腿刚断、本地腿
       恰好在重连"的空窗。ping 失败不在这里处理 —— 该腿真要断, 轮询会报 EV_ERROR。 */
    backend_t *other = (hb->use_cloud && hb->cloud) ? hb->local : hb->cloud;
    if (other && hb_leg_pollable(hb, other)) be_ping(other);
    if (hb->use_cloud && hb->cloud) {
        double now = now_s();
        /* 下行时龄判死: 黑洞链路下 send 永远成功, 只有"很久没收到任何帧"
           (含每 25s 一次的 pong) 能暴露. 3 个 ping 周期无下行才翻, 防偶发抖动 */
        if (now - hb->cloud_rx_at > xr_runtime_config()->cloud_rx_dead_s) {
            hb_cloud_down(hb, "云端无下行帧超过阈值(疑似死链)");
            return 0;
        }
        if (be_ping(hb->cloud) == 0) { hb->ping_fails = 0; return 0; }
        if (++hb->ping_fails >= xr_runtime_config()->cloud_ping_failures)
            hb_cloud_down(hb, "云端心跳连续失败");
        return 0;    /* 本地健在, 不让前端重启 */
    }
    return (hb->local_down || hb->reconnect_running) ? 0 : be_ping(hb->local);
}
static int hb_has_audio(backend_t *b) {
    hybrid_be_t *hb = (hybrid_be_t *)b;
    return hb->active ? be_has_audio(hb->active) : 0;
}
static int hb_seen_stt(backend_t *b) {
    hybrid_be_t *hb = (hybrid_be_t *)b;
    return hb->active ? be_seen_stt(hb->active) : 0;
}
static int hb_drop_turn(backend_t *b) {
    /* 前端 8s/20s 兜底弃轮时调用, 常规清轮。
       2026-10-20 起不再"翻本地补答": 云腿已是板内 8001, 而"服务端有意不答话"
       (噪声/纯唤醒/空识别)已由 silent 回执明确表达; 替它换一套模型再答一轮,
       用户听到的是莫名其妙的回复(实测"嗯好好好的"被本地 LLM 答成
       "好的，请问有什么可…")。该路径还是纯文本提交, 会绕过 wake=required
       唤醒门 —— 云端判"没叫我"的轮次会被本地重新答一遍。真断线仍由轮内
       EV_ERROR 兜底。 */
    hybrid_be_t *hb = (hybrid_be_t *)b;
    if (hb->active && hb->active->ops->drop_turn) hb->active->ops->drop_turn(hb->active);
    hb_turn_clear(hb);
    return 0;
}

static int hb_wants_audio(backend_t *b) {
    hybrid_be_t *hb = (hybrid_be_t *)b;
    return hb->use_cloud && hb->cloud && be_wants_audio(hb->cloud);
}
static int hb_submit_audio(backend_t *b, const backend_audio_request_t *r) {
    hybrid_be_t *hb = (hybrid_be_t *)b;
    if (!r || !r->pcm || !r->samples || r->samples > 16000u * 15) return -1;
    hb_turn_clear(hb);
    hb->last_pcm = malloc(r->samples * sizeof(*r->pcm));
    if (!hb->last_pcm) return -1;
    memcpy(hb->last_pcm, r->pcm, r->samples * sizeof(*r->pcm));
    hb->last_samples = r->samples; hb->transcribe = r->transcribe;
    if (r->fallback_text)
        snprintf(hb->last_text, sizeof(hb->last_text), "%s", r->fallback_text);
    hb->turn_state = HB_TURN_WAIT_REPLY;
    if (hb->use_cloud && hb->cloud) {
        if (be_submit_audio(hb->cloud, r) == 0) {
            hb->active = hb->cloud;
            return 0;
        }
        hb_cloud_down(hb, "云端音频发送失败");
    }
    if (hb_submit_local_fallback(hb) == 0) {
        hb->active = hb->local; hb->turn_state = HB_TURN_RESCUED;
        return 0;
    }
    hb_turn_clear(hb);
    return -1;
}

static int hb_announce_reply(backend_t *b, const char *id, int accepted) {
    hybrid_be_t *hb = (hybrid_be_t *)b;
    backend_t *source = hb->offer_from;
    hb->offer_from = NULL;
    if (!source || !source->ops->announce_reply) return -1;
    int rc = source->ops->announce_reply(source, id, accepted);
    if (rc == 0 && accepted) {
        hb->active = source;
        hb->turn_state = HB_TURN_WAIT_REPLY;
    }
    return rc;
}
static const backend_ops_t HYBRID_OPS = {
    .create = NULL, .connect = hb_connect, .disconnect = hb_disconnect,
    .abort = hb_abort,
    .submit_text = hb_submit_text, .poll_event = hb_poll_event,
    .ping = hb_ping, .has_audio = hb_has_audio,
    .seen_stt = hb_seen_stt, .drop_turn = hb_drop_turn,
    .wants_audio = hb_wants_audio, .submit_audio = hb_submit_audio,
    .announce_reply = hb_announce_reply,
};
backend_t *be_open_hybrid(void) {
    backend_t *local = be_open_local();
    if (!local) return NULL;
    backend_t *cloud = be_open_cloud();   /* 无 XR_CLOUD_URL 时为 NULL, 退化纯本地 */
    hybrid_be_t *hb = calloc(1, sizeof(hybrid_be_t));
    if (!hb) return NULL;
    hb->base.ops = &HYBRID_OPS;
    hb->cloud = cloud;
    hb->local = local;
    return (backend_t *)hb;
}
