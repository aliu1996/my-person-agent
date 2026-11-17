import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, rmdir, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { chat, fetchModels } from '../desktop/provider.mjs'
import { createSettingsStore, normalizeBaseUrl } from '../desktop/settings.mjs'

async function localStore(context) {
  const dir = await mkdtemp(join(tmpdir(), 'assistant-models-'))
  const file = join(dir, 'models.sqlite3')
  const legacy = join(dir, 'models.json')
  const stores = []
  context.after(async () => {
    for (const store of stores) await store.close()
    await rm(file, { force: true })
    await rm(legacy, { force: true })
    await rmdir(dir)
  })
  const keyStore = {
    encrypt: async () => 'test-ciphertext',
    decrypt: async () => 'example-key',
  }
  function open() {
    const store = createSettingsStore(file, keyStore)
    stores.push(store)
    return store
  }
  return { store: open(), file, legacy, open }
}

test('validates base URLs for HTTPS and direct local networks', () => {
  assert.equal(normalizeBaseUrl(' http://localhost:11434/v1/ '), 'http://localhost:11434/v1')
  assert.equal(normalizeBaseUrl('http://192.168.1.5:9000/v1'), 'http://192.168.1.5:9000/v1')
  assert.equal(normalizeBaseUrl('https://example.com/api/v1'), 'https://example.com/api/v1')
  for (const address of [
    'http://example.com/v1', 'file:///etc/passwd', 'https://name:password@example.com/v1',
    'https://example.com/v1?key=secret', 'https://example.com/v1/chat/completions',
  ]) {
    assert.throws(() => normalizeBaseUrl(address))
  }
})

test('keeps Key out of renderer snapshots and removes it when the API address changes', async (t) => {
  const { store, file, open } = await localStore(t)
  const baseUrl = 'https://example.com/v1'
  const { savedId: id, settings } = await store.saveProvider({
    name: '测试供应商', baseUrl, apiKey: 'example-key',
  })
  assert.equal(settings.providers[0].hasKey, true)
  assert.equal(JSON.stringify(settings).includes('test-ciphertext'), false)
  const disk = await readFile(file, 'utf8')
  assert.equal(disk.includes('example-key'), false)
  assert.equal(disk.includes('test-ciphertext'), true)
  await store.close()
  const restarted = open()
  assert.equal((await restarted.connection(id, 'provider')).apiKey, 'example-key')
  await restarted.saveProvider({ id, name: '更新名称', baseUrl })
  assert.equal((await restarted.connection(id, 'provider')).apiKey, 'example-key')
  await restarted.saveProvider({ id, name: '更新地址', baseUrl: 'https://another.example/v1' })
  assert.equal((await restarted.connection(id, 'provider')).apiKey, '')
})

test('imports legacy encrypted credentials and selection once and keeps the original file', async (t) => {
  const { store, file, legacy, open } = await localStore(t)
  const snapshot = JSON.stringify({
    version: 1, selectedModelId: 'm1',
    providers: [{ id: 'p1', name: 'Legacy', baseUrl: 'https://example.com/v1',
      encryptedKey: 'test-ciphertext' }],
    models: [{ id: 'm1', providerId: 'p1', modelCode: 'chat', name: 'Chat' }],
  })
  await writeFile(legacy, snapshot)
  assert.equal((await store.list()).selectedModelId, 'm1')
  assert.equal((await store.connection('m1', 'model')).apiKey, 'example-key')
  assert.equal((await readFile(file)).subarray(0, 15).toString(), 'SQLite format 3')
  await store.deleteProvider('p1')
  await store.close()
  assert.deepEqual(await open().list(), { providers: [], models: [], selectedModelId: null })
  assert.equal(await readFile(legacy, 'utf8'), snapshot)
})

test('rolls back an invalid import and retries after repairing the legacy file', async (t) => {
  const { store, legacy } = await localStore(t)
  const snapshot = {
    version: 1, selectedModelId: 'm1',
    providers: [{ id: 'p1', name: 'Legacy', baseUrl: 'https://example.com/v1' }],
    models: [{ id: 'm1', providerId: 'missing', modelCode: 'chat', name: 'Chat' }],
  }
  await writeFile(legacy, JSON.stringify(snapshot))
  await assert.rejects(store.list(), /导入旧配置/)
  snapshot.models[0].providerId = 'p1'
  await writeFile(legacy, JSON.stringify(snapshot))
  assert.equal((await store.list()).selectedModelId, 'm1')
})

test('serializes edits, prevents duplicate models, and moves selection after deletion', async (t) => {
  const { store } = await localStore(t)
  const [one, two] = await Promise.all([
    store.saveProvider({ name: '一号', baseUrl: 'https://one.example/v1' }),
    store.saveProvider({ name: '二号', baseUrl: 'https://two.example/v1' }),
  ])
  const first = await store.saveModel({ providerId: one.savedId, modelCode: 'chat' })
  const second = await store.saveModel({ providerId: two.savedId, modelCode: 'chat' })
  await assert.rejects(store.saveModel({ providerId: two.savedId, modelCode: 'chat' }), /已配置/)
  await store.selectModel(second.savedId)
  assert.equal((await store.list()).selectedModelId, second.savedId)
  await store.deleteProvider(two.savedId)
  const result = await store.list()
  assert.deepEqual(result.models.map((model) => model.id), [first.savedId])
  assert.equal(result.selectedModelId, first.savedId)
})

test('fetches compatible model lists and sends chat to the selected upstream model', async () => {
  const connection = { baseUrl: 'https://example.com/v1', apiKey: 'test-key', modelCode: 'm-1' }
  const list = await fetchModels(connection, async (url, options) => {
    assert.equal(url, 'https://example.com/v1/models')
    assert.equal(options.headers.Authorization, 'Bearer test-key')
    assert.equal(options.redirect, 'error')
    return Response.json({ data: [{ id: 'm-1' }, { id: 'm-1' }, { id: 'test-key' }] })
  })
  assert.deepEqual(list, ['m-1'])
  const messages = [{ role: 'user', content: '你好' }]
  const reply = await chat(connection, messages, async (url, options) => {
    assert.equal(url, 'https://example.com/v1/chat/completions')
    assert.deepEqual(JSON.parse(options.body), { model: 'm-1', messages, stream: false })
    return Response.json({ choices: [{ message: { content: '回答 test-key' } }] })
  })
  assert.equal(reply, '回答 ••••')
})

test('treats a missing model list as a prompt for manual model configuration', async () => {
  const connection = { baseUrl: 'http://localhost:11434/v1', apiKey: '' }
  await assert.rejects(fetchModels(connection, async () => new Response(null, { status: 404 })),
    /手动输入模型 ID/)
})
