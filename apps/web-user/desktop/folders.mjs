import path from 'node:path'
import { existsSync, mkdirSync, realpathSync } from 'node:fs'
import { writeFile } from 'node:fs/promises'

export class PathError extends Error {}

export function pathApi(platform = process.platform) {
  return platform === 'win32' ? path.win32 : path.posix
}

function requiredPath(input) {
  if (typeof input !== 'string' || !input.trim() || input.trim().length > 4096) {
    throw new PathError('请输入有效的本地路径')
  }
  return input.trim()
}

function stripTrailingSep(value, pathMod) {
  const root = pathMod.parse(value).root
  const equals = (left, right) =>
    pathMod.sep === '\\' ? left.toLowerCase() === right.toLowerCase() : left === right
  while (value.length > root.length && (value.endsWith(pathMod.sep) || value.endsWith('/'))) {
    const next = value.replace(/[\\/]+$/, '')
    if (equals(pathMod.parse(next).root, next) || next.length < root.length) break
    value = next
  }
  return value
}

export function resolveLocalPath(input, { platform = process.platform, cwd } = {}) {
  const pathMod = pathApi(platform)
  const resolved = stripTrailingSep(pathMod.resolve(cwd ?? process.cwd(), requiredPath(input)), pathMod)
  if (!pathMod.isAbsolute(resolved)) throw new PathError('请使用绝对路径')
  return resolved
}

function comparable(value, platform) {
  return platform === 'win32' ? value.toLowerCase() : value
}

export function isInsideAuthorizedFolder(folders, targetPath, options = {}) {
  const platform = options.platform ?? process.platform
  const pathMod = pathApi(platform)
  const target = comparable(resolveLocalPath(targetPath, { ...options, platform }), platform)
  return folders.some((folder) => {
    const root = comparable(resolveLocalPath(folder, { ...options, platform }), platform)
    const prefix = root.endsWith(pathMod.sep) ? root : root + pathMod.sep
    return target.startsWith(prefix)
  })
}

function realExisting(target, pathMod) {
  try {
    return existsSync(target) ? realpathSync(target) : target
  } catch {
    return target
  }
}

export function resolveAuthorizedWritePath(folders, targetPath, options = {}) {
  const platform = options.platform ?? process.platform
  const pathMod = pathApi(platform)
  const target = resolveLocalPath(targetPath, { ...options, platform })
  if (!isInsideAuthorizedFolder(folders, target, { ...options, platform })) {
    throw new PathError('该路径不在已授权的本机目录内')
  }
  if (options.realpath === false) return target
  const roots = folders.map((folder) => realExisting(resolveLocalPath(folder, { ...options, platform }), pathMod))
  let directory = pathMod.dirname(target)
  const name = pathMod.basename(target)
  directory = realExisting(directory, pathMod)
  const resolved = existsSync(target) ? realExisting(target, pathMod) : pathMod.join(directory, name)
  if (!isInsideAuthorizedFolder(roots, resolved, { ...options, platform })) {
    throw new PathError('该路径不在已授权的本机目录内')
  }
  return resolved
}

export async function writeAuthorizedFile(folders, { path: targetPath, content }, confirm, options = {}) {
  if (typeof content !== 'string') throw new PathError('请提供要写入的文本内容')
  if (content.length > 2_000_000) throw new PathError('写入内容超过大小限制')
  const target = resolveAuthorizedWritePath(folders, targetPath, options)
  if (!await confirm(target)) throw new PathError('已取消写入')
  const pathMod = pathApi(options.platform ?? process.platform)
  const directory = pathMod.dirname(target)
  if (options.createDir !== false && !existsSync(directory)) {
    if (!isInsideAuthorizedFolder(folders, directory, options)) {
      throw new PathError('该路径不在已授权的本机目录内')
    }
    mkdirSync(directory, { recursive: true, mode: 0o700 })
  }
  await writeFile(target, content, { encoding: 'utf8', flag: 'w' })
  return { path: target }
}
