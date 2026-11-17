# 个人工作助手 · MVP（P0）分阶段实施计划

**日期：** 2026-11-17
**状态：** 实施计划
**关联文档：**
- 功能需求：[2026-11-17-personal-assistant-functional-requirements.md](../specs/2026-11-17-personal-assistant-functional-requirements.md)
- 技术架构：[2026-11-16-personal-assistant-server-design.md](../specs/2026-11-16-personal-assistant-server-design.md)
- 数据模型：[2026-11-17-personal-assistant-data-model.md](../specs/2026-11-17-personal-assistant-data-model.md)
- 接口协议：[2026-11-17-personal-assistant-api-contract.md](../specs/2026-11-17-personal-assistant-api-contract.md)
- 交互原型：[prototype/index.html](../../../prototype/index.html)、[admin.html](../../../prototype/admin.html)、[mobile.html](../../../prototype/mobile.html)

---

## 阶段总览

> 当前开发优先级：**先跑通桌面端个人供应商配置和对话，再推进 Phase 3 平台流式对话**。每个阶段独立做冒烟测试。

| 阶段 | 名称 | 核心交付 | 验收门槛 |
|---|---|---|---|
| **Phase 0** | 工程骨架与本地环境 | Monorepo 目录、Docker Compose（PG16+pgvector / Redis / MinIO / Higress） | `docker compose up` 全绿，FastAPI 健康检查通过 |
| **Phase 1** | 数据模型与后台 M18 | 全部 MVP 表迁移 + 管理员 2FA + M18 模型管理 + Higress 同步 | 后台可配供应商/模型并在后台「在线试聊」成功 |
| **Phase 2** | C 端认证、积分与会话 | 手机号 OTP 登录 + 4 桶积分账户 + 会话 CRUD / 分享 / 隐私权利 | 可登录、查余额、新建会话、生成分享快照 |
| **当前优先项** | 桌面端个人供应商与模型 | Electron 启动、「设置 → 模型」、本机加密保存 API Key | 新增供应商与模型后能切换试聊，重启后保留配置 |
| **Phase 3** | 快路径流式、搜索、审核与记忆 | `chat-stream` + `search-service` + 双层审核 + 自动记忆 + 幂等扣费 | 端到端流式问答、断线续传、记忆提示与撤销、扣费准确 |
| **Phase 4** | Agent 路径、沙箱、Library 与定时任务 | `dsh-bridge` + 远程沙箱 + HITL `confirm` + 产物 + `arq` 定时任务 | 触发图表/PPT 生成、发邮件确认卡片、早报按时投递 |
| **Phase 5** | 前端开发、可观测性与上线准备 | Vue 3 用户端/Electron、管理后台、Flutter、OTel/Langfuse/Loki、审核评测 | 三端联调通过，压测与审核拒答率达标 |

---

## Phase 0：工程骨架与本地基础设施

1. **目录结构**：
   - `services/chat-api/`（FastAPI 业务服务 + Alembic 迁移）
   - `services/chat-stream/`（轻量流式网关，仅依赖 `httpx` + `redis`）
   - `services/worker/`（`arq` 异步任务：定时任务、记忆提取、每日积分刷新、注销清理）
   - `services/search-service/`（博查 + Tavily 适配与缓存）
   - `services/dsh-bridge/`（Node ≥ 24.9，`dsh` 插件封装，**不修改 `vendor/deepseek-harness-master`**）
   - `apps/web-user/`（Vue 3 + Electron）、`apps/web-admin/`（Vue 3 + Element Plus）、`apps/mobile/`（Flutter）
   - `deploy/compose/` 与 `deploy/helm/`
2. **本地一键环境**：Docker Compose 拉起 PostgreSQL 16（含 `pgvector`）、Redis、MinIO、Higress。

---

## Phase 1：数据库迁移与管理后台 M18（先通配置）

> 先做 M18 是为了从第一天起**不在代码里写死任何模型地址或 Key**。

1. **Alembic 建表**：按数据模型文档创建全量表与分区（`chat_session` HASH 16 区、`chat_message` 按月 RANGE 分区、`user_memory` HNSW 索引）。
2. **后台认证**：`admin_user` 独立登录 + TOTP 2FA。
3. **M18 模型管理接口与同步**：
   - 供应商 & Key 池 AES-GCM 加密入库、掩码返回、连通性测试；
   - 模型 CRUD、三倍率（入/缓/出）、降级模型、下线影响检查、在线试聊；
   - 系统用途绑定（快路径 / Agent / 摘要 / 记忆 / 审核）；
   - 变更后通过 Higress Console API 下发 xDS 热更新，并写入 `audit_log`。
