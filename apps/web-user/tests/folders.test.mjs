import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import test from 'node:test'
import { isInsideAuthorizedFolder, resolveLocalPath } from '../desktop/folders.mjs'
import { captureSelectedText } from '../desktop/selection.mjs'
import { openSettingsDatabase } from '../desktop/settings-db.mjs'
import { createSettingsStore } from '../desktop/settings.mjs'

const V1_SCHEMA = `
  CREATE TABLE providers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL, encrypted_key TEXT
  ) STRICT;
  CREATE TABLE models (
    id TEXT PRIMARY KEY, provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    model_code TEXT NOT NULL, name TEXT NOT NULL, UNIQUE(provider_id, model_code)
  ) STRICT;
  CREATE TABLE selection (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    model_id TEXT REFERENCES models(id) ON DELETE SET NULL
  ) STRICT;
  INSERT INTO selection (singleton) VALUES (1);
  PRAGMA user_version = 1;
`

async function localStore(context) {
  const dir = await mkdtemp(join(tmpdir(), 'assistant-folders-'))
  const file = join(dir, 'models.sqlite3')
  const stores = []
  context.after(async () => {
    for (const store of stores) await store.close()
    await rm(file, { force: true })
    await rm(dir, { recursive: true, force: true })
  })
  const keyStore = { encrypt: async () => 'test-ciphertext', decrypt: async () => 'example-key' }
  function open() {
    const store = createSettingsStore(file, keyStore)
    stores.push(store)
    return store
  }
  return { file, dir, open }
}

function mockClipboard(initial = 'original') {
  let text = initial
  return {
    get text() { return text },
    readText: () => text,
    writeText: (value) => { text = value },
    readHTML: () => '',
    readRTF: () => '',
    readImage: () => ({ isEmpty: () => true }),
    write: (payload) => { text = payload.text ?? '' },
    clear: () => { text = '' },
  }
}

test('rejects path traversal and paths outside the authorized folder', () => {
  const options = { platform: 'win32', cwd: 'C:\\' }
  assert.equal(resolveLocalPath('C:\\notes\\', options), 'C:\\notes')
  assert.equal(isInsideAuthorizedFolder(['C:\\notes'], 'C:\\notes\\todo.txt', options), true)
  assert.equal(isInsideAuthorizedFolder(['C:\\notes'], 'C:\\Notes\\todo.txt', options), true)
  assert.equal(isInsideAuthorizedFolder(['C:\\notes'], 'C:\\notes\\..\\secrets\\x.txt', options), false)
  assert.equal(isInsideAuthorizedFolder(['C:\\notes'], 'C:\\notes2\\x.txt', options), false)
  assert.equal(isInsideAuthorizedFolder(['C:\\notes'], 'C:\\notes\\..\\notes\\a.txt', options), true)
  const posix = { platform: 'linux', cwd: '/' }
  assert.equal(isInsideAuthorizedFolder(['/data/notes'], '/data/notes/a.txt', posix), true)
  assert.equal(isInsideAuthorizedFolder(['/data/notes'], '/data/notes/../secret.txt', posix), false)
  assert.equal(isInsideAuthorizedFolder(['/data/notes'], '/data/notes2/a.txt', posix), false)
})

test('migrates a v1 settings database and keeps existing providers', async (t) => {
  const { dir, file, open } = await localStore(t)
  const db = new DatabaseSync(file, { enableForeignKeyConstraints: true })
  db.exec(V1_SCHEMA)
  db.prepare('INSERT INTO providers VALUES (?, ?, ?, ?)').run(
    'p1', 'Legacy', 'https://example.com/v1', 'test-ciphertext')
  db.exec("UPDATE selection SET model_id = NULL")
  const version = db.prepare('PRAGMA user_version').get().user_version
  assert.equal(version, 1)
  db.close()

  const opened = await openSettingsDatabase(file, () => { throw new Error('should not import') })
  assert.equal(opened.prepare('PRAGMA user_version').get().user_version, 2)
  assert.equal(opened.prepare('SELECT name FROM providers').get().name, 'Legacy')
  assert.deepEqual(opened.prepare('SELECT * FROM folder_whitelist').all(), [])
  opened.close()

  const restarted = open()
  const added = await restarted.addFolder(dir)
  assert.equal(added.path, resolveLocalPath(dir))
  assert.equal((await restarted.listFolders()).length, 1)
  await assert.rejects(restarted.addFolder(dir), /已在白名单/)
})

test('writes a file only after confirmation and rejects paths outside the whitelist', async (t) => {
  const { open, dir } = await localStore(t)
  const store = open()
  const folder = await store.addFolder(dir)
  const target = join(folder.path, 'nested', 'note.txt')
  const escaped = join(dir, '..', 'escape.txt')
  await assert.rejects(store.writeLocalFile({ path: target, content: 'secret' }, async () => false), /取消/)
  assert.equal(existsSync(target), false)
  await assert.rejects(store.writeLocalFile({ path: escaped, content: 'nope' }, async () => true), /不在/)
  assert.equal(existsSync(escaped), false)
  const written = await store.writeLocalFile({ path: target, content: '已确认' }, async (path) => {
    assert.equal(path, target)
    return true
  })
  assert.equal(written.path, target)
  assert.equal(await readFile(target, 'utf8'), '已确认')
  await store.removeFolder(folder.id)
  await assert.rejects(store.writeLocalFile({ path: target, content: 'again' }, async () => true), /不在/)
})

test('restores the clipboard after a successful or failed selection capture', async () => {
  const clipboard = mockClipboard('original')
  const missed = await captureSelectedText({
    platform: 'win32', clipboard, sendCopy: async () => {}, wait: async () => {},
  })
  assert.deepEqual(missed, { text: '', captured: false })
  assert.equal(clipboard.text, 'original')

  const copied = await captureSelectedText({
    platform: 'win32', clipboard, wait: async () => {},
    sendCopy: async () => { clipboard.writeText('选中的段落') },
  })
  assert.deepEqual(copied, { text: '选中的段落', captured: true })
  assert.equal(clipboard.text, 'original')

  const failed = await captureSelectedText({
    platform: 'win32', clipboard, wait: async () => {},
    sendCopy: async () => { throw new Error('copy failed') },
  })
  assert.deepEqual(failed, { text: '', captured: false })
  assert.equal(clipboard.text, 'original')
})
