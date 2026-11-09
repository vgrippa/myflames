'use strict';
// The workspace window's preload (sandboxed, context-isolated). It exposes the
// path on disk of a File the person dropped onto the window or chose in a file
// input (Electron 32 removed File.path), which the Files place uploads, and the
// in-place update bridge (updater.cjs). The update calls take no arguments: the
// main process reads the staged update from its own folder and answers only
// the workspace's top frame. No Node and no other Electron API reach the page.
// A File built in JavaScript has no path: the answer is "".
const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('myflamesDesktop', Object.freeze({
  pathForFile(file) {
    try {
      return webUtils.getPathForFile(file);
    } catch {
      return '';
    }
  },
  update: Object.freeze({
    support: () => ipcRenderer.invoke('myflames:update-support'),
    prepare: () => ipcRenderer.invoke('myflames:update-prepare'),
    restart: () => ipcRenderer.invoke('myflames:update-restart'),
  }),
}));
