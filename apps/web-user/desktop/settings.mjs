import { randomUUID } from 'node:crypto'
import { isIP } from 'node:net'
import { PathError, isInsideAuthorizedFolder, resolveLocalPath, writeAuthorizedFile } from './folders.mjs'
import { changeSettings, deleteFolder, insertFolder, listFolders, openSettingsDatabase, readSettings } from './settings-db.mjs'

export class SettingsError extends Error {}

function requiredText(value, label, limit) {
  if (typeof value !== 'string' || !value.trim() || value.trim().length > limit) {
    throw new SettingsError(`请输入有效的${label}（最多 ${limit} 字）`)
  }
  return value.trim()
}

function localAddress(hostname) {
  const host = hostname.replace(/^\[|\]$/g, '')
  if (host === 'localhost' || host === '::1' || host.endsWith('.localhost')) return true
  if (isIP(host) === 6) return /^f[cd][0-9a-f]{2}:/i.test(host)
  if (isIP(host) !== 4) return false
  const [first, second] = host.split('.').map(Number)
  return first === 10 || first === 127 || (first === 192 && second === 168) ||
    (first === 172 && second >= 16 && second <= 31)
}

export function normalizeBaseUrl(input) {
  let url
  try {
    url = new URL(requiredText(input, '接口地址', 2048))
  } catch {
    throw new SettingsError('请输入完整的接口地址，例如 http://localhost:11434/v1')
  }
  if (
    !['http:', 'https:'].includes(url.protocol) ||
    url.username || url.password || url.search || url.hash
  ) {
    throw new SettingsError('接口地址应为 HTTP(S) URL，且不能带凭据、参数或片段')
  }
  if (url.protocol === 'http:' && !localAddress(url.hostname)) {
    throw new SettingsError('请为公网供应商使用 HTTPS。内网 HTTP 可填写本地地址或私有 IP。')
  }
  const path = url.pathname.replace(/\/+$/, '')
  if (/\/(models|chat\/completions)$/i.test(path)) {
    throw new SettingsError('请填写 API 根地址，例如 https://api.example.com/v1')
  }
  return url.origin + path
}

function parseLegacy(settings) {
  if (settings?.version !== 1 || !Array.isArray(settings.providers) || !Array.isArray(settings.models)) {
    throw new SettingsError('旧模型配置格式无效')
  }
  settings.providers = settings.providers.map((row) => ({
    id: requiredText(row.id, '供应商 ID', 200),
    name: requiredText(row.name, '供应商名称', 80),
    baseUrl: normalizeBaseUrl(row.baseUrl),
    encryptedKey: row.encryptedKey ?? null,
  }))
  selectRemaining(settings)
  return settings
}

function publicSettings(settings) {
  return {
    providers: settings.providers.map(({ id, name, baseUrl, encryptedKey }) => ({
      id, name, baseUrl, hasKey: Boolean(encryptedKey),
    })),
    models: settings.models,
    selectedModelId: settings.selectedModelId,
  }
}

function ownedRecord(rows, id, type) {
  const row = rows.find((item) => item.id === id)
  if (!row) throw new SettingsError(`${type}已不存在`)
  return row
}

function selectRemaining(settings) {
  if (!settings.models.some((model) => model.id === settings.selectedModelId)) {
    settings.selectedModelId = settings.models[0]?.id ?? null
  }
}

function samePath(left, right) {
  return process.platform === 'win32' ? left.toLowerCase() === right.toLowerCase() : left === right
}

function rethrowPath(error) {
  if (error instanceof PathError || error instanceof SettingsError) {
    throw error instanceof SettingsError ? error : new SettingsError(error.message)
  }
  throw new SettingsError('无法更新本机目录授权')
}

