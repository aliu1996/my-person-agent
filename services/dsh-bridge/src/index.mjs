import { randomBytes } from 'node:crypto'
import http from 'node:http'
import { fileURLToPath } from 'node:url'
import { runDsh } from './dsh.mjs'
import { runGateway } from './gateway.mjs'
import { MiB, TOKEN_HEADER, readBody, respond, serviceToken, sse, tokenMatches } from './http.mjs'
import { agentServices } from './tools.mjs'

const UUID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/

function validTurn(turn) {
  return UUID.test(turn?.turnId || '') &&
    Number.isSafeInteger(turn.userId) && turn.userId > 0 &&
    Number.isSafeInteger(turn.sessionId) && turn.sessionId > 0 &&
    typeof turn.modelCode === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$/.test(turn.modelCode) &&
    Number.isInteger(turn.maxTokens) && turn.maxTokens > 0 && turn.maxTokens <= 8192 &&
    Array.isArray(turn.messages) && turn.messages.length <= 60 && turn.messages.every((message) =>
      ['system', 'assistant', 'user'].includes(message?.role) && typeof message.content === 'string') &&
    turn.permissions?.hostTools === false && turn.permissions?.confirmationRequired === true
}

export function createServer(env = process.env, engines = { dsh: runDsh, gateway: runGateway }) {
  const token = serviceToken(env)
  const engine = env.DSH_ENGINE || (env.DSH_BIN ? 'dsh' : 'gateway')
  const relays = new Map()
  return http.createServer(async (request, response) => {
    if (request.method === 'GET' && request.url === '/healthz') {
      return respond(response, 200, { code: 0, message: 'ok',
        data: { service: 'dsh-bridge', status: 'healthy', engine }, traceId: '' })
    }
    if (request.method === 'POST' && request.url?.startsWith('/internal/dsh/tool/')) {
      const relay = relays.get(request.url.split('/').at(-1))
      if (!relay || !tokenMatches(request.headers.authorization, relay.key)) {
        return respond(response, 401, { error: 'Unauthorized' })
      }
      try {
        const { name, args, stepId } = await readBody(request, 80 * 1024)
        return respond(response, 200, await relay.services.call(name, args, stepId))
      } catch {
        return respond(response, 400, { error: 'Tool request rejected' })
      }
    }
    if (request.method !== 'POST' || request.url !== '/internal/dsh/turn') {
      return respond(response, 404, { error: 'Unknown endpoint' })
    }
    if (!tokenMatches(request.headers[TOKEN_HEADER], token)) {
      return respond(response, 401, { error: 'Unauthorized' })
    }
    let turn
    try { turn = await readBody(request, MiB) } catch {
      return respond(response, 400, { error: 'Invalid turn' })
    }
    if (!validTurn(turn)) return respond(response, 400, { error: 'Invalid turn' })
    const trace = request.headers.traceparent
    if (typeof trace === 'string' && /^00-[a-f0-9]{32}-[a-f0-9]{16}-[a-f0-9]{2}$/.test(trace)) {
      turn.traceparent = trace
    }
    if (!Object.hasOwn(engines, engine) || relays.has(turn.turnId) || relays.size >= 32) {
      return respond(response, 503, { error: 'Agent busy or unavailable' })
    }
    const controller = new AbortController()
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(765000)])
    response.on('close', () => controller.abort())
    response.writeHead(200, {
      'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no',
    })
    response.flushHeaders()
    const emit = (event, data) => sse(response, event, { ...data, turnId: turn.turnId })
    const heartbeat = setInterval(() => emit('ping', {}), 10000)
    const services = agentServices(turn, emit, env, token, signal)
    const relay = { services, key: randomBytes(32).toString('hex'),
      url: 'http://127.0.0.1:' + request.socket.localPort + '/internal/dsh/tool/' + turn.turnId }
    relays.set(turn.turnId, relay)
    let status = 'failed'
    try {
      const usage = await engines[engine](turn, services, emit, env, signal, relay)
      await services.finish()
      emit('done', { usage, ...services.stats })
      status = 'done'
    } catch {
      emit('error', { code: 5000, message: 'Agent execution unavailable', retryable: true })
    } finally {
      console.info(JSON.stringify({ service: 'dsh-bridge', trace_id: turn.traceparent?.slice(3, 35),
        turn_id: turn.turnId, user_id: turn.userId, status }))
      relays.delete(turn.turnId)
      clearInterval(heartbeat)
      response.end()
    }
  })
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  createServer().listen(Number(process.env.DSH_BRIDGE_PORT || 8103),
    process.env.DSH_BRIDGE_HOST || '127.0.0.1')
}
