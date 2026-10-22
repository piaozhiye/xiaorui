#!/usr/bin/env python3
"""Deploy only HA audit files to a cloned release, rollback on failed readiness."""
import os
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
BASE = "/data/xiaorui-gateway"
ADB_SERIAL = os.environ.get("XZ_BOARD_SERIAL")
if not ADB_SERIAL:
    raise SystemExit("缺少板卡序列号: export XZ_BOARD_SERIAL=<序列号> 后重试")
ADB = ["adb", "-s", ADB_SERIAL]
# 部署到克隆 release 的文件。HA 审计三件套(关联证据) + 电台链路修复。
# 每次内容变化都会得到新的 release tag, 板上的 active release 可据此核对。
FILES = {"core/ha_audit.py": ROOT / "board-server/pkg/core/ha_audit.py",
         "core/handle/haIntent.py": ROOT / "board-server/pkg/core/handle/haIntent.py",
         "core/handle/sendAudioHandle.py": ROOT / "board-server/pkg/core/handle/sendAudioHandle.py",
         "core/handle/radioIntent.py": ROOT / "board-server/pkg/core/handle/radioIntent.py",
         "core/handle/intentHandler.py": ROOT / "board-server/pkg/core/handle/intentHandler.py",
         "core/handle/helloHandle.py": ROOT / "board-server/pkg/core/handle/helloHandle.py",
         # wake=required 标记在这里解析(冷启动轮的唤醒判定交给云端转写)
         "core/handle/textHandler/listenMessageHandler.py":
             ROOT / "board-server/pkg/core/handle/textHandler/listenMessageHandler.py",
         "plugins_func/functions/play_music.py":
             ROOT / "board-server/pkg/plugins_func/functions/play_music.py",
         # config.yaml 含第三方 API 密钥, 在 .gitignore 里不入库, 但它是板上
         # 网关实际读的那份 —— 唤醒词表就靠它(真源见
         # board-server/config/wakeup_words.xiaorui.yaml)。推它等于把本机的
         # 那份覆盖到板上; 推之前两边必须一致(2026-10-20 实测 0 行差异)。
         "config.yaml": ROOT / "board-server/pkg/config.yaml"}


def shell(cmd, timeout=20):
    return subprocess.run(ADB + ["shell", cmd], check=True, capture_output=True,
                          text=True, timeout=timeout).stdout.strip()


def remote(code):
    return subprocess.run(ADB + ["shell", "/data/miniforge/bin/python -"], input=code,
                          check=True, capture_output=True, text=True, timeout=20).stdout.strip()


def wakeup_words():
    """唤醒词表真源(入库的 overlay)。生效位置是**实例的自定义配置**,
    不是 release 的 config.yaml —— 网关的有效配置 = 默认 config.yaml 与
    instances/<role>/data/.config.yaml 合并, 自定义优先(见
    config/config_loader.py: load_config/merge_configs)。2026-10-20 实测:
    只改 release 的 config.yaml 完全无效, 实例配置里那行 wakeup_words 照样
    只有"小瑞小瑞,小瑞"两条。"""
    import yaml
    overlay = ROOT / "board-server" / "config" / "wakeup_words.xiaorui.yaml"
    words = yaml.safe_load(overlay.read_text())["wakeup_words"]
    if "小瑞小瑞" not in words:
        raise RuntimeError("唤醒词表里没有本机唤醒词")
    return words


