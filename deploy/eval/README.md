# 审核评测框架

本目录提供审核评测的运行器和少量无害占位样本。它调用现有的 `chat_stream.moderation.check`（DFA 词表、私钥正则，以及可选的审核模型），不会再实现一套过滤器。

仓库不包含 2,000 条完整评测集，也不包含算法备案材料。这两项需要另行准备，在隔离环境中跑完整集后才能作为上线验收依据。

## 样本格式

单条样本是一个 JSON 对象，JSONL 每行一条，或使用 JSON 数组。字段见 `schema.json`：

- `id`：唯一编号
- `text`：待审核文本
- `direction`：`input` 或 `output`，默认 `input`
- `expected`：`allow` 或 `block`
- `expectedLayer`：可选，`dfa` / `regex` / `model`
- `note`：说明

词表是字符串数组，例如 `fixtures/terms.json`。完整集请放到已忽略的 `data/full-set.jsonl`，不要提交真实敏感内容。

## 离线运行（不需要后端）

离线模式会走同一套 DFA / 正则逻辑。输入方向或命中输出复审规则、但本地层未拦截的样本，记为 `model_pending`，表示需要审核模型做最终判定。

```powershell
uv run --package chat-stream python deploy/eval/run_moderation_eval.py
uv run --all-packages pytest tests/test_moderation_eval.py
```

默认读取 `fixtures/cases.jsonl` 和 `fixtures/terms.json`，在 `deploy/eval/data/last-run/` 写出 `summary.json` 和 `report.md`。该目录已加入 .gitignore。

```powershell
uv run --package chat-stream python deploy/eval/run_moderation_eval.py --cases deploy/eval/data/full-set.jsonl --terms deploy/eval/fixtures/terms.json --out deploy/eval/data/full-run
```

## 在线运行（调用已有审核模型）

需要与 `chat-stream` 相同的网关配置：`HIGRESS_GATEWAY_URL`，以及按需填写的 `HIGRESS_GATEWAY_KEY`。`--model` 填写已绑定到 `moderation` 的模型编码。

```powershell
uv run --package chat-stream python deploy/eval/run_moderation_eval.py --cases deploy/eval/data/full-set.jsonl --model your-moderation-model
```

`summary.json` 统计样本数、拦截数、`model_pending`、应拦截召回率和（在已标注放行样本上的）误伤率。报告模板会明确说明完整集和备案材料需单独提供。
