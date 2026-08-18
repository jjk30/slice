<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { getJson } from './api.js'
import { useLiveEvents } from './live.js'
import { money, percent, integer } from './format.js'
import KpiTile from './components/KpiTile.vue'
import LivePill from './components/LivePill.vue'
import TeamBudgets from './components/TeamBudgets.vue'
import ModelsChart from './components/ModelsChart.vue'
import RecentCalls from './components/RecentCalls.vue'
import GuardrailsTile from './components/GuardrailsTile.vue'

const RECENT_LIMIT = 20
const REFRESH_DEBOUNCE_MS = 400

// Raw API payloads; null until the first successful load.
const summary = ref(null)
const models = ref(null)
const teams = ref(null)
const recent = ref(null)
const error = ref('')
// True after a load failed: cards render dashes instead of "Loading…".
const failed = computed(() => Boolean(error.value))

// ---- loading ---------------------------------------------------------------

// Monotonic sequence so an older in-flight refresh cannot overwrite a newer one.
// Only the refresh that actually applies its result may clear the error banner:
// a stale one finishing after a newer failure must not hide that failure.
let aggSeq = 0
// Whether /dashboard/recent has ever been loaded from the record book. Live rows
// alone don't count: until the DB list has been fetched the table is incomplete.
let recentLoaded = false

async function loadAggregates() {
  const seq = ++aggSeq
  const [s, m, t] = await Promise.all([
    getJson('/dashboard/summary'),
    getJson('/dashboard/models'),
    getJson('/dashboard/teams'),
  ])
  if (seq !== aggSeq) return false
  summary.value = s
  models.value = m
  teams.value = t
  error.value = ''
  return true
}

function normalizeRecent(r) {
  return {
    key: `db:${r.id}`,
    created_at: r.created_at,
    team: r.team ?? null,
    model: r.model ?? null,
    routed_from: r.routed_from ?? null,
    status: r.status ?? null,
    cost: r.cost_usd ?? null,
    cached: Boolean(r.cached),
  }
}

async function loadRecent() {
  const body = await getJson(`/dashboard/recent?limit=${RECENT_LIMIT}`)
  const fetched = (body.requests || []).map(normalizeRecent)
  // Fetched (DB) rows are canonical and go first so their keys win; any live
  // rows that arrived while the fetch was in flight are kept unless they match
  // a DB row (see sameCall).
  recent.value = mergeRecent(fetched, recent.value || [])
  recentLoaded = true
}

// A failed load shows the banner and dashes: aggregates are cleared so no
// stale figure is presented as current. Live rows in `recent` are kept.
function markFailed(e) {
  error.value = e && e.message ? e.message : 'Backend unavailable.'
  summary.value = null
  models.value = null
  teams.value = null
}

async function loadAll() {
  try {
    await Promise.all([loadAggregates(), loadRecent()])
  } catch (e) {
    markFailed(e)
  }
}

// ---- recent list merging ----------------------------------------------------

function isDbRow(r) {
  return String(r.key).startsWith('db:')
}

// A live row (key = request_id) and its DB counterpart (key = db:N) describe
// the same call when every field matches and the timestamps are identical: the
// gateway stamps one instant on both the event and the Postgres row, at
// microsecond resolution, so equality is exact rather than a guessed window.
// Rows from the same source are only ever deduped by key.
function sameCall(a, b) {
  if (isDbRow(a) === isDbRow(b)) return false
  if (a.created_at !== b.created_at) return false
  if (a.team !== b.team || a.model !== b.model || a.status !== b.status) return false
  return a.routed_from === b.routed_from && a.cached === b.cached
}

function mergeRecent(existing, incoming) {
  const out = []
  const seen = new Set()
  for (const row of [...existing, ...incoming]) {
    if (seen.has(row.key)) continue
    if (out.some((o) => sameCall(o, row))) continue
    seen.add(row.key)
    out.push(row)
  }
  out.sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0))
  return out.slice(0, RECENT_LIMIT)
}

// ---- live events ------------------------------------------------------------

let refreshTimer = null

