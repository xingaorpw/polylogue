# 参与贡献

## 先跑起来

```bash
git clone https://github.com/xingaorpw/polylogue.git
cd polylogue
pip install -e .
python -m unittest discover -s tests -t . -v
polylogue --list-providers
```

## 几个硬约定

**不引第三方依赖。** HTTP 用 `urllib`，Web 服务用 `http.server`，前端是一个不加载外部资源的 HTML 文件。引 requests 或 FastAPI 会让 `pip install` 变重，也会让离线测试变麻烦。

**测试不许联网、不许花钱。** 所有 provider 测试都拦截 `_post`，Web 测试起一个本地假上游。CI 里网络抖动或某个厂商抽风，不该让测试变红。

**收敛检测必须和同一轮其他模型比。** 只跟本轮之前的历史比，会漏掉「三个模型说同一件事」，而那正是最该停的时候。`tests/test_session.py::TestConvergence` 盯着这条。

**未知成本不能当 0。** 查不到价格返回 `None`，界面上显示「未知」。返回 0 会让人以为免费，这是危险的谎。

**不要为了过测试改断言。** 测试挂了先确认是实现错了还是测试写错了。

## 加一个 provider

大多数情况不用写代码——如果对方是 OpenAI 兼容接口，只要在 `providers.py` 的 `PRESETS` 里加一条：

```python
"newvendor": {
    "kind": "openai",
    "base_url": "https://api.newvendor.com/v1",
    "env": "NEWVENDOR_API_KEY",
    "model": "their-default-model",
},
```

如果是原生协议（像 Claude、Gemini 那样），需要新增一个 `Provider` 子类，实现 `complete()`，并在 `build_provider` 里按 `kind` 分派。

两种情况都要：

1. 在 `pricing.py` 加价格（查不到就先不加，会显示「未知」，好过瞎填）
2. 在 `tests/test_providers.py` 加请求格式测试 —— `test_every_preset_is_buildable_with_key` 会自动覆盖新预设

## 加一个护栏

护栏都放在 `guard.py`。新的护栏必须：

1. 返回 `StopReason`，不要直接 `raise`（单个模型的问题不该杀掉整个房间）
2. 有明确的 `code`，方便调用方判断
3. 补测试，包括「不该触发时不触发」的反例

## 改 Web 界面

`polylogue/static/index.html` 是单个文件，直接改。要求：

- **不加载任何外部资源**（没有 CDN、没有字体外链），否则离线打不开
- SSE 事件格式改动要同步改 `web.py` 和前端解析
- 改完跑 `tests/test_web.py`

## License

提交的代码默认按 [MIT](LICENSE) 授权。
