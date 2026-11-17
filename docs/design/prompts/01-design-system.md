# 第 1 步：设计规范（Design System）提示词

> 用法：整段复制给能画 UI 的 AI 工具（Figma Make、v0、Claude、ChatGPT 等）。
> 若工具只能出图片，使用文末"图片模型简版"。

---

## 完整版提示词

```
你是一名资深产品 UI 设计师。请为一款 AI 个人工作助手产品设计一页完整的「设计规范 / Design System」。

【产品背景】
- 产品：AI 个人工作助手（类似 Grok / ChatGPT），一个对话里完成问答、联网搜索、写文档、生成图表/PPT、提醒和定时任务。
- 本次只做 Web / 桌面端（Electron），桌面宽度 1440px。
- 用户：国内普通互联网用户，界面文字全部为简体中文。
- 前端技术：Vue 3。

【风格方向】
- 参考 Grok：深色、极简、科技感、克制。大面积近黑背景，以黑白灰为主，只用一个强调色点缀。
- 少用阴影和渐变，靠细边框和明度层级区分层次；浮层可用轻微毛玻璃。
- 圆角柔和，留白充足，信息密度适中，不要花哨插画。

【设计令牌（请严格使用）】
颜色 - 背景层级：
  bg/base #0A0A0A（页面底色）
  bg/surface #141414（侧边栏、卡片）
  bg/elevated #1C1C1C（输入框、下拉菜单、弹窗）
  bg/hover #232323
  border/default #262626，border/strong #3A3A3A
颜色 - 文字：
  text/primary #FAFAFA，text/secondary #A1A1A1，text/tertiary #6B6B6B，text/disabled #4A4A4A
颜色 - 强调色：
  accent #5B8CFF（链接、焦点环、选中态、进度），accent/hover #7AA2FF，accent/subtle rgba(91,140,255,0.12)
颜色 - 语义色：
  success #22C55E，warning #F59E0B，danger #EF4444，info #38BDF8（各配一个 12% 透明度的浅底色）
主按钮采用「白底黑字」反色风格（#FAFAFA 底，#0A0A0A 字），这是 Grok 风格的关键。

字体：
  中文 PingFang SC / 思源黑体，英文数字 Inter，代码 JetBrains Mono
  字号阶梯：12 / 13 / 14 / 16 / 18 / 20 / 24 / 30 / 36（px）
  正文 14px 行高 1.6；对话内容 15px 行高 1.75；字重只用 400 / 500 / 600

间距：4px 基准，阶梯 4 / 8 / 12 / 16 / 20 / 24 / 32 / 40 / 48 / 64
圆角：6（小标签）/ 10（按钮、输入框）/ 14（卡片）/ 20（对话输入框、弹窗）/ 999（胶囊、头像）
图标：线性图标风格（如 Lucide），线宽 1.5px，尺寸 16 / 20 / 24

【页面内容：请按以下分区，从上到下排布在一个 1440px 宽的长画板上，每个分区有中文标题】

1. 色彩：展示上面所有颜色色块，标注名称和色值。
2. 字体：展示字号阶梯，每级给中文示例文字和用途说明（如"页面标题 24/600"）。
3. 间距与圆角：可视化展示间距阶梯和各档圆角。
4. 按钮：主按钮（白底黑字）、次按钮（深灰底）、幽灵按钮（透明）、危险按钮、纯图标按钮；
   大中小三种尺寸；每种都展示 默认 / 悬停 / 按下 / 禁用 / 加载中 五种状态。
5. 表单：文本输入框（默认/聚焦/错误/禁用）、手机号+验证码输入组、下拉选择、开关、复选框、单选、滑块。
6. 对话输入框（最重要的组件）：大圆角输入区，左下角有「附件」「联网搜索」开关，
   右下角有「模型选择器」胶囊和「发送」圆形按钮；再展示一个「生成中」状态，发送按钮变成「停止」方块按钮。
7. 消息样式：
   - 用户消息：右侧，深灰圆角气泡。
   - AI 回复：左侧，无气泡，全宽排版，包含标题、列表、加粗、行内代码、代码块（带复制按钮）、表格。
   - 回复下方操作栏：复制、重新生成、点赞、点踩、分享，以及小字"消耗 12 积分"。
8. 对话中的特殊卡片：
   - 思考过程：可折叠，标题"已深度思考（用时 8 秒）"。
   - 工具调用步骤：可折叠的步骤列表，如"正在联网搜索… → 读取 3 个网页 → 生成图表"，每步有状态图标。
   - 引用来源：一排小卡片，显示网站图标、标题、域名，正文中用上标数字 [1][2] 对应。
   - 记忆提示：一条轻量的行内提示"已记住：你在上海工作，偏好简洁回答"，右侧有「撤销」「编辑」文字按钮。
   - 敏感操作确认卡片：标题"需要你确认"，说明"即将发送邮件给 zhang@example.com"，按钮「允许」「拒绝」，以及勾选项"本次会话总是允许"。
   - 产物卡片：文件图标 + 文件名"Q3 销售分析.pptx" + 大小 + 「预览」「下载」按钮；另一种是图表缩略图卡片。
   - 错误提示条：内容审核拦截 / 积分不足 两种样式。
9. 导航组件：
   - 侧边栏会话项：默认 / 悬停 / 选中 / 置顶 四种状态，悬停时右侧出现「···」更多按钮。
   - 会话分组标题：今天 / 最近 7 天 / 更早。
   - 标签页：All / Media / Files 样式。
   - 面包屑、分页。
10. 浮层：
   - 下拉菜单、右键菜单。
   - 模型选择菜单：每个模型一行，显示模型名、能力标签（视觉 / 深度思考 / 工具调用）和积分倍率"×2"，当前选中带对勾。
   - 弹窗（Modal）、轻提示（Toast，成功/警告/错误）、气泡提示（Tooltip）。
11. 数据展示：标签/徽章、头像（含默认字母头像）、积分余额胶囊（如"今日剩余 860 积分"）、进度条、空状态（简洁线性图标 + 一句话 + 按钮）、骨架屏加载态。

【输出要求】
- 所有组件状态都要画出来，不要只画默认态。
- 每个组件旁用小字标注尺寸、圆角、颜色令牌名称，方便开发对照。
- 整体保持统一、克制、高级，不要使用彩色渐变背景和卡通插画。
- 如果你能输出代码：请输出一个完整的单文件 HTML（内联 CSS，使用 CSS 变量定义上述令牌），可直接用浏览器打开查看。
```

---

## 图片模型简版

```
A complete dark-mode UI design system sheet for an AI assistant web app, Grok-inspired style, 1440px wide long artboard. Near-black background #0A0A0A, surfaces #141414 and #1C1C1C, thin #262626 borders, white primary buttons with black text, single accent color #5B8CFF. Sections: color palette swatches with hex labels, typography scale (Inter + PingFang SC, Chinese sample text), spacing and corner radius scale, buttons in all states (default, hover, pressed, disabled, loading), form inputs, a large rounded chat composer with attach / web search / model selector / send button, chat message styles (user bubble right, assistant full-width markdown with code block and table), collapsible "thinking" and tool-step cards, citation source cards, inline "memory saved" notice with undo, confirmation card with Allow/Deny buttons, file artifact card, sidebar conversation items, tabs, dropdown, model selector menu with capability tags, modal, toast, badges, avatars, empty state, skeleton loading. Minimal, clean, high-end, flat, subtle glassmorphism on popovers, Lucide line icons, Chinese UI labels, dribbble-quality, crisp, no gradients, no illustrations.
```

> 图片模型对中文文字渲染较差，图中文字可能乱码，只用来看整体风格；精确规范以完整版输出为准。
