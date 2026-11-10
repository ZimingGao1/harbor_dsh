# 千帆受限模型代理：本地验证与阿里云单题部署

目标：Harbor 0.25.0、DSH SDK/runtime 0.1.0rc6 不变，保持当前 Minimal、
500 步策略。代理在任务容器之外保存真实 Key，仅允许指定模型接口，并补充
`top_p=0.95`。本次没有自动调用付费 API，也没有启动评测。

## 边界

```text
任务 main 容器（独立 PID/挂载命名空间、无真实 Key）
  DSH SDK/runtime ── 短期代理 token ──► dsh-model-proxy:8080
  Bash             ── 也可访问同一受限模型通道 ──┘
        │                                │
        └── 直接公网/宿主/云元数据被阻断    └── HTTPS 千帆固定上游
```

这实现的是“任务禁止直接公网，只开放受限模型通道”。Bash 和模型进程同网络，
Bash 可读/使用代理 token，不能声称只有 DSH 可以请求模型。这也是一个受限的
信息传出通道。代理 token 不是千帆 Key，按 trial 独立生成，有到期时间、次数和
输出预算限制；禁止复用真实 Key 作为 token。若要求 Bash 连模型通道也不能用，
需要进一步分离执行身份/网络，当前没有实现。

以下部署属于**单题隔离验收模板**，没有 Docker 实测结果（当前 Mac 无 Docker）。
必须在阿里云 Linux 以 dummy 凭证完成验收后，才能宣称隔离通过。
容器/内核漏洞、宿主 Docker 管理员及任务原本挂载的敏感宿主目录不在此边界内。

## 代理行为

`src/dsh_tb_agent/model_proxy.py` 仅使用 Python 标准库：

- 生产入口固定 HTTPS 上游 `qianfan.baidubce.com:443/v2/chat/completions`，
  TLS 验证开启，不读取系统 HTTP 代理，不提供上游 URL 配置。
- 仅 POST 精确路径 `/v2/chat/completions`；禁止 CONNECT、绝对 URL、query、
  其他模型、额外顶层字段；不跟随重定向。
- 只接受 enabled/max/temperature=1、stream=true；缺失 top_p 时补充 0.95，
  已提供但值不同则拒绝。messages/tools/reasoning 原样透传。
- 上游 Authorization 由代理重新生成，不转发客户端认证头。
- 默认最多 1600 次（含失败请求）、2 小时 token 有效期、单次输出不超过 49152、
  16 MiB 请求体、4 个并发连接、120 秒上游 socket 空闲超时。
  这些是代理的工程预算，不是模型能力声明；1600 次兼容最多 500 步及 SDK 重试。
- SSE 字节流转发，断流不补 `[DONE]`；代理不重试。上游 HTTP 错误保留状态码，
  正文和响应头不回传，防止反射认证值。这会减少上游错误详情。
- 日志仅包含请求序号、模型、top_p、状态码、耗时，无认证头/请求正文/工具输出。
  单个 trial 的 proxy 容器日志与 Harbor job 一起保留。

代理 token 放入任务进程环境；真实千帆 Key 仅以只读文件挂载到代理。宿主 Harbor
进程也不应配置真实 Key，更不能用 `--ae QIANFAN_API_KEY=...`。
这样任务 `/proc` 只能看到自己的进程和代理 token，而不是依靠 Bash 环境清理。

## 本地离线测试

```bash
DSH_TEST_ARTIFACT_DIR=/tmp/dsh-proxy-validation \
  .venv/bin/python -m unittest discover -s tests -v
```

真实 SDK/runtime 对接本地代理及 HTTP Mock，测试注入本地连接 factory；生产 CLI
没有该入口。所有凭证均为 dummy，不继承真实环境变量。
验证 top_p、原参数、鉴权替换、路径/模型/字段限制、重定向与错误不回传密钥、
预算/到期、断流不伪造完成，并保留原有 500 步和适配器测试。

本地进程 Mock 不能验证 Docker PID、挂载、capabilities、防火墙、DNS 或公网阻断。
不要用本地 Mock 通过代替下面的 Linux 验收。

## 阿里云准备：先 dummy，后真实 Key

需要 Linux Docker Engine 的 iptables 后端、Docker Compose >=2.24.4、Python 3.12。
不要在默认公网开放的 Docker 环境中直接开始模型评测。

