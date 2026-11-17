import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  app, BrowserWindow, clipboard, desktopCapturer, dialog, globalShortcut, ipcMain, protocol,
  safeStorage, session, shell,
} from 'electron'
import { chat, fetchModels } from './provider.mjs'
import { SettingsError, createSettingsStore } from './settings.mjs'
import { captureSelectedText, formatMiniQuestion } from './selection.mjs'

const ENTRY = 'assistant://desktop/index.html'
const PLATFORM = 'assistant://desktop/platform/index.html'
const MINI = 'assistant://desktop/mini.html'
const CHAT_ROUTE = /^\/api\/(auth|chat|user|model|points|memory|library|activity|schedule|file)\//
const ASSETS = new Map([
  ['/index.html', 'text/html; charset=utf-8'],
  ['/app.js', 'text/javascript; charset=utf-8'],
  ['/styles.css', 'text/css; charset=utf-8'],
  ['/mini.html', 'text/html; charset=utf-8'],
  ['/mini.js', 'text/javascript; charset=utf-8'],
  ['/mini.css', 'text/css; charset=utf-8'],
])
let window
let platformWindow
let miniWindow
let miniSelection = ''

app.setName('24小时个人助理')
protocol.registerSchemesAsPrivileged([
  { scheme: 'assistant', privileges: {
    standard: true, secure: true, supportFetchAPI: true, stream: true, corsEnabled: true,
  } },
])

async function requireKeyStore() {
  if (!await safeStorage.isAsyncEncryptionAvailable() ||
      (process.platform === 'linux' && safeStorage.getSelectedStorageBackend() === 'basic_text')) {
    throw new SettingsError('系统密钥库不可用，无法保存或读取 API Key')
  }
}

const keyStore = {
  async encrypt(key) {
    await requireKeyStore()
    return (await safeStorage.encryptStringAsync(key)).toString('base64')
  },
  async decrypt(ciphertext) {
    await requireKeyStore()
    try {
      const { result, isTemporarilyUnavailable } = await safeStorage.decryptStringAsync(
        Buffer.from(ciphertext, 'base64')
      )
      if (isTemporarilyUnavailable) throw new SettingsError('系统密钥库暂时不可用，请稍后重试')
      if (typeof result !== 'string' || !result) throw new Error('Missing decrypted API Key')
      return result
    } catch (error) {
      if (error instanceof SettingsError) throw error
      throw new SettingsError('无法读取已保存的 API Key，请重新填写')
    }
  },
}

function fromFrame(sender, senderFrame, target, url) {
  return sender === target?.webContents && senderFrame && !senderFrame.parent && senderFrame.url === url
}

function handleIpc(event, url, target, operation) {
  ipcMain.handle(event, async ({ sender, senderFrame }, input) => {
    if (!fromFrame(sender, senderFrame, target(), url)) {
      return { ok: false, error: '无法处理该窗口的请求' }
    }
    try {
      return { ok: true, data: await operation(input) }
    } catch (error) {
      return {
        ok: false,
        error: error instanceof SettingsError ? error.message : '操作失败，请重试',
      }
    }
  })
}

function registerHandlers() {
  const store = createSettingsStore(join(app.getPath('userData'), 'models.sqlite3'), keyStore)
  app.on('will-quit', () => store.close().catch(() => {}))
  const operations = {
    list: store.list,
    saveProvider: store.saveProvider,
    deleteProvider: store.deleteProvider,
    saveModel: store.saveModel,
    deleteModel: store.deleteModel,
    selectModel: store.selectModel,
    fetchModels: async (id) => fetchModels(await store.connection(id, 'provider')),
    chat: async ({ modelId, messages }) => chat(await store.connection(modelId, 'model'), messages),
    openPlatform: () => openPlatform(),
  }
  for (const [event, operation] of Object.entries(operations)) {
    handleIpc(`models:${event}`, ENTRY, () => window, operation)
  }
  handleIpc('folders:list', ENTRY, () => window, () => store.listFolders())
  handleIpc('folders:add', ENTRY, () => window, async () => {
    const picked = await dialog.showOpenDialog(window, {
      title: '选择要授权的本机目录',
      properties: ['openDirectory'],
    })
    if (picked.canceled || !picked.filePaths[0]) throw new SettingsError('已取消选择目录')
    return store.addFolder(picked.filePaths[0])
  })
  handleIpc('folders:remove', ENTRY, () => window, (id) => store.removeFolder(id))
  handleIpc('folders:write', ENTRY, () => window, async (input) => store.writeLocalFile(input, async (target) => {
    const result = await dialog.showMessageBox(window ?? undefined, {
      type: 'warning',
      title: '确认写入',
      message: '即将写入本地文件，请确认路径无误。',
      detail: target,
      buttons: ['写入', '取消'],
      defaultId: 1,
      cancelId: 1,
      noLink: true,
    })
    return result.response === 0
  }))
  handleIpc('mini:ask', MINI, () => miniWindow, async (input) => {
    const prompt = typeof input?.prompt === 'string' ? input.prompt.trim() : ''
    if (!prompt || prompt.length > 4000) throw new SettingsError('请输入要询问的问题（最多 4000 字）')
    const settings = await store.list()
    if (!settings.selectedModelId) throw new SettingsError('请先在设置中添加并选择模型')
    const selection = miniSelection.trim().slice(0, 12000)
    const content = formatMiniQuestion(selection, prompt)
    return {
      reply: await chat(await store.connection(settings.selectedModelId, 'model'), [
        { role: 'user', content },
      ]),
    }
  })
  handleIpc('mini:openPlatform', MINI, () => miniWindow, () => openPlatform())
  handleIpc('mini:close', MINI, () => miniWindow, () => dismissMini())
  return store
}

