import { TOOLS } from './tools.mjs'

export function emptyUsage() {
  return { inputTokens: 0, cachedTokens: 0, outputTokens: 0, estimated: false }
}

export async function* dataFrames(body) {
  const decoder = new TextDecoder()
  let buffer = ''
  let data = []
  for await (const bytes of body) {
    buffer += decoder.decode(bytes, { stream: true })
    if (buffer.length > 1000000) throw new Error('Malformed upstream stream')
    let newline
    while ((newline = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, newline).replace(/\r$/, '')
      buffer = buffer.slice(newline + 1)
      if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
      else if (!line && data.length) {
        yield data.join('\n')
        data = []
      }
    }
  }
  if (data.length) yield data.join('\n')
}

export async function runGateway(turn, services, emit, env, signal) {
  const url = (env.HIGRESS_GATEWAY_URL || 'http://127.0.0.1:8080').replace(/\/$/, '') +
    '/v1/chat/completions'
  const headers = { 'content-type': 'application/json', 'X-Turn-Id': turn.turnId,
    'X-User-Id': String(turn.userId), ...(turn.traceparent && { traceparent: turn.traceparent }) }
  if (env.HIGRESS_GATEWAY_KEY) headers.Authorization = 'Bearer ' + env.HIGRESS_GATEWAY_KEY
  const usage = emptyUsage()
  const messages = [{ role: 'system', content: await services.context() }, ...turn.messages]
  for (let step = 0; step < 7; step++) {
    const started = Date.now()
    const response = await fetch(url, {
      method: 'POST', headers, signal, redirect: 'error', body: JSON.stringify({
        model: turn.modelCode, messages, max_tokens: turn.maxTokens, stream: true,
        stream_options: { include_usage: true },
        ...(step < 6 ? { tools: TOOLS.map((definition) => ({ type: 'function', function: definition })) } : {}),
      }),
    })
    if (!response.ok || !response.body) throw new Error('Higress Agent request failed')
    let content = ''
    let reasoning = false
    let finished = false
    const calls = new Map()
    for await (const frame of dataFrames(response.body)) {
      if (frame === '[DONE]') { finished = true; break }
      const value = JSON.parse(frame)
      if (value.error) throw new Error('Higress Agent response failed')
      if (value.usage) {
        usage.inputTokens += value.usage.prompt_tokens || 0
        usage.cachedTokens += value.usage.prompt_tokens_details?.cached_tokens ||
          value.usage.prompt_cache_hit_tokens || 0
        usage.outputTokens += value.usage.completion_tokens || 0
        emit('usage', usage)
      }
      const choice = value.choices?.[0]
      if (!choice) continue
      if (choice.finish_reason) finished = true
      const delta = choice.delta || {}
      if (delta.reasoning_content && !reasoning) {
        reasoning = true
        emit('thinking', { durationMs: 0, finished: false })
      }
      if (typeof delta.content === 'string' && delta.content) {
        content += delta.content
        if (content.length > 96000) throw new Error('Agent response too large')
        emit('delta', { text: delta.content })
      }
      for (const call of delta.tool_calls || []) {
        const index = call.index ?? 0
        const current = calls.get(index) || { id: '', type: 'function',
          function: { name: '', arguments: '' } }
        current.id += call.id || ''
        current.function.name += call.function?.name || ''
        current.function.arguments += call.function?.arguments || ''
        if (current.function.arguments.length > 64000 || calls.size > 6) throw new Error('Tool call too large')
        calls.set(index, current)
      }
    }
    if (!finished) throw new Error('Incomplete Agent response')
    if (reasoning) emit('thinking', { durationMs: Date.now() - started, finished: true })
    if (!calls.size) {
      if (!content.trim()) throw new Error('Empty Agent response')
      return usage
    }
    if (step === 6) throw new Error('Tool step limit reached')
    messages.push({ role: 'assistant', content: content || null, tool_calls: [...calls.values()] })
    for (const call of calls.values()) {
      let result
      try { result = await services.call(call.function.name, JSON.parse(call.function.arguments), call.id) }
      catch (error) {
        if (signal.aborted) throw error
        result = { error: 'Tool call was rejected' }
      }
      messages.push({ role: 'tool', tool_call_id: call.id, content: JSON.stringify(result) })
    }
  }
  throw new Error('Agent step limit reached')
}