```bash
export DSH_REPO_ROOT="$PWD"
export DSH_TASK_SUBNET=172.30.90.0/24
export DSH_PROXY_IP=172.30.90.2
export DSH_TASK_IP=172.30.90.3
export DSH_QIANFAN_KEY_FILE=/etc/dsh-proxy-smoke/qianfan_key
export DSH_PROXY_TOKEN_FILE=/etc/dsh-proxy-smoke/proxy_token

# 仅构建代理，内部没有 Harbor 或 DSH 依赖。
docker build -t dsh-model-proxy:local -f deploy/model-proxy/Dockerfile .
docker pull python:3.12.10-slim

# dummy 文件，不涉及真实 API Key。UID 与 proxy 镜像用户一致。
sudo python3 - <<'PY'
from pathlib import Path
import os, secrets
root = Path('/etc/dsh-proxy-smoke')
root.mkdir(mode=0o700, exist_ok=True)
for name, value in [('qianfan_key', 'dummy-upstream-isolation-sentinel'),
                    ('proxy_token', secrets.token_urlsafe(32))]:
    p = root / name
    if p.exists():
        raise SystemExit('Files already exist; choose a fresh trial directory')
    p.write_text(value)
    os.chown(p, 10001, 10001)
    p.chmod(0o400)
PY
```

确认 subnet 未被宿主、VPC 或其他 Docker network 使用。模板只允许一个 trial
使用这个 subnet；后续并发必须给每个 trial 独立 subnet、token 和 proxy。
不要把这份固定 IP 模板直接用于 267 次并发运行。

在启动容器前安装限制规则（不清空现有规则）：

```bash
sudo modprobe br_netfilter
sudo sysctl -w net.bridge.bridge-nf-call-iptables=1
sudo env DSH_TASK_SUBNET="$DSH_TASK_SUBNET" DSH_PROXY_IP="$DSH_PROXY_IP" \
  bash deploy/model-proxy/harden-network.sh
```

脚本在 DOCKER-USER 链只允许任务网段到 proxy:8080 的连接及其响应，拒绝其他转发；
INPUT 链阻止任务访问宿主上的其他服务，包括可能存在的通用 HTTP/SOCKS 代理。
任务禁用 IPv6、去除 NET_ADMIN/NET_RAW/SYS_ADMIN/SYS_PTRACE 等能力，防止修改规则
或伪造 IP；只连接 internal network。DNS upstream 指向容器自身，保留 Docker
内部服务名解析，禁止任意公网 DNS 转发。所有这些都需要实测。

若 Docker 使用 nftables 后端而没有 DOCKER-USER，脚本会停止，**不能跳过**；
需单独适配该后端的主机网络规则。重启/防火墙服务更新后应重新检查规则。

## 不付费的隔离验收

```bash
docker compose -p dsh-proxy-isolation \
  -f deploy/model-proxy/probe.yaml -f deploy/model-proxy/harbor-proxy.yaml config --quiet
docker compose -p dsh-proxy-isolation \
  -f deploy/model-proxy/probe.yaml -f deploy/model-proxy/harbor-proxy.yaml up -d --wait

MAIN_ID=$(docker compose -p dsh-proxy-isolation -f deploy/model-proxy/probe.yaml \
  -f deploy/model-proxy/harbor-proxy.yaml ps -q main)
PROXY_ID=$(docker compose -p dsh-proxy-isolation -f deploy/model-proxy/probe.yaml \
  -f deploy/model-proxy/harbor-proxy.yaml ps -q dsh-model-proxy)
sudo python3 tools/check-proxy-deployment.py "$MAIN_ID" "$PROXY_ID"
docker exec "$MAIN_ID" python /checks/check-task-isolation.py
```

两条检查均须通过。它们检查实际 Docker 网络/挂载/capability/PID 与 mount namespace、
防火墙拒绝规则、任务 `/proc` 中的 dummy Key、secret 文件不可见性、直连公网 TCP、
IPv6、DNS、云元数据、宿主服务阻断以及代理可达但未认证请求被拒。
探针不会向千帆发出已认证请求，也不读取/输出真实 Key。
探针里的宿主地址默认是 172.30.90.1；若改 subnet，必须同步该地址。
公共连通性阴性结果只有在宿主本身可达相应网络时才有意义；用宿主的非付费
HTTP 测试确认正向对照，再记录结果。以上探针为有限测试，还应审阅有效规则，
不能当作任意目标完全不可达的数学证明。

保存检查输出、`iptables-save`、容器及镜像 ID、网络元数据（过滤认证值）。
不要将未过滤的 `docker inspect` 环境或 secret 文件输出到报告。
验收后只停止本次 probe，保留镜像/文件/证据：

```bash
docker compose -p dsh-proxy-isolation -f deploy/model-proxy/probe.yaml \
  -f deploy/model-proxy/harbor-proxy.yaml down
```