4. **积分规则接口**：`point_rule` 读写。

---

## Phase 2：C 端账号、积分账户与会话基础

1. **认证与隐私**：`sendOtp`（含防刷限流）、`loginByPhone`、自动初始化 `point_account` + 首日 `daily_free` 积分桶；个人资料与自定义指令读写；数据导出与 7 天冷静期注销任务。
2. **会话与分享**：`chat_session` / `chat_group` CRUD、消息树查询（`parent_id` + `active_leaf_msg_id`）、公开分享快照（`shares/{token}.json`）与举报接口。
3. **文件预签名**：S3/MinIO 预签名上传与 `user_file` 登记。

---

## 当前优先项：桌面端个人供应商与模型

1. 在 `apps/web-user` 启动 Electron。个人模型功能可独立运行。
2. 在「设置 → 模型」新增、编辑或删除个人 OpenAI 兼容供应商。供应商配置包括名称、API 根地址和按需填写的 API Key。Key 使用系统加密能力存放在本机。
3. 每个供应商可添加多个模型，支持获取模型列表和手动填写模型 ID。在对话页选择模型后，由桌面进程直接请求该供应商。
4. 先验收「启动 → 添加供应商 → 添加模型 → 选择模型 → 收到回复」的闭环，再检查重启保留配置和删除后的模型列表。启动步骤见 [README](../../../README.md)。

---

## Phase 3：快路径流式、审核、搜索与自动记忆

1. **`chat-stream` 核心链路**：
   - 鉴权 + 会话并发锁（`chat:lock:{sessionId}`）+ 积分门槛校验；
   - **双层内容审核**：敏感词 DFA + 自建审核模型（经 Higress 调用），输入/输出按句缓冲拦截并写 `moderation_log`；
   - **快慢路由预判**：无附件/未显式触发工具时走快路径直连 Higress（开启 `stream_options.include_usage`）；若模型输出需要工具信号，同回合无缝转交 `dsh-bridge`；
   - 统一 SSE 事件写入 Redis Stream（`chat:stream:{sessionId}`），实现 `/api/chat/stream/resume` 断线续传。
2. **`search-service`**：中文走博查、英文走 Tavily，互备降级 + Redis 缓存，输出 `source` 事件。
3. **积分结算**：回合结束按 `ai_model` 三倍率 + 工具单价汇总，`turn_id` 幂等写 `point_ledger`，4 桶按优先级扣减。
4. **自动记忆**：`arq` 异步提取记忆写入 `user_memory`（非临时对话），通过 SSE `memory` 事件推送「已记住：xxx」，支持 `undo` / `restore` / `edit`。

---

## Phase 4：Agent 路径（`dsh-bridge`）、沙箱、Library 与定时任务

1. **`dsh-bridge` 插件与冷恢复**：
   - 禁用 `dsh` 宿主机文件/命令工具；
   - 对话历史以通用 JSON 读写 PG/S3，30 分钟无交互驱逐内存，新 Pod 自动从 S3 冷恢复；
   - 将 `dsh` 内部事件翻译为标准 SSE（`thinking` / `tool` / `artifact` / `done`）。
2. **远程沙箱池**：Docker Rootless + Seccomp（K8s 用 Kata/gVisor），只读挂载脚本、默认禁止公网、限制 CPU/内存/时长，沙箱耗时计入积分。
3. **HITL 人工确认**：敏感操作（发邮件/写外部系统等）挂起并推送 `confirm` 事件；用户调用 `/api/chat/confirm` 后恢复执行。
4. **Library & 定时任务**：
   - `user_file` + `session_artifact` 合并分页查询与预览；
   - `arq` 周期扫描 `chat_schedule_task`，到点触发执行并写固定会话 + `user_notification` + 推送，连续失败自动暂停。

---

## Phase 5：前端三端、可观测性与上线前评测

1. **前端按确认原型落地**：
   - `apps/web-admin`（Vue 3 + Element Plus）：M18 模型管理、积分规则、审核复审队列、用户与审计日志；
   - `apps/web-user`（Vue 3 + Electron）：完善工作台与平台服务联调，接入深/浅色、划词浮窗、截屏提问，并保留提前交付的个人供应商设置；
   - `apps/mobile`（Flutter）：对话、语音输入、拍照识图、App Push 与弱网续传。
2. **可观测性**：OpenTelemetry 串联全链路、Prometheus/Grafana 看板、Loki 日志、Langfuse 自托管（10% 抽样 + 隐私脱敏 + 临时对话不上报内容）。
3. **上线门槛**：自建审核模型拒答率评测与算法备案材料准备；5,000 并发 SSE 压测。
