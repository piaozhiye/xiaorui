/* RK3576 WebSocket/Opus playback smoke test. No microphone or wake word yet. */
#define _POSIX_C_SOURCE 200809L
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MAX_FRAME (1024 * 1024)
#define PLAY_RATE 48000       /* libopus decode output and ALSA playback rate */
#define PLAY_CHANNELS 2      /* ES8388 playback path is stereo */
#define MAX_FRAME_SAMPLES 5760 /* 120 ms at 48 kHz; opus_decode requires this capacity */

typedef void *(*opus_create_fn)(int, int, int *);
typedef int (*opus_decode_fn)(void *, const unsigned char *, int, short *, int, int);
typedef void (*opus_destroy_fn)(void *);

static int write_all(int fd, const void *data, size_t len) {
    const unsigned char *p = data;
    while (len) {
        ssize_t n = write(fd, p, len);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

static int read_all(int fd, void *data, size_t len) {
    unsigned char *p = data;
    while (len) {
        ssize_t n = read(fd, p, len);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

static int ws_send(int fd, unsigned char opcode, const void *data, size_t len) {
    unsigned char h[14];
    size_t n = 0;
    h[n++] = 0x80 | opcode;
    if (len < 126) h[n++] = 0x80 | (unsigned char)len;
    else if (len < 65536) {
        h[n++] = 0x80 | 126;
        h[n++] = (unsigned char)(len >> 8);
        h[n++] = (unsigned char)len;
    } else return -1;
    unsigned char mask[4];
    for (int i = 0; i < 4; i++) mask[i] = (unsigned char)rand();
    memcpy(h + n, mask, 4);
    n += 4;
    if (write_all(fd, h, n)) return -1;
    const unsigned char *p = data;
    unsigned char chunk[1024];
    for (size_t off = 0; off < len;) {
        size_t count = len - off < sizeof(chunk) ? len - off : sizeof(chunk);
        for (size_t i = 0; i < count; i++) chunk[i] = p[off + i] ^ mask[(off + i) & 3];
        if (write_all(fd, chunk, count)) return -1;
        off += count;
    }
    return 0;
}

static int ws_recv(int fd, unsigned char *opcode, unsigned char *buf, size_t *length) {
    unsigned char h[2];
    if (read_all(fd, h, 2)) return -1;
    if (!(h[0] & 0x80)) { fprintf(stderr, "fragmented frame unsupported\n"); return -1; }
    *opcode = h[0] & 15;
    uint64_t len = h[1] & 127;
    if (len == 126) {
        unsigned char b[2];
        if (read_all(fd, b, 2)) return -1;
        len = ((uint64_t)b[0] << 8) | b[1];
    } else if (len == 127) {
        unsigned char b[8];
        if (read_all(fd, b, 8)) return -1;
        len = 0;
        for (int i = 0; i < 8; i++) len = (len << 8) | b[i];
    }
    if (len > MAX_FRAME) { fprintf(stderr, "frame too large\n"); return -1; }
    unsigned char mask[4] = {0};
    if ((h[1] & 0x80) && read_all(fd, mask, 4)) return -1;
    if (read_all(fd, buf, (size_t)len)) return -1;
    if (h[1] & 0x80) for (size_t i = 0; i < (size_t)len; i++) buf[i] ^= mask[i & 3];
    buf[len] = 0;
    *length = (size_t)len;
    return 0;
}

static int connect_server(const char *host, int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    struct timeval tv = {.tv_sec = 0, .tv_usec = 700 * 1000};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port)};
    if (inet_pton(AF_INET, host, &addr.sin_addr) != 1 ||
        connect(fd, (struct sockaddr *)&addr, sizeof(addr))) {
        close(fd);
        return -1;
    }
    return fd;
}

static int handshake(int fd, const char *host, int port) {
    char req[1024];
    int n = snprintf(req, sizeof(req),
        "GET /xiaorui/v1/ HTTP/1.1\r\n"
        "Host: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "device-id: AA:BB:CC:DD:EE:44\r\n"
        "client-id: AA:BB:CC:DD:EE:44-board\r\n\r\n", host, port);
    if (n < 0 || n >= (int)sizeof(req) || write_all(fd, req, (size_t)n)) return -1;
    char reply[4096];
    size_t used = 0;
    while (used + 1 < sizeof(reply)) {
        if (read_all(fd, reply + used, 1)) return -1;
        used++;
        reply[used] = 0;
        if (used >= 4 && !memcmp(reply + used - 4, "\r\n\r\n", 4)) break;
    }
    if (strncmp(reply, "HTTP/1.1 101 ", 13) || !strstr(reply, "Sec-WebSocket-Accept:")) {
        fprintf(stderr, "WebSocket upgrade failed: %.100s\n", reply);
        return -1;
    }
    puts("[board] WebSocket upgrade OK");
    return 0;
}

static int start_player(const char *speaker, pid_t *pid) {
    int pipefd[2];
    if (pipe(pipefd)) return -1;
    *pid = fork();
    if (*pid < 0) { close(pipefd[0]); close(pipefd[1]); return -1; }
    if (*pid == 0) {
        dup2(pipefd[0], STDIN_FILENO);
        close(pipefd[0]); close(pipefd[1]);
        /* aplay 在句间断粮时刷 "underrun!!!" 到 stderr, 只会是噪音, 丢弃 */
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) { dup2(devnull, STDERR_FILENO); close(devnull); }
        execlp("aplay", "aplay", "-D", speaker, "-q", "-t", "raw", "-f", "S16_LE",
               "-r", "48000", "-c", "2", (char *)NULL);
        _exit(127);
    }
    close(pipefd[0]);
    return pipefd[1];
}

static int json_escape(const char *src, char *dst, size_t cap) {
    size_t n = 0;
    for (const unsigned char *p = (const unsigned char *)src; *p; p++) {
        const char *escaped = NULL;
        if (*p == '"') escaped = "\\\"";
        else if (*p == '\\') escaped = "\\\\";
        else if (*p == '\n') escaped = "\\n";
        else if (*p == '\r') escaped = "\\r";
        else if (*p == '\t') escaped = "\\t";
        if (escaped) {
            size_t len = strlen(escaped);
            if (n + len >= cap) return -1;
            memcpy(dst + n, escaped, len);
            n += len;
        } else {
            if (*p < 32 || n + 1 >= cap) return -1;
            dst[n++] = (char)*p;
        }
    }
    dst[n] = 0;
    return 0;
}

static int check_wav_level(const char *path) {
    FILE *fp = fopen(path, "rb");
    if (!fp) { perror(path); return 2; }
    unsigned char header[12];
    if (fread(header, 1, 12, fp) != 12 || memcmp(header, "RIFF", 4) ||
        memcmp(header + 8, "WAVE", 4)) { fputs("invalid WAV\n", stderr); fclose(fp); return 2; }
    /* 用最响的 1 s 窗判定: 短促说话混在 6 s 静音里, 全文件平均会被拉低误拦 */
    uint64_t sumsq = 0, samples = 0, win_samples = 0;
    double best_power = 0.0, avg_power_sum = 0.0;
    int windows = 0;
    while (1) {
        unsigned char chunk[8];
        if (fread(chunk, 1, 8, fp) != 8) break;
        uint32_t size = (uint32_t)chunk[4] | ((uint32_t)chunk[5] << 8) |
                        ((uint32_t)chunk[6] << 16) | ((uint32_t)chunk[7] << 24);
        if (!memcmp(chunk, "data", 4)) {
            for (uint32_t i = 0; i + 1 < size; i += 2) {
                unsigned char b[2];
                if (fread(b, 1, 2, fp) != 2) break;
                int16_t v = (int16_t)((uint16_t)b[0] | ((uint16_t)b[1] << 8));
                sumsq += (int64_t)v * v;
                samples++; win_samples++;
                if (win_samples == 16000) {
                    double p = (double)sumsq / win_samples / (32768.0 * 32768.0);
                    if (p > best_power) best_power = p;
                    avg_power_sum += p; windows++;
                    sumsq = 0; win_samples = 0;
                }
            }
            if (win_samples) {
                double p = (double)sumsq / win_samples / (32768.0 * 32768.0);
                if (p > best_power) best_power = p;
                avg_power_sum += p; windows++;
            }
            break;
        }
        if (fseek(fp, (long)size + (size & 1), SEEK_CUR)) break;
    }
    fclose(fp);
    if (!samples) { fputs("WAV has no samples\n", stderr); return 2; }
    double db = best_power > 0 ? 10.0 * log10(best_power) : -120.0;
    double avg_db = windows ? 10.0 * log10(avg_power_sum / windows + 1e-12) : -120.0;
    printf("[board] mic peak-1s RMS %.1f dBFS, avg %.1f dBFS (%llu samples)\n", db, avg_db,
           (unsigned long long)samples);
    return db >= -60.0 ? 0 : 4;
}

/* 把多个 16k 单声道 wav 的 data 块拼成带头的 16k/mono wav 写 stdout (VAD 掐尾分片用) */
static uint32_t wr_str(FILE *f, const char *s, uint32_t n) { return fwrite(s, 1, n, f) == n ? n : 0; }

static int concat_wav_data(int argc, char **argv) {
    /* 先把所有片段的 PCM 拼到临时文件, 得到总长后写带正确 size 的 wav 头 */
    const char *tmp = "/tmp/.xz_concat.pcm";
    FILE *out = fopen(tmp, "wb");
    if (!out) return 2;
    size_t total = 0;
    for (int i = 0; i < argc; i++) {
        FILE *fp = fopen(argv[i], "rb");
        if (!fp) continue;
        unsigned char h[12];
        if (fread(h, 1, 12, fp) != 12 || memcmp(h, "RIFF", 4) || memcmp(h + 8, "WAVE", 4)) { fclose(fp); continue; }
        while (1) {
            unsigned char ch[8];
            if (fread(ch, 1, 8, fp) != 8) break;
            uint32_t size = (uint32_t)ch[4] | ((uint32_t)ch[5] << 8) |
                            ((uint32_t)ch[6] << 16) | ((uint32_t)ch[7] << 24);
            if (!memcmp(ch, "data", 4)) {
                unsigned char buf[4096];
                while (size) {
                    uint32_t take = size < sizeof(buf) ? size : sizeof(buf);
                    if (fread(buf, 1, take, fp) != take) break;
                    fwrite(buf, 1, take, out);
                    total += take;
                    size -= take;
                }
                break;
            }
            if (fseek(fp, (long)size + (size & 1), SEEK_CUR)) break;
        }
        fclose(fp);
    }
    fclose(out);
    if (!total) { unlink(tmp); return 4; }

    /* 标准 44 字节 PCM wav 头: 16000 Hz, mono, 16 bit */
    uint32_t sr = 16000, byte_rate = sr * 2;
    FILE *o = stdout;
    wr_str(o, "RIFF", 4);
    uint32_t v = 36 + (uint32_t)total; fwrite(&v, 4, 1, o);
    wr_str(o, "WAVEfmt ", 8);
    v = 16; fwrite(&v, 4, 1, o);
    uint16_t u16 = 1; fwrite(&u16, 2, 1, o);          /* PCM */
    u16 = 1; fwrite(&u16, 2, 1, o);                   /* mono */
    fwrite(&sr, 4, 1, o);
    fwrite(&byte_rate, 4, 1, o);
    u16 = 2; fwrite(&u16, 2, 1, o);                   /* block align */
    u16 = 16; fwrite(&u16, 2, 1, o);                  /* bits */
    wr_str(o, "data", 4);
    v = (uint32_t)total; fwrite(&v, 4, 1, o);

    FILE *in = fopen(tmp, "rb");
    unsigned char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), in)) > 0) fwrite(buf, 1, n, o);
    fclose(in);
    unlink(tmp);
    return 0;
}

