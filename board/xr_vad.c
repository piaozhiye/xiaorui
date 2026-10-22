/* xr_vad.c — 标定/门/预滚/端点 (逻辑与顺序照搬拆分前 xiaorui_live.c 的同名逻辑) */
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "xr_vad.h"

void xr_vad_init(xr_vad_t *v, int16_t *utt_buf, size_t cap) {
    memset(v, 0, sizeof(*v));
    v->noise_db = -75.0;
    v->base_db = -75.0;
    v->recal_left = 0;
    v->margin = 6.0;            /* 2026-10-11: PC喇叭注入电平-47dB/底噪-52dB 实测下调 */
    v->sustain_margin = 2.0;
    v->preroll_frames = 5;
    v->trailing_s = 0.5;
    v->min_voice_frames = 3;
    v->utt_max_s = 12;
    v->utt = utt_buf; v->utt_cap = cap;
}

double xr_vad_frame_db(const int16_t *pcm, int n) {
    int64_t sum = 0;
    for (int i = 0; i < n; i++) sum += (int64_t)pcm[i] * pcm[i];
    double pw = (double)sum / n / (32768.0 * 32768.0);
    return pw > 0 ? 10.0 * log10(pw) : -120.0;
}

void xr_vad_calibrate_feed(xr_vad_t *v, double db) {
    if (v->db_n < 20) v->dbs[v->db_n++] = db;
}

void xr_vad_calibrate_finish(xr_vad_t *v) {
    for (int i = 1; i < v->db_n; i++) {
        double x = v->dbs[i]; int j = i - 1;
        while (j >= 0 && v->dbs[j] > x) { v->dbs[j+1] = v->dbs[j]; j--; }
        v->dbs[j+1] = x;
    }
    v->noise_db = fmax(v->noise_db, v->db_n ? v->dbs[v->db_n / 2] : v->noise_db);
    v->base_db = v->noise_db;      /* 记住开机标定值, reset_gate 回到这里而不是 -75 */
    v->db_n = 0;
}

double xr_vad_noise_db(const xr_vad_t *v) { return v->noise_db; }
void xr_vad_clear_preroll(xr_vad_t *v) { v->pre_n = 0; v->pre_i = 0; }

void xr_vad_configure(xr_vad_t *v, int preroll_frames, double trigger_db, double sustain_db) {
    if (preroll_frames >= 0 && preroll_frames <= XR_VAD_PREROLL_MAX)
        v->preroll_frames = preroll_frames;
    if (trigger_db > 0) v->margin = trigger_db;
    if (sustain_db >= 0) v->sustain_margin = sustain_db;
}
/* 喇叭刚响过: 回到开机标定的底噪并开启快速重标定。
   以前直接打回 -75 初值: 播完 TTS 立刻说话时底噪还停在 -73, 持续门 -71 远低于
   房间电平 -53, 静帧全被算成说话 -> 12.1s 上限截断(2026-10-21 RK3308 实测,
   用户体感"反应很慢")。回到标定值 + 600ms 快速重标定可兼顾"播放后环境变化" */
void xr_vad_reset_gate(xr_vad_t *v) {
    v->noise_db = v->base_db;
    v->recal_left = XR_VAD_RECAL_FRAMES;
}

/* 静默环境误触发: 门再抬, 把空转轮耗掉的跟说窗口时间抢回来 */
void xr_vad_raise_gate(xr_vad_t *v) { if (v->noise_db < -65) v->noise_db += 3; }

/* 回补预滚环里的最近 tail_frames 条(最旧在前)。tail_frames<0 表示全部 pre_n 条。
   能量触发路径必须传 pre_n-1: 触发帧此刻已经写进环里, 后续由 utt_append 按真实
   长度追加一次; 若这里连它一起回补, 唤醒词中间会插进一段 100ms 的重复音频
   (2026-10-21 RK3308 实测: 「小瑞小瑞」被云 ASR 听成「小小瑞瑞」)。
   KWS 强制开句路径不把当前帧写环, 传 -1(全量)。 */
