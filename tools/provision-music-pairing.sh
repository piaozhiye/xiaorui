#!/bin/sh
# Generate a private local CA and provision HTTPS + one-time QR pairing on an
# RK3562 Snapcast music server. The CA private key stays on the provisioning PC.
set -eu

usage() {
    cat >&2 <<'EOF'
usage: provision-music-pairing.sh --serial ADB_SERIAL [options]
       provision-music-pairing.sh --generate-only [options]

Options:
  --serial SERIAL       Explicit ADB serial for the 4G music-server board
  --hostname HOST       QR hostname (default: xiaorui-music.local)
  --ip ADDRESS          Board IPv4 (read from eth0 when deploying)
  --ca-dir DIR          Persistent CA key directory (default: OUTPUT_DIR/ca)
  --output-dir DIR      Certificates / public CA output (default: .local-provisioning)
  --generate-only       Generate certificates without contacting a board
EOF
    exit 2
}

SERIAL=
HOSTNAME=xiaorui-music.local
BOARD_IP=
GENERATE_ONLY=0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
OUTPUT_DIR=${XR_MUSIC_PROVISIONING_OUTPUT:-$REPO_ROOT/.local-provisioning}
CA_DIR=${XR_MUSIC_CA_DIR:-}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --serial) [ "$#" -ge 2 ] || usage; SERIAL=$2; shift 2 ;;
        --hostname) [ "$#" -ge 2 ] || usage; HOSTNAME=$2; shift 2 ;;
        --ip) [ "$#" -ge 2 ] || usage; BOARD_IP=$2; shift 2 ;;
        --ca-dir) [ "$#" -ge 2 ] || usage; CA_DIR=$2; shift 2 ;;
        --output-dir) [ "$#" -ge 2 ] || usage; OUTPUT_DIR=$2; shift 2 ;;
        --generate-only) GENERATE_ONLY=1; shift ;;
        -h|--help) usage ;;
        *) echo "unknown option: $1" >&2; usage ;;
    esac
done

[ -n "$CA_DIR" ] || CA_DIR=$OUTPUT_DIR/ca

case "$HOSTNAME" in
    ''|*[!A-Za-z0-9.-]*|.*|*.|*..*) echo "invalid hostname: $HOSTNAME" >&2; exit 2 ;;
esac
if [ "$GENERATE_ONLY" -eq 0 ]; then
    [ -n "$SERIAL" ] || usage
    command -v adb >/dev/null 2>&1 || { echo "adb is required" >&2; exit 2; }
    adb -s "$SERIAL" get-state 2>/dev/null | grep -qx device || {
        echo "device unavailable: $SERIAL" >&2; exit 2;
    }
    if [ -z "$BOARD_IP" ]; then
        BOARD_IP=$(adb -s "$SERIAL" shell 'ifconfig eth0' 2>/dev/null |
            sed -n 's/.*inet addr:\([0-9.][0-9.]*\).*/\1/p' | tr -d '\r' | head -1)
    fi
    case "$BOARD_IP" in
        ''|*[!0-9.]*|*..*) echo "could not determine board IPv4; pass --ip" >&2; exit 2 ;;
    esac
fi

command -v openssl >/dev/null 2>&1 || { echo "openssl is required" >&2; exit 2; }
mkdir -p "$CA_DIR" "$OUTPUT_DIR"
chmod 700 "$CA_DIR"
CA_KEY=$CA_DIR/root-ca.key
CA_CERT=$CA_DIR/root-ca.crt

if [ -e "$CA_KEY" ] || [ -e "$CA_CERT" ]; then
    [ -f "$CA_KEY" ] && [ -f "$CA_CERT" ] || {
        echo "incomplete CA material in $CA_DIR; preserve it and choose a new --ca-dir" >&2
        exit 2
    }
    chmod 600 "$CA_KEY"
    KEY_PUB=$(openssl pkey -in "$CA_KEY" -pubout 2>/dev/null | openssl sha256)
    CERT_PUB=$(openssl x509 -in "$CA_CERT" -pubkey -noout 2>/dev/null | openssl pkey -pubin -outform PEM 2>/dev/null | openssl sha256)
    [ "$KEY_PUB" = "$CERT_PUB" ] || {
        echo "CA certificate does not match private key in $CA_DIR" >&2; exit 2;
    }
else
    umask 077
    openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 \
        -keyout "$CA_KEY" -out "$CA_CERT" \
        -subj "/CN=Xiaorui Music Local CA" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash"
    chmod 600 "$CA_KEY"
    chmod 644 "$CA_CERT"
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
LEAF_KEY=$OUTPUT_DIR/music-ui.key
LEAF_CERT=$OUTPUT_DIR/music-ui.crt
ROOT_COPY=$OUTPUT_DIR/xiaorui-music-root-ca.crt
cp "$CA_CERT" "$ROOT_COPY"
chmod 644 "$ROOT_COPY"

cat >"$TMP/leaf.ext" <<EOF_EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=@san
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
[san]
DNS.1=$HOSTNAME
EOF_EXT
if [ -n "$BOARD_IP" ]; then
    printf 'IP.1=%s\n' "$BOARD_IP" >>"$TMP/leaf.ext"
