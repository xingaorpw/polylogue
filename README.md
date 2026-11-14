# polylogue

[![CI](https://github.com/xingaorpw/polylogue/actions/workflows/ci.yml/badge.svg)](https://github.com/xingaorpw/polylogue/actions/workflows/ci.yml)
![tests](https://img.shields.io/badge/tests-103%20passing-brightgreen)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![deps](https://img.shields.io/badge/dependencies-0-success)
![license](https://img.shields.io/badge/license-MIT-green)

让多个 AI 在一个房间里会商：能同时问，也能让它们互相点名接话，带成本硬顶和收敛检测。

> GPT、Claude、Gemini、Grok、DeepSeek 各说各的，你得在五个标签页之间复制粘贴。更麻烦的是**它们之间不通**——Gemini 挑出的漏洞，没法让 Grok 接着反驳。
>
> polylogue 把它们放进一个房间。你 `@critic`，它回答完可以 `@builder` 让另一个接着做。你能看见每一步，也能随时叫停。

---

## 安装

```bash
pip install -e .
```

零依赖，只用标准库。

---

## 用法

### 命令行

```bash
# 同时问三个模型
export GEMINI_API_KEY=...  XAI_API_KEY=...  DEEPSEEK_API_KEY=...
polylogue "这个方案有什么问题？" --models gemini,grok,deepseek

# 只点名一个，让它自己交接
polylogue "@critic 先说风险" --models critic,builder --roles critic=critic,builder=builder

# 交互模式：一直聊下去
polylogue --models gemini,claude,grok

# 本地 Web 界面
polylogue --web --models gemini,claude,grok
```

真实运行输出（用一个假的上游服务代替真实 API，这样输出可复现、不花钱）：

```
$ polylogue "@critic 我们要做多人同时编辑的文档服务，先说这个方案最大的风险。" \
    --models critic,builder --roles critic=critic,builder=builder \
    --max-cost 1.0

房间成员：critic, builder
价格表版本：2026-11（估算，以各家官网为准）

[critic] 方案的问题是并发写入会丢数据，@builder 你的实现怎么保证顺序？

[builder] 用单写者队列保证顺序，代价是吞吐降到三分之一。

— 1832 tokens · $0.0032 · 没有模型继续接话，等你发言
```

注意 `critic` 结尾写了 `@builder`，所以 `builder` 自动接了话。没人再点名时对话停下来等你，不会自己转下去。

### 工作台

```bash
polylogue --web --models gemini,claude,grok
# polylogue 工作台 → http://127.0.0.1:8765
```

三栏工作台，不是单页聊天：

```
┌──────────────────────────────────────────────────────────────┐
│ polylogue        并发方案评审 · gemini / claude / grok        │
│                                           [导出]  [停止]      │
├──────────┬───────────────────────────────┬───────────────────┤
│ 房间     │  [用户] 并发写入怎么保证顺序   │ 花费              │
│          │                               │  tokens   18,204  │
│ ▸ 默认   │  [critic] 会丢数据，@builder  │  成本   $0.0312   │
│ ▸ 评审   │                               │                   │
│ ▸ 调研   │  [builder] 用单写者队列，代价  │ 每个模型          │
│          │            是吞吐降三分之一    │  critic   9,120   │
│ ＋       │                               │  builder  9,084   │
│          ├───────────────────────────────┤                   │
│          │ @gemini @claude @grok         │ 说明              │
│          │ [说点什么…            ] [发送] │ 价格表 2026-11    │
└──────────┴───────────────────────────────┴───────────────────┘
```

- **左栏**：房间列表。可以开多个房间（比如「方案评审」「调研」「写文档」），互不干扰，各自有自己的成员、预算和转录。
- **中栏**：对话。成员 chip 点一下就插入 `@名字`；会商中「停止」按钮可用，停在轮次边界，不会打断正在飞的请求。
- **右栏**：实时花费，按模型拆开。有模型查不到价格时会明确标「未知」，不假装是 0。

界面是单个 HTML 文件，标准库 `http.server` + SSE 推送，**没有任何前端构建步骤，不加载任何外部资源**，断网也能开。模型回复边生成边推到页面上。

工作台的 HTTP 接口也可以直接用：

```
GET    /api/info                   provider 列表（含哪些配了 key）
GET    /api/rooms                  房间列表
POST   /api/rooms                  建房间 {name, members, max_rounds, max_cost_usd}
DELETE /api/rooms/{id}             删房间
POST   /api/rooms/{id}/say         发言，SSE 流式返回
POST   /api/rooms/{id}/stop        手动停止
GET    /api/rooms/{id}             完整转录
GET    /api/rooms/{id}/stats       按模型的 token / 花费
GET    /api/rooms/{id}/export      Markdown 导出
```

### 支持的模型

```
$ polylogue --list-providers
名字           适配器        环境变量                   默认模型
------------------------------------------------------------------------------
openai       openai     OPENAI_API_KEY         gpt-4o
claude       anthropic  ANTHROPIC_API_KEY      claude-sonnet-4-20250514
gemini       gemini     GEMINI_API_KEY         gemini-2.5-flash
grok         openai     XAI_API_KEY            grok-3
deepseek     openai     DEEPSEEK_API_KEY       deepseek-chat
qwen         openai     DASHSCOPE_API_KEY      qwen-plus
kimi         openai     MOONSHOT_API_KEY       moonshot-v1-32k
glm          openai     ZHIPU_API_KEY          glm-4-plus
groq         openai     GROQ_API_KEY           llama-3.3-70b-versatile
openrouter   openai     OPENROUTER_API_KEY     anthropic/claude-sonnet-4
mistral      openai     MISTRAL_API_KEY        mistral-large-latest
together     openai     TOGETHER_API_KEY       meta-llama/Llama-3.3-70B-Instruct-Turbo
ollama       openai     （不需要）                  llama3.2
lmstudio     openai     （不需要）                  local-model
```

三个适配器覆盖全部：

- **`openai`** —— OpenAI 兼容协议。绝大多数厂商都提供，包括 Grok、DeepSeek、通义、Kimi、智谱、Groq、OpenRouter、Ollama。
- **`anthropic`** —— Claude 原生 Messages API（`max_tokens` 必填，system 是顶层字段，和 OpenAI 不一样）。
- **`gemini`** —— Google 原生 `generateContent`（`assistant` 角色要映射成 `model`）。

> 注意 **`grok`** 和 **`groq`** 是两个不同的东西：前者是 xAI 的模型，后者是跑 Llama 的推理托管商。别名表里分开处理。

任何 OpenAI 兼容的端点都能接：

```bash
polylogue "..." --models local --provider local=http://localhost:8000/v1
```

---

## 它凭什么比单开一个模型好

这个项目的前提不是「连通」（litellm 一行就能做），而是**让多模型讨论产出比单模型更好的东西**。为此有三个设计决定：

### 1. 模型只被点名时才说话

不这么做的话，4 个模型 5 轮 = 20 次调用，每次带全量历史，成本指数上涨，而且它们会互相附和。

```
你:     @critic 先说风险
critic: 并发写入会丢数据，@builder 你怎么保证顺序？
builder: 用单写者队列，代价是吞吐降到三分之一。
        （没人再点名 → 停下来等你）
```

没点名 = 全员回答（这是「同时问多个模型」的场景）；模型不点名 = 它认为可以收敛了。

### 2. 同一轮并行，互相看不见

你一次问三个模型时，它们**同时**回答，这一轮互相看不到对方的答案。下一轮才看得到。

这是有意的：如果要它们互相看见，就得串行等，第一个模型的答案会锚定后面所有模型的思路——那样你得到的不是三个独立判断，而是一个判断加两个附和。

`--sequential` 可以切成串行，如果你确实想要逐层递进。

### 3. 三层护栏

| 护栏 | 作用 |
|---|---|
| 轮次 / 调用次数上限 | 防止 `a→b→a→b` 无限互推 |
| 成本硬顶 | 每次调用记 token，超预算直接断 |
| 收敛检测 | 新发言和已有发言词重合度过高就停 |

收敛检测有一个容易漏的地方：**必须和同一轮其他模型的发言比，不能只和本轮之前的历史比**。否则三个模型说同一件事时，各自相对历史都是「全新」的，检测不出来。这个 bug 是写测试时抓到的，`tests/test_session.py::test_low_novelty_stops` 盯着它。

成本还有一条保守规则：**查不到价格的调用，成本按未知处理**，已知成本达到上限的 1/3 就停。因为未知成本意味着真实花费只会更高，不能当成安全放行。

---

## 常用参数

```bash
--models a,b,c          房间成员
--model a=gpt-4o        覆盖某个成员的模型名
--provider x=URL        自定义 OpenAI 兼容端点
--roles critic,builder  角色分配（按顺序），或 critic=critic,builder=builder
--max-rounds 6          轮次上限
--max-calls 24          调用次数上限
--max-cost 0.5          美元上限
--sequential            同一轮串行问
--web --port 8765       启动 Web 界面
--list-providers        看支持哪些模型
```

角色预设：`critic`（找漏洞）、`builder`（落到实现）、`reviewer`（查事实错误）、`explorer`（提替代方案）。也可以直接写一句自定义描述。

交互模式下：`/cost` 看每个模型花了多少，`/export` 导出转录，`/quit` 退出。

---

## 已知限制

- **没有用真实 API key 验证过。** 这个仓库里所有测试都用假的上游服务（`MockProvider` 和一个本地假的 OpenAI 兼容服务），验证的是请求格式、路由逻辑和整条 HTTP 链路。真实调用没跑过——因为作者手上没有这十几家的 key。第一次用真实 key 时如果某个 provider 报错，很可能是它的接口细节和这里写的不一致。
- **价格表是 2026-11 的估算。** 各家随时会调价，查不到价格会显示「未知」而不是 0。别拿它做财务核算。
- **上下文是全量重放。** 每一轮都把完整转录发给模型，没有做摘要压缩。长对话会很快变贵，这是最需要改进的地方。
- **不支持流式输出到 CLI。** 一个模型的回复是拿全了才显示的。Web 界面是边算边推，但推的是「整个模型的回复」，不是逐字。
- **没有持久化。** 关掉进程对话就没了。`/export` 能导出文本，仅此而已。
- **同一时刻只有一个房间在跑。** 可以开多个房间，但同时只有一个在会商，其余排队（最多等 180 秒）。并行跑多个房间会让成本失控，而成本失控正是这类工具最容易死的地方。
- **不做工具调用 / 联网。** 纯对话，模型不能执行代码或搜索。
- **不做事实校验。** 一个模型说错了，别的模型可能附和。`reviewer` 角色只是提示词层面的建议，不是保证。
- **Web 界面没有鉴权。** 默认只绑 `127.0.0.1`。改 `--host 0.0.0.0` 之前先想清楚。

---

## 跑测试

```bash
python -m unittest discover -s tests -t . -v
```

103 个用例，全部离线，不花钱：

| 文件 | 覆盖 |
|---|---|
| `test_room.py` | `@名字` 解析、`@all` 展开、邮箱里的 `@` 不算点名、没点名=全员、模型不能点自己、别人的发言要包成 user 并标说话人 |
| `test_session.py` | 交接链 `a→b→c`、`a→b→a→b` 被轮次上限截断、同轮并行时互相看不见、收敛短语、低新信息停止、单个模型挂了不拖垮整轮 |
| `test_providers.py` | 三种请求格式（Claude 的 `max_tokens` 必填、Gemini 的 `assistant→model`、OpenAI 的 system 在 messages 里）、usage 字段映射、别名解析、缺 key 报错、`grok`≠`groq` |
| `test_web.py` | 起真 HTTP 服务 + 假上游：工作台页面、provider 可用性标记、建/删房间、SSE 流、点名只问一个、转录持久化、统计拆分、Markdown 导出、各种 400/404、页面不引外部资源 |

---

## License

[MIT](LICENSE) © 2026 xingaorpw
