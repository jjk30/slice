<script setup>
import { computed } from 'vue'
import { money, compactInt, integer } from '../format.js'

// `data` is the /dashboard/teams payload, or null while loading; `failed`
// means the load failed and a dash should replace "Loading…".
const props = defineProps({
  data: { type: Object, default: null },
  failed: { type: Boolean, default: false },
})

const teams = computed(() => (props.data ? props.data.teams || [] : null))

// Panel meta, right of the title: team count and the cap, all from the payload.
const meta = computed(() => {
  if (!teams.value) return ''
  const n = teams.value.length
  const cap = money(props.data?.budget_usd)
  return `${integer(n)} ${n === 1 ? 'team' : 'teams'} · cap ${cap}`
})

// Fill ratio, colour band and aria values for one team's meter. "Used" is
// budget_used_usd — the live gate counter the cap is enforced against, or the
// recorded spend when Redis is down (budget_source says which). When either
// used or budget is not a finite number the usage is unknown and the meter is
// rendered as indeterminate rather than as an empty (= 0%) bar.
function meterFor(t) {
  const cap = typeof t.budget_usd === 'number' ? t.budget_usd : NaN
  const used = typeof t.budget_used_usd === 'number' ? t.budget_used_usd : NaN
  const warnRatio = Number(props.data?.warn_ratio)
  const known = Number.isFinite(cap) && cap > 0 && Number.isFinite(used)
  const ratio = known ? used / cap : null
  let tone = ''
  if (known && used >= cap) tone = 'meter-fill--danger'
  else if (known && Number.isFinite(warnRatio) && used >= warnRatio * cap) tone = 'meter-fill--warn'
  return {
    known,
    widthPct: known ? Math.max(0, Math.min(ratio, 1)) * 100 : 0,
    tone,
    used,
    cap,
  }
}

function meterTitle(t) {
  if (!meterFor(t).known) return 'Budget usage unknown'
  const recorded = `Recorded spend ${money(t.spend_usd)}`
  if (t.gate_spend_usd === null || t.gate_spend_usd === undefined) {
    return `${recorded} · gate counter unavailable (Redis down), meter uses recorded spend`
  }
  return `${recorded} · gate counter ${money(t.gate_spend_usd)} (what the cap is enforced against)`
}

// A small honest note when the meter is NOT built on the gate counter.
function sourceNote(t) {
  return t.budget_source === 'postgres' ? 'gate counter unavailable, showing recorded spend' : ''
}

function tokensLine(t) {
  const n = t.estimated_tokens_remaining
  if (n === null || n === undefined) return '—'
  return `~${compactInt(n)} tokens left (estimate)`
}

function tokensTitle(t) {
  const n = t.estimated_tokens_remaining
  if (n === null || n === undefined) return 'Not estimable this month'
  return `${integer(n)} tokens (estimate)`
}

const unattributed = computed(() => {
  const u = props.data?.unattributed
  if (!u || !(u.requests > 0)) return ''
  return `${integer(u.requests)} ${u.requests === 1 ? 'request' : 'requests'} (${money(u.spend_usd)}) predate team tracking`
})
</script>

<template>
  <section class="card panel">
    <div class="panel-head">
      <h2 class="panel-title">Budgets per team</h2>
      <span v-if="meta" class="meta">{{ meta }}</span>
    </div>
    <p v-if="teams === null && failed" class="empty">—</p>
    <p v-else-if="teams === null" class="loading">Loading…</p>
    <p v-else-if="teams.length === 0" class="empty">No team spend this month yet.</p>
    <ul v-else class="team-list">
      <li v-for="t in teams" :key="t.team" class="team">
        <div class="team-head">
          <span class="team-name mono">{{ t.team ?? '—' }}</span>
          <span class="mono small amounts">
            <span class="used">{{ money(t.budget_used_usd) }}</span>
            <span class="muted"> used of {{ money(t.budget_usd) }} · </span>
            <span class="left">{{ money(t.remaining_usd) }} left</span>
          </span>
        </div>
        <div
          class="meter"
          :class="{ 'meter--unknown': !meterFor(t).known }"
          role="progressbar"
          :aria-label="`${t.team} budget used`"
          :aria-valuenow="meterFor(t).known ? meterFor(t).used : undefined"
          aria-valuemin="0"
          :aria-valuemax="meterFor(t).known ? meterFor(t).cap : undefined"
          :title="meterTitle(t)"
        >
          <div class="meter-fill" :class="meterFor(t).tone" :style="{ width: meterFor(t).widthPct + '%' }"></div>
        </div>
        <p class="mono small tokens" :title="tokensTitle(t)">
          <span class="estimate">{{ tokensLine(t) }}</span><span v-if="sourceNote(t)" class="muted"> · {{ sourceNote(t) }}</span>
        </p>
      </li>
    </ul>
    <p v-if="unattributed" class="mono small muted unattributed">{{ unattributed }}</p>
  </section>
</template>

<style scoped>
.team-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.team-head {
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

.unattributed {
  margin: 18px 0 0;
}
</style>
