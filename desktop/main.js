'use strict'

// slice desktop, main process. Plain JavaScript (repo rule: no TypeScript).
//
// Responsibilities kept here, out of the renderer, so the slice key never enters a
// web page: read/write ~/.slice/config.json (the exact format the CLI uses, shared so
// one login serves both), run the GitHub device flow against the gateway, and make the
// authenticated /account and /scanner calls. The renderer talks to this over a tiny,
// explicit IPC bridge (see preload.js) and never sees the key.

const { app, BrowserWindow, ipcMain, shell } = require('electron')
const path = require('node:path')
const fs = require('node:fs')
const os = require('node:os')

app.setName('slice')

// The app's home gateway. Login always targets this; an existing key from the CLI is
// honored against whatever base_url it was saved under.
const APP_BASE = 'https://api.sliceapp.dev'
const GITHUB_DEVICE_URL = 'https://github.com/login/device'

const CONFIG_DIR = path.join(os.homedir(), '.slice')
const CONFIG_PATH = path.join(CONFIG_DIR, 'config.json')

const WIN = { width: 1280, height: 820, minWidth: 980, minHeight: 640 }

let flowWindow = null
let dashboardWindow = null

// --- config file (exact CLI format: base_url, slice_key, jwt, login, account_id) ----

function loadConfig () {
  try {
    return JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'))
  } catch {
    return {}
  }
}

function saveConfig (data) {
  fs.mkdirSync(CONFIG_DIR, { recursive: true })
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(data, null, 2) + '\n', { mode: 0o600 })
  try { fs.chmodSync(CONFIG_PATH, 0o600) } catch { /* best effort */ }
}

function gatewayBase () {
  const saved = loadConfig().base_url
  return (saved || APP_BASE).replace(/\/+$/, '')
}

function sliceKey () {
  return loadConfig().slice_key || null
}

// --- gateway HTTP (Node fetch; never logs the key or response bodies) ---------------

async function postJson (base, path_, body) {
  const res = await fetch(base + path_, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body)
  })
  let json = null
  try { json = await res.json() } catch { json = null }
  return { status: res.status, body: json }
}

async function authed (method, path_, body) {
  const key = sliceKey()
  if (!key) return { status: 401, body: { error: { message: 'Not signed in.' } } }
  const res = await fetch(gatewayBase() + path_, {
    method,
    headers: {
      'content-type': 'application/json',
      Authorization: `Bearer ${key}`
    },
    body: body === undefined ? undefined : JSON.stringify(body)
  })
  let json = null
  try { json = await res.json() } catch { json = null }
  return { status: res.status, body: json }
}

function errorMessage (payload, fallback) {
  const e = payload && payload.error
  if (e && typeof e === 'object' && e.message) return e.message
  if (typeof e === 'string') return e
  return fallback
}

// --- windows ------------------------------------------------------------------------

function createFlowWindow () {
  flowWindow = new BrowserWindow({
    ...WIN,
    title: 'slice',
    backgroundColor: '#FBF8F1',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  })
  flowWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'))
  flowWindow.once('ready-to-show', () => flowWindow.show())
  flowWindow.on('closed', () => { flowWindow = null })
}

// Screen 4: the user's real live dashboard (the repo's Vue SPA), bundled inside the app
// and pointed at api.sliceapp.dev. webSecurity is relaxed for THIS window only so the
// bundled file:// SPA may call the api.sliceapp.dev origin; the clean fix (a one-line
// gateway CORS allowance, or serving the SPA from the gateway) is written up in the
// report. The dashboard preload seeds the slice key into sessionStorage so the SPA skips
// its paste-a-key screen.
function openDashboard () {
  dashboardWindow = new BrowserWindow({
    ...WIN,
    title: 'slice',
    backgroundColor: '#FBF8F1',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'dashboard-preload.js'),
      contextIsolation: false,
      nodeIntegration: false,
      sandbox: false,
      webSecurity: false
    }
  })
  dashboardWindow.loadFile(path.join(__dirname, 'dashboard', 'index.html'))
  dashboardWindow.once('ready-to-show', () => dashboardWindow.show())
  dashboardWindow.on('closed', () => { dashboardWindow = null })
  if (flowWindow) flowWindow.close()
}

