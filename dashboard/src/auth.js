// Phase 21: the dashboard's GitHub session, carried in an httpOnly cookie.
//
// The old paste-a-slice-key bridge is gone. Sign-in is a full-page redirect to
// /auth/github/login, GitHub and back, and the gateway sets a session cookie the browser
// sends on its own with every same-origin API and SSE call: no header, no key in a URL.
// `session` is the account ({login, id}) when signed in, null otherwise.
import { ref } from 'vue'
import { apiBase } from './api.js'

export const session = ref(null)

// Ask the gateway who we are (the cookie rides along). Sets `session` and returns it, or
// null when there is no valid session. Never throws; a 401 is just "signed out".
export async function loadSession() {
  try {
    const res = await fetch(apiBase() + '/auth/me', { headers: { Accept: 'application/json' } })
    if (!res.ok) {
      session.value = null
      return null
    }
    const body = await res.json()
    session.value = body && body.account ? body.account : null
  } catch (e) {
    session.value = null
  }
  return session.value
}

// Clear the session cookie on the gateway, then drop the local session. Returns true
// when the gateway answered the call, false when it could not be reached: the local
// session is dropped either way, and the caller can say which one happened.
export async function logout() {
  let reached = false
  try {
    const res = await fetch(apiBase() + '/auth/logout', { method: 'POST' })
    reached = res.ok
  } catch (e) {
    // A failed logout call still signs out locally.
    reached = false
  }
  session.value = null
  return reached
}

// The last GitHub username that signed in here, kept in localStorage so the sign-in card
// can offer "Log in as {name}" next time. Written only from the session the gateway
// returned (never from a query string or anything typed), and left alone by logout:
// forgetLogin() is the one way to clear it. Every call tolerates a browser that blocks
// storage (private mode, disabled site data) by doing nothing.
export const LAST_LOGIN_KEY = 'slice:last_login'

export function rememberLogin(username) {
  if (typeof username !== 'string' || !username) return
  try {
    localStorage.setItem(LAST_LOGIN_KEY, username)
  } catch (e) {
    // Storage unavailable: the card just shows the plain sign-in next time.
  }
}

export function lastLogin() {
  try {
    const name = localStorage.getItem(LAST_LOGIN_KEY)
    return typeof name === 'string' && name ? name : null
  } catch (e) {
    return null
  }
}

export function forgetLogin() {
  try {
    localStorage.removeItem(LAST_LOGIN_KEY)
  } catch (e) {
    // Nothing to forget if storage is unavailable.
  }
}
