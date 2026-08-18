<script setup>
import { money, time } from '../format.js'

// `rows` are normalized {key, created_at, team, model, routed_from, status,
// cost, cached}; null while loading. Newest first, already capped by the parent.
// `failed` means the load failed and a dash should replace "Loading…".
defineProps({
  rows: { type: Array, default: null },
  failed: { type: Boolean, default: false },
})

function statusTone(s) {
  if (typeof s !== 'number') return 'pill--neutral'
  if (s >= 500) return 'pill--danger'
  if (s >= 400) return 'pill--warn'
  return 'pill--neutral'
}
</script>

<template>
  <section class="card">
    <h2 class="card-title">Recent calls</h2>
    <p v-if="rows === null && failed" class="empty">—</p>
    <p v-else-if="rows === null" class="loading">Loading…</p>
    <p v-else-if="rows.length === 0" class="empty">No requests recorded yet.</p>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">Time</th>
            <th scope="col">Team</th>
            <th scope="col">Model</th>
            <th scope="col">Status</th>
            <th scope="col" class="num">Cost</th>
            <th scope="col"><span class="sr-only">Cache</span></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.key">
            <td class="mono" :title="r.created_at ?? ''">{{ time(r.created_at) }}</td>
            <td>{{ r.team ?? '—' }}</td>
            <td class="mono">
              <template v-if="r.routed_from">
                <span class="muted">{{ r.routed_from }}</span>
                <span class="muted arrow" aria-hidden="true"> → </span>
                <span class="sr-only"> routed to </span>
              </template>
              {{ r.model ?? '—' }}
            </td>
            <td>
              <span class="pill" :class="statusTone(r.status)">{{ r.status ?? '—' }}</span>
            </td>
            <td class="num mono">{{ money(r.cost) }}</td>
            <td><span v-if="r.cached" class="badge-cached">cached</span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.arrow {
  font-size: 12px;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
</style>
