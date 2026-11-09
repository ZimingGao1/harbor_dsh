#!/usr/bin/env python3
"""批量重算已有 job 的 ATIF trajectory（修复 cacheReadTokens 累计语义后）。

用法: python3 rebuild-trajectories.py <job-dir>
对每个 trial: agent/sessions/*.jsonl -> trajectory.json（原文件备份为 .bak）
"""
import json
import logging
import sys
from pathlib import Path

from dsh_tb_agent.agent import DshAgent
from harbor.utils.trajectory_utils import format_trajectory_json


def rebuild(job_dir: Path) -> tuple[int, int, int]:
    agent = DshAgent.__new__(DshAgent)
    agent.logger = logging.getLogger("rebuild")
    n_ok = n_skip = n_bak = 0
    for trial in sorted(job_dir.iterdir()):
        if not trial.is_dir():
            continue
        sessions = trial / "agent" / "sessions"
        if not sessions.is_dir():
            continue
        jsonls = sorted(sessions.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not jsonls:
            n_skip += 1
            continue
        model = None
        try:
            cfg = json.load(open(trial / "config.json"))
            model = (
                (cfg.get("agent", {}).get("model_name") or "").split("/", 1)[-1] or None
            )
        except Exception:
            pass
        if not model:
            n_skip += 1
            continue
        latest = jsonls[-1]
        session_id = latest.name.removesuffix("-session.jsonl")
        traj = agent._build_trajectory(latest, session_id, model)
        if traj is None:
            n_skip += 1
            continue
        out = trial / "agent" / "trajectory.json"
        if out.exists():
            out.replace(out.with_suffix(".json.bak"))
            n_bak += 1
        out.write_text(format_trajectory_json(traj.to_json_dict()), encoding="utf-8")
        n_ok += 1
    return n_ok, n_skip, n_bak


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <job-dir>", file=sys.stderr)
        sys.exit(2)
    ok, skip, bak = rebuild(Path(sys.argv[1]))
    print(f"重算 {ok} 个 trajectory（备份 {bak} 个），跳过 {skip} 个（无 session/模型）")
