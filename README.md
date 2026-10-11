# codex-smart-router

用户选择 `smart-auto`（显示为“⭐ Auto”）后，服务根据任务、规则和允许读取的项目上下文，选择具体模型与 `reasoning.effort`。默认沿用 Codex 当前的 ChatGPT 登录，无需填写 `OPENAI_API_KEY`。

```text
Codex Desktop / CLI（当前 ChatGPT 登录）
  model: smart-auto（⭐ Auto）或原来的手动模型
       ↓ Codex 自动携带账号凭据
Smart Router :4000
       ↓ Auto 选型；手动选择原样转发
官方 Codex 后端 /backend-api/codex
  gpt-6.1-sol（主力）/ gpt-6-luna（非常简单的任务）
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
| 通用路由 | 按任务类别、复杂度、风险选档；未知项目和无项目任务也能使用；默认没有项目专属规则 |
| Task Classifier | 可选使用 Luna + 严格 JSON schema；超时、错误或不合格输出回退到确定性策略 |
| 项目识别 | 可选识别 Salesforce、React、Java、Python、Rust、Go、.NET；作为上下文，不按技术栈名称决定模型 |
| Git 改动 | 只读检查工作区的 staged、unstaged、untracked 路径，不执行 checkout、reset 或修改源项目 |
| 连续调用 | 根据 response id 或任务/会话标识保留原模型；状态缺失返回明确的 409 |
| 本地凭据 | 免费路由预览使用独立本地 token；账号调用透传 Codex 凭据；API 模式单独配置上游 key |
| 路由预览 | `POST /v1/route` 返回分类、项目摘要、选定模型、reasoning 和命中规则 |

默认分类器关闭，路由预览不调用模型。`rules.yaml` 默认 `rules: []`，不需要为每个项目维护关键词或文件后缀规则。路由按通用任务分析进入各分档；技术栈未知、没有 Git 或没有项目上下文时仍使用同一套策略。

当前配置保留本地选择的推理强度：普通任务使用 GPT-6.1 Sol / `high`，复杂及高风险任务用 Sol / `xhigh`，架构任务用 Sol / `max`。GPT-6 Luna / `xhigh` 只接明确的小型样式、文案、错别字、注释或文本转换任务；模型与 reasoning 可以分别在 YAML 调整。仅含 CSS、README 等词，或涉及完整文档、布局重构、功能开发时仍使用主力模型。

| 任务 | 模型 | Reasoning |
| --- | --- | --- |
| 修改访问权限校验 | gpt-6.1-sol | xhigh |
| 排查程序崩溃 / 增加 webhook 幂等性 | gpt-6.1-sol | xhigh |
| 设计系统架构 | gpt-6.1-sol | max |
| 修改按钮间距 | gpt-6-luna | xhigh |
| 新增查询接口 / 解释一段代码 | gpt-6.1-sol | high |
| 修正权限说明文档中的错别字 | gpt-6-luna | xhigh |
| 翻译一句短文本 / 格式化一小段 JSON | gpt-6-luna | xhigh |
| 研究部署方案 / 规划一次活动 | gpt-6.1-sol | high |
| README 增加完整使用说明 | gpt-6.1-sol | high |
| 重构全站 CSS 响应式布局 | gpt-6.1-sol | high |

当前账号模式的分析仍是通用关键词/任务线索和固定阈值的启发式分析，并未启用模型语义分析。它能跨项目使用，但不能保证识别所有表达或风险；不明确的任务保守使用 Sol。可选语义分类器目前仅支持 API 模式，默认关闭。

## 快速开始：使用当前 Codex 账号

按下面顺序完成首次配置。默认走当前 ChatGPT/Codex 账号，不需要 OpenAI API key，也不需要启动 LiteLLM。要求 Python 3.12+、uv，以及本机的 Codex Desktop / CLI；本机已在 Python 3.12.14、Codex CLI 0.162.1 上验证 CLI 接入。

### 1. 确认账号登录

```sh
codex login status
```

应看到 `Logged in using ChatGPT`。如果尚未登录，运行 `codex login` 并选择 ChatGPT 登录，完成后再次检查。账号需要有对应模型的使用权限。

### 2. 安装依赖并初始化本地配置

```sh
cd "/Users/Codex Auto /codex-smart-router"
uv --cache-dir .run/uv-cache sync --extra dev --frozen
.venv/bin/python scripts/init_env.py
```

`init_env.py` 创建权限为 `0600` 的 `.env`，默认设置 `SMART_ROUTER_AUTH_MODE=chatgpt`，生成路由预览 token 和备用 API 网关 token，并保留已有 `.env`。`OPENAI_API_KEY` 可以保持为空。初始化不会访问 OpenAI，也不会修改用户 Codex 配置。

如果之前使用过 API 模式，编辑项目的 `.env`，确认以下两项，再启动服务：

```dotenv
SMART_ROUTER_AUTH_MODE=chatgpt
SMART_ROUTER_CLASSIFIER_ENABLED=false
```

### 3. 启动 Router 并检查路由

在一个终端中运行，使用期间保持这个终端开启：

```sh
cd "/Users/Codex Auto /codex-smart-router"
.venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 4000 --workers 1
```

看到 Uvicorn 在 `http://127.0.0.1:4000` 启动后，另开终端检查服务和规则：

