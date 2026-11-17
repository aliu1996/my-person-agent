# 24小时个人助理

个人工作助手的 Monorepo。包含 Electron 桌面端、M18 模型管理、用户与会话接口，以及 Phase 3 平台流式对话服务。

## 首个开发与验收项：桌面端自定义模型

安装 Node.js 24，在仓库根目录执行以下命令。

```powershell
cd apps/web-user
npm install
npm start
```

1. 打开「设置 → 模型 → 添加供应商」，填写名称、OpenAI 兼容 API 根地址和该供应商要求的 API Key。本机 Ollama 的根地址可填 `http://localhost:11434/v1`。公网地址使用 HTTPS，本机或私有 IP 可以使用 HTTP。
2. 保存供应商后点「获取模型列表」选择模型。也可以直接填写模型 ID。点「添加模型」完成配置。
3. 回到对话页，从顶部下拉框选择刚添加的模型并发送一条消息。每个模型的当前对话在应用运行期间独立保留。
4. 重启应用，确认供应商、模型和当前选择仍在。验收时再编辑一次配置，并验证删除模型后下拉列表会更新。

桌面端从本机直连所选供应商，请求包含发送给该模型的对话消息。供应商、模型和默认选择保存在桌面应用数据目录下的 `models.sqlite3`，API Key 由 Electron 的系统密钥库加密。首次启动会在一个事务内导入原有的 `models.json`，原文件保留供手动恢复。地址变更后，填写新地址对应的 Key。个人模型对话可以独立于后端服务运行。

```powershell
npm test
```

## 本地启动

依赖 Python 3.11+、uv 和 Docker Compose。在仓库根目录执行以下命令（PowerShell）。

```powershell
uv sync --all-packages
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose --project-directory . -f deploy/compose/docker-compose.yml up -d
```

默认只启动 Postgres、Redis、对象存储和 Higress。Postgres / Redis / 控制台端口只绑定本机回环地址。若要把应用进程也放到 Docker 里，或在一台 Linux 服务器上自建，见 [deploy/compose/README.md](deploy/compose/README.md)。

在 `.env` 设置初始管理员的 `INIT_ADMIN_USERNAME` 和 `INIT_ADMIN_PASSWORD`。首次迁移创建账号。之后修改环境变量不会重置已有管理员的密码。部署前为用户端 JWT、管理员 JWT 和加密主密钥分别设置高强度随机值。加密主密钥用于供应商 Key 和 TOTP 密钥，后续读取密文需要同一个值。

