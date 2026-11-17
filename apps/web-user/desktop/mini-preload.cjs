const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('miniAssistant', {
  onSelection: (handler) => {
    const listener = (_event, text) => handler(typeof text === 'string' ? text : '')
    ipcRenderer.on('mini:selection', listener)
    return () => ipcRenderer.removeListener('mini:selection', listener)
  },
  ask: (data) => ipcRenderer.invoke('mini:ask', data),
  openPlatform: () => ipcRenderer.invoke('mini:openPlatform'),
  close: () => ipcRenderer.invoke('mini:close'),
})