def patch_instance_wake_words():
    """把唤醒词表写进两个实例的自定义配置(保持其余内容不动)。"""
    payload = ",".join(wakeup_words())
    for role in (8001, 8002):
        remote(f'''import pathlib,re
p=pathlib.Path('/data/xiaorui-gateway/instances/{role}/data/.config.yaml')
t=p.read_text()
value={payload!r}
if re.search(r'(?m)^wakeup_words:', t):
    t=re.sub(r'(?m)^wakeup_words:.*$', 'wakeup_words: '+value, t)
else:
    t += "\\nwakeup_words: "+value+"\\n"
p.write_text(t)
print('wakeup_words entries', len(value.split(',')))
''')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True,
                        help="explicitly allow gateway restart and profile changes")
    parser.parse_args()
    digest = hashlib.sha256(b"".join(p.read_bytes() for p in FILES.values())).hexdigest()[:12]
    tag = "ha-audit-" + digest
    release = f"{BASE}/releases/{tag}/server"
    backup = f"{BASE}/backups/{tag}-{time.time_ns()}"
    cron = None
    switched = False
    success = False
    report = dict(ok=False, release=tag, backup=backup)
    try:
        old = remote('''import pathlib,re,json,sqlite3
b=pathlib.Path('/data/xiaorui-gateway')
c=sqlite3.connect('file:'+str(b/'state/timers.sqlite3')+'?mode=ro',uri=True)
assert c.execute("select count(*) from timers where state in ('pending','claimed')").fetchone()[0]==0,'pending timers'
out=[]
for role in ('8001','8002'):
 t=(b/'instances'/role/'runtime.env').read_text()
 out.append(re.search(r'^XR_CODE_DIR=(.*)$',t,re.M)[1].strip("\\\"'"))
assert out[0]==out[1], 'different release roots'
print(json.dumps(out[0]))
''')
        old = json.loads(old)
        if not old.startswith(BASE + "/releases/") or not old.endswith("/server"):
            raise RuntimeError("unexpected source release")
        if old == release:
            raise RuntimeError("audit release already active; refusing redundant restart")
        shell(f"umask 077; mkdir -p {backup}; test ! -e {release}; mkdir -p {BASE}/releases/{tag}; cp -a {old} {release}")
        for relative, local in FILES.items():
            subprocess.run(ADB + ["push", str(local), release + "/" + relative],
                           check=True, capture_output=True, text=True, timeout=20)
        # Check exact byte identity before touching running instances.
        for relative, local in FILES.items():
            expected = hashlib.sha256(local.read_bytes()).hexdigest()
            if shell(f"sha256sum {release}/{relative}").split()[0] != expected:
                raise RuntimeError("deployed hash mismatch")
        cron = shell("pidof crond")
        if not cron.isdigit() or shell(f"awk '/^State:/ {{print $2}}' /proc/{cron}/status") == "T":
            raise RuntimeError("cron unavailable or already paused")
        shell(f"kill -STOP {cron}")
        remote(f'''import pathlib,shutil
b=pathlib.Path({BASE!r}); backup=pathlib.Path({backup!r})
for role in ('8001','8002'):
 p=b/'instances'/role/'runtime.env'
 shutil.copy2(p,backup/(role+'.env'))
''')
        switched = True
        # 唤醒词表要在重启之前写好: 网关启动时读实例的自定义配置
        patch_instance_wake_words()
        remote(f'''import pathlib,re,os,signal
b=pathlib.Path({BASE!r})
for role in ('8001','8002'):
 p=b/'instances'/role/'runtime.env'; t=p.read_text()
 assert len(re.findall('^XR_CODE_DIR=',t,re.M))==1
 assert len(re.findall('^XR_RELEASE_ID=',t,re.M))==1
 t=re.sub(r'^XR_CODE_DIR=.*$', 'XR_CODE_DIR='+{release!r},t,flags=re.M)
 t=re.sub(r'^XR_RELEASE_ID=.*$', 'XR_RELEASE_ID='+{tag!r},t,flags=re.M)
 p.write_text(t)
 pid=int((p.parent/'run/server.pid').read_text())
 cmd=pathlib.Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\\0')
 assert ({old!r}+'/app.py').encode() in cmd
 os.kill(pid,signal.SIGTERM)
''')
        for role in (8001, 8002):
            print(shell(f"sh {BASE}/start-gateway-instance.sh {role}", timeout=120), flush=True)
        success = True
        report["ok"] = True
    except Exception as error:
        report["error"] = type(error).__name__
        if isinstance(error, subprocess.CalledProcessError):
            report["detail"] = (error.stderr or error.stdout or "")[-1500:]
    finally:
        if switched and not success:
            try:
                remote(f'''import pathlib,os,signal,shutil
b=pathlib.Path({BASE!r}); backup=pathlib.Path({backup!r})
for role in ('8001','8002'):
 p=b/'instances'/role/'runtime.env'
 try:
  pid=int((p.parent/'run/server.pid').read_text())
  cmd=pathlib.Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\\0')
  if ({release!r}+'/app.py').encode() in cmd: os.kill(pid,signal.SIGTERM)
 except (FileNotFoundError,ProcessLookupError): pass
 shutil.copy2(backup/(role+'.env'),p)
''')
                for role in (8001, 8002):
                    shell(f"sh {BASE}/start-gateway-instance.sh {role}", timeout=120)
                report["rollback"] = "ready"
            except Exception as error:
                report["rollback_error"] = type(error).__name__
        if cron and cron.isdigit():
            try:
                shell(f"kill -CONT {cron}")
            except Exception as error:
                report.update(ok=False, cron_error=type(error).__name__)
        path = Path(f"/tmp/deploy-ha-audit-{time.time_ns()}.json")
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(dict(report, evidence=str(path)), ensure_ascii=False), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
