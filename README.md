# codex-smart-router

用户选择 `smart-auto`（显示为“⭐ Auto”）后，服务根据任务、规则和允许读取的项目上下文，选择具体模型与 `reasoning.effort`。默认沿用 Codex 当前的 ChatGPT 登录，无需填写 `OPENAI_API_KEY`。

```text
Codex Desktop / CLI（当前 ChatGPT 登录）
  model: smart-auto（⭐ Auto）或原来的手动模型
       ↓ Codex 自动携带账号凭据
Smart Router :4000
       ↓ Auto 选型；手动选择原样转发
官方 Codex 后端 /backend-api/codex
  gpt-6-astra / gpt-5.6-sol / gpt-5.6-terra
```

本项目不修改桌面程序代码。自定义 provider 把连接指向本地网关；网关在账号的原生模型目录中追加 Auto，保留其他手动模型。需要正式合并 Codex 配置后，桌面端才能使用该入口。可用模型、额度和权限仍由当前账号与官方后端决定。

`OPENAI_API_KEY` 只用于可选的 API 模式，走 OpenAI Platform 的独立计费。默认账号模式不经过 LiteLLM。

## 已实现的 MVP

| 功能 | 行为 |
| --- | --- |
| Responses 转发 | 保留输入消息、图片引用、工具 schema、工具结果、instructions、加密 reasoning 和其他请求字段；替换模型与 reasoning effort |
| 当前账号 | `requires_openai_auth=true`，由 Codex 自己发送登录凭据、处理刷新；Router 不读取或保存登录文件 |
| 原生模型目录 | 从账号后端取得模型列表，追加 Auto；手动选择模型时保留原模型和 reasoning |
| 请求压缩 | 支持 Codex 的 zstd 请求体；编码前后均限制大小 |
| 流式响应 | 保留 SSE 事件内容，观察 response id 以固定连续调用的模型 |
| 规则引擎 | 授权/权限、Apex Trigger、支付回调/幂等、迁移和高风险 Git 改动优先匹配规则 |
| Task Classifier | 可选使用 Terra + 严格 JSON schema；超时、错误或不合格输出回退到确定性策略 |
| 项目识别 | Salesforce、React、Java；只有构建描述文件包含 Spring Boot 依赖或插件时才标记 Spring Boot |
| Git 改动 | 只读检查工作区的 staged、unstaged、untracked 路径，不执行 checkout、reset 或修改源项目 |
| 连续调用 | 根据 response id 或任务/会话标识保留原模型；状态缺失返回明确的 409 |
| 本地凭据 | 免费路由预览使用独立本地 token；账号调用透传 Codex 凭据；API 模式单独配置上游 key |
| 路由预览 | `POST /v1/route` 返回分类、项目摘要、选定模型、reasoning 和命中规则 |

默认分类器关闭，路由预览不调用模型。下面是已覆盖的代表性行为；新增业务规则应修改 `router/rules.yaml` 并用实际任务验证。

| 任务 | 模型 | Reasoning |
| --- | --- | --- |
| 修改 Salesforce Authorization Chain | gpt-6-astra | high |
| 优化医院授权模式校验 | gpt-6-astra | high |
| 修改 Apex Trigger / Stripe webhook 幂等性 | gpt-6-astra | high |
| 设计系统架构 | gpt-6-astra | xhigh |
| 修改 LWC CSS 按钮间距 | gpt-5.6-terra | low |
| 修改付款成功页面 | gpt-5.6-terra | medium |
| 增加普通页面筛选功能 | gpt-5.6-sol | medium |

## 安装与本地路由预览

要求 Python 3.12+、uv；实际 Codex 接入还需要本机安装 Codex。已在 Python 3.12.14、Codex CLI 0.162.1 上验证。

```sh
cd "/Users/Codex Auto /codex-smart-router"
uv --cache-dir .run/uv-cache sync --extra dev --frozen
.venv/bin/python scripts/init_env.py
.venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 4000 --workers 1
```

`init_env.py` 创建权限为 `0600` 的 `.env`，默认设置 `SMART_ROUTER_AUTH_MODE=chatgpt`，生成路由预览 token 和备用 API 网关 token，并保留已有 `.env`。`OPENAI_API_KEY` 可以保持为空。初始化不会访问 OpenAI，也不会修改用户 Codex 配置。

另开终端预览规则：

```sh
cd "/Users/Codex Auto /codex-smart-router"
.venv/bin/python scripts/preview.py "修改 Salesforce Authorization Chain"
.venv/bin/python scripts/preview.py "修改 LWC CSS 按钮间距"
```

健康检查 `GET /healthz` 只报告服务与配置状态，不证明上游可用。`/v1/route` 预览使用本地 token；账号模式下的模型调用和原生模型列表使用 Codex 发送的 ChatGPT bearer token。

