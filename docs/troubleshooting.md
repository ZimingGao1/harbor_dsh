# Troubleshooting（踩坑记录）

这里记录了把 dsh 适配器跑通 Harbor 全流程时遇到的所有坑。按阶段排列。

## 控制面（Harbor 宿主进程）

### HOME 只读导致 harbor 写缓存失败

Harbor 会写 `~/.cache/harbor`。在受限环境（sandbox/容器）里 `HOME` 不可写会直接失败：

```bash
export HOME=$PWD/.harbor-home
export XDG_CACHE_HOME=$PWD/.xdg-cache
export XDG_CONFIG_HOME=$PWD/.xdg-config
export XDG_DATA_HOME=$PWD/.xdg-data
```

### 后台任务被误杀

`harbor run` 用 `nohup ... &` 启动时，如果启动它的 shell/agent 会话被 abort，
进程组信号会连带杀掉 harbor。用 `setsid` 完全脱离会话：

```bash
(setsid nohup harbor run ... > run.log 2>&1 &)
```

## 容器内 setup（install 引导链）

### apt 装 tzdata 卡交互

Daytona 等无 TTY 的 exec 里，`apt-get install python3` 会在 tzdata 的 debconf 提示上
卡到超时。必须：

```bash
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-pip
```

（本地 Docker 有 TTY 所以从不暴露——只有云沙箱踩到。）

### 1-CPU 沙箱 apt 太慢

云沙箱 1 vCPU 装 python3 全套要 250s+，超过 setup 预算 360s：

```bash
# 适配器内已把 apt timeout 提到 300s
harbor run ... --agent-setup-timeout-multiplier 2   # 只放宽 setup（基础设施），不动任务时限
```

### Debian 13 的 pip RECORD bug

`python3 -m pip install` 升级系统包时报 `RECORD not found`（Debian 打包不带 RECORD）：

```bash
pip install --break-system-packages --ignore-installed deepseek-harness-sdk
```

老镜像不支持 `--break-system-packages`/`--ignore-installed` 时自动回退无 flag 版本（适配器已处理）。

## dsh 运行时

### `filename must be an instance of str`（aiohttp TypeError）

Harbor 的 Daytona 环境把 upload 目标路径直接传给 SDK，SDK 再交给 aiohttp 的
`filename=` 参数。**传 `pathlib.Path` 会炸**（`isinstance(Path, str)` 为 False，
`%s` 打印出来却和字符串一模一样，极易误判）。

适配器内所有 upload target 都用 `str()` 包裹（见 `agent.py` 顶部的 `_DRIVER_PATH` 等）。

### Node 单文件运行时解压失败 `mkdir ~/.cache/pkg: ENOENT`

dsh 的 bundled runtime 是 pkg 打包的单文件 exe，首次运行要解压到 `~/.cache/pkg`。
宿主 HOME 不可写时插件加载全部失败（报 `cordis:include` loader 错误）。
**同样用 HOME 重定向解决**。

### dsh API 请求不走代理（TRANSPORT 错误）

Node 24 的 fetch（undici）默认不读 `http_proxy`。driver 检测到代理环境变量时设置：

```bash
export NODE_USE_ENV_PROXY=1
```

### `DEEPSEEK_BASE_URL` 空串

空串会被 llm-deepseek 适配器当作显式 endpoint（请求打到空 URL）。不设置或设置真实值，
空值不要传。

## Daytona 平台

### `Total disk limit exceeded. Maximum allowed: 30GiB`

免费层磁盘配额。见 [overseas.md](overseas.md#免费层磁盘配额30gib)。

### 磁盘撞线时 upload 报 `filename must be an instance of str`

服务端在配额边缘对 upload 请求返回的误导性错误（不是本地 bug）。
清理残留沙箱（`client.list()` + `delete()`）后恢复。

### 沙箱卡在 CREATING 删不掉

harbor 进程被强杀时创建流程中断，沙箱停在 CREATING。等自动超时释放即可
（或等 `auto_delete_interval`）。

### `Failed to download logs`

agent 超时瞬间 Harbor 拉取沙箱日志失败，trajectory 可能缺失，不影响 verifier 评分。

## 监控 / 调试

- 面板看不到新 job：`JOB_OVERRIDE=jobs/<ts>` 指定；改代码后要按端口找 PID 杀干净再重启
  （`ss -tlnp | grep :PORT`），旧进程占端口时新进程静默失败。
- mock server 调试请求结构：见 [README](../README.md) Tools 一节。
