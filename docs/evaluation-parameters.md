# DSH 0.1.0rc6 评测参数与验证边界

本项目使用 Harbor 0.25.0、deepseek-harness-sdk/runtime-bin 0.1.0rc6，
模型是千帆托管的 `deepseek-v4.1-flash-1017`。以下是本适配器的行为，
不代表复现 DeepSeek 原版模型的 90.6% 成绩。

更新：可选的外部受限代理已实现，可在不修改 SDK 的情况下补充 top_p。
以下“未发送”描述仍适用于 SDK 直连；代理模式与未完成的 Linux 隔离验收见
[model-proxy.md](model-proxy.md)。

## 参数

| 项目 | 当前行为 |
| --- | --- |
| `thinking.type` | Cordis 的 `thinking` 配置经 SDK serializer 发送；默认 enabled |
| `reasoning_effort` | 现有外部插件通过 `agent/request` 传递；Harbor 默认 max |
| `temperature` | 现有外部插件通过 `agent/request` 传递；Harbor 默认 1.0 |
| `top_p` | SDK 直连未发送；可选外部代理补充 0.95，本地真实 SDK 代理链路已验证 |
| `DSH_CONTEXT_WINDOW` | 默认 1,000,000，声明给 DSH 模型目录的上下文容量元数据 |
| `DSH_MAX_TOKENS` | 原有默认 49,152，单次输出预算；不等于上下文容量 |
| `DSH_MAX_STEPS` | 默认 500，由外部插件通过官方 pre-step 接口限制每个 turn |

本次直接读取已安装 macOS runtime 的 `__pkg_manifest__` 和 `__pkg_archive__`，
只读提取对应模块，未修改二进制或安装包。确认：

- `dsh-llm-deepseek/lib/index.js` 中的 `serializeRequest` 显式枚举字段，
  没有 `top_p` 或通用 extra-body 透传；URL 是 `baseURL + /chat/completions`。
- `dsh-agent-loop/lib/index.js` 的配置没有 `maxSteps`；循环在 `step/start`
  之前调用 `agent/pre-step`，`reject` 会产生 `turn/end: blocked`。
- `dsh-sdk-jsonrpc-server/lib/index.js` 初始化支持 maxTokens，没有最大步数字段。

## 500 步的语义

一次 step 是一次 Agent 循环，包括模型生成及由其触发的工具执行。
同一步的 HTTP 重试不增加 step；一步可执行多个工具；最终文本回答也占一步。
`effort-plugin.mjs` 在拟进入的 `payload.step > DSH_MAX_STEPS` 时返回 `reject`，
所以 500 步完整执行后不会进入第 501 步。不是运行后再数日志或强杀进程。

这属于**本项目的外部评测策略**，利用官方扩展接口实现，并非 SDK 原生的
`maxSteps=500` 配置，也未经证明与原厂评测的计数口径完全相同。
范围是每个 turn；当前 driver 每个试验只创建新会话并提交一次任务，
不续聊、不恢复。若以后引入多 turn/恢复，必须重新定义并验证试验总步数。

结果记录 `steps_started`、`step_limit_reached` 和 `evaluation`。
达到限制时 `finish_reason=blocked`，不是 completed。
原有 `ok` 表示 SDK 调用返回，**不是任务通过或模型成功的充分条件**；
正式评分必须由 Harbor verifier 提供，不能以 `ok=true` 判定通过。
现有 driver 对 SDK 返回的 error 仍可能写入 `ok=true`，这是另一个待修问题。

## 上下文与 top-p 的限制

`contextWindow` 是 Harness 元数据，不是发给 HTTP API 的参数。
当前 Minimal 没有上下文压缩，也没有本项目实现的精确 token 截断器；
设置 1M 不证明本地强制执行 1M 上限，更不证明千帆实际接受 1M 输入。
千帆上下文容量、单次输出上限及超限错误，需要服务端文档和验收确认。
思维模式下采样参数可能被服务端忽略；HTTP 捕获只能证明发送情况。

没有添加无效的 `DSH_TOP_P`，没有修改官方 serializer，也没有全局
替换 fetch。可选受限兼容代理已实现并测试 top_p、SSE、断流、错误、认证和预算；
尚未在阿里云 Docker 部署验收。部署步骤与限制见 [model-proxy.md](model-proxy.md)。

## 安全与 Minimal 差异

- 本地虚假 Key 探针确认：普通 Bash 环境中没有直接继承
  `DEEPSEEK_API_KEY`。runtime 的 subprocess 层会清理子进程凭证环境。
- 但 driver 和 runtime 仍在任务容器中持有 Key。同用户或 root 的 Linux
  进程是否可读 `/proc/<pid>/environ`，取决于 UID、capabilities 和 procfs
  策略，尚未验证。环境清理不能视为进程/文件隔离。
- `danger-full-access` 的 Bash 启动路径不调用 sandbox.confine；编辑器使用
  本地文件系统。现有代码没有实施防火墙或网络命名空间限制。
  Bash 描述中的“没有互联网”不是网络策略。
- 与当前架构兼容的后续方案：真实 Key 留在容器外固定上游的认证代理，
  任务仅拿短期受限标识；Docker 网络禁止直接公网，仅允许代理及明确允许的
  内网镜像源。代理必须禁止任意 URL/CONNECT/重定向转发，限制模型和额度。
  任务 shell 仍能访问该代理，这是需要报告的例外；若要求 shell 连模型代理
  也不可达，需要进一步分离执行身份或模型进程，不在本次实现。
- 当前保留 persistent Bash 与 str_replace_editor 两个模型工具，单一 persona，
  关闭 identity/runtime/workspace/skills 注入，未整体替换配置。
  工具描述、输出截断、超时及新增步数策略属于本项目配置；未核验原厂完整
  TB2.1 配置，不能称为官方完全一致。三次独立试验及网络隔离仍由后续部署验收。

## 本地验证

```bash
DSH_TEST_ARTIFACT_DIR=/tmp/dsh-evaluation-validation \
  .venv/bin/python -m unittest discover -s tests -v
```

测试使用真实 SDK/runtime，只有 HTTP 服务返回 Mock 响应。子进程环境显式
构造且只包含虚假凭证，不继承真实 Key 或代理。请求证据不保存认证请求头。
覆盖文本请求、实际 500 步工具循环、503 重试不计新步、Bash 凭证环境清理、
非法步数配置，以及真实 Harbor 基类搭配 Fake Environment 的安装/运行/结果处理。
Fake Environment 不安装软件、不启动 Docker，也不证明 Linux verifier 链路。
本次没有原有测试套件可复跑，新增测试不代替 Linux 单题验收。