static void utt_begin_tail(xr_vad_t *v, int tail_frames) {
    v->in_utt = 1; v->voice_frames = 1; v->quiet = 0; v->utt_n = 0;
    int n = tail_frames < 0 ? v->pre_n : tail_frames;
    if (n > v->pre_n) n = v->pre_n;
    if (n < 0) n = 0;
    for (int k = 0; k < n; k++) {
        int idx = (v->pre_i - v->pre_n + XR_VAD_PREROLL_MAX + k) % XR_VAD_PREROLL_MAX;
        memcpy(v->utt + v->utt_n, v->pre[idx], (size_t)FRAME_SAMPLES * 2);
        v->utt_n += FRAME_SAMPLES;
    }
}
static void utt_begin(xr_vad_t *v) { utt_begin_tail(v, -1); }
static void utt_append(xr_vad_t *v, const int16_t *f, int n) {
    if (v->utt_n + (size_t)n <= v->utt_cap) {
        memcpy(v->utt + v->utt_n, f, (size_t)n * 2);
        v->utt_n += (size_t)n;
    }
}
static xr_vad_result_t utt_tail(xr_vad_t *v, const int16_t *f, int n, double db) {
    utt_append(v, f, n);
    /* 持续门比触发门低: 韵母尾部电平衰减不再被当静音 */
    double gate = fmax(v->noise_db, -75.0) + v->sustain_margin;
    int done = 0;
    if (db >= gate) {
        v->voice_frames++; v->quiet = 0;
        if (v->voice_frames == 3) fprintf(stderr, "[xr] 检测到说话\n");
    } else if (++v->quiet * FRAME_MS / 1000.0 >= v->trailing_s) done = 1;
    if ((double)v->utt_n / RATE > v->utt_max_s) {
        fprintf(stderr, "[xr] 超长, 截断识别\n");
        done = 1;      /* 超长也识别, 不白说 */
    }
    if (done) {
        v->in_utt = 0; v->quiet = 0;
        if (v->voice_frames < v->min_voice_frames) {
            v->voice_frames = 0;
            xr_vad_raise_gate(v);
            return XR_VAD_TOO_SHORT;
        }
        v->voice_frames = 0;
        return XR_VAD_UTT_DONE;
    }
    return XR_VAD_CONTINUE;
}

xr_vad_result_t xr_vad_feed(xr_vad_t *v, const int16_t *frame, int n, double db,
                            int *started) {
    if (started) *started = 0;
    if (!v->in_utt) {
        /* 环形保留最近 preroll_frames 帧: 触发时回补, 防开头半个字被吞 */
        memcpy(v->pre[v->pre_i], frame, (size_t)n * 2);
        v->pre_i = (v->pre_i + 1) % XR_VAD_PREROLL_MAX;
        if (v->pre_n < v->preroll_frames) v->pre_n++;
        if (db >= v->noise_db + v->margin) {
            /* 当前帧已在环里(上一行刚写入), 只回补它之前的 pre_n-1 条,
               本帧由 utt_append 按真实长度追加一次 —— 不得重复成两段 */
            utt_begin_tail(v, v->pre_n > 0 ? v->pre_n - 1 : 0);
            utt_append(v, frame, n);
            if (started) *started = 1;
            /* 注意: 触发帧不进入 utt_tail —— 现状控制流在 if(!in_utt) 分支内结束 */
            return XR_VAD_CONTINUE;
        }
        /* 底噪跟踪(空闲态, 每 100ms 帧一次)。不对称: 抬升快(0.05/帧, 约 2s
           时间常数)让环境突然变吵时立刻让位; 回落快一档(0.01/帧, 半衰约 7s)
           —— 两个方向的代价不对称: 门偏高 = 安静房间里听不到人(不可用),
           门偏低 = 安静房间里稍多误触发(可接受), 所以回落取快。
           修复前只有抬升路径(且仅在 db>noise_db 时生效), 门一旦抬高就再也
           降不回来 —— 在噪声房间待久后回到安静房间, 用户说话过不了门。
           下限取 -75(init 初值, 与 utt_tail 里 fmax(noise_db,-75) 一致):
           实测板端 ambient 恒为 -75, 放更低只会让门低于真实底噪而误触发。 */
        if (v->recal_left > 0 && db > -75.0) {
            /* 重置后的快速重标定: 对称 alpha=0.5, 6 帧(600ms)内收敛到当前底噪。
               慢速不对称跟踪(0.05/0.01)要十几秒才追得上, 这期间说话就会撞上限 */
            v->recal_left--;
            v->noise_db += (db - v->noise_db) * 0.5;
            return XR_VAD_CONTINUE;
        }
        if (db > v->noise_db) v->noise_db = v->noise_db * 0.95 + db * 0.05;
        else if (db > -75.0) v->noise_db += (db - v->noise_db) * 0.01;
        return XR_VAD_CONTINUE;
    }
    return utt_tail(v, frame, n, db);
}

/* KWS 命中但电平未过门: 声学证据优先. 预滚环不更新(现状控制流跳过入环),
   回补更早帧 + 追加当前帧; 调用方必须紧接着把同一帧喂给 xr_vad_feed,
   由它再追加一次并做端点判定 —— 与拆分前的控制流(KWS 块设状态+追加,
   随后 VAD 块走 else 分支再追加+端点判定)一一对应, 不得在此做端点判定 */
xr_vad_result_t xr_vad_force_start(xr_vad_t *v, const int16_t *frame, int n,
                                   double db, int *started) {
    (void)db;
    if (started) *started = 0;
    if (v->in_utt) return XR_VAD_CONTINUE;
    utt_begin(v);
    utt_append(v, frame, n);
    if (started) *started = 1;
    return XR_VAD_CONTINUE;
}

const int16_t *xr_vad_utterance(const xr_vad_t *v, size_t *n) {
    if (n) *n = v->utt_n;
    return v->utt;
}