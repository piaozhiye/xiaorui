/* Isolated regression probe: exercise repeated online streams and in-process
 * offline RKNN refinement without opening ALSA or touching the live process.
 * Reuse the exact API bindings/configuration from xiaorui_live.c. */
#define main xr_live_main
#include "xiaorui_live.c"
#undef main

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "usage: %s 16k-mono-s16.wav rounds\n", argv[0]);
        return 2;
    }
    int rounds = atoi(argv[2]);
    if (rounds < 1 || rounds > 20) return 2;
    FILE *fp = fopen(argv[1], "rb");
    if (!fp) { perror(argv[1]); return 2; }
    unsigned char wh[12];
    if (fread(wh, 1, sizeof(wh), fp) != sizeof(wh) ||
        memcmp(wh, "RIFF", 4) || memcmp(wh + 8, "WAVE", 4)) {
        fprintf(stderr, "expected RIFF/WAVE\n");
        fclose(fp);
        return 2;
    }
    int valid_fmt = 0;
    size_t count = 0;
    for (;;) {
        unsigned char chunk[8];
        if (fread(chunk, 1, sizeof(chunk), fp) != sizeof(chunk)) break;
        uint32_t size = (uint32_t)chunk[4] | (uint32_t)chunk[5] << 8 |
                        (uint32_t)chunk[6] << 16 | (uint32_t)chunk[7] << 24;
        if (!memcmp(chunk, "fmt ", 4) && size >= 16) {
            unsigned char fmt[16];
            if (fread(fmt, 1, sizeof(fmt), fp) != sizeof(fmt)) break;
            valid_fmt = fmt[0] == 1 && fmt[1] == 0 && fmt[2] == 1 && fmt[3] == 0 &&
                        fmt[4] == 0x80 && fmt[5] == 0x3e && fmt[14] == 16 && fmt[15] == 0;
            if (fseek(fp, (long)(size - 16 + (size & 1)), SEEK_CUR)) break;
        } else if (!memcmp(chunk, "data", 4)) {
            count = size / 2;
            break;
        } else if (fseek(fp, (long)(size + (size & 1)), SEEK_CUR)) break;
    }
    if (!valid_fmt || !count) {
        fprintf(stderr, "expected 16k/mono/S16_LE WAV with data\n");
        fclose(fp);
        return 2;
    }
    int16_t *pcm = malloc(count * sizeof(*pcm));
    if (!pcm || fread(pcm, sizeof(*pcm), count, fp) != count) {
        fprintf(stderr, "WAV data truncated\n");
        fclose(fp);
        free(pcm);
        return 2;
    }
    fclose(fp);
    if (sherpa_load() || offline_init()) return 2;
    xr_recog_cfg_t cfg = {0};
    cfg.feat.sample_rate = RATE; cfg.feat.feature_dim = 80;
    cfg.model.paraformer.encoder = "/data/rknn-test/sherpa/model-streaming/encoder.int8.onnx";
    cfg.model.paraformer.decoder = "/data/rknn-test/sherpa/model-streaming/decoder.int8.onnx";
    cfg.model.tokens = "/data/rknn-test/sherpa/model-streaming/tokens.txt";
    cfg.model.num_threads = 2; cfg.model.provider = "cpu";
    cfg.decoding_method = "greedy_search";
    void *online = s_create_recog(&cfg);
    if (!online) { fprintf(stderr, "online init failed\n"); return 2; }
    float wave[FRAME_SAMPLES];
    for (int round = 1; round <= rounds; ++round) {
        void *stream = s_create_stream(online);
        if (!stream) return 2;
        for (size_t off = 0; off + FRAME_SAMPLES <= count; off += FRAME_SAMPLES) {
            for (int k = 0; k < FRAME_SAMPLES; ++k) wave[k] = pcm[off + k] / 32768.0f;
            s_accept_wave(stream, RATE, wave, FRAME_SAMPLES);
        }
        int loops = 0;
        while (s_is_ready(online, stream) && loops++ < 5000) s_decode(online, stream);
        const char *draft = s_get_result_json(online, stream);
        fprintf(stderr, "[probe] round=%d online_loops=%d draft=%.120s\n",
                round, loops, draft ? draft : "(null)");
        s_destroy_stream(stream);

        double started = now_s();
        void *offline = off_create_stream(off_recog);
        if (!offline) return 2;
        for (size_t off = 0; off < count; off += FRAME_SAMPLES) {
            size_t n = count - off < FRAME_SAMPLES ? count - off : FRAME_SAMPLES;
            for (size_t k = 0; k < n; ++k) wave[k] = pcm[off + k] / 32768.0f;
            off_accept_wave(offline, RATE, wave, (int)n);
        }
        off_decode(off_recog, offline);
        const char *result = off_result_json(offline);
        fprintf(stderr, "[probe] round=%d refine=%.120s elapsed=%.3fs\n",
                round, result ? result : "(null)", now_s() - started);
        if (!result) return 1;
        off_free_result_json(result);
        off_destroy_stream(offline);
    }
    free(pcm);
    s_destroy_recog(online);
    off_destroy_recog(off_recog);
    dlclose(sherpa_lib);
    return 0;
}