```sh
cd "/Users/Codex Auto /codex-smart-router"
curl --fail --silent --show-error http://127.0.0.1:4000/healthz
.venv/bin/python scripts/preview.py "修改访问权限校验"
.venv/bin/python scripts/preview.py "修改按钮间距"
```

预览输出的 `decision` 包含 `model`、`reasoning`、`source` 和可选的 `rule_id`。上面两个任务按当前配置分别选择 6.1 Sol / xhigh、6 Luna / xhigh。默认没有自定义硬规则，`source` 为 `heuristic`、`rule_id` 为空。默认关闭分类器，这些预览不会调用真实模型或消耗模型额度。

健康检查只报告服务与配置状态，不证明上游可用。预览使用 `.env` 中的本地 token；真正的账号模型调用由 Codex 发送 ChatGPT 凭据。

### 4. 将 provider 配置合并到 Codex

先备份已有配置。如果 `~/.codex/config.toml` 已存在，可运行：

```sh
cp "$HOME/.codex/config.toml" "$HOME/.codex/config.toml.before-smart-router-$(date +%Y%m%d-%H%M%S)"
```

编辑 `~/.codex/config.toml`，参考 [账号模式配置示例](config/codex.config.example.toml) 合并以下内容。保留原有默认模型、reasoning 和其他设置：

```toml
# 放在文件顶层、任何 [表名] 之前；已有 model_provider 时修改原行。
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

已有同名 provider 表时，编辑原表，避免重复定义。如果此前为 Router 的 API 模式设置过顶层 `model_catalog_json`，切回账号模式时移除该项，让 Codex 加载账号的动态模型目录。账号 provider 不设置 `env_key` 或 `auth.command`。

项目脚本不会自动修改这个文件。这一步修改的是你的 Codex 配置，会让使用该 provider 的对话依赖本地 Router；停止服务后，需要恢复原 provider 才能继续正常连接。

### 5. 在 Desktop 新建对话并选择 Auto

保持 Router 运行，保存 Codex 配置后重新启动 Codex Desktop，让它重新加载配置和模型目录。新建对话，在模型选择器中选择“⭐ Auto”（模型标识为 `smart-auto`），然后输入任务。

例如，输入“修改按钮间距”会选择 6 Luna / xhigh；输入“修改访问权限校验”会选择 6.1 Sol / xhigh。Auto 入口显示的 `medium` 只是目录入口设置，实际 reasoning 由 Router 决定。手动选择其他模型时，Router 保留你选择的模型和 reasoning。

可用第 3 步的预览命令查看任务的选型依据。真实请求的响应头也包含 `x-smart-router-model`、`x-smart-router-reasoning` 和 `x-smart-router-source`，供客户端或调试工具读取；项目没有增加桌面内的选型展示面板。相同对话中的连续工具调用保持原模型；希望另一项任务重新选型时，新建 Auto 对话。

当前已验证 CLI → Router → 官方账号模型的真实请求。Desktop 的菜单显示与具体任务执行仍需在完成上述配置后确认。

## 日常使用、停止与恢复

首次配置完成后，每次使用只需启动 Router，再打开 Codex 的 Auto 对话：

```sh
cd "/Users/Codex Auto /codex-smart-router"
.venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 4000 --workers 1
```

修改 `.env` 后需要重启 Router。服务只使用一个 worker，当前没有注册开机启动；电脑重启后需要重新运行启动命令。Router 重启会清空对话路由状态，应新建 Auto 对话。

停止服务：在运行 Router 的终端按 `Ctrl+C`。退出 Router 模式：将 `~/.codex/config.toml` 中的 `model_provider` 恢复为启用前的值；如果原来没有这一行，就移除新增的顶层 `model_provider`，然后重新启动 Codex。新增的 provider 表可以保留备用。

首次配置后没有做其他配置修改时，也可以从上面生成的备份恢复整个文件；已有后续修改时，只恢复 provider 相关设置，避免覆盖新设置。

## 使用当前账号

先运行 `codex login status`，确认当前登录方式是 ChatGPT。服务默认把请求转发至 `https://chatgpt.com/backend-api/codex`。不需要读取 `auth.json`、复制 access token 或设置 API key。

