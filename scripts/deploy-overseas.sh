#!/usr/bin/env bash
# ============================================================================
# Harbor TB 2.1 海外部署脚本 —— 贴官方环境（无代理、直连 docker hub / pypi）
#
# 用法（在海外 Linux 机器上，bundle 目录内）:
#   DEEPSEEK_API_KEY=sk-xxx ./deploy.sh            # 一步到位: 装环境+拉镜像+跑评测
#   CONCURRENCY=4 ./deploy.sh                       # 机器规格小就降并发（默认 8）
#   SKIP_PULL=1 ./deploy.sh                         # 跳过镜像预拉
#   INSTALL_ONLY=1 ./deploy.sh                      # 只装环境+拉镜像，不跑评测
#   或先 export DEEPSEEK_API_KEY=... 再 ./deploy.sh
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

CONCURRENCY="${CONCURRENCY:-8}"
MODEL="${MODEL:-deepseek/deepseek-v4-pro}"
EFFORT="${EFFORT:-max}"
LOG="harbor-overseas.log"

echo "==> [1/5] Docker"
if ! command -v docker >/dev/null 2>&1; then
  echo "    安装 docker ..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker || service docker start || true
fi
docker info >/dev/null 2>&1 || { echo "!! docker daemon 未运行: 请先启动 (systemctl start docker)"; exit 1; }

echo "==> [2/5] Python + venv"
command -v python3 >/dev/null || { echo "!! 需要 python3 (>=3.12)"; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
  || { echo "!! 需要 Python >=3.12 (Harbor 0.25.0)"; exit 1; }
if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q -U pip setuptools wheel
  ./.venv/bin/pip install -q harbor==0.25.0 deepseek-harness-sdk==0.1.0rc6
  ./.venv/bin/pip install -q -e ./tb2-agent
fi
. ./.venv/bin/activate

echo "==> [3/5] API key"
: "${DEEPSEEK_API_KEY:?请通过环境变量传入 DEEPSEEK_API_KEY (export DEEPSEEK_API_KEY=sk-...)}"
echo "    model: $MODEL | effort: $EFFORT | concurrency: $CONCURRENCY"

echo "==> [4/5] 预拉任务镜像 (alexgshaw/*:20251031, 直连 docker hub)"
if [ "${SKIP_PULL:-0}" != "1" ]; then
  grep -h '^docker_image' tasks/*/task.toml | sed 's/.*= *"//; s/"//' | sort -u \
    | xargs -P 4 -I{} docker pull {} || echo "!! 部分镜像拉取失败，harbor 运行时会自动补拉"
fi

echo "==> [5/5] Harbor run (89 tasks, -n $CONCURRENCY, 无代理 / 官方镜像)"
set -a; . ./.env 2>/dev/null || true; set +a
export DEEPSEEK_API_KEY
nohup harbor run --path tasks \
  -a dsh_tb_agent:DshAgent \
  -m "$MODEL" \
  --ae DSH_REASONING_EFFORT="$EFFORT" \
  -x qemu-startup -x qemu-alpine-ssh \
  -k 1 -n "$CONCURRENCY" -y \
  > "$LOG" 2>&1 &
echo "    harbor run 已启动 (pid $!) -> 日志: $LOG"
echo "    监控: tail -f $LOG"
echo "    (qemu-startup / qemu-alpine-ssh 因容器内 Python 3.9 不支持 dsh SDK 而排除)"