// Debounce: a burst of events triggers a single refresh ~400ms after the last one.
function scheduleRefresh() {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = setTimeout(async () => {
    refreshTimer = null
    try {
      // Also fetch /recent if the record-book list never loaded (e.g. Postgres was
      // down at mount): live rows alone would make the table look complete.
      await Promise.all([loadAggregates(), recentLoaded ? Promise.resolve() : loadRecent()])
    } catch (e) {
      markFailed(e)
    }
  }, REFRESH_DEBOUNCE_MS)
}

const { status: liveStatus } = useLiveEvents(
  (row) => {
    recent.value = mergeRecent([row], recent.value || [])
    scheduleRefresh()
  },
  // The stream (re)opened. After an outage events may have been missed, so resync.
  // On the very first open only resync if the mount-time load already finished and
  // failed (or never got the record-book list) — the page may have been opened while
  // the gateway was still starting. If that load is still in flight there is nothing
  // to refetch yet; if it later fails, the next event's refresh retries.
  (firstOpen) => {
    if (!firstOpen) loadAll()
    else if (initialLoadDone && (error.value || !recentLoaded)) loadAll()
  },
)

let initialLoadDone = false
onMounted(() => { loadAll().finally(() => { initialLoadDone = true }) })
onBeforeUnmount(() => {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = null
})

// ---- derived display values -----------------------------------------------

const month = computed(() => summary.value?.month ?? teams.value?.month ?? models.value?.month ?? null)

// null while loading; a dash once a load has failed.
function kpi(fmt) {
  if (summary.value) return fmt(summary.value)
  return failed.value ? '—' : null
}

const spend = computed(() => kpi((s) => money(s.spend_usd)))
// Spend only sums costs the pricing table knows. When served requests carried an
// unknown price, say so next to the total rather than let it read as complete.
const spendSub = computed(() => {
  const n = summary.value?.unpriced_requests
  if (typeof n !== 'number' || n <= 0) return ''
  return `${integer(n)} served ${n === 1 ? 'request' : 'requests'} unpriced, not included`
})
const saved = computed(() => kpi((s) => money(s.savings_usd)))
const requests = computed(() => kpi((s) => integer(s.requests)))
const requestsSub = computed(() => {
  if (!summary.value) return ''
  return `${integer(summary.value.cache_hits)} cached · ${integer(summary.value.routed_down)} routed down`
})
const passRate = computed(() => kpi((s) => percent(s.eval?.pass_rate)))
const passRateSub = computed(() => {
  if (!summary.value) return ''
  return `${integer(summary.value.eval?.count)} scored`
})
// A loaded summary with no guardrails object renders dashes, not "Loading…".
const guardrails = computed(() => (summary.value ? summary.value.guardrails ?? {} : null))
</script>

<template>
  <div class="page">
    <header class="header">
      <div class="brand">
        <img class="brand-logo" src="/favicon.png" alt="" width="24" height="24" />
        <h1 class="brand-name">slice</h1>
        <span class="brand-path">/ dashboard</span>
      </div>
      <div class="header-right">
        <span class="meta">this month · {{ month ?? '—' }}</span>
        <LivePill :status="liveStatus" />
      </div>
    </header>

    <div v-if="error" class="banner" role="alert">
      Backend unavailable — {{ error }}
    </div>

    <main class="grid">
      <KpiTile label="spend this month" :value="spend" :sub="spendSub" :failed="failed" tone="cherry" />
      <KpiTile label="saved this month" :value="saved" :failed="failed" tone="teal" />
      <KpiTile label="requests" :value="requests" :sub="requestsSub" :failed="failed" />
      <KpiTile label="eval pass rate" :value="passRate" :sub="passRateSub" :failed="failed" />

      <TeamBudgets class="span-2" :data="teams" :failed="failed" />
      <ModelsChart class="span-2" :data="models" :failed="failed" />

      <RecentCalls class="span-4" :rows="recent" :failed="failed" />

      <GuardrailsTile :guardrails="guardrails" :failed="failed" />
    </main>
  </div>
</template>