/* VAD 掐尾: 监视 arecord 正在写的 wav, 按 0.25s 帧扫能量.
   规则: 见到人声帧后, 静音累计 1.25s 即 kill 录音进程, 识别立刻开始.
   终止条件: 录音进程退出(自然录满或被杀). */
static int vad_cut(const char *path, int rec_pid) {
    const int FRAME_BYTES = 16000 / 4 * 2;   /* 0.25 s @16k mono 16bit */
    const double VOICE_DB = -58.0;           /* 帧人声门限, 与总门一致偏松 */
    const int QUIET_FRAMES = 5;              /* 5*0.25=1.25s 静音截断 */
    FILE *fp;
    int quiet = 0, seen_voice = 0;
    unsigned char buf[FRAME_BYTES];
    size_t total_bytes = 0;
    for (int i = 0; i < 200; i++) {          /* 最多等 10s 文件出现 */
        fp = fopen(path, "rb");
        if (fp) break;
        usleep(50 * 1000);
    }
    if (!fp) return 2;
    unsigned char h[44];
    if (fread(h, 1, 44, fp) != 44) { fclose(fp); return 2; }  /* 跳过 wav 头 */
    while (1) {
        size_t n = fread(buf, 1, sizeof(buf), fp);
        if (n < (size_t)FRAME_BYTES) {
            /* 写入端尚未产生新数据: 等待或发现录音结束 */
            usleep(30 * 1000);
            if (kill((pid_t)rec_pid, 0) != 0) break;          /* arecord 已退出 */
            clearerr(fp);
            continue;
        }
        int64_t sumsq = 0;
        for (size_t i = 0; i + 1 < n; i += 2) {
            int16_t v = (int16_t)((uint16_t)buf[i] | ((uint16_t)buf[i + 1] << 8));
            sumsq += (int64_t)v * v;
        }
        total_bytes += n;
        double power = (double)sumsq / (n / 2) / (32768.0 * 32768.0);
        double db = power > 0 ? 10.0 * log10(power) : -120.0;
        if (db >= VOICE_DB) { seen_voice = 1; quiet = 0; }
        else if (seen_voice && ++quiet >= QUIET_FRAMES) {
            kill((pid_t)rec_pid, SIGTERM);
            usleep(200 * 1000);
            kill((pid_t)rec_pid, SIGKILL);
            usleep(100 * 1000);
            /* arecord 被 SIGKILL 后 wav 头里的长度字段是 0, 按实际数据量补写 */
            FILE *fx = fopen(path, "r+b");
            if (fx) {
                uint32_t data_len = (uint32_t)((total_bytes + 1) & ~1u);
                uint32_t riff_len = 36 + data_len;
                uint16_t ch = 1, bits = 16;
                uint32_t sr = 16000, br = 32000;
                fseek(fx, 4, SEEK_SET); fwrite(&riff_len, 4, 1, fx);
                fseek(fx, 40, SEEK_SET); fwrite(&data_len, 4, 1, fx);
                (void)ch; (void)bits; (void)sr; (void)br;
                fclose(fx);
            }
            break;
        }
    }
    fclose(fp);
    printf("[board] vad-cut: seen_voice=%d quiet_frames=%d\n", seen_voice, quiet);
    return 0;
}

