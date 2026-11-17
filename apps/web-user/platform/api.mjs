export class ApiError extends Error {
  constructor(message, code, status) {
    super(message)
    this.code = code
    this.status = status
  }
}

export async function api(path, { method = 'GET', body, jwt, signal } = {}) {
  const headers = {}
  if (jwt) headers.Authorization = 'Bearer ' + jwt
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const response = await fetch('/api' + path, {
    method, headers, signal, body: body === undefined ? undefined : JSON.stringify(body),
  })
  const envelope = await response.json().catch(() => ({}))
  if (!response.ok || envelope.code !== 0) {
    throw new ApiError(envelope.message || '服务暂不可用', envelope.code, response.status)
  }
  return envelope.data
}

export async function* readSse(body) {
  if (!body) throw new Error('未收到事件流')
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let event = 'message'
  let id = ''
  let data = []
  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += done ? decoder.decode() + '\n\n' : decoder.decode(value, { stream: true })
      if (buffer.length > 1000000) throw new Error('事件过大')
      let newline
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline).replace(/\r$/, '')
        buffer = buffer.slice(newline + 1)
        if (!line) {
          if (data.length) yield { id, event, data: JSON.parse(data.join('\n')) }
          event = 'message'
          id = ''
          data = []
          continue
        }
        const value = line.slice(line.indexOf(':') + 1).replace(/^ /, '')
        if (line.startsWith('id:')) id = value
        else if (line.startsWith('event:')) event = value
        else if (line.startsWith('data:')) data.push(value)
      }
      if (done) return
    }
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

export async function streamTurn(payload, getToken, receive, signal) {
  let cursor = ''
  const seen = new Set()
  for (let attempt = 0; attempt < 4; attempt++) {
    const query = new URLSearchParams({ sessionId: payload.sessionId })
    if (cursor) query.set('lastEventId', cursor)
    const path = attempt ? '/resume?' + query : '/send'
    try {
      const headers = { Authorization: 'Bearer ' + await getToken() }
      if (!attempt) headers['Content-Type'] = 'application/json'
      const response = await fetch('/api/chat/stream' + path, {
        method: attempt ? 'GET' : 'POST', headers, signal,
        body: attempt ? undefined : JSON.stringify(payload),
      })
      if (!response.ok) {
        const value = await response.json().catch(() => ({}))
        throw new ApiError(value.message || '连接失败', value.code, response.status)
      }
      for await (const frame of readSse(response.body)) {
        if (frame.id && seen.has(frame.id)) continue
        if (frame.id) {
          cursor = frame.id
          seen.add(frame.id)
        }
        receive(frame)
        if (frame.event === 'done' || frame.event === 'error') return
      }
    } catch (error) {
      if (signal.aborted || (error instanceof ApiError && error.status < 500)) throw error
    }
    if (attempt < 3) await new Promise((resolve) => setTimeout(resolve, 300 * (attempt + 1)))
  }
  throw new Error('连接中断，请刷新会话查看结果')
}

export async function upload(file, sessionId, jwt) {
  const signed = await api('/file/presignUpload', { method: 'POST', jwt, body: {
    sessionId, fileName: file.name, fileSize: file.size, mimeType: file.type || 'text/plain',
  } })
  const headers = Object.fromEntries(Object.entries(signed.headers).filter(
    ([key]) => key.toLowerCase() !== 'content-length',
  ))
  const response = await fetch(signed.uploadUrl, { method: signed.method, headers, body: file })
  if (!response.ok) throw new Error('文件上传失败')
  return api('/file/confirmUpload', { method: 'POST', jwt, body: { fileId: signed.fileId } })
}

export function safeUrl(value) {
  try {
    const url = new URL(value)
    if (['https:', 'http:'].includes(url.protocol) && !url.username && !url.password) return url.href
  } catch {}
  return ''
}
