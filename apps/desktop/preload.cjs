'use strict';
// The workspace window's preload (sandboxed, context-isolated). It exposes one
// function to the page: the path on disk of a File the person dropped onto the
// window or chose in a file input (Electron 32 removed File.path), which the
// Files place uploads. No IPC, no Node and no other Electron API reach the
// page. A File built in JavaScript has no path: the answer is "".
const { contextBridge, webUtils } = require('electron');

contextBridge.exposeInMainWorld('myflamesDesktop', Object.freeze({
  pathForFile(file) {
    try {
      return webUtils.getPathForFile(file);
    } catch {
      return '';
    }
  },
}));
