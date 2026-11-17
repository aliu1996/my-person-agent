# 个人工作助手 · 接口与 SSE 协议设计

**日期：** 2026-11-17
**状态：** 草案 v1（仅设计，暂不写代码）
**前置文档：**
- [2026-11-16-personal-assistant-server-design.md](./2026-11-16-personal-assistant-server-design.md)
- [2026-11-17-personal-assistant-functional-requirements.md](./2026-11-17-personal-assistant-functional-requirements.md)
- [2026-11-17-personal-assistant-data-model.md](./2026-11-17-personal-assistant-data-model.md)

---

## 1. 全局约定

- **路径风格**：统一采用 **camelCase**（禁止 kebab-case），如 `/api/chat/session/page`。
- **统一响应信封**（非 SSE 接口）：
  ```json
  { "code": 0, "message": "ok", "data": {}, "traceId": "4f8a9c..." }
  ```
- **认证隔离**：
  - 用户端（Web / Electron / Flutter）：`Authorization: Bearer <user_jwt>`
  - 管理后台：`/api/admin/**` 使用独立的 `admin_jwt`（独立密钥签发，强制 2FA）
- **核心业务错误码**：

| code | 含义 | 前端处理 |
|---|---|---|
| `0` | 成功 | 正常渲染 |
| `4001` | 参数校验失败 | 表单字段提示 |
| `4010` | 未登录 / Token 过期 | 跳转登录 |
| `4020` | 积分不足（可用余额 < 门槛） | 弹出积分不足提示，不发起模型调用 |
| `4031` | 输入内容审核拦截 | 展示拦截原因，不扣费 |
| `4091` | 同一会话并发发送冲突 | 提示"上一条正在生成中"或排队 |
| `4290` | 请求/Token 限流 | 提示稍后重试 |

---

## 2. 统一 SSE 流式协议（用户端 ↔ `chat-stream`）

> 严格执行**协议隔离**：`dsh` 内部事件在 `dsh-bridge` 层全部翻译为下述标准事件，三端只认此协议。

### 2.1 帧格式与断点续传

每条事件带单调递增 `id` 写入 Redis Stream（`chat:stream:{sessionId}`，保留 10 分钟）：

```text
id: 1731813600001-0
event: delta
data: {"turnId":"t_901","text":"华东区 Q3 总营收为"}
```

断线重连或跨端同步时调用：
`GET /api/chat/stream/resume?sessionId={sessionId}&lastEventId={lastEventId}`

### 2.2 事件类型清单（11 种）

| event | 触发时机 | Payload 关键字段 |
|---|---|---|
| `meta` | 回合开始或路由升级 | `turnId`, `messageId`, `routeType` (`fast`/`agent`), `modelCode`, `fallbackFrom` (降级时非空), `aiGenerated`, `routeChanged` (升级时为真) |
| `thinking` | 思考模型推理中 | `turnId`, `delta`, `durationMs`, `finished` |
| `tool` | Agent 工具调用 | `turnId`, `stepId`, `title`, `status` (`running`/`done`/`failed`), `errorMsg`, `retryable` |
| `source` | 联网搜索完成 | `turnId`, `items`: `[{index, title, url, domain}]` |
| `delta` | 正文流式文本 | `turnId`, `text` |
| `confirm` | 敏感操作 / 定时任务待确认 | `turnId`, `confirmId`, `kind` (`action`/`schedule`), `title`, `detail`, `allowSessionRemember`, `payload` |
| `memory` | D10 提取并写入新记忆 | `turnId`, `memoryId`, `content`, `canUndo` |
| `artifact` | 生成 PPT/图表/图片 | `turnId`, `artifactId`, `title`, `category` (`media`/`file`), `mimeType`, `size`, `previewUrl` |
| `done` | 回合结束结算 | `turnId`, `status` (`done`/`stopped`), `pointCost`, `balanceAfter`, `usage` (`inputTokens`/`cachedTokens`/`outputTokens`/`estimated`), `aiGenerated` |
| `error` | 审核拦截 / 异常中止 | `turnId`, `code` (`4031`/`4020`/`5000`), `message`, `retryable`, `replaceOutput` (需替换本轮已显示内容时为真) |
| `ping` | 心跳保活（每 15s） | `{}` |

---

## 3. REST 接口清单

### 3.1 账号与隐私（M1 / M15）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/auth/sendOtp` | 发送手机验证码（含防刷限流） |
| `POST` | `/api/auth/loginByPhone` | 手机号 + 验证码登录/注册，返回 JWT |
| `GET` | `/api/user/profile` | 获取当前用户信息、自定义指令、记忆开关、积分余额 |
| `PUT` | `/api/user/profile` | 更新昵称、自定义指令、`memoryEnabled` |
| `POST` | `/api/user/exportData` | 异步打包导出会话/文件/记忆 |
| `POST` | `/api/user/deleteAccount` | 申请注销账号（进入 7 天冷静期） |

