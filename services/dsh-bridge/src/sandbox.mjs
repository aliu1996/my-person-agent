import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { chmod, mkdir, mkdtemp, realpath, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { isAbsolute, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { MiB, TOKEN_HEADER, readBody, respond, serviceToken, tokenMatches } from './http.mjs'

const MAX_CODE = 32000
const MAX_ATTACHMENTS = 20
const MAX_DATA = 50 * MiB

export function dockerArgs(image, name, input) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9/.:@_-]{0,200}$/.test(image) || input.includes(',')) {
    throw new Error('Invalid sandbox deployment configuration')
  }
  return [
    'run', '--rm', '--pull=never', '--name', name, '-i', '--network=none', '--read-only',
    '--user=65532:65532', '--cap-drop=ALL', '--security-opt=no-new-privileges=true',
    '--security-opt=seccomp=builtin', '--cpus=1', '--memory=1g', '--memory-swap=1g',
    '--pids-limit=64', '--ulimit', 'nofile=128:128', '--ipc=none',
    '--tmpfs', '/workspace:rw,exec,nosuid,nodev,size=64m,mode=1777',
    '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777',
    '--mount', `type=bind,source=${input},target=/inputs,readonly`, image,
  ]
}

export function rootlessResources(info) {
  const values = new Map(Object.entries(info).map(([key, value]) => [key.toLowerCase(), value]))
  return info.OSType === 'linux' && info.CgroupVersion === '2' &&
    info.CgroupDriver === 'systemd' && info.SecurityOptions?.includes('name=rootless') &&
    info.MemoryLimit === true && values.get('cpucfsquota') === true && info.PidsLimit === true
}

function command(args, { input, signal, limit = 64 * 1024 }) {
  return new Promise((resolve, reject) => {
    const child = spawn('docker', args, { stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true })
    const chunks = []
    let size = 0
    let exceeded = false
    const cancel = () => child.kill('SIGKILL')
    signal?.addEventListener('abort', cancel, { once: true })
    if (signal?.aborted) cancel()
    child.stdout.on('data', (chunk) => {
      size += chunk.length
      if (size > limit) {
        exceeded = true
        cancel()
      } else {
        chunks.push(chunk)
      }
    })
    child.stdin.on('error', () => {})
    child.stdin.end(input)
    child.on('error', reject)
    child.on('close', (code) => {
      signal?.removeEventListener('abort', cancel)
      if (code !== 0 || exceeded || signal?.aborted) reject(new Error('Sandbox command failed'))
      else resolve(Buffer.concat(chunks).toString('utf8'))
    })
  })
}

function validateJob(body) {
  if (!['python', 'javascript'].includes(body?.language) ||
      typeof body.code !== 'string' || !body.code.trim() || body.code.length > MAX_CODE ||
      !Array.isArray(body.files) || body.files.length > MAX_ATTACHMENTS) {
    throw new Error('Invalid sandbox job')
  }
  let bytes = 0
  const seen = new Set()
  const files = body.files.map(({ name, content }) => {
    if (typeof name !== 'string' || !/^[0-9]+_[\p{L}\p{N}_.-]{1,180}$/u.test(name) ||
        seen.has(name) || typeof content !== 'string') throw new Error('Invalid input file')
    const data = Buffer.from(content, 'base64')
    bytes += data.length
    if (bytes > MAX_DATA) throw new Error('Attachments exceed sandbox capacity')
    seen.add(name)
    return [name, data]
  })
  return { language: body.language, code: body.code, files }
}

export async function executeSandbox(body, image, requestSignal) {
  const { files, ...program } = validateJob(body)
  if (process.platform !== 'linux') throw new Error('Sandbox requires its Linux executor')
  const info = JSON.parse(await command(['info', '--format', '{{json .}}'], {
    signal: AbortSignal.any([requestSignal, AbortSignal.timeout(5000)]), limit: MiB,
  }))
  if (!rootlessResources(info)) throw new Error('Rootless resource controls unavailable')
  const base = await realpath(tmpdir())
  const directory = await mkdtemp(join(base, 'pa-sandbox-'))
  const input = join(directory, 'input')
  const name = 'pa-sandbox-' + randomUUID()
  try {
    await mkdir(input, { mode: 0o755 })
    await chmod(input, 0o755)
    for (const [filename, data] of files) {
      await writeFile(join(input, filename), data, { flag: 'wx', mode: 0o444 })
    }
    const started = performance.now()
    const result = await command(dockerArgs(image, name, input), {
      input: JSON.stringify(program), limit: 69 * MiB,
      signal: AbortSignal.any([requestSignal, AbortSignal.timeout(60000)]),
    })
    return { ...JSON.parse(result), elapsedMs: Math.ceil(performance.now() - started) }
  } finally {
    await command(['rm', '-f', name], { signal: AbortSignal.timeout(5000) }).catch(() => {})
    const resolved = await realpath(directory)
    const child = relative(base, resolved)
    if (/^pa-sandbox-[^/\\]+$/.test(child) && !isAbsolute(child)) {
      await rm(resolved, { recursive: true, force: true })
    }
  }
}

export function sandboxServer(env = process.env, run = executeSandbox) {
  const token = serviceToken(env)
  const image = env.SANDBOX_IMAGE || 'pa-sandbox:phase4'
  let active = 0
  return createServer(async (request, response) => {
    if (request.method !== 'POST' || request.url !== '/internal/sandbox/run') {
      return respond(response, 404, { error: 'Unknown endpoint' })
    }
    if (!tokenMatches(request.headers[TOKEN_HEADER], token)) {
      return respond(response, 401, { error: 'Unauthorized' })
    }
    if (active >= 4) return respond(response, 429, { error: 'Sandbox queue is busy' })
    const controller = new AbortController()
    response.on('close', () => controller.abort())
    active += 1
    try {
      const job = await readBody(request, 69 * MiB)
      const result = await run(job, image, controller.signal)
      respond(response, 200, result)
    } catch {
      if (!response.destroyed) respond(response, 503, { error: 'Sandbox execution unavailable' })
    } finally {
      active -= 1
    }
  })
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  sandboxServer().listen(Number(process.env.SANDBOX_PORT || 8104),
    process.env.SANDBOX_HOST || '127.0.0.1')
}
