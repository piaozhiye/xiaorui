# xiaorui — Chinese Voice Assistant on RK3576 (Cloud + Fully-Offline Dual Mode)

[中文](README.md) | English

xiaorui is a Chinese voice assistant running on an RK3576 board: a **single-process C
frontend** handles wake word / utterance segmentation / recognition / playback, while
**two on-board gateways** provide the "fast cloud" and "works offline" links, and
**Home Assistant** bridges to Mi Home devices. Wake, ASR, dialogue and TTS close the
loop on the board — unplug the network and dialogue / clock / timers / alarms /
volume all keep working (the local gateway's RKLLM takes over); with connectivity the
cloud LLM answers, falling back automatically and imperceptibly in both directions.

```
The full stack auto-starts on boot (/etc/init.d/S99xrvoice → start-all.sh). Wake examples:
    "Xiaorui Xiaorui, what time is it"    ← clock is fully local, zero network dependency
    "Xiaorui Xiaorui, turn off the light" ← HA intent, zero-LLM direct device control
    "Wei, Xiaorui Xiaorui…" / "Thanks, Xiaorui Xiaorui…" ← lead-in word whitelist
    "He said Xiaodu Xiaodu"               ← negative example: cold state stays silent
```

## Feature Overview (all verified by on-device acoustic acceptance)

| Feature | Notes |
| --- | --- |
| Acoustic wake | sherpa-onnx KeywordSpotter 3.3M, always-on frame feeding; server-side shape gate (Xiao-X Xiao-Y + lead-in whitelist) as second layer |
| Capture enhancement | ES8388 4ch (live mic + spare slot + 2 digital loopback refs) → VQE plugin with aec/anr/agc/bf enabled; 52 dB end-to-end echo suppression at 1 kHz |
| Barge-in | Speak during answer / music / radio playback to interrupt; three-way control-thread coverage |
| Dialogue | Dual-gateway hybrid routing: cloud qwen-flash + cloud ASR ⇄ local RKLLM Qwen2.5-1.5B, per-turn automatic switching |
| Noise immunity | Server-judged noise → silent turn close; the frontend ends silent turns **without granting a free follow-up window**; single-filler-token gate; short-text gate keeps <4-char noise out of the LLM |
| Online music / radio | MCP streaming music + SomaFM radio; voice control for volume / next / station / stop during playback; questions during playback **auto-yield the music and resume after the answer**, pure wake is ignored without interrupting |
| Timers / alarms | Chinese clock-style expressions + cloud-ASR variants; announce channel proactively speaks due reminders; sqlite globalized across reconnects |
| Clock / repeat | Fully offline, colloquial UTC+8 phrasing, instant answer |
| Local voiceprint | campplus 192-d: enrollment / "who am I" / LLM personalization injection |
| Home Assistant | Mi Home light/plug voice control with zero LLM via HA REST (master-device whitelist prevents mass-toggle damage); on-board at 8123 |
| Watchdog self-healing | Main loop stalled 120 s → automatic reboot → unattended boot-chain recovery |

## Supported Platforms

| Platform | Mode | NPU | Status |
|---|---|---|---|
| RK3576 | Dual-mode (cloud + fully on-device) | yes (ASR refine + local LLM) | **verified** (primary target of this repo) |
| RK3308 | Cloud audio | no | cloud-only end-to-end **passed on-device** (2026-10-21 board test, gateway hosted off-board) |

Shared core code is not forked per SoC; platform configuration lives under
`ports/<soc>/`. `XR_BACKEND` selects the service route; `XR_LOCAL_ASR=off|rknn`
independently selects local recognition capability. RK3576 defaults are unchanged.

---

## 1. System Diagrams

### 1.1 Overall topology

```
                 RK3576 board (Buildroot 2024.02, kernel 6.1.99 #69)
┌───────────────────────────────────────────────────────────────────────┐
│                                                                       │
│  ┌─────────────────────┐  WebSocket (xiaozhi v1)     ┌──────────────┐ │
│  │  xiaorui_live (C)   │ ──────── 8001 ───────────▶ │ Cloud gateway │ │
│  │  single-process     │ ◀──── detect/text ──────── │ qwen-flash    │ │
│  │  KWS+VAD+ASR+play   │                           │ cloud ASR     │ │
│  │                     │ ──────── 8002 ───────────▶ ├──────────────┤ │
│  │  XR_BACKEND=hybrid  │ ◀──── local, fully ─────── │ Local gateway │ │
│  └────────┬────────────┘        offline             │ RKLLM@8112    │ │
│           │                                          │ paraformer    │ │
│  ALSA     │ xr_vqe_16k / xr_spk                     │ matcha+intents│ │
│  ┌────────▼────────────┐                           └──────┬─────────┘ │
│  │ ES8388 (named card)│                                  │ REST      │
│  │ 4ch=mic+spare+2×ref │                           ┌──────▼─────────┐ │
│  │ SAI1 SDI1 loopback  │                           │ Home Assistant │─┼─▶ Mi Cloud
│  └─────────────────────┘                           │ 8123 + Mi Home │ │
│                                                    └────────────────┘ │
└───────────────────────────────────────────────────────────────────────┘
   Ethernet                       crond */5 gateway watchdog        USB adb
```

Hybrid mode switches per turn: cloud gateway unreachable or unanswered → the local
gateway seamlessly continues; the local gateway periodically probes cloud recovery.
**Both gateways judging noise → silent receipt, the frontend closes the turn silently.**

### 1.2 Frontend internals (board/, 39 .c files)

```
 ALSA capture 16k/1ch (xr_vqe_16k, period 1600 EXACT)
     │
     ▼
 ┌──────────┐ hit      ┌────────────┐ end-of-utt ┌──────────────┐
 │ KWS 3.3M  │────────▶ │ dual-thr   │──────────▶ │ 20s RKNN ASR  │ 0.56s refine
 │ thr=0.10  │ always-on│ VAD        │            │ paraformer    │ (audio turns→cloud)
 └──────────┘          └────────────┘            └──────┬───────┘
                                                          │ text/audio turn
     ┌──────────────── session FSM voice_session.c ─────────────────┐
     │ LISTEN⇄RESPOND; follow-up window 30s — only turns that really│
     │ produced audio renew it; noise/zero-audio turns close silently│
     │ without granting the window; echo guard; 20s absolute bound   │
     └────────────────────────────┬──────────────────────────────────┘
                                  ▼
                     backend_hybrid ─┬─ backend_cloud (8001)
                                      └─ backend_local  (8002)
                                  │
                    TTS opus 24k ─▶ xr_spk playback thread (duck / 3-way interrupt)
```

### 1.3 Audio capture chain (the AEC key)

```
 ES8388 ──left mic PGA+24dB (numid52=8)──────────┐ ch0 live mic
         └right slot reserved (unwired)──────────┼ ch1 silent
 SAI1 loopback (numid22=1, src=SDO0 numid18=0)───┼ ch2 refL ┐ digital refs
                                                  └ ch3 refR ┘ (playback tap)
     4ch@48k  hw:rockchipes8388,0
        │
        ▼  rkaudio_vqe ALSA plugin (Rockchip VQE, fixed build)
   aec+anr+agc+bf on, everything else off; ref_channels=2, out_channels=1
        │   measured 52 dB end-to-end suppression at 1 kHz
        ▼  rkaudio_vqe_48k (slave period_size=4800 ← 1600×3 grid fit)
        ▼  xr_vqe_16k = thin plug, 48k→16k only ──▶ frontend

 Playback: TTS 24k/1ch ─▶ xr_spk (plug→hw:rockchipes8388,0 48k/2ch) ─▶ on-board speaker
```

---

## 2. Repository Layout

```
board/                  frontend C sources (xiaorui_live, 39 .c files)
board-server/           gateways (forked from xiaozhi-esp32-server, Python)
  pkg/core/handle/      intent families (12-family registry) + wake/noise/short-text gates
  start-gateway-instance.sh   dual-instance launcher (runtime.env + pidfile + release)
  es8388-aec-mixer.sh   capture-mixer setup script
  xiaorui.env.example   frontend environment template
  config/               audio configs (asound-vqe / asound-novqe / VQE json / wake words)
tools/                  build + all test entry points (§6)
tests/                  unit tests + acoustic suites (audio/ corpus not committed, gen scripts provided)
```

## 3. On-Board Environment & Model Inventory

### 3.1 /data layout (~4.9 GB total, all rebuildable from repo + backup)

| Directory | Size | Contents | Used by |
|---|---|---|---|
| `xiaorui-server/` | 1.6G | frontend binary (board-scripts/), RKLLM model, matcha, start-all.sh, xiaorui.env, ha_token.txt, mdns-ha.py, llm_rkllm.py | frontend + RKLLM + mDNS |
| `ha-env/` | 1.4G | Python 3.14 venv for HA 2026.10.2 (incl. cross-compiled pymicro-vad / pyspeex-noise wheels) | Home Assistant |
| `miniforge/` | 630M | Python 3.14 runtime (conda, deduped & slimmed: version-alias hardlinks unified, package manager/headers removed) | gateways + RKLLM launcher + HA borrows libstdc++ |
| `xiaorui-gateway/` | 119M | instances/ (dual profiles), releases/ (current + rollback copies; test candidates cleaned after use), deps/ (MCP music), voiceprint store, timer sqlite; models symlinked to the shared `/data/xiaorui-server/server/models`, no per-instance copies | both gateways |
| `xiaorui-ffmpeg/` | 588M | ffmpeg 9.0.2 bundle (headless conda; hard-links openvino 107M — do not delete) | music/radio streaming, HA |
| `rknn-test/sherpa/` | 566M | KWS/ASR/voiceprint models + sherpa-onnx v1.13.8 runtime | frontend (embedded paths) + gateway voiceprint |
| `kws-corp/` | 14M | wake corpus, positive+negative with transcripts | acoustic regression |
| `ha-config/` | 7.7M | HA config + .storage (Mi Home OAuth binding lives here) | Home Assistant |

### 3.2 Runtime environments (three independent bundles, not shared)

| Environment | Version | Role | Key constraint |
|---|---|---|---|
| `miniforge` | Python 3.14.7 | gateway app.py + llm_rkllm.py (RKLLM daemon) | — |
| `ha-env` | Python 3.14.7 venv | homeassistant + mdns-ha.py | **must start with `LD_LIBRARY_PATH=/data/miniforge/lib`** (system libstdc++ lacks CXXABI_1.3.15) |
| `xiaorui-ffmpeg` | ffmpeg 9.0.2 | music-chain stream decode → PCM → opus | symlink `/usr/bin/ffmpeg`; openvino is a hard NEEDED dependency |
| VQE libs | Rockchip VQE quartet | capture enhancement | `librkaudio_{vqe,common,nn}.so` → `/usr/lib/`; fixed plugin → `/usr/lib/alsa-lib/`; json+wopt → `/usr/share/rkaudio/` |

### 3.3 Resident processes & ports

| Process | Executable | Port |
|---|---|---|
| xiaorui_live (single mic owner) | `/data/xiaorui-server/board-scripts/xiaorui_live` | — |
| Cloud gateway 8001 | miniforge python + releases/<id>/server/app.py | WS **8001** / health **8004** |
| Local gateway 8002 | same (may share the release with 8001) | WS **8002** / health **8005** |
| RKLLM inference service | `llm_rkllm.py --model qwen25-1.5b…` | **8112** (managed exclusively by 8002) |
| Home Assistant | ha-env python -m homeassistant -c /data/ha-config | **8123** |
| mdns-ha responder | ha-env python mdns-ha.py | :5353 (homeassistant.local → board IP) |
| crond watchdog | watch-gateways.sh */5 | self-heals both gateways + RKLLM (runs both launchers) |

### 3.4 Model inventory

| Model | Purpose / loader | Size | On-board path | Source |
|---|---|---|---|---|
| Qwen2.5-1.5B w4a16 | local-gateway LLM; RKLLM@8112 (8002 exclusive), short answers + prompt cache measured first token 0.2s / full sentence 1.5s | 1.4G | `/data/xiaorui-server/qwen25-1.5b-rk3576-w4a16.rkllm` | RKLLM conversion, **not in git**, see §5.3 (a 0.5B variant was dropped — unreliable identity answers) |
| Paraformer-large (20s single-pass) | frontend recognition refine (RKNN NPU 0.56s/utterance; streaming deleted); cloud ASR replaces it for cloud turns | 458M | `/data/rknn-test/sherpa/model-20s` | sherpa-onnx RKNN conversion |
| KWS wake 3.3M | acoustic wake, always-on, threshold 0.10; **keywords.txt v1.13.8 format = inline prefix `@name :boost tokens` — the three-field format errors out** | 5.5M | `/data/rknn-test/sherpa/model-kws` | ModelScope |
| campplus 192-d | voiceprint enrollment / "who am I"; loaded by gateway voiceprint.model | 27M | `/data/rknn-test/sherpa/model-voiceprint` | ships with sherpa |
| Matcha zh-baker (icefall) | local TTS for both gateways (plain voice, fully offline) | 92M | `/data/xiaorui-server/tts-matcha/matcha-icefall-zh-baker` | hf-mirror direct link |
| wopt 2mic linear 60mm | VQE bf beam weights | 80K | `/usr/share/rkaudio/wopt_2mic_linear_60mm_*.bin` | ships with the Rockchip VQE package (2mic_2ref config) |
| Cloud models (qwen-flash + cloud ASR) | in the cloud, no board resources | — | dashscope | — |

The sherpa-onnx v1.13.8 runtime (libonnxruntime / libsherpa-*.so) lives at
`/data/rknn-test/sherpa/sherpa-onnx-*/lib` and is injected into the frontend via
`LD_LIBRARY_PATH` by start-all.sh.

## 4. Configuration Reference

### 4.1 Frontend environment variables (`/data/xiaorui-server/xiaorui.env`, must be exported)

| Variable | Default | Description |
|---|---|---|
| `XR_BACKEND` | local | `hybrid` (production) / `cloud` / `local` |
| `XR_CLOUD_URL` | — | cloud gateway ws URL (on-board `ws://127.0.0.1:8001/xiaorui/v1/`) |
| `XR_LOCAL_PORT` | 8002 | local gateway port |
| `XR_MIC` | asr_mic | capture device; ES8388 AEC chain = `xr_vqe_16k` (the device name is given here — the core stays platform-agnostic) |
| `XR_MIC_PERIOD` | 1600 | capture period (frames); the 3576 VQE chain's 48k/4800 lands at 1600@16k, the RK3308 plugin only accepts 256 |
| `XR_SPK` | xr_spk | playback device name (plug → hw:rockchipes8388,0, 48k/2ch) |
| `XR_KWS_THRESHOLD` | 0.15 | wake threshold; measured **0.10** for ES8388+bf |
| `XR_CLOUD_ASR` | 0 | upload audio turns for cloud transcription (hybrid production = 1) |
| `XR_MUSIC_CONTROLS` | 0 | voice control during playback |
| `XR_THINK_CUE_MS` | — | threshold for the "hmm" waiting cue |
| `XR_TEXT_INJECT` | — | debug hook: inject a text turn (**bypasses the cold wake gate**) |
| `XR_UTT_DUMP` | — | debug: dumps `<dir>/raw.pcm`+`utt-NNN.wav` (main loop: rolling raw frames / pre-upload utterance) and `ctrl-raw.pcm`+`ctrl-utt-NNN.wav` (same for the playback control thread, for KWS-miss analysis); zero overhead when unset |
| `XR_VAD_TRIGGER_DB` / `XR_VAD_SUSTAIN_DB` / `XR_VAD_PREROLL` | 6 / 2 / 5 | trigger gate, sustain gate (dB relative to the noise floor) and pre-roll frames |

### 4.2 Audio nodes (/etc/asound.conf; VQE version = board-server/config/asound-vqe.conf)

- `rkaudio_vqe_48k`: the VQE plugin. The slave attaches **directly to hw:rockchipes8388,0** (no
  dsnoop — the plugin does not support refine); `period_size 4800` (3× the
  frontend's EXACT 1600@16k; 768 fails to open); `ref_channels 2` +
  `out_channels 1` (native mono output — wrapping it in route/plug for channel
  conversion fails with "Slave PCM not usable").
- `xr_vqe_16k`: thin plug, 48k→16k only.
- `xr_spk`: plug → hw:rockchipes8388,0, 48k/2ch playback.

### 4.3 VQE switches (/usr/share/rkaudio/config_aivqe_new.json)

Production setting: **aec / bf / agc / anr enabled, everything else off**; agc =
max_gain 15 / max_peak -10; bf uses the bundled 2mic linear 60mm wopt. The three
capture-mixer settings must be re-applied on every boot (start-all.sh already calls
`/data/es8388-aec-mixer.sh`): `numid 52=8` (left mic PGA), `22=1` (SAI1 SDI1
loopback on), `18=0` (loopback source = SDO0).

### 4.4 Gateway dual instances (release mechanism)

An instance = `instances/<role>/runtime.env` (pointing at a `releases/<id>/server`
code directory + work/data directories) + `run/server.pid`. Start/restart **only**
via `start-gateway-instance.sh <role>` with pidfile liveness; code changes = create
a candidate release → point runtime.env at it → pidfile restart; rollback = point
back to the old release. **8001/8002 may share one release — never edit shared code
in place.** The HA side reads `XR_HA_URL` / `XR_HA_TOKEN`.

## 5. Reproduction Guide (bare board → full stack)

### 5.1 Prerequisites (two capture routes)

- RK3576 EVB1 + firmware built from **RK3576_LINUX6.1_SDK**
- USB adb (`2207:0006` system / `2207:350e` loader); upgrade_tool v2.30

**Route A: no VQE (repo-only, nothing proprietary needed)** — see §5.2.
**Route B: VQE AEC capture (current production)** — apply to Rockchip for the
VQE artifacts (algorithm library quartet + ALSA plugin + wopt weights,
**.so binaries only, not distributed with this repo**). Also
flash the kernel patch `patches/es8323-capture-channels-16.patch` (capture
channels_max 2→16, a hard prerequisite for SAI-loopback 4ch capture;
`./build.sh rockchip_rk3576_evb1_v10_defconfig && ./build.sh kernel` produces the
FIT boot.img; `di -b` flashes the boot partition only). Measured echo suppression:
52 dB at 1 kHz. Configuration in §4.2/§4.3 and board-server/config/.

### 5.2 No-VQE Capture Configuration (Route A, repo-only)

Runs the full voice stack (wake / dialogue / intents / HA control) without any
proprietary component — just no echo cancellation. Config:
`board-server/config/asound-novqe.conf`; two mic options:

| Option | XR_MIC | Requirement | Notes |
|---|---|---|---|
| USB mic | `asr_mic` (frontend default) | ZR USB sound card | most stable, zero mixer setup, -75dB noise floor |
| On-board ES8388 direct | `xr_mic_16k` | stock kernel is enough (2ch) | re-apply `amixer -c rockchipes8388 cset numid=52 8` (left mic PGA) every boot — hook it into start-all.sh |

```sh
adb push board-server/config/asound-novqe.conf /etc/asound.conf
# xiaorui.env per §4.1; KWS threshold 0.15 (the 0.10 of the VQE route assumes bf/agc-boosted levels)
```

Expectations vs the VQE route: no AEC — during playback the frontend echo guard
mitigates self-hearing; interruption stays reliable in quiet rooms, less so in
noise; no ANR/AGC, recognition is more noise-sensitive; ES8388 direct capture is
noisier/less sensitive than the USB mic (prefer USB for wake stability). Neither
option needs the kernel patch or the SAI-loopback mixer.

### 5.3 Models & dependencies (large files are not in git)

The git repo contains **no models or large binaries** (`.gitignore`: `*.rkllm
*.onnx *.rknn *.so *.wav miniforge*/ tts-*/ wheels*/`; the small audio cues under
board-server/cues/ are the one exception). After cloning, run:

```sh
tools/download-models.sh            # auto-downloads public items + sha256 checks; --list to view
```

| Item | Size | How to obtain |
|---|---|---|
| Matcha zh-baker | 92M | **auto-downloaded by the script** (official sherpa-onnx release, sha verified) |
| KWS 3.3M acoustic | 5.5M | **auto-downloaded by the script** (ModelScope `pkufool` mirror; all four files byte-identical to the on-board copies — verified; GitHub official packages differ by sha, do not switch sources); **keywords.txt: use the repo-root copy** |
| campplus voiceprint | 27M | **auto-downloaded by the script** (official sherpa release, tag `speaker-recongition-models` — the typo is upstream's; byte-identical to the on-board copy — verified) |
| Paraformer RKNN trio | 458M | **auto-downloaded by the script** (official sherpa-onnx rk3576 prebuilt package, same source as the sister repo [rk3576-asr-live](https://github.com/piaozhiye/rk3576-asr-live) setup_models.sh) |
| Qwen2.5-1.5B rkllm | 1.4G | auto-downloadable via the script from an **HF-hosted prebuild** (`RKNNAI/RK3576-LLM-Qwen2.5-1.5B-Instruct`; filename discovered at runtime, automatic hf-mirror fallback, repo page carries SHA-256 for manual checks; **the org's official status is unverified** — Rockchip's own distribution channel is the rkllm_model_zoo drive linked from the [airockchip/rknn-llm](https://github.com/airockchip/rknn-llm) README, fetch code rkllm; **not byte-identical** to this project's file — measured 1.08G vs 1.4G, different conversion variants, switching requires aligning llm_rkllm.py params); for byte-exact reproduction self-provide the exact file (sha in the script); toolchain + runtime at [airockchip/rknn-llm](https://github.com/airockchip/rknn-llm) |
| sherpa-onnx RKNN runtime | 47M | **auto-downloaded by the script** (vendored trio from the sister repo) |
| wopt bf weights + VQE libs/plugin | ~1M+26M | **apply to Rockchip for the .so files** (route B only, all binaries, not distributed with this repo) |
| miniforge / ha-env / ffmpeg bundles | 2.6G+588M | restore from a board backup (conda environments cannot be rebuilt by script) |

Policy: **everything publicly downloadable carries a sha256 anchor and is verified
automatically**; self-made/proprietary items are marked SELF in the script with
fingerprints and provenance; a missing item makes the script exit non-zero — it
never silently installs half. Per-component licenses and commercial status: §8.1
(the single exception, KWS data lineage, is §8.2).

### 5.4 Build

```sh
./build-board-live.sh        # → board/xiaorui_live (buildroot aarch64 toolchain,
                             #   -march=armv8-a to avoid LSE, file-prefix-map for stable SHAs)
tools/check-no-lse.sh        # the LSE gate must pass
```

### 5.5 Board provisioning (push piece by piece from the repo; no prebuilt package)

The on-board directory layout is embedded in the frontend/gateway — paths must match:

```
/data/xiaorui-server/            frontend + models + startup scripts
  board-scripts/xiaorui_live     build output
  start-all.sh xiaorui.env llm_rkllm.py mdns-ha.py
  qwen25-1.5b-rk3576-w4a16.rkllm tts-matcha/
/data/xiaorui-gateway/
  releases/<id>/server/          gateway code (= board-server/pkg contents)
  instances/8001|8002/           runtime.env + work/ + data/ (config holds secrets, self-provided)
  start-gateway-instance.sh watch-gateways.sh
/data/rknn-test/sherpa/          model-kws/ model-20s/ model-voiceprint/ + sherpa-onnx runtime lib/
```

Provision (run from the repo root):

```sh
adb shell 'mkdir -p /data/xiaorui-server/board-scripts /data/xiaorui-gateway/releases/r1/server \
  /data/xiaorui-gateway/instances/8001/work /data/xiaorui-gateway/instances/8001/data \
  /data/xiaorui-gateway/instances/8002/work /data/xiaorui-gateway/instances/8002/data \
  /data/rknn-test/sherpa /usr/share/rkaudio'
adb push board/xiaorui_live /data/xiaorui-server/board-scripts/
adb push board-server/start-all.sh board-server/llm_rkllm.py board-server/mdns-ha.py /data/xiaorui-server/
( cd board-server/pkg && tar cf - . ) | adb shell 'tar xf - -C /data/xiaorui-gateway/releases/r1/server'
adb push board-server/start-gateway-instance.sh board-server/watch-gateways.sh /data/xiaorui-gateway/
```

Model placement (sources in §5.3, paths in §3.4): the `model-kws / model-20s /
model-voiceprint` directories plus the sherpa-onnx runtime → `/data/rknn-test/sherpa/`;
`tts-matcha/` and the qwen rkllm → `/data/xiaorui-server/`; `keywords.txt`
(repo root) → inside `model-kws/`.

Gateway instances & runtime:

```sh
# Both instance runtime.env files: copy from board-server/instance.env.example and edit
#   8001: XR_HTTP_PORT=8004   8002: XR_HTTP_PORT=8005; CODE_DIR → releases/r1/server
# Gateway python env: conda python3.14 + pip install -r board-server/pkg/requirements.txt
#   (production uses /data/miniforge; any equivalent works; llm_rkllm also needs
#    librkllmrt.so and the RKLLM runtime)
```

Audio config & system bits:

```sh
adb push board-server/config/asound-vqe.conf /etc/asound.conf        # route A: asound-novqe.conf instead
adb push board-server/config/config_aivqe_new.json /usr/share/rkaudio/   # route B
adb push board-server/es8388-aec-mixer.sh /data/                      # route B, every boot (start-all hooks it)
adb shell 'chmod +x /data/xiaorui-server/board-scripts/xiaorui_live /data/xiaorui-server/*.sh \
  /data/xiaorui-gateway/*.sh /data/es8388-aec-mixer.sh'               # adb push drops exec bits
adb push board-server/xiaorui.env.example /data/xiaorui-server/xiaorui.env  # edit per §4.1 (backend/URL/threshold)
cat > /tmp/S99xrvoice <<'EOS'
#!/bin/sh
sh /data/xiaorui-server/start-all.sh > /tmp/xr_start.log 2>&1 &
EOS
adb push /tmp/S99xrvoice /etc/init.d/ && adb shell 'chmod +x /etc/init.d/S99xrvoice'
adb shell '(crontab -l 2>/dev/null; echo "*/5 * * * * /data/xiaorui-gateway/watch-gateways.sh >/dev/null 2>&1") | crontab -'
```

Route-B Rockchip VQE artifacts (the .so files you applied for): quartet libs → `/usr/lib/`,
ALSA plugin → `/usr/lib/alsa-lib/`, wopt weights → `/usr/share/rkaudio/`.
Optional components: Home Assistant (8123, for device control; install your own venv,
**start it with `LD_LIBRARY_PATH` pointing at a directory with a newer libstdc++**,
and never run a second instance against the same .storage); ffmpeg bundle →
symlink `/usr/bin/ffmpeg` (online music/radio chain).

### 5.6 Start & verify

```sh
adb shell 'sh /data/xiaorui-server/start-all.sh'
adb shell "ss -tln | grep -E ':8001|:8002'"      # 8002 needs ~30s for RKLLM
adb shell 'tail -f /tmp/xr_live.log'             # expect "mic=xr_vqe_16k 聆听中" (listening)
adb forward tcp:18123 tcp:8123       # channel used by the HA acoustic suite
```

## 6. Test System

| Entry point | Coverage | Notes |
|---|---|---|
| `tools/run-gates.sh` | **40** host gates: unit tests / intent families / cross builds / no-LSE / reference tracking / private weather credentials / logger fallback | all green before commit |
| `tools/run-ha-acoustic.sh` | light-control acoustic loop (PC plays corpus → wake → intent → HA state flip, triple evidence, single-attempt scoring) | plays corpus audibly; needs a live HA instance |
| `tools/run-wake-silent-live.py --apply` | full isolated candidate run: 180s quiet + wake positives/negatives + 3 HA rounds, auto-restores production afterward | the only path to promote a candidate |
| `tools/test-music-candidate.py --apply --acoustic` | isolated music-playback candidate suite: gateway yield/resume protocol probe + three acoustic legs (pure-wake ignored without forwarding / question yield + resume / stop interrupt without disconnect; PC plays the corpus); auto-restores production afterward | `--pure-wake-wav` explicit corpus (level/clipping checks + fingerprint recorded), `--utt-dump` pulls board-side mic recordings and levels |
| `tools/gen-*.py` | matcha corpus synthesis (loudness aligned to rms≈4558) | tests/audio not committed |

**Acoustic timing discipline**: any "play corpus → wait for answer" loop must wait
for the frontend log to be genuinely quiet (`wait_board_quiet`, mtime ≥32s) before
playing the next sample — fixed sleeps overlap with the next round once answer
chains grow. HA rounds are spaced ≥35s (the follow-up window is 30s).

Intent registry (intentHandler.py, order is semantic): `reboot → volume → ha-light →
vp-enroll → whoami → repeat → time → next → radio → timer-list → timer-set →
clock(raw)`. Three gates before the LLM: single filler-token noise gate →
wake-word/shape prefix strip (lead-in whitelist, max two chars) → short-text gate
(<4 chars answers "didn't catch that" without the LLM).

## 7. Known Limitations & Notes

1. **LSE**: the toolchain's default architecture emits `casal`, which SIGILLs on
   ARMv8.0 — all cross builds use `-march=armv8-a`, gated by `check-no-lse.sh`.
2. **Frontend period EXACT semantics**: the VQE plugin slave period must be 4800
   (= 1600×3).
3. **busybox ldd fails silently**: ffmpeg really links openvino while ldd reports
   nothing — deleting based on ldd breaks it (openvino 107M must stay).
4. **HA venv**: must run with `LD_LIBRARY_PATH=/data/miniforge/lib` (system
   libstdc++ lacks CXXABI_1.3.15); **two HA instances writing the same .storage
   wipe the password hash** — single instance is an iron rule.
5. **Process management**: `pkill -x` mismatches names longer than 15 chars
   (check liveness via /proc/*/exe); kill gateways only via
   `instances/<role>/run/server.pid`; background processes from adb shell need
   the `(setsid … &)` subshell form.

## 8. Licensing & Acknowledgements

### 8.1 License overview

| Component | License | Commercial use |
|---|---|---|
| This project's own code | **MIT** | ✅ |
| Gateway upstream [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) | MIT | ✅ |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) runtime | Apache-2.0 | ✅ |
| [Home Assistant](https://www.home-assistant.io/) | Apache-2.0 | ✅ |
| RKLLM/RKNN runtime (librkllmrt/librknnrt) | Check the license shipped with the exact runtime version, not just a repository-level license | Verify before redistribution |
| Qwen2.5-1.5B rkllm model | Apache-2.0 (declared by [RKNNAI](https://huggingface.co/RKNNAI/RK3576-LLM-Qwen2.5-1.5B-Instruct); Qwen2.5 base is Apache-2.0) | ✅ |
| Paraformer RKNN / campplus / Matcha TTS | Apache-2.0 (sherpa releases / 3D-Speaker) | ✅ |
| KWS wake model (wenetspeech 3.3M) | Apache-2.0 (declared by author pkufool); **exception in 8.2** | ⚠️ assess |
| Rockchip VQE artifacts (algorithm libs / ALSA plugin / wopt weights) | Rockchip SDK terms | per SDK terms |
| RK3576_LINUX6.1_SDK / firmware | Rockchip SDK terms | per SDK terms |

### 8.2 KWS models and training data

The KWS model author declares Apache-2.0 on ModelScope (model layer is
commercial-friendly), but its training corpus WenetSpeech is scraped web audio whose
dataset itself is research-only — whether data restrictions propagate to model
weights is legally unsettled. Note the same author's GigaSpeech variant is an
**English model** (English BPE units, cannot spot Chinese wake words). Check both
model and training-data terms when selecting a model, or self-train using clearly
authorized Chinese data (icefall). This project makes no blanket permission claim
for commercial or demo use.

Full upstream copyright notices: THIRD-PARTY-NOTICES.md.

### 8.3 Acknowledgements

- [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) — gateway upstream
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) / pkufool's KWS models — wake & recognition
- [Home Assistant](https://www.home-assistant.io/) — device-control hub
- Rockchip RKLLM / RKNN / rkaudio VQE — on-device inference and speech enhancement

---

## 9. WeChat Account

More hands-on articles about embedded / on-device AI (in Chinese) on this
WeChat official account:

<p align="center">
  <img src="assets/wechat-account-qr.jpg" width="240" alt="WeChat official account QR code" />
</p>
