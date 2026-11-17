import { TOOLS } from './tools.mjs'

export const name = 'pa-tool-relay'
export const inject = ['tools']

export function apply(ctx) {
  const names = new Set(TOOLS.map((tool) => tool.name))
  ctx.tools.guard((execution) => names.has(execution.name) ? undefined : 'Tool is disabled')
  for (const definition of TOOLS) {
    ctx.tools.register({
      ...definition,
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
      },
      async execute(args, execution) {
        const response = await fetch(process.env.PA_TOOL_RELAY_URL, {
          method: 'POST', signal: execution.signal, redirect: 'error',
          headers: { 'Content-Type': 'application/json', Authorization: process.env.PA_TOOL_RELAY_KEY },
          body: JSON.stringify({ name: definition.name, args, stepId: execution.callId }),
        })
        if (!response.ok) throw new Error('Tool relay unavailable')
        return response.json()
      },
    })
  }
}
