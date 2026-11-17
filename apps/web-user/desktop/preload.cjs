const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('desktopModels', {
  list: () => ipcRenderer.invoke('models:list'),
  saveProvider: (data) => ipcRenderer.invoke('models:saveProvider', data),
  deleteProvider: (id) => ipcRenderer.invoke('models:deleteProvider', id),
  saveModel: (data) => ipcRenderer.invoke('models:saveModel', data),
  deleteModel: (id) => ipcRenderer.invoke('models:deleteModel', id),
  selectModel: (id) => ipcRenderer.invoke('models:selectModel', id),
  fetchModels: (id) => ipcRenderer.invoke('models:fetchModels', id),
  chat: (data) => ipcRenderer.invoke('models:chat', data),
  openPlatform: () => ipcRenderer.invoke('models:openPlatform'),
})
