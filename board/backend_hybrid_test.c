/* Deterministic fault tests; fake backends never touch microphones or networks. */
#include "backend.h"
#include <assert.h>
#include <string.h>
#include <stdio.h>
#include <stdatomic.h>
#include <sched.h>

typedef struct {
    backend_t base;
    int submits, audio, fail_submit, audio_submits, aborts, fail_abort;
    int disconnected;           /* 拆过的腿: 再轮询恒报 EV_ERROR(同 cb_poll_event 读已关闭 fd) */
    backend_ev_t next;
    char text[1400];
} fake_t;
static fake_t local, cloud;
static atomic_int local_connects, local_disconnects;
static atomic_int cloud_disconnects;
static atomic_int local_connect_hold;
static int connect_fake(backend_t *b) {
    ((fake_t *)b)->disconnected = 0;
    if (b == &local.base) {
        atomic_fetch_add(&local_connects, 1);
        while (atomic_load(&local_connect_hold)) sched_yield();
    }
    return 0;
}
static void disconnect_fake(backend_t *b) {
    ((fake_t *)b)->disconnected = 1;
    if (b == &local.base) atomic_fetch_add(&local_disconnects, 1);
    else atomic_fetch_add(&cloud_disconnects, 1);
}
static int announce_fake(backend_t *b, const char *id, int accepted) {
    (void)id; (void)accepted;
    return b == &local.base ? 0 : -1;
}
static int submit_fake(backend_t *b, const char *text) {
    fake_t *f = (fake_t *)b;
    ++f->submits; f->audio = 0;
    snprintf(f->text, sizeof(f->text), "%s", text);
    return f->fail_submit ? -1 : 0;
}
static backend_ev_t poll_fake(backend_t *b, unsigned char *buf, size_t cap,
                            size_t *len, char *text, size_t text_cap, int timeout) {
    (void)buf; (void)cap; (void)text; (void)text_cap; (void)timeout;
    fake_t *f = (fake_t *)b;
    backend_ev_t ev = f->next; f->next = EV_TIMEOUT; *len = 0;
    /* 已拆开的腿再被轮询, 真实实现读已关闭 fd 恒返 EV_ERROR */
    if (f->disconnected) return EV_ERROR;
    if (ev == EV_TTS_AUDIO) f->audio = 1;
    return ev;
}
static int audio_fake(backend_t *b) { return ((fake_t *)b)->audio; }
static int drop_fake(backend_t *b) { ((fake_t *)b)->audio = 0; return 0; }
static int abort_fake(backend_t *b) {
    fake_t *f = (fake_t *)b;
    ++f->aborts;
    f->audio = 0;
    return f->fail_abort ? -1 : 0;
}
static int wants_fake(backend_t *b) { (void)b; return 1; }
static int submit_audio_fake(backend_t *b, const backend_audio_request_t *r) {
    assert(r->samples == 100 && r->pcm[0] == 123);
    fake_t *f = (fake_t *)b; ++f->audio_submits;
    return f->fail_submit ? -1 : 0;
}
static const backend_ops_t ops = {
    .connect=connect_fake, .disconnect=disconnect_fake, .submit_text=submit_fake,
    .abort=abort_fake,
    .poll_event=poll_fake, .ping=connect_fake, .has_audio=audio_fake,
    .seen_stt=connect_fake, .drop_turn=drop_fake,
    .wants_audio=wants_fake, .submit_audio=submit_audio_fake,
    .announce_reply=announce_fake,
};
backend_t *be_open_local(void) { return &local.base; }
backend_t *be_open_cloud(void) { return &cloud.base; }
int be_connect(backend_t *b) { return b->ops->connect(b); }
void be_disconnect(backend_t *b) { b->ops->disconnect(b); }
int be_abort(backend_t *b) { return b->ops->abort ? b->ops->abort(b) : -1; }
int be_submit_text(backend_t *b, const char *t) { return b->ops->submit_text(b,t); }
backend_ev_t be_poll_event(backend_t *b,unsigned char *p,size_t c,size_t *n,char *t,size_t tc,int ms) {
    return b->ops->poll_event(b,p,c,n,t,tc,ms);
}
int be_ping(backend_t *b) { return b->ops->ping(b); }
int be_has_audio(backend_t *b) { return b->ops->has_audio(b); }
int be_seen_stt(backend_t *b) { return b->ops->seen_stt(b); }
int be_drop_turn(backend_t *b) { return b->ops->drop_turn(b); }
int be_wants_audio(backend_t *b) { return b->ops->wants_audio(b); }
int be_submit_audio(backend_t *b, const backend_audio_request_t *r) {
    return b->ops->submit_audio(b, r);
}
static int transcribes;
static int transcribe_fake(const int16_t *pcm, size_t n, char *out, size_t cap) {
    assert(n == 100 && pcm[0] == 123); ++transcribes;
    snprintf(out, cap, "audio fallback"); return 1;
}
static backend_ev_t poll_once(backend_t *b) {
    unsigned char buf[8]; char text[8]; size_t len;
    return be_poll_event(b,buf,sizeof(buf),&len,text,sizeof(text),0);
}
static void reset(backend_t *b) {
    memset(&local,0,sizeof(local)); memset(&cloud,0,sizeof(cloud));
    local.base.ops=&ops; cloud.base.ops=&ops;
    local.next=cloud.next=EV_TIMEOUT;
    assert(be_connect(b)==0);
    assert(be_submit_text(b,"test command")==0);
}
int main(void) {
    local.base.ops=cloud.base.ops=&ops;
    backend_t *b=be_open_hybrid(); assert(b);
    reset(b);
    assert(be_abort(b)==0 && cloud.aborts==1 && local.aborts==0);
    assert(be_abort(b)==0 && cloud.aborts==1);  /* Idle abort is a no-op. */

    reset(b);
    cloud.fail_submit=1;
    assert(be_submit_text(b,"fallback music")==0 && local.submits==1);
    local.audio=1;
    assert(be_abort(b)==0 && local.aborts==1 && cloud.aborts==0 && !local.audio);
    assert(cloud.disconnected);              /* Never touch the disconnected leg. */

    reset(b);
    cloud.fail_abort=1;
    assert(be_abort(b)==-1 && cloud.aborts==1 && local.aborts==0);

    reset(b);
    cloud.next=EV_ERROR;
    assert(poll_once(b)==EV_RESCUED);
    assert(local.submits==1 && !strcmp(local.text,"test command"));
    assert(be_drop_turn(b)==0 && local.submits==1);

    reset(b);
    cloud.next=EV_TTS_AUDIO; assert(poll_once(b)==EV_TTS_AUDIO);
    cloud.next=EV_ERROR; assert(poll_once(b)==EV_TTS_STOP);
    assert(local.submits==0);

    /* 弃轮不再"翻本地补答"(2026-10-20): 云端零音频 = 服务端有意不答。 */
    reset(b);
    assert(be_drop_turn(b)==0 && local.submits==0);
    assert(be_drop_turn(b)==0 && local.submits==0);

    reset(b);
    local.fail_submit=1; cloud.next=EV_ERROR;
    assert(poll_once(b)==EV_ERROR);
    assert(be_drop_turn(b)==0 && local.submits==1);

    /* 云端零音频正常收轮: 不补投本地, 前端只收一个 EV_TTS_STOP。 */
    reset(b);
    cloud.next=EV_TTS_STOP; assert(poll_once(b)==EV_TTS_STOP);
    assert(local.submits==0);
    assert(poll_once(b)==EV_TIMEOUT);
    assert(be_drop_turn(b)==0 && local.submits==0);
    reset(b);
    cloud.next=EV_TTS_AUDIO; assert(poll_once(b)==EV_TTS_AUDIO);
    cloud.next=EV_TTS_STOP; assert(poll_once(b)==EV_TTS_STOP);
    assert(local.submits==0);
    int16_t pcm[100] = {123};
    backend_audio_request_t r = {.pcm=pcm, .samples=100, .transcribe=transcribe_fake};
    reset(b); local.submits=0;
    assert(be_submit_audio(b,&r)==0 && transcribes==0);
    pcm[0]=456; /* retained PCM must be independent of the next utterance */
    cloud.next=EV_ERROR;
    assert(poll_once(b)==EV_RESCUED && transcribes==1);
    assert(local.submits==1 && !strcmp(local.text,"audio fallback"));
    assert(be_drop_turn(b)==0 && transcribes==1);

    reset(b); local.submits=0; pcm[0]=123;
    assert(be_submit_audio(b,&r)==0);
    assert(be_drop_turn(b)==0 && transcribes==1 && local.submits==0);
    assert(be_drop_turn(b)==0 && transcribes==1);

    reset(b); local.submits=0;
    assert(be_submit_audio(b,&r)==0);
    cloud.next=EV_TTS_AUDIO; assert(poll_once(b)==EV_TTS_AUDIO);
    cloud.next=EV_ERROR; assert(poll_once(b)==EV_TTS_STOP);
    assert(local.submits==0 && transcribes==1);

    /* 服务端静默回执(纯唤醒/噪声): 原样透传并清轮, 绝不能走"云端无声收轮,
       转本地续答"——否则一次纯唤醒会被补投本地 LLM, 闲聊出一整轮。 */
    reset(b); local.submits=0;
    cloud.next=EV_SILENT;
    assert(poll_once(b)==EV_SILENT);
    assert(local.submits==0 && transcribes==1);
    assert(be_drop_turn(b)==0 && local.submits==0);

    reset(b); local.submits=0; cloud.fail_submit=1;
    assert(be_submit_audio(b,&r)==0 && transcribes==2 && local.submits==1);
    assert(be_drop_turn(b)==0);
    /* 音频轮同样: 云端零音频收轮即结束, 不拿本地转写替它答一轮。 */
    reset(b); local.submits=0;
    assert(be_submit_audio(b,&r)==0);
    cloud.next=EV_TTS_STOP;
    assert(poll_once(b)==EV_TTS_STOP && transcribes==2 && local.submits==0);
    assert(be_drop_turn(b)==0 && local.submits==0);

    /* 云腿被拆掉后进入空闲期: 不得再把"已断开的云腿"当故障反复重报。
       hb_cloud_down() 已 be_disconnect(cloud), 而 cb_poll_event 读已关闭 fd
       恒返 EV_ERROR —— 若后台腿照旧轮询它, 每一轮都会再拆一次并刷一行
       "[hybrid] 后台云端腿断开"(板上实测 ~10 行/秒), 还会把 last_probe 顶掉,
       60s 探测再也回不了云腿。 */
    reset(b);
    cloud.next=EV_ERROR;             /* 轮内云端断开 -> 拆云腿并补投本地 */
    assert(poll_once(b)==EV_RESCUED);
    assert(be_drop_turn(b)==0);      /* 清轮 -> 进入空闲期 */
    int cloud_teardowns = atomic_load(&cloud_disconnects);
    for (int i = 0; i < 50; ++i) assert(poll_once(b)==EV_TIMEOUT);
    assert(atomic_load(&cloud_disconnects) == cloud_teardowns);
    reset(b); be_disconnect(b); assert(be_connect(b) == 0);
    int connects = atomic_load(&local_connects);
    int disconnects = atomic_load(&local_disconnects);
    local.next = EV_ERROR;
    assert(poll_once(b) == EV_TIMEOUT);
    assert(atomic_load(&local_disconnects) == disconnects + 1);
    /* Reconnect is asynchronous; cloud remains usable throughout. */
    atomic_store(&local_connect_hold, 1);
    assert(be_submit_text(b, "cloud still works") == 0);
    cloud.next = EV_TTS_AUDIO; assert(poll_once(b) == EV_TTS_AUDIO);
    cloud.next = EV_TTS_STOP; assert(poll_once(b) == EV_TTS_STOP);
    for (int i = 0; i < 10; ++i) assert(poll_once(b) == EV_TIMEOUT);
    assert(atomic_load(&local_disconnects) == disconnects + 1);
    atomic_store(&local_connect_hold, 0);
    local.next = EV_ANNOUNCE_REQUEST;
    backend_ev_t ev = EV_TIMEOUT;
    for (int i = 0; i < 100000 && ev == EV_TIMEOUT; ++i) {
        ev = poll_once(b);
        sched_yield();
    }
    assert(ev == EV_ANNOUNCE_REQUEST);
    assert(atomic_load(&local_connects) == connects + 1);
    assert(b->ops->announce_reply(b, "01234567890123456789012345678901", 1) == 0);
    local.next = EV_TTS_AUDIO; assert(poll_once(b) == EV_TTS_AUDIO);
    local.next = EV_TTS_STOP; assert(poll_once(b) == EV_TTS_STOP);
    be_disconnect(b);
    puts("hybrid fault tests passed");
    return 0;
}
