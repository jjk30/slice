// Tiny fetch wrapper for the dashboard endpoints.

// Empty/unset VITE_API_BASE_URL means same-origin (the gateway serving dist/).
export function apiBase() {
  const raw = import.meta.env.VITE_API_BASE_URL
  return raw ? String(raw).replace(/\/+$/, '') : ''
}

// GET a JSON endpoint. Throws an Error whose message is the server's
// error.message when available (e.g. the 503 "database not connected" body),
// a plain network-failure message otherwise, or a non-JSON message when a 2xx
// body is not a JSON object (e.g. an HTML fallback page served at /dashboard/*).
export async function getJson(path) {
  let res
  try {
    res = await fetch(apiBase() + path, { headers: { Accept: 'application/json' } })
  } catch (e) {
    throw new Error(`Cannot reach the gateway at ${apiBase() || window.location.origin}.`)
  }
  let body = null
  try {
    body = await res.json()
  } catch (e) {
    body = null
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
