#!/usr/bin/env python3
"""Isolated RK3576 music gateway test; optional acoustic frontend, always restore.

No production releases/env/mixer are modified. Requires the board's private
8001 configuration and existing read-only music/model assets. Never promotes.
"""
import os
import argparse
import array
import hashlib
import json
import math
from pathlib import Path
import shlex
import subprocess
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
BASE = "/data/xiaorui-gateway"
PROD = "/data/xiaorui-server/board-scripts/xiaorui_live"
PYTHON = "/data/miniforge/bin/python"
LIBS = "/data/rknn-test/sherpa/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib:/usr/lib"
PATCHES = ("core/providers/tts/base.py", "core/utils/music_stream.py",
           "core/handle/abortHandle.py", "core/handle/sendAudioHandle.py",
           "core/handle/receiveAudioHandle.py", "core/handle/textHandler/listenMessageHandler.py")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", required=True)
    ap.add_argument("--acoustic", action="store_true")
    ap.add_argument("--serial", default=os.environ.get("XZ_BOARD_SERIAL"))
    ap.add_argument("--play-dev", default="plughw:1,0")
    ap.add_argument("--pure-wake-gain-db", type=float, default=0,
                    help="gain only the quiet pure-wake fixture into a temporary WAV; reject clipping")
    ap.add_argument("--pure-wake-wav", default=None,
                    help="explicit corpus override for the pure-wake leg only "
                         "(validated mono S16 WAV, e.g. a loudness-matched rebuild)")
    ap.add_argument("--utt-dump", action="store_true",
                    help="enable XR_UTT_DUMP on the candidate and pull the board-side "
                         "mic evidence (ctrl-utt wavs + rolling ctrl-raw.pcm) per leg")
    ap.add_argument("--venv", default=os.path.expanduser("~/rk/xiaozhi-esp32-server/main/xiaozhi-server/.venv/bin/python"))
    args = ap.parse_args()
    if not args.serial:
        ap.error("缺少板卡序列号: 传 --serial 或 export XZ_BOARD_SERIAL")
    if not 0 <= args.pure_wake_gain_db <= 12:
        ap.error("pure-wake-gain-db must be 0..12")
    if args.pure_wake_wav and args.pure_wake_gain_db:
        ap.error("--pure-wake-wav and --pure-wake-gain-db are mutually exclusive")
    # Validate the override before touching the board: mono S16, sane duration,
    # audible but never clipped. Provenance (sha256 + level) goes into the report.
    override = None
    if args.pure_wake_wav:
        override_path = Path(args.pure_wake_wav)
        if not override_path.is_file():
            ap.error("pure-wake wav not found: " + str(override_path))
        with wave.open(str(override_path)) as wav:
            if (wav.getsampwidth(), wav.getnchannels()) != (2, 1):
                ap.error("pure-wake wav must be mono S16 PCM")
            rate = wav.getframerate()
            samples = array.array('h', wav.readframes(wav.getnframes()))
        peak = max(abs(v) for v in samples)
        rms = (sum(v * v for v in samples) // len(samples)) ** 0.5
        clipped = sum(1 for v in samples if abs(v) >= 32767)
        if not 0.3 <= len(samples) / rate <= 10:
            ap.error("pure-wake wav duration out of range (0.3s..10s)")
        if clipped or peak < 4096:
            ap.error(f"pure-wake wav level invalid (peak={peak} clipped={clipped})")
        override = dict(path=str(override_path),
                        sha256=hashlib.sha256(override_path.read_bytes()).hexdigest(),
                        rate=rate, samples=len(samples), peak=peak,
                        peak_dbfs=round(20 * math.log10(peak / 32768), 1),
                        rms_dbfs=round(20 * math.log10(rms / 32768), 1),
                        clipped=clipped)
    stamp = str(time.time_ns())
    role = "codex-music-" + stamp
    profile = BASE + "/instances/" + role
    code = BASE + "/releases/" + role + "/server"
    candidate = profile + "/xiaorui_live"
    front_log = profile + "/run/frontend.log"
    evidence = Path("/tmp/" + role)
    evidence.mkdir(mode=0o700)
    if override:
        (evidence / "pure-wake-override.wav").write_bytes(Path(override["path"]).read_bytes())
    adb = ["adb", "-s", args.serial]
    report = dict(ok=False, restored=False, evidence=str(evidence), profile=profile, code=code)
    if override:
        report["pure_wake_override"] = override
    utt_dir = profile + "/uttdump" if args.utt_dump else None
    seen_utts = set()

    def level_dbfs(samples):
        if not samples:
            return None
        rms = (sum(v * v for v in samples) / len(samples)) ** 0.5
        return round(20 * math.log10(rms / 32768), 1)

    def mic_evidence(tag):
        # Pull what the board mic actually received during a leg: captured control
        # utterances (ctrl-utt-NNN.wav, written even when recognition fails) plus a
        # snapshot of the rolling raw dump (ctrl-raw.pcm, 16k mono S16, ~120s).
        if not utt_dir:
            return None
        out = evidence / ("mic-" + tag)
        out.mkdir(exist_ok=True)
        entries = []
        listing = board("ls -1 " + utt_dir + " 2>/dev/null")
        for name in [x for x in listing.splitlines() if x.startswith("ctrl-utt-") and x not in seen_utts]:
            subprocess.run(adb + ["pull", utt_dir + "/" + name, str(out / name)],
                           check=True, capture_output=True, timeout=15)
            seen_utts.add(name)
            with wave.open(str(out / name)) as wav:
                samples = array.array('h', wav.readframes(wav.getnframes()))
                entries.append(dict(file=name, dur_s=round(wav.getnframes() / 16000, 2),
                                    rms_dbfs=level_dbfs(samples)))
        board_raw = utt_dir + "/ctrl-raw.pcm"
        if board("[ -f " + board_raw + " ] && echo yes") == "yes":
            target = out / "ctrl-raw.pcm"
            subprocess.run(adb + ["pull", board_raw, str(target)], check=True,
                           capture_output=True, timeout=60)
            data = target.read_bytes()
            samples = array.array('h', data[:len(data) // 2 * 2])
            entries.append(dict(file="ctrl-raw.pcm", dur_s=round(len(samples) / 16000, 1),
                                rms_dbfs=level_dbfs(samples)))
        report["mic_" + tag] = entries
        return entries
    changed = False
    cron = None
    cron_pause_attempted = False
    forwarded = False

    def board(cmd, timeout=30):
        return subprocess.run(adb + ["shell", cmd], check=True, capture_output=True,
                              text=True, timeout=timeout).stdout.strip()

    def remote(script, timeout=30):
        compile(script, "<board-script>", "exec")
        return subprocess.run(adb + ["shell", PYTHON + " -"], input=script, check=True,
                              capture_output=True, text=True, timeout=timeout).stdout.strip()

    def push(source, target):
        subprocess.run(adb + ["push", str(source), target], check=True,
                       capture_output=True, timeout=30)

    def pids(path):
        return remote(f'''import pathlib,json
target={path!r}; found=[]
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdigit(): continue
 try:
  matched=(target in (p/'cmdline').read_bytes().decode().split(chr(0)) if target.endswith('.py')
           else str((p/'exe').readlink())==target)
  if matched: found.append(int(p.name))
 except (OSError,UnicodeError): pass
print(json.dumps(found))
''')

    def terminate(path):
        targets = json.loads(pids(path))
        if len(targets) > 1:
            raise RuntimeError("multiple instances for " + path)
        if targets:
            board(f"kill -TERM {targets[0]}")
        for _ in range(25):
            if not json.loads(pids(path)):
                return
            time.sleep(1)
        raise RuntimeError("process did not release: " + path)

    def start_front(path, injected=None):
        existing = json.loads(pids(path))
        if existing:
            if injected is None and len(existing) == 1:
                return existing[0]
            raise RuntimeError("refusing duplicate frontend")
        log = front_log if injected is not None else "/tmp/xr_live.log"
        extra = ""
        if injected is not None:
            extra = (f"XR_BACKEND=cloud XR_CLOUD_URL=ws://127.0.0.1:8101/xiaozhi/v1/ "
                     f"XR_TEXT_INJECT={shlex.quote(injected)} XR_WATCHDOG_S=0 XR_TIMING=1 "
                     f"XR_VOLUME_FILE={profile}/volume XR_DIAG_AUDIO=1 ")
            if utt_dir:
                extra += "XR_UTT_DUMP=" + utt_dir + " "
        board("cd /data/xiaorui-server; . ./xiaorui.env; unset XR_TEXT_INJECT XR_UTT_DUMP; "
              f"(setsid env LD_LIBRARY_PATH={LIBS} " + extra + shlex.quote(path)
              + " >" + shlex.quote(log) + " 2>&1 </dev/null &)")
        for _ in range(90):
            text = board("tail -50 " + log)
            if "ambient=" in text and len(json.loads(pids(path))) == 1:
                return json.loads(pids(path))[0]
            time.sleep(1)
        raise TimeoutError("frontend readiness")

    def log():
        return board("cat " + front_log)

    def gateway_log():
        return board("cat " + profile + "/run/server.log")

    def wait_log(predicate, seconds=40):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            text = log()
            if predicate(text):
                return text
            time.sleep(1)
        raise TimeoutError("candidate event missing; see saved frontend log")

    def play(name):
        source = ROOT / "tests/audio" / name
        if name == "wake_tts_pure.wav" and override:
            source = Path(override["path"])
        elif name == "wake_tts_pure.wav" and args.pure_wake_gain_db:
            with wave.open(str(source)) as wav:
                if wav.getsampwidth() != 2:
                    raise RuntimeError("gain check requires S16 PCM")
                samples = array.array('h', wav.readframes(wav.getnframes()))
            peak = max(abs(v) for v in samples)
            if peak * 10 ** (args.pure_wake_gain_db / 20) > 32767:
                raise RuntimeError("pure-wake gain would clip; original fixture unchanged")
            target = evidence / "pure-wake-gained.wav"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
                "-af", f"volume={args.pure_wake_gain_db}dB", "-c:a", "pcm_s16le", str(target)],
                check=True, timeout=10)
            report["pure_wake_gain_db"] = args.pure_wake_gain_db
            report["pure_wake_original_peak"] = peak
            source = target
        print("PC playback: " + str(source), flush=True)
        subprocess.run(["aplay", "-q", "-D", args.play_dev, str(source)],
                       check=True, timeout=30)

    try:
        if "rockchip,rk3576" not in board("cat /proc/device-tree/compatible"):
            raise RuntimeError("not RK3576")
        baseline = remote(f'''import pathlib,json,sqlite3,urllib.request,hashlib
b=pathlib.Path({BASE!r})
c=sqlite3.connect('file:'+str(b/'state/timers.sqlite3')+'?mode=ro',uri=True)
assert c.execute("select count(*) from timers where state in ('pending','claimed')").fetchone()[0]==0
d={{'health':{{}},'env_hash':{{}}}}
for role,port in (('8001',8004),('8002',8005)):
 h=json.load(urllib.request.urlopen('http://127.0.0.1:%d/health/ready'%port,timeout=3)); assert h['ready']
 d['health'][role]={{k:h.get(k) for k in ('ready','role','release')}}
 d['env_hash'][role]=hashlib.sha256((b/'instances'/role/'runtime.env').read_bytes()).hexdigest()
print(json.dumps(d))
''')
        report["before"] = json.loads(baseline)
        report["production_sha"] = board("sha256sum " + PROD).split()[0]
        report["candidate_sha"] = hashlib.sha256((ROOT / "board/xiaorui_live").read_bytes()).hexdigest()
        if len(json.loads(pids(PROD))) != 1 or board("pgrep -x xr_cand_live || true"):
            raise RuntimeError("expected exactly one original frontend and no legacy candidate")
        if board("ss -tln | grep -E ':(8101|8104) ' || true"):
            raise RuntimeError("candidate ports occupied")
        source = remote(f'''import pathlib,re
t=pathlib.Path({BASE!r}+'/instances/8001/runtime.env').read_text()
print(re.search(r'^XR_CODE_DIR=(.*)$',t,re.M)[1])
''')
        if not source.startswith(BASE + "/releases/") or not source.endswith("/server"):
            raise RuntimeError("unexpected production code path")
        board(f"umask 077; mkdir -p {profile}/run {profile}/data {profile}/state {profile}/work {code}; "
              f"chmod 700 {profile} {profile}/data; cp -a {source}/. {code}/")
        for item in PATCHES:
            push(ROOT / "board-server/pkg" / item, code + "/" + item)
        remote(f'''import pathlib,yaml,os
p=pathlib.Path({profile!r}); cfg=yaml.safe_load(pathlib.Path({BASE!r}+'/instances/8001/data/.config.yaml').read_text())
cfg['server']['ip']='127.0.0.1'; cfg['server']['port']=8101; cfg['server']['http_port']=8104
assert not cfg['server'].get('auth',{{}}).get('enabled'), 'probe needs loopback test auth support'
assert cfg.get('enable_greeting') is False, 'explicitly disabled greeting required'
target=p/'data/.config.yaml'; target.write_text(yaml.safe_dump(cfg,allow_unicode=True)); target.chmod(0o600)
env={{'XR_RELEASE_ID':{role!r},'XR_CODE_DIR':{code!r},'XR_WORK_DIR':str(p/'work'),
 'XR_DATA_DIR':str(p/'data'),'XR_CONFIG_FILE':str(target),'XR_STATE_DIR':str(p/'state'),
 'XR_HTTP_PORT':'8104','PYTHONPATH':{code!r}}}
(p/'runtime.env').write_text(''.join(k+'='+v+chr(10) for k,v in env.items()))
''')
        board(f"sh {BASE}/start-gateway-instance.sh {role}", timeout=100)
        subprocess.run(adb + ["forward", "tcp:18121", "tcp:8101"], check=True, capture_output=True)
        forwarded = True
        probe = subprocess.run([args.venv, str(ROOT / "tools/probe_music_yield.py"),
            "--url", "ws://127.0.0.1:18121/xiaorui/v1/", "--no-greeting",
            "--ask-after", "40", "--seconds", "50"], capture_output=True, text=True, timeout=80)
        (evidence / "protocol.log").write_text(probe.stdout + probe.stderr)
        report["protocol_exit"] = probe.returncode
        print(probe.stdout, flush=True)
        if probe.returncode:
            raise RuntimeError("strict protocol probe failed")
        if args.acoustic:
            push(ROOT / "board/xiaorui_live", candidate)
            if board("sha256sum " + candidate).split()[0] != report["candidate_sha"]:
                raise RuntimeError("candidate checksum")
            board(f"chmod 755 {candidate}; cp -p /data/xiaorui-server/xiaorui_volume {profile}/volume; "
                  f"cp /tmp/xr_live.log {profile}/run/production-before.log")
            cron = board("pidof crond")
            if not cron.isdigit() or board(f"awk '/^State:/ {{print $2}}' /proc/{cron}/status") == "T":
                raise RuntimeError("cron missing or already paused")
            # Record intent before the command: it can take effect even if ADB
            # times out before returning. Restore cron independently of frontend.
            cron_pause_attempted = True
            board(f"kill -STOP {cron}")
            if utt_dir:
                board("mkdir -p " + utt_dir)
            changed = True
            terminate(PROD)
            report["candidate_pid"] = start_front(candidate, "播放轻音乐电台")
            wait_log(lambda t: '"media"' in t and '"music"' in t and "首包" in t)
            time.sleep(3)
            offset = len(log())
            play("wake_tts_pure.wav")
            time.sleep(9)
            segment = log()[offset:]
            (evidence / "pure-wake.log").write_text(segment)
            mic_evidence("pure_wake")
            report["pure_wake"] = ("KWS命中" in segment and "非控制语音忽略:" in segment
                                   and "[music] 转交普通指令:" not in segment)
            offset = len(log())
            gw_offset = len(gateway_log())
            play("wake_tts_time.wav")
            wait_log(lambda t: "[music] 已作为正常轮提交" in t[offset:] and
                     "现在" in t[offset:] and '"music"' in t[offset:] and
                     "音乐恢复播放(让位结束)" in gateway_log()[gw_offset:])
            time.sleep(3)
            segment = log()[offset:]
            (evidence / "question-resume.log").write_text(segment)
            mic_evidence("question")
            gw_segment = gateway_log()[gw_offset:]
            (evidence / "question-resume-gateway.log").write_text(gw_segment)
            music_event = segment.rfind('"media"')
            report["question_resume"] = ("[music] 已作为正常轮提交" in segment
                and "发送第一段语音: 现在" in gw_segment and "首包" in segment
                and music_event >= 0 and "pkt#" in segment[music_event:])
            offset = len(log())
            play("wake_tts_stop.wav")
            wait_log(lambda t: "[music] 语音停止播放" in t[offset:])
            time.sleep(4)
            segment = log()[offset:]
            (evidence / "stop.log").write_text(segment)
            mic_evidence("stop")
            report["stop"] = "已发 abort" in segment and "[xr] connected" not in segment
            if not all(report[k] for k in ("pure_wake", "question_resume", "stop")):
                raise RuntimeError("acoustic evidence incomplete")
        report["ok"] = True
    except Exception as error:
        report["error"] = type(error).__name__ + ": " + str(error)
        print(report["error"], flush=True)
    finally:
        # Every cleanup step is attempted, even if a previous step failed.
        for name, action in (
            ("save_gateway_log", lambda: (evidence / "gateway.log").write_text(
                board(f"if [ -f {profile}/run/server.log ]; then tail -1500 {profile}/run/server.log; fi"))),
            ("save_frontend_log", lambda: (evidence / "frontend.log").write_text(log()) if changed else None),
            ("mic_pull_final", lambda: mic_evidence("final") if changed else None),
            ("stop_candidate", lambda: terminate(candidate) if changed else None),
            ("restore_frontend", lambda: start_front(PROD) if changed else None),
            ("resume_cron", lambda: board(f"kill -CONT {cron}") if cron_pause_attempted else None),
            ("stop_test_gateway", lambda: terminate(code + "/app.py")),
            ("remove_forward", lambda: subprocess.run(adb + ["forward", "--remove", "tcp:18121"],
                check=True, capture_output=True) if forwarded else None),
        ):
            try:
                value = action()
                if name == "restore_frontend":
                    report["restored_pid"] = value
            except Exception as error:
                report.setdefault("cleanup_errors", {})[name] = type(error).__name__ + ": " + str(error)
        try:
            after = json.loads(remote(f'''import pathlib,hashlib,json,urllib.request
b=pathlib.Path({BASE!r}); d={{'health':{{}},'env_hash':{{}}}}
for role,port in (('8001',8004),('8002',8005)):
 h=json.load(urllib.request.urlopen('http://127.0.0.1:%d/health/ready'%port,timeout=3)); assert h['ready']
 d['health'][role]={{k:h.get(k) for k in ('ready','role','release')}}
 d['env_hash'][role]=hashlib.sha256((b/'instances'/role/'runtime.env').read_bytes()).hexdigest()
print(json.dumps(d))
'''))
            report["after"] = after
            report["restored"] = (after == report.get("before") and
                board("sha256sum " + PROD).split()[0] == report.get("production_sha") and
                len(json.loads(pids(PROD))) == 1 and not json.loads(pids(candidate)) and
                not json.loads(pids(code + '/app.py')) and
                (not cron_pause_attempted or
                 board(f"awk '/^State:/ {{print $2}}' /proc/{cron}/status") in ("S", "R", "I")) and
                not report.get("cleanup_errors"))
        except Exception as error:
            report["restore_error"] = str(error)
        (evidence / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["ok"] and report["restored"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
