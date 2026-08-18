<script setup>
import { computed } from 'vue'
import { integer } from '../format.js'

// `guardrails` is summary.guardrails, or null while loading; `failed` means
// the load failed and a dash should replace "Loading…". Blocks read in cherry
// once there are any (a block is a warning), ink at zero.
const props = defineProps({
  guardrails: { type: Object, default: null },
  failed: { type: Boolean, default: false },
})

const blockedCount = computed(() => {
  const n = props.guardrails?.blocked
  return typeof n === 'number' ? n : null
})
const blocked = computed(() => (props.guardrails ? integer(props.guardrails.blocked) : null))
const tone = computed(() => (blockedCount.value > 0 ? 'tone-cherry' : ''))

// "input N · output N" — omitted entirely when blocked_by_rail is empty.
const byRail = computed(() => {
  const rows = props.guardrails?.blocked_by_rail
  if (!Array.isArray(rows) || rows.length === 0) return ''
  return rows.map((r) => `${r.rail} ${integer(r.count)}`).join(' · ')
})

const errors = computed(() => {
  const n = props.guardrails?.errors
  if (typeof n !== 'number' || n <= 0) return ''
  return `${integer(n)} ${n === 1 ? 'error' : 'errors'} (failed open)`
})
</script>

<template>
  <section class="card">
    <p class="kpi-label">guardrail blocks this month</p>
    <p v-if="blocked === null && !failed" class="loading">Loading…</p>
    <p v-else class="kpi-value" :class="tone">{{ blocked ?? '—' }}</p>
    <p v-if="byRail" class="kpi-sub">{{ byRail }}</p>
    <p v-if="errors" class="kpi-sub">{{ errors }}</p>
  </section>
</template>
