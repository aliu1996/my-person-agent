<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'
import MarkdownIt from 'markdown-it'
import { api, safeUrl, streamTurn, upload } from './api.mjs'
import { useAccount } from './account.js'

const account = useAccount()
const markdown = new MarkdownIt({ html: false, breaks: true, linkify: true })
const defaultLink = markdown.renderer.rules.link_open ||
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))
markdown.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer')
  return defaultLink(tokens, idx, options, env, self)
}
markdown.renderer.rules.image = (tokens, idx) => markdown.utils.escapeHtml(tokens[idx].content)

const view = ref('chat')
const phone = ref('')
const otp = ref('')
const notice = ref('')
const dark = ref(localStorage.getItem('pa-theme') !== 'light')
const sessions = ref([])
const selected = ref(null)
const models = ref([])
const model = ref('')
const messages = ref([])
const prompt = ref('')
const web = ref(false)
const files = ref([])
const temporary = ref(false)
const busy = ref(false)
const turnId = ref(null)
const entries = ref([])
const libraryFilter = ref('all')
const page = ref(1)
const more = ref(false)
const openedAsset = ref(null)
const memoryEdit = ref(null)
const canCapture = Boolean(navigator.mediaDevices?.getDisplayMedia)
let controller

const nav = [
  ['chat', '对话'], ['library', '资产库'], ['activity', '动态'],
  ['schedules', '定时任务'], ['settings', '设置'],
]
const balance = computed(() => account.profile?.pointsBalance ?? 0)
const signedIn = computed(() => Boolean(account.jwt))
const request = (...args) => account.request(...args)

async function guard(fn) {
  notice.value = ''
  try { await fn() } catch (error) { notice.value = error.message }
}

async function sendCode() {
  await guard(async () => {
    await api('/auth/sendOtp', { method: 'POST', body: { phone: phone.value } })
    notice.value = '验证码已发送'
  })
}

async function login() {
  await guard(async () => {
    await account.login(phone.value, otp.value)
    otp.value = ''
    const data = await request('/model/listForSelection')
    models.value = data.items
    await refreshSessions()
  })
}

function theme() {
  dark.value = !dark.value
  localStorage.setItem('pa-theme', dark.value ? 'dark' : 'light')
}

async function refreshSessions() {
  sessions.value = (await request('/chat/session/page')).items
}

function resetChat() {
  selected.value = null
  messages.value = []
  files.value = []
  prompt.value = ''
  temporary.value = false
}

async function openSession(id) {
  if (busy.value) return
  await guard(async () => {
    const data = await request('/chat/session/' + id + '/messages')
    selected.value = id
    const session = sessions.value.find((entry) => entry.id === id)
    model.value = session?.modelCode || ''
    messages.value = data.items.map((item) => ({ ...item, cards: [] }))
    files.value = []
    temporary.value = false
    view.value = 'chat'
  })
}

async function addChat() {
  const title = prompt.value.trim().slice(0, 42) || '新会话'
  const session = await request('/chat/session/add', {
    method: 'POST', body: { title, isTemporary: temporary.value },
  })
  selected.value = session.id
  return session.id
}

async function capture() {
  await guard(async () => {
    const stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false })
    try {
      const video = document.createElement('video')
      video.srcObject = stream
      await video.play()
      const canvas = Object.assign(document.createElement('canvas'), {
        width: video.videoWidth, height: video.videoHeight,
      })
      canvas.getContext('2d').drawImage(video, 0, 0)
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'))
      if (!blob) throw new Error('截图失败')
      files.value.push(new File([blob], '截图.png', { type: 'image/png' }))
    } finally { stream.getTracks().forEach((track) => track.stop()) }
  })
}

async function send() {
  const content = prompt.value.trim()
  if (busy.value || !content) return
  busy.value = true
  let started = false
  await guard(async () => {
    const sessionId = selected.value || await addChat()
    const fileIds = []
    for (const file of files.value) {
      fileIds.push((await upload(file, sessionId, account.jwt)).fileId)
    }
    started = true
    prompt.value = ''
    files.value = []
    messages.value.push({ role: 'user', content, cards: [] },
      { role: 'assistant', content: '', cards: [], status: 'streaming' })
    const response = messages.value.at(-1)
    controller = new AbortController()
    await streamTurn({
      sessionId, content, fileIds, modelCode: model.value || null,
      webSearch: web.value, isTemporary: temporary.value,
    }, () => account.jwt, ({ event, data }) => {
      if (data.turnId) turnId.value = data.turnId
      if (event === 'meta') response.routeType = data.routeType
      else if (event === 'delta') response.content += data.text
      else if (event === 'error') {
        if (data.replaceOutput) response.content = ''
        response.error = data.message
        response.status = 'failed'
      } else if (event === 'done') {
        response.status = data.status
        response.pointCost = data.pointCost
        if (data.balanceAfter !== undefined) account.profile.pointsBalance = data.balanceAfter
      } else if (['thinking', 'source', 'tool', 'confirm', 'memory', 'artifact'].includes(event)) {
        const existing = event === 'tool' && response.cards.find(
          (card) => card.event === event && card.stepId === data.stepId,
        )
        if (existing) Object.assign(existing, data)
        else if (event !== 'thinking' || data.finished) response.cards.push({ ...data, event })
      }
    }, controller.signal)
    await refreshSessions()
  })
  if (started && messages.value.at(-1)?.status === 'streaming') {
    messages.value.at(-1).status = 'interrupted'
  }
  busy.value = false
  turnId.value = null
  controller = null
}