## 使用当前账号

先运行 `codex login status`，确认当前登录方式是 ChatGPT。服务默认把请求转发至 `https://chatgpt.com/backend-api/codex`。不需要读取 `auth.json`、复制 access token 或设置 API key。

Codex 在请求中发送 `Authorization` 和可用的 `ChatGPT-Account-ID`；Router 原样传给官方后端。账号额度与限流响应头也返回给客户端。登录过期和账号权限由 Codex 与官方后端处理。

原生模型目录按凭据摘要、账号和查询参数隔离，在内存中缓存 5 分钟，最多 8 份；缓存不保存 token 或 HTTP 请求对象。Codex 的目录查询等待窗口较短，首次下载较慢时可以再次刷新。真实验证脚本最多重试两次只读目录查询，不重试模型推理。

## 可选：API key / LiteLLM 模式

只有需要 OpenAI Platform 的独立 API 计费时，才改为 `SMART_ROUTER_AUTH_MODE=api`，填写 `OPENAI_API_KEY` 并安装、启动 LiteLLM：

```sh
cd "/Users/Codex Auto /codex-smart-router"
uv --cache-dir .run/uv-cache sync --extra dev --extra gateway --frozen
uv --cache-dir .run/uv-cache run --no-sync --env-file .env \
  litellm --config config/litellm.yaml --host 127.0.0.1 --port 4001
```

`SMART_ROUTER_UPSTREAM_API_KEY` 与 `LITELLM_MASTER_KEY` 必须一致；初始化脚本已设置这一关系。真实 OpenAI key 只供 LiteLLM 使用。`--no-sync` 保留先前安装的 gateway 依赖。

API 模式的 Codex 示例是 `config/codex.api.config.example.toml`，使用本地 Router token，不使用 ChatGPT 凭据。如需直连 OpenAI API，可将 `SMART_ROUTER_UPSTREAM_BASE_URL` 改为 `https://api.openai.com/v1`，并将 `SMART_ROUTER_UPSTREAM_API_KEY` 设为 API key。修改模式后重启 Router。

Router 默认禁用环境中的 HTTP proxy，并且不跟随上游重定向。非本地上游需要 HTTPS。如网络必须使用专用代理，应在 HTTP 客户端中明确配置并单独验证。

## Codex 配置

先审查 `config/codex.config.example.toml`。项目没有自动写入 `~/.codex/config.toml`；正式启用时需要合并这些字段，保存原有配置备份，并重新加载 Codex。

账号模式的关键字段如下。保留原有默认模型和 reasoning 设置；配置加载后，在新对话中选择“⭐ Auto”启用自动选型：

```toml
model_provider = "smart-router"

[model_providers.smart-router]
name = "OpenAI"
base_url = "http://127.0.0.1:4000/backend-api/codex"
model_catalog_url = "http://127.0.0.1:4000/backend-api/codex/models"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
```

账号模式不设置 `env_key` 或 `auth.command`，由 Codex 使用当前登录。显式 `model_catalog_url` 让当前版本从网关加载带 Auto 的目录。不要设置静态 `model_catalog_json`，否则会覆盖账号的动态模型列表。`name = "OpenAI"` 和原生 `/backend-api/codex` 路径保留当前版本的账号连接行为；实际本地服务仍是 Smart Router。

模型目录的显示名是“⭐ Auto”，它仅广告一个 `medium` 选择入口；Router 会按策略设置真正的 reasoning。实际选型可从 `x-smart-router-model`、`x-smart-router-reasoning`、`x-smart-router-source` 响应头或路由预览读取。上游响应内的真实模型名称不被伪装成 Auto。

Auto 尽可能采用账号目录中 GPT-5.6 Sol 的基础指令；静态备用目录也保留安装包中的指令。升级 Codex 后，可重新生成备用指令并验证兼容性：

```sh
.venv/bin/python scripts/refresh_catalog.py
```

重新生成只修改项目内的模型目录。模型目录格式可能随 Codex 版本变化；当前版本已通过实际加载与 CLI 调用验证。

## 项目上下文与分类器

默认 `SMART_ROUTER_PROJECT_ROOTS=[]`，不扫描任何本地项目。需要项目上下文时，在 `.env` 中列出允许读取的根目录，例如：

```dotenv
SMART_ROUTER_PROJECT_ROOTS=["/absolute/path/to/project"]
SMART_ROUTER_CLASSIFIER_ENABLED=false
```

目录必须真实位于允许的根目录内；路径穿越和符号链接逃逸会被拒绝。扫描排除 `.git`、`node_modules`、虚拟环境、构建目录，默认最多检查 5,000 个文件名。只读取小型项目描述文件，不将项目源码上传给分类器。