int main(int argc, char **argv) {
    if (argc == 3 && !strcmp(argv[1], "--check-level"))
        return check_wav_level(argv[2]);
    if (argc == 4 && !strcmp(argv[1], "--vad-cut"))
        return vad_cut(argv[2], atoi(argv[3]));
    if (argc >= 2 && !strcmp(argv[1], "--strip-wav-headers"))
        return concat_wav_data(argc - 2, argv + 2);
    const char *phrase = argc > 1 ? argv[1] : "你好，请简单介绍一下你自己";
    const char *speaker = argc > 2 ? argv[2] : "plughw:1,0";
    char stdin_phrase[512];
    if (!strcmp(phrase, "-")) {
        if (!fgets(stdin_phrase, sizeof(stdin_phrase), stdin)) { fputs("stdin text missing\n", stderr); return 2; }
        stdin_phrase[strcspn(stdin_phrase, "\r\n")] = 0;
        phrase = stdin_phrase;
    }
    if (strlen(phrase) > 500) { fputs("text too long\n", stderr); return 2; }
    char escaped[1024];
    if (json_escape(phrase, escaped, sizeof(escaped))) { fputs("invalid text\n", stderr); return 2; }
    signal(SIGPIPE, SIG_IGN);
    srand((unsigned)time(NULL) ^ (unsigned)getpid());
    void *lib = dlopen("libopus.so.0", RTLD_NOW);
    if (!lib) { fprintf(stderr, "libopus: %s\n", dlerror()); return 1; }
    opus_create_fn create = (opus_create_fn)dlsym(lib, "opus_decoder_create");
    opus_decode_fn decode = (opus_decode_fn)dlsym(lib, "opus_decode");
    opus_destroy_fn destroy = (opus_destroy_fn)dlsym(lib, "opus_decoder_destroy");
    if (!create || !decode || !destroy) { fputs("libopus symbols missing\n", stderr); return 1; }
    int opus_error = 0;
    void *decoder = create(PLAY_RATE, 1, &opus_error);
    if (!decoder || opus_error) { fprintf(stderr, "opus decoder error %d\n", opus_error); return 1; }
    int fd = connect_server("127.0.0.1", 8000);
    if (fd < 0 || handshake(fd, "127.0.0.1", 8000)) { perror("connect/handshake"); return 1; }
    /* The server-side hello describes the 16 kHz wire audio; decoder output is 48 kHz. */
    const char *hello = "{\"type\":\"hello\",\"version\":1,\"transport\":\"websocket\","
        "\"features\":{\"mcp\":false,\"aec\":false},\"audio_params\":{\"format\":\"opus\","
        "\"sample_rate\":16000,\"channels\":1,\"frame_duration\":60}}";
    if (ws_send(fd, 1, hello, strlen(hello))) return 1;
    unsigned char *buf = malloc(MAX_FRAME + 1);
    if (!buf) return 1;
    unsigned char opcode;
    size_t len;
    if (ws_recv(fd, &opcode, buf, &len) || opcode != 1 || !strstr((char *)buf, "\"type\"")) {
        fputs("server hello missing\n", stderr); return 1;
    }
    puts("[board] server hello OK");
    char msg[1280];
    int n = snprintf(msg, sizeof(msg), "{\"type\":\"listen\",\"state\":\"detect\",\"text\":\"%s\"}", escaped);
    if (n < 0 || n >= (int)sizeof(msg) || ws_send(fd, 1, msg, (size_t)n)) return 1;
    printf("[board] sent: %s\n", phrase);
    int packets = 0;
    pid_t player_pid = -1;
    int player = -1;
    int wrote_ok = 1, aplay_bad = 0, sentences = 0;
    size_t played_frames = 0;
    short pcm[MAX_FRAME_SAMPLES];
    short stereo[MAX_FRAME_SAMPLES * PLAY_CHANNELS];
    time_t deadline = time(NULL) + 150;
    int got_stop = 0;
    while (time(NULL) < deadline) {
        errno = 0;
        if (ws_recv(fd, &opcode, buf, &len)) {
            if (errno == EAGAIN || errno == EWOULDBLOCK)
                continue; /* 句间空档: 保持单管道, 流内静音; 该 USB 声卡经不起频繁重开 */
            break;
        }
        if (opcode == 9) { if (ws_send(fd, 10, buf, len)) break; continue; }
        if (opcode == 8) break;
        if (opcode == 1) {
            printf("[event] %.220s\n", buf);
            if (strstr((char *)buf, "\"state\": \"stop\"") ||
                strstr((char *)buf, "\"state\":\"stop\"")) { got_stop = 1; break; }
        } else if (opcode == 2) {
            int samples = decode(decoder, buf, (int)len, pcm, MAX_FRAME_SAMPLES, 0);
            if (samples < 0) { fprintf(stderr, "opus decode error %d\n", samples); break; }
            if (player < 0) {
                player = start_player(speaker, &player_pid);
                if (player < 0) { perror("aplay"); break; }
                puts("[board] streaming playback started");
            }
            for (int i = 0; i < samples; i++) {
                stereo[(size_t)i * PLAY_CHANNELS] = pcm[i];
                stereo[(size_t)i * PLAY_CHANNELS + 1] = pcm[i];
            }
            if (write_all(player, stereo, (size_t)samples * PLAY_CHANNELS * sizeof(short))) {
                wrote_ok = 0;
                fprintf(stderr, "aplay write failed\n");
                break;
            }
            if (getenv("XZ_DUMP_PCM")) {
                static FILE *dumpf;
                if (!dumpf) dumpf = fopen("/tmp/xz_dump.raw", "wb");
                if (dumpf) fwrite(stereo, sizeof(short), (size_t)samples * PLAY_CHANNELS, dumpf);
            }
            played_frames += (size_t)samples;
            packets++;
        }
    }
    int play_ok = 0;
    if (player >= 0) {
        close(player);
        int status = -1;
        if (waitpid(player_pid, &status, 0) > 0 && WIFEXITED(status) &&
            WEXITSTATUS(status) == 0) sentences++;
        else aplay_bad = 1;
    }
    play_ok = (played_frames > 0 && wrote_ok && !aplay_bad);
    printf("[board] Opus packets=%d, tts_stop=%s, sentences=%d, decoded=%.2f s, pcm_rate=%d, channels=%d, pcm_frames=%zu, playback=%s\n",
           packets, got_stop ? "yes" : "no", sentences, (double)played_frames / PLAY_RATE,
           PLAY_RATE, PLAY_CHANNELS, played_frames,
           play_ok ? "ok" : "failed");
    free(buf);
    close(fd);
    destroy(decoder);
    dlclose(lib);
    return got_stop && packets > 0 && play_ok ? 0 : 1;
}
