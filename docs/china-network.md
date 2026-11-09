# 国内网络环境（GFW 后面）运行指南

在本地 Docker 沙箱跑 Terminal-Bench 时，容器需要访问 PyPI、GitHub、DeepSeek API。
国内网络下按以下三层处理：

## 1. 宿主机代理（clash / mihomo 等）

容器统一走宿主机的 HTTP 代理（LAN IP，任意 docker 网络可达）：

```bash
# 放行宿主代理端口（7890 示例，mihomo mixed-port）
sudo iptables -I INPUT -p tcp --dport 7890 -j ACCEPT

# harbor run 时注入代理环境变量（--ae = agent env）
harbor run --path <tasks> -a dsh_tb_agent:DshAgent -m deepseek/<model> \
  --ae http_proxy=http://<PROXY_HOST>:7890 前缀补 http:// \
  --ae https_proxy=<PROXY_HOST>:7890 前缀补 http:// \
  -n 8 -y
```

- 代理地址用 **LAN IP**（如 `192.168.1.4`），不要用 `127.0.0.1`（容器内回环不通宿主）。
- 确保 mihomo `allow-lan: true`，且**不要**用 `route-exclude-address` 把 docker 网段排除。
- verifier 也需要代理：给任务 `task.toml` 的 `[verifier.env]` 注入同样的 `http_proxy/https_proxy/no_proxy`。
- dsh 的 Node runtime 默认不读 `http_proxy`，driver 会自动设置 `NODE_USE_ENV_PROXY=1`（见 `run_dsh.py`）。

## 2. 容器内 pip / get-pip 镜像

适配器通过环境变量控制镜像，不硬编码：

```bash
# 国内：TUNA 源 + 阿里云 get-pip
export DSH_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# DSH_GETPIP_URL 默认已是阿里云，无需设置
```

海外环境（Daytona / 海外 VPS）则**不设置**这两个变量，自动走官方 PyPI 和 bootstrap.pypa.io。

## 3. 镜像加速

TB 2.1 官方镜像在 Docker Hub（`alexgshaw/<name>:20251031`）。国内拉取慢时：

- 把 `task.toml` 的 `docker_image` 前缀换成加速器，例如 `docker.1ms.run/alexgshaw/...`；
- 或提前 `docker pull` 全部镜像（89 个）本地化。

> 贴官方环境运行（无任何 hack）请看 [overseas.md](overseas.md)。

## 常见问题

| 现象 | 原因 / 解法 |
|---|---|
| dsh 请求 `TRANSPORT` 错误 | Node fetch 没走代理 → 确认 `NODE_USE_ENV_PROXY=1`（driver 自动设置）或 `DEEPSEEK_BASE_URL` 为空串被显式传入（空值不要传） |
| 容器内 `docker.1ms.run` 拉镜像超时 | 换回 `alexgshaw/` 直连（海外机器）或换加速器 |
| verifier 判分时网络卡死 | `[verifier.env]` 漏了代理 |
| busybox 容器 wget 走代理 502 | busybox 的 CONNECT 实现不兼容，用真实工具链（curl/apt/uv） |
