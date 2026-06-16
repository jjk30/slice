<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import { api } from "./api.js";
import Logo from "./components/Logo.vue";
import KpiCards from "./components/KpiCards.vue";
import SpendChart from "./components/SpendChart.vue";
import ModelBreakdown from "./components/ModelBreakdown.vue";
import RecentCalls from "./components/RecentCalls.vue";
import WhenToSwitch from "./components/WhenToSwitch.vue";

const DAYS = 30;
const REFRESH_MS = 20_000;

const loading = ref(true); // only true on the very first load
const error = ref("");
const summary = ref(null);
const daily = ref([]);
const models = ref([]);
const recent = ref([]);
const budgets = ref([]);
const suggestions = ref([]);
const totalPotentialUsd = ref(null);

let timer = null;

async function load(initial = false) {
  if (initial) loading.value = true;
  try {
    const [s, d, m, r, b, sug] = await Promise.all([
      api.summary(DAYS),
      api.spendDaily(DAYS),
      api.spendByModel(DAYS),
      api.recent(8),
      api.budgets(),
      api.suggestions(DAYS),
    ]);
    summary.value = s;
    daily.value = d.series;
    models.value = m.models;
    recent.value = r.requests;
    budgets.value = b.teams;
    suggestions.value = sug.suggestions;
    totalPotentialUsd.value = sug.totalPotentialUsd;
    error.value = "";
  } catch (e) {
    error.value = e?.message || "Could not reach the gateway.";
  } finally {
    loading.value = false;
  }
}

// "No data yet" = the requests table is empty (recent is all-time, the most
// reliable signal of a fresh DB). With rows present we always show the
// dashboard, even if the selected window happens to be all-zeros.
const isEmpty = computed(() => !error.value && summary.value && recent.value.length === 0);
const ready = computed(() => !loading.value && !error.value && summary.value && !isEmpty.value);

// Real current month, e.g. "June 2026 · live".
const periodLabel = computed(
  () => new Date().toLocaleDateString("en-US", { month: "long", year: "numeric" }) + " · live",
);

onMounted(() => {
  load(true);
  timer = setInterval(() => load(false), REFRESH_MS);
});
onBeforeUnmount(() => clearInterval(timer));
</script>

<template>
  <div class="wrap">
    <div class="top">
      <div class="brand">
        <Logo />
        <b>slice</b><span class="sep">/</span><span class="t">dashboard</span>
      </div>
      <span class="period"><span class="d" />{{ periodLabel }}</span>
    </div>

    <!-- loading -->
    <div v-if="loading" class="state">
      <div class="spinner" />
      <div>Loading your spend…</div>
    </div>

    <!-- error -->
    <div v-else-if="error" class="state">
      <h2>Can't reach the gateway</h2>
      <p>{{ error }}</p>
      <code>Is slice running on the API base URL? (see dashboard/.env.example)</code>
    </div>

    <!-- empty: fresh DB, no requests yet -->
    <div v-else-if="isEmpty" class="state">
      <h2>No requests yet</h2>
      <p>Once traffic flows through slice, your spend, savings, and suggestions show up here.</p>
      <code>ANTHROPIC_BASE_URL=http://localhost:8080 claude</code>
    </div>

    <!-- dashboard -->
    <template v-else-if="ready">
      <KpiCards :summary="summary" :budgets="budgets" />

      <div class="grid">
        <div class="panel">
          <div class="ph"><h2>AI usage</h2><span class="meta">daily spend, last {{ summary.range.days }} days</span></div>
          <p class="sub">Daily spend, every request metered live as it passes through slice.</p>
          <SpendChart :series="daily" />
          <ModelBreakdown :models="models" />
          <RecentCalls :requests="recent" />
        </div>

        <WhenToSwitch :suggestions="suggestions" :total-potential-usd="totalPotentialUsd" />
      </div>
    </template>
  </div>
</template>
