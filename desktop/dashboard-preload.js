'use strict'

// Runs in the bundled dashboard window before the Vue SPA boots. It seeds the slice key
// into the exact place the SPA looks for it (sessionStorage['slice.key'], see
// dashboard/src/auth.js) so the user is not asked to paste a key they already signed in
// for. contextIsolation is off for this window only, so this shares the page's window.
const { ipcRenderer } = require('electron')

try {
  const key = ipcRenderer.sendSync('get-slice-key')
  if (key) window.sessionStorage.setItem('slice.key', key)
} catch {
  // If seeding fails the SPA simply falls back to its own paste-a-key screen.
}
