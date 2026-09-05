<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { setExpected, AuthError } from '../api.js'
import { session } from '../auth.js'

// Phase 24b: the newest scan run's findings, with a switch per row to mark one as
// expected ("this is on purpose, stop emailing me"). `data` is the /scanner/findings
// payload, or null while loading; `failed` means the load failed and a dash replaces
// "Loading…". The switch calls the gateway and flips the row in place on success; a
// failure leaves the row as it was and says so under the table.
const props = defineProps({
  data: { type: Object, default: null },
  failed: { type: Boolean, default: false },
})

const rows = ref(null)
const error = ref('')
// Rows with a request in flight, keyed like the rows, so a double click cannot race.
const busy = reactive({})

watch(
  () => props.data,
  (d) => {
    rows.value = d
      ? (d.findings || []).map((f) => ({ ...f, key: `${f.check}:${f.resource_id}` }))
      : null
  },
  { immediate: true },
)

const expectedCount = computed(() => (rows.value || []).filter((r) => r.expected).length)
const meta = computed(() => {
  if (!rows.value || rows.value.length === 0) return ''
  const n = rows.value.length
  const parts = [`${n} ${n === 1 ? 'finding' : 'findings'}`]
  if (expectedCount.value > 0) parts.push(`${expectedCount.value} expected`)
  return parts.join(' · ')
})

const SEVERITY_CLASS = { high: 'pill--danger', med: 'pill--warn', low: 'pill--neutral' }
const SEVERITY_LABEL = { high: 'high', med: 'medium', low: 'low' }

async function toggle(row) {
  if (busy[row.key]) return
  const next = !row.expected
  busy[row.key] = true
  error.value = ''
  try {
    await setExpected(row.check, row.resource_id, next)
    row.expected = next
  } catch (e) {
    if (e instanceof AuthError) {
      session.value = null
    } else {
      error.value = e && e.message ? e.message : 'Could not update the finding.'
    }
  } finally {
    busy[row.key] = false
  }
}
</script>

<template>
  <section class="card panel">
    <div class="panel-head">
      <h2 class="panel-title">AWS findings</h2>
      <span v-if="meta" class="meta">{{ meta }}</span>
    </div>
    <p v-if="rows === null && failed" class="empty">&mdash;</p>
    <p v-else-if="rows === null" class="loading">Loading…</p>
    <p v-else-if="rows.length === 0" class="empty">
      No findings yet. The first scan runs within an hour of connecting AWS.
    </p>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>severity</th>
            <th>finding</th>
            <th>resource</th>
            <th class="num">expected</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.key" :class="{ 'is-expected': row.expected }">
            <td>
              <span class="pill" :class="SEVERITY_CLASS[row.severity] || 'pill--neutral'">
                {{ SEVERITY_LABEL[row.severity] || row.severity }}
              </span>
            </td>
            <td class="title">{{ row.title || row.summary }}</td>
            <td class="mono resource">{{ row.resource_id }}</td>
            <td class="num">
              <label class="switch" :class="{ on: row.expected, busy: busy[row.key] }">
                <input
                  type="checkbox"
                  :checked="row.expected"
                  :disabled="busy[row.key]"
                  :aria-label="`expected: ${row.resource_id}`"
                  @change="toggle(row)"
                />
                <span class="track" aria-hidden="true"><span class="knob"></span></span>
                <span class="switch-text mono">expected</span>
              </label>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-if="error" class="panel-error mono small" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
/* The finding line is a sentence; let it wrap instead of forcing one long row. */
td.title {
  white-space: normal;
  min-width: 260px;
  line-height: 1.45;
}

td.resource {
  font-size: 12px;
  color: var(--muted);
}

/* An expected row stays in the list but reads as settled. */
tr.is-expected td {
  color: var(--muted);
}

tr.is-expected td.title,
tr.is-expected td.resource,
tr.is-expected .pill {
  opacity: 0.55;
}

.switch {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
}

.switch input {
  position: absolute;
  opacity: 0;
  width: 1px;
  height: 1px;
}

.track {
  position: relative;
  width: 30px;
  height: 16px;
  border-radius: 999px;
  background: var(--line-strong);
  transition: background 0.14s ease;
  flex: 0 0 auto;
}

.knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--card);
  transition: transform 0.14s ease;
}

.switch.on .track {
  background: var(--teal);
}

.switch.on .knob {
  transform: translateX(14px);
}

.switch.busy {
  opacity: 0.6;
  cursor: progress;
}

.switch input:focus-visible + .track {
  outline: 2px solid var(--teal);
  outline-offset: 2px;
}

.switch-text {
  font-size: 12px;
  color: var(--muted);
}

.panel-error {
  margin: 8px 0 0;
  color: var(--cherry-text);
}
</style>
