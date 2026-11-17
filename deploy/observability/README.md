# 流式对话压测

脚本是 `stream-load.js`。每个虚拟用户使用 `PA_LOAD_USERS` 里一条独立的 `{ sessionId, jwt }`，向 `PA_LOAD_URL` 发送一次 `POST /api/chat/stream/send`。默认 4,250 个快路径 + 750 个 Agent 虚拟用户，需要至少 5,000 条测试会话，且必须在隔离环境运行。

账户 JSON 的格式见 `users.schema.json`。`users.example.json` 只有两条占位记录，把 `jwt` 换成预先创建的测试用户令牌后，即可做本地冒烟。完整账户文件请放到 `users.local.json`（已忽略），不要提交真实令牌。

## 本地冒烟（1 + 1 VU）

先安装 [k6](https://k6.io/docs/get-started/installation/)，再准备两个测试会话。

```powershell
$env:PA_LOAD_URL = "http://127.0.0.1:8101"
$env:PA_LOAD_USERS = "deploy/observability/users.example.json"
$env:PA_FAST_VUS = "1"
$env:PA_AGENT_VUS = "1"
k6 run deploy/observability/stream-load.js --summary-export=deploy/observability/reports/smoke-summary.json
```

## 完整 5,000 VU

```powershell
$env:PA_LOAD_URL = "https://stream.example.internal"
$env:PA_LOAD_USERS = "deploy/observability/users.local.json"
$env:PA_FAST_VUS = "4250"
$env:PA_AGENT_VUS = "750"
k6 run deploy/observability/stream-load.js --summary-export=deploy/observability/reports/full-summary.json
```

不设置 `PA_FAST_VUS` / `PA_AGENT_VUS` 时，脚本默认就是 4250 + 750。把摘要填进 `load-report-template.md`。没有隔离环境和足够的测试会话时，不要声称压测验收已完成。
