#!/bin/bash
# 模型/运行库获取脚本 -- 大文件不入 git(.gitignore: *.rkllm *.onnx *.rknn *.so),
# clone 后跑本脚本补齐可公开下载的部分; 自制件(标 SELF)给出指纹与来源指引。
#
# 用法:
#   tools/download-models.sh              # 下载全部公开件到 ./models/ 并核对 sha256
#   tools/download-models.sh matcha kws   # 只处理指定项
#   tools/download-models.sh --list       # 只列清单
# 校验失败/缺自制件时退出码非零; 已存在且 sha 对的项自动跳过。
set -u
cd "$(dirname "$0")/.."
OUT=${XR_MODELS_DIR:-./models}
mkdir -p "$OUT"
OK=0; PENDING=0; FAIL=0

# 名字 | 公开URL 或 SELF | 目标相对路径 | sha256 | 备注
MANIFEST='
matcha|https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/matcha-icefall-zh-baker.tar.bz2|matcha-icefall-zh-baker/model-steps-3.onnx|ff1cc7f7abdedc4eeec668a4a0dc2e72a0c314c5637d5fce885345b490bd08de|TTS 92M(tar 内含 hifigan_v2) -- 已实测(sha 合)
kws-enc|https://www.modelscope.cn/models/pkufool/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/resolve/master/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx|model-kws/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx|017af32f2c0138f931d05fbc009ee864295e910aff304f77d2f563815fc834fb|唤醒声学 encoder(int8 4.8M) -- ModelScope pkufool 镜像, 与板上件逐字节一致(实测); 注意 GitHub 官方 kws-models 包 sha 均不同, 勿换源
kws-dec|https://www.modelscope.cn/models/pkufool/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/resolve/master/decoder-epoch-12-avg-2-chunk-16-left-64.onnx|model-kws/decoder-epoch-12-avg-2-chunk-16-left-64.onnx|bb3d8640cc6a495088707173bc1707a8a4ffe014594fc9adcba8389e27d0339f|唤醒 decoder(实测同上)
kws-join|https://www.modelscope.cn/models/pkufool/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/resolve/master/joiner-epoch-12-avg-2-chunk-16-left-64.onnx|model-kws/joiner-epoch-12-avg-2-chunk-16-left-64.onnx|d990f5b67ae9d740048709f5d5f3e777af442061896a8322d5c3ab3ec480f3cb|唤醒 joiner(实测同上)
kws-tokens|https://www.modelscope.cn/models/pkufool/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/resolve/master/tokens.txt|model-kws/tokens.txt|72316508d9119696145abc6f1f8cdc46287535c34e5ce7e595f845cb1499cf2e|唤醒 tokens(实测同上); keywords.txt 用仓根副本(自定义小瑞词表, 勿用包内样例)
campplus|https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx|model-voiceprint/campplus.onnx|f682b514c05d947ee3fa91cd6ec6c5c7543479a128373fa29b1faedccd21fd11|声纹 27M -- sherpa 官方 release(tag 名即拼错的 recongition), 与板上件逐字节一致(实测)
asr20-rknn|https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-rk3576-20-seconds-paraformer-zh-2025-10-07.tar.bz2|sherpa-onnx-rk3576-20-seconds-paraformer-zh-2025-10-07/encoder.rknn|d9c1e1dc16d62591bdbd025d42322e6770a8266a67fa98c2fad69109fc6b2e1d|Paraformer RKNN 官方预转换包(374M tar, 含 encoder/decoder/predictor.rknn) -- 与姊妹仓 rk3576-asr-live 的 setup_models.sh 同源; 部署时整目录改名为 model-20s
qwen15-rkllm|SELF|qwen25-1.5b-rk3576-w4a16.rkllm|70b4b3c54221892cc403aa877612e0bfdc24abeda5e0011ee3a2e9a3c24e332c|本项目精确件(sha 锚定, 当年即下载所得): 板端备份; 下一条 alt 件为 HF 预转换(非同字节, 见其行注)
qwen15-rkllm-alt|hfr:RKNNAI/RK3576-LLM-Qwen2.5-1.5B-Instruct|qwen25-1.5b-rk3576-w4a16-alt.rkllm||HF 预转换件(RKNNAI 组织, 官方性未核实; Rockchip 官方渠道=rknn-llm README 的 rkllm_model_zoo 网盘, 取码 rkllm; 仓页面附 SHA-256 可手工核; 文件名运行时从仓库 API 发现); 与本项目精确件非同字节(实测: 官方件 1.08G sha df9ebaef…, 本项目件 1.4G sha 70b4b3c5…, 不同转换变体), 换用需同步 llm_rkllm.py 启动参数; HF 不通自动走 hf-mirror
sherpa-rknn-runtime|https://raw.githubusercontent.com/piaozhiye/rk3576-asr-live/main/sherpa/lib/libsherpa-onnx-c-api.so|sherpa-lib/libsherpa-onnx-c-api.so|e3b93a1c9a685bd8d81a3afd5654c4afd0b33a268d4b6efc3eb2c152cf9b454e|RKNN 版 sherpa 运行库(c-api) -- 姊妹仓 rk3576-asr-live vendored 件, 同源直用
sherpa-rknn-runtime2|https://raw.githubusercontent.com/piaozhiye/rk3576-asr-live/main/sherpa/lib/libonnxruntime.so|sherpa-lib/libonnxruntime.so||同上: onnxruntime(34M, 无指纹项)
sherpa-rknn-runtime3|https://raw.githubusercontent.com/piaozhiye/rk3576-asr-live/main/sherpa/lib/librknnrt.so|sherpa-lib/librknnrt.so||同上: RKNN 运行时(7.7M, 无指纹项)
wopt-vqe|SELF|wopt_2mic_linear_60mm_5inputbeam_5outputbeam.bin fdab7c2473957642568c0d2e9b713b23b679cbd07678ef5a92d6700ce1eb766d|bf 波束权重 -- 瑞芯微 VQE 专有(随其发布包 2mic_2ref 配置)
vqe-libs|SELF|libasound_module_pcm_rkaudio_vqe.so 0223aa21b19fd6f5... (修复版) + librkaudio_{vqe,common,nn}.so|瑞芯微 VQE 专有件 -- vendor/ 有修复版插件, 四件套库向瑞芯微申请
rkllm-rt|https://raw.githubusercontent.com/airockchip/rknn-llm/main/rkllm-runtime/Linux/librkllm_api/aarch64/librkllmrt.so|librkllmrt.so|f25e9b099db08aaacd0a3ac62b4697d3951d6ae61ae41ea09f6702cfa89eb32c|RKLLM 官方运行时(airockchip/rknn-llm 仓树直链, 7674816 字节与本项目板上件一致); 注意须与板上 NPU 驱动版本配对
'

