<script setup>
// A single stat card: small mono label, big display number, small mono subline.
// `value` is already formatted by the parent; when it is null/undefined we are
// still loading and show a muted "Loading…", unless the load failed, in which
// case a dash is shown instead. `tone` colours the number: cherry for spend,
// teal for savings, default ink otherwise. `tint` picks the soft card background
// (lavender / green / bluegrey / amber / rose); none means a plain white card, and
// 'white' is the explicit white card with the standard border (the AWS bill tile).
// `icon` is an optional image shown before the label, 18px tall at its own ratio,
// with `iconAlt` as its alt text; the other tiles leave it out and render as before.
defineProps({
  label: { type: String, required: true },
  value: { type: String, default: null },
  sub: { type: String, default: '' },
  failed: { type: Boolean, default: false },
  tone: { type: String, default: '' },
  tint: { type: String, default: '' },
  icon: { type: String, default: '' },
  iconAlt: { type: String, default: '' },
})
</script>

<template>
  <section class="card" :class="tint ? `tint-${tint}` : ''">
    <p class="kpi-label" :class="{ 'with-icon': icon }">
      <img v-if="icon" class="kpi-icon" :src="icon" :alt="iconAlt" />{{ label }}
    </p>
    <p v-if="(value === null || value === undefined) && !failed" class="loading">Loading…</p>
    <p v-else class="kpi-value" :class="tone ? `tone-${tone}` : ''">{{ value ?? '\u2014' }}</p>
    <p v-if="sub" class="kpi-sub">{{ sub }}</p>
  </section>
</template>