Codex 在请求中发送 `Authorization` 和可用的 `ChatGPT-Account-ID`；Router 原样传给官方后端。账号额度与限流响应头也返回给客户端。登录过期和账号权限由 Codex 与官方后端处理。

原生模型目录按凭据摘要、账号和查询参数隔离，在内存中缓存 5 分钟，最多 8 份；缓存不保存 token 或 HTTP 请求对象。Codex 的目录查询等待窗口较短，首次下载较慢时可以再次刷新。真实验证脚本最多重试两次只读目录查询，不重试模型推理。

## 可选：API key / LiteLLM 模式

这条路径是 `Codex → Smart Router :4000 → LiteLLM :4001 → OpenAI API`，使用 OpenAI Platform 的独立 API 计费。继续使用当前账号时，跳过本节即可。

先完成上面的依赖安装和 `.env` 初始化，再编辑项目的 `.env`：

```dotenv
SMART_ROUTER_AUTH_MODE=api
SMART_ROUTER_UPSTREAM_BASE_URL=http://127.0.0.1:4001/v1
OPENAI_API_KEY=你的本地OpenAI_API_key
```

保留初始化生成的 `SMART_ROUTER_API_KEY`、`SMART_ROUTER_UPSTREAM_API_KEY` 和 `LITELLM_MASTER_KEY`；其中后两项必须一致。将真实 key 填入本地文件，不要提交到代码仓库。

在终端一安装 gateway 依赖并启动 LiteLLM：

```sh
cd "/Users/Codex Auto /codex-smart-router"
uv --cache-dir .run/uv-cache sync --extra dev --extra gateway --frozen
uv --cache-dir .run/uv-cache run --no-sync --env-file .env \
  litellm --config config/litellm.yaml --host 127.0.0.1 --port 4001
```

真实 OpenAI key 在这条路径中只供 LiteLLM 使用。`--no-sync` 保留先前安装的 gateway 依赖。

在终端二启动或重启 Router，命令与账号模式相同：

```sh
cd "/Users/Codex Auto /codex-smart-router"
.venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 4000 --workers 1
```

备份并编辑 `~/.codex/config.toml`，合并 [API 模式配置示例](config/codex.api.config.example.toml)，将顶层 provider 改为 `smart-router-api`。示例中的绝对路径需要与本机项目位置一致。这个 provider 使用本地 Router token，通过 `auth.command` 读取 `.env`，不使用 ChatGPT 凭据。保存后重启 Codex，在新对话中使用 Auto。使用期间两个服务都需要运行，停止时分别按 `Ctrl+C`。

如需直连 OpenAI API，可将 `SMART_ROUTER_UPSTREAM_BASE_URL` 改为 `https://api.openai.com/v1`，并将 `SMART_ROUTER_UPSTREAM_API_KEY` 设为 API key，这时无需启动 LiteLLM。修改模式后重启 Router。

Router 默认禁用环境中的 HTTP proxy，并且不跟随上游重定向。非本地上游需要 HTTPS。如网络必须使用专用代理，应在 HTTP 客户端中明确配置并单独验证。

## 配置模型与路由规则

Codex 配置中的 `model = "smart-auto"` 或模型菜单中的 Auto 只负责选择 Router。实际调用哪个模型、使用什么 reasoning，由下面的项目文件控制：

| 文件 | 配置内容 | 何时需要修改 |
| --- | --- | --- |
| [router/rules.yaml](router/rules.yaml) | 默认模型、通用任务分档和可选自定义覆盖规则 | 改主力模型、Luna 使用范围；无需按项目配置 |
| [providers/models.yaml](providers/models.yaml) | 可用于 Auto 的模型及各自支持的 reasoning | 新增模型，或更新模型能力声明 |
| [.env.example](.env.example) → 本地 `.env` | 账号/API 模式、允许扫描的项目、可选分类器 | 改认证方式、项目上下文或分类器开关 |
| [config/litellm.yaml](config/litellm.yaml) | LiteLLM 模型别名与实际 OpenAI 模型的映射 | 仅在 API + LiteLLM 模式中新增或替换模型 |

