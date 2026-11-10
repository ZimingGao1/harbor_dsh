"""No paid calls: real HTTP sockets, fixed-upstream factory replaced only in tests."""
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
from dsh_tb_agent.agent import _resolve_dsh_connection
from dsh_tb_agent.model_proxy import API_PATH, MODEL, Policy, ProxyServer

KEY, TOKEN = "dummy-upstream-secret", "dummy-client-token"
SSE = b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'


@contextmanager
def bridge(status=200, response=SSE, limit=5):
    captures, audits = [], []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            captures.append((self.path, json.loads(raw), self.headers.get("Authorization")))
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Location", "http://127.0.0.1:1/forbidden")
            self.send_header("X-Unsafe-Header", KEY)
            self.end_headers()
            self.wfile.write(response)
            self.wfile.flush()

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    proxy = ProxyServer(("127.0.0.1", 0), Policy(KEY, TOKEN, max_requests=limit),
                        connection_factory=lambda: HTTPConnection("127.0.0.1", upstream.server_port, timeout=2),
                        audit=audits.append)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (upstream, proxy)]
    for t in threads:
        t.start()
    try:
        yield proxy, captures, audits
    finally:
        for server in (proxy, upstream):
            server.shutdown()
            server.server_close()
        for t in threads:
            t.join(timeout=3)


def body(**kwargs):
    return {"model": MODEL, "messages": [{"role": "user", "content": "local mock"}],
            "stream": True, "thinking": {"type": "enabled"}, "reasoning_effort": "max",
            "temperature": 1.0, "max_tokens": 32, **kwargs}


def request(proxy, data=None, path=API_PATH, token=TOKEN, method="POST"):
    conn = HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
    conn.request(method, path, json.dumps(body() if data is None else data),
                 {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    resp = conn.getresponse()
    result = resp.status, dict(resp.getheaders()), resp.read()
    conn.close()
    return result


class ProxyTests(unittest.TestCase):
    def test_parameters_auth_stream_and_safe_logs(self):
        with bridge() as (proxy, captures, audits):
            original = body(tools=[{"type": "function", "function": {"name": "bash", "parameters": {}}}])
            status, headers, content = request(proxy, original)
            self.assertEqual(status, 200)
            self.assertEqual(content, SSE)
            self.assertNotIn("X-Unsafe-Header", headers)
            self.assertNotIn("Location", headers)
            self.assertEqual(captures, [(API_PATH, {**original, "top_p": 0.95}, f"Bearer {KEY}")])
            self.assertNotIn(KEY, content.decode())
            self.assertNotIn(KEY, json.dumps(audits))
            self.assertNotIn(TOKEN, json.dumps(audits))

    def test_rejects_auth_methods_paths_models_and_extra_fields(self):
        with bridge() as (proxy, captures, audits):
            self.assertEqual(request(proxy, token="wrong")[0], 401)
            for path in ("http://169.254.169.254/v2/chat/completions", "/v2/chat/completions?url=evil", "/v2/models"):
                self.assertEqual(request(proxy, path=path)[0], 404)
            for data in (body(model="other"), body(url="http://evil"), body(top_p=0.8),
                         body(max_tokens=999999), body(thinking={"type": "disabled"}),
                         body(reasoning_effort="off"), body(temperature=0), body(stream=False)):
                self.assertEqual(request(proxy, data)[0], 400)
            self.assertEqual(request(proxy, method="CONNECT")[0], 501)
            self.assertEqual(captures, [])

    def test_error_and_redirect_do_not_reflect_secret_or_retry(self):
        for status in (302, 401, 429, 503):
            with self.subTest(status=status), bridge(status, KEY.encode()) as (proxy, captures, audits):
                actual, headers, content = request(proxy)
                self.assertEqual(actual, 502 if status == 302 else status)
                self.assertNotIn(KEY.encode(), content)
                self.assertNotIn("X-Unsafe-Header", headers)
                self.assertEqual(len(captures), 1)

    def test_truncated_stream_is_not_completed_by_proxy(self):
        partial = b'data: {"choices": []}\n\n'
        with bridge(response=partial) as (proxy, captures, audits):
            self.assertEqual(request(proxy)[2], partial)
            self.assertNotIn(b"[DONE]", partial)

    def test_budget_and_expiry(self):
        with bridge(limit=1) as (proxy, captures, audits):
            self.assertEqual(request(proxy)[0], 200)
            self.assertEqual(request(proxy)[0], 429)
            self.assertEqual(len(captures), 1)
        with bridge() as (proxy, captures, audits):
            proxy.deadline = time.monotonic() - 1
            self.assertEqual(request(proxy)[0], 403)
            self.assertEqual(captures, [])

    def test_adapter_proxy_mode_ignores_real_credentials_and_fails_closed(self):
        with patch.dict(os.environ, {"DSH_PROXY_URL": "http://dsh-model-proxy:8080/v2", "DSH_PROXY_TOKEN": TOKEN,
                                     "QIANFAN_API_KEY": KEY, "QIANFAN_BASE_URL": "https://wrong", "DEEPSEEK_API_KEY": KEY}, clear=True):
            self.assertEqual(_resolve_dsh_connection(None), (TOKEN, "http://dsh-model-proxy:8080/v2"))
            del os.environ["DSH_PROXY_TOKEN"]
            with self.assertRaises(ValueError):
                _resolve_dsh_connection(None)


if __name__ == "__main__":
    unittest.main()
