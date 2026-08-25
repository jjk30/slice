'use strict'

// Renderer for screens 1 to 3. All privileged work (config, device flow, profile calls)
// happens in the main process behind window.slice (see preload.js). When that bridge is
// absent (a plain browser preview), a small mock stands in so the screens still render.

const params = new URLSearchParams(location.search)

const MOCK = {
  async configStatus () { return { loggedIn: false } },
  async deviceStart () {
    return { ok: true, session_id: 'mock', user_code: 'WDJB-MJHT', verification_uri: 'https://github.com/login/device', interval: 5 }
  },
  async devicePoll () { return { status: 'pending', interval: 5 } },
  async profileGet () {
    return { ok: true, profile: { login: params.get('login') || 'jjk30', email: params.get('email') || '', whatsapp_number: '', aws_connected: params.get('aws') === '1' } }
  },
  async profilePut (patch) { return { ok: true, profile: patch } },
  async awsConnect () { return { ok: true } },
  async openExternal () {},
  async gotoDashboard () {}
}

const api = window.slice || MOCK
const state = { login: params.get('login') || 'there', pollTimer: null }

// --- helpers ----------------------------------------------------------------

function show (id) {
  for (const s of document.querySelectorAll('.screen')) s.classList.toggle('on', s.id === id)
}

function timeOfDay () {
  const h = new Date().getHours()
  if (h < 12) return 'morning'
  if (h < 18) return 'afternoon'
  return 'evening'
}

const $ = (id) => document.getElementById(id)

// --- screen 1: welcome ------------------------------------------------------

$('welcome-line').textContent = `Good ${timeOfDay()}. Let's get your AI bill under control.`

$('signin-btn').addEventListener('click', startLogin)

// --- screen 2: login (device flow) ------------------------------------------

async function startLogin () {
  show('login')
  setStatus('Starting sign in', false)
  $('user-code').textContent = '....'
  const started = await api.deviceStart()
  if (!started.ok) { setStatus(started.error || 'Could not start sign in', true); return }

  $('user-code').textContent = started.user_code
  const verifyUrl = started.verification_uri
  $('open-github-btn').onclick = () => api.openExternal(verifyUrl)
  setStatus('Waiting for you to authorize', false)
  poll(started.session_id, (started.interval || 5) * 1000)
}

function setStatus (text, isError) {
  const el = $('login-status')
  el.classList.toggle('err', !!isError)
  $('login-status-text').textContent = text
}

function poll (sessionId, delay) {
  clearTimeout(state.pollTimer)
  state.pollTimer = setTimeout(async () => {
    let res
    try { res = await api.devicePoll(sessionId) } catch { res = { status: 'error', error: 'Lost contact with slice' } }
    if (res.status === 'authorized') {
      state.login = res.login || state.login
      await enterSetup()
      return
    }
    if (res.status === 'pending' || res.status === 'slow_down') {
      poll(sessionId, (res.interval || delay / 1000) * 1000)
      return
    }
    if (res.status === 'expired') { setStatus('That code expired. Sign in again.', true); return }
    if (res.status === 'denied') { setStatus('Sign in was declined on GitHub.', true); return }
    setStatus(res.error || 'Sign in failed', true)
  }, delay)
}

// --- screen 3: hello + setup ------------------------------------------------

async function enterSetup () {
  const tod = timeOfDay()
  $('hello-line').textContent = `Good ${tod}, ${state.login}.`
  document.querySelector('#setup .sub').textContent = 'Two quick things so slice can reach you.'

  const res = await api.profileGet()
  if (res.ok && res.profile) {
    state.login = res.profile.login || state.login
    $('hello-line').textContent = `Good ${tod}, ${state.login}.`
    $('email').value = res.profile.email || ''
    $('whatsapp').value = res.profile.whatsapp_number || ''
    reflectAws(res.profile.aws_connected)
  }
  show('setup')
}

function reflectAws (connected) {
  const btn = $('aws-btn')
  const sub = $('aws-sub')
  if (connected) {
    btn.outerHTML = '<span class="aws-ok" id="aws-btn">Connected</span>'
    sub.textContent = 'slice is watching this account, read only.'
  }
}

$('aws-btn').addEventListener('click', async () => {
  const res = await api.awsConnect()
  if (!res.ok) setErr(res.error || 'Could not open AWS connect')
})

$('save-btn').addEventListener('click', save)
$('skip-btn').addEventListener('click', finish)

function setErr (text) { $('setup-err').textContent = text || '' }

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/
const E164_RE = /^\+[1-9]\d{1,14}$/

async function save () {
  setErr('')
  const email = $('email').value.trim()
  const whatsapp = $('whatsapp').value.trim()
  const patch = {}
  if (email) {
    if (!EMAIL_RE.test(email)) { setErr('That email does not look right.'); return }
    patch.email = email
  }
  if (whatsapp) {
    if (!E164_RE.test(whatsapp)) { setErr('Use an international number, like +14155552671.'); return }
    patch.whatsapp_number = whatsapp
  }
  if (Object.keys(patch).length === 0) { finish(); return }

  const btn = $('save-btn')
  btn.disabled = true
  btn.textContent = 'Saving'
  const res = await api.profilePut(patch)
  if (!res.ok) { setErr(res.error || 'Could not save'); btn.disabled = false; btn.textContent = 'Save and continue'; return }
  finish()
}

function finish () { api.gotoDashboard() }

// --- startup ----------------------------------------------------------------

async function boot () {
  const forced = params.get('screen')
  if (forced === 'welcome') { show('welcome'); return }
  if (forced === 'login') { show('login'); $('user-code').textContent = 'WDJB-MJHT'; return }
  if (forced === 'setup') { await enterSetup(); return }

  // A valid key already on disk skips straight to the setup screen.
  const status = await api.configStatus()
  if (status && status.loggedIn) {
    state.login = status.login || state.login
    await enterSetup()
  } else {
    show('welcome')
  }
}

boot()
