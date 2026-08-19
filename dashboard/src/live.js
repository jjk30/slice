import { ref, onMounted, onBeforeUnmount } from 'vue'
import { apiBase } from './api.js'
import { withKeyParam, hasKey } from './auth.js'

const BACKOFF_START_MS = 1000
const BACKOFF_CAP_MS = 15000

// Composable wrapping an EventSource on /dashboard/events.
// - status: 'live' | 'reconnecting' | 'offline'
// - onEvent(row): called with a normalized row for each named "request" event
// - onOpen(firstOpen): called on EVERY successful open. `firstOpen` is true the
//   first time the stream connects and false on every reopen after an outage;
//   the caller decides whether a resync is due (App.vue reloads on any reopen,
//   and on the first open only when its initial load had failed — e.g. the
//   page was opened while the gateway was still starting).
//
// Reconnection is manual: on error we close the source and reopen after a
// backoff that doubles 1s -> 2s -> 4s -> ... capped at 15s, reset on open.
export function useLiveEvents(onEvent, onOpen) {
  const status = ref('offline')
  let es = null
  let timer = null
  let backoff = BACKOFF_START_MS
  let stopped = false
  let hadOpened = false

  function normalize(payload) {
    return {
      key: payload.request_id,
      created_at: payload.created_at,
      team: payload.team ?? null,
      model: payload.model ?? null,
      routed_from: payload.routed_from ?? null,
      status: payload.status ?? null,
      cost: payload.cost ?? null,
      cached: Boolean(payload.cached),
    }
  }

  function scheduleReconnect() {
    if (stopped || timer) return
    status.value = 'reconnecting'
    timer = setTimeout(() => {
      timer = null
      backoff = Math.min(backoff * 2, BACKOFF_CAP_MS)
      connect()
    }, backoff)
  }

  function connect() {
    if (stopped) return
    // Phase 12: no key, no stream — the gateway would just 401 it into a reconnect loop.
    if (!hasKey()) {
      status.value = 'offline'
      return
    }
    if (es) es.close()
    // Status stays 'offline' until the first open; errors flip it to 'reconnecting'.
    // EventSource can't set headers, so the slice key rides as a query param (the
    // gateway accepts it there for this one GET only).
    es = new EventSource(withKeyParam(apiBase() + '/dashboard/events'))
    es.addEventListener('open', () => {
      backoff = BACKOFF_START_MS
      status.value = 'live'
      const firstOpen = !hadOpened
      hadOpened = true
      if (typeof onOpen === 'function') onOpen(firstOpen)
    })
    es.addEventListener('request', (evt) => {
      try {
        onEvent(normalize(JSON.parse(evt.data)))
      } catch (e) {
        // Malformed payload: ignore it rather than tearing down the stream.
      }
    })
    es.addEventListener('error', () => {
      // Close so the browser's own retry does not race our backoff.
      if (es) es.close()
      es = null
      scheduleReconnect()
    })
  }

  function stop() {
    stopped = true
    if (timer) clearTimeout(timer)
    timer = null
    if (es) es.close()
    es = null
    status.value = 'offline'
  }

  // Restart after a login: reset the stopped latch and the backoff, then connect.
  function start() {
    stopped = false
    hadOpened = false
    backoff = BACKOFF_START_MS
    connect()
  }

  onMounted(connect)
  onBeforeUnmount(stop)

  return { status, start, stop }
}