async function stop() {
  await guard(() => request('/chat/stream/stop', { method: 'POST',
    body: turnId.value ? { turnId: turnId.value } : { sessionId: selected.value },
  }))
}

async function confirm(card, decision) {
  await guard(async () => {
    await request('/chat/confirm', { method: 'POST', body: {
      confirmId: card.confirmId, decision, rememberInSession: Boolean(card.remember),
    } })
    card.decision = decision
  })
}

async function undo(card) {
  await guard(async () => {
    await request('/memory/' + card.memoryId + '/undo', { method: 'POST' })
    card.undone = true
  })
}

async function editMemory() {
  const { card, content } = memoryEdit.value
  if (!content.trim()) return
  await guard(async () => {
    await request('/memory/edit', { method: 'PUT', body: { id: card.memoryId, content } })
    card.content = content
    memoryEdit.value = null
  })
}

async function preview(id, download = false) {
  await guard(async () => {
    const asset = await request('/library/' + id + '/preview?download=' + download)
    openedAsset.value = { ...asset, url: safeUrl(asset.url), download }
  })
}

async function navigate(name, append = false) {
  view.value = name
  if (!append) { entries.value = []; page.value = 1 }
  const paths = {
    library: '/library/page?category=' + libraryFilter.value + '&page=' + page.value,
    activity: '/activity/page?page=' + page.value, schedules: '/schedule/list',
    settings: '/memory/list?page=' + page.value,
  }
  if (!paths[name]) return
  await guard(async () => {
    const data = await request(paths[name])
    if (view.value !== name) return
    entries.value = append ? [...entries.value, ...data.items] : data.items
    more.value = Boolean(data.hasMore)
  })
}

async function activity(row) {
  const id = row.targetUrl?.match(/^\/chat\/(\d+)$/)?.[1]
  if (id) await openSession(Number(id))
  await guard(() => request('/activity/' + row.id + '/read', { method: 'POST' }))
  row.isRead = true
}

async function scheduleAction(row, action) {
  await guard(async () => {
    if (action === 'delete' && !window.confirm('确认删除这个定时任务？')) return
    if (action === 'pause') {
      await request('/schedule/edit', { method: 'PUT', body: {
        id: row.id, paused: row.status === 'active',
      } })
    } else {
      await request('/schedule/' + row.id + (action === 'run' ? '/runNow' : ''),
        { method: action === 'run' ? 'POST' : 'DELETE' })
    }
    await navigate('schedules')
    if (action === 'run') notice.value = '已排队，执行结果会出现在动态中'
  })
}

async function preferences() {
  await guard(async () => {
    const { nickname, customInstructions, memoryEnabled } = account.profile
    account.profile = await request('/user/profile', { method: 'PUT',
      body: { nickname, customInstructions, memoryEnabled } })
    notice.value = '设置已保存'
  })
}

async function exportData() {
  await guard(async () => {
    const data = await request('/user/exportData', { method: 'POST' })
    notice.value = '导出已排队'
    for (let i = 0; i < 30 && account.jwt; i++) {
      const record = await request('/user/exportData/' + data.exportId)
      if (record.status === 'ready') {
        openedAsset.value = { url: safeUrl(record.downloadUrl), title: '个人数据导出', download: true }
        return
      }
      await new Promise((resolve) => setTimeout(resolve, 2000))
    }
  })
}

async function logout() {
  if (busy.value) return
  await account.logout()
  resetChat()
  sessions.value = []
  openedAsset.value = null
  view.value = 'chat'
}

onBeforeUnmount(() => controller?.abort())
</script>