async function proxyApi(request, url) {
  if (!CHAT_ROUTE.test(url.pathname) || !['GET', 'POST', 'PUT', 'DELETE'].includes(request.method)) {
    return new Response(null, { status: 404 })
  }
  const base = url.pathname.startsWith('/api/chat/stream/') ?
    process.env.CHAT_STREAM_URL || 'http://127.0.0.1:8101' :
    process.env.CHAT_API_URL || 'http://127.0.0.1:8100'
  const headers = new Headers()
  for (const key of ['authorization', 'content-type', 'last-event-id']) {
    if (request.headers.has(key)) headers.set(key, request.headers.get(key))
  }
  try {
    const upstream = await fetch(new URL(url.pathname + url.search, base), {
      method: request.method, headers, redirect: 'error', signal: request.signal,
      ...(request.method === 'GET' ? {} : { body: await request.arrayBuffer() }),
    })
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { 'Content-Type': upstream.headers.get('Content-Type') || 'application/json',
        'Cache-Control': 'no-store' },
    })
  } catch {
    return Response.json({ code: 5000, message: '无法连接平台后端' }, { status: 503 })
  }
}

function registerAssets() {
  protocol.handle('assistant', async (request) => {
    const url = new URL(request.url)
    if (url.host !== 'desktop') return new Response(null, { status: 404 })
    if (url.pathname.startsWith('/api/')) return proxyApi(request, url)
    if (request.method === 'GET' && (
      url.pathname === '/platform/index.html' ||
      /^\/platform\/assets\/[a-zA-Z0-9_-]+\.(js|css)$/.test(url.pathname)
    )) {
      const types = { html: 'text/html', js: 'text/javascript', css: 'text/css' }
      try {
        const content = await readFile(new URL('../dist' + url.pathname, import.meta.url))
        return new Response(content, { headers: {
          'Content-Type': types[url.pathname.split('.').at(-1)] + '; charset=utf-8',
          'X-Content-Type-Options': 'nosniff',
          'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; " +
            "connect-src 'self' https: http:; img-src https: http: data:; object-src 'none'; " +
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        } })
      } catch {
        return new Response('请先运行 npm run build', { status: 503 })
      }
    }
    const mime = ASSETS.get(url.pathname)
    if (request.method !== 'GET' || !mime) {
      return new Response(null, { status: 404 })
    }
    const body = await readFile(new URL(`../renderer${url.pathname}`, import.meta.url))
    return new Response(body, { headers: { 'Content-Type': mime, 'X-Content-Type-Options': 'nosniff' } })
  })
}

async function openPlatform() {
  if (platformWindow) {
    if (platformWindow.isMinimized()) platformWindow.restore()
    platformWindow.focus()
    return
  }
  platformWindow = new BrowserWindow({
    width: 1120, height: 820, minWidth: 440, minHeight: 520, autoHideMenuBar: true,
    backgroundColor: '#0a0a0a',
    webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false },
  })
  platformWindow.webContents.on('will-navigate', (event) => event.preventDefault())
  platformWindow.webContents.setWindowOpenHandler(({ url }) => {
    const target = new URL(url)
    if (['https:', 'http:'].includes(target.protocol) && !target.username && !target.password) {
      shell.openExternal(target.href).catch(() => {})
    }
    return { action: 'deny' }
  })
  platformWindow.on('closed', () => { platformWindow = null; quitIfIdle() })
  await platformWindow.loadURL(PLATFORM)
}

