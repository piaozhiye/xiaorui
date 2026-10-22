#!/bin/sh
# 板端联网环境体检: 出网/DNS/时钟/CA-TLS/云API(连接与鉴权分开报告)
# 位置: /data/xiaorui-gateway/check-network.sh  (BusyBox sh 兼容)
# 用法: sh check-network.sh [keyfile]   # keyfile 缺省用网关私有配置里的 key
# 退出码: 0=无 FAIL  1=有 FAIL (SKIP 不算失败)
# key 只在 python 进程内存里流转, 不回显不入日志.
PY=/data/miniforge/bin/python
GW_CFG=/data/xiaorui-gateway/server/data/.config.yaml
API_HOST=dashscope.aliyuncs.com
PASS=0; FAIL=0; SKIP=0; FAILED_ITEMS=""

ok()   { PASS=$((PASS+1));  echo "PASS $1 | $2"; }
bad()  { FAIL=$((FAIL+1));  FAILED_ITEMS="$FAILED_ITEMS $1"; echo "FAIL $1 | $2"; }
skip() { SKIP=$((SKIP+1));  echo "SKIP $1 | $2"; }

# ---- 1) 出网 (ICMP) ----
if ping -c 2 -W 3 223.5.5.5 >/dev/null 2>&1; then
    ok ping "223.5.5.5 2/2"
else
    bad ping "223.5.5.5 不通(可能禁 ICMP, 看 DNS/TLS 项)"
fi

# ---- 2) DNS ----
DNS_OUT=$($PY - <<'EOF' 2>&1
import socket, sys
try:
    ips = sorted({a[4][0] for a in socket.getaddrinfo("dashscope.aliyuncs.com", 443)})
    print("OK " + ",".join(ips[:3]))
except Exception as e:
    print("ERR " + str(e)[:80])
EOF
)
case "$DNS_OUT" in
    OK*) ok dns "$DNS_OUT" ;;
    *)   bad dns "$DNS_OUT" ;;
esac

# ---- 3) 时钟 + 4) CA/TLS 握手 (一次 TLS 连接同时验证两者) ----
TLS_OUT=$($PY - <<'EOF' 2>&1
import socket, ssl, sys
try:
    ctx = ssl.create_default_context()   # 默认 CA 存储, 禁 verify=False
    raw = socket.create_connection(("dashscope.aliyuncs.com", 443), timeout=8)
    s = ctx.wrap_socket(raw, server_hostname="dashscope.aliyuncs.com")
    tlsv = s.version()
    import email.utils, datetime
    hdr = None
    s.sendall(b"HEAD / HTTP/1.1\r\nHost: dashscope.aliyuncs.com\r\nConnection: close\r\n\r\n")
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 8192:
        chunk = s.recv(1024)
        if not chunk: break
        data += chunk
    for line in data.decode("latin-1", "ignore").split("\r\n"):
        if line.lower().startswith("date:"):
            hdr = line.split(":", 1)[1].strip()
    s.close()
    if not hdr:
        print("ERR HEAD 无 Date 头")
    else:
        srv = email.utils.parsedate_to_datetime(hdr)
        import time
        skew = abs(srv - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        print("OK %s skew=%.0fs" % (tlsv, skew))
except Exception as e:
    print("ERR " + str(e)[:100])
EOF
)
case "$TLS_OUT" in
    OK*skew=*)
        SKEW=$(echo "$TLS_OUT" | sed 's/.*skew=//;s/s$//')
        ok tls "$TLS_OUT"
        if [ "$SKEW" -gt 300 ] 2>/dev/null; then
            bad clock "与服务器偏差 ${SKEW}s (>300s), TLS 暂时靠宽容窗口, 续期即断"
        else
            ok clock "偏差 ${SKEW}s"
        fi
        ;;
    *) bad tls "$TLS_OUT"; skip clock "TLS 未通, 无法取服务器时间" ;;
esac

# ---- 5) 云 API: 连接与鉴权 (LLM + ASR 各一次最小调用) ----
KEYFILE="${1:-}"
API_OUT=$($PY - "$KEYFILE" "$GW_CFG" <<'EOF' 2>&1
import sys, json, yaml
keyfile, cfgpath = sys.argv[1], sys.argv[2]
key = None
try:
    if keyfile:
        key = open(keyfile).read().strip()
    else:
        cfg = yaml.safe_load(open(cfgpath))
        key = cfg["LLM"]["AliLLM"]["api_key"]
except Exception:
    pass
if not key or key == "INJECT_KEY":
    print(json.dumps({"llm": "SKIP", "asr": "SKIP", "reason": "无凭据"}))
    sys.exit(0)

def http_code(path, payload, ctype="application/json", extra=None):
    import urllib.request, urllib.error
    req = urllib.request.Request("https://dashscope.aliyuncs.com" + path,
                                 data=json.dumps(payload).encode(), method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", ctype)
    if extra: req.add_header(extra[0], extra[1])
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return str(type(e).__name__)

llm_code = http_code("/compatible-mode/v1/chat/completions",
    {"model": "qwen-flash", "messages": [{"role": "user", "content": "只回复:收到"}], "max_tokens": 4})
# ASR 鉴权探测: 空音频体也会先过鉴权层, 401/403=鉴权问题, 400=已过鉴权但请求非法
asr_code = http_code("/api/v1/services/aigc/multimodal-generation/generation",
    {"model": "qwen3-asr-flash", "input": {"messages": []}})
def verdict(code):
    if code == 200: return "auth-ok"
    if code in (401, 403): return "auth-fail(%s)" % code
    if code == 429: return "quota(429)"
    if code == 400: return "auth-ok-req-invalid(400)"   # 鉴权已过, 载荷被拒
    if isinstance(code, str): return "conn-%s" % code
    return "http-%s" % code
print(json.dumps({"llm": "%s(%s)" % (verdict(llm_code), llm_code),
                  "asr": "%s(%s)" % (verdict(asr_code), asr_code)}))
EOF
)
LLM_V=$(echo "$API_OUT" | $PY -c "import sys,json; d=json.load(sys.stdin); print(d.get('llm',''))" 2>/dev/null)
ASR_V=$(echo "$API_OUT" | $PY -c "import sys,json; d=json.load(sys.stdin); print(d.get('asr',''))" 2>/dev/null)
if [ -z "$LLM_V" ] && [ -z "$ASR_V" ]; then
    bad api "探测脚本异常: $API_OUT"
else
    case "$LLM_V" in
        SKIP) skip api-llm "无凭据" ;;
        auth-ok*) ok api-llm "qwen-flash $LLM_V" ;;
        *) bad api-llm "qwen-flash $LLM_V" ;;
    esac
    case "$ASR_V" in
        SKIP) skip api-asr "无凭据" ;;
        auth-ok*) ok api-asr "qwen3-asr-flash $ASR_V" ;;
        *) bad api-asr "qwen3-asr-flash $ASR_V" ;;
    esac
fi

echo "----"
echo "RESULT pass=$PASS fail=$FAIL skip=$SKIP${FAILED_ITEMS:+ failed:$FAILED_ITEMS}"
[ "$FAIL" -eq 0 ]
