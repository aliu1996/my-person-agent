(() => {
  'use strict'

  const $ = (id) => document.getElementById(id)
  const bridge = window.desktopModels
  const foldersBridge = window.desktopFolders
  let folders = []
  let settings = { providers: [], models: [], selectedModelId: null }
  let providerId = null
  let editingModelId = null
  let settingsBusy = false
  let sending = false
  let switching = false
  const threads = new Map()

  function status(id, text = '', state = '') {
    const element = $(id)
    element.className = ['status', state].filter(Boolean).join(' ')
    element.textContent = text
  }

  async function invoke(method, input) {
    if (!bridge) throw new Error('请在 Electron 桌面端打开此页面')
    const result = await bridge[method](input)
    if (!result.ok) throw new Error(result.error)
    return result.data
  }

  async function invokeFolders(method, input) {
    if (!foldersBridge) throw new Error('请在 Electron 桌面端打开此页面')
    const result = await foldersBridge[method](input)
    if (!result.ok) throw new Error(result.error)
    return result.data
  }

  function showSettingsTab(tab) {
    const foldersView = tab === 'folders'
    $('modelsTab').setAttribute('aria-selected', String(!foldersView))
    $('foldersTab').setAttribute('aria-selected', String(foldersView))
    $('settingsModelsView').hidden = foldersView
    $('settingsFoldersView').hidden = !foldersView
    $('settingsTitle').textContent = foldersView ? '本机目录' : '模型'
    if (foldersView) loadFolders()
  }

  function renderFolders() {
    $('folderEmpty').hidden = folders.length > 0
    $('folderList').replaceChildren(...folders.map((folder) => {
      const item = element('li', undefined, 'model-row')
      const details = element('div', undefined, 'model-info')
      details.append(element('span', folder.path, 'folder-path'))
      const remove = button('移除', 'text-button danger', () => {
        withSettings('folderStatus', async () => {
          folders = (await invokeFolders('remove', folder.id)).folders
          renderFolders()
          status('folderStatus', '目录已移除', 'success')
        })
      })
      item.append(details, remove)
      return item
    }))
  }

  async function loadFolders() {
    if (!foldersBridge) {
      status('folderStatus', '请在 Electron 桌面端打开此页面', 'error')
      return
    }
    try {
      folders = await invokeFolders('list')
      renderFolders()
    } catch (error) {
      status('folderStatus', error.message, 'error')
    }
  }

  function threadKey(model) {
    const provider = settings.providers.find((item) => item.id === model.providerId)
    return JSON.stringify([model.id, provider?.baseUrl, model.modelCode])
  }

  function accept(next) {
    settings = next
    const valid = new Set(settings.models.map(threadKey))
    for (const key of threads.keys()) {
      if (!valid.has(key)) threads.delete(key)
    }
    renderChat()
  }

  function activeModel() {
    return settings.models.find((model) => model.id === settings.selectedModelId)
  }

  function element(tag, text, className) {
    const node = document.createElement(tag)
    if (text !== undefined) node.textContent = text
    if (className) node.className = className
    return node
  }

  function button(label, className, handler) {
    const control = element('button', label, className)
    control.type = 'button'
    control.disabled = settingsBusy
    control.addEventListener('click', handler)
    return control
  }

  function renderChat() {
    const model = activeModel()
    const busy = sending || switching
    const choices = settings.models.map((entry) => {
      const supplier = settings.providers.find((item) => item.id === entry.providerId)
      return new Option(`${entry.name} · ${supplier?.name ?? ''}`, entry.id)
    })
    $('modelSelect').replaceChildren(new Option('选择模型', ''), ...choices)
    $('modelSelect').value = model?.id ?? ''
    $('modelSelect').disabled = !choices.length || busy
    $('newChat').disabled = !model || busy
    $('settingsNav').disabled = busy
    $('prompt').disabled = !model || busy
    $('send').disabled = !model || busy
    $('send').textContent = sending ? '发送中…' : '发送'

    const history = model ? threads.get(threadKey(model)) ?? [] : []
    $('empty').hidden = Boolean(history.length)
    $('messages').hidden = !history.length
    $('emptyTitle').textContent = model ? '你想聊些什么？' : '先添加一个模型'
    $('emptyHint').textContent = model ? '' : '在设置里配置供应商，添加模型后即可开始对话。'
    $('emptyHint').hidden = Boolean(model)
    $('emptySettings').hidden = Boolean(model)
    $('messages').replaceChildren(...history.map((message) => {
      const item = element('article', undefined, `message ${message.role}`)
      item.append(element('strong', message.role === 'user' ? '你' : model.name),
        element('p', message.content))
      return item
    }))
    $('conversation').scrollTop = $('conversation').scrollHeight
  }

  function currentProvider() {
    return settings.providers.find((item) => item.id === providerId)
  }

  function renderProviderList() {
    $('providerList').replaceChildren(...settings.providers.map((provider) => {
      const item = element('li')
      const control = button('', 'provider-item', () => showProvider(provider.id))
      control.append(element('span', provider.name), element('small', new URL(provider.baseUrl).host))
      control.setAttribute('aria-pressed', String(provider.id === providerId))
      item.append(control)
      return item
    }))
  }

  function editModel(model = null) {
    editingModelId = model?.id ?? null
    $('modelName').value = model?.name ?? ''
    $('modelCode').value = model?.modelCode ?? ''
    $('saveModel').textContent = model ? '保存模型' : '添加模型'
    $('cancelModelEdit').hidden = !model
  }

  function renderModelList() {
    const models = settings.models.filter((model) => model.providerId === providerId)
    $('configuredModels').replaceChildren(...models.map((model) => {
      const item = element('li', undefined, 'model-row')
      const details = element('div', undefined, 'model-info')
      details.append(element('span', model.name), element('small', model.modelCode))
      const actions = element('div', undefined, 'model-actions')
      const selected = settings.selectedModelId === model.id
      const select = button(selected ? '当前模型' : '设为当前', 'text-button', () => {
        withSettings('modelStatus', async () => {
          accept((await invoke('selectModel', model.id)).settings)
          status('modelStatus', '已切换当前模型', 'success')
        })
      })
      select.disabled = selected || settingsBusy
      const edit = button('编辑', 'text-button', () => {
        editModel(model)
        status('modelStatus')
        $('modelCode').focus()
      })
      const remove = button('删除', 'text-button danger', () => {
        withSettings('modelStatus', async () => {
          accept((await invoke('deleteModel', model.id)).settings)
          if (editingModelId === model.id) editModel()
          status('modelStatus', '模型已删除', 'success')
        })
      })
      actions.append(select, edit, remove)
      item.append(details, actions)
      return item
    }))
  }

  function showProvider(id) {
    providerId = id
    const provider = currentProvider()
    renderProviderList()
    $('providerTitle').textContent = provider ? '供应商配置' : '添加供应商'
    $('providerName').value = provider?.name ?? ''
    $('baseUrl').value = provider?.baseUrl ?? ''
    $('apiKey').value = ''
    $('apiKey').placeholder = provider?.hasKey ? '留空保留当前 Key' : '按供应商要求填写'
    $('keyStatus').textContent = provider?.hasKey ? '已保存' : ''
    $('clearKeyRow').hidden = !provider?.hasKey
    $('clearKey').checked = false
    $('deleteProvider').hidden = !provider
    $('modelsSection').hidden = !provider
    $('discoveredRow').hidden = true
    $('discoveredModels').replaceChildren()
    status('providerStatus')
    status('modelStatus')
    editModel()
    renderModelList()
  }

  function controls() {
    $('providerFields').disabled = settingsBusy || !bridge
    $('modelFields').disabled = settingsBusy || !bridge
    $('fetchModels').disabled = settingsBusy || !bridge
    $('addProvider').disabled = settingsBusy || !bridge
    $('closeSettings').disabled = settingsBusy
    $('addFolder').disabled = settingsBusy || !foldersBridge
    $('modelsTab').disabled = settingsBusy
    $('foldersTab').disabled = settingsBusy
    renderProviderList()
    renderModelList()
  }

  async function withSettings(statusId, execute) {
    if (settingsBusy) return
    settingsBusy = true
    status(statusId)
    controls()
    try {
      await execute()
    } catch (error) {
      status(statusId, error.message, 'error')
    } finally {
      settingsBusy = false
      controls()
    }
  }

  function openSettings() {
    if (sending || $('settingsDialog').open) return
    showProvider(currentProvider()?.id ?? settings.providers[0]?.id ?? null)
    showSettingsTab('models')
    controls()
    $('settingsDialog').showModal()
    if (!bridge) status('providerStatus', '请在 Electron 桌面端打开此页面', 'error')
  }

  $('settingsNav').addEventListener('click', openSettings)
  $('platformNav').addEventListener('click', async () => {
    try { await invoke('openPlatform') } catch (error) { status('chatStatus', error.message, 'error') }
  })
  $('emptySettings').addEventListener('click', openSettings)
  $('closeSettings').addEventListener('click', () => $('settingsDialog').close())
  $('settingsDialog').addEventListener('cancel', (event) => {
    if (settingsBusy) event.preventDefault()
  })
  $('settingsDialog').addEventListener('close', () => { $('apiKey').value = '' })
  $('addProvider').addEventListener('click', () => {
    showProvider(null)
    $('providerName').focus()
  })
  $('providerForm').addEventListener('submit', (event) => {
    event.preventDefault()
    withSettings('providerStatus', async () => {
      const previous = currentProvider()
      const result = await invoke('saveProvider', {
        id: providerId,
        name: $('providerName').value,
        baseUrl: $('baseUrl').value,
        apiKey: $('apiKey').value,
        clearKey: $('clearKey').checked,
      })
      accept(result.settings)
      showProvider(result.savedId)
      const cleared = previous?.hasKey && !currentProvider().hasKey &&
        previous.baseUrl !== currentProvider().baseUrl
      status('providerStatus', cleared ? '地址已更新，原 API Key 已清除。' : '供应商已保存。', 'success')
    })
  })
  $('deleteProvider').addEventListener('click', () => {
    const provider = currentProvider()
    if (!provider || !window.confirm(`删除「${provider.name}」及其配置的模型？`)) return
    withSettings('providerStatus', async () => {
      accept((await invoke('deleteProvider', provider.id)).settings)
      showProvider(settings.providers[0]?.id ?? null)
      status('providerStatus', '供应商已删除。', 'success')
    })
  })
  $('fetchModels').addEventListener('click', () => {
    withSettings('modelStatus', async () => {
      status('modelStatus', '正在连接供应商…')
      $('discoveredRow').hidden = true
      const models = await invoke('fetchModels', providerId)
      $('discoveredModels').replaceChildren(new Option('选择模型', ''),
        ...models.map((id) => new Option(id, id)))
      $('discoveredRow').hidden = !models.length
      status('modelStatus', models.length
        ? `已获取 ${models.length} 个模型，可以选择或手动填写 ID。`
        : '供应商返回了空列表，可以手动填写模型 ID。', models.length ? 'success' : '')
    })
  })
  $('discoveredModels').addEventListener('change', () => {
    if ($('discoveredModels').value) $('modelCode').value = $('discoveredModels').value
  })
  $('modelForm').addEventListener('submit', (event) => {
    event.preventDefault()
    withSettings('modelStatus', async () => {
      accept((await invoke('saveModel', {
        id: editingModelId,
        providerId,
        modelCode: $('modelCode').value,
        name: $('modelName').value,
      })).settings)
      editModel()
      status('modelStatus', '模型已保存。', 'success')
    })
  })
  $('cancelModelEdit').addEventListener('click', () => editModel())

  $('modelSelect').addEventListener('change', async () => {
    const id = $('modelSelect').value
    if (!id) {
      renderChat()
      return
    }
    switching = true
    renderChat()
    try {
      accept((await invoke('selectModel', id)).settings)
      status('chatStatus')
    } catch (error) {
      status('chatStatus', error.message, 'error')
    } finally {
      switching = false
      renderChat()
    }
  })
  $('newChat').addEventListener('click', () => {
    const model = activeModel()
    if (!model || sending) return
    threads.delete(threadKey(model))
    $('prompt').value = ''
    status('chatStatus')
    renderChat()
    $('prompt').focus()
  })
  $('prompt').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault()
      $('chatForm').requestSubmit()
    }
  })
  $('chatForm').addEventListener('submit', async (event) => {
    event.preventDefault()
    const model = activeModel()
    const content = $('prompt').value.trim()
    if (!model || !content || sending || switching) return
    const key = threadKey(model)
    const history = threads.get(key) ?? []
    const messages = [...history, { role: 'user', content }]
    if (messages.length > 31) {
      status('chatStatus', '这段对话已达长度上限，请开启新对话。', 'error')
      return
    }
    threads.set(key, messages)
    sending = true
    $('prompt').value = ''
    status('chatStatus', '正在等待模型回复…')
    renderChat()
    try {
      const reply = await invoke('chat', { modelId: model.id, messages })
      threads.set(key, [...messages, { role: 'assistant', content: reply }])
      status('chatStatus')
    } catch (error) {
      threads.set(key, history)
      $('prompt').value = content
      status('chatStatus', error.message, 'error')
    } finally {
      sending = false
      renderChat()
      $('prompt').focus()
    }
  })

  renderChat()
  if (bridge) invoke('list').then(accept).catch((error) => status('chatStatus', error.message, 'error'))
})()
