# Phase 0 · 工程骨架与本地环境设计文档

**日期：** 2026-11-17
**状态：** 设计定稿（暂不写代码）
**前置计划：** [2026-11-17-personal-assistant-mvp-plan.md](../plans/2026-11-17-personal-assistant-mvp-plan.md)

---

## 1. 目标与边界

- **目标**：搭建可运行的多服务 Monorepo 骨架与一键本地基础设施，让后续 Phase 1–5 直接往对应目录填业务，不再反复调整工程结构。
- **硬边界**：
  1. **不修改 `vendor/deepseek-harness-master/` 源码**，`dsh-bridge` 作为独立包通过依赖/插件方式接入。
  2. **`chat-stream` 保持轻量**：只依赖 `fastapi`、`uvicorn`、`httpx`、`redis`、`pydantic`，严禁引入 SQLAlchemy 或重型 Agent 包，保证 5,000 并发 SSE 时的低内存占用。
  3. **三环境零绑定**：本地用 Docker Compose（MinIO 替代云 OSS），生产通过环境变量无缝切换为云上 PG / Redis / S3 与 K8s Higress。

---

## 2. Monorepo 目录结构

```text
my-agent/
├── pyproject.toml                 # uv workspace 根配置 (Python >= 3.11, ruff, pytest)
├── .env.example                   # 本地环境变量模板（禁止提交真实 .env）
├── vendor/
│   └── deepseek-harness-master/   # 上游源码（只读，不修改）
├── packages/
│   └── pa-shared/                 # Python 公共库（配置、响应信封、错误码、JWT/AES、SSE Schema）
├── services/
│   ├── chat-api/                  # FastAPI 业务服务 + Alembic 数据库迁移
│   ├── chat-stream/               # FastAPI 轻量流式网关（鉴权、审核、路由、SSE 续传）
│   ├── worker/                    # arq 异步 Worker（定时任务、记忆抽取、积分刷新）
│   ├── search-service/            # 博查 / Tavily 搜索适配与 Redis 缓存
│   └── dsh-bridge/                # Node >= 24.9 + TS，封装 dsh 与标准 SSE 翻译
├── apps/
│   ├── web-user/                  # Vue 3 用户端 + Electron 桌面壳（Phase 5）
│   ├── web-admin/                 # Vue 3 + Element Plus 管理后台（Phase 5）
│   └── mobile/                    # Flutter 移动端（Phase 5）
├── deploy/
│   ├── compose/
│   │   ├── docker-compose.yml     # 本地基础设施（PG16+pgvector / Redis / MinIO / Higress）
│   │   └── init-db.sql            # 创建 pgvector 扩展
│   └── helm/                      # 生产 K8s Chart 占位（Phase 5 补全）
├── docs/                          # 需求、架构、数据模型、接口、计划文档
└── prototype/                     # 已定稿交互原型（HTML）
```

---

## 3. 公共包 `packages/pa-shared` 职责划分

为避免 4 个 Python 服务重复编写基础逻辑，抽一个极轻公共包，**但不强制所有服务依赖数据库驱动**（采用可选 extras）：

| 模块 | 内容 | 谁使用 |
|---|---|---|
| `pa_shared.settings` | Pydantic Settings 读取 `.env` | 全部 Python 服务 |
| `pa_shared.response` | `R[T]` 统一信封、业务错误码枚举（`4001`/`4020`/`4031` 等） | `chat-api`, `chat-stream` |
| `pa_shared.security` | 用户端/管理端双密钥 JWT 签发与验签、AES-GCM 加解密 | `chat-api`, `chat-stream` |
| `pa_shared.sse` | 11 种标准 SSE 事件 Pydantic 模型与帧序列化 | `chat-stream`, `dsh-bridge` 对齐 |
| `pa_shared.db` *(extra: `[db]`)* | SQLAlchemy 2.0 `asyncpg` 引擎与会话工厂 | `chat-api`, `worker`（**`chat-stream` 不装**） |

---

## 4. 本地基础设施（`deploy/compose/docker-compose.yml`）

| 容器 | 镜像 | 端口 | 初始化要求 |
|---|---|---|---|
| `pa-postgres` | `pgvector/pgvector:pg16` | `5432` | 启动挂载 `init-db.sql`，自动执行 `CREATE EXTENSION IF NOT EXISTS vector;` |
| `pa-redis` | `redis:7-alpine` | `6379` | 开启 `--appendonly yes` |
| `pa-minio` | `minio/minio:latest` | `9000` (API) / `9001` (控制台) | 附加 `minio-init` 一次性容器自动创建 `pa-assets` bucket |
| `pa-higress` | `higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/all-in-one:latest` | `8080` (网关) / `8001` (Console) | 仅本地验证 `ai-proxy` 路由与 Console API 下发 |

---

## 5. 本地端口与环境变量约定

### 5.1 端口分配表

| 端口 | 服务 | 说明 |
|---|---|---|
| `5432` | PostgreSQL 16 | 主库 + pgvector |
| `6379` | Redis 7 | Stream / 锁 / 队列 |
| `9000 / 9001` | MinIO | 对象存储 / 控制台 |
| `8080 / 8001` | Higress | AI 网关 / Console |
| `8100` | `chat-api` | 业务 REST API |
| `8101` | `chat-stream` | 流式对话与续传 |
| `8102` | `search-service` | 联网搜索服务 |
| `8103` | `dsh-bridge` | Agent 执行桥接（仅内网） |
| `5173 / 5174` | `web-user` / `web-admin` | 前端开发服务器（Phase 5） |

### 5.2 `.env.example` 关键变量

```ini
# 数据与存储
DATABASE_URL=postgresql+asyncpg://pa:pa_dev@127.0.0.1:5432/personal_assistant
REDIS_URL=redis://127.0.0.1:6379/0
S3_ENDPOINT=http://127.0.0.1:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=pa-assets

# 网关与内部服务地址
HIGRESS_GATEWAY_URL=http://127.0.0.1:8080
HIGRESS_CONSOLE_URL=http://127.0.0.1:8001
SEARCH_SERVICE_URL=http://127.0.0.1:8102
DSH_BRIDGE_URL=http://127.0.0.1:8103

# 安全密钥（本地占位，生产必须替换）
USER_JWT_SECRET=dev-user-secret-change-me
ADMIN_JWT_SECRET=dev-admin-secret-change-me
KEY_ENCRYPT_MASTER=dev-32bytes-aes-key-placeholder
```

---

## 6. 健康检查与 Phase 0 验收标准

1. **统一健康检查**：所有 HTTP 服务（`chat-api`、`chat-stream`、`search-service`、`dsh-bridge`）在骨架阶段只暴露 `GET /healthz`，返回：
   ```json
   { "code": 0, "message": "ok", "data": { "service": "chat-api", "status": "healthy" } }
   ```
2. **验收清单**：
   - `docker compose -f deploy/compose/docker-compose.yml up -d` 四个容器全部 `healthy`；
   - `SELECT extname FROM pg_extension WHERE extname = 'vector';` 返回 `vector`；
   - `uv sync` 安装依赖无冲突，`ruff check` 与 `pytest`（基础健康检查与 `pa-shared` 单元测试）全绿；
   - 4 个服务的 `/healthz` 接口均返回 `200 OK`。
