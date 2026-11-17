import { mkdir, readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'

const FOLDER_TABLE = `
  CREATE TABLE folder_whitelist (
    id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE
  ) STRICT;
`

const SCHEMA = `
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
  ${FOLDER_TABLE}
  PRAGMA user_version = 2;
`

function transaction(db, run) {
  db.exec('BEGIN IMMEDIATE')
  try {
    const result = run()
    db.exec('COMMIT')
    return result
  } catch (error) {
    if (db.isTransaction) db.exec('ROLLBACK')
    throw error
  }
}

export async function openSettingsDatabase(file, parseLegacy) {
  const folder = dirname(file)
  await mkdir(folder, { recursive: true, mode: 0o700 })
  const db = new DatabaseSync(file, { enableForeignKeyConstraints: true })
  try {
    db.exec('PRAGMA busy_timeout = 1000; PRAGMA secure_delete = ON')
    const version = () => db.prepare('PRAGMA user_version').get().user_version
    if (version() === 0) {
      let legacy
      try {
        legacy = parseLegacy(JSON.parse(await readFile(join(folder, 'models.json'), 'utf8')))
      } catch (error) {
        if (error.code !== 'ENOENT') throw error
      }
      transaction(db, () => {
        if (version() !== 0) return
        db.exec(SCHEMA)
        if (!legacy) return
        const provider = db.prepare('INSERT INTO providers VALUES (?, ?, ?, ?)')
        for (const row of legacy.providers) {
          provider.run(row.id, row.name, row.baseUrl, row.encryptedKey)
        }
        const model = db.prepare('INSERT INTO models VALUES (?, ?, ?, ?)')
        for (const row of legacy.models) model.run(row.id, row.providerId, row.modelCode, row.name)
        db.prepare('UPDATE selection SET model_id = ? WHERE singleton = 1').run(legacy.selectedModelId)
      })
    }
    if (version() === 1) {
      transaction(db, () => {
        if (version() !== 1) return
        db.exec(FOLDER_TABLE)
        db.exec('PRAGMA user_version = 2')
      })
    }
    if (version() !== 2) throw new Error('Unsupported settings database version')
    return db
  } catch (error) {
    db.close()
    throw error
  }
}

export function readSettings(db) {
  return {
    providers: db.prepare(`
      SELECT id, name, base_url AS baseUrl, encrypted_key AS encryptedKey FROM providers ORDER BY rowid
    `).all(),
    models: db.prepare(`
      SELECT id, provider_id AS providerId, model_code AS modelCode, name FROM models ORDER BY rowid
    `).all(),
    selectedModelId: db.prepare('SELECT model_id FROM selection WHERE singleton = 1').get().model_id,
  }
}

export function listFolders(db) {
  return db.prepare('SELECT id, path FROM folder_whitelist ORDER BY rowid').all()
}

export function insertFolder(db, id, path) {
  db.prepare('INSERT INTO folder_whitelist (id, path) VALUES (?, ?)').run(id, path)
}

export function deleteFolder(db, id) {
  return db.prepare('DELETE FROM folder_whitelist WHERE id = ?').run(id).changes
}

export function changeSettings(db, mutate) {
  return transaction(db, () => {
    const previous = readSettings(db)
    const next = structuredClone(previous)
    const savedId = mutate(next)
    const retainedProviders = new Set(next.providers.map(({ id }) => id))
    const retainedModels = new Set(next.models.map(({ id }) => id))
    const removeProvider = db.prepare('DELETE FROM providers WHERE id = ?')
    const removeModel = db.prepare('DELETE FROM models WHERE id = ?')
    for (const { id } of previous.providers) if (!retainedProviders.has(id)) removeProvider.run(id)
    for (const { id } of previous.models) if (!retainedModels.has(id)) removeModel.run(id)
    const putProvider = db.prepare(`
      INSERT INTO providers VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
        name = excluded.name, base_url = excluded.base_url, encrypted_key = excluded.encrypted_key
    `)
    for (const row of next.providers) {
      const old = previous.providers.find(({ id }) => id === row.id)
      if (!old || old.name !== row.name || old.baseUrl !== row.baseUrl ||
          old.encryptedKey !== row.encryptedKey) {
        putProvider.run(row.id, row.name, row.baseUrl, row.encryptedKey)
      }
    }
    const putModel = db.prepare(`
      INSERT INTO models VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
        provider_id = excluded.provider_id, model_code = excluded.model_code, name = excluded.name
    `)
    for (const row of next.models) {
      const old = previous.models.find(({ id }) => id === row.id)
      if (!old || old.name !== row.name || old.providerId !== row.providerId ||
          old.modelCode !== row.modelCode) {
        putModel.run(row.id, row.providerId, row.modelCode, row.name)
      }
    }
    if (next.selectedModelId !== previous.selectedModelId) {
      db.prepare('UPDATE selection SET model_id = ? WHERE singleton = 1').run(next.selectedModelId)
    }
    return { settings: next, savedId }
  })
}
