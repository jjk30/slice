// Tiny fetch wrapper for the dashboard endpoints.
import { session } from './auth.js'

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
      headers: { Accept: 'application/json' },
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
    // The session is missing or rejected: drop it so the app shows the login screen.
    session.value = null
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

// POST a JSON endpoint (no request body) and read its JSON reply. Same error and 401
// handling as getJson: a 401 drops the session so the app returns to the login screen.
export function postJson(path) {
  return sendJson('POST', path)
}

// Send `method` to a JSON endpoint, with an optional JSON body, and read its JSON reply.
// Same error and 401 handling as getJson.
export async function sendJson(method, path, payload) {
  let res
  try {
    const headers = { Accept: 'application/json' }
    const init = { method, headers }
    if (payload !== undefined) {
      headers['Content-Type'] = 'application/json'
      init.body = JSON.stringify(payload)
    }
    res = await fetch(apiBase() + path, init)
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
    session.value = null
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

// The caller's AWS bill this month (yesterday + month-to-date). `month_to_date` is null
// when AWS is not connected or nothing has been fetched yet; the tile says so.
export function getAwsCost() {
  return getJson('/dashboard/aws_cost')
}

// Phase 24b: the AWS findings panel. The connection status decides whether the panel
// shows at all; the findings list is the newest run, each row carrying `title` (the
// email's plain-words line) and `expected`. Marking a finding expected (or undoing it)
// is a POST or DELETE of the same {check, resource_id} body.
export function getScannerConnect() {
  return getJson('/scanner/connect')
}

export function getFindings() {
  return getJson('/scanner/findings')
}

export function setExpected(check, resourceId, expected) {
  return sendJson(expected ? 'POST' : 'DELETE', '/scanner/expectations', {
    check,
    resource_id: resourceId,
  })
}
