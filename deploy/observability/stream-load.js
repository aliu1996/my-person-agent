import http from 'k6/http'
import { check } from 'k6'
import { SharedArray } from 'k6/data'
import exec from 'k6/execution'

const users = new SharedArray('seeded accounts', () => JSON.parse(open(__ENV.PA_LOAD_USERS)))
const fast = Number(__ENV.PA_FAST_VUS || 4250)
const agent = Number(__ENV.PA_AGENT_VUS || 750)
if (users.length < fast + agent || !__ENV.PA_LOAD_URL) {
  throw new Error('Provide one different test session per VU and an explicit load target')
}
export const options = {
  scenarios: {
    fast: { executor: 'per-vu-iterations', exec: 'fastTurn', vus: fast, iterations: 1, maxDuration: '10m' },
    agent: { executor: 'per-vu-iterations', exec: 'agentTurn', vus: agent, iterations: 1, maxDuration: '10m' },
  },
  thresholds: { checks: ['rate>0.99'], http_req_failed: ['rate<0.01'] },
}

function send(user, content) {
  const response = http.post(__ENV.PA_LOAD_URL + '/api/chat/stream/send', JSON.stringify({
    sessionId: user.sessionId, content, webSearch: false,
  }), { headers: { Authorization: 'Bearer ' + user.jwt, 'Content-Type': 'application/json' },
    timeout: '9m' })
  check(response, { 'SSE completes': (reply) => reply.status === 200 &&
    reply.body.includes('event: delta') && reply.body.includes('event: done') &&
    !reply.body.includes('event: error') })
}

export function fastTurn() { send(users[exec.vu.idInTest - 1], '用一句话解释什么是事务。') }
export function agentTurn() { send(users[exec.vu.idInTest - 1], '制作一个包含三个月示例销量的图表。') }