### 默认模型与任务分档

`router/rules.yaml` 中的当前配置如下。编辑已有的 `default` 和 `tiers`，不要在文件中重复添加同名键。`rules: []` 表示不使用自定义覆盖规则。

```yaml
default:
  model: gpt-6.1-sol
  reasoning: high
tiers:
  high_risk:
    model: gpt-6.1-sol
    reasoning: xhigh
  architecture:
    model: gpt-6.1-sol
    reasoning: max
  complex:
    model: gpt-6.1-sol
    reasoning: xhigh
  simple:
    model: gpt-6-luna
    reasoning: xhigh
    enabled: true
    max_complexity: 1
    categories: [style, documentation, transformation]
rules: []
```

`default` 是普通或无法明确判断任务的主力；`high_risk` 用于高风险/安全任务，`architecture` 用于架构或复杂度 5 的任务，`complex` 用于复杂度 4 的任务。这些分档按上述顺序检查，剩余任务才考虑 `simple`，最后回到 `default`。复杂度是 Router 的 1–5 分估计，不代表经过验证的业务风险评级。

`simple` 默认仅接复杂度 1、低风险、分析建议 reasoning 不高于 `low` 的小改动或文本转换；执行时仍采用该分档配置的 reasoning，当前为 `xhigh`。启发式分析要求明确的小任务线索（如颜色、间距、错别字、注释、翻译、格式化），不超过 240 字符，且没有重构、响应式、多页面、布局、接口、功能、研究等扩大范围的线索。项目风险上升或分类器判断需要更复杂分析时，使用 Sol。分类器也不能把不满足这些小任务条件的请求降到 Luna。

希望所有任务都走主力模型时，将 `tiers.simple.enabled` 改为 `false`。希望调整主力任务的推理强度时，修改 `default.reasoning`；高风险和架构等分档仍使用各自的配置。硬规则中也有独立的 `model` 和 `reasoning`，修改默认值不会覆盖这些规则。

### 可选自定义覆盖规则

`rules` 是可选的手动覆盖规则。保持 `rules: []` 时，Router 使用通用分析与 `tiers` 分档，不需要给每个项目配置规则。如果只是调整普通任务、复杂任务的模型或 reasoning，或者关闭 Luna，直接修改 `default` 和 `tiers` 即可。

只有希望某类请求固定使用某个模型和 reasoning 时，才添加规则。例如，希望任务包含“深入分析”时固定使用 Sol / max，可以把 `router/rules.yaml` 中的：

```yaml
rules: []
```

替换为以下内容；保留原有 `default` 和 `tiers`，不要重复添加 `rules` 键：

```yaml
rules:
  - id: detailed-analysis
    priority: 100
    any_keywords:
      - 深入分析
      - detailed analysis
    model: gpt-6.1-sol
    reasoning: max
```

配置后，“深入分析这个程序为什么会卡死”会命中这条规则；“解释这个程序为什么会卡死”没有这些关键词，会继续按通用策略选型。关键词采用文本匹配，不会自动识别近义表达。

| 字段 | 含义 |
| --- | --- |
| `id` | 唯一规则名，也会出现在预览的 `decision.rule_id` 中 |
| `priority` | 数字越大越先匹配；建议不同规则使用不同值 |
| `any_keywords` | 任务文本中包含任意一项即匹配，英文不区分大小写，采用子串匹配 |
| `any_changed_suffixes` | Git 当前改动路径有任意一个后缀匹配，例如 `[.py]` |
| `project_risk` | 当前项目风险等于指定值时匹配，例如 `high` |
| `model` / `reasoning` | 命中规则后采用的模型与推理强度 |

每条规则至少需要一个匹配条件。同一条规则中的关键词、文件后缀和风险条件是“任一满足”关系。例如同时配置关键词和 `.py` 后缀，任一条件满足就会命中。路径和项目风险条件需要先允许项目扫描，并且客户端提供项目位置。

自定义硬规则按 `priority` 从大到小，使用第一条命中的规则。当前没有内置权限、Apex 或其他项目规则，通用高风险和复杂度判断进入 `tiers`。自定义规则可以覆盖分档，因此需要自行验证，避免让高风险任务命中简单模型。

