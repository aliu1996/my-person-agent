import { join, normalize, relative, sep } from 'node:path'

function under(root, folder, pathname) {
  if (typeof pathname !== 'string' || !pathname.startsWith('/') || pathname.includes('\0')) {
    throw new Error('invalid asset path')
  }
  const base = normalize(join(root, folder))
  const target = normalize(join(base, ...pathname.split('/').filter(Boolean)))
  const rel = relative(base, target)
  if (!rel || rel.startsWith('..') || rel.split(sep).includes('..')) {
    throw new Error('invalid asset path')
  }
  return target
}

export function desktopFile(root, name) {
  return under(root, 'desktop', `/${name}`)
}

export function rendererFile(root, pathname) {
  return under(root, 'renderer', pathname)
}

export function platformAssetFile(root, pathname) {
  return under(root, 'dist', pathname)
}
