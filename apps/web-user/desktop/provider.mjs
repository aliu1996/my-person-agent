import { SettingsError } from './settings.mjs'

const RESPONSE_LIMIT = 2 * 1024 * 1024

async function readJson(response) {
  const reader = response.body?.getReader()
  if (!reader) throw new SettingsError('供应商返回了空响应')
  const decoder = new TextDecoder()
  let size = 0
  let json = ''
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) return JSON.parse(json + decoder.decode())
      size += value.byteLength
      if (size > RESPONSE_LIMIT) {
        await reader.cancel()
        throw new SettingsError('供应商响应超过大小限制')
      }
      json += decoder.decode(value, { stream: true })
    }
  } finally {
    reader.releaseLock()
  }
}

function redact(text, key) {
  return key ? text.replaceAll(key, '••••') : text
}

async function request(connection, resource, payload, fetcher) {
  const { baseUrl, apiKey } = connection
  const headers = { Accept: 'application/json' }
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`
  if (payload) headers['Content-Type'] = 'application/json'
  const timeout = AbortSignal.timeout(payload ? 90_000 : 12_000)
  try {
    const response = await fetcher(`${baseUrl}/${resource}`, {
      method: payload ? 'POST' : 'GET',
      headers,
      body: payload ? JSON.stringify(payload) : undefined,
      redirect: 'error',
      signal: timeout,
    })
    if (response.ok) return await readJson(response)
    await response.body?.cancel()
    if (response.status === 401 || response.status === 403) {
      throw new SettingsError('供应商拒绝了请求，请检查 API Key 和模型权限')
    }
    if (response.status === 404 && resource === 'models') {
      throw new SettingsError('供应商未提供模型列表，可以手动输入模型 ID')
    }
    if (response.status === 429) {
      throw new SettingsError('供应商请求已达限额，请检查配额或稍后重试')
    }
    throw new SettingsError(`供应商请求失败（HTTP ${response.status}）`)
  } catch (error) {
    if (error instanceof SettingsError) throw error
    if (timeout.aborted) throw new SettingsError('供应商响应超时，请稍后重试')
    if (error instanceof SyntaxError) throw new SettingsError('供应商未返回兼容的 JSON 响应')
    throw new SettingsError('无法连接供应商，请检查接口地址和网络')
  }
}

export async function fetchModels(connection, fetcher = fetch) {
  const payload = await request(connection, 'models', null, fetcher)
  if (!Array.isArray(payload?.data)) throw new SettingsError('供应商未返回兼容的模型列表')
  const models = payload.data.map((item) => item?.id).filter((id) =>
    typeof id === 'string' && id.length && id.length <= 200 &&
    (!connection.apiKey || !id.includes(connection.apiKey)))
  return [...new Set(models)].slice(0, 500)
}

export async function chat(connection, messages, fetcher = fetch) {
  if (!Array.isArray(messages) || messages.length < 1 || messages.length > 31 ||
      messages.at(-1)?.role !== 'user') {
    throw new SettingsError('对话太长或消息格式无效，请开启新对话')
  }
  const cleanMessages = messages.map(({ role, content }) => {
    if (!['user', 'assistant'].includes(role) || typeof content !== 'string' ||
        content.trim() === '' || content.length > 40_000) {
      throw new SettingsError('对话包含无效或过长的消息')
    }
    return { role, content }
  })
  const payload = await request(connection, 'chat/completions', {
    model: connection.modelCode,
    messages: cleanMessages,
    stream: false,
  }, fetcher)
  const reply = payload?.choices?.[0]?.message?.content
  if (typeof reply !== 'string' || !reply.trim()) {
    throw new SettingsError('供应商未返回文本回复，请检查模型是否支持对话')
  }
  return redact(reply.slice(0, 40_000), connection.apiKey)
}
