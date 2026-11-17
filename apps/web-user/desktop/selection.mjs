import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)

const SEND_CTRL_C = `
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class NativeInput {
  [DllImport("user32.dll")]
  static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
  public static void SendCtrlC() {
    keybd_event(0x11, 0, 0, UIntPtr.Zero);
    keybd_event(0x43, 0, 0, UIntPtr.Zero);
    keybd_event(0x43, 0, 2, UIntPtr.Zero);
    keybd_event(0x11, 0, 2, UIntPtr.Zero);
  }
}
"@
[NativeInput]::SendCtrlC()
`

export function formatMiniQuestion(selection, prompt) {
  const question = typeof prompt === 'string' ? prompt.trim() : ''
  const excerpt = typeof selection === 'string' ? selection.trim() : ''
  if (!question) throw new Error('请输入要询问的问题')
  if (!excerpt) return question
  return `请根据以下选中的内容回答问题。\n\n「选中内容」\n${excerpt}\n\n「问题」\n${question}`
}

function snapshotClipboard(clipboard) {
  const image = clipboard.readImage?.()
  return {
    text: clipboard.readText?.() ?? '',
    html: clipboard.readHTML?.() ?? '',
    rtf: clipboard.readRTF?.() ?? '',
    image: image && !image.isEmpty?.() ? image : null,
  }
}

function restoreClipboard(clipboard, snapshot) {
  const payload = {}
  if (snapshot.text) payload.text = snapshot.text
  if (snapshot.html) payload.html = snapshot.html
  if (snapshot.rtf) payload.rtf = snapshot.rtf
  if (snapshot.image) payload.image = snapshot.image
  if (Object.keys(payload).length) clipboard.write(payload)
  else clipboard.clear()
}

export async function sendCopyKey(platform = process.platform) {
  if (platform === 'win32') {
    await execFileAsync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', SEND_CTRL_C], {
      windowsHide: true,
      timeout: 5000,
    })
    return
  }
  if (platform === 'darwin') {
    await execFileAsync('osascript', [
      '-e', 'tell application "System Events" to keystroke "c" using command down',
    ], { timeout: 5000 })
    return
  }
  throw new Error('unsupported')
}

export async function captureSelectedText({
  platform = process.platform,
  clipboard,
  sendCopy = sendCopyKey,
  wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  delay = 180,
} = {}) {
  if (!clipboard || (platform !== 'win32' && platform !== 'darwin')) {
    return { text: '', captured: false }
  }
  const previous = snapshotClipboard(clipboard)
  const marker = `__PA_SEL_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}__`
  try {
    clipboard.writeText(marker)
    await sendCopy(platform)
    await wait(delay)
    const text = clipboard.readText()
    restoreClipboard(clipboard, previous)
    if (typeof text !== 'string' || !text || text === marker) return { text: '', captured: false }
    return { text, captured: true }
  } catch {
    try { restoreClipboard(clipboard, previous) } catch {}
    return { text: '', captured: false }
  }
}
