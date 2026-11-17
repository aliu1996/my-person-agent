# Phase 5 · 前端三端、可观测性与上线前评测设计文档

**日期：** 2026-11-17
**状态：** 设计定稿（暂不写代码）
**前置文档：**
- [2026-11-16-personal-assistant-server-design.md](./2026-11-16-personal-assistant-server-design.md)
- [2026-11-17-phase4-agent-sandbox-library-schedule-design.md](./2026-11-17-phase4-agent-sandbox-library-schedule-design.md)
- 交互原型：[prototype/index.html](../../../prototype/index.html)、[admin.html](../../../prototype/admin.html)、[mobile.html](../../../prototype/mobile.html)

---

## 1. 目标与边界

- **目标**：按已定稿原型完成 **Vue 3 管理后台**、**Vue 3 用户端 + Electron 桌面端**、**Flutter 移动端** 开发与接口联调，接入全链路可观测性，完成上线前的安全审核评测与并发压测。

---

## 2. 前端三端工程落地

### 2.1 管理后台（`apps/web-admin`：Vue 3 + Element Plus + Pinia）

- **菜单与页面**（对齐 [admin.html](../../../prototype/admin.html)）：
  - 独立登录页（账号密码 + TOTP 2FA 扫码绑定/输入）；
  - **M18 模型管理**：供应商与 Key 池（掩码显示、测试连通）、模型目录（三倍率、降级、下线影响弹窗、在线试聊）、系统用途绑定；
  - **积分规则配置**：每日免费额度与工具单价；
  - **内容审核队列**：分页查看被拦截片段，支持「误判放行」与「确认违规」；
  - **用户管理 & 变更审计日志**。

### 2.2 用户 Web 端 & Electron 桌面端（`apps/web-user`：Vue 3 + Vite + Pinia）

- **设计规范落地**：将 [tokens.css](../../../prototype/tokens.css) 直接作为主题样式基础，内置**深色 / 浅色一键切换**（默认深色，Grok Bot 极简布局）。
- **核心模块**：
  - **SSE 客户端封装**：解析 11 种标准事件，自动记录 `lastEventId`，断网重连自动调 `/api/chat/stream/resume`；
  - **消息渲染**：Markdown + 代码高亮复制 + 表格 + 思考过程折叠 + 引用角标 + `memory`（撤销/编辑）+ `confirm`（允许/拒绝）+ `artifact` 预览；
  - **极窄图标栏**：Bot 切换、历史抽屉、Library & Activity 中心弹窗、设置弹窗（记忆管理 / 积分余额 / 数据导出与注销）。
- **Electron 桌面专属层**：
  - 保留提前开发的「设置 → 模型」，支持添加个人 OpenAI 兼容供应商、选用模型并直连对话。开发顺序见 [MVP 实施计划](../plans/2026-11-17-personal-assistant-mvp-plan.md#当前优先项桌面端个人供应商与模型)。
  - 主进程注册全局快捷键（`Alt+Space` 呼出**划词助手迷你浮窗**）、系统截屏框选直传图片、本地目录白名单授权（写操作强制二次确认）。

### 2.3 手机端（`apps/mobile`：Flutter）

- **对齐 [mobile.html](../../../prototype/mobile.html)**：底部 4 Tab（对话 / Library / 动态 / 我的）、横向滑动引用/产物卡片、单行胶囊输入。
- **移动端专属能力**：
  - **SSE 后台续传**：App 切后台/弱网恢复时，自动带本地最后 `lastEventId` 拉取遗漏片段；
  - **语音输入与播报**：接入端侧录音上传与服务端 ASR/TTS；
  - **拍照识图 / 文档扫描**：相机拍图压缩后走预签名上传接入多模态模型；
  - **推送通道**：接入 APNs + 国内聚合推送（个推/极光），点击定时早报推送直达对应会话。

---

## 3. 可观测性与隐私保护

| 维度 | 方案 | 关键配置 |
|---|---|---|
| **链路追踪** | OpenTelemetry | `trace_id` 贯穿 Higress → `chat-stream` → `dsh-bridge` → Higress AI 网关；`dsh-bridge` 复用 `dsh` 内置 `telemetry` 包上报同一 OTel Collector |
| **指标看板** | Prometheus + Grafana | 监控 SSE 并发数、首字延迟（TTFT）、各服务 QPS/错误率、Higress `ai-statistics` Token 吞吐、远程沙箱池水位 |
| **日志检索** | Loki（接入 Grafana） | 结构化 JSON 日志强制带 `trace_id`、`user_id`、`turn_id`，支持从监控/链路一键跳日志 |
| **模型链路** | Langfuse（自托管 v3） | **10% 抽样上报**；报错、点踩、Agent 回合 **100% 上报**；入库前脱敏手机号/身份证；**临时对话不上报内容**，仅报指标；ClickHouse 数据设 30 天 TTL |

---

## 4. 上线前两项硬门槛（评测与压测）

1. **自建内容审核评测（合规红线）**：
   - 使用不少于 2,000 条安全测试集（涉政、涉黄、暴力、提示词注入、越狱诱导）评测「DFA + 自建审核模型」双层拦截率与误杀率；
   - 整理模型安全评估报告与审核机制说明，满足国内生成式 AI 算法备案与安全评估要求。
2. **性能压测（17 万用户基线）**：
   - 压测目标：**5,000 并发 SSE 连接**（约 4,250 快路径 + 750 Agent 路径）；
   - 验证 `chat-stream` 内存与 CPU 水位、PG 分区查询延迟、Redis Stream 内存上限与自动驱逐、Higress 故障摘除与热更新不断流。

---

## 5. Phase 5 验收标准

1. **三端联调**：Web、Electron 桌面端、Flutter 手机端全部连上真实后端，完成「登录 → 对话（快路径/Agent）→ 记忆提示与撤销 → 确认卡片 → 产物入 Library → 定时早报推送 → 跨端续传」全流程走查。
2. **后台闭环**：在管理后台新增/修改模型、调整积分倍率、处理审核复审，用户端立即生效且审计日志完整。
3. **可观测性**：在 Grafana 可同时查看指标与 Loki 日志，在 Langfuse 可按 `turn_id` 追溯完整 Agent 调用链且敏感信息已脱敏。
4. **压测与评测报告**：5,000 并发 SSE 压测无内存泄漏/掉线，审核评测达到备案要求指标。
