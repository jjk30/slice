'use strict'

// The only bridge between the renderer and the main process. Each call is an explicit,
// named IPC round trip; the renderer never gets Node, the filesystem, or the slice key.
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('slice', {
  configStatus: () => ipcRenderer.invoke('config:status'),
  deviceStart: () => ipcRenderer.invoke('device:start'),
  devicePoll: (sessionId) => ipcRenderer.invoke('device:poll', sessionId),
  profileGet: () => ipcRenderer.invoke('profile:get'),
  profilePut: (patch) => ipcRenderer.invoke('profile:put', patch),
  awsConnect: () => ipcRenderer.invoke('aws:connect'),
  openExternal: (url) => ipcRenderer.invoke('open:external', url),
  gotoDashboard: () => ipcRenderer.invoke('goto:dashboard')
})
