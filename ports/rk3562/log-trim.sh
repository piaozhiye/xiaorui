#!/bin/sh
# RK3562 板上日志轮转: 大于 10MB 的日志截断保留尾部 2000 行。
# 由 crond 每天调用。截断用 "tail > tmp && cat tmp > 原文件" 保 inode:
# 写日志的进程都是 O_APPEND 追加打开, truncate 后自动续写新末尾, 无需重启。
MAX=10485760   # 10MB
KEEP=2000
for f in /data/xiaorui-gateway/data/gateway.log \
         /data/xiaorui-gateway/data/rkllm.log \
         /data/xiaorui-gateway/data/stack.log \
         /data/xiaorui/frontend.log \
         /data/xiaorui-ui/ui.log \
         /data/ha-config/ha.log; do
    [ -f "$f" ] || continue
    sz=$(stat -c%s "$f" 2>/dev/null) || continue
    [ "$sz" -gt "$MAX" ] || continue
    tail -n $KEEP "$f" > "$f.trim" 2>/dev/null && \
        cat "$f.trim" > "$f" && rm -f "$f.trim"
    echo "$(date '+%F %T') trimmed $f (was ${sz}B)" >> /tmp/log-trim.log
done
exit 0