保存后，停止并重新启动 Router，让它加载新规则，然后在另一个终端预览：

```sh
cd "/Users/Codex Auto /codex-smart-router"
.venv/bin/python scripts/preview.py "深入分析这个程序为什么会卡死"
```

检查输出的 `decision`，应包含以下字段（实际输出还包含 `reason`）：

```json
{
  "model": "gpt-6.1-sol",
  "reasoning": "max",
  "source": "rule",
  "rule_id": "detailed-analysis"
}
```

也可以预览“解释这个程序为什么会卡死”，确认没有命中这条规则。多条规则同时命中时，只采用优先级最高的第一条，不会合并其他规则；同优先级按文件中的排列顺序处理，建议使用不同优先级。准备实际执行任务时，在 Codex 新建 Auto 对话，避免继续使用旧对话已固定的路由。

### 模型能力与上游映射

当前 `providers/models.yaml` 为：

```yaml
models:
  gpt-6.1-sol:
    supported_reasoning: [low, medium, high, xhigh, max]
  gpt-6-luna:
    supported_reasoning: [none, low, medium, high, xhigh, max]
```

这些能力按 [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol) 和 [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) 的官方文档配置；6.1 Sol 不支持 `none`。Router 启动时校验默认值、所有分档和硬规则，模型未注册或 reasoning 不受支持会导致启动失败，而不是发送错误组合给上游。能力声明不会授予账号的模型权限。

新增模型时，先在这个文件登记准确模型名和 reasoning，再修改 `rules.yaml` 中需要使用它的默认值、分档或规则。账号模式要求官方账号后端支持该模型；API + LiteLLM 模式还需在 `config/litellm.yaml` 中增加同名 `model_name`，并将 `litellm_params.model` 指向对应的 `openai/模型名`。仅将模型加入 `models.yaml` 不会改变现有路由。

可选分类器的模型由 `.env` 中的 `SMART_ROUTER_CLASSIFIER_MODEL=gpt-6-luna` 控制，和执行任务的 `simple` 模型独立。分类器默认关闭，目前仅支持 API 模式；启用后会增加一次分类调用。

### 修改后的验证与生效

修改 `rules.yaml`、`models.yaml` 或 `.env` 后，停止并重新启动 Router，让它重新加载配置；修改 LiteLLM 映射时，也重启 LiteLLM。然后在另一终端检查：

```sh
cd "/Users/Codex Auto /codex-smart-router"
.venv/bin/python scripts/preview.py "新增查询接口"
.venv/bin/python scripts/preview.py "修改按钮颜色"
.venv/bin/python scripts/preview.py "修改访问权限校验"
.venv/bin/python scripts/preview.py "README 增加完整使用说明"
```

按当前配置结果依次为 Sol / high、Luna / xhigh、Sol / xhigh、Sol / high。检查 `analysis` 中的类别、复杂度、风险，以及 `decision` 中的模型、reasoning 和来源；未配置覆盖规则时 `rule_id` 为空。分类器关闭时，预览不调用真实模型。

重启会清空连续调用的路由状态，修改后在 Codex 新建 Auto 对话。修改主力模型后，账号模型目录会使用该默认模型的原生基础指令；如果还要更新 API 模式的静态备用指令，运行 `scripts/refresh_catalog.py --template-model 对应模型名`，然后让 Codex 重新加载模型目录。

## 模型目录与账号配置说明

账号模式不设置 `env_key` 或 `auth.command`，由 Codex 使用当前登录。显式 `model_catalog_url` 让当前版本从网关加载带 Auto 的目录。不要设置静态 `model_catalog_json`，否则会覆盖账号的动态模型列表。`name = "OpenAI"` 和原生 `/backend-api/codex` 路径保留当前版本的账号连接行为；实际本地服务仍是 Smart Router。

模型目录的显示名是“⭐ Auto”，它仅广告一个 `medium` 选择入口；Router 会按策略设置真正的 reasoning。实际选型可从 `x-smart-router-model`、`x-smart-router-reasoning`、`x-smart-router-source` 响应头或路由预览读取。上游响应内的真实模型名称不被伪装成 Auto。

Auto 尽可能采用账号目录中 GPT-6.1 Sol 的基础指令；静态备用目录也保留安装包中的指令。升级 Codex 后，可重新生成备用指令并验证兼容性：

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

