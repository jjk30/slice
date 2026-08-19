// Tiny fetch wrapper for the dashboard endpoints.
import { authHeader, clearKey } from './auth.js'

// Empty/unset VITE_API_BASE_URL means same-origin (the gateway serving dist/).
export function apiBase() {
  const raw = import.meta.env.VITE_API_BASE_URL
  return raw ? String(raw).replace(/\/+$/, '') : ''
}

// Thrown when the gateway rejects the slice key (401). App.vue catches it to drop the
// key and show the login screen, rather than render the failure as a generic outage.
export class AuthError extends Error {}

// GET a JSON endpoint. Throws an Error whose message is the server's
// error.message when available (e.g. the 503 "database not connected" body),
// a plain network-failure message otherwise, or a non-JSON message when a 2xx
// body is not a JSON object (e.g. an HTML fallback page served at /dashboard/*).
export async function getJson(path) {
  let res
  try {
    res = await fetch(apiBase() + path, {
      headers: { Accept: 'application/json', ...authHeader() },
    })
  } catch (e) {
    throw new Error(`Cannot reach the gateway at ${apiBase() || window.location.origin}.`)
  }
  let body = null
  try {
    body = await res.json()
  } catch (e) {
    body = null
  }
  if (res.status === 401) {
    // The slice key is missing or rejected: forget it so the app shows the login screen.
    clearKey()
    const msg = body && body.error && body.error.message ? body.error.message : 'Not authorized.'
    throw new AuthError(msg)
  }
  if (!res.ok) {
    const msg = body && body.error && body.error.message
      ? body.error.message
      : `Request to ${path} failed with HTTP ${res.status}.`
    throw new Error(msg)
  }
  if (body === null || typeof body !== 'object') {
    throw new Error(`Unexpected non-JSON response from ${path}.`)
  }
  return body
}