function destroyMini() {
  if (!miniWindow || miniWindow.isDestroyed()) return
  miniWindow.removeAllListeners('close')
  const current = miniWindow
  miniWindow = null
  current.destroy()
}

function dismissMini() {
  if (!miniWindow || miniWindow.isDestroyed()) return
  miniWindow.hide()
  if (!window && !platformWindow) destroyMini()
}

function quitIfIdle() {
  if (window || platformWindow) return
  if (miniWindow && !miniWindow.isDestroyed() && miniWindow.isVisible()) return
  destroyMini()
}

function sendMiniSelection() {
  if (!miniWindow || miniWindow.isDestroyed()) return
  miniWindow.webContents.send('mini:selection', miniSelection)
}

async function openMiniAssistant() {
  const captured = await captureSelectedText({ clipboard })
  miniSelection = captured.text ?? ''
  if (miniWindow && !miniWindow.isDestroyed()) {
    if (miniWindow.isMinimized()) miniWindow.restore()
    miniWindow.show()
    miniWindow.focus()
    sendMiniSelection()
    return
  }
  miniWindow = new BrowserWindow({
    width: 420, height: 520, minWidth: 360, minHeight: 420, show: false,
    autoHideMenuBar: true, alwaysOnTop: true, minimizable: false, maximizable: false,
    backgroundColor: '#141414', title: '划词助手',
    webPreferences: {
      preload: fileURLToPath(new URL('./mini-preload.cjs', import.meta.url)),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webviewTag: false,
    },
  })
  miniWindow.webContents.on('will-navigate', (event) => event.preventDefault())
  miniWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  miniWindow.webContents.on('before-input-event', (event, input) => {
    if (input.type === 'keyDown' && input.key === 'Escape') {
      event.preventDefault()
      dismissMini()
    }
  })
  miniWindow.on('close', (event) => {
    if (app.isQuitting) return
    event.preventDefault()
    dismissMini()
  })
  miniWindow.on('closed', () => { miniWindow = null })
  miniWindow.webContents.on('did-finish-load', () => sendMiniSelection())
  await miniWindow.loadURL(MINI)
  miniWindow.show()
  miniWindow.focus()
}

function registerCapture() {
  session.defaultSession.setDisplayMediaRequestHandler(async (request, callback) => {
    if (request.frame?.url !== PLATFORM || !request.userGesture || !request.videoRequested) {
      callback({})
      return
    }
    try {
      const sources = await desktopCapturer.getSources({
        types: ['screen', 'window'], thumbnailSize: { width: 0, height: 0 },
      })
      const targets = sources.slice(0, 12)
      if (!targets.length || !platformWindow) { callback({}); return }
      const result = await dialog.showMessageBox(platformWindow, {
        type: 'question', message: '选择要截取的屏幕或窗口',
        buttons: [...targets.map(({ name }) => name), '取消'],
        defaultId: targets.length, cancelId: targets.length,
      })
      callback(targets[result.response] ? { video: targets[result.response] } : {})
    } catch {
      callback({})
    }
  }, { useSystemPicker: true })
}

async function createWindow() {
  window = new BrowserWindow({
    width: 1110,
    height: 810,
    minWidth: 660,
    minHeight: 540,
    autoHideMenuBar: true,
    backgroundColor: '#101010',
    webPreferences: {
      preload: fileURLToPath(new URL('./preload.cjs', import.meta.url)),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webviewTag: false,
    },
  })
  window.webContents.on('will-navigate', (event) => event.preventDefault())
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.on('closed', () => { window = null; quitIfIdle() })
  await window.loadURL(ENTRY)
}

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (window?.isMinimized()) window.restore()
    window?.focus()
  })
  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit()
  })
  app.on('before-quit', () => { app.isQuitting = true })
  app.on('will-quit', () => globalShortcut.unregisterAll())
  app.whenReady().then(async () => {
    session.defaultSession.setPermissionRequestHandler((contents, permission, answer) => {
      answer(contents === platformWindow?.webContents && permission === 'display-capture')
    })
    session.defaultSession.setPermissionCheckHandler((contents, permission) => {
      return contents === platformWindow?.webContents && permission === 'display-capture'
    })
    registerCapture()
    registerAssets()
    registerHandlers()
    await createWindow()
    globalShortcut.register('Alt+Space', () => openMiniAssistant().catch(() => {}))
    app.on('activate', () => {
      if (!window) createWindow().catch(() => dialog.showErrorBox('桌面端', '无法打开窗口'))
    })
  }).catch(() => {
    dialog.showErrorBox('桌面端', '无法启动 24小时个人助理')
    app.quit()
  })
}
