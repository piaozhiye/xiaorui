#!/usr/bin/env python3
"""分析验收 monitor.csv: 各进程 RSS/fd/线程趋势与后段斜率(规范要求:
预热后 fd/线程无持续增长, 后 10 分钟 RSS 无持续线性增长)."""
import sys
import csv
from collections import defaultdict

path = sys.argv[1]
samples = []  # (ts, {pid_key: (rss, fd, th)})
cur = {}
for row in csv.reader(open(path)):
    row = [c.strip() for c in row]
    if not row or not row[0]:
        continue
    if row[0].startswith("20"):  # 时间戳行, 新采样组
        if cur:
            samples.append(cur)
        cur = {}
    elif row[0].startswith("pid="):
        # pid=NNN rss=RRR fd=FF th=TT  (整行空格分隔, csv.reader 不拆, 手动拆)
        fields = [kv for cell in row for kv in cell.split()]
        d = dict(x.split("=", 1) for x in fields if "=" in x)
        pid = d.get("pid", "?")
        rss = int(d["rss"]) if d.get("rss", "").isdigit() else None
        fd = int(d["fd"]) if d.get("fd", "").isdigit() else None
        th = int(d["th"]) if d.get("th", "").isdigit() else None
        if rss and rss > 50000:  # 过滤 pgrep 自身匹配的瞬态小进程
            cur[pid] = (rss, fd, th)
if cur:
    samples.append(cur)

# pid 无法跨重启关联, 按采样内排序序号聚合(每分钟最多 4 个目标进程, 顺序稳定)
keys = defaultdict(list)  # slot -> [(sample_idx, rss, fd, th)]
for i, s in enumerate(samples):
    for slot, (pid, v) in enumerate(sorted(s.items())):
        keys[slot].append((i, *v))

print("slots=%d samples=%d" % (len(keys), len(samples)))
ok = True
for slot, series in sorted(keys.items()):
    n = len(series)
    rss = [x[1] for x in series]
    fd = [x[2] or 0 for x in series]
    th = [x[3] or 0 for x in series]
    head, tail = series[: max(1, n // 3)], series[-max(3, n // 3):]
    rss_head = sum(x[1] for x in head) / len(head)
    rss_tail = sum(x[1] for x in tail) / len(tail)
    fd_last, fd_head = fd[-1], (sum(fd[: max(1, n // 3)]) / max(1, n // 3))
    name = {0: "frontend?", 1: "gateway?", 2: "8000?", 3: "rkllm?"}.get(slot, "p%d?" % slot)
    drift = (rss_tail - rss_head) / 1024.0
    print("slot%d %-10s n=%d rss %.0f→%.0f MB (Δ%+.0f MB) fd %s→%s th %s→%s"
          % (slot, name, n, rss_head / 1024, rss_tail / 1024, drift,
             int(fd_head), fd_last, th[0], th[-1]))
    if fd_last - fd_head > 10:
        print("  WARN: fd 持续增长")
        ok = False
    if drift > 150:
        print("  WARN: RSS 后段线性增长(>150MB)")
        ok = False
print("RESOURCE", "PASS" if ok else "WARN")
