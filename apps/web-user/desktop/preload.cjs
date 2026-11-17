const { contextBridge, ipcRenderer } = require('electron')

function invoke(channel, data) {
  return ipcRenderer.invoke(channel, data)
}

contextBridge.exposeInMainWorld('desktopModels', {
  list: () => invoke('models:list'),
  saveProvider: (data) => invoke('models:saveProvider', data),
  deleteProvider: (id) => invoke('models:deleteProvider', id),
  saveModel: (data) => invoke('models:saveModel', data),
  deleteModel: (id) => invoke('models:deleteModel', id),
  selectModel: (id) => invoke('models:selectModel', id),
  fetchModels: (id) => invoke('models:fetchModels', id),
  chat: (data) => invoke('models:chat', data),
  openPlatform: () => invoke('models:openPlatform'),
})

contextBridge.exposeInMainWorld('desktopFolders', {
  list: () => invoke('folders:list'),
  add: () => invoke('folders:add'),
  remove: (id) => invoke('folders:remove', id),
  write: (data) => invoke('folders:write', data),
})