项目技术栈只作为上下文标签；未知技术栈返回空列表也能正常路由。改动范围使用相同阈值：少于 10 个路径为低风险摘要，10–19 个为中风险，20 个及以上为高风险；涉及 `auth/`、`authorization`、`permissions/`、`security/`、`migrations/`、`schema/` 等路径线索时提高风险。`.cls`、`.trigger`、`.py` 等源文件后缀本身不会提高风险。这里是保守的改动摘要，没有读取代码内容或分析调用链，不能证明真实业务风险。

账号模式默认使用通用启发式分析与任务分档。可选模型分类器目前仅支持 API 模式：设置 `SMART_ROUTER_CLASSIFIER_ENABLED=true` 后，未命中自定义规则且本地未识别为高风险的任务可增加一次 Luna 分类调用，包括路由预览。分类器针对通用编码与非编码任务，接收任务文本、技术栈、变化数量和风险摘要；不要求特定项目。分类结果不能降低本地已识别的风险，也不能把缺少明确小任务线索的请求降到 Luna。分类器默认超时 8 秒，不自动重试；失败时回到通用启发式策略。

## 常见问题

| 现象 | 检查与处理 |
| --- | --- |
| 提示连接失败，或无法连接 `127.0.0.1:4000` | 检查 Router 终端是否仍在运行，并请求 `/healthz`。确认 Codex 的 `base_url` 与实际监听端口一致。 |
| 启动时提示端口被占用 | 检查是否已有 Router 在运行；先停止旧实例再启动。更换端口时，需要同步修改 Codex 配置和预览脚本中的地址。 |
| 模型列表没有“⭐ Auto” | 检查顶层 `model_provider`、`model_catalog_url`，确认账号模式没有静态 `model_catalog_json` 覆盖目录。保持 Router 运行后重新启动 Codex；首次目录下载超时可以再次重新加载。 |
| Codex 提示 TOML 配置错误 | 检查是否重复添加同名字段或 provider 表，顶层设置是否误放到了其他表下面。按配置示例修改已有项。 |
| 账号模式返回 401 | 运行 `codex login status` 检查登录；必要时重新执行 `codex login`，确认使用 ChatGPT 登录。不要将本地预览 token 填成账号凭据。 |
| 官方返回模型权限错误或 429 | 检查当前账号的模型权限或额度，并按官方返回的等待时间处理。Router 会保留这些错误，不会自动换模型重试。 |
| 返回 `routing_state_missing` / HTTP 409 | Router 重启、缓存过期或旧对话缺少路由状态时可能出现。新建 Auto 对话，让 Router 重新选择模型。 |
| 预览中的项目扫描显示 `disabled` | 默认不扫描本地项目。按“项目上下文与分类器”配置 `SMART_ROUTER_PROJECT_ROOTS`，重启 Router，并在预览中传入 `--project-root`。 |
| API 模式无法连接 `127.0.0.1:4001` | 确认 LiteLLM 已在另一个终端启动；账号模式无需运行这个服务。 |
| API 模式中 LiteLLM 返回认证错误 | 检查本地 `.env` 中 `SMART_ROUTER_UPSTREAM_API_KEY` 与 `LITELLM_MASTER_KEY` 是否一致，以及 `OPENAI_API_KEY` 是否有效；修改后重启相应服务。 |

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

当前通用路由通过 92 项本地测试和 Ruff 检查，覆盖跨技术栈相同选型、无项目任务、普通文本转换、风险保底、不同文件后缀与改动范围，以及原有认证、数据透传、流式响应和连续调用行为。

此前模型更新时，曾使用真实 Codex CLI、Router、LiteLLM Proxy 与本地模拟模型服务验证工具调用往返，两次调用均保持 `gpt-6.1-sol` / `high`；未调用真实模型或产生 API 费用。报告见 `.run/model-update-smoke-litellm.json`。这个历史报告不是当前通用路由的端到端验收。

此前账号模式曾以旧配置 `gpt-5.6-sol` / `medium` 完成一次真实联调，官方返回 `completed`，Codex 收到预期标记。这项历史结果不能证明新模型已完成真实调用。此次新模型的账号目录查询因自动审批系统额度限制未执行；6.1 Sol 和 6 Luna 的当前账号可用性、真实模型工具调用、compact 和 Desktop 菜单仍需单独验收，桌面配置尚未正式合并。

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
- [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)、[GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)：当前模型标识与支持的 reasoning。
