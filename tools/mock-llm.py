#!/usr/bin/env python3
"""Mock DeepSeek-compatible LLM API: capture full request structure.

Records every request to captured.jsonl and prints a readable summary of the
messages/tools structure. Responds with a canned completion (SSE or JSON).
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAPTURE = os.environ.get("MOCK_CAPTURE", "captured.jsonl")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
EXPECTED_PATH = os.environ.get("MOCK_EXPECT_PATH")
EXPECTED_MODEL = os.environ.get("MOCK_EXPECT_MODEL")
EXPECTED_REASONING = os.environ.get("MOCK_EXPECT_REASONING_EFFORT")
EXPECTED_THINKING = os.environ.get("MOCK_EXPECT_THINKING_TYPE")
EXPECTED_TEMPERATURE = os.environ.get("MOCK_EXPECT_TEMPERATURE")
REQUIRE_TOP_P = os.environ.get("MOCK_REQUIRE_TOP_P", "0") == "1"


def summarize(req):
    out = []
    msgs = req.get("messages", [])
    out.append(f"stream={req.get('stream')} messages={len(msgs)} tools={len(req.get('tools', []))}")
    thinking = req.get("thinking") or {}
    out.append(
        "model={} max_tokens={} reasoning_effort={} thinking.type={} "
        "temperature={} top_p={}".format(
            req.get("model"),
            req.get("max_tokens"),
            req.get("reasoning_effort"),
            thinking.get("type") if isinstance(thinking, dict) else None,
            req.get("temperature"),
            req.get("top_p"),
        )
    )
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
        capture = {
            "path": self.path,
            "ts": time.time(),
            "headers": {
                k.lower(): v
                for k, v in self.headers.items()
                if k.lower() != "authorization"
            },
            "request": req,
        }
        with open(CAPTURE, "a", encoding="utf-8") as f:
            f.write(json.dumps(capture, ensure_ascii=False) + "\n")
        print(f"\n[mock] POST {self.path}  ({time.strftime('%H:%M:%S')})")
        if req:
            print(summarize(req), flush=True)
        errors = []
        if req is None:
            errors.append("request body is not valid JSON")
        else:
            if EXPECTED_PATH and self.path != EXPECTED_PATH:
                errors.append(f"path={self.path!r}, expected {EXPECTED_PATH!r}")
            if EXPECTED_MODEL and req.get("model") != EXPECTED_MODEL:
                errors.append(f"model={req.get('model')!r}, expected {EXPECTED_MODEL!r}")
            thinking = req.get("thinking")
            if EXPECTED_THINKING and (
                not isinstance(thinking, dict) or thinking.get("type") != EXPECTED_THINKING
            ):
                errors.append(f"thinking.type is not {EXPECTED_THINKING!r}")
            if EXPECTED_REASONING and req.get("reasoning_effort") != EXPECTED_REASONING:
                errors.append(
                    f"reasoning_effort={req.get('reasoning_effort')!r}, "
                    f"expected {EXPECTED_REASONING!r}"
                )
            if EXPECTED_TEMPERATURE and req.get("temperature") != float(EXPECTED_TEMPERATURE):
                errors.append(
                    f"temperature={req.get('temperature')!r}, expected {EXPECTED_TEMPERATURE}"
                )
            if REQUIRE_TOP_P and "top_p" not in req:
                errors.append("top_p is missing (the official SDK 0.1.0rc6 does not support it)")
        if errors:
            print("[mock] VALIDATION FAILED: " + "; ".join(errors), file=sys.stderr, flush=True)
            self.send_error(400, "mock validation failed")
            return
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
                        "model": req.get("model", "mock-model"),
                        "choices": [{"index": 0, "delta": delta, "finish_reason": fr}]}
                self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            payload = {"id": "mock-1", "object": "chat.completion", "created": int(time.time()),
                       "model": req.get("model", "mock-model"),
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
