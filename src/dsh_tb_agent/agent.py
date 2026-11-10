"""Harbor installed-agent adapter that runs DeepSeek Harness (dsh) in the task container.

The adapter class runs in the Harbor control plane. ``install`` pip-installs
the DeepSeek Harness Python SDK (bundled single-file runtime, no Node needed)
into the task container and uploads the container-side driver plus the dsh
agent composition. ``run`` launches the driver through ``environment.exec``,
which starts the bundled dsh JSON-RPC runtime with a fresh session id and the
task instruction; dsh's own persistent bash and string-replace editor tools
then operate directly on the task container filesystem.
"""

import json
import os
import shlex
import tempfile
import uuid
from urllib.parse import urlsplit
from pathlib import Path, PurePosixPath
from typing import override

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.agents.agent_auth_resolution import ModelConnectionSpec
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories.agent import Agent as TrajAgent
from harbor.models.trajectories.final_metrics import FinalMetrics
from harbor.models.trajectories.metrics import Metrics
from harbor.models.trajectories.observation_result import ObservationResult
from harbor.models.trajectories.step import Observation, Step, ToolCall
from harbor.models.trajectories.trajectory import Trajectory
from harbor.utils.trajectory_utils import format_trajectory_json

_INSTALL_DIR = PurePosixPath("/installed-agent")
# str() everywhere: some harbor environments (e.g. daytona) pass the target
# path straight to their SDK, which requires plain str (aiohttp rejects
# Path-like filename with "filename must be an instance of str").
_DRIVER_PATH = str(_INSTALL_DIR / "run_dsh.py")
_CORDIS_PATH = str(_INSTALL_DIR / "minimal.cordis.yml")
_INSTRUCTION_PATH = str(_INSTALL_DIR / "instruction.txt")
_RESULT_REMOTE_PATH = PurePosixPath("/logs/agent/dsh-result.json")
_RESULT_LOCAL_NAME = "dsh-result.json"
_DEFAULT_QIANFAN_BASE_URL = "https://qianfan.baidubce.com/v2"
_DEFAULT_QIANFAN_MODEL = "deepseek-v4.1-flash-1017"


def _resolve_dsh_connection(access) -> tuple[str, str]:
    """Resolve the endpoint and key forwarded to the DSH child process.

    Harbor 0.25's connection object may contain an inferred DeepSeek default;
    that inference must not override the explicit Qianfan route.
    """
    proxy_url = os.environ.get("DSH_PROXY_URL", "")
    proxy_token = os.environ.get("DSH_PROXY_TOKEN", "")
    if proxy_url or proxy_token:
        endpoint = urlsplit(proxy_url)
        if (not proxy_token or endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment or endpoint.path.rstrip("/") != "/v2"):
            raise ValueError("DSH_PROXY_URL (/v2) and DSH_PROXY_TOKEN must both be configured")
        # Proxy mode is fail-closed: never select the real provider key or URL.
        return proxy_token, proxy_url.rstrip("/")
    access_api_key = getattr(access, "api_key", None)
    access_user_base_url = getattr(access, "user_supplied_base_url", None)
    access_base_url = getattr(access, "resolved_base_url", None) or access_user_base_url
    qianfan_api_key = os.environ.get("QIANFAN_API_KEY", "")
    qianfan_base_url = os.environ.get("QIANFAN_BASE_URL", "")
    deepseek_base_url = os.environ.get("DEEPSEEK_BASE_URL", "")
    if access_user_base_url:
        # Explicit Harbor --base-url remains highest priority.
        api_key = access_api_key or qianfan_api_key
        base_url = access_user_base_url
    elif bool(qianfan_api_key or qianfan_base_url) or not (
        access_base_url or deepseek_base_url
    ):
        key_source = getattr(access, "api_key_source_name", None)
        api_key = qianfan_api_key or (
            access_api_key
            if access_api_key and key_source != "DEEPSEEK_API_KEY"
            else ""
        )
        base_url = qianfan_base_url or _DEFAULT_QIANFAN_BASE_URL
    else:
        api_key = access_api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        base_url = access_base_url or deepseek_base_url
    return api_key, base_url


