#!/usr/bin/env python3
"""Container-side driver: run one DeepSeek Harness session against a task.

Runs inside the Harbor task container. Reads the task instruction from a file,
starts the bundled dsh JSON-RPC runtime through the Python SDK, waits for the
whole agent turn to finish, and writes a JSON result file into /logs/agent
(which Harbor mounts from the host trial directory).

Usage: run_dsh.py <cordis.yml> <instruction.txt> <session-id> <result-path>
"""

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 5:
        print(f"usage: {sys.argv[0]} <cordis.yml> <instruction.txt> <session-id> <result-path>", file=sys.stderr)
        return 2

    cordis_path, instruction_path, session_id, result_path = sys.argv[1:5]

    try:
        instruction = Path(instruction_path).read_text(encoding="utf-8")
    except OSError as exc:
        return _finish(result_path, {"ok": False, "error": f"cannot read instruction: {exc}"})

    model = os.environ.get("DSH_MODEL", "")
    if not model:
        return _finish(result_path, {"ok": False, "error": "DSH_MODEL is not set"})

    # Node's fetch (undici) ignores http_proxy by default; with the env-proxy
    # flag on Node 24 the bundled dsh runtime routes its model requests
    # through the same proxy as the shell tools. Only meaningful when a proxy
    # is configured.
    if os.environ.get("http_proxy") or os.environ.get("https_proxy"):
        os.environ.setdefault("NODE_USE_ENV_PROXY", "1")

    try:
        max_tokens = int(os.environ.get("DSH_MAX_TOKENS", "49152") or "49152")
    except ValueError:
        max_tokens = 49152

    try:
        max_steps = int(os.environ.get("DSH_MAX_STEPS", "500"))
        context_window = int(os.environ.get("DSH_CONTEXT_WINDOW", "1000000"))
        if not (1 <= max_steps <= 2**53 - 1 and 1 <= context_window <= 2**53 - 1):
            raise ValueError
    except ValueError:
        return _finish(result_path, {
            "ok": False,
            "error": "DSH_MAX_STEPS and DSH_CONTEXT_WINDOW must be positive safe integers",
        })

    try:
        from deepseek_harness import DeepSeekHarness
    except ImportError as exc:
        return _finish(result_path, {"ok": False, "error": f"deepseek_harness import failed: {exc}"})

    session_root = os.environ.get("DSH_SESSION_ROOT", "/tmp/dsh-sessions")
    dest_dir = Path(result_path).parent / "sessions"
    stop_event = threading.Event()
    sync_thread = threading.Thread(
        target=_sync_session_loop,
        args=(session_root, dest_dir, session_id, stop_event),
        daemon=True,
    )
    sync_thread.start()

    try:
        with DeepSeekHarness(
            provider="deepseek-official",
            model=model,
            max_tokens=max_tokens,
            cwd=os.getcwd(),
            session_root=session_root,
            cordis=cordis_path,
        ) as harness:
            result = harness.run(instruction, session_id=session_id)
        payload = {
            "ok": True,
            "session_id": result.session_id,
            "finish_reason": result.finish_reason,
            "final_response": result.final_response,
            "evaluation": {
                "max_steps_per_turn": max_steps,
                "context_window_metadata": context_window,
                "max_output_tokens": max_tokens,
                "top_p_supported": False,
            },
        }
        steps_started = sum(e.get("type") == "step/start" for e in result.events)
        payload["steps_started"] = steps_started
        payload["step_limit_reached"] = (
            result.finish_reason == "blocked" and steps_started == max_steps
        )
        if result.finish_reason == "error":
            payload["error_detail"] = _extract_error_detail(result)
    except Exception as exc:  # noqa: BLE001 - report any failure to Harbor
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        stop_event.set()
        sync_thread.join(timeout=5)
        # Final flush so the latest events survive even when the driver is
        # killed right after (agent timeout): the sync loop may have missed
        # the last few seconds.
        _copy_session(session_root, dest_dir, session_id)

    return _finish(result_path, payload)


def _sync_session_loop(
    session_root: str, dest_dir: Path, session_id: str, stop_event: threading.Event
) -> None:
    """Continuously copy the live dsh session JSONL next to the result file
    (mounted to the host trial agent dir), so a killed driver still leaves a
    trajectory behind. Best-effort: any error is swallowed."""
    while not stop_event.is_set():
        try:
            _copy_session(session_root, dest_dir, session_id)
        except Exception:  # noqa: BLE001
            pass
        stop_event.wait(3)


def _copy_session(session_root: str, dest_dir: Path, session_id: str) -> None:
    try:
        root = Path(session_root)
        if not root.is_dir():
            return
        out = dest_dir / f"{session_id}-session.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        hits = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not hits:
            return
        # copyfile (not copy2): do not carry the runtime's private 0600 mode
        # into the mounted dir, or the host control plane cannot read it.
        shutil.copyfile(hits[-1], out)
        os.chmod(out, 0o644)
    except Exception:  # noqa: BLE001
        pass



def _extract_error_detail(result) -> dict:
    """Best-effort error context from the last session events and notifications."""
    detail = {}
    try:
        import json as _json

        tail_events = [
            e.model_dump() if hasattr(e, "model_dump") else e
            for e in getattr(result, "events", [])[-6:]
        ]
        detail["last_events"] = _json.loads(
            _json.dumps(tail_events, default=str, ensure_ascii=False)
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        notifications = getattr(result, "notifications", None) or []
        texts = []
        for n in notifications[-10:]:
            text = getattr(n, "message", None) or getattr(n, "text", None)
            if text:
                texts.append(str(text))
        detail["last_notifications"] = texts
    except Exception:  # noqa: BLE001
        pass
    return detail


def _finish(result_path: str, payload: dict) -> int:
    try:
        Path(result_path).parent.mkdir(parents=True, exist_ok=True)
        Path(result_path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"cannot write result file {result_path}: {exc}", file=sys.stderr)
        return 1
    print(payload.get("final_response") or json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
