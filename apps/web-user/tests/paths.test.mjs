import assert from 'node:assert/strict'
import { join } from 'node:path'
import test from 'node:test'
import { desktopFile, platformAssetFile, rendererFile } from '../desktop/paths.mjs'

const root = join('installed', 'resources', 'app.asar')

test('resolves preload, renderer, and platform assets from the packaged app root', () => {
  assert.equal(desktopFile(root, 'preload.cjs'), join(root, 'desktop', 'preload.cjs'))
  assert.equal(desktopFile(root, 'mini-preload.cjs'), join(root, 'desktop', 'mini-preload.cjs'))
  assert.equal(rendererFile(root, '/mini.html'), join(root, 'renderer', 'mini.html'))
  assert.equal(
    platformAssetFile(root, '/platform/index.html'),
    join(root, 'dist', 'platform', 'index.html'),
  )
  assert.equal(
    platformAssetFile(root, '/platform/assets/index-abc123.js'),
    join(root, 'dist', 'platform', 'assets', 'index-abc123.js'),
  )
})

test('rejects path traversal when resolving packaged assets', () => {
  assert.throws(() => rendererFile(root, '/../desktop/main.mjs'), /invalid asset path/)
  assert.throws(() => platformAssetFile(root, '/platform/../../desktop/main.mjs'), /invalid asset path/)
  assert.throws(() => desktopFile(root, '../package.json'), /invalid asset path/)
  assert.throws(() => rendererFile(root, 'mini.html'), /invalid asset path/)
})