fi
umask 077
openssl req -new -newkey rsa:2048 -sha256 -nodes \
    -keyout "$LEAF_KEY" -out "$TMP/music-ui.csr" \
    -subj "/CN=$HOSTNAME"
openssl x509 -req -sha256 -days 825 \
    -in "$TMP/music-ui.csr" -CA "$CA_CERT" -CAkey "$CA_KEY" \
    -CAcreateserial -extfile "$TMP/leaf.ext" \
    -out "$LEAF_CERT"
chmod 600 "$LEAF_KEY"
chmod 644 "$LEAF_CERT"
openssl verify -CAfile "$CA_CERT" "$LEAF_CERT" >/dev/null

if [ "$GENERATE_ONLY" -eq 0 ]; then
    REMOTE=/data/xiaorui/snapcast
    REMOTE_TLS=$REMOTE/tls
    adb -s "$SERIAL" shell "mkdir -p '$REMOTE_TLS' '$REMOTE/music-state'"
    adb -s "$SERIAL" push "$LEAF_CERT" "$REMOTE_TLS/music-ui.crt.new" >/dev/null
    adb -s "$SERIAL" push "$LEAF_KEY" "$REMOTE_TLS/music-ui.key.new" >/dev/null
    adb -s "$SERIAL" shell "chmod 644 '$REMOTE_TLS/music-ui.crt.new' && chmod 600 '$REMOTE_TLS/music-ui.key.new' && mv '$REMOTE_TLS/music-ui.crt.new' '$REMOTE_TLS/music-ui.crt' && mv '$REMOTE_TLS/music-ui.key.new' '$REMOTE_TLS/music-ui.key'"

    adb -s "$SERIAL" pull "$REMOTE/service.env" "$TMP/service.env" >/dev/null
    python3 - "$TMP/service.env" "$HOSTNAME" "$BOARD_IP" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
hostname, board_ip = sys.argv[2:]
settings = {
    "XR_MUSIC_UI": "1",
    "XR_MUSIC_UI_BIND": "0.0.0.0",
    "XR_MUSIC_UI_TLS_CERT": "/data/xiaorui/snapcast/tls/music-ui.crt",
    "XR_MUSIC_UI_TLS_KEY": "/data/xiaorui/snapcast/tls/music-ui.key",
    "XR_MUSIC_UI_PAIRING_STATE": "/data/xiaorui/snapcast/music-state/pairing.json",
    "XR_MUSIC_UI_PAIRING_CODE": "/data/xiaorui/snapcast/music-state/pairing-code.json",
    "XR_MUSIC_UI_PUBLIC_URL": f"https://{hostname}:8765",
    "XR_MUSIC_UI_ALLOWED_HOSTS": f"{hostname},{board_ip}",
    "XR_MUSIC_MDNS_ALIAS": hostname,
    "XR_MDNS_PYTHON": "/data/gwvenv/bin/python",
    "XR_MDNS_HELPER": "/data/xiaorui-server/mdns-ha.py",
}
seen = set()
output = []
for line in path.read_text(encoding="utf-8").splitlines():
    match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", line)
    key = match.group(1) if match else None
    if key not in settings:
        output.append(line)
    elif key not in seen:
        output.append(f"{key}={settings[key]}")
        seen.add(key)
for key, value in settings.items():
    if key not in seen:
        output.append(f"{key}={value}")
path.write_text("\n".join(output) + "\n", encoding="utf-8")
PY
    adb -s "$SERIAL" push "$TMP/service.env" "$REMOTE/service.env.new" >/dev/null
    adb -s "$SERIAL" shell "chmod 600 '$REMOTE/service.env.new' && mv '$REMOTE/service.env.new' '$REMOTE/service.env'"
    adb -s "$SERIAL" shell "$REMOTE/service.sh restart"
    adb -s "$SERIAL" shell "/data/gwvenv/bin/python -c 'import json,ssl,urllib.request; c=ssl._create_unverified_context(); b=urllib.request.urlopen(\"https://127.0.0.1:8765/api/pairing/status\",context=c,timeout=5).read(); d=json.loads(b); assert d[\"enabled\"] and len(d[\"code\"]) == 8 and d[\"public_url\"] == \"https://$HOSTNAME:8765\", d; q=urllib.request.urlopen(\"https://127.0.0.1:8765/api/pairing/qr\",context=c,timeout=5); assert q.headers.get_content_type() == \"image/svg+xml\" and b\"?pair=\" in q.read(), \"QR response invalid\"; print(\"pairing enabled; code valid; QR SVG generated\")'"
fi

echo "Root CA certificate: $ROOT_COPY"
echo "CA private key (keep private, not for phones or Git): $CA_KEY"
echo "Server certificate: $LEAF_CERT"
if [ "$GENERATE_ONLY" -eq 0 ]; then
    echo "Phone URL: https://$HOSTNAME:8765"
    echo "Install the root CA certificate on each phone before scanning the QR code."
fi
