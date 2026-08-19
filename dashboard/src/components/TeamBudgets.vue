<script setup>
import { computed } from 'vue'
import { money, compactInt, integer, percent } from '../format.js'

// `data` is the /dashboard/teams payload, or null while loading; `failed`
// means the load failed and a dash should replace "Loading…".
//
// Phase 12: the budget cap is per *account*, so there is one meter (`data.budget`),
// and `data.teams` is the per-label breakdown (each label's spend and its share of the
// account total) shown beneath it.
const props = defineProps({
  data: { type: Object, default: null },
  failed: { type: Boolean, default: false },
})

const budget = computed(() => (props.data ? props.data.budget || null : null))
const teams = computed(() => (props.data ? props.data.teams || [] : null))

const meta = computed(() => {
  if (!props.data) return ''
  const cap = money(props.data.budget_usd)
  return `account · cap ${cap}`
})

// The one account meter: fill ratio, colour band, and aria values. "Used" is
// budget_used_usd — the live gate counter the cap is enforced against, or the recorded
// spend when Redis is down (budget_source says which). Unknown → indeterminate.
const meter = computed(() => {
  const b = budget.value
  if (!b) return { known: false, widthPct: 0, tone: '', used: NaN, cap: NaN }
  const cap = typeof b.budget_usd === 'number' ? b.budget_usd : NaN
  const used = typeof b.budget_used_usd === 'number' ? b.budget_used_usd : NaN
  const warnRatio = Number(props.data?.warn_ratio)
  const known = Number.isFinite(cap) && cap > 0 && Number.isFinite(used)
  const ratio = known ? used / cap : null
  let tone = ''
  if (known && used >= cap) tone = 'meter-fill--danger'
  else if (known && Number.isFinite(warnRatio) && used >= warnRatio * cap) tone = 'meter-fill--warn'
  return { known, widthPct: known ? Math.max(0, Math.min(ratio, 1)) * 100 : 0, tone, used, cap }
})

const meterTitle = computed(() => {
  const b = budget.value
  if (!b || !meter.value.known) return 'Budget usage unknown'
  const recorded = `Recorded spend ${money(b.spend_usd)}`
  if (b.gate_spend_usd === null || b.gate_spend_usd === undefined) {
    return `${recorded} · gate counter unavailable (Redis down), meter uses recorded spend`
  }
  return `${recorded} · gate counter ${money(b.gate_spend_usd)} (what the cap is enforced against)`
})

const sourceNote = computed(() =>
  budget.value?.budget_source === 'postgres' ? 'gate counter unavailable, showing recorded spend' : ''
)

const tokensLine = computed(() => {
  const n = budget.value?.estimated_tokens_remaining
  if (n === null || n === undefined) return '—'
  return `~${compactInt(n)} tokens left (estimate)`
})

const unattributed = computed(() => {
  const u = props.data?.unattributed
  if (!u || !(u.requests > 0)) return ''
  return `${integer(u.requests)} ${u.requests === 1 ? 'request' : 'requests'} (${money(u.spend_usd)}) predate team tracking`
})

function shareLine(t) {
  if (t.share === null || t.share === undefined) return ''
  return percent(t.share)
}
</script>

<template>
  <section class="card panel">
    <div class="panel-head">
      <h2 class="panel-title">Account budget</h2>
      <span v-if="meta" class="meta">{{ meta }}</span>
    </div>
    <p v-if="budget === null && failed" class="empty">—</p>
    <p v-else-if="budget === null" class="loading">Loading…</p>
    <template v-else>
      <div class="account-head">
        <span class="team-name mono">{{ budget.account ?? 'account' }}</span>
        <span class="mono small amounts">
          <span class="used">{{ money(budget.budget_used_usd) }}</span>
          <span class="muted"> used of {{ money(budget.budget_usd) }} · </span>
          <span class="left">{{ money(budget.remaining_usd) }} left</span>
        </span>
      </div>
      <div
        class="meter"
        :class="{ 'meter--unknown': !meter.known }"
        role="progressbar"
        aria-label="account budget used"
        :aria-valuenow="meter.known ? meter.used : undefined"
        aria-valuemin="0"
        :aria-valuemax="meter.known ? meter.cap : undefined"
        :title="meterTitle"
      >
        <div class="meter-fill" :class="meter.tone" :style="{ width: meter.widthPct + '%' }"></div>
      </div>
      <p class="mono small tokens">
        <span class="estimate">{{ tokensLine }}</span><span v-if="sourceNote" class="muted"> · {{ sourceNote }}</span>
      </p>

      <div v-if="teams && teams.length" class="labels">
        <div class="labels-head mono small muted">by team label</div>
        <ul class="team-list">
          <li v-for="t in teams" :key="t.team" class="label-row mono small">
            <span class="team-name">{{ t.team ?? '—' }}</span>
            <span class="muted">{{ money(t.spend_usd) }}<span v-if="shareLine(t)"> · {{ shareLine(t) }}</span></span>
          </li>
        </ul>
      </div>
      <p v-if="unattributed" class="mono small muted unattributed">{{ unattributed }}</p>
    </template>
  </section>
</template>

<style scoped>
.account-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.team-name {
  font-weight: 500;
  font-size: 13px;
}

.amounts .used {
  color: var(--ink);
}

.amounts .left {
  color: var(--teal-text);
}

.tokens {
  margin: 8px 0 0;
  color: var(--muted);
}

.tokens .estimate {
  color: var(--amber-text);
}

.labels {
  margin-top: 18px;
  border-top: 1px solid var(--line);
  padding-top: 12px;
}

.labels-head {
  margin-bottom: 8px;
}

.team-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.label-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.unattributed {
  margin: 18px 0 0;
}
</style>
