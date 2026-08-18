<script setup>
// A single stat tile. `value` is already formatted by the parent; when it is
// null/undefined we are still loading and show a muted "Loading…", unless the
// load failed, in which case a dash is shown instead.
defineProps({
  label: { type: String, required: true },
  value: { type: String, default: null },
  sub: { type: String, default: '' },
  failed: { type: Boolean, default: false },
})
</script>

<template>
  <section class="card">
    <p class="kpi-label">{{ label }}</p>
    <p v-if="(value === null || value === undefined) && !failed" class="loading">Loading…</p>
    <p v-else class="kpi-value">{{ value ?? '—' }}</p>
    <p v-if="sub" class="kpi-sub">{{ sub }}</p>
  </section>
</template>
