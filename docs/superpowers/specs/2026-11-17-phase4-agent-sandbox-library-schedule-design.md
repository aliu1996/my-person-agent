# Phase 4 · Agent 路径（`dsh-bridge`）、沙箱、Library 与定时任务设计文档

**日期：** 2026-11-17
**状态：** 设计定稿（暂不写代码）
**前置文档：**
- [2026-11-16-personal-assistant-server-design.md](./2026-11-16-personal-assistant-server-design.md)
- [2026-11-17-phase3-fastpath-moderation-memory-design.md](./2026-11-17-phase3-fastpath-moderation-memory-design.md)

---

## 1. 目标与边界

- **目标**：打通 ~15% 复杂工具/文件/PPT 回合的 `dsh-bridge` 执行链路、远程容器沙箱隔离、HITL 人工确认挂起与恢复、Library 合并资产库以及 `arq` 定时任务与 Activity 动态。
- **代码边界**：`services/dsh-bridge` 通过插件/配置接入 `dsh`，**严禁修改 `vendor/deepseek-harness-master/` 源码**。

---

## 2. `dsh-bridge` 服务设计与冷恢复

### 2.1 粘性路由与无状态冷恢复

- `chat-stream` 路由到 Agent 路径时，先查 Redis `runtime:sticky:{sessionId}`：
  - 若对应 `dsh-bridge` Pod 存活，直接粘性转发 `POST /internal/dsh/turn`；
  - 若 Pod 缩容/宕机或首次进入 Agent 路径，负载均衡选新 Pod 并更新粘性键（`EX 1800`）。
- **冷恢复**：`dsh` 进程内只保留 30 分钟热内存会话；回合结束时把完整事件增量上传 S3 `sessions/{user_id}/{session_id}/events.jsonl`。新 Pod 收到请求时若本地无缓存，自动从 S3 拉取历史并转换为标准上下文恢复。

### 2.2 协议翻译与禁用宿主机工具

- **禁用危险原生工具**：在 `dsh-bridge` 启动配置中强制关闭 `dsh` 宿主机本地 `shell` / `fs` 工具，改为注册：
  1. `remote_sandbox`（远程沙箱执行 Python / JS / 生成图表与 Office 文档）；
  2. `web_search`（调用 `search-service`）；
  3. `schedule_create`（创建提醒/定时任务，触发 `confirm` 确认卡片）。
- **模型出口统一**：`dsh` 的 LLM Provider 指向 **Higress AI 网关**（带 `X-Turn-Id`、`X-User-Id`），确保 token 统计与积分结算口径与快路径完全一致。
- **事件翻译**：`dsh-bridge` 拦截 `dsh` 内部事件，逐一映射为标准 SSE（`thinking` / `tool` / `confirm` / `artifact` / `delta` / `done`）。

---

## 3. 远程沙箱池隔离方案

| 隔离维度 | 生产（K8s） | 本地 / 机房降级 |
|---|---|---|
| 运行时 | Kata Containers / gVisor | Docker Rootless + Seccomp |
| 网络 | 默认 `--network=none`（禁止公网与内网元数据访问） | 同左 |
| 资源配额 | 单次 1 vCPU / 1GB RAM / 最长执行 60s / 输出 ≤ 50MB | 同左 |
| 文件挂载 | 仅挂载当前会话所需附件（只读）与临时工作区 `/workspace` | 同左 |

- **产物自动沉淀**：沙箱执行结束时，收集 `/workspace/output/*` 产出的 PPT、Excel、PNG 图表，自动上传 S3 `artifacts/{user_id}/...` 并写入 `session_artifact`，向前端推送 `artifact` 事件。
- **时长计费**：沙箱实际运行时长（按每 10 秒向上取整 × `point_rule.sandbox_10s_cost`）计入本回合积分消耗。

---

## 4. 人工确认（HITL）挂起与恢复

1. **触发场景**：外发动作（发邮件、写外部系统）、不可逆删除、创建定时任务/提醒。
2. **挂起流程**：
   - `dsh-bridge` 拦截工具调用前，若命中需确认列表（且未在当前会话勾选“总是允许”），生成 `confirmId` 写入 Redis（`EX 600`），推送 SSE `confirm` 事件后**挂起等待**（释放工作线程，异步等待唤醒）。
3. **用户决策（`POST /api/chat/confirm`）**：
   - 用户点「允许执行」或「拒绝」后，`chat-api` 校验会话归属并将决策投递给 `dsh-bridge /internal/dsh/confirm`；
   - 允许 → 继续执行工具并把结果返回模型；若勾选 `rememberInSession`，在 Redis 记录本会话该类操作免确认；
   - 拒绝 → 向模型注入“用户拒绝执行该操作”，模型据此礼貌回应或提供替代方案；
   - 超时 10 分钟未操作 → 自动按拒绝处理。

---

## 5. Library 资产库与定时任务（M9 / M10 / M11）

### 5.1 Library 合并视图（`GET /api/library/page`）

- 用 SQL `UNION ALL` 合并 `user_file`（上传文件）与 `session_artifact`（AI 产出），按 `created_at DESC` 排序并分成 `Today / This week / Older` 三组；
- 支持 `category` 过滤（`all` / `media` 图片图表 / `files` 文档表格）；
- 预览与下载均返回 S3 15 分钟限时预签名 URL；**删除会话时不自动删除 Library 产物**（需在 Library 单独删除）。

### 5.2 定时任务与 Activity（`arq`）

- **自然语言创建**：用户在对话中说“每天早上 8 点给我发 AI 早报” → `dsh` 调用 `schedule_create` 弹出 `confirm` 确认卡片（可微调时间/渠道） → 确认后写入 `chat_schedule_task` 并绑定固定早报会话 `target_session_id`。
- **调度执行**：
  - `worker` 每分钟扫描 `status='active' AND next_run_at <= now()` 的任务（使用 `FOR UPDATE SKIP LOCKED` 防多 Worker 重复领取）；
  - 执行前先调用 `check_points_threshold`，积分不足则写 `chat_schedule_log (status='skipped_no_points')` 并推站内信通知；
  - 正常到点后构造内部请求走 `chat-stream` 生成结果，写入 `target_session_id`、记录 `chat_schedule_log`、写 `user_notification` 并触发站内信 / App Push；
  - 连续失败 3 次（`fail_streak >= 3`）自动置 `auto_paused` 并通知用户检查。

---

## 6. Phase 4 验收标准

1. **Agent 与产物**：上传表格并要求“分析并生成对比图与 PPT”，`dsh-bridge` 通过远程沙箱完成执行，对话中展示 `tool` 与 `artifact` 卡片，且 Library 中立即能查到并预览。
2. **安全隔离**：在沙箱内尝试访问宿主机路径或内网地址被拒绝。
3. **HITL 确认**：触发需确认操作时流挂起并推送 `confirm` 卡片；点「拒绝」后不执行且模型正常收尾；点「允许」后继续执行完成。
4. **定时任务**：对话创建 1 分钟后触发的测试提醒，到期后 `worker` 自动执行、落入目标会话并在 Activity 动态中可查。
