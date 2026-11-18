/* 瑞芯微 rkaudio_vqe 内置唤醒: 从状态文件读 wakeup 边沿。
 *
 * 为什么读状态文件而不是调 rkaudio_vqe_get_wakeup(pcm):
 *   前端可能用 XR_MIC=fifo:<path>(独立进程喂麦, 见 start-frontend-vqe.sh),
 *   那种模式下前端进程里没有 VQE 的 snd_pcm_t, 调不了插件 API;
 *   状态文件是插件写的, 任何进程都能读, 两种模式都能用。
 *
 * 状态文件格式(插件 docs/README_CN.md 第 5 节): doa:pitch:wakeup:keyword
 *   例 "45:10:1:打开灯" -> DOA=45 俯仰=10 已唤醒 关键词="打开灯"
 *   wakeup 字段: 0=无, >0=已唤醒(插件的检测窗口内, 默认 3s 后自动清零)
 *
 * 边沿语义: 插件把 wakeup 从 0 置成 >0 的那一刻才算一次"新唤醒"。
 *   之后 3 秒内一直是 >0(窗口期), 不会再算第二次; 过期清 0 后下次再算。
 */
#ifndef XR_VQE_WAKE_H
#define XR_VQE_WAKE_H

#ifdef __cplusplus
extern "C" {
#endif

/* 状态文件可读(插件在跑并写了状态)。不可用时前端退回原 KWS。 */
int xr_vqe_wake_available(void);

/* 轮询状态文件: 1 = 本次调用发现了新的唤醒(0 -> >0 的边沿)。 */
int xr_vqe_wake_poll(void);

/* 插件的检测窗口是否还开着(状态文件里的 wakeup 字段仍 >0)。
 * 句尾收割用: 唤醒词那一句结束时, 窗口通常还没过期。 */
int xr_vqe_wake_active(void);

/* 1 = 最近 keep_ms 毫秒内发生过唤醒。 */
int xr_vqe_wake_recent(int keep_ms);

/* 放弃当前这次唤醒(用户打断/弃轮时调用)。
 * 注意: 不清"上次读到的 wakeup 值" —— 插件的检测窗口还没过期, 清了会让
 * 下一次轮询误判成新唤醒。 */
void xr_vqe_wake_reset(void);

#ifdef __cplusplus
}
#endif
#endif /* XR_VQE_WAKE_H */
