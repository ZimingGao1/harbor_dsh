"""Fixed-upstream Qianfan bridge. No SDK patches, HTTP CONNECT, or URL forwarding.

Production entrypoint reads credentials from files, never environment values.
The task receives only a short-lived, per-trial proxy token. Logs exclude bodies,
headers and upstream error text. Local tests inject an HTTP connection factory.
"""
from dataclasses import dataclass, field
import hmac
from http.client import HTTPSConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

MODEL = "deepseek-v4.1-flash-1017"
API_PATH = "/v2/chat/completions"
MAX_BODY = 16 * 1024 * 1024
ALLOWED_FIELDS = {"model", "messages", "tools", "stream", "stream_options",
                  "thinking", "reasoning_effort", "temperature", "top_p", "max_tokens", "stop"}


@dataclass
class Policy:
    api_key: str = field(repr=False)
    token: str = field(repr=False)
    max_requests: int = 1600
    lifetime_seconds: int = 7200
    max_output_tokens: int = 49152
    upstream_timeout: float = 120

    def __post_init__(self):
        for value in (self.api_key, self.token):
            if not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
                raise ValueError("Credential files must contain raw, printable ASCII tokens")
        if self.api_key.lower().startswith("bearer ") or self.api_key == self.token:
            raise ValueError("Use a raw upstream key and a different proxy token")
        if min(self.max_requests, self.lifetime_seconds, self.max_output_tokens, self.upstream_timeout) <= 0:
            raise ValueError("Proxy limits must be positive")


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, policy, connection_factory=None, audit=None):
        self.policy = policy
        # Production uses only this constant host/path and the default TLS trust.
        # No HTTP_PROXY/HTTPS_PROXY support and no redirect handling.
        self.connection_factory = connection_factory or (
            lambda: HTTPSConnection("qianfan.baidubce.com", timeout=policy.upstream_timeout)
        )
        self.audit = audit or (lambda event: print(json.dumps(event), flush=True))
        self.deadline = time.monotonic() + policy.lifetime_seconds
        self.request_count = 0
        self.counter_lock = threading.Lock()
        self.workers = threading.BoundedSemaphore(4)
        super().__init__(address, ProxyHandler)

    def process_request(self, request, client_address):
        if not self.workers.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Busy\r\nContent-Length: 0\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.workers.release()

    def handle_error(self, request, client_address):
        # No traceback/body/header logging across the credential boundary.
        self.audit({"event": "proxy_handler_error"})


class ProxyHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.reply(code, "request rejected")

    def reply(self, status, message):
        body = json.dumps({"error": {"message": message, "type": "restricted_proxy"}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self):
        if self.path != API_PATH:
            self.reply(404, "path not allowed")
            return
        auth = self.headers.get_all("Authorization", [])
        expected = f"Bearer {self.server.policy.token}"
        if len(auth) != 1 or not hmac.compare_digest(auth[0].encode(), expected.encode()):
            self.reply(401, "proxy authentication required")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
            self.reply(400, "one Content-Length required")
            return
        try:
            length = int(lengths[0])
            if length <= 0 or length > MAX_BODY:
                self.reply(413, "request body too large")
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError
            body = json.loads(raw)
            self.validate(body)
        except (ValueError, TypeError, UnicodeError, TimeoutError):
            self.reply(400, "invalid model request or evaluation parameters")
            return
        with self.server.counter_lock:
            if time.monotonic() >= self.server.deadline:
                self.reply(403, "proxy token expired")
                return
            if self.server.request_count >= self.server.policy.max_requests:
                self.reply(429, "proxy request budget exhausted")
                return
            self.server.request_count += 1
            index = self.server.request_count
        # Only additive parameter compatibility; messages and tools are preserved.
        body["top_p"] = 0.95
        connection = self.server.connection_factory()
        started = time.monotonic()
        status = 502
        sent_headers = False
        try:
            connection.request("POST", API_PATH, body=json.dumps(body).encode(), headers={
                "Authorization": f"Bearer {self.server.policy.api_key}",
                "Content-Type": "application/json", "Accept": "text/event-stream",
            })
            upstream = connection.getresponse()
            status = upstream.status
            # Do not forward redirects, HTML gateways, response headers or errors
            # that might reflect Authorization. Preserve the error HTTP status.
            if status != 200:
                self.reply(status if 400 <= status <= 599 else 502, "upstream request rejected")
                return
            if upstream.getheader("Content-Type", "").split(";")[0].strip() != "text/event-stream":
                status = 502
                self.reply(status, "upstream did not return SSE")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            sent_headers = True
            self.close_connection = True
            while chunk := upstream.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, ValueError, HTTPException):
            status = 502
            if not sent_headers:
                self.reply(status, "upstream transport failed")
            # On a partial stream, close without synthesizing [DONE]. The SDK
            # must observe a stream failure; there is no proxy-level retry.
        finally:
            connection.close()
            self.server.audit({"event": "model_request", "request": index,
                               "status": status, "model": MODEL, "top_p": 0.95,
                               "elapsed_ms": round((time.monotonic() - started) * 1000)})

    def validate(self, body):
        if not isinstance(body, dict) or set(body) - ALLOWED_FIELDS:
            raise ValueError
        if (body.get("model") != MODEL or body.get("stream") is not True
                or body.get("thinking") != {"type": "enabled"}
                or body.get("reasoning_effort") != "max"
                or type(body.get("temperature")) not in (int, float)
                or body["temperature"] != 1.0):
            raise ValueError
        if "top_p" in body and (type(body["top_p"]) not in (int, float) or body["top_p"] != 0.95):
            raise ValueError
        if (type(body.get("max_tokens")) is not int
                or not 1 <= body["max_tokens"] <= self.server.policy.max_output_tokens):
            raise ValueError
        if not isinstance(body.get("messages"), list) or not body["messages"]:
            raise ValueError


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--max-requests", type=int, default=1600)
    parser.add_argument("--lifetime-seconds", type=int, default=7200)
    parser.add_argument("--max-output-tokens", type=int, default=49152)
    args = parser.parse_args()
    try:
        policy = Policy(args.key_file.read_text().strip(), args.token_file.read_text().strip(),
                        args.max_requests, args.lifetime_seconds, args.max_output_tokens)
    except (OSError, ValueError):
        parser.error("Cannot load proxy credentials or limits; check secret files")
    with ProxyServer((args.listen, args.port), policy) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
