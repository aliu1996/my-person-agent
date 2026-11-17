import { timingSafeEqual } from 'node:crypto'

export const TOKEN_HEADER = 'x-internal-token'
export const MiB = 1024 * 1024

export function tokenMatches(value, target) {
  if (typeof value !== 'string' || !target) return false
  const provided = Buffer.from(value)
  const expected = Buffer.from(target)
  return provided.length === expected.length && timingSafeEqual(provided, expected)
}

export function serviceToken(env) {
  const token = env.INTERNAL_API_TOKEN || (env.ENV === 'prod' ? '' : 'dev-internal-token-change-me')
  if (env.ENV === 'prod' && Buffer.byteLength(token) < 32) {
    throw new Error('INTERNAL_API_TOKEN is required')
  }
  return token
}

export async function readBody(request, limit) {
  const chunks = []
  let size = 0
  for await (const chunk of request) {
    size += chunk.length
    if (size > limit) throw new Error('Request too large')
    chunks.push(chunk)
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'))
}

export function respond(response, status, data) {
  response.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
  response.end(JSON.stringify(data))
}

export function sse(response, event, data) {
  if (!response.destroyed && !response.writableEnded) {
    response.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
  }
}

export async function requestJson(url, options, signal) {
  const response = await fetch(url, { ...options, redirect: 'error', signal })
  if (!response.ok) throw Object.assign(new Error('Service request failed'), {
    status: response.status,
  })
  return response.json()
}
