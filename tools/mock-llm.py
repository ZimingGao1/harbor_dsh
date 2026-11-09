#!/usr/bin/env python3
"""Mock DeepSeek-compatible LLM API: capture full request structure.

Records every request to captured.jsonl and prints a readable summary of the
messages/tools structure. Responds with a canned completion (SSE or JSON).
"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAPTURE = os.environ.get("MOCK_CAPTURE", "captured.jsonl")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899


def summarize(req):
    out = []
    msgs = req.get("messages", [])
    out.append(f"stream={req.get('stream')} messages={len(msgs)} tools={len(req.get('tools', []))}")
    out.append(f"model={req.get('model')} max_tokens={req.get('max_tokens')} reasoning_effort={req.get('reasoning_effort')}")
    for i, m in enumerate(msgs):
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, list):
            parts = []
            for c in content:
                t = c.get("type")
                if t == "text":
                    parts.append(f"text:{c.get('text','')[:120]}")
                elif t == "tool_use" or t == "tool_call":
                    parts.append(f"tool_use:{json.dumps(c.get('tool_call', c), ensure_ascii=False)[:150]}")
                elif t == "tool_result" or t == "tool":
                    parts.append(f"tool_result:{json.dumps(c, ensure_ascii=False)[:150]}")
                else:
                    parts.append(f"{t}:{json.dumps(c, ensure_ascii=False)[:120]}")
            content = " | ".join(parts)
        out.append(f"[{i}] {role}: {str(content)[:300]}")
    return "\n".join(out)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        req = None
        try:
            req = json.loads(body)
        except Exception:
            pass
        with open(CAPTURE, "a") as f:
            f.write(json.dumps({"path": self.path, "ts": time.time(), "request": req}, ensure_ascii=False) + "\n")
        print(f"\n[mock] POST {self.path}  ({time.strftime('%H:%M:%S')})")
        if req:
            print(summarize(req), flush=True)
        stream = bool(req and req.get("stream"))
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            now = int(time.time())
            for delta, fr in [({"role": "assistant", "content": ""}, None),
                              ({"content": "Mock response: task is complete."}, None),
                              ({}, "stop")]:
                data = {"id": "mock-1", "object": "chat.completion.chunk", "created": now,
                        "model": "deepseek-v4-pro",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": fr}]}
                self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            payload = {"id": "mock-1", "object": "chat.completion", "created": int(time.time()),
                       "model": "deepseek-v4-pro",
                       "choices": [{"index": 0, "message": {"role": "assistant", "content": "Mock response: task is complete."}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            out = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)


print(f"[mock-llm] listening on 127.0.0.1:{PORT}, capturing to {CAPTURE}", flush=True)
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
