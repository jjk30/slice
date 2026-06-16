<script setup>
import { computed } from "vue";
import { usd, prettyModel } from "../format.js";

const props = defineProps({
  models: { type: Array, default: () => [] }, // [{ model, requests, spendUsd }]
});

// Mockup dot/track palette, applied to real models in spend order.
const PALETTE = ["#E2535F", "#0F6E56", "#C07C1E", "#8C5A2B", "#A7C77E"];

const rows = computed(() => {
  const max = Math.max(0, ...props.models.map((m) => m.spendUsd));
  return props.models.map((m, i) => ({
    name: prettyModel(m.model),
    color: PALETTE[i % PALETTE.length],
    // width relative to the biggest spender; $0 rows render an empty track + a
    // green "$0" amount, exactly like the mockup's free row.
    width: m.spendUsd > 0 && max > 0 ? Math.max(3, (m.spendUsd / max) * 100) : 0,
    amount: usd(m.spendUsd),
    free: m.spendUsd <= 0,
  }));
});
</script>

<template>
  <div class="models">
    <div class="empty-mini" v-if="!rows.length">No model spend in this range yet.</div>
    <div class="mrow" v-for="r in rows" :key="r.name">
      <span class="nm"><span class="dot" :style="{ background: r.color }" />{{ r.name }}</span>
      <span class="track"><i :style="{ width: r.width + '%', background: r.color }" /></span>
      <span class="amt" :class="{ free: r.free }">{{ r.amount }}</span>
    </div>
  </div>
</template>