export function createSettingsStore(file, keyStore) {
  let pendingWrite = Promise.resolve()
  let opening

  function load() {
    opening ??= openSettingsDatabase(file, parseLegacy).catch(() => {
      opening = null
      throw new SettingsError('无法读取本机模型设置或导入旧配置')
    })
    return opening
  }

  function update(mutate, prepare) {
    const change = pendingWrite.then(async () => {
      const db = await load()
      const encrypted = await prepare?.()
      try {
        const { settings, savedId } = changeSettings(db, (draft) => mutate(draft, encrypted))
        return { settings: publicSettings(settings), savedId }
      } catch (error) {
        if (error instanceof SettingsError) throw error
        throw new SettingsError('无法保存本机模型设置')
      }
    })
    pendingWrite = change.catch(() => {})
    return change
  }

  function queued(work) {
    const change = pendingWrite.then(async () => work(await load()))
    pendingWrite = change.catch(() => {})
    return change
  }

  return {
    async list() {
      await pendingWrite
      return publicSettings(readSettings(await load()))
    },

    async close() {
      await pendingWrite
      if (opening) (await opening).close()
      opening = null
    },

    saveProvider(input) {
      return update((settings, ready) => {
        const { name, baseUrl, encryptedKey } = ready
        const existing = input.id ? ownedRecord(settings.providers, input.id, '供应商') : null
        const provider = {
          id: existing?.id ?? randomUUID(),
          name,
          baseUrl,
          encryptedKey: null,
        }
        if (encryptedKey) {
          provider.encryptedKey = encryptedKey
        } else if (!input.clearKey && existing?.baseUrl === baseUrl) {
          provider.encryptedKey = existing.encryptedKey
        }
        if (existing) Object.assign(existing, provider)
        else settings.providers.push(provider)
        return provider.id
      }, async () => {
        const name = requiredText(input.name, '供应商名称', 80)
        const baseUrl = normalizeBaseUrl(input.baseUrl)
        const apiKey = typeof input.apiKey === 'string' ? input.apiKey.trim() : ''
        if (apiKey.length > 4096 || /[^\x21-\x7e]/.test(apiKey)) {
          throw new SettingsError('API Key 包含无效字符或超过长度限制')
        }
        return { name, baseUrl, encryptedKey: apiKey ? await keyStore.encrypt(apiKey) : null }
      })
    },

    deleteProvider(id) {
      return update((settings) => {
        ownedRecord(settings.providers, id, '供应商')
        settings.providers = settings.providers.filter((provider) => provider.id !== id)
        settings.models = settings.models.filter((model) => model.providerId !== id)
        selectRemaining(settings)
      })
    },

    saveModel(input) {
      return update((settings) => {
        const provider = ownedRecord(settings.providers, input.providerId, '供应商')
        const modelCode = requiredText(input.modelCode, '模型 ID', 200)
        const name = typeof input.name === 'string' && input.name.trim()
          ? requiredText(input.name, '模型名称', 80)
          : modelCode
        const existing = input.id ? ownedRecord(settings.models, input.id, '模型') : null
        const duplicate = settings.models.some((model) =>
          model.providerId === provider.id && model.modelCode === modelCode && model.id !== existing?.id)
        if (duplicate) throw new SettingsError('该供应商已配置这个模型 ID')
        const model = { id: existing?.id ?? randomUUID(), providerId: provider.id, modelCode, name }
        if (existing) Object.assign(existing, model)
        else settings.models.push(model)
        selectRemaining(settings)
        return model.id
      })
    },

    deleteModel(id) {
      return update((settings) => {
        ownedRecord(settings.models, id, '模型')
        settings.models = settings.models.filter((model) => model.id !== id)
        selectRemaining(settings)
      })
    },

    selectModel(id) {
      return update((settings) => {
        ownedRecord(settings.models, id, '模型')
        settings.selectedModelId = id
      })
    },

    async connection(id, kind) {
      await pendingWrite
      const settings = readSettings(await load())
      const model = kind === 'model' ? ownedRecord(settings.models, id, '模型') : null
      const provider = ownedRecord(settings.providers, model?.providerId ?? id, '供应商')
      const { baseUrl, encryptedKey } = provider
      return {
        baseUrl,
        modelCode: model?.modelCode,
        apiKey: encryptedKey ? await keyStore.decrypt(encryptedKey) : '',
      }
    },

    async listFolders() {
      await pendingWrite
      return listFolders(await load())
    },

    addFolder(input) {
      return queued((db) => {
        try {
          const folder = resolveLocalPath(input)
          const folders = listFolders(db)
          if (folders.some((row) => samePath(row.path, folder))) {
            throw new SettingsError('该目录已在白名单中')
          }
          const id = randomUUID()
          insertFolder(db, id, folder)
          return { id, path: folder, folders: listFolders(db) }
        } catch (error) {
          rethrowPath(error)
        }
      })
    },

    removeFolder(id) {
      return queued((db) => {
        if (typeof id !== 'string' || !id) throw new SettingsError('目录不存在')
        if (!deleteFolder(db, id)) throw new SettingsError('目录不存在')
        return { folders: listFolders(db) }
      })
    },

    writeLocalFile(input, confirm) {
      if (typeof confirm !== 'function') throw new SettingsError('写入前必须确认')
      return queued(async (db) => {
        const folders = listFolders(db).map((row) => row.path)
        try {
          return await writeAuthorizedFile(folders, input, confirm)
        } catch (error) {
          if (error instanceof PathError || error instanceof SettingsError) {
            throw error instanceof SettingsError ? error : new SettingsError(error.message)
          }
          throw new SettingsError('无法写入本地文件')
        }
      })
    },
  }
}

export { isInsideAuthorizedFolder, resolveLocalPath }
