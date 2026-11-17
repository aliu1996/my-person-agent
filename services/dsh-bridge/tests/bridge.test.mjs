import assert from 'node:assert/strict'
import test from 'node:test'
import { profilePatch } from '../src/dsh.mjs'
import { dataFrames } from '../src/gateway.mjs'
import { dockerArgs, rootlessResources } from '../src/sandbox.mjs'

test('sandbox requires rootless CPU, RAM and PID controls and has no network', () => {
  const verified = {
    OSType: 'linux', CgroupVersion: '2', CgroupDriver: 'systemd', MemoryLimit: true,
    CPUCfsQuota: true, PidsLimit: true, SecurityOptions: ['name=rootless'],
  }
  assert.equal(rootlessResources(verified), true)
  assert.equal(rootlessResources({ ...verified, CPUCfsQuota: false }), false)
  assert.equal(rootlessResources({ ...verified, SecurityOptions: [] }), false)
  const args = dockerArgs('pa-sandbox:phase4', 'pa-sandbox-test', '/tmp/test-input')
  for (const flag of ['--network=none', '--read-only', '--cap-drop=ALL',
    '--security-opt=seccomp=builtin', '--cpus=1', '--memory=1g', '--pids-limit=64']) {
    assert.ok(args.includes(flag))
  }
  assert.ok(args.includes('type=bind,source=/tmp/test-input,target=/inputs,readonly'))
})

test('DSH profile disables host execution and supplies only the registered relay tools', () => {
  const patch = profilePatch({ userId: 1, turnId: 'test', modelCode: 'test-model' },
    'system', 'https://example.test/v1')
  for (const id of ['tool-fs', 'tool-working-directory', 'persistent-bash', 'persistent-pwsh']) {
    assert.ok(patch.some((row) => row.id === id && row.disabled))
  }
  const { insert } = patch.at(-1)
  assert.deepEqual(insert.map(({ id }) => id), ['pa-gateway', 'pa-tool-relay'])
})

test('gateway events preserve UTF-8 across partial frames', async () => {
  const bytes = Buffer.from('data: {"delta":"你好"}\r\n\r\ndata: [DONE]\n\n')
  const result = []
  for await (const value of dataFrames([bytes.subarray(0, 17), bytes.subarray(17)])) {
    result.push(value)
  }
  assert.deepEqual(result, ['{"delta":"你好"}', '[DONE]'])
})