class DshAgent(BaseInstalledAgent):
    """Run DeepSeek Harness as an installed agent inside the task container."""

    MODEL_CONNECTION = ModelConnectionSpec(default_provider="deepseek")

    @staticmethod
    def name() -> str:
        return "dsh"

    def version(self) -> str | None:
        return None

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        pkg_dir = Path(__file__).resolve().parent

        # Mirror selection: DSH_PIP_INDEX_URL and DSH_GETPIP_URL make the
        # bootstrap chain environment-driven so the same adapter works both
        # behind the GFW (TUNA/Aliyun mirrors, default) and on overseas
        # sandboxes (unset -> official pypi.org / bootstrap.pypa.io).
        index_url = os.environ.get("DSH_PIP_INDEX_URL", "")
        index_arg = f" -i {shlex.quote(index_url)}" if index_url else ""
        getpip_url = os.environ.get(
            "DSH_GETPIP_URL",
            "https://mirrors.aliyun.com/pypi/get-pip.py",
        )

        # Ensure python3 and pip exist, within the 360s harbor setup budget.
        # Order: existing python/pip → ensurepip (offline) → get-pip.py (hard
        # timeouts; slow proxies must fail fast) → apt last resort. Images
        # that ship pip finish in seconds; images without pip otherwise burn
        # the whole budget on slow get-pip/apt fetches.
        await self.exec_as_root(
            environment,
            # Offline images are prepared before evaluation; do not access
            # package indexes inside the isolated task network.
            "python3 -c \"from importlib.metadata import version; "
            "assert version('deepseek-harness-sdk') == '0.1.0rc6'; "
            "assert version('deepseek-harness-runtime-bin') == '0.1.0rc6'\""
            if os.environ.get("DSH_OFFLINE_INSTALL") == "1" else (
            "set -e; "
            "python3 --version >/dev/null 2>&1 && "
            "python3 -m pip --version >/dev/null 2>&1 || "
            "(python3 --version >/dev/null 2>&1 && "
            "python3 -m ensurepip --upgrade >/dev/null 2>&1 && "
            "python3 -m pip --version >/dev/null 2>&1) || "
            "(python3 --version >/dev/null 2>&1 && "
            f"timeout 45 python3 -c \"import socket,urllib.request;"
            f"socket.setdefaulttimeout(40);"
            f"urllib.request.urlretrieve("
            f"'{getpip_url}','/tmp/get-pip.py')\" "
            f"&& timeout 120 python3 /tmp/get-pip.py -q "
            f"{index_arg} --break-system-packages "
            "> /dev/null 2>&1 && python3 -m pip --version >/dev/null 2>&1) || "
            "(timeout 90 apt-get update -qq && "
            "DEBIAN_FRONTEND=noninteractive timeout 300 apt-get install "
            "-y -qq --no-install-recommends "
            "python3 python3-pip)"),
            timeout_sec=600,
        )

        await environment.upload_file(pkg_dir / "run_dsh.py", _DRIVER_PATH)
        await environment.upload_file(pkg_dir / "minimal.cordis.yml", _CORDIS_PATH)
        await environment.upload_file(
            pkg_dir / "effort-plugin.mjs",
            str(_INSTALL_DIR / "effort-plugin.mjs"),
        )
        if environment.default_user is not None:
            await self.exec_as_root(
                environment,
                f"chown -R {shlex.quote(str(environment.default_user))} "
                f"{shlex.quote(str(_INSTALL_DIR))}",
            )

        if os.environ.get("DSH_OFFLINE_INSTALL") == "1":
            return

        # Prefer the PEP 668-aware install (Ubuntu 24.04+ / Debian 13);
        # --ignore-installed avoids pip failing on Debian system packages
        # without a RECORD file (e.g. typing_extensions). Older images fall
        # back to the plain variant when the flag is unsupported.
        # DSH_PIP_INDEX_URL points pip at a faster mirror when PyPI is slow.
        index_arg = ""
        index_url = os.environ.get("DSH_PIP_INDEX_URL", "")
        if index_url:
            index_arg = f" -i {shlex.quote(index_url)}"
        try:
            await self.exec_as_root(
                environment,
                "python3 -m pip install --break-system-packages --ignore-installed "
                f"--quiet deepseek-harness-sdk==0.1.0rc6{index_arg}",
                timeout_sec=600,
            )
        except Exception:
            await self.exec_as_root(
                environment,
                f"python3 -m pip install --quiet deepseek-harness-sdk==0.1.0rc6{index_arg}",
                timeout_sec=600,
            )

    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        model = (self.model_name or "").split("/", maxsplit=1)[-1]
        if not model:
            model = os.environ.get("DSH_MODEL", _DEFAULT_QIANFAN_MODEL)
        if not model:
            raise ValueError(
                "Model name is required: pass -m <provider>/<model> or set DSH_MODEL"
            )

        access = self.model_connection
        # Only pass non-empty values: the dsh adapter treats an empty-but-set
        # DEEPSEEK_BASE_URL as an explicit (broken) endpoint, while an unset
        # variable falls back to the provider default.
        env = {
            "DSH_MODEL": model,
            "DSH_SESSION_ROOT": "/tmp/dsh-sessions",
            "DSH_MAX_TOKENS": os.environ.get("DSH_MAX_TOKENS", "49152"),
            "DSH_MAX_STEPS": os.environ.get("DSH_MAX_STEPS", "500"),
            "DSH_CONTEXT_WINDOW": os.environ.get("DSH_CONTEXT_WINDOW", "1000000"),
            "DSH_THINKING": os.environ.get("DSH_THINKING", "enabled"),
            "DSH_TEMPERATURE": os.environ.get("DSH_TEMPERATURE", "1.0"),
            "DSH_SYSTEM_PROMPT": os.environ.get(
                "DSH_SYSTEM_PROMPT",
                "You are a helpful software engineer assistant.",
            ),
        }
        # Harbor 0.25 exposes ResolvedModelConnection.api_key plus
        # resolved_base_url/user_supplied_base_url. It does not expose the
        # older configured_base_url attribute. Qianfan variables select the
        # Qianfan route; Harbor's inferred api.deepseek.com URL is ignored.
        api_key, base_url = _resolve_dsh_connection(access)
        if api_key:
            env["DEEPSEEK_API_KEY"] = api_key
        if base_url:
            env["DEEPSEEK_BASE_URL"] = base_url
        pip_index = os.environ.get("DSH_PIP_INDEX_URL", "")
        if pip_index:
            env["PIP_INDEX_URL"] = pip_index
        env["DSH_REASONING_EFFORT"] = os.environ.get(
            "DSH_REASONING_EFFORT", "max"
        )

        session_id = f"dsh-{uuid.uuid4().hex[:12]}"

        with tempfile.TemporaryDirectory(prefix="dsh-agent-") as tmp:
            local_instruction = Path(tmp) / "instruction.txt"
            local_instruction.write_text(instruction, encoding="utf-8")
            await environment.upload_file(local_instruction, _INSTRUCTION_PATH)

        command = (
            f"python3 {shlex.quote(str(_DRIVER_PATH))} "
            f"{shlex.quote(str(_CORDIS_PATH))} "
            f"{shlex.quote(str(_INSTRUCTION_PATH))} "
            f"{shlex.quote(session_id)} "
            f"{shlex.quote(str(_RESULT_REMOTE_PATH))}"
        )
        try:
            await self.exec_as_agent(environment, command, env=env)
        finally:
            # Also runs when harbor cancels this coroutine on agent timeout:
            # build an ATIF trajectory from whatever the live sync loop
            # already copied, so a killed run still leaves a visible trace.
            self._export_trajectory_from_sessions()

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        result_file = self.logs_dir / _RESULT_LOCAL_NAME
        data = None
        if result_file.is_file():
            try:
                data = json.loads(result_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                self.logger.warning(
                    "Unparseable dsh result file %s: %s", result_file, exc
                )
        else:
            # Agent timeout or crash: the driver never wrote its result file,
            # but the live session sync should have left a JSONL behind.
            self._export_trajectory_from_sessions()
        if data is not None:
            context.metadata = {"dsh": data}
            if data.get("ok"):
                self._export_trajectory_from_result(data)
        # Token usage for the hub (agent_result.n_*_tokens). Harbor backfills
        # these from AgentContext after run(); without them the hub shows
        # "no token data". Semantics match the ATIF metrics (and computer_1):
        # prompt = miss + cache-hit per request, cache tracked separately.
        self._fill_token_usage(context)

    def _fill_token_usage(self, context: AgentContext) -> None:
        sessions_dir = self.logs_dir / "sessions"
        if not sessions_dir.is_dir():
            return
        jsonls = sorted(
            sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime
        )
        if not jsonls:
            return
        n_input = n_output = n_cache = 0
        for line in jsonls[-1].read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "assistant/chunk":
                continue
            usage = ev.get("data", {}).get("chunk", {}).get("usage")
            if isinstance(usage, dict):
                n_input += usage.get("inputTokens", 0) or 0
                n_output += usage.get("outputTokens", 0) or 0
                n_cache += usage.get("cacheReadTokens", 0) or 0
        if n_input or n_output:
            context.n_input_tokens = n_input + n_cache
            context.n_cache_tokens = n_cache
            context.n_output_tokens = n_output

    def _export_trajectory_from_result(self, data: dict) -> None:
        """Export ATIF trajectory from a completed run's result file."""
        session_id = data.get("session_id")
        model = (self.model_name or "").split("/", maxsplit=1)[-1] or None
        if not (session_id and model):
            return
        jsonl = self.logs_dir / "sessions" / f"{session_id}-session.jsonl"
        if not jsonl.is_file():
            self.logger.warning("No dsh session jsonl at %s", jsonl)
            return
        self._write_trajectory(jsonl, session_id, model)

    def _export_trajectory_from_sessions(self) -> None:
        """Export an ATIF trajectory from any synced session JSONL (works for
        timed-out/crashed runs where the result file is missing)."""
        model = (self.model_name or "").split("/", maxsplit=1)[-1] or None
        sessions_dir = self.logs_dir / "sessions"
        if not sessions_dir.is_dir() or not model:
            return
        jsonls = sorted(
            sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime
        )
        if not jsonls:
            return
        latest = jsonls[-1]
        session_id = latest.name.removesuffix("-session.jsonl")
        self._write_trajectory(latest, session_id, model)

    def _write_trajectory(
        self, session_jsonl: Path, session_id: str, model: str
    ) -> None:
        trajectory = self._build_trajectory(session_jsonl, session_id, model)
        if trajectory is None:
            self.logger.warning(
                "Failed to build ATIF trajectory from %s", session_jsonl
            )
            return
        out = self.logs_dir / "trajectory.json"
        out.write_text(
            format_trajectory_json(trajectory.to_json_dict()),
            encoding="utf-8",
        )
        self.logger.info("Wrote ATIF trajectory %s", out)

    def _build_trajectory(
        self, session_jsonl: Path, session_id: str, model: str
    ) -> Trajectory | None:
        """Convert a dsh session JSONL into an ATIF trajectory (harbor format)."""
        try:
            events = [
                json.loads(line)
                for line in session_jsonl.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError) as exc:
            self.logger.warning("Cannot read session jsonl %s: %s", session_jsonl, exc)
            return None

        # Tool results by call id, from tool/result events.
        tool_results: dict[str, str] = {}
        for ev in events:
            if ev.get("type") != "tool/result":
                continue
            for block in ev.get("data", {}).get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool-result":
                    call_id = block.get("toolCallId")
                    text = _extract_block_text(block.get("content", []), "text")
                    if call_id and call_id not in tool_results:
                        tool_results[call_id] = text

        header = next(
            (ev for ev in events if ev.get("type") == "request/header"), None
        )
        effort = None
        if header is not None:
            effort = (
                header.get("data", {})
                .get("header", {})
                .get("config", {})
                .get("reasoningEffort")
            )

        # Per-(turn, step) usage. DeepSeek streaming usage reports:
        #   inputTokens      = this request's cache-miss tokens (new content)
        #   cacheReadTokens  = this request's cache-hit tokens (history prefix,
        #                      grows with the conversation — NOT a session
        #                      cumulative value)
        #   outputTokens     = completion tokens (includes reasoning tokens)
        # So a step's total prompt = inputTokens + cacheReadTokens.
        step_usage: dict[tuple[int, int], dict] = {}
        for ev in events:
            if ev.get("type") != "assistant/chunk":
                continue
            data = ev.get("data", {})
            chunk = data.get("chunk", {})
            if not (isinstance(chunk, dict) and chunk.get("type") == "usage"):
                continue
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                step_usage[(data.get("turn"), data.get("step"))] = usage
        step_metrics: dict[tuple[int, int], Metrics] = {}
        for key, u in step_usage.items():
            step_metrics[key] = Metrics(
                prompt_tokens=((u.get("inputTokens", 0) or 0)
                               + (u.get("cacheReadTokens", 0) or 0)),
                completion_tokens=u.get("outputTokens", 0) or 0,
                cached_tokens=u.get("cacheReadTokens", 0) or 0,
            )

        steps: list[Step] = []
        for ev in events:
            data = ev.get("data", {})
            if ev.get("type") == "user/message":
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        source="user",
                        message=_extract_block_text(data.get("content", []), "text"),
                    )
                )
            elif ev.get("type") == "assistant/message":
                message = data.get("message", {})
                blocks = message.get("content", [])
                text = _extract_block_text(blocks, "text")
                reasoning = _extract_block_text(blocks, "reasoning") or None
                tool_calls: list[ToolCall] = []
                for block in blocks:
                    if not (isinstance(block, dict) and block.get("type") == "tool-call"):
                        continue
                    try:
                        arguments = json.loads(block.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        arguments = {"raw": block.get("arguments")}
                    tool_calls.append(
                        ToolCall(
                            tool_call_id=block.get("id"),
                            function_name=block.get("name"),
                            arguments=arguments,
                        )
                    )
                observation = None
                if tool_calls:
                    observation = Observation(
                        results=[
                            ObservationResult(
                                source_call_id=tc.tool_call_id,
                                content=tool_results.get(tc.tool_call_id),
                            )
                            for tc in tool_calls
                        ]
                    )
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        source="agent",
                        model_name=model,
                        reasoning_effort=effort,
                        message=text or "",
                        reasoning_content=reasoning,
                        tool_calls=tool_calls or None,
                        observation=observation,
                        metrics=step_metrics.get(
                            (data.get("turn"), data.get("step"))
                        ),
                    )
                )

        if not steps:
            return None

        input_tokens = output_tokens = cached_tokens = 0
        for ev in events:
            if ev.get("type") != "assistant/chunk":
                continue
            usage = ev.get("data", {}).get("chunk", {}).get("usage")
            if isinstance(usage, dict):
                input_tokens += usage.get("inputTokens", 0) or 0
                output_tokens += usage.get("outputTokens", 0) or 0
                cached_tokens += usage.get("cacheReadTokens", 0) or 0

        return Trajectory(
            session_id=session_id,
            agent=TrajAgent(name="dsh", version="0.1.0rc6", model_name=model),
            steps=steps,
            final_metrics=FinalMetrics(
                # ATIF: prompt tokens "including cached" — per-step totals
                # (miss + hit) summed across the run.
                total_prompt_tokens=(input_tokens + cached_tokens) or None,
                total_completion_tokens=output_tokens or None,
                total_cached_tokens=cached_tokens or None,
                total_steps=len(steps),
            ),
        )


def _extract_block_text(blocks, block_type: str) -> str:
    """Concatenate text of one block type (text/reasoning/tool-result nested)."""
    out: list[str] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == block_type:
            text = block.get("text", "")
            if text:
                out.append(text)
        elif block_type == "text" and block.get("type") == "tool-result":
            out.append(_extract_block_text(block.get("content", []), "text"))
    return "\n".join(out)
