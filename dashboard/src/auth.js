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

// Clear the session cookie on the gateway, then drop the local session.
export async function logout() {
  try {
    await fetch(apiBase() + '/auth/logout', { method: 'POST' })
  } catch (e) {
    // A failed logout call still signs out locally; ignore.
  }
  session.value = null
}
