# 海外云沙箱（Daytona）运行指南

Harbor 原生支持云沙箱：`-e daytona`（也支持 modal/e2b/ec2 等，见
[Harbor 官方文档 Cloud Sandboxes](https://www.harborframework.com/docs/run-jobs/cloud-sandboxes)）。
任务执行移到云端后是 I/O 受限而不是 CPU 受限，并发可以远超本地核数。

## 前置

1. 注册 [Daytona Cloud](https://www.daytona.io/cloud)（免费额度 $200，无信用卡可开）
2. 在账号里使用优惠码 **`HARBOR_NETWORK`** 解除网络限制（否则很多 benchmark 跑不了）
3. 创建 API key

## 运行

```bash
pip install harbor[daytona]      # daytona SDK

export DAYTONA_API_KEY=dtn_xxxx
export DEEPSEEK_API_KEY=sk-xxxx
export DSH_GETPIP_URL=https://bootstrap.pypa.io/get-pip.py   # 海外直连官方

harbor run --path <tasks> -a dsh_tb_agent:DshAgent -m deepseek/deepseek-v4-pro \
  --ae DSH_REASONING_EFFORT=max \
  -e daytona \
  --agent-setup-timeout-multiplier 2 \
  -x qemu-startup -x qemu-alpine-ssh \
  -k 1 -n 8 -y
```

## 与本地（国内）运行的关键差异

| 项 | 本地 Docker | Daytona 云沙箱 |
|---|---|---|
| 镜像 | `docker.1ms.run/alexgshaw/...` 加速前缀 | `alexgshaw/...` 直连 Docker Hub |
| 容器网络 | 宿主 clash 代理注入 | 无代理，直连 |
| pip | TUNA 镜像 | PyPI 官方 |
| get-pip | 阿里云 | `bootstrap.pypa.io`（`DSH_GETPIP_URL`） |
| dsh API | 走代理（`NODE_USE_ENV_PROXY`） | 直连 `api.deepseek.com` |
| 资源规格 | 宿主 CPU/内存 | 按 `task.toml` 自动分配（cpus/memory_mb/storage_mb） |

## 已知限制与对策

### 免费层磁盘配额（30GiB）

Daytona 免费账号总磁盘 30GiB，TB 2.1 任务默认 `storage_mb = 10240`（10GB）→
**最多 3 个并发沙箱**。对策：

- 升级 tier（`app.daytona.io/dashboard/limits`）解锁更大磁盘/并发；
- 或调小任务的 `storage_mb`（例如轻任务 3GB / 编译类 6GB）换取更高并发。
  注意：同时跑 2 个 10GB + 4 个 3GB = 32GB 会撞线，先算好窗口。

### 超时任务重跑

max-effort 推理慢，官方 900s 超时下会有相当比例任务 agent 超时。
两个处理手段：

1. **重跑并放开超时**：`scripts/collect-timeouts.sh <job-dir> <src-tasks-dir> <dst>`
   收集 reward=0 且 `AgentTimeoutError` 的任务，把 `[agent].timeout_sec` 分档放宽
   （900→3600、1800→7200 等），再对 dst 单独 `harbor run`。
2. **合并进原 job**：Harbor 支持 `harbor job resume -p jobs/<ts> -f AgentTimeoutError`——
   resume 要求 job `config.json` 不变，而任务超时不在 config 里（从 `task.toml` 运行时读取），
   所以可以：改任务源的超时 → resume → 新结果写回同一个 job 目录。

### qemu 任务（容器 Python 3.9）

`qemu-startup` / `qemu-alpine-ssh` 的容器是 Python 3.9，dsh SDK 需要 ≥3.10，无法用本适配器。
Harbor 内置的 `terminus-2` agent（宿主侧驱动、仅需容器有 bash）可以跑它们。

## 成本速查（2026-08 官方定价）

| 资源 | 价格 |
|---|---|
| vCPU | $0.0504 / vCPU-h |
| 内存 | $0.0162 / GiB-h |
| 存储 | $0.000108 / GiB-h（前 5GB 免费） |

87 任务全量一次：实际均值 ~$2，全部跑满超时上限 ~$11。大头始终是模型 API 费用。
