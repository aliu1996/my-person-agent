(() => {
  'use strict'

  const $ = (id) => document.getElementById(id)
  const bridge = window.miniAssistant
  let sending = false

  function status(text = '', state = '') {
    $('status').className = ['status', state].filter(Boolean).join(' ')
    $('status').textContent = text
  }

  function setExcerpt(text) {
    const excerpt = $('excerpt')
    const has = Boolean(text && text.trim())
    excerpt.classList.toggle('empty', !has)
    excerpt.textContent = has ? text : '未检测到选中文本，仍可以向当前模型提问。'
  }

  function addMessage(role, content) {
    $('thread').hidden = false
    const item = document.createElement('article')
    item.className = `message ${role}`
    const label = document.createElement('strong')
    label.textContent = role === 'user' ? '你' : '助理'
    const body = document.createElement('p')
    body.textContent = content
    item.append(label, body)
    $('thread').append(item)
    $('thread').scrollTop = $('thread').scrollHeight
  }

  async function invoke(method, input) {
    if (!bridge) throw new Error('请在 Electron 桌面端打开划词助手')
    const result = await bridge[method](input)
    if (!result.ok) throw new Error(result.error)
    return result.data
  }

  $('close').addEventListener('click', () => invoke('close').catch(() => window.close()))
  $('openPlatform').addEventListener('click', async () => {
    try {
      await invoke('openPlatform')
      status()
    } catch (error) {
      status(error.message, 'error')
    }
  })
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !event.isComposing) {
      event.preventDefault()
      invoke('close').catch(() => window.close())
    }
  })
  $('prompt').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault()
      $('askForm').requestSubmit()
    }
  })
  $('askForm').addEventListener('submit', async (event) => {
    event.preventDefault()
    const prompt = $('prompt').value.trim()
    if (!prompt || sending) return
    sending = true
    $('send').disabled = true
    $('send').textContent = '发送中…'
    $('prompt').value = ''
    addMessage('user', prompt)
    status('正在等待模型回复…')
    try {
      const { reply } = await invoke('ask', { prompt })
      addMessage('assistant', reply)
      status()
    } catch (error) {
      $('prompt').value = prompt
      status(error.message, 'error')
    } finally {
      sending = false
      $('send').disabled = false
      $('send').textContent = '发送'
      $('prompt').focus()
    }
  })

  if (bridge?.onSelection) bridge.onSelection(setExcerpt)
  $('prompt').focus()
})()
