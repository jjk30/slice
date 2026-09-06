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

// "{email} email, {gateway} gateway": who blocked (the email assistant's rails or the
// gateway's), from summary.guardrails.blocked_by_source. "none yet" when both are 0.
// Empty (no line) until the summary has loaded a block count at all.
const bySource = computed(() => {
  if (blockedCount.value === null) return ''
  const split = props.guardrails?.blocked_by_source || {}
  const email = typeof split.email === 'number' ? split.email : 0
  const gateway = typeof split.gateway === 'number' ? split.gateway : 0
  if (email === 0 && gateway === 0) return 'none yet'
  return `${integer(email)} email, ${integer(gateway)} gateway`
})

// "input N · output N", omitted entirely when blocked_by_rail is empty.
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
    <p v-else class="kpi-value" :class="tone">{{ blocked ?? '\u2014' }}</p>
    <p v-if="bySource" class="kpi-sub by-source">{{ bySource }}</p>
    <p v-if="byRail" class="kpi-sub">{{ byRail }}</p>
    <p v-if="errors" class="kpi-sub">{{ errors }}</p>
  </section>
</template>
