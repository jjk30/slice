<script setup>
import { computed } from "vue";
import { usd, pct, int } from "../format.js";

const props = defineProps({
  summary: { type: Object, required: true },
  budgets: { type: Array, default: () => [] },
});

// Aggregate budget across all teams for the headline Budget card (mockup: one
// "spend / limit" with a bar).
const budget = computed(() => {
  const spend = props.budgets.reduce((s, t) => s + (t.spendUsd || 0), 0);
  const limit = props.budgets.reduce((s, t) => s + (t.limitUsd || 0), 0);
  return { spend, limit, used: limit > 0 ? (spend / limit) * 100 : 0 };
});
</script>

<template>
  <div class="kpis">
    <div class="kpi">
      <div class="l">AI spend this month</div>
      <div class="v">{{ usd(summary.spendUsd) }}</div>
      <div class="s">counted live, through slice</div>
    </div>

    <div class="kpi">
      <div class="l">Saved so far</div>
      <div class="v pos">{{ usd(summary.savedUsd) }}</div>
      <div class="s">{{ pct(summary.savedPct) }} less than going direct</div>
    </div>

    <div class="kpi">
      <div class="l">Budget</div>
      <div class="v">{{ usd(budget.spend) }}<span class="vsub"> / {{ usd(budget.limit) }}</span></div>
      <div class="bar"><i :style="{ width: Math.min(100, budget.used) + '%' }" /></div>
    </div>

    <div class="kpi">
      <div class="l">Requests</div>
      <div class="v">{{ int(summary.requestCount) }}</div>
      <div class="s">{{ int(summary.cacheHitCount) }} served from cache</div>
    </div>
  </div>
</template>