### 3.2 统一对话与确认（M2 / M8）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/chat/stream/send` | 发送消息（**返回 SSE**）；入参：`sessionId`, `parentMsgId` (D7 分支), `content`, `modelCode`, `webSearch`, `fileIds[]`, `isTemporary` |
| `GET` | `/api/chat/stream/resume` | SSE 断线续传 |
| `POST` | `/api/chat/stream/stop` | 提交 `sessionId` 或 `turnId`，停止当前生成并按已产生用量结算积分 |
| `POST` | `/api/chat/confirm` | 提交敏感操作确认（`confirmId`, `decision`: `allow`/`deny`, `rememberInSession`）或定时任务确认 |
| `POST` | `/api/chat/tool/retry` | 工具失败后点击“重试此步”（`turnId`, `stepId`） |
| `GET` | `/api/chat/session/page` | 侧边栏会话分页列表（支持关键词搜索、分组过滤） |
| `POST` | `/api/chat/session/add` | 新建空会话（可选指定 `botId`） |
| `PUT` | `/api/chat/session/edit` | 重命名、置顶、移入分组 |
| `DELETE` | `/api/chat/session/{id}` | 逻辑删除移入回收站 |
| `GET` | `/api/chat/session/{id}/messages` | 获取会话当前分支消息历史（支持按 `branchMsgId` 切换分支） |
| `POST` | `/api/chat/feedback` | 点赞 / 点踩（带原因） |
| `POST` | `/api/chat/share/add` | 生成分享快照链接（有效期、撤销） |
| `GET` | `/api/chat/share/{token}` | 公开只读查看分享快照（免登录） |
| `POST` | `/api/chat/share/{token}/report` | 举报分享内容 |

### 3.3 记忆管理（M4 / D2 / D10）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/memory/list` | 分页查看我的记忆条目 |
| `PUT` | `/api/memory/edit` | 编辑某条记忆内容 |
| `POST` | `/api/memory/{id}/undo` | 撤销“已记住：xxx”提示（状态置 `undone`） |
| `POST` | `/api/memory/{id}/restore` | 恢复已撤销的记忆 |
| `DELETE` | `/api/memory/{id}` | 永久删除单条记忆 |

### 3.4 文件与 Library 资产库（M5 / M9）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/file/presignUpload` | 获取 S3 预签名上传地址（校验大小与类型） |
| `POST` | `/api/file/confirmUpload` | 上传完成后登记 `user_file` |
| `GET` | `/api/library/page` | Library 合并视图分页：`category` (`all`/`media`/`files`)，返回按 `Today / This week / Older` 分组 |
| `GET` | `/api/library/{id}/preview` | 获取预签名预览/下载链接 |

### 3.5 定时任务、Activity 与模型目录（M10 / M11 / M14）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/schedule/list` | 我的定时任务与提醒列表 |
| `PUT` | `/api/schedule/edit` | 修改时间/渠道、暂停或恢复 |
| `POST` | `/api/schedule/{id}/runNow` | 立即触发执行一次 |
| `DELETE` | `/api/schedule/{id}` | 删除定时任务 |
| `GET` | `/api/activity/page` | Activity 动态流（定时任务日志、长任务进度、通知） |
| `GET` | `/api/model/listForSelection` | 当前用户可见模型列表（含能力标签、积分倍率） |
| `GET` | `/api/points/summary` | 今日剩余积分、今日已用、流水明细（V1） |

### 3.6 管理后台（M16 / M18，独立 `admin_jwt`）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/admin/auth/login` | 管理员账号密码 + 2FA TOTP 登录 |
| `GET` | `/api/admin/provider/list` | 供应商列表（Key 只返回掩码） |
| `POST` | `/api/admin/provider/add` / `PUT .../edit` | 新增/修改供应商与 Key 池（加密入库并同步 Higress） |
| `POST` | `/api/admin/provider/{id}/test` | 连通性测试，返回延迟与 `/models` 列表 |
| `GET` | `/api/admin/model/list` | 模型目录列表 |
| `POST` | `/api/admin/model/add` / `PUT .../edit` | 配置 `model_code`、能力标签、三倍率、降级模型、灰度比例 |
| `GET` | `/api/admin/model/{code}/impact` | 下线前检查受影响的会话、Bot、定时任务数量 |
| `POST` | `/api/admin/model/testChat` | 后台在线试聊验证 |
| `GET` / `PUT` | `/api/admin/model/bindings` | 查看与修改系统各用途的 `model_code` 绑定 |
| `GET` / `PUT` | `/api/admin/points/rules` | 配置每日免费积分与搜索/沙箱/生图单价 |
| `GET` | `/api/admin/moderation/page` | 内容审核拦截待复审队列 |
| `POST` | `/api/admin/moderation/{id}/review` | 人工复审（`false_positive` 放行 / `confirmed` 确认违规） |
| `GET` / `PUT` | `/api/admin/moderation/terms` | 读取或替换敏感词，写入时提交 `{"items":["词条"]}` |
| `GET` / `POST` | `/api/admin/user/page` / `.../ban` | 用户查询、封禁/解封、赠送活动积分 |
| `GET` | `/api/admin/auditLog/page` | 配置与运营操作审计日志 |

---

## 4. 内部服务契约（仅内网）

| 调用方 → 被调方 | 接口 | 职责 |
|---|---|---|
| `chat-stream` → `dsh-bridge` | `POST /internal/dsh/turn` (SSE) | 传入 `sessionId`、`turnId`、通用 JSON 历史、权限声明，输出标准 SSE 事件 |
| `chat-stream` → `dsh-bridge` | `POST /internal/dsh/confirm` | 恢复挂起的 HITL 工具调用 |
| `chat-stream` / `dsh` → `search-service` | `POST /internal/search` | 传入 `query`、`lang`，自动路由博查/Tavily 并缓存 |
| `chat-api` → Higress Console API | `PUT /v1/ai/providers` & `routes` | 后台修改供应商/Key/模型后热同步到 Higress AI 网关 |