打开 [Higress Console](http://localhost:8001) 初始化控制台账号，将凭据填入 `HIGRESS_CONSOLE_USERNAME` 和 `HIGRESS_CONSOLE_PASSWORD`。如果网关使用 Key Auth，在控制台创建 Consumer，再配置 `HIGRESS_GATEWAY_CONSUMER` 和 `HIGRESS_GATEWAY_KEY`。无 Consumer 的配置适用于本机开发。

```powershell
uv run --all-packages alembic -c services/chat-api/alembic.ini upgrade head
uv run --package chat-api uvicorn chat_api.main:app --port 8100
```

另开一个终端启动 Worker，处理网关同步重试、消息分区、用户数据导出和过期数据清理。

```powershell
uv run --package pa-worker arq worker.main.WorkerSettings
```

服务健康检查在 [http://localhost:8100/healthz](http://localhost:8100/healthz)，API 文档在 [http://localhost:8100/docs](http://localhost:8100/docs)。

## 首次登录和模型联调

1. 调用 `POST /api/admin/auth/login`，提交 `username` 和 `password`。首次登录返回 `setupToken` 和 `otpauthUri`。
2. 将 `otpauthUri` 导入身份验证器，调用 `POST /api/admin/auth/bindTotp`，提交 `setupToken` 和六位字符串 `totpCode`。后续在登录请求直接提交新的 TOTP 验证码。
3. 复制响应中的 `data.adminJwt`，在 Swagger UI 的 **Authorize** 中填入，或使用请求头 `Authorization: Bearer <adminJwt>`。
4. 调用 `POST /api/admin/provider/add` 添加供应商，再用返回的 `id` 调用 `POST /api/admin/provider/{provider_id}/test`。示例请求体如下。

   ```json
   {
     "name": "DeepSeek",
     "providerType": "cloud",
     "protocol": "openai",
     "baseUrl": "https://api.deepseek.com/v1",
     "keys": [{"apiKey": "<替换为实际 Key>"}]
   }
   ```

5. 调用 `POST /api/admin/model/add`，提交 `modelCode`、`providerId`、`upstreamModel`、`displayName` 和三项倍率 `rateIn`、`rateCache`、`rateOut`。以供应商连通性测试返回的模型 ID 为准。
6. 用 `GET /api/admin/gateway/syncStatus` 查看下发状态。若返回 `pending_retry`，Worker 会重试，也可调用 `POST /api/admin/gateway/sync` 立即下发。状态变为 `synced` 后，调用 `POST /api/admin/model/testChat`，提交 `modelCode` 和 `prompt`，查看首字延迟、回复和用量。
7. 用 `GET /api/admin/auditLog/page` 查看审计。积分规则和用途绑定分别通过 `GET / PUT /api/admin/points/rules` 和 `GET / PUT /api/admin/model/bindings` 读写。

编辑供应商时，省略 `keys` 会保留 Key 池。提交 `keys` 时，列表表示保存后的完整 Key 池，带 `id` 且省略 `apiKey` 的条目保留原凭据。

## Phase 2 用户端联调

先在 [RustFS 控制台](http://localhost:9001) 创建私有桶 `pa-assets`，或把 `S3_BUCKET` 配置为已有的私有桶。使用与客户端可访问地址不同的内部 S3 地址时，将公开访问地址填入 `S3_PUBLIC_ENDPOINT`。浏览器直传需要在桶上允许前端来源发起 `PUT`，并允许 `Content-Type` 和 `If-None-Match` 请求头。

1. 以 `ENV=dev` 运行时，调用 `POST /api/auth/sendOtp`，提交 11 位手机号。再调用 `POST /api/auth/loginByPhone`，提交 `phone` 和 `otpCode`。本地默认验证码为 `123456`，可通过 `OTP_DEV_CODE` 修改。
2. 将响应中的 `data.userJwt` 填入 Swagger UI 的 **User JWT** 或请求头 `Authorization: Bearer <userJwt>`。用 `GET /api/user/profile` 查看脱敏手机号和积分余额。访问令牌过期后，可将 `refreshToken` 提交给 `POST /api/auth/refresh`。
3. 通过 `POST /api/chat/session/add` 创建会话。已有消息的会话支持调用 `GET /api/chat/session/{id}/messages?leafMsgId=...` 查看指定分支。删除后可用 `GET /api/chat/session/page?recycled=true` 查看回收站，并调用 `POST /api/chat/session/{id}/restore` 恢复。
4. 用 `POST /api/chat/share/add` 提交 `sessionId` 生成公开链接，用 `DELETE /api/chat/share/{token}` 撤销。访问者可以提交举报，管理员可在 `GET /api/admin/share/reports` 查看。
5. 用 `POST /api/file/presignUpload` 提交 `fileName`、`mimeType`、`fileSize` 和可选的 `sessionId`。按响应中的 `method`、`uploadUrl` 和签名请求头上传原始文件，再将 `fileId` 提交给 `POST /api/file/confirmUpload`。浏览器应传入实际文件或 Blob，由浏览器设置匹配的 `Content-Length`。
6. 用 `POST /api/user/exportData` 申请导出，然后使用 `GET /api/user/exportData/{export_id}` 查看进度并获取下载地址。用 `POST /api/user/deleteAccount` 进入 7 天注销冷静期，期间通过手机号验证码登录即可恢复账号。

生产环境应设置 `ENV=prod`，为 `PHONE_HASH_SECRET` 设置独立且稳定的高强度密钥，并配置 `SMS_WEBHOOK_URL` 与 `SMS_WEBHOOK_TOKEN`。短信适配服务通过 HTTPS 接收 Bearer 鉴权的 JSON 请求，字段为 `phone`、`code`、`expiresIn`，成功接收并受理短信后返回 2xx。

## Phase 3 平台流式对话联调

先完成数据库迁移，启动 `chat-api` 和 Worker，再分别启动流式服务与搜索服务。

```powershell
uv run --package chat-stream uvicorn chat_stream.main:app --port 8101
uv run --package search-service uvicorn search_service.main:app --port 8102
```

1. 在 M18 添加并同步模型，把普通对话和审核模型分别绑定到 `fast_chat` 和 `moderation`。审核模型须返回布尔判定，例如 `{"blocked":false}`。管理员可通过 `PUT /api/admin/moderation/terms` 更新敏感词。
2. 登录并创建会话后，带用户 JWT 请求 `POST http://localhost:8101/api/chat/stream/send`，提交 `sessionId` 和 `content`。检查 `meta`、`delta`、`done` 帧和返回的积分用量。通过 `POST /api/chat/stream/stop` 提交 `sessionId` 或 `turnId` 可停止生成。
3. 断开连接后，用 `GET /api/chat/stream/resume?sessionId=...&lastEventId=...` 从最后收到的事件继续。Redis 保留最近 500 帧，最后一帧写入后保留 10 分钟。游标过期时，可重新获取会话历史。
4. 配置 `BOCHA_API_KEY` 或 `TAVILY_API_KEY` 后，以 `webSearch: true` 发消息并检查 `source` 事件。两家均配置时，搜索服务可在单家异常后切换。
5. 绑定 `memory_extract` 和 `embedding`，后者须输出 1536 维向量。启用个人记忆后发送明确的长期偏好，检查 `memory` 事件和 `GET /api/memory/list`。用 `POST /api/memory/{id}/undo` 撤销，下一轮检索会跳过该条目。临时对话跳过记忆读写和消息历史落库。

生产部署应为 `chat-api`、`chat-stream`、`search-service`、`dsh-bridge` 和沙箱服务配置同一个至少 32 字节的随机 `INTERNAL_API_TOKEN`，并在网关上只开放公共 API。

## Phase 4 Agent、资产库与定时任务

升级数据库到 `0005`，启动前述服务和 Worker，再从仓库根目录启动桥接。

```powershell
npm --prefix services/dsh-bridge start
```

- 默认 `DSH_ENGINE=gateway` 直接使用 Higress 上支持工具调用的模型。接入仓库所用的 DSH SDK 时，设置 `DSH_ENGINE=dsh`，并填写 `DSH_BIN` 和 `DSH_SDK_MODULE` 的路径。配置会关闭宿主机文件与命令工具，注册搜索、沙箱及定时任务工具。
- 在 Linux Rootless Docker 主机上，用 `docker build -f deploy/sandbox/Dockerfile -t pa-sandbox:phase4 .` 构建隔离镜像，再运行 `npm --prefix services/dsh-bridge run sandbox`。通过 `SANDBOX_URL` 指向该内网服务。执行器检查 cgroup v2、CPU、内存和进程数支持，任务容器在无网络模式下运行。
- 在平台对话上传表格后请求生成图表，检查 `tool`、`artifact` 与「资产库」预览。再提交一分钟后提醒，确认允许或拒绝，并通过「定时任务」和「动态」检查结果。每日任务使用北京时间，单次任务接受带时区的时间。

## Phase 5 客户端与监控

- 桌面用户从「平台对话」登录平台。开发 Web 端或管理台时，在对应目录 `apps/web-user` 或 `apps/web-admin` 执行 `npm install` 和 `npm run dev`，默认端口分别为 5173 和 5174。
- 桌面端用 `Alt+Space` 呼出划词助手迷你浮窗：会尽量读取当前选中的文本，结合补充问题发送给已选择的本机模型，并可用「在平台中打开」进入平台对话。在设置的「本机目录」中通过系统目录选择器授权本地文件夹；通过 IPC 写入这些目录中的文件前，会弹出原生确认框。
- 手机端入口在 `apps/mobile`。Android 工程壳已包含在仓库中，需先安装 Flutter SDK，然后在该目录执行 `flutter pub get` 和 `flutter run`。用 `--dart-define=CHAT_API_URL=...` 与 `--dart-define=CHAT_STREAM_URL=...` 设置设备可访问地址。麦克风、语音识别和相机权限已在 Android 清单中声明；真机请使用 HTTPS 网关。
- 仓库未包含 iOS Xcode 工程（需在 macOS 上用 Xcode 构建）。安装 Flutter 后，在 `apps/mobile` 执行 `flutter create --platforms=ios --project-name pa_mobile .` 生成 iOS 壳，并避免覆盖已有 `lib/` 源码。随后在 `ios/Runner/Info.plist` 中声明 `NSMicrophoneUsageDescription`、`NSSpeechRecognitionUsageDescription` 和 `NSCameraUsageDescription`。
- 移动端推送走已有的 FCM 后端。登录后会注册设备令牌，退出登录时注销；点击带会话信息的通知会打开对应会话。应用在缺少 Firebase 配置时仍可编译运行，只是推送不可用。启用推送时请把 Firebase 控制台下载的 `google-services.json` 放到 `apps/mobile/android/app/`，iOS 则把 `GoogleService-Info.plist` 放到 `ios/Runner/`。这两份文件已加入 `.gitignore`，不要提交。无需额外 `--dart-define`；配置文件就位后，下次构建会自动接入 Google Services 插件。
- 监控配置位于 `deploy/observability`。用 `docker compose -f deploy/observability/compose.yml up -d` 启动 Grafana、Prometheus、Loki 和 OTLP 接收器。Prometheus 从宿主机抓取 8100–8102 的 `/metrics`。Loki 读取仓库的 `logs/*.log`。配置 `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces` 可导出请求追踪。设置 Langfuse 地址与凭据后，上报采样后的回合元数据。
- 单机 Docker Compose 部署见 [deploy/compose/README.md](deploy/compose/README.md)。本机开发默认只启动基础设施；在一台 Linux 服务器上自建时，设置 `COMPOSE_PROFILES=app` 后即可一并启动应用进程。
- 审核评测框架见 [deploy/eval/README.md](deploy/eval/README.md)。离线可跑仓库内的少量占位样本；完整 2,000 条评测集和算法备案材料需另行提供，不能把占位结果当作上线验收已完成。
- 仅在隔离压测环境运行完整的 `k6` 脚本。用法见 [deploy/observability/README.md](deploy/observability/README.md)。通过 `PA_LOAD_URL` 指定 URL，通过 `PA_LOAD_USERS` 指向测试账户 JSON，列表项含 `sessionId` 和 `jwt`，每个虚拟用户使用独立会话。默认包含 4,250 个快路径和 750 个 Agent 虚拟用户，可用 `PA_FAST_VUS` 与 `PA_AGENT_VUS` 调整。本地冒烟可将这两个变量设为 `1`，并使用 `deploy/observability/users.example.json`。完整 5,000 并发需要隔离环境和 5,000 条测试会话。

## 测试和设计

```powershell
uv run --all-packages pytest
```

要运行 PostgreSQL 积分与消息分支测试，将 `PHASE2_TEST_DATABASE_URL` 指向已迁移的 PostgreSQL 测试库，再运行 pytest。

- [MVP 实施计划](docs/superpowers/plans/2026-11-17-personal-assistant-mvp-plan.md)
- [Phase 1 设计](docs/superpowers/specs/2026-11-17-phase1-db-and-m18-design.md)
- [Phase 2 设计](docs/superpowers/specs/2026-11-17-phase2-auth-points-session-design.md)
- [Phase 3 设计](docs/superpowers/specs/2026-11-17-phase3-fastpath-moderation-memory-design.md)
- [Phase 4 设计](docs/superpowers/specs/2026-11-17-phase4-agent-sandbox-library-schedule-design.md)
- [Phase 5 设计](docs/superpowers/specs/2026-11-17-phase5-clients-observability-launch-design.md)
- [API 契约](docs/superpowers/specs/2026-11-17-personal-assistant-api-contract.md)
- [数据模型](docs/superpowers/specs/2026-11-17-personal-assistant-data-model.md)

## Windows 桌面端安装包

桌面端可以用 electron-builder 打出可安装的 Windows 程序。打包前需要已安装 Node.js 24。日常开发仍使用上文的 `npm start`。

在仓库中进入桌面端目录，安装依赖并生成安装包：

```powershell
cd apps/web-user
npm install
npm run dist:win
```

`npm run pack:win` 会先构建平台页面，再导出未打包的 `release/win-unpacked/` 目录，便于检查文件是否完整。`npm run dist:win` 同样先执行 `npm run build`，然后生成可安装的 exe。

成品在 `apps/web-user/release/`，该目录已加入 `.gitignore`，不要把安装包提交到 Git。典型文件名：

- `personal-assistant-desktop-0.1.0-setup-win-x64.exe`：NSIS 安装程序。双击运行，按向导安装到 Windows，可创建桌面快捷方式和开始菜单项。
- `personal-assistant-desktop-0.1.0-portable-win-x64.exe`：便携版，无需安装即可运行。

安装完成后，从开始菜单或桌面快捷方式打开「24小时个人助理」。个人模型配置和 API Key 仍保存在当前 Windows 用户的应用数据目录。平台对话默认连接 `http://127.0.0.1:8100` 和 `http://127.0.0.1:8101`。若后端部署在远程服务器，先设置环境变量再启动，例如 `$env:CHAT_API_URL="http://你的服务器地址:8100"` 和 `$env:CHAT_STREAM_URL="http://你的服务器地址:8101"`。

当前打包不包含代码签名。如果打包时因 npm.taobao.org 证书错误导致 Electron 下载失败，可在本次终端临时设置 `$env:ELECTRON_MIRROR="https://cdn.npmmirror.com/binaries/electron/"` 和 `$env:ELECTRON_BUILDER_BINARIES_MIRROR="https://cdn.npmmirror.com/binaries/electron-builder-binaries/"`，然后再运行 `npm run dist:win`。Windows 可能提示未知发布者，选择仍要运行即可。若需要签名，在 `apps/web-user/package.json` 的 `build` 配置中自行添加证书。
