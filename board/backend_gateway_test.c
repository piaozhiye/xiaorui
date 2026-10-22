#define _POSIX_C_SOURCE 200809L
#include "backend.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static backend_ev_t next_event(backend_t *b) {
    unsigned char buf[4096]; char text[4096]; size_t len;
    double deadline = now_s() + 1;
    while (now_s() < deadline) {
        backend_ev_t ev = be_poll_event(b, buf, sizeof(buf), &len,
                                       text, sizeof(text), 10);
        if (ev != EV_TIMEOUT) return ev;
    }
    assert(0 && "missing gateway event");
    return EV_ERROR;
}

int main(int argc, char **argv) {
    assert(argc == 2);
    backend_t *b = strstr(argv[1], "-local") ? be_open_local() : be_open_cloud();
    assert(b && be_connect(b) == 0);
    if (!strncmp(argv[1], "continue-", 9)) {
        assert(be_submit_text(b, "answer then resume") == 0);
        assert(next_event(b) == EV_STT);
        assert(next_event(b) == EV_TTS_AUDIO);
        assert(next_event(b) == EV_NONE);      /* Segment end is not turn stop. */
        assert(next_event(b) == EV_TTS_START);
        assert(next_event(b) == EV_TTS_AUDIO); /* Resumed audio is not dropped. */
        assert(next_event(b) == EV_TTS_STOP);
        be_disconnect(b);
        puts("gateway music continuation passed");
        return 0;
    }
    if (!strncmp(argv[1], "abort-", 6)) {
        assert(be_submit_text(b, "music") == 0);
        assert(next_event(b) == EV_STT);
        assert(next_event(b) == EV_TTS_AUDIO);
        assert(be_abort(b) == 0);
        assert(!be_has_audio(b) && !be_seen_stt(b));
        assert(next_event(b) == EV_NONE);  /* Late stop after local cancellation. */
        assert(next_event(b) == EV_NONE);  /* PING/PONG proves same socket survives. */
        assert(be_submit_text(b, "second turn") == 0);
        assert(next_event(b) == EV_STT);
        assert(next_event(b) == EV_TTS_AUDIO);
        assert(next_event(b) == EV_TTS_STOP);
        be_disconnect(b);
        puts("gateway abort without reconnect passed");
        return 0;
    }
    if (!strcmp(argv[1], "announce_ack")) {
        for (int accept = 0; accept < 2; ++accept) {
            unsigned char buffer[4096]; char id[64]; size_t len = 0;
            assert(be_poll_event(b, buffer, sizeof(buffer), &len, id, sizeof(id), 1000)
                   == EV_ANNOUNCE_REQUEST);
            assert(!be_seen_stt(b));
            assert(!strcmp(id, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"));
            assert(be_announce_reply(b, id, accept) == 0);
        }
        assert(!be_seen_stt(b));
        assert(next_event(b) == EV_NONE); /* Matching server grant accepts the turn. */
        assert(be_seen_stt(b));
        assert(next_event(b) == EV_TTS_AUDIO);
        assert(next_event(b) == EV_TTS_STOP);
        be_disconnect(b);
        puts("gateway announce ACK passed");
        return 0;
    }
    if (!strcmp(argv[1], "reboot")) {
        /* 语音重启: stt 回执后 EV_REBOOT, 无音频无 stop(客户端会真的重启) */
        int got_reboot = 0;
        double wait_until = now_s() + 3;
        unsigned char rbuf[4096]; char rtext[4096]; size_t rlen;
        while (now_s() < wait_until) {
            backend_ev_t ev = be_poll_event(b, rbuf, sizeof(rbuf), &rlen,
                                           rtext, sizeof(rtext), 200);
            if (ev == EV_REBOOT) { ++got_reboot; break; }
        }
        assert(got_reboot == 1);
        be_disconnect(b);
        printf("gateway reboot passed\n");
        return 0;
    }
    if (!strcmp(argv[1], "announce")) {
        /* 空闲期主动播报(定时提醒): 无本地提交, announce -> 音频 -> stop */
        int got_announce = 0, got_audio = 0, got_stop = 0;
        double wait_until = now_s() + 3;
        unsigned char abuf[4096]; char atext[4096]; size_t alen;
        while (now_s() < wait_until) {
            backend_ev_t ev = be_poll_event(b, abuf, sizeof(abuf), &alen,
                                           atext, sizeof(atext), 200);
            if (ev == EV_ANNOUNCE) ++got_announce;
            else if (ev == EV_TTS_AUDIO) ++got_audio;
            else if (ev == EV_TTS_STOP) { ++got_stop; break; }
        }
        assert(got_announce == 1 && got_audio == 1 && got_stop == 1);
        be_disconnect(b);
        printf("gateway announce passed\n");
        return 0;
    }
    if (!strcmp(argv[1], "audio")) {
        int16_t pcm[1957] = {0};
        backend_audio_request_t r = {.pcm=pcm, .samples=1957};
        assert(be_wants_audio(b) && be_submit_audio(b, &r) == 0);
    } else assert(be_submit_text(b, "gateway test") == 0);
    unsigned char buf[4096]; char text[4096]; size_t len;
    int accepted = 0, audio = 0, stopped = 0, error = 0, volume_ev = 0;
    double end = now_s() + 1.0;
    while (now_s() < end) {
        double before = now_s();
        backend_ev_t ev = be_poll_event(b, buf, sizeof(buf), &len,
                                       text, sizeof(text), 10);
        /* 包含半帧时，10ms 轮询不能阻塞到后续帧体到达。 */
        assert(now_s() - before < 0.2);
        if (ev == EV_STT) ++accepted;
        if (ev == EV_VOLUME) {
            ++volume_ev;
            assert(!strcmp(text, "set:60"));
        }
        if (ev == EV_TTS_AUDIO) {
            assert(len == 3 && !memcmp(buf, "abc", 3));
            ++audio;
        }
        if (ev == EV_TTS_STOP) { ++stopped; break; }
        if (ev == EV_ERROR) { ++error; break; }
    }
    if (!strcmp(argv[1], "normal")) {
        assert(accepted == 1 && audio == 1 && stopped == 1 && error == 0);
        /* 完成后不能被迟到文本重新激活；下一轮仍可正常受理。 */
        assert(next_event(b) == EV_NONE);
        assert(next_event(b) == EV_NONE);
        assert(next_event(b) == EV_NONE);
        assert(!be_seen_stt(b) && !be_has_audio(b));
        assert(be_submit_text(b, "second turn") == 0);
        assert(next_event(b) == EV_STT);
        assert(next_event(b) == EV_TTS_AUDIO);
        assert(next_event(b) == EV_TTS_STOP);
    }
    else if (!strcmp(argv[1], "volume")) {
        /* 音量指令轮: EV_STT 回执后 EV_VOLUME(set:60), 无音频无 stop */
        assert(accepted == 1 && volume_ev == 1
               && audio == 0 && stopped == 0 && error == 0);
    }
    else if (!strcmp(argv[1], "audio"))
        assert(accepted==1 && audio==1 && stopped==1 && error==0);
    else if (!strcmp(argv[1], "disorder")) {
        /* 乱序轮: 受理前 stop 丢弃、重复/迟到事件不影响收轮; stop 后标志按设计清零
           (normal 场景已覆盖该语义), 这里只验收轮干净 + 迟到批次全丢 */
        assert(accepted == 1 && stopped == 1 && error == 0 && audio == 1);
        /* 重复 stop + 迟到 STT/audio/stop + 批次确认 ping。 */
        for (int i = 0; i < 5; ++i) assert(next_event(b) == EV_NONE);
        assert(!be_seen_stt(b) && !be_has_audio(b));
    }
    else if (!strcmp(argv[1], "midstream"))
        assert(accepted == 1 && audio == 1 && stopped == 0 && error == 1
               && be_has_audio(b)); /* hybrid 判定不可重复回答所需 */
    else if (!strcmp(argv[1], "silent"))
        assert(accepted == 0 && audio == 0 && stopped == 0 && error == 0);
    else if (!strcmp(argv[1], "before_audio"))
        assert(accepted == 0 && audio == 0 && stopped == 0 && error == 1);
    else assert(0);
    be_disconnect(b);
    printf("gateway %s passed\n", argv[1]);
    return 0;
}
