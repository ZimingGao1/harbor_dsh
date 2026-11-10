# harbor-dsh-agent

Run **Terminal-Bench 2.1** (and any other Harbor task) with **DeepSeek Harness (dsh)** as the agent.

This repository contains a Harbor *installed agent* that:

1. Bootstraps Python + pip inside the task container (works on minimal images that ship no Python at all),
2. `pip install`s the [deepseek-harness-sdk](https://pypi.org/project/deepseek-harness-sdk/) (bundled single-file Node runtime — no Node toolchain needed inside the container),
3. Uploads a minimal dsh agent composition (`minimal.cordis.yml`): persistent bash + string-replace editor tools, DeepSeek LLM adapter, no harness identity / skills / workspace context,
4. Runs the task instruction through dsh and exports an **ATIF trajectory** back to Harbor (`trajectory.json`), even when the agent is killed by the official time limit.

## Architecture

```
┌─ Harbor control plane ─────────────────────────────┐
│  DshAgent (BaseInstalledAgent)                     │
│   ├─ install(): bootstrap python/pip → upload      │
│   │   driver + cordis → pip install SDK            │
│   └─ run(): exec driver → dsh session → ATIF traj  │
└──────────────┬─────────────────────────────────────┘
               │ environment.exec() / upload / download
┌──────────────▼─────────────────────────────────────┐
│  Task container (Docker / Daytona / E2B / …)       │
│   └─ run_dsh.py → dsh JSON-RPC runtime             │
│       ├─ llm-deepseek  (DeepSeek API)              │
│       ├─ persistent bash tool                      │
│       └─ str_replace_editor tool                   │
└────────────────────────────────────────────────────┘
```

Why an *installed* agent? dsh needs Python ≥ 3.10 in the task container, while the
Harbor control plane for this project uses Python ≥ 3.12; many official TB 2.1
images are bare (no Python at all). The bootstrap chain handles every case:

```
python3 + pip present        → use it
python3 present, no pip      → ensurepip → get-pip.py (configurable mirror)
no python3                   → apt install python3 python3-pip (noninteractive)
```

## Quick start

```bash
# 1. Install the adapter into your Harbor environment
pip install -e .

# 2. Point Harbor at your tasks dataset
harbor run \
  --path <tasks-dir> \
  -a dsh_tb_agent:DshAgent \
  -m deepseek/deepseek-v4-pro \
  --ae DSH_REASONING_EFFORT=max \
  -n 8 -k 1 -y
```

Local (Docker) sandboxes, cloud sandboxes and even the official Harbor Hub all use
the same `-a dsh_tb_agent:DshAgent` entry point.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `QIANFAN_API_KEY` | — | 千帆 API key; adapter forwards it to DSH as `DEEPSEEK_API_KEY` |
| `QIANFAN_BASE_URL` | `https://qianfan.baidubce.com/v2` | 千帆 OpenAI-compatible API root |
| `DSH_MODEL` | `deepseek-v4.1-flash-1017` | Model id (`-m` on the CLI overrides) |
| `DSH_REASONING_EFFORT` | `max` | `off` \| `low` \| `high` \| `max`, applied by `effort-plugin.mjs` |
| `DSH_THINKING` | `enabled` | Sent as `thinking: {"type": ...}` by the official DSH adapter |
| `DSH_TEMPERATURE` | `1.0` | Sent as the official adapter's `temperature` field |
| `DSH_MAX_TOKENS` | `49152` | Max output tokens per request |
| `DSH_SYSTEM_PROMPT` | `You are a helpful software engineer assistant.` | The single system prompt (persona) |
| `DSH_PIP_INDEX_URL` | official PyPI | pip mirror for in-container installs (e.g. TUNA) |
| `DSH_GETPIP_URL` | Aliyun mirror | get-pip.py download URL (use `https://bootstrap.pypa.io/get-pip.py` outside China) |
| `DSH_SESSION_ROOT` | `/tmp/dsh-sessions` | Where dsh keeps session JSONL in the container |

### Trajectories

The container-side driver tails the dsh session JSONL and Harbor downloads it.
`agent/trajectory.json` (ATIF format) is written from the host side in
`populate_context_post_run`, so **trajectories survive agent timeouts** —
exactly what you want when max-effort reasoning hits the official time limit.

## Tools

- `tools/monitor.py` — zero-dependency live monitor panel (`python3 tools/monitor.py <port>`):
  per-task status, rewards, score ratio, ETA, per-task trajectory/log links.
  `JOB_OVERRIDE=jobs/<ts>` pins a specific job; `DSH_RUN_ROOT` / `DSH_TASKS_DIR` configure paths.
- `tools/mock-llm.py` — DeepSeek-compatible mock API that captures the exact request
  structure (messages/tools/reasoning_effort/thinking/temperature) — point
  `DEEPSEEK_BASE_URL` at it. The official DSH SDK used here has no `top_p`
  field, so `top_p=0.95` is not emitted or claimed to be verified.
- `tools/rebuild-trajectories.py` — rebuild `trajectory.json` for an existing job
  from its session JSONL (use after upgrading the token-metrics logic; backs up originals as `.bak`).
- `scripts/collect-timeouts.sh` — collect agent-timeout trials (reward 0) into a retry
  directory with relaxed `[agent].timeout_sec` (see docs).
- `scripts/deploy-overseas.sh` — one-shot overseas runner (Daytona / bare VPS).

## Docs

- [docs/china-network.md](docs/china-network.md) — running from behind the GFW (proxy, mirrors)
- [docs/overseas.md](docs/overseas.md) — cloud sandboxes (Daytona) & official-environment runs
- [docs/troubleshooting.md](docs/troubleshooting.md) — every pitfall we hit and how to avoid it

## Requirements

- Harbor `>= 0.21.0` (`pip install harbor`)
- Python `>= 3.12` (Harbor control plane)
- A Qianfan API key
- The TB 2.1 dataset (`harbor dataset download terminal-bench-2.1` or any task directory)

## License

MIT
