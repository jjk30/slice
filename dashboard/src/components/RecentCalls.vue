<script setup>
import { computed } from 'vue'
import { money, time, integer } from '../format.js'

// `rows` are normalized {key, created_at, team, model, routed_from, status,
// cost, cached}; null while loading. Newest first, already capped by the parent.
// `failed` means the load failed and a dash should replace "Loading…".
const props = defineProps({
  rows: { type: Array, default: null },
  failed: { type: Boolean, default: false },
})

// Panel meta, right of the title: how many rows are showing (from the data).
const meta = computed(() => {
  if (!props.rows) return ''
  const n = props.rows.length
  return `${integer(n)} ${n === 1 ? 'call' : 'calls'} · live`
})

// 2xx teal, 4xx amber, 5xx cherry, anything else neutral.
function statusTone(s) {
  if (typeof s !== 'number') return 'pill--neutral'
  if (s >= 500) return 'pill--danger'
  if (s >= 400) return 'pill--warn'
  if (s >= 200 && s < 300) return 'pill--ok'
  return 'pill--neutral'
}
</script>

<template>
  <section class="card panel">
    <div class="panel-head">
      <h2 class="panel-title">Recent calls</h2>
      <span v-if="meta" class="meta">{{ meta }}</span>
    </div>
    <p v-if="rows === null && failed" class="empty">—</p>
    <p v-else-if="rows === null" class="loading">Loading…</p>
    <p v-else-if="rows.length === 0" class="empty">No requests recorded yet.</p>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">time</th>
            <th scope="col">team</th>
            <th scope="col">model</th>
            <th scope="col">status</th>
            <th scope="col" class="num">cost</th>
            <th scope="col"><span class="sr-only">cache</span></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.key">
            <td class="mono muted" :title="r.created_at ?? ''">{{ time(r.created_at) }}</td>
            <td class="mono">{{ r.team ?? '—' }}</td>
            <td class="mono">
              <template v-if="r.routed_from">
                <span class="muted">{{ r.routed_from }}</span>
                <span class="arrow" aria-hidden="true">→</span>
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
  display: inline-block;
  margin: 0 6px;
  color: var(--amber-text);
  font-size: 12px;
}

td .pill {
  padding: 1px 9px;
  font-size: 11px;
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