// --- IPC: the only surface the renderer can reach --------------------------------

ipcMain.handle('config:status', async () => {
  // Is there already a usable key? Validate it so a stale key falls back to login.
  const key = sliceKey()
  if (!key) return { loggedIn: false }
  try {
    const res = await authed('GET', '/auth/me')
    if (res.status === 200 && res.body && res.body.account) {
      return { loggedIn: true, login: res.body.account.login }
    }
  } catch { /* offline or unreachable: fall through to login */ }
  return { loggedIn: false }
})

ipcMain.handle('device:start', async () => {
  const res = await postJson(APP_BASE, '/auth/device/start')
  if (res.status !== 200 || !res.body) {
    return { ok: false, error: errorMessage(res.body, `Could not start sign in (HTTP ${res.status}).`) }
  }
  return {
    ok: true,
    session_id: res.body.session_id,
    user_code: res.body.user_code,
    verification_uri: res.body.verification_uri || GITHUB_DEVICE_URL,
    interval: res.body.interval || 5
  }
})

ipcMain.handle('device:poll', async (_e, sessionId) => {
  const res = await postJson(APP_BASE, '/auth/device/poll', { session_id: sessionId })
  if (res.status !== 200 || !res.body) {
    return { status: 'error', error: errorMessage(res.body, `Sign in failed (HTTP ${res.status}).`) }
  }
  const body = res.body
  if (body.status === 'authorized') {
    const existing = loadConfig()
    const account = body.account || {}
    saveConfig({
      ...existing,
      base_url: APP_BASE,
      slice_key: body.slice_key,
      jwt: body.jwt,
      login: account.login,
      account_id: account.id
    })
    return { status: 'authorized', login: account.login }
  }
  return { status: body.status, interval: body.interval }
})

ipcMain.handle('profile:get', async () => {
  const res = await authed('GET', '/account/profile')
  if (res.status !== 200) {
    return { ok: false, error: errorMessage(res.body, `Could not load your profile (HTTP ${res.status}).`) }
  }
  return { ok: true, profile: res.body }
})

ipcMain.handle('profile:put', async (_e, patch) => {
  const res = await authed('PUT', '/account/profile', patch)
  if (res.status !== 200) {
    return { ok: false, error: errorMessage(res.body, `Could not save (HTTP ${res.status}).`) }
  }
  return { ok: true, profile: res.body }
})

// Connect AWS: the existing Phase 18 flow. Ask the gateway for the CloudFormation
// quick-create URL (per account), then open it in the user's real browser.
ipcMain.handle('aws:connect', async () => {
  const res = await authed('GET', '/scanner/connect')
  if (res.status !== 200 || !res.body) {
    return { ok: false, error: errorMessage(res.body, `Could not start AWS connect (HTTP ${res.status}).`) }
  }
  const url = res.body.quick_create_url
  if (!url) {
    return { ok: false, error: 'This account has no AWS connect step to open.' }
  }
  await shell.openExternal(url)
  return { ok: true }
})

ipcMain.handle('open:external', async (_e, url) => {
  if (typeof url === 'string' && /^https?:\/\//.test(url)) await shell.openExternal(url)
})

ipcMain.handle('goto:dashboard', async () => { openDashboard() })

// The dashboard preload asks (synchronously, before the SPA boots) for the key to seed
// into sessionStorage. This is the one place the key leaves the main process, and only
// into the trusted bundled dashboard window.
ipcMain.on('get-slice-key', (event) => { event.returnValue = sliceKey() })

// --- lifecycle ----------------------------------------------------------------------

app.whenReady().then(() => {
  createFlowWindow()
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createFlowWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