check_sha() {  # file sha -> 0 ok; 空 sha 恒不匹配(防"文件缺失+空指纹"误判 OK)
  [ -n "$2" ] && [ -f "$1" ] && [ "$(sha256sum "$1" 2>/dev/null | cut -d' ' -f1)" = "$2" ]
}

[ "${1:-}" = "--list" ] && { echo "$MANIFEST" | awk -F'|' 'NF>2{printf "%-18s %-6s %s\n",$1,($2=="SELF"?"自备":"公开"),$5}'; exit 0; }

while IFS='|' read -r name url dest sha note; do
  [ -z "$name" ] && continue
  if [ $# -ge 1 ] && ! printf '%s\n' "$@" | grep -qx "$name"; then continue; fi
  main=${dest%% *}
  if check_sha "$OUT/$main" "${sha%% *}"; then echo "OK    $name (已有, sha 合)"; OK=$((OK+1)); continue; fi
  if [ "$url" = "SELF" ]; then
    echo "自备  $name -- $note"; PENDING=$((PENDING+1)); continue
  fi
  if [ -z "${sha%% *}" ] && [ -f "$OUT/$main" ]; then
    echo "OK    $name (已有, 无指纹项-跳过校验)"; OK=$((OK+1)); continue
  fi
  case "$url" in
    hfr:*)
      repo=${url#hfr:}
      echo "发现  $name <- HF 仓库 $repo (查询文件清单)"
      list=$(curl -sf --max-time 30 "https://hf-mirror.com/api/models/$repo"         || curl -sf --max-time 30 "https://huggingface.co/api/models/$repo") || true
      file=$(printf '%s' "$list" | tr ',' '\n' | grep -o '"rfilename":"[^"]*\.rkllm"' | head -1 | cut -d'"' -f4)
      if [ -z "$file" ]; then echo "FAIL  $name 无法获取文件清单(HF/hf-mirror 均不可达或仓内无 .rkllm)"; FAIL=$((FAIL+1)); continue; fi
      echo "      文件: $file"
      wget -q -c -O "$OUT/$main" "https://hf-mirror.com/$repo/resolve/main/$file"         || wget -q -c -O "$OUT/$main" "https://huggingface.co/$repo/resolve/main/$file"         || { echo "FAIL  $name 下载失败"; FAIL=$((FAIL+1)); continue; }
      ;;
    *)
      echo "下载  $name <- $url"
      wget -q -c -P "$OUT" "$url" || { echo "FAIL  $name 下载失败(检查网络/代理, HF 资产可试 hf-mirror)"; FAIL=$((FAIL+1)); continue; }
      f="$OUT/$(basename "$url")"
      case "$f" in
        *.tar.bz2|*.tar.gz)
          # The destination is a member of the archive, not the archive itself.
          if ! tar xf "$f" -C "$OUT"; then
            echo "FAIL  $name 解压失败"; FAIL=$((FAIL+1)); continue
          fi
          ;;
        *)
          if [ "$f" != "$OUT/$main" ]; then
            if ! mkdir -p "$(dirname "$OUT/$main")" || ! mv -f "$f" "$OUT/$main"; then
              echo "FAIL  $name 文件落位失败"; FAIL=$((FAIL+1)); continue
            fi
          fi
          ;;
      esac
      ;;
  esac
  if [ ! -f "$OUT/$main" ]; then
    echo "FAIL  $name 目标文件缺失: $main"; FAIL=$((FAIL+1)); continue
  fi
  if [ -z "${sha%% *}" ]; then
    echo "OK    $name (官方/社区预转换件, 不做指纹校验 -- 与本项目 SELF 件非同字节)"
    OK=$((OK+1))
  elif check_sha "$OUT/$main" "${sha%% *}"; then echo "OK    $name (sha256 合)"; OK=$((OK+1))
  else echo "FAIL  $name sha256 不合(期望 ${sha%% *} 实得 $(sha256sum "$OUT/$main" 2>/dev/null | cut -c1-16)...)"; FAIL=$((FAIL+1)); fi
done <<EOF
$MANIFEST
EOF

echo "====  OK=$OK 自备=$PENDING FAIL=$FAIL  (输出目录 $OUT) ===="
[ "$FAIL" -eq 0 ] || exit 1
exit 0
