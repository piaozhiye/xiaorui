#!/usr/bin/env python3
"""OpenAI 兼容的桩 LLM(纯 stdlib, 流式 SSE).

给 xiaozhi-esp32-server 当本地"大模型": 不用申请任何 API key 就能把
detect -> LLM -> TTS 全链路跑通. 挂上后服务端 LLM 配置:

    selected_module:
      LLM: StubLLM
    LLM:
      StubLLM:
        type: openai
        model_name: stub-1
        base_url: http://127.0.0.1:8111/v1
        api_key: stub

回复 = 复述用户最后一句话 + 固定尾巴, 好在喇叭里听清是端到端的.
"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8111
TAIL = "。桩模型收到，链路是通的。"


def last_user_text(messages):
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content", "")).strip()
    return ""


def sse_chunk(text):
    return (
        'data: ' + json.dumps({
            "id": "chatcmpl-stub",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }, ensure_ascii=False) + "\n\n"
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[stub] {fmt % args}")

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps({"object": "list", "data": [{"id": "stub-1", "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        text = last_user_text(req.get("messages", []))
        print(f"[stub] chat: {text!r}")
        reply = f"你说的是「{text}」{TAIL}"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write_chunk(b: bytes):
            self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n")

        # 逐字推流, 模拟真实 LLM 的吐字节奏
        for ch in reply:
            write_chunk(sse_chunk(ch).encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.02)
        write_chunk(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


if __name__ == "__main__":
    print(f"[stub] OpenAI-compatible stub on http://127.0.0.1:{PORT}/v1")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
