/* xr_vad_test.c — 标定/触发/预滚回补/判停/拒识/截断断言 */
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "xr_vad.h"

static int failures;
#define CHECK(cond, msg) do { \
    if (!(cond)) { failures++; fprintf(stderr, "FAIL %d: %s\n", __LINE__, (msg)); } \
} while (0)

static int16_t utt[RATE * 15];
static xr_vad_t v;
static int16_t frame[FRAME_SAMPLES];

static void fill(int16_t v16) { for (int i = 0; i < FRAME_SAMPLES; i++) frame[i] = v16; }

static int count_of(const int16_t *buf, size_t n, int16_t want) {
    int c = 0;
    for (size_t i = 0; i < n; i++) if (buf[i] == want) c++;
    return c;
}

static void calibrate(double noise) {
    for (int i = 0; i < 20; i++) xr_vad_calibrate_feed(&v, noise);
    xr_vad_calibrate_finish(&v);
}

int main(void) {
    int started = 0;
    xr_vad_result_t r;

    /* 1. 标定: 中位数, 且 -75 下限 */
    xr_vad_init(&v, utt, sizeof utt / sizeof utt[0]);
    calibrate(-50.0);
    CHECK(fabs(xr_vad_noise_db(&v) - (-50.0)) < 1e-9, "中位数标定应为 -50");
    xr_vad_init(&v, utt, sizeof utt / sizeof utt[0]);
    calibrate(-90.0);
    CHECK(fabs(xr_vad_noise_db(&v) - (-75.0)) < 1e-9, "底噪应被 -75 下限截住");

    /* 2. 能量门触发: 预滚回补(不含当前帧) + 当前帧恰好追加一次, 触发帧不跑端点判定 */
    xr_vad_init(&v, utt, sizeof utt / sizeof utt[0]);
    calibrate(-50.0);
    fill(1000);
    xr_vad_feed(&v, frame, FRAME_SAMPLES, -60.0, &started);   /* 静 */
    xr_vad_feed(&v, frame, FRAME_SAMPLES, -60.0, &started);
    xr_vad_feed(&v, frame, FRAME_SAMPLES, -60.0, &started);
    CHECK(started == 0 && !v.in_utt, "静帧不应开句");
    fill(2000);
    xr_vad_feed(&v, frame, FRAME_SAMPLES, -20.0, &started);
    CHECK(started == 1 && v.in_utt, "响帧应开句");
    {
        size_t un = 0;
        const int16_t *b = xr_vad_utterance(&v, &un);
        /* 环里此刻有 3 静帧 + 触发帧 = 4 条(pre_n=4); 回补其中前 3 条,
           触发帧只由 utt_append 追加一次 —— 不得重复 100ms(会喂给云 ASR 一段插入的音频) */
        CHECK(un == 4 * FRAME_SAMPLES, "开句后应是 3 静帧 + 触发帧一次");
        CHECK(count_of(b, un, 1000) == 3 * FRAME_SAMPLES, "3 个静帧各一次");
        CHECK(count_of(b, un, 2000) == 1 * FRAME_SAMPLES, "触发帧只应出现一次");
    }

    /* 3. 端点: 持续门低于触发门, 尾静音 0.5s 判停 */
    fill(2000);
    for (int i = 0; i < 2; i++) xr_vad_feed(&v, frame, FRAME_SAMPLES, -20.0, &started);
    fill(0);
    for (int i = 0; i < 4; i++) {
        r = xr_vad_feed(&v, frame, FRAME_SAMPLES, -100.0, &started);
        CHECK(r == XR_VAD_CONTINUE, "第 4 个静帧还没到 0.5s");
    }
    r = xr_vad_feed(&v, frame, FRAME_SAMPLES, -100.0, &started);
    CHECK(r == XR_VAD_UTT_DONE, "第 5 个静帧应判停(0.5s 尾静音)");
    CHECK(!v.in_utt, "判停后应退出采集态");

    /* 4. too_short: 开句后一直静 => voice_frames<min => 拒识并抬门 */
    xr_vad_init(&v, utt, sizeof utt / sizeof utt[0]);
    calibrate(-80.0);
    CHECK(fabs(xr_vad_noise_db(&v) - (-75.0)) < 1e-9, "底噪 -75 起");
    fill(2000);
    xr_vad_feed(&v, frame, FRAME_SAMPLES, -60.0, &started);   /* 触发门 = -75+6 = -69 */
    fill(0);
    r = XR_VAD_CONTINUE;
    for (int i = 0; i < 5; i++) r = xr_vad_feed(&v, frame, FRAME_SAMPLES, -100.0, &started);
    CHECK(r == XR_VAD_TOO_SHORT, "未达 min_voice_frames 应拒识");
    CHECK(fabs(xr_vad_noise_db(&v) - (-72.0)) < 1e-9, "拒识后底噪应 +3(-75<-65)");

    /* 5. 抬门只在 noise<-65 时生效 */
    xr_vad_init(&v, utt, sizeof utt / sizeof utt[0]);
    calibrate(-50.0);
    xr_vad_raise_gate(&v);
    CHECK(fabs(xr_vad_noise_db(&v) - (-50.0)) < 1e-9, "noise>=-65 时不应抬门");

    /* 6. 超长截断: 12s */
    xr_vad_init(&v, utt, sizeof utt / sizeof utt[0]);
    calibrate(-80.0);
    fill(2000);
    xr_vad_feed(&v, frame, FRAME_SAMPLES, -60.0, &started);
    r = XR_VAD_CONTINUE;
    for (int i = 0; i < 125 && r != XR_VAD_UTT_DONE; i++)
        r = xr_vad_feed(&v, frame, FRAME_SAMPLES, -20.0, &started);
    CHECK(r == XR_VAD_UTT_DONE, "超 12s 应截断识别");

    /* 7. KWS 强制开句: 不更新预滚环, 当前帧两次, 且本帧跑端点判定 */
    xr_vad_init(&v, utt, sizeof utt / sizeof utt[0]);
    calibrate(-50.0);
    fill(1000);
    for (int i = 0; i < 3; i++) xr_vad_feed(&v, frame, FRAME_SAMPLES, -60.0, &started);
    /* 强制开句后 main 会把同一帧再喂一次 feed(现状控制流: KWS 块设置状态+追加,
       随后的 VAD 块走 else 分支再追加并做端点判定) —— 端点判定必须只发生一次 */
    fill(2000);
    started = 0;
    r = xr_vad_force_start(&v, frame, FRAME_SAMPLES, -55.0, &started);
    CHECK(started == 1 && v.in_utt, "强制开句应生效");
    r = xr_vad_feed(&v, frame, FRAME_SAMPLES, -55.0, &started);
    CHECK(r == XR_VAD_CONTINUE, "强制开句帧的端点判定应在随后的 feed 里做一次");
    CHECK(v.quiet == 1, "强制开句帧的 quiet 只应累加一次(端点判定不得跑两遍)");
    {
        size_t un = 0;
        const int16_t *b = xr_vad_utterance(&v, &un);
        CHECK(un == 5 * FRAME_SAMPLES, "3 静帧 + 触发帧两次, 不得变成三次");
        CHECK(count_of(b, un, 2000) == 2 * FRAME_SAMPLES, "强制开句帧应出现两次");
        CHECK(count_of(b, un, 1000) == 3 * FRAME_SAMPLES, "静帧各出现一次");
    }
    /* 强制开句帧(db=-55 低于持续门 -48)已跑端点判定并把 quiet 记为 1:
       再喂 3 个静帧仍 CONTINUE, 第 4 个静帧即判停 */
    fill(0);
    for (int i = 0; i < 3; i++) {
        r = xr_vad_feed(&v, frame, FRAME_SAMPLES, -100.0, &started);
        CHECK(r == XR_VAD_CONTINUE, "强制开句后的静帧计数应从 1 起算");
    }
    r = xr_vad_feed(&v, frame, FRAME_SAMPLES, -100.0, &started);
    /* 强制开句帧电平低于持续门 => voice_frames 停在 1, 判停时未达下限, 按拒识处理 */
    CHECK(r == XR_VAD_TOO_SHORT, "强制开句后无有效语音应拒识");

    /* 8. 短帧: 回补按 FRAME_SAMPLES 定长(不含当前帧), 当前帧按 n 追加一次 */
    xr_vad_init(&v, utt, sizeof utt / sizeof utt[0]);
    calibrate(-50.0);
    fill(1000);
    for (int i = 0; i < 2; i++) xr_vad_feed(&v, frame, FRAME_SAMPLES, -60.0, &started);
    fill(2000);
    memset(frame + 800, 0, sizeof(int16_t) * 800);   /* 后半段静 */
    started = 0;
    xr_vad_feed(&v, frame, 800, -20.0, &started);
    CHECK(started == 1, "短帧也应触发开句");
    {
        size_t un = 0;
        const int16_t *b = xr_vad_utterance(&v, &un);
        /* 环里 2 静帧 + 当前短帧, pre_n=3; 回补前 2 条静帧, 当前帧按 n 追加一次 */
        CHECK(un == 2 * FRAME_SAMPLES + 800,
              "回补 2 条静帧(不含当前帧) + 当前帧按 n 计入一次");
        /* 追加段必须是这 800 个真实采样: 末样本是 2000, 且总长恰好截到 800 处
           (环槽里 800 之后残留的是上一条 fill(1000), 抄进来就会变成 1000) */
        CHECK(b[un - 1] == 2000, "追加段末样本应是真实采样 2000");
        CHECK(count_of(b, un, 1000) == 2 * FRAME_SAMPLES,
              "只有在环里的 2 条静帧是 1000, 不得混入环槽残留");
    }

    /* 9. 预滚深度: 应回补最近 4 帧(不含当前帧) + 当前帧一次, 更早的帧被环覆盖掉 */
    xr_vad_init(&v, utt, sizeof utt / sizeof utt[0]);
    calibrate(-50.0);
    fill(1000);
    for (int i = 0; i < 8; i++) xr_vad_feed(&v, frame, FRAME_SAMPLES, -60.0, &started);
    fill(2000);
    started = 0;
    xr_vad_feed(&v, frame, FRAME_SAMPLES, -20.0, &started);
    CHECK(started == 1, "响帧应开句");
    /* 8 个静帧 + 触发帧 => 环里 9 条但 pre_n 封顶 5; 回补最近 5 条里的前 4 条(静帧),
       触发帧只追加一次 => 共 5 帧 */
    {
        size_t un = 0;
        const int16_t *b = xr_vad_utterance(&v, &un);
        CHECK(un == 5 * (size_t)FRAME_SAMPLES,
              "开句后应是 预滚N-1 帧 + 触发帧一次");
        CHECK(count_of(b, un, 1000) == 4 * FRAME_SAMPLES,
              "回补的应是最近 N-1 帧, 更早的已被覆盖");
        CHECK(count_of(b, un, 2000) == 1 * FRAME_SAMPLES, "触发帧只应出现一次");
        CHECK(b[0] == 1000 && b[4 * FRAME_SAMPLES] == 2000,
              "回补按时间顺序, 最后一条应是触发帧");
    }

    /* 10. 底噪门必须能回落。
       修复前 noise_db 只有上行路径(标定 fmax / raise_gate +=3 / 空闲时
       `noise_db = noise_db*0.95 + db*0.05` 且仅在 db>noise_db 时生效),
       抬上去就再也降不回来: 在噪声环境待久了门被顶高, 回到安静房间后
       用户说话过不了门, 助手变聋。 */
    xr_vad_init(&v, utt, sizeof utt / sizeof utt[0]);
    calibrate(-50.0);                     /* 噪声房间, 门被标定到 -50 */
    CHECK(fabs(xr_vad_noise_db(&v) - (-50.0)) < 1e-9, "噪声房间底噪应标定到 -50");
    fill(0);
    for (int i = 0; i < 300; i++)        /* 30s 安静房间, 每帧 -70 */
        xr_vad_feed(&v, frame, FRAME_SAMPLES, -70.0, &started);
    CHECK(xr_vad_noise_db(&v) < -65.0,
          "安静 30s 后底噪门必须回落(修复前恒为 -50)");
    CHECK(xr_vad_noise_db(&v) >= -75.0, "回落不得低于 -75 初值下限");

    /* 11. 跟踪必须不对称: 抬升快(0.05/帧)、回落慢(约 10s 半衰),
           否则门会被环境突变来回拖拽。两段都刻意取"高于底噪但低于触发门"
           的电平 —— 底噪只在空闲态跟踪, 一旦开句就不再更新。 */
    xr_vad_init(&v, utt, sizeof utt / sizeof utt[0]);
    calibrate(-70.0);                     /* 触发门 -64 */
    double before = xr_vad_noise_db(&v);
    fill(0);
    started = 0;
    for (int i = 0; i < 10; i++)         /* 1s 低于底噪 4dB 的静帧 */
        xr_vad_feed(&v, frame, FRAME_SAMPLES, -74.0, &started);
    CHECK(!started, "回落段不应开句");
    double drop_1s = before - xr_vad_noise_db(&v);
    CHECK(drop_1s > 0.0 && drop_1s < 1.0,
          "1s 静帧回落应不足 1dB(慢回落)");

    xr_vad_init(&v, utt, sizeof utt / sizeof utt[0]);
    calibrate(-50.0);                     /* 触发门 -44 */
    before = xr_vad_noise_db(&v);
    fill(0);
    started = 0;
    for (int i = 0; i < 10; i++)         /* 1s 高于底噪 4dB 但未过门的响帧 */
        xr_vad_feed(&v, frame, FRAME_SAMPLES, -46.0, &started);
    CHECK(!started, "抬升段不应开句");
    CHECK(xr_vad_noise_db(&v) - before > 2 * drop_1s,
          "同幅偏移下抬升应远快于回落(不对称跟踪)");

    /* 12. 播放后重置底噪门: 不得把底噪打回 -75 悬崖。
       实测(2026-10-21 RK3308): 播完 TTS 立刻说话, 底噪还停在 -73, 持续门 -71
       远低于房间电平 -53 -> 每个静帧都算说话 -> 攒不满 0.5s 静音 -> 每句撞
       12.1s 上限才截断, 云端虽能转对, 但用户要干等十几秒, 体感"反应很慢"。 */
    xr_vad_init(&v, utt, sizeof utt / sizeof utt[0]);
    calibrate(-50.0);
    xr_vad_reset_gate(&v);
    CHECK(fabs(xr_vad_noise_db(&v) - (-50.0)) < 1e-9,
          "重置后应回到标定底噪 -50, 不得回到 -75");
    fill(0);
    started = 0;
    for (int i = 0; i < 5; i++) xr_vad_feed(&v, frame, FRAME_SAMPLES, -60.0, &started);
    CHECK(fabs(xr_vad_noise_db(&v) - (-60.0)) < 1.5,
          "重置后应快速重标定(500ms 内收敛到新底噪)");

    /* 13. 回归: 重置后紧接着说话, 句子必须在 0.5s 静音处收尾, 不得拖到 12.1s */
    xr_vad_init(&v, utt, sizeof utt / sizeof utt[0]);
    calibrate(-50.0);
    xr_vad_reset_gate(&v);
    fill(1000);
    started = 0;
    for (int i = 0; i < 2; i++) xr_vad_feed(&v, frame, FRAME_SAMPLES, -53.0, &started);
    CHECK(started == 0, "重置后的房间底噪帧不应触发开句");
    fill(2000);
    for (int i = 0; i < 3; i++) xr_vad_feed(&v, frame, FRAME_SAMPLES, -24.0, &started);
    fill(0);
    int quiet_frames = 0;
    r = XR_VAD_CONTINUE;
    for (int i = 0; i < 8 && r == XR_VAD_CONTINUE; i++) {
        r = xr_vad_feed(&v, frame, FRAME_SAMPLES, -53.0, &started);
        quiet_frames++;
    }
    CHECK(r == XR_VAD_UTT_DONE && quiet_frames <= 5,
          "重置后开句必须在 0.5s 静音内收尾(修复前会拖到 12.1s 上限)");

    fprintf(stderr, failures ? "xr_vad_test: %d FAILED\n" : "xr_vad_test: OK\n", failures);
    return failures ? 1 : 0;
}