#ifndef XR_RUNTIME_CONFIG_H
#define XR_RUNTIME_CONFIG_H
typedef struct {
    int cloud_asr;            /* 0=text route, 1=16k Opus audio route */
    int local_asr;            /* 1=rknn (legacy default), 0=off, -1=invalid */
    /* KWS 声学命中即信(跳过文本唤醒门), 仅在 cloud_asr 下生效。默认 0:
       USB/未处理链路保持双门确认。VQE 直采(rkaudio_vqe/xr_vqe 设备)自动信任;
       fifo 供给 VQE 流时由启动器显式置 1。 */
    int kws_trust_acoustic;
    int cloud_conn_ms;
    int cloud_probe_s;
    int cloud_rx_dead_s;
    int cloud_ping_failures;
    int accepted_silence_s;
    int response_timeout_s;
    /* 2026-10-13 收编: 现场(嘈杂/远场)调参不再重编译. main 注入各模块, 模块自身保持纯净 */
    int vad_preroll_frames;   /* 预滚帧数(100ms/帧), 防指令开头被截 */
    int vad_trigger_db;       /* 能量门触发余量(底噪+N dB) */
    int vad_sustain_db;       /* 持续余量(句内断句容错) */
    int follow_window_s;      /* 跟说窗(应答后免唤醒秒数) */
    int ack_cool_ms;          /* 纯唤醒 ack 后余音冷却 */
    int finish_cool_ms;       /* 应答排干后近场冷却 */
    int abandon_cool_ms;      /* 判空弃轮后冷却 */
    int fail_cue;             /* 弃轮反馈音开关: 用户干等一轮后给一声"没听清"(默认1) */
    int fail_cue_cool_ms;     /* 弃轮反馈音后的余音冷却 */
    int think_cue_ms;         /* 慢响应填充音阈值: 提交后多久还没音频就补"嗯"(0=关) */
    int echo_guard_s;         /* 应答/ack 后，跟说窗内已知余音词的抑制时间 */
    int echo_min_cmd_chars;   /* 保留旧变量名：只对字数低于此值的已知余音词生效 */
    int volume_control;       /* 语音音量控制(本地意图): 1=开, XR_VOLUME_CONTROL=0 关 */
    int music_controls;       /* 仅音乐播放期USB麦KWS+本地音量/停止控制 */
    int music_command_ms;     /* KWS命中后收集控制语音的时长 */
} xr_runtime_config_t;
/* Load once on the frontend thread; environment changes require restart. */
const xr_runtime_config_t *xr_runtime_config(void);
/* Fail before opening devices/models for an unsupported capability combination. */
int xr_runtime_config_validate(const xr_runtime_config_t *config, const char *backend);
#endif
