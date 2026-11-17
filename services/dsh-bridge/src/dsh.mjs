import { mkdtemp, realpath, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { isAbsolute, join, relative } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { emptyUsage } from './gateway.mjs'

const DISABLED = [
  'deepseek-llm-api-extensions', 'session-log-deepseek', 'plugin-package-inventory-deepseek',
  'llm-deepseek', 'tool-fs', 'tool-working-directory', 'persistent-bash', 'persistent-pwsh',
]

export function profilePatch(turn, system, gateway) {
  return [
    ...DISABLED.map((id) => ({ id, disabled: true })),
    { id: 'system-prompt', config: {
      includeHarnessIdentity: false, includeRuntimeContext: false, personaPrefix: system,
    } },
    { insert: [
      { id: 'pa-gateway', name: '@deepseek-ai/dsh-llm-pi-ai', config: { providers: {
        'pa-higress': { apiKeyEnv: 'PA_GATEWAY_KEY', api: 'openai-completions', baseURL: gateway,
          headers: { 'X-Turn-Id': turn.turnId, 'X-User-Id': String(turn.userId),
            ...(turn.traceparent && { traceparent: turn.traceparent }) },
          models: [{ id: turn.modelCode }] },
      } } },
      { id: 'pa-tool-relay', name: fileURLToPath(new URL('./dsh-tools.mjs', import.meta.url)) },
    ] },
  ]
}

export async function runDsh(turn, services, emit, env, signal, relay) {
  if (!env.DSH_SDK_MODULE || !env.DSH_BIN) throw new Error('Configure the DSH SDK runtime')
  const imported = isAbsolute(env.DSH_SDK_MODULE) ? pathToFileURL(env.DSH_SDK_MODULE).href :
    env.DSH_SDK_MODULE
  const { DeepSeekHarness } = await import(imported)
  const base = await realpath(tmpdir())
  const work = await mkdtemp(join(base, 'pa-dsh-'))
  let harness
  const cancel = () => harness?.close().catch(() => {})
  try {
    const system = [await services.context(), ...turn.messages
      .filter(({ role }) => role === 'system').map(({ content }) => content)].join('\n')
    const gateway = (env.HIGRESS_GATEWAY_URL || 'http://127.0.0.1:8080').replace(/\/$/, '') + '/v1'
    const patch = join(work, 'pa.patch.yml')
    await writeFile(patch, JSON.stringify(profilePatch(turn, system, gateway), null, 2), { mode: 0o600 })
    const childEnv = Object.fromEntries(['PATH', 'SystemRoot', 'TEMP', 'TMP', 'LANG'].filter(
      (key) => env[key] !== undefined).map((key) => [key, env[key]]))
    harness = new DeepSeekHarness({
      profile: 'sdk-minimal', dshBin: env.DSH_BIN, dshHome: work, patches: [patch],
      processCwd: work, cwd: work, provider: 'pa-higress', model: turn.modelCode,
      maxTokens: turn.maxTokens, initializeTimeoutMs: 20000,
      env: { ...childEnv, PA_GATEWAY_KEY: env.HIGRESS_GATEWAY_KEY || 'no-auth',
        PA_TOOL_RELAY_URL: relay.url, PA_TOOL_RELAY_KEY: relay.key, NODE_ENV: 'production' },
    })
    signal.addEventListener('abort', cancel, { once: true })
    signal.throwIfAborted()
    const messages = turn.messages.filter(({ role }) => role !== 'system')
    const prompt = 'Use the conversation context and respond to the final user message.\n' +
      JSON.stringify(messages)
    const usage = emptyUsage()
    const result = await harness.run([{ type: 'text', text: prompt }], {
      sessionId: 'turn-' + turn.turnId,
      onNotification({ method, params }) {
        if (method !== 'session.event') return
        const event = params.event
        if (event.type === 'step/start') emit('thinking', { durationMs: 0, finished: false })
        if (event.type === 'step/end') emit('thinking', { durationMs: 0, finished: true })
        if (event.type !== 'assistant/message' || !event.data.usage) return
        const counts = event.data.usage
        usage.inputTokens += (counts.inputTokens || 0) + (counts.cacheReadTokens || 0) +
          (counts.cacheWriteTokens || 0)
        usage.cachedTokens += counts.cacheReadTokens || 0
        usage.outputTokens += counts.outputTokens || 0
        emit('usage', usage)
      },
    })
    if (result.finishReason !== 'completed' || !result.finalResponse?.trim()) {
      throw new Error('DSH turn failed')
    }
    emit('delta', { text: result.finalResponse })
    return usage
  } finally {
    signal.removeEventListener('abort', cancel)
    await harness?.close().catch(() => {})
    const resolved = await realpath(work)
    const owned = relative(base, resolved)
    if (/^pa-dsh-[^/\\]+$/.test(owned) && !isAbsolute(owned)) {
      await rm(resolved, { recursive: true, force: true })
    }
  }
}