Codex 请求中的 `<environment_context><cwd>…</cwd></environment_context>` 可提供项目位置；其他客户端也可传 `metadata.smart_router_project_root`。这个 Router 专用 metadata 会在转发前移除。

```sh
.venv/bin/python scripts/preview.py "修改 LWC 样式" \
  --project-root "/absolute/path/to/project"
```

`files_changed` 表示 Git 当前工作区变化的路径数量，不是预估未来会修改的文件数。扫描或 Git 检查不完整时会明确返回状态。Java 构建文件不自动等同于 Spring Boot，普通 `package.json` 也不自动等同于 React。

账号模式默认使用规则与启发式分类。可选模型分类器目前仅支持 API 模式：设置 `SMART_ROUTER_CLASSIFIER_ENABLED=true` 后，未命中硬规则的任务可增加一次 Terra 分类调用，包括路由预览。分类器只接收任务文本、技术栈、变化数量和风险摘要；模型调用仍由规则与允许的 reasoning 范围约束。分类器默认超时 8 秒，不自动重试。

## 验证

```sh
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python scripts/smoke_codex.py
.venv/bin/python scripts/smoke_codex.py --account-auth \
  --report .run/smoke-account-report.json
.venv/bin/python scripts/smoke_codex.py --with-litellm \
  --report .run/smoke-litellm-report.json
```

前两类检查验证路由行为、权限边界、数据透传、错误处理与格式。smoke 脚本启动随机 loopback 端口的模拟 Responses 服务和真实 Router，调用已安装的 Codex CLI，让它执行一次只读 `pwd`，然后通过流式响应收到固定标记。

`--account-auth` 使用 Codex 当前登录向本地模拟上游验证凭据透传和原生模型目录，不读取或保存登录凭据。`--with-litellm` 再加入实际 LiteLLM Proxy，推理上游仍是本地模拟服务。这些测试不调用真实模型、不写用户 Codex 配置，退出时停止测试服务。报告位于 `.run/`；它们不证明真实模型权限或桌面端显示。

另有真实账号验证脚本。默认只查询官方模型目录，带 `--invoke` 时发送一次很小的真实请求，使用当前账号的 Codex 额度：

```sh
.venv/bin/python scripts/verify_account.py
.venv/bin/python scripts/verify_account.py --invoke
```

脚本临时启动本地 Router，让 Codex 自己管理账号认证，不改变全局配置，退出时停止服务。报告只记录目录状态、路由、返回模型与用量，不记录凭据或输入历史；保存在 `.run/live-account-report.json`。真实账号请求完成也不等于桌面模型菜单已经验收。

本机已完成一次当前账号的真实联调：Auto 选择 `gpt-5.6-sol` / `medium`，官方返回 `completed`，Codex 收到预期标记。60 项单元与协议测试通过；当前登录的原生模型目录包含三个路由目标。真实模型工具调用和 compact 尚未验收，桌面配置尚未正式合并。

## MVP 边界

- 路由依据规则和可选分类器，没有针对业务任务训练或评估过准确率，不应把分类分数当成业务风险保证。
- 连续工具调用和携带旧加密 reasoning 的对话保持原模型；完整历史中的后续用户消息也可能继续原路由。希望新任务重新选型时，开始新的 Auto 对话。
- 路由状态保存在单进程内，默认 2 小时 TTL、最多 4,096 条；Router 只启动一个 worker。重启、过期或淘汰后，无法识别的继续调用返回 `routing_state_missing` / HTTP 409，需要新对话。
- 没有自动升级、失败后换模型、跨 provider 加密 reasoning 转换、持久化用户 Memory，也没有部署或注册开机服务。
- 上游 401/429/5xx 保留原错误和 Retry-After；中途流断开会发送错误事件，Router 不重放任务。
- 支持完整 Responses、SSE、模型目录和上下文 compact 路由；真实 compact 需单独验收。MVP 不支持 WebSocket、私有 Responses Lite、搜索等额外 Codex 端点；模型目录关闭 Lite 传输。

## 依据

- [Codex Custom Providers](https://learn.chatgpt.com/docs/config-file/config-advanced)：provider、认证与连接配置。
- [Sign in with ChatGPT through a gateway](https://learn.chatgpt.com/docs/enterprise/sign-in-with-chatgpt-through-a-gateway)：账号凭据经过网关，转发到 Codex 后端。
- [Codex Authentication](https://learn.chatgpt.com/docs/auth)：ChatGPT 订阅登录与 API key 计费的区别。
- [Codex Configuration Reference](https://learn.chatgpt.com/docs/config-file/config-reference)：`model_catalog_json`、Responses、WebSocket 与凭据命令。
- [LiteLLM Responses API](https://docs.litellm.ai/docs/response_api)：代理 Responses 和 SSE。
- [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)、[GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol)、[GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra)：当前模型标识与支持的 reasoning。
