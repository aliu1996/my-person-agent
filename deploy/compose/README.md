# 单机 Docker Compose 部署

面向一台 Linux 服务器的个人自建，用同一个 Compose 文件启动基础设施和应用进程。本机开发也可以只用基础设施：默认配置不启用 `app` profile，应用仍可在宿主机运行。

不要把真实密钥提交到 Git。这是个人自建方案，不是多节点生产集群。

## 本机开发（只启动基础设施）

在仓库根目录：

```bash
cp -n .env.example .env
docker compose --project-directory . -f deploy/compose/docker-compose.yml up -d
```

PowerShell 可将 `cp -n` 换成 `if (-not (Test-Path .env)) { Copy-Item .env.example .env }`。然后按根目录 README 的「本地启动」在宿主机运行迁移和 API。

Postgres、Redis、Higress 控制台和对象存储控制台只绑定在 `127.0.0.1`，本机开发可继续用 `localhost` 访问。

## 单机自建

1. 在服务器上安装 Docker 和 Docker Compose 插件，把仓库放到服务器并进入根目录。
2. 复制环境文件并改密钥：

   ```bash
   cp .env.example .env
   ```

   至少修改这些项（使用足够长的随机值，密码尽量只用字母和数字，避免打乱数据库连接串）：

   - `ENV=prod`
   - `POSTGRES_PASSWORD`
   - `S3_ACCESS_KEY` / `S3_SECRET_KEY`
   - `USER_JWT_SECRET` / `ADMIN_JWT_SECRET` / `KEY_ENCRYPT_MASTER` / `INTERNAL_API_TOKEN` / `PHONE_HASH_SECRET`
   - `INIT_ADMIN_USERNAME` / `INIT_ADMIN_PASSWORD`
   - 按需填写短信、Higress、搜索和 FCM 配置

   再设置：

   ```bash
   COMPOSE_PROFILES=app
   S3_PUBLIC_ENDPOINT=http://你的服务器地址:9000
   ```

   `COMPOSE_PROFILES=app` 之后，普通的 `up -d` 就会带上应用进程。`S3_PUBLIC_ENDPOINT` 要改成浏览器能访问的地址，用于文件直传。

3. 启动：

   ```bash
   docker compose --project-directory . -f deploy/compose/docker-compose.yml up -d --build
   curl http://127.0.0.1:8100/healthz
   ```

   首次会构建镜像并执行 Alembic 迁移。

4. 对外只开放应用端口。默认发布的是：

   - `8100`：chat-api（管理端登录、用户 API、Swagger）
   - `8101`：chat-stream（平台流式对话）
   - `9000`：对象存储 API（浏览器直传；若暂时不用文件上传，可在防火墙关掉）

   默认不对外暴露：`5432`（Postgres）、`6379`（Redis）、`9001`（对象存储控制台）、`8080` / `8001` / `8443`（Higress，含控制台）。它们只监听服务器本机，需要时用 SSH 隧道访问控制台，例如 `ssh -L 8001:127.0.0.1:8001 user@server`。

5. 首次管理员登录与模型配置，按根目录 README 的「首次登录和模型联调」。接口地址改为 `http://你的服务器地址:8100`。先通过 `POST /api/admin/auth/login` 绑定 TOTP。

6. 桌面端安装见根目录 README 末尾的「Windows 桌面端安装包」。安装完成后，在启动快捷方式前设置：

   ```powershell
   $env:CHAT_API_URL = "http://你的服务器地址:8100"
   $env:CHAT_STREAM_URL = "http://你的服务器地址:8101"
   ```

   平台对话会连到这台服务器；本机个人模型仍直连你配置的供应商。若从公网访问，建议在前面自行加 HTTPS 反代，并把上述地址改成 `https://`。

## 沙箱（可选）

Agent 沙箱需要 Linux 与 Docker socket，不随 `app` profile 启动：

```bash
docker build -f deploy/sandbox/Dockerfile -t pa-sandbox:phase4 .
docker compose --project-directory . -f deploy/compose/docker-compose.yml --profile app --profile sandbox up -d --build
```

或在 `.env` 中设置 `COMPOSE_PROFILES=app,sandbox`。

## 常用命令

```bash
docker compose --project-directory . -f deploy/compose/docker-compose.yml ps
docker compose --project-directory . -f deploy/compose/docker-compose.yml logs -f chat-api
docker compose --project-directory . -f deploy/compose/docker-compose.yml down
```

加 `--volumes` 会删除数据卷，个人数据会一起清掉。
