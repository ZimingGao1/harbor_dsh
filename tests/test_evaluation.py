"""Offline tests: real installed SDK/runtime; only the HTTP model is simulated.

Run from the repository: .venv/bin/python -m unittest discover -s tests -v
No production credentials are inherited by the runtime subprocess.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

# Harbor imports LiteLLM for model metadata; use its bundled offline catalog.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
from dsh_tb_agent.agent import DshAgent
from harbor.models.agent.context import AgentContext
from dsh_tb_agent.model_proxy import Policy, ProxyServer
from http.client import HTTPConnection

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "src/dsh_tb_agent/run_dsh.py"
CORDIS = DRIVER.with_name("minimal.cordis.yml")
MODEL = "deepseek-v4.1-flash-1017"


class RuntimeTests(unittest.TestCase):
    def run_case(self, limit, mode="loop", fail_first=False, use_default=False, via_proxy=False):
        with tempfile.TemporaryDirectory(prefix="dsh-evaluation-") as tmp:
            root = Path(tmp)
            (root / "probe.txt").write_text("local tool evidence\n")
            (root / "instruction.txt").write_text("Follow the mock response.")
            captures = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    if via_proxy and self.headers.get("Authorization") != "Bearer dummy-upstream-server-secret":
                        self.send_error(401)
                        return
                    # Never persist headers, in particular Authorization.
                    captures.append({"path": self.path, "request": request})
                    if fail_first and len(captures) == 1:
                        self.send_response(503)
                        self.end_headers()
                        self.wfile.write(b'{"error":{"message":"mock transient failure"}}')
                        return
                    complete = mode == "text" or (mode == "bash" and len(captures) > 1)
                    if complete:
                        delta, reason = {"content": "mock completed"}, "stop"
                    else:
                        if mode == "bash":
                            # A dummy key is installed by the test; only presence
                            # is checked. Neither its value nor a real key is read.
                            name = "bash"
                            args = {"command": "if [ -n \"${DEEPSEEK_API_KEY:-}\" ]; then printf 'credential-visible\\n'; else printf 'credential-hidden\\n'; fi"}
                        else:
                            name = "str_replace_editor"
                            args = {"command": "view", "path": str(root / "probe.txt")}
                        call = {"index": 0, "id": f"call-{len(captures)}", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)}}
                        delta, reason = {"tool_calls": [call]}, "tool_calls"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for d, finish in [(delta, None), ({}, reason)]:
                        chunk = {"id": "mock", "model": MODEL, "object": "chat.completion.chunk",
                                 "choices": [{"index": 0, "delta": d, "finish_reason": finish}]}
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            proxy = proxy_thread = None
            if via_proxy:
                proxy = ProxyServer(("127.0.0.1", 0),
                                    Policy("dummy-upstream-server-secret", "dummy-client-proxy-token"),
                                    connection_factory=lambda: HTTPConnection("127.0.0.1", server.server_port, timeout=5),
                                    audit=lambda event: None)
                proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
                proxy_thread.start()
            env = {"PATH": os.defpath, "TMPDIR": str(root), "DSH_HOME": str(root / "home"),
                   "XDG_CACHE_HOME": str(root / "cache"), "DSH_MODEL": MODEL,
                   "DEEPSEEK_API_KEY": "dummy-offline-test-key",
                   "DEEPSEEK_BASE_URL": f"http://127.0.0.1:{server.server_port}/v2",
                   "DSH_SESSION_ROOT": str(root / "runtime-sessions"), "DSH_MAX_TOKENS": "512",
                   "DSH_REASONING_EFFORT": "max", "DSH_THINKING": "enabled",
                   "DSH_TEMPERATURE": "1.0", "DSH_CONTEXT_WINDOW": "1000000"}
            if not use_default:
                env["DSH_MAX_STEPS"] = str(limit)
            if via_proxy:
                env["DEEPSEEK_BASE_URL"] = f"http://127.0.0.1:{proxy.server_port}/v2"
                env["DEEPSEEK_API_KEY"] = "dummy-client-proxy-token"
                self.assertNotIn("dummy-upstream-server-secret", env.values())
            try:
                proc = subprocess.run([sys.executable, str(DRIVER), str(CORDIS),
                                       str(root / "instruction.txt"), "offline-case", str(root / "result.json")],
                                      cwd=root, env=env, capture_output=True, text=True, timeout=180)
            finally:
                if proxy:
                    proxy.shutdown()
                    proxy.server_close()
                    proxy_thread.join(timeout=5)
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            result = json.loads((root / "result.json").read_text())
            events = [json.loads(line) for line in
                      (root / "sessions/offline-case-session.jsonl").read_text().splitlines() if line]
            if directory := os.environ.get("DSH_TEST_ARTIFACT_DIR"):
                evidence = Path(directory) / self._testMethodName
                evidence.mkdir(parents=True, exist_ok=True)
                (evidence / "result.json").write_text(json.dumps(result, indent=2))
                (evidence / "session.jsonl").write_text("\n".join(json.dumps(e) for e in events))
                # Keep actual HTTP bodies, never headers or credentials.
                (evidence / "requests.jsonl").write_text("\n".join(json.dumps(c) for c in captures))
            self.assertTrue(captures, result)
            for capture in captures:
                self.assertEqual(capture["path"], "/v2/chat/completions")
                req = capture["request"]
                self.assertEqual(req["model"], MODEL)
                self.assertEqual(req["thinking"], {"type": "enabled"})
                self.assertEqual(req["reasoning_effort"], "max")
                self.assertEqual(req["temperature"], 1.0)
                # This is a recorded limitation, not a claim of top_p support.
                if via_proxy:
                    self.assertEqual(req["top_p"], 0.95)
                else:
                    self.assertNotIn("top_p", req)
                self.assertEqual({t["function"]["name"] for t in req["tools"]},
                                 {"bash", "str_replace_editor"})
            contexts = [e["data"]["contextWindow"] for e in events if e.get("type") == "request/context"]
            self.assertTrue(contexts)
            self.assertEqual(set(contexts), {1000000})
            return result, events, captures

    def test_real_sdk_text_request(self):
        result, events, captures = self.run_case(500, "text")
        self.assertEqual(result["finish_reason"], "completed")
        self.assertEqual(result["steps_started"], 1)
        self.assertFalse(result["step_limit_reached"])
        self.assertEqual(len(captures), 1)

    def test_real_sdk_through_restricted_proxy(self):
        result, events, captures = self.run_case(500, "text", via_proxy=True)
        self.assertEqual(result["finish_reason"], "completed")
        self.assertEqual(len(captures), 1)

    def test_default_500_steps_blocks_501(self):
        result, events, captures = self.run_case(500, use_default=True)
        self.assertEqual(result["finish_reason"], "blocked")
        self.assertTrue(result["step_limit_reached"])
        self.assertEqual(result["steps_started"], 500)
        self.assertEqual(len(captures), 500)
        for kind in ("step/start", "step/end", "tool/result"):
            selected = [e["data"]["step"] for e in events if e.get("type") == kind]
            self.assertEqual(selected, list(range(1, 501)), kind)
        self.assertIn("local tool evidence", json.dumps(captures[-1]["request"]["messages"]))

    def test_retry_is_not_an_extra_step(self):
        result, events, captures = self.run_case(2, fail_first=True)
        self.assertEqual(result["finish_reason"], "blocked")
        self.assertEqual(result["steps_started"], 2)
        self.assertEqual(len(captures), 3)
        self.assertTrue(any(e.get("type") == "llm/retry" for e in events))

    def test_bash_environment_does_not_inherit_dummy_key(self):
        result, events, captures = self.run_case(3, "bash")
        self.assertEqual(result["finish_reason"], "completed")
        outputs = [e for e in events if e.get("type") == "tool/result"]
        self.assertEqual(len(outputs), 1)
        blocks = outputs[0]["data"]["message"]["content"][0]
        self.assertFalse(blocks.get("isError", False))
        self.assertEqual(blocks["content"][0]["text"].strip(), "credential-hidden")


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_mode_offline_install_and_launch(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "DSH_PROXY_URL": "http://dsh-model-proxy:8080/v2", "DSH_PROXY_TOKEN": "dummy-client-token",
            "DSH_OFFLINE_INSTALL": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "QIANFAN_API_KEY": "dummy-upstream-secret",
        }, clear=True):
            class FakeEnvironment:
                default_user = None
                commands = []
                uploads = []

                async def upload_file(self, source, target):
                    self.uploads.append(str(target))

                async def exec(self, **kwargs):
                    self.commands.append(kwargs)
                    return SimpleNamespace(return_code=0, stdout="", stderr="")

            agent = DshAgent(logs_dir=Path(tmp), model_name=f"deepseek/{MODEL}")
            env = FakeEnvironment()
            await agent.install(env)
            self.assertEqual(len(env.commands), 1)
            self.assertIn("importlib.metadata", env.commands[0]["command"])
            self.assertNotIn("pip install", env.commands[0]["command"])
            await agent.run("offline task", env, AgentContext())
            launch = env.commands[-1]["env"]
            self.assertEqual(launch["DEEPSEEK_API_KEY"], "dummy-client-token")
            self.assertEqual(launch["DEEPSEEK_BASE_URL"], "http://dsh-model-proxy:8080/v2")
            self.assertNotIn("dummy-upstream-secret", launch.values())
            self.assertEqual(len(env.uploads), 4)

    async def test_invalid_limits_fail_before_sdk_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "instruction.txt").write_text("offline")
            for value in ("0", "-1", "nan", "9007199254740992"):
                proc = subprocess.run([
                    sys.executable, str(DRIVER), str(CORDIS), str(root / "instruction.txt"),
                    "invalid", str(root / "result.json"),
                ], env={"DSH_MODEL": MODEL, "DSH_MAX_STEPS": value},
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(proc.returncode, 1)
                self.assertFalse(json.loads((root / "result.json").read_text())["ok"])

    async def test_harbor_install_run_and_post_run(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "QIANFAN_API_KEY": "dummy-adapter-key", "QIANFAN_BASE_URL": "http://127.0.0.1:9/v2",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }, clear=True):
            root = Path(tmp)

            class FakeEnvironment:
                default_user = None
                uploads = {}
                executions = []

                async def upload_file(self, source, target):
                    self.uploads[str(target)] = Path(source).read_bytes()

                async def exec(self, **kwargs):
                    self.executions.append(kwargs)
                    return SimpleNamespace(return_code=0, stdout="", stderr="")

            agent = DshAgent(logs_dir=root, model_name=f"deepseek/{MODEL}")
            env = FakeEnvironment()
            await agent.install(env)
            for filename in ("run_dsh.py", "minimal.cordis.yml", "effort-plugin.mjs"):
                self.assertIn(f"/installed-agent/{filename}", env.uploads)
            context = AgentContext()
            await agent.run("Task instruction: preserve exactly.\n", env, context)
            self.assertEqual(env.uploads["/installed-agent/instruction.txt"],
                             b"Task instruction: preserve exactly.\n")
            launch = env.executions[-1]["env"]
            self.assertEqual(launch["DSH_MODEL"], MODEL)
            self.assertEqual(launch["DSH_MAX_STEPS"], "500")
            self.assertEqual(launch["DSH_CONTEXT_WINDOW"], "1000000")
            self.assertEqual(launch["DEEPSEEK_BASE_URL"], "http://127.0.0.1:9/v2")
            self.assertEqual(launch["DEEPSEEK_API_KEY"], "dummy-adapter-key")
            (root / "dsh-result.json").write_text(json.dumps({
                "ok": True, "finish_reason": "blocked", "steps_started": 500, "step_limit_reached": True,
            }))
            agent.populate_context_post_run(context)
            self.assertTrue(context.metadata["dsh"]["step_limit_reached"])


if __name__ == "__main__":
    unittest.main()
