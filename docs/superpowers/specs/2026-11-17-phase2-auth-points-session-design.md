# Phase 2 · C 端认证、积分账户与会话设计文档

**日期：** 2026-11-17
**状态：** 设计定稿（暂不写代码）
**前置文档：**
- [2026-11-17-personal-assistant-data-model.md](./2026-11-17-personal-assistant-data-model.md)
- [2026-11-17-personal-assistant-api-contract.md](./2026-11-17-personal-assistant-api-contract.md)
- [2026-11-17-phase1-db-and-m18-design.md](./2026-11-17-phase1-db-and-m18-design.md)

---

## 1. 目标与边界

- **目标**：打通 C 端用户完整生命周期（手机号登录、个人资料与隐私权利）、4 桶积分账户与幂等结算原语、统一会话与消息树分支查询、公开分享快照与文件预签名上传。
- **边界**：本阶段先完成静态会话与资产骨架，**流式模型调用与 `dsh` 接入在 Phase 3–4 完成**。

---

## 2. C 端认证与隐私权利（M1 / M15）

### 2.1 手机号登录与防刷

- **存储安全**：`sys_user.phone` 不存明文索引——使用 `phone_hash`（HMAC-SHA256）做唯一查询，使用 `phone_ciphertext`（AES-GCM）存密文，返回前端时脱敏为 `138****8000`。
- **`POST /api/auth/sendOtp`**：
  1. 校验 11 位手机号格式；
  2. Redis 限流：同一手机号 60 秒冷却（`auth:rl:cd:{phone}`）+ 每日最多 10 次（`auth:rl:day:{phone}`）；
  3. 生成 6 位随机码写入 `auth:otp:{phone}`（`EX 300`，最多允许错 5 次）；本地开发环境通过 `ENV=dev` 允许固定测试码。
- **`POST /api/auth/loginByPhone`**：
  1. 校验验证码后，若用户不存在则在同一数据库事务内创建 `sys_user`、`point_account` 与首日 `daily_free` 积分桶（额度读 `point_rule.daily_free_amount`）；
  2. 若用户处于注销冷静期（`status = 'deleting'`），登录时提示并自动恢复为 `active`；
  3. 使用 `USER_JWT_SECRET` 签发 `user_jwt`（Access 7 天 / Refresh 30 天）。

### 2.2 数据导出与账号注销

- **数据导出（`POST /api/user/exportData`）**：投递 `arq` 异步任务，汇总用户的会话消息、`user_memory`（`active`）与 `user_file` 元数据打包为 ZIP 上传 S3，生成 24 小时有效的预签名下载链接并通过站内信通知。
- **注销冷静期（`POST /api/user/deleteAccount`）**：置 `status = 'deleting'`、`delete_at = now() + 7 days` 并吊销现有 JWT；`worker` 每日扫描 `delete_at <= now()` 的用户，物理清理 PG 行、向量、S3 对象与分享快照。

---

## 3. 积分账户与 4 桶结算原语（D8 / D9）

### 3.1 懒刷新 + 定时刷新双保险

- **每日刷新**：`daily_free` 桶的 `expire_at` 设为次日 `00:00:00+08:00`。
- **为什么需要懒刷新**：17 万用户在零点瞬间全量扫表压力大；因此除 `worker` 在凌晨低峰批量刷新外，用户任何时候查询余额或发起对话时，若发现 `daily_free` 桶已过期，就在事务内**立即重置为当日免费额度（不累积）** 并同步更新 `point_account.total_balance`。

### 3.2 扣费与门槛原语（供 Phase 3 调用）