<template>
  <div class="shell" :data-theme="dark ? 'dark' : 'light'">
    <main v-if="!signedIn" class="signin">
      <div class="logomark" aria-hidden="true"></div>
      <h1>24小时个人助理</h1>
      <form @submit.prevent="login">
        <label>手机号<input v-model.trim="phone" inputmode="tel" pattern="[0-9]{11}" required placeholder="11 位手机号"></label>
        <label>验证码</label>
        <div class="inline"><input v-model.trim="otp" inputmode="numeric" pattern="[0-9]{6}" aria-label="验证码" required>
          <button type="button" :disabled="phone.length !== 11" @click="sendCode">发送验证码</button>
        </div>
        <button class="primary" type="submit">登录</button>
      </form>
      <p role="status" class="notice">{{ notice }}</p>
    </main>

    <template v-else>
      <aside class="sidebar">
        <div class="brand"><span class="logomark" aria-hidden="true"></span>24小时个人助理</div>
        <button class="primary" :disabled="busy" @click="resetChat(); view = 'chat'">＋ 新对话</button>
        <nav aria-label="工作区"><button v-for="[id, label] in nav" :key="id" :class="{ selected: view === id }" @click="navigate(id)">{{ label }}</button></nav>
        <div class="history"><strong>最近对话</strong>
          <button v-for="session in sessions" :key="session.id" :disabled="busy" @click="openSession(session.id)">{{ session.title }}</button>
        </div>
        <button @click="theme">{{ dark ? '浅色模式' : '深色模式' }}</button>
      </aside>

      <main class="workspace">
        <header class="topbar"><h1>{{ nav.find(([id]) => id === view)?.[1] }}</h1><span>{{ balance }} 积分</span></header>
        <p v-if="notice" role="status" class="notice">{{ notice }}</p>

        <template v-if="view === 'chat'">
          <section class="transcript" aria-label="会话消息" aria-live="polite">
            <div v-if="!messages.length" class="empty"><span class="logomark" aria-hidden="true"></span><h2>今天想做什么？</h2></div>
            <article v-for="(message, index) in messages" :key="message.id || index" :class="['message', message.role]">
              <div v-if="message.role === 'assistant'" class="markdown" v-html="markdown.render(message.content || '')"></div>
              <p v-else>{{ message.content }}</p>
              <div v-for="(card, number) in message.cards" :key="number" class="card">
                <details v-if="card.event === 'thinking'"><summary>思考完成{{ card.durationMs ? ' · ' + (card.durationMs / 1000).toFixed(1) + ' 秒' : '' }}</summary></details>
                <span v-if="card.event === 'tool'">{{ card.title }} · {{ { running: '运行中', done: '完成', failed: '失败' }[card.status] }}</span>
                <div v-if="card.event === 'source'" class="sources">
                  <a v-for="source in card.items" :key="source.url" :href="safeUrl(source.url)" target="_blank" rel="noopener noreferrer">{{ source.index }}. {{ source.title }}</a>
                </div>
                <template v-if="card.event === 'confirm'">
                  <strong>{{ card.title }}</strong><p>{{ card.detail }}</p>
                  <template v-if="!card.decision"><label v-if="card.allowSessionRemember"><input v-model="card.remember" type="checkbox"> 本会话允许同类操作</label>
                    <div class="inline"><button class="primary" @click="confirm(card, 'allow')">允许</button><button @click="confirm(card, 'deny')">拒绝</button></div>
                  </template><p v-else>{{ card.decision === 'allow' ? '已允许' : '已拒绝' }}</p>
                </template>
                <div v-if="card.event === 'memory'" class="inline">
                  <span>{{ card.undone ? '已撤销记忆' : '已记住 ' + card.content }}</span>
                  <button v-if="!card.undone" @click="undo(card)">撤销</button><button v-if="!card.undone" @click="memoryEdit = { card, content: card.content }">编辑</button>
                </div>
                <div v-if="card.event === 'artifact'" class="inline"><strong>{{ card.title }}</strong>
                  <button @click="preview(card.assetId)">预览</button><button @click="preview(card.assetId, true)">下载</button>
                </div>
              </div>
              <p v-if="message.error" class="notice">{{ message.error }}</p>
              <small v-if="message.role === 'assistant' && ['done', 'stopped'].includes(message.status)">
                内容由 AI 生成<span v-if="message.pointCost !== undefined"> · {{ message.pointCost }} 积分</span>
              </small>
            </article>
          </section>
          <form class="composer" @submit.prevent="send">
            <textarea v-model="prompt" rows="2" placeholder="输入消息…" aria-label="消息" maxlength="24000" :disabled="busy" @keydown.enter.exact.prevent="!$event.isComposing && send()"></textarea>
            <div v-if="files.length" class="inline"><button v-for="(file, index) in files" :key="index" type="button" @click="files.splice(index, 1)">{{ file.name }} ×</button></div>
            <div class="compose-options">
              <label class="attach">附件<input type="file" multiple :disabled="busy || temporary" @change="files.push(...$event.target.files); $event.target.value = ''"></label>
              <button v-if="canCapture" type="button" :disabled="busy || temporary" @click="capture">截屏</button>
              <label><input v-model="web" type="checkbox"> 联网</label>
              <label v-if="!selected"><input v-model="temporary" type="checkbox" :disabled="files.length > 0"> 临时</label>
              <select v-model="model" aria-label="模型" :disabled="busy"><option value="">平台默认模型</option><option v-for="item in models" :key="item.modelCode" :value="item.modelCode">{{ item.displayName }}</option></select>
              <button v-if="busy" type="button" @click="stop">停止</button><button v-else type="submit" class="primary">发送</button>
            </div>
          </form>
        </template>

        <section v-else class="collection">
          <template v-if="view === 'library'">
            <select v-model="libraryFilter" aria-label="资产类别" @change="navigate('library')"><option value="all">全部资产</option><option value="media">图片与图表</option><option value="files">文件</option></select>
            <article v-for="asset in entries" :key="asset.id" class="entry">
              <div><strong>{{ asset.title }}</strong><p>{{ { Today: '今天', 'This week': '本周', Older: '更早' }[asset.group] }} · {{ Math.ceil((asset.size || 0) / 1024) }} KB</p></div>
              <div class="inline"><button @click="preview(asset.id)">预览</button><button @click="preview(asset.id, true)">下载</button></div>
            </article>
          </template>
          <template v-if="view === 'activity'"><article v-for="row in entries" :key="row.id" class="entry">
            <div><strong>{{ row.title }}</strong><p>{{ row.body }}</p></div><button @click="activity(row)">{{ row.targetUrl ? '打开会话' : '标为已读' }}</button>
          </article></template>
          <template v-if="view === 'schedules'"><article v-for="row in entries" :key="row.id" class="entry">
            <div><strong>{{ row.title }}</strong><p>{{ row.nextRunAt ? new Date(row.nextRunAt).toLocaleString() : '无计划运行' }} · {{ row.status }}</p></div>
            <div class="inline"><button @click="scheduleAction(row, 'run')">立即运行</button><button @click="scheduleAction(row, 'pause')">{{ row.status === 'active' ? '暂停' : '恢复' }}</button><button @click="scheduleAction(row, 'delete')">删除</button></div>
          </article></template>
          <template v-if="view === 'settings' && account.profile">
            <form class="preferences" @submit.prevent="preferences"><label>昵称<input v-model="account.profile.nickname" maxlength="80"></label>
              <label>自定义指令<textarea v-model="account.profile.customInstructions" maxlength="8000"></textarea></label>
              <label><input v-model="account.profile.memoryEnabled" type="checkbox"> 启用记忆</label>
              <div class="inline"><button class="primary" type="submit">保存</button><button type="button" @click="exportData">导出数据</button><button type="button" @click="logout">退出登录</button></div>
            </form>
            <article v-for="item in entries" :key="item.id" class="entry"><span>{{ item.content }}</span><div class="inline">
              <button v-if="item.status === 'active' && !item.undone" @click="item.memoryId = item.id; undo(item)">撤销</button><button @click="item.memoryId = item.id; memoryEdit = { card: item, content: item.content }">编辑</button>
            </div></article>
          </template>
          <p v-if="!entries.length && view !== 'settings'" class="muted">暂无记录</p>
          <button v-if="more" @click="page++; navigate(view, true)">加载更多</button>
        </section>
      </main>
    </template>

    <div v-if="memoryEdit" class="overlay" @click.self="memoryEdit = null">
      <form class="asset-modal" role="dialog" aria-modal="true" aria-label="编辑记忆" @submit.prevent="editMemory">
        <label>编辑记忆<textarea v-model="memoryEdit.content" rows="3" maxlength="500" required></textarea></label>
        <p v-if="notice" role="status">{{ notice }}</p>
        <div class="inline"><button type="button" @click="memoryEdit = null">取消</button><button class="primary" type="submit">保存</button></div>
      </form>
    </div>
    <div v-if="openedAsset" class="overlay" @click.self="openedAsset = null">
      <section class="asset-modal" role="dialog" aria-modal="true" aria-label="资产预览">
        <header class="inline"><strong>{{ openedAsset.title }}</strong><button @click="openedAsset = null">关闭</button></header>
        <img v-if="!openedAsset.download && openedAsset.mimeType?.startsWith('image/')" :src="openedAsset.url" :alt="openedAsset.title">
        <a v-if="openedAsset.url" :href="openedAsset.url" target="_blank" rel="noopener noreferrer">{{ openedAsset.download ? '下载文件' : '打开文件' }}</a>
      </section>
    </div>
  </div>
</template>
