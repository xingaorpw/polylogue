# Changelog

## [Unreleased]

## [0.1.0] - 2026-11-14

首个可用版本。

### Added

- **多方会商房间**：多个模型共享一份转录，可以互相 `@名字` 接话。用户没点名 = 全员回答，模型不点名 = 它认为可以收敛。
- **同一轮并行**：一次问多个模型时同时调用，这一轮互相看不见对方的答案（`--sequential` 可切成串行）。
- **三层护栏**：轮次上限、调用次数上限、成本硬顶；外加收敛检测（新发言和已有发言词重合度过高就停）。
- **未知成本保守处理**：查不到价格的调用按未知计，已知成本到上限 1/3 就停，因为真实花费只会更高。
- **三个适配器覆盖全部厂商**：OpenAI 兼容（Grok / DeepSeek / 通义 / Kimi / 智谱 / Groq / OpenRouter / Ollama 等）、Claude 原生 Messages API、Gemini 原生 generateContent。
- **14 个预设 + 15 个别名**：`--list-providers` 可查。`grok`（xAI）和 `groq`（推理托管）分开处理。
- **本地工作台**：三栏界面（房间列表 / 对话 / 花费面板），多房间并行管理，各自独立的成员、预算和转录；手动停止（停在轮次边界）；Markdown 导出。标准库 `http.server` + SSE，单个 HTML 文件，无前端构建、无外部资源，离线可用。
- **工作台 HTTP 接口**：`/api/info`、`/api/rooms`（增删查）、`/api/rooms/{id}/say`（SSE）、`/stop`、`/stats`、`/export`，可以直接当 API 用。
- **交互模式**：`/cost` 看每个模型的花费、`/export` 导出转录。
- **角色预设**：`critic` / `builder` / `reviewer` / `explorer`，避免所有模型说同一件事。
- 100 个测试，全部离线；CI 覆盖 Ubuntu + Windows、Python 3.9 / 3.10 / 3.12，另加冒烟任务验证「缺 key 必须报错」和「工作台必须真的起得来且不引外部资源」。

### Notes

- 所有测试用假上游，**没有用真实 API key 验证过**。已验证的是请求格式、路由逻辑和整条 HTTP + SSE 链路。
- 上下文全量重放，没做摘要压缩。长对话会很快变贵，这是已知的最大短板。
- 同一时刻只有一个房间在跑会商，其余排队。

[0.1.0]: https://github.com/xingaorpw/polylogue/releases/tag/v0.1.0