- **发送前门槛（`check_points_threshold`）**：校验 `total_balance >= 1`（可配置），不足直接返回错误码 `4020`，不触发任何模型调用。
- **回合结算（`settle_turn_points(user_id, turn_id, cost, detail)`）**：
  1. `SELECT ... FOR UPDATE` 锁定 `point_account`；
  2. 按 `priority ASC, expire_at ASC` 遍历余额 > 0 的 `point_bucket` 依次扣减（1 `daily_free` → 2 `promo` → 3 `org` → 4 `paid`）；
  3. 若本回合实际消耗略超当前余额，允许最后一笔扣减后 `total_balance` 小幅为负（下限 `-20`），保证不中途掐断正在生成的回答；
  4. 写入 `point_ledger`（`turn_id` 唯一约束保证即使重试也绝不重复扣费）。

---

## 4. 统一会话、消息树与分享快照（M2 / D7）

### 4.1 消息树与分支回溯

```text
msg_1 (user: 你好)
 └── msg_2 (assistant: 你好，有什么可以帮您？)
      ├── msg_3a (user: 总结销售表) ── msg_4a (assistant: 旧回答)
      └── msg_3b (user: 改问预算表) ── msg_4b (assistant: 新回答)  <-- active_leaf_msg_id
```

- **查询当前分支（`GET /api/chat/session/{id}/messages`）**：
  - 支持传入 `leafMsgId`（默认取 `chat_session.active_leaf_msg_id`）；
  - 使用 PostgreSQL `WITH RECURSIVE` 沿 `parent_id` 向上回溯出当前分支的有序消息链；
  - 同时统计链上每个节点的同级兄弟节点数（`siblingCount`、`siblingIds`），为 V1 前端分支切换 UI 留好完整数据。
- **编辑重发**：Phase 3 发消息时若传入 `parentMsgId`，即在树上新增分叉并把 `active_leaf_msg_id` 指向新叶节点，旧分支完整保留。

### 4.2 分组、回收站与公开分享

- **分组与置顶**：`chat_group` CRUD；`chat_session` 支持 `is_pinned`、`group_id`、`title` 修改，列表按 `is_pinned DESC, last_msg_at DESC` 排序。
- **逻辑删除**：`DELETE /api/chat/session/{id}` 仅写 `deleted_at`，30 天后由 `worker` 物理清理。
- **公开分享快照（`POST /api/chat/share/add`）**：
  - 将当前分支消息（**剔除记忆提示与内部系统字段**）序列化为 JSON 存入 S3 `shares/{share_token}.json`，写 `chat_share` 记录（含 `expire_at`）。
  - `GET /api/chat/share/{token}` 免登录读取 S3 快照；若已过期或 `is_revoked = true` 则拒绝访问。
  - `POST /api/chat/share/{token}/report` 写入举报记录，达到阈值自动暂封并进入后台审核队列。

---

## 5. 文件预签名上传（M5）

- **`POST /api/file/presignUpload`**：
  - 校验 MIME 白名单（图片、PDF、Office 文档、TXT/MD/CSV）与文件大小（≤ 50MB）；
  - 生成 S3/MinIO 预签名 `PUT` URL（有效期 10 分钟）与目标 `s3_key`：`uploads/{user_id}/{yyyy-mm}/{file_id}_{safe_name}`。
- **`POST /api/file/confirmUpload`**：
  - 后端调 S3 `head_object` 确认文件真实存在且大小匹配后，写入 `user_file` 表并返回 `fileId`。

---

## 6. Phase 2 验收标准

1. **认证与隐私**：`sendOtp` + `loginByPhone` 走通，首次登录自动创建 `sys_user`、`point_account` 与 1000 积分 `daily_free` 桶；申请注销后 7 天内重登可自动恢复。
2. **积分**：修改 `daily_free` 桶 `expire_at` 为昨天后查询余额，验证懒刷新立即重置为 1000 且不累积；连续两次用同一 `turn_id` 调用结算函数，只扣一次积分。
3. **消息树与分享**：手动插入带分支的测试消息后，`GET .../messages` 能按 `leafMsgId` 准确返回各自路径；生成分享链接免登录可访问，撤销后立即失效。
4. **文件上传**：通过预签名 URL 直传文件到 MinIO，`confirmUpload` 成功落库 `user_file`。
