# Harbor run 命令示例

## 本地 Docker（国内，走代理）

```bash
export HOME=$PWD/.harbor-home
set -a; . ./.env; set +a   # DEEPSEEK_API_KEY 等

harbor run \
  --path tasks \
  -a dsh_tb_agent:DshAgent \
  -m deepseek/deepseek-v4-pro \
  --ae DSH_REASONING_EFFORT=max \
  --ae http_proxy=http://<PROXY_HOST>:7890 前缀补 http:// \
  --ae https_proxy=<PROXY_HOST>:7890 前缀补 http:// \
  -x qemu-startup -x qemu-alpine-ssh \
  -k 1 -n 8 -y
```

## Daytona 云沙箱（海外，贴官方环境）

```bash
export DAYTONA_API_KEY=dtn_xxx
export DSH_GETPIP_URL=https://bootstrap.pypa.io/get-pip.py

harbor run \
  --path tasks \
  -a dsh_tb_agent:DshAgent \
  -m deepseek/deepseek-v4-pro \
  --ae DSH_REASONING_EFFORT=max \
  -e daytona \
  --agent-setup-timeout-multiplier 2 \
  -x qemu-startup -x qemu-alpine-ssh \
  -k 1 -n 8 -y
```

## 超时任务重跑（放开 agent 超时）

```bash
# 1. 收集超时丢分任务 → retry-timeout/（分档放开 timeout_sec）
scripts/collect-timeouts.sh jobs/2026-08-15__19-28-13 tasks retry-timeout

# 2. 重跑（并发 = 任务数，磁盘允许）
harbor run --path retry-timeout -a dsh_tb_agent:DshAgent -m deepseek/deepseek-v4-pro \
  --ae DSH_REASONING_EFFORT=max -e daytona --agent-setup-timeout-multiplier 2 \
  -k 1 -n "$(ls retry-timeout | wc -l)" -y

# 3.（可选）把重跑结果合并回原 job —— resume 要求 config.json 不变，任务超时不在 config 里
harbor job resume -p jobs/2026-08-15__19-28-13 -f AgentTimeoutError
```

## 本地冒烟（1-3 个快任务）

```bash
mkdir -p smokeset && cp -r tasks/{bn-fit-modify,fix-git,crack-7z-hash} smokeset/
harbor run --path smokeset -a dsh_tb_agent:DshAgent -m deepseek/deepseek-v4-pro \
  --ae DSH_REASONING_EFFORT=max -e daytona --agent-setup-timeout-multiplier 2 -k 1 -n 3 -y
```

## 用 mock LLM 看请求结构

```bash
python3 tools/mock-llm.py 8899 &            # 捕获到 captured.jsonl
cd <workspace> && export HOME=<writable-home> \
  DSH_MODEL=deepseek-v4-pro DEEPSEEK_API_KEY=test \
  DEEPSEEK_BASE_URL=http://127.0.0.1:8899 \
  DSH_SESSION_ROOT=$PWD/sessions DSH_MAX_TOKENS=4096 \
  DSH_REASONING_EFFORT=max
python3 run_dsh.py minimal.cordis.yml instruction.txt mock-session result.json
# 看 captured.jsonl：messages / tools / reasoning_effort
```