## 接入 Harbor 单题：离线安装与保留密钥边界

隔离环境不能运行现有的 apt/pip 公网引导。因此先在评测外为单题镜像预装
Python >=3.10、pip 和 SDK/runtime 0.1.0rc6；确认目标架构 linux/amd64。
保存衍生镜像 digest，并把镜像变化记录为相对官方镜像的差异。
不要在正式计时期间联网安装或临时给任务开放公网。

`DSH_OFFLINE_INSTALL=1` 仅校验预装 SDK/runtime 精确版本并上传现有 driver/config/plugin，
不会 apt/pip 下载；未预装时直接失败，正常安装路径保持原样。

将真实 Key 写入**新 trial 专用**目录的 key 文件，不配置给 Harbor 的环境：

```bash
# 在服务器手动执行；输入隐藏，不进入 shell history 或命令行参数。
sudo python3 - <<'PY'
from pathlib import Path
from getpass import getpass
import os, secrets
root = Path('/etc/dsh-proxy-single')
root.mkdir(mode=0o700, exist_ok=True)
key = getpass('Raw Qianfan API key (hidden): ')
if not key or key.lower().startswith('bearer ') or any(ord(c)<33 or ord(c)>126 for c in key):
    raise SystemExit('Invalid raw key')
for name, value in [('qianfan_key', key), ('proxy_token', secrets.token_urlsafe(32))]:
    p = root / name
    if p.exists():
        raise SystemExit('Use a fresh trial secret directory')
    p.write_text(value)
    os.chown(p, 10001, 10001)
    p.chmod(0o400)
PY
```

之后设置文件路径并让适配器只传代理 token；**以下 Harbor 命令包含真实模型调用，
本次不执行，须完成 dummy 验收后由负责人手动启动单题**：

```bash
unset QIANFAN_API_KEY DEEPSEEK_API_KEY QIANFAN_BASE_URL DEEPSEEK_BASE_URL
export DSH_QIANFAN_KEY_FILE=/etc/dsh-proxy-single/qianfan_key
export DSH_PROXY_TOKEN_FILE=/etc/dsh-proxy-single/proxy_token
export DSH_PROXY_URL=http://dsh-model-proxy:8080/v2
export DSH_PROXY_TOKEN="$(sudo cat "$DSH_PROXY_TOKEN_FILE")"
export DSH_OFFLINE_INSTALL=1
harbor run --path /absolute/path/to/ONE-prepared-task \
  -a dsh_tb_agent:DshAgent -m deepseek/deepseek-v4.1-flash-1017 \
  --extra-docker-compose "$PWD/deploy/model-proxy/harbor-proxy.yaml" \
  -n 1 -k 1
```

这里使用已安装 Harbor 0.25 的 `--extra-docker-compose` 接口，没有修改 Harbor。
任务的自定义 compose、network_mode、secrets、host mounts 或 Harbor 动态网络策略
可能影响合并结果。必须对**真实 trial 容器**重复上述 host/inside 检查；不要仅检查
独立 probe 就认定 Harbor 的实际容器已经隔离。
若任务已有多个服务/特殊网络，先单独适配；本模板不能直接覆盖所有 89 题。
本模板没有把 Harbor task 声明的禁止网络策略改成允许网络；若 Harbor 额外施加
none/deny-all，代理也可能不可达，需明确配置模型通道例外并记录差异，不能绕过
后声称“完全无网络”。

真实 Key 不得出现于 `.env`、Harbor `--ae`、`docker run -e`、任务文件或挂载目录。
真实 Key 只在 proxy 文件和内存中；任务拿到 token 是预期行为。不要给任务挂载
`/etc/dsh-proxy-*`、Docker socket 或宿主 `/proc`。不要运行 `set -x` 打印 token。

代理日志的 200 表示上游已接受连接，不保证 SSE 完整完成；完成以 session/driver/
verifier 为准。500 步封顶会产生 blocked，不是通过。当前 driver 的
`evaluation.top_p_supported=false` 记录的是原始 SDK 不支持；代理补充后的值
以 proxy/upstream 捕获或千帆入站记录为证，不能用 driver 元数据替代。

## 当前验收状态

- 本地真实 SDK 经代理的 HTTP 参数与回包已验证。
- 原有 6 项测试保留；官方 SDK/Harbor 未升级或修改。
- Docker/防火墙/命名空间隔离、千帆真实入站 top_p 及 89 题特殊网络尚未验收。
- 采样字段已发送不代表千帆思维模式实际采用了采样值。
- 本文只给单题准备步骤，没有启动付费调用或全量评测。
