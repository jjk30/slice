// Phase 12: the plain slice-key bridge for the dashboard.
//
// The dashboard reads /admin and /dashboard, which are now locked. Until the full
// GitHub web login lands (a later step), the dashboard just asks for a slice key,
// keeps it in sessionStorage (so it is gone when the tab closes, never persisted),
// and sends it as `Authorization: Bearer <key>` on every API and SSE call. A missing
// or rejected key shows the login screen.
import { ref } from 'vue'

const STORAGE_KEY = 'slice.key'

// Reactive so App.vue can react to login / logout / a 401.
export const sliceKey = ref(sessionStorage.getItem(STORAGE_KEY) || '')

export function setKey(key) {
  const trimmed = (key || '').trim()
  sliceKey.value = trimmed
  if (trimmed) sessionStorage.setItem(STORAGE_KEY, trimmed)
  else sessionStorage.removeItem(STORAGE_KEY)
}

export function clearKey() {
  setKey('')
}

export function hasKey() {
  return Boolean(sliceKey.value)
}

// The Authorization header to attach, or an empty object when there is no key.
export function authHeader() {
  return sliceKey.value ? { Authorization: `Bearer ${sliceKey.value}` } : {}
}

// EventSource cannot set headers, so the SSE URL carries the key as a query param
// instead (the gateway accepts it there for the events stream only).
export function withKeyParam(url) {
  if (!sliceKey.value) return url
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}slice_key=${encodeURIComponent(sliceKey.value)}`
}
